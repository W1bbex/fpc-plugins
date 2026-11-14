"""Core domain logic, Hub API client, binary installer, and update evaluator."""

from __future__ import annotations

import ast
import copy
from datetime import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import threading
import uuid
from typing import Any, Callable, Optional

import requests

from .catalog import fetch_catalog

from .runtime import (
    API_BASE,
    CREDITS,
    LOADER_HEADER,
    NO_PROXY,
    PATHS,
    PLUGIN_FS_LOCK,
    SUPPORTED_PLUGIN_EXTENSIONS,
    UUID as HUB_UUID,
    VERSION,
    logger,
)
from .storage import (
    atomic_write_bytes,
    load_data,
    queue_pending_deletion,
    update_data,
)


def safe_json(resp: Any) -> Optional[dict[str, Any]]:
    """Parse HTTP JSON safely, returning None on non-JSON or non-dict payloads."""
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError, AttributeError):
        return None


def _extract_json_error(raw_text: str) -> Optional[str]:
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        return str(parsed.get("error") or "Ошибка сервера.")
    return None


def _clean_segment(raw: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw or "").strip()).strip("._")


def sanitize_segment(raw: Any, fallback: str) -> str:
    """Sanitize a filename or directory component against path traversal."""
    cleaned = _clean_segment(raw)
    if cleaned and cleaned not in (".", ".."):
        return cleaned
    safe_fallback = _clean_segment(fallback)
    return safe_fallback if (safe_fallback and safe_fallback not in (".", "..")) else "item"


def plugin_label_from_info(plugin_id: str, plugin_info: Optional[dict[str, Any]]) -> str:
    """Derive a safe plugin stub name from catalog metadata or plugin ID."""
    raw_name = str(plugin_info.get("name", "")).split(" ")[0] if plugin_info else ""
    return sanitize_segment(raw_name, sanitize_segment(plugin_id, "plugin").capitalize())


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if latest is strictly greater than current."""
    norm_latest = str(latest or "0").strip().lstrip("vV")
    norm_current = str(current or "0").strip().lstrip("vV")
    if norm_latest == norm_current:
        return False

    try:
        from packaging.version import InvalidVersion, parse as parse_version
    except ImportError:
        InvalidVersion = None  # type: ignore[assignment,misc]
        parse_version = None  # type: ignore[assignment]

    if parse_version is not None:
        try:
            return parse_version(norm_latest) > parse_version(norm_current)
        except InvalidVersion:  # type: ignore[misc]
            pass

    pre_ranks = {"dev": -4, "a": -3, "alpha": -3, "b": -2, "beta": -2, "rc": -1, "c": -1}
    semver_re = re.compile(
        r"^([0-9]+(?:\.[0-9]+)*)(?:[._-]?(dev|alpha|beta|rc|a|b|c)[._-]?([0-9]*))?$",
        re.IGNORECASE,
    )

    def _key(ver: str) -> tuple[Any, ...]:
        m = semver_re.match(ver)
        if m:
            raw_nums = [int(part) for part in m.group(1).split(".")]
            while len(raw_nums) > 1 and raw_nums[-1] == 0:
                raw_nums.pop()
            nums = tuple(raw_nums)
            tag = (m.group(2) or "").lower()
            if not tag:
                return (1, nums, 0, 0)
            return (1, nums, pre_ranks[tag], int(m.group(3) or 0))
        tokens = [
            (0, int(tok)) if tok.isdigit() else (-1, tok.lower())
            for tok in re.split(r"[._-]+", ver)
            if tok
        ]
        return (0, tuple(tokens))

    return _key(norm_latest) > _key(norm_current)


def get_plugins(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch catalog metadata through the shared status-aware cache."""
    return fetch_catalog(force_refresh=force_refresh)


def get_sys_info() -> dict[str, str]:
    """Collect normalized host platform metadata."""
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "hub_version": VERSION,
    }


def get_funpay_identity(cardinal: Any) -> dict[str, str]:
    """Extract FunPay identity cleanly without turning missing IDs into 'None'."""
    account = getattr(cardinal, "account", None)
    user_id = ""
    username = "-"
    if account is not None:
        raw_id = getattr(account, "id", None)
        if raw_id in (None, ""):
            raw_id = getattr(account, "user_id", None)
        if raw_id not in (None, ""):
            user_id = str(raw_id).strip()
        raw_name = getattr(account, "username", None)
        if raw_name not in (None, ""):
            username = str(raw_name).strip() or "-"
    return {
        "id": user_id,
        "funpay_user_id": user_id,
        "username": username,
        "funpay_username": username,
    }


def get_telegram_identity(user: Any) -> dict[str, str]:
    """Extract Telegram identity safely without turning None into 'None'."""
    raw_id = getattr(user, "id", None)
    raw_name = getattr(user, "username", None)
    tg_id = str(raw_id).strip() if raw_id not in (None, "") else ""
    tg_user = str(raw_name).strip().lstrip("@") if raw_name not in (None, "") else ""
    return {
        "id": tg_id,
        "telegram_user_id": tg_id,
        "username": tg_user,
        "telegram_username": tg_user,
    }


def fetch_hub_latest_version(cardinal: Any, tg_user: Any) -> tuple[str, bool]:
    """Query /hub/v1/init and return (latest_version, has_newer_hub_version)."""
    fp_ident = get_funpay_identity(cardinal)
    tg_ident = get_telegram_identity(tg_user)
    payload = {
        "funpay_id": fp_ident["id"] or "unknown",
        "funpay_username": fp_ident["username"],
        "telegram_id": tg_ident["id"],
        "telegram_username": tg_ident["username"],
        "current_version": VERSION,
    }
    try:
        resp = requests.post(f"{API_BASE}/init", json=payload, timeout=5, proxies=NO_PROXY)
        if resp.status_code == 200:
            data = safe_json(resp)
            if data:
                latest = str(data.get("latest_version", VERSION))
                return latest, is_newer_version(latest, VERSION)
    except requests.RequestException as exc:
        logger.error("Hub init error: %s", exc)
    return VERSION, False


def request_trial_key(cardinal: Any, plugin_name: str, tg_user: Any) -> tuple[bool, str]:
    """Call /hub/v1/trial and return (success, key_or_error_message)."""
    fp_ident = get_funpay_identity(cardinal)
    tg_ident = get_telegram_identity(tg_user)
    payload = {
        "plugin_name": plugin_name,
        "funpay_id": fp_ident["id"] or "unknown",
        "funpay_username": fp_ident["username"],
        "telegram_id": tg_ident["id"],
        "telegram_username": tg_ident["username"],
    }
    try:
        resp = requests.post(f"{API_BASE}/trial", json=payload, timeout=10, proxies=NO_PROXY)
    except requests.RequestException as exc:
        return False, f"Ошибка сети: {exc}"

    data = safe_json(resp)
    if resp.status_code != 200 or not data:
        return False, str((data or {}).get("error", f"HTTP {resp.status_code}"))
    if not data.get("success", False):
        return False, str(data.get("error", "Ошибка проверки"))

    trial_key = str(data.get("key", "")).strip()
    if not trial_key:
        return False, "Сервер вернул пустой ключ"
    return True, trial_key


def remove_hub_stub(stub_path: Optional[str]) -> tuple[bool, Optional[str]]:
    """Safely verify and delete a Hub-generated loader stub."""
    if not stub_path or not os.path.exists(stub_path):
        return True, None
    try:
        with open(stub_path, "r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError as exc:
        logger.error("Failed reading stub %s: %s", stub_path, exc)
        return False, f"Ошибка чтения лоадера: {exc}"

    if LOADER_HEADER not in first_line:
        logger.warning("Refusing to remove %s: not a W1bbex Hub loader", stub_path)
        return False, "Файл не является лоадером хаба."
    try:
        os.remove(stub_path)
        return True, None
    except OSError as exc:
        logger.error("Failed removing stub %s: %s", stub_path, exc)
        return False, f"Не удалось удалить файл лоадера: {exc}"


def clean_old_plugin(plugin_name: str, new_stub_path: Optional[str] = None) -> None:
    """Remove old stub file when a plugin's stub path changes during an update."""
    installed = load_data().get("installed")
    entry = installed.get(plugin_name) if isinstance(installed, dict) else None
    old_stub = entry.get("stub_path") if isinstance(entry, dict) else None
    if old_stub and (
        new_stub_path is None
        or os.path.normcase(os.path.abspath(old_stub)) != os.path.normcase(os.path.abspath(new_stub_path))
    ):
        remove_hub_stub(old_stub)


def extract_py_plugin_metadata(file_path: str) -> dict[str, Any]:
    """Safely inspect top-level plugin constants (UUID, CREDITS, VERSION, NAME) via AST."""
    info: dict[str, Any] = {
        "uuid": None,
        "credits": None,
        "version": None,
        "name": None,
        "is_hub_loader": False,
    }
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except OSError as exc:
        logger.debug("Skipping unreadable plugin file %s: %s", file_path, exc)
        return info

    first_line = source.splitlines()[0] if source else ""
    info["is_hub_loader"] = LOADER_HEADER in first_line

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return info

    for node in tree.body:
        target_name: Optional[str] = None
        val_node: Optional[ast.AST] = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            val_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            val_node = node.value

        if target_name in ("UUID", "CREDITS", "VERSION", "NAME") and val_node is not None:
            if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                info[target_name.lower()] = val_node.value.strip()
    return info


def _matches_creator(file_credits: Optional[str], expected_credits: Optional[str]) -> bool:
    if not file_credits or not expected_credits:
        return False
    return expected_credits.strip().lower() in file_credits.strip().lower()


def scan_legacy_plugin_files() -> list[tuple[str, str, dict[str, Any]]]:
    """Scan non-loader .py plugin files and extract metadata once per batch."""
    plugins_dir = PATHS.plugins_dir
    if not os.path.isdir(plugins_dir):
        return []
    with os.scandir(plugins_dir) as it:
        entries = sorted(
            (e for e in it if e.is_file() and e.name.lower().endswith(".py")),
            key=lambda e: e.name,
        )
    scanned: list[tuple[str, str, dict[str, Any]]] = []
    for entry in entries:
        meta = extract_py_plugin_metadata(entry.path)
        if meta.get("is_hub_loader"):
            continue
        scanned.append((entry.name, entry.path, meta))
    return scanned


def _iter_matching_legacy_files(
    target_uuid: Optional[str],
    expected_credits: Optional[str] = "@w1bbex",
    exclude_paths: Optional[list[str]] = None,
    legacy_entries: Optional[list[tuple[str, str, dict[str, Any]]]] = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return non-loader .py entries in plugins_dir matching target_uuid and expected_credits."""
    norm_uuid = str(target_uuid or "").strip().lower()
    if not norm_uuid:
        return []

    excluded = {os.path.normcase(os.path.abspath(PATHS.hub_file))}
    for path in exclude_paths or ():
        if path:
            excluded.add(os.path.normcase(os.path.abspath(path)))

    candidates = legacy_entries if legacy_entries is not None else scan_legacy_plugin_files()
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for filename, file_path, meta in candidates:
        if os.path.normcase(os.path.abspath(file_path)) in excluded:
            continue
        if str(meta.get("uuid") or "").strip().lower() != norm_uuid:
            continue
        if expected_credits and not _matches_creator(meta.get("credits"), expected_credits):
            continue
        matches.append((filename, file_path, meta))
    return matches


def find_manual_plugin_files(
    target_uuid: Optional[str],
    expected_credits: Optional[str] = "@w1bbex",
    exclude_paths: Optional[list[str]] = None,
    legacy_entries: Optional[list[tuple[str, str, dict[str, Any]]]] = None,
) -> list[dict[str, str]]:
    """List manual or legacy plugin files in plugins/ matching UUID and creator."""
    return [
        {
            "filename": filename,
            "path": file_path,
            "version": str(meta.get("version") or ""),
            "name": str(meta.get("name") or ""),
        }
        for filename, file_path, meta in _iter_matching_legacy_files(
            target_uuid, expected_credits, exclude_paths, legacy_entries=legacy_entries
        )
    ]


def archive_legacy_plugins(
    target_uuid: Optional[str],
    expected_credits: Optional[str] = "@w1bbex",
    exclude_paths: Optional[list[str]] = None,
) -> list[str]:
    """Move manual or legacy @w1bbex plugins matching target_uuid into plugins/w1bbex/archive/."""
    matches = _iter_matching_legacy_files(target_uuid, expected_credits, exclude_paths)
    if not matches:
        return []

    archive_dir = os.path.join(PATHS.plugins_dir, "w1bbex", "archive")
    archived: list[str] = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for filename, file_path, _meta in matches:
        os.makedirs(archive_dir, exist_ok=True)
        stem, ext = os.path.splitext(filename)
        dest = os.path.join(archive_dir, f"{stem}_{stamp}{ext}")
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(archive_dir, f"{stem}_{stamp}_{counter}{ext}")
            counter += 1
        try:
            shutil.move(file_path, dest)
            archived.append(filename)
            logger.info("Archived legacy plugin %s -> %s", file_path, dest)
        except OSError:
            logger.exception("Failed archiving legacy plugin %s", file_path)
    return archived


def generate_loader(
    plugin_name: str,
    plugin_label: str,
    folder_name: str,
    api_name: str,
    api_version: str,
    api_desc: str,
    target_filename: Optional[str] = None,
) -> str:
    """Generate and atomically write the Cardinal loader stub for a plugin binary."""
    safe_label = sanitize_segment(plugin_label, "plugin")
    safe_folder = sanitize_segment(folder_name, "plugin_build")
    safe_target = os.path.basename(target_filename) if target_filename else ""
    loader_path = os.path.join(PATHS.plugins_dir, f"{safe_label}.py")
    fallback_uuid = str(
        uuid.UUID(
            bytes=hashlib.sha256(f"w1bbex:{plugin_name}".encode("utf-8")).digest()[:16],
            version=4,
        )
    )
    loader_code = (
        f"{LOADER_HEADER}\n"
        "import os\n"
        "import sys\n"
        "import importlib.util\n\n"
        f"base_dir = os.path.join(os.path.dirname(__file__), 'w1bbex', {repr(safe_folder)})\n"
        "pyd_path = None\n"
        "if os.path.exists(base_dir):\n"
        f"    _preferred = os.path.join(base_dir, {repr(safe_target)}) if {repr(safe_target)} else ''\n"
        "    if _preferred and os.path.isfile(_preferred):\n"
        "        pyd_path = _preferred\n"
        "    else:\n"
        "        for _exts in (('.pyd', '.so'), ('.py',)):\n"
        "            for fname in sorted(os.listdir(base_dir)):\n"
        "                if fname.endswith(_exts):\n"
        "                    pyd_path = os.path.join(base_dir, fname)\n"
        "                    break\n"
        "            if pyd_path:\n"
        "                break\n\n"
        "if not pyd_path:\n"
        "    raise ImportError('Бинарный файл плагина не найден!')\n\n"
        f"spec = importlib.util.spec_from_file_location({repr(safe_label)}, pyd_path)\n"
        "if spec is None or spec.loader is None:\n"
        "    raise ImportError(f'Не удалось создать загрузчик для {pyd_path}')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "try:\n"
        "    spec.loader.exec_module(module)\n"
        "except Exception:\n"
        "    sys.modules.pop(spec.name, None)\n"
        "    raise\n\n"
        "for name in dir(module):\n"
        "    if not name.startswith('__'):\n"
        "        globals()[name] = getattr(module, name)\n\n"
        f"NAME = {repr(api_name)}\n"
        f"VERSION = {repr(api_version)}\n"
        f"DESCRIPTION = {repr(api_desc)}\n"
        f"CREDITS = getattr(module, 'CREDITS', {repr(CREDITS)})\n"
        f"_FALLBACK_UUID = {repr(fallback_uuid)}\n"
        "UUID = getattr(module, 'UUID', _FALLBACK_UUID)\n"
        "SETTINGS_PAGE = getattr(module, 'SETTINGS_PAGE', False)\n"
        "BIND_TO_DELETE = getattr(module, 'BIND_TO_DELETE', None)\n"
    )
    atomic_write_bytes(loader_path, loader_code.encode("utf-8"))
    return loader_path


def download_plugin(
    cardinal: Any,
    plugin_name: str,
    license_key: Optional[str],
    tg_user: Any,
) -> tuple[bool, str]:
    """Download, verify, install, and persist metadata for a plugin build (.py, .pyd, .so)."""
    payload = {
        "plugin_name": plugin_name,
        "license_key": (license_key or "").strip(),
        "system": get_sys_info(),
        "funpay": get_funpay_identity(cardinal),
        "telegram": get_telegram_identity(tg_user),
    }
    try:
        resp = requests.post(f"{API_BASE}/download", json=payload, timeout=15, proxies=NO_PROXY)
    except requests.RequestException as exc:
        logger.error("Download plugin error: %s", exc)
        return False, f"Ошибка сети: {exc}"

    if resp.status_code != 200:
        err_json = safe_json(resp)
        return False, (
            str(err_json["error"])
            if (err_json and err_json.get("error"))
            else f"Ошибка сервера: HTTP {resp.status_code}"
        )

    content_type = resp.headers.get("Content-Type", "").lower()
    if "application/json" in content_type or resp.content.lstrip().startswith(b"{"):
        json_err = safe_json(resp)
        if json_err is not None and not json_err.get("success", True):
            return False, str(json_err.get("error", "Неизвестная ошибка API."))
    if not resp.content:
        return False, "Сервер вернул пустой файл сборки."

    raw_filename = resp.headers.get("X-Filename", f"{plugin_name}_Release.py")
    base_filename = raw_filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not base_filename
        or not base_filename.endswith(SUPPORTED_PLUGIN_EXTENSIONS)
        or base_filename in (".", "..")
    ):
        filename = f"{sanitize_segment(plugin_name, 'plugin')}.py"
    else:
        ext = next(e for e in SUPPORTED_PLUGIN_EXTENSIONS if base_filename.endswith(e))
        filename = f"{sanitize_segment(base_filename[:-len(ext)], plugin_name)}{ext}"

    plugin_info = next((x for x in get_plugins() if x.get("id") == plugin_name), None)
    installed_map = load_data().get("installed")
    old_entry = installed_map.get(plugin_name) if isinstance(installed_map, dict) else None
    if not isinstance(old_entry, dict):
        old_entry = {}

    expected_sha = (
        resp.headers.get("X-File-SHA256")
        or resp.headers.get("X-SHA256")
        or (plugin_info.get("sha256") if isinstance(plugin_info, dict) else None)
    )
    if expected_sha:
        actual_sha = hashlib.sha256(resp.content).hexdigest().lower()
        if actual_sha != str(expected_sha).strip().lower():
            return False, "Контрольная сумма (SHA-256) загруженного файла не совпадает."

    if filename.lower().endswith(".py"):
        try:
            source_text = resp.content.decode("utf-8")
            ast.parse(source_text, filename=filename)
        except UnicodeDecodeError:
            return False, "Загруженный .py файл поврежден (некорректная кодировка UTF-8)."
        except SyntaxError as exc:
            return False, f"Загруженный .py файл содержит синтаксическую ошибку (строка {exc.lineno})."

    plugin_label = str(old_entry.get("name") or plugin_name) if not plugin_info else plugin_label_from_info(plugin_name, plugin_info)
    version = str(plugin_info.get("latest_version") or old_entry.get("version") or "v1.0") if plugin_info else str(old_entry.get("version") or "v1.0")
    if filename.lower().endswith(".py"):
        folder_name = sanitize_segment(plugin_label, sanitize_segment(plugin_name, "plugin"))
    else:
        folder_name = sanitize_segment(f"{plugin_name}_{version}", f"{plugin_name}_build")
    plugin_dir = os.path.join(PATHS.plugins_dir, "w1bbex", folder_name)
    save_path = os.path.join(plugin_dir, filename)
    api_name = str(plugin_info.get("name", plugin_label)) if plugin_info else plugin_label
    api_desc = (
        str(plugin_info.get("description", "Загружено через W1bbex Hub"))
        if plugin_info
        else "Загружено через W1bbex Hub"
    )
    old_bin_dir = old_entry.get("bin_dir")

    expected_loader_path = os.path.join(PATHS.plugins_dir, f"{sanitize_segment(plugin_label, 'plugin')}.py")
    prev_save_bytes: Optional[bytes] = None
    prev_loader_bytes: Optional[bytes] = None
    wrote_save = False
    wrote_loader = False

    try:
        with PLUGIN_FS_LOCK:
            if os.path.isfile(save_path):
                with open(save_path, "rb") as prev_f:
                    prev_save_bytes = prev_f.read()
            if os.path.isfile(expected_loader_path):
                with open(expected_loader_path, "rb") as prev_l:
                    prev_loader_bytes = prev_l.read()

            atomic_write_bytes(save_path, resp.content)
            wrote_save = True
            save_abs = os.path.normcase(os.path.abspath(save_path))
            if os.path.isdir(plugin_dir):
                for entry_name in os.listdir(plugin_dir):
                    if entry_name.lower().endswith(SUPPORTED_PLUGIN_EXTENSIONS):
                        candidate = os.path.join(plugin_dir, entry_name)
                        if os.path.normcase(os.path.abspath(candidate)) != save_abs and os.path.isfile(candidate):
                            os.remove(candidate)

            extracted_meta = extract_py_plugin_metadata(save_path) if save_path.lower().endswith(".py") else {}
            target_uuid = (plugin_info.get("uuid") if plugin_info else None) or extracted_meta.get("uuid")
            target_credits = (
                (plugin_info.get("credits") if plugin_info else None)
                or extracted_meta.get("credits")
                or "@w1bbex"
            )
            archived_files = archive_legacy_plugins(target_uuid=target_uuid, expected_credits=target_credits)

            loader_path = generate_loader(
                plugin_name,
                plugin_label,
                folder_name,
                api_name,
                version,
                api_desc,
                target_filename=filename,
            )
            wrote_loader = True
            clean_old_plugin(plugin_name, new_stub_path=loader_path)
            if old_bin_dir and os.path.normcase(os.path.abspath(old_bin_dir)) != os.path.normcase(
                os.path.abspath(plugin_dir)
            ):
                queue_pending_deletion(old_bin_dir)
            update_data(
                lambda d: d.setdefault("installed", {}).__setitem__(
                    plugin_name,
                    {"name": plugin_label, "version": version, "stub_path": loader_path, "bin_dir": plugin_dir,
                     "catalog": {k: (plugin_info or {}).get(k) for k in
                                 ("description", "uuid", "credits", "is_free", "prefix")}},
                )
            )
    except OSError as exc:
        with PLUGIN_FS_LOCK:
            if wrote_loader:
                try:
                    if prev_loader_bytes is not None:
                        atomic_write_bytes(expected_loader_path, prev_loader_bytes)
                    elif os.path.isfile(expected_loader_path):
                        os.remove(expected_loader_path)
                except OSError:
                    logger.warning("Rollback failed for loader %s", expected_loader_path, exc_info=True)
            if wrote_save:
                try:
                    if prev_save_bytes is not None:
                        atomic_write_bytes(save_path, prev_save_bytes)
                    elif os.path.isfile(save_path):
                        os.remove(save_path)
                except OSError:
                    logger.warning("Rollback failed for binary %s", save_path, exc_info=True)
        if isinstance(exc, PermissionError):
            logger.exception("Plugin binary locked during reinstall of %s", plugin_name)
            return (
                False,
                "Файл плагина занят процессом. Удалите плагин в Hub, выполните /restart и установите снова.",
            )
        logger.exception("Failed saving plugin %s", plugin_name)
        return False, f"Ошибка сохранения файлов на диск: {exc}"

    archived_note = (
        f" В архив (plugins/w1bbex/archive): {', '.join(archived_files)}."
        if archived_files
        else ""
    )
    return True, f"Плагин {plugin_label} (версия {version}) успешно скачан и установлен.{archived_note}"


def validate_hub_source(raw_bytes: bytes, content_type: str = "") -> tuple[bool, str]:
    """Verify downloaded W1bbexHub.py payload is valid Python with Cardinal metadata."""
    try:
        source_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False, "Полученный файл поврежден (некорректная кодировка UTF-8)."

    stripped = source_text.lstrip()
    if "application/json" in content_type.lower() or stripped.startswith("{"):
        err_msg = _extract_json_error(stripped)
        if err_msg or "application/json" in content_type.lower():
            return False, err_msg or "Сервер вернул JSON вместо файла плагина."

    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        return False, f"Получен поврежденный Python-файл: {exc.msg}"

    assigned: dict[str, Optional[str]] = {}
    for node in tree.body:
        val = (
            node.value.value.strip()
            if isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
            else None
        )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = val
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned[node.target.id] = val

    if not {"VERSION", "UUID", "BIND_TO_PRE_INIT"}.issubset(assigned):
        return False, "Скачанный файл не содержит обязательных метаданных W1bbex Hub."
    if (assigned.get("UUID") or "").strip().lower() != HUB_UUID.strip().lower():
        return False, "UUID скачанного файла не совпадает с W1bbex Hub."
    return True, ""


def download_and_update_hub(target_path: Optional[str] = None) -> tuple[bool, str]:
    """Download latest W1bbexHub.py, validate syntax/metadata, and write atomically."""
    try:
        resp = requests.get(f"{API_BASE}/download_client", timeout=15, proxies=NO_PROXY)
    except requests.RequestException as exc:
        logger.error("Hub self-update network error: %s", exc)
        return False, f"Ошибка сети: {exc}"

    if resp.status_code != 200:
        err_json = safe_json(resp)
        err_msg = err_json.get("error") if err_json else f"HTTP {resp.status_code}"
        return False, f"Ошибка скачивания: {err_msg}"

    expected_sha = resp.headers.get("X-File-SHA256") or resp.headers.get("X-SHA256")
    if expected_sha:
        actual_sha = hashlib.sha256(resp.content).hexdigest().lower()
        if actual_sha != str(expected_sha).strip().lower():
            return False, "Контрольная сумма (SHA-256) файла обновления Hub не совпадает."

    ok, reason = validate_hub_source(resp.content, resp.headers.get("Content-Type", ""))
    if not ok:
        return False, reason

    dest = os.path.abspath(target_path or PATHS.hub_file)
    try:
        atomic_write_bytes(dest, resp.content)
    except OSError as exc:
        logger.exception("Failed writing Hub update")
        return False, f"Ошибка записи файла обновления: {exc}"
    return True, "W1bbex HUB успешно обновлен!"


def collect_pending_updates(data: dict[str, Any], plugins: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Compute installed plugin updates that are eligible for notification."""
    installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
    notified = data.get("notified_updates") if isinstance(data.get("notified_updates"), dict) else {}
    pending: list[dict[str, Any]] = []

    for p in plugins:
        pid = p.get("id", "")
        entry = installed.get(pid)
        if not isinstance(entry, dict) or not p.get("notify_users", True):
            continue
        curr_ver = str(entry.get("version", "v1.0"))
        latest = str(p.get("latest_version", "v1.0"))
        if not is_newer_version(latest, curr_ver):
            continue

        update_key = f"{pid}_{latest}"
        remind_at = notified.get(update_key)
        if remind_at == -1 or (isinstance(remind_at, (int, float)) and now < remind_at):
            continue

        pending.append(
            {
                "id": pid,
                "name": str(p.get("name", pid)),
                "latest": latest,
                "curr_ver": curr_ver,
                "urgency": str(p.get("urgency", "medium")),
                "changelog": str(p.get("changelog") or "Без описания"),
                "update_key": update_key,
            }
        )
    return pending


def _versions_differ(ver_a: Optional[str], ver_b: Optional[str]) -> bool:
    if not ver_a or not ver_b:
        return False
    return is_newer_version(ver_a, ver_b) or is_newer_version(ver_b, ver_a)


def _normalize_plugin_path(raw_path: Optional[str]) -> str:
    if not raw_path:
        return ""
    resolved = raw_path if os.path.isabs(raw_path) else os.path.join(PATHS.cardinal_root, raw_path)
    return os.path.normcase(os.path.abspath(resolved))


def get_disk_hub_pending_version() -> Optional[str]:
    """Return newer Hub version written on disk awaiting Cardinal restart, if any."""
    if not PATHS.hub_file or not os.path.isfile(PATHS.hub_file):
        return None
    meta = extract_py_plugin_metadata(PATHS.hub_file)
    disk_ver = meta.get("version")
    if disk_ver and is_newer_version(str(disk_ver), VERSION):
        return str(disk_ver)
    return None


def find_loaded_plugin(
    cardinal: Any,
    plugin_info: Optional[dict[str, Any]],
    installed_entry: Optional[dict[str, Any]],
) -> tuple[Optional[str], Optional[Any]]:
    """Locate a loaded PluginData instance in cardinal.plugins by UUID, stub path, or exact name."""
    plugins_map = getattr(cardinal, "plugins", None)
    if not isinstance(plugins_map, dict):
        return None, None

    hub_uuid = HUB_UUID.strip().lower()
    target_uuid = str((plugin_info or {}).get("uuid") or "").strip().lower()
    if target_uuid and target_uuid != hub_uuid:
        for uid, pdata in plugins_map.items():
            uid_str = str(uid).strip().lower()
            pdata_uuid = str(getattr(pdata, "uuid", "") or "").strip().lower()
            if uid_str == target_uuid or pdata_uuid == target_uuid:
                return str(uid), pdata

    stub_norm = _normalize_plugin_path((installed_entry or {}).get("stub_path"))
    if stub_norm:
        for uid, pdata in plugins_map.items():
            if str(uid).strip().lower() == hub_uuid:
                continue
            if _normalize_plugin_path(getattr(pdata, "path", None)) == stub_norm:
                return str(uid), pdata

    target_name = str((plugin_info or {}).get("name") or "").strip().lower()
    if target_name:
        for uid, pdata in plugins_map.items():
            if str(uid).strip().lower() == hub_uuid:
                continue
            if str(getattr(pdata, "name", "") or "").strip().lower() == target_name:
                return str(uid), pdata

    return None, None


def inspect_plugin_state(
    cardinal: Any,
    plugin_info: dict[str, Any],
    installed: Optional[dict[str, Any]] = None,
    keys: Optional[dict[str, Any]] = None,
    legacy_entries: Optional[list[tuple[str, str, dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Build a complete read-only status snapshot for one catalog plugin."""
    pid = str(plugin_info.get("id", ""))
    name = str(plugin_info.get("name", pid))
    latest_version = str(plugin_info.get("latest_version", "v1.0"))
    is_free = bool(plugin_info.get("is_free", False))

    installed_map = installed if isinstance(installed, dict) else {}
    keys_map = keys if isinstance(keys, dict) else {}
    raw_entry = installed_map.get(pid)
    installed_entry = raw_entry if isinstance(raw_entry, dict) else None
    installed_version = str(installed_entry.get("version", "")) if installed_entry else None
    stub_path = str(installed_entry.get("stub_path") or "") if installed_entry else ""
    stub_exists = bool(stub_path and os.path.isfile(stub_path))
    bin_dir = str(installed_entry.get("bin_dir") or "") if installed_entry else ""

    plugins_registry_present = isinstance(getattr(cardinal, "plugins", None), dict)
    loaded_uuid, loaded_plugin = find_loaded_plugin(cardinal, plugin_info, installed_entry)
    loaded_version = (str(getattr(loaded_plugin, "version", "") or "") or None) if loaded_plugin else None
    loaded_enabled = bool(getattr(loaded_plugin, "enabled", True)) if loaded_plugin else False
    loaded_path = _normalize_plugin_path(getattr(loaded_plugin, "path", None)) if loaded_plugin else ""
    loaded_file_exists = bool(loaded_path and os.path.isfile(loaded_path))
    raw_cmds = getattr(loaded_plugin, "commands", None) if loaded_plugin else None
    commands = [(str(cmd), str(desc)) for cmd, desc in raw_cmds.items()] if isinstance(raw_cmds, dict) else []
    settings_page = bool(getattr(loaded_plugin, "settings_page", False)) if loaded_plugin else False

    exclude = [stub_path] if stub_path else []
    manual_files = find_manual_plugin_files(
        plugin_info.get("uuid"),
        plugin_info.get("credits") or "@w1bbex",
        exclude_paths=exclude,
        legacy_entries=legacy_entries,
    )
    plugin_label = plugin_label_from_info(pid, plugin_info)
    storage_dir = os.path.join(PATHS.cardinal_root, "storage", "w1bbex", sanitize_segment(plugin_label, pid))
    storage_dir_exists = os.path.isdir(storage_dir)

    key_val = str(keys_map.get(pid) or "")
    has_key = bool(key_val)
    masked_key = f"{key_val[:6]}…{key_val[-4:]}" if len(key_val) > 12 else key_val

    needs_restart = False
    restart_action = "general"
    if installed_entry is not None:
        if not stub_exists:
            status_code = "broken_files"
        elif not plugins_registry_present:
            status_code = "installed"
        elif loaded_plugin is None:
            status_code = "restart_install"
            needs_restart = True
            restart_action = "install"
        elif _versions_differ(installed_version, loaded_version):
            status_code = "restart_update"
            needs_restart = True
            restart_action = "update"
        elif loaded_path != _normalize_plugin_path(stub_path):
            status_code = "restart_install"
            needs_restart = True
            restart_action = "install"
        elif not loaded_enabled:
            status_code = "disabled"
        else:
            status_code = "active"
    else:
        if loaded_plugin is not None:
            if loaded_file_exists:
                status_code = "manual"
            else:
                status_code = "restart_delete"
                needs_restart = True
                restart_action = "delete"
        elif manual_files:
            status_code = "manual"
        else:
            status_code = "not_installed"

    manual_versions = {item["version"] for item in manual_files if item.get("version")}
    matching_paths = {_normalize_plugin_path(item["path"]) for item in manual_files}
    matched_loaded = bool(loaded_version and (str(loaded_uuid or "").lower() == str(plugin_info.get("uuid") or "").lower()
                          or loaded_path in matching_paths))
    manual_version = loaded_version if matched_loaded else (next(iter(manual_versions)) if len(manual_versions) == 1 else None)
    current_version = installed_version if installed_entry is not None else manual_version if status_code == "manual" else None
    has_update = bool(current_version and plugin_info.get("catalog_known", True) and is_newer_version(latest_version, current_version))

    return {
        "id": pid,
        "name": name,
        "plugin_info": plugin_info,
        "latest_version": latest_version,
        "is_free": is_free,
        "description": str(plugin_info.get("description") or ""),
        "status_code": status_code,
        "needs_restart": needs_restart,
        "restart_action": restart_action,
        "installed_entry": installed_entry,
        "installed_version": installed_version,
        "stub_path": stub_path,
        "stub_exists": stub_exists,
        "bin_dir": bin_dir,
        "loaded_uuid": loaded_uuid,
        "loaded_version": loaded_version,
        "loaded_enabled": loaded_enabled,
        "loaded_file_exists": loaded_file_exists,
        "commands": commands,
        "settings_page": settings_page,
        "manual_files": manual_files,
        "manual_version": manual_version,
        "storage_dir_exists": storage_dir_exists,
        "has_key": has_key,
        "masked_key": masked_key,
        "has_update": has_update,
        "catalog_known": plugin_info.get("catalog_known", True),
    }


def delete_plugin_files(plugin_name: str) -> dict[str, Any]:
    """Remove Hub loader stub, delete or defer plugin binaries, and preserve license/user storage."""
    with PLUGIN_FS_LOCK:
        data = load_data()
        installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
        keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
        info = installed.get(plugin_name)
        if not isinstance(info, dict):
            return {"ok": False, "error": "Плагин не установлен или уже удален."}

        stub_path = info.get("stub_path")
        bin_dir = info.get("bin_dir")

        stub_ok, denial_reason = remove_hub_stub(stub_path)
        if not stub_ok and denial_reason:
            return {"ok": False, "error": denial_reason}

        locked = queue_pending_deletion(bin_dir)
        update_data(lambda cur: (cur.get("installed") or {}).pop(plugin_name, None))

        return {
            "ok": True,
            "plugin_id": plugin_name,
            "plugin_name": str(info.get("name") or plugin_name),
            "version": str(info.get("version") or ""),
            "stub_path": str(stub_path or ""),
            "stub_name": os.path.basename(str(stub_path)) if stub_path else "",
            "bin_dir": str(bin_dir or ""),
            "bin_dir_name": os.path.basename(str(bin_dir)) if bin_dir else "",
            "bin_deferred": locked,
            "kept_key": bool(keys.get(plugin_name)),
        }


def trigger_cardinal_restart(
    delay_seconds: float = 0.35,
    restart_fn: Optional[Callable[[], Any]] = None,
) -> threading.Timer:
    """Schedule Cardinal process restart slightly after HTTP/Telegram acknowledgement completes."""
    if restart_fn is None:
        try:
            from Utils.cardinal_tools import restart_program as restart_fn  # type: ignore[import-not-found]
        except Exception:
            restart_fn = lambda: os.execl(sys.executable, sys.executable, *sys.argv)

    def _run_restart() -> None:
        try:
            restart_fn()
        except Exception:
            logger.exception("Failed restarting Cardinal")

    timer = threading.Timer(delay_seconds, _run_restart)
    timer.daemon = True
    timer.start()
    return timer


def invoke_plugin_command(
    cardinal: Any,
    loaded_plugin: Any,
    base_message: Any,
    from_user: Any,
) -> tuple[bool, str]:
    """Dispatch the plugin's primary slash command via Cardinal's registered bot message handlers."""
    if not getattr(loaded_plugin, "enabled", True):
        return False, "Плагин отключён в настройках Cardinal — сначала включите его."

    raw_commands = getattr(loaded_plugin, "commands", None)
    if not isinstance(raw_commands, dict) or not raw_commands:
        return False, "У этого плагина нет зарегистрированной Telegram-команды."

    cmd = str(next(iter(raw_commands.keys()))).lstrip("/").strip()
    bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
    handlers = getattr(bot, "message_handlers", None) if bot else None
    if not isinstance(handlers, list):
        return False, f"Отправьте команду /{cmd} вручную в чат."

    target_fn: Optional[Callable[[Any], Any]] = None
    for entry in handlers:
        if not isinstance(entry, dict):
            continue
        filters = entry.get("filters") or {}
        cmds = filters.get("commands")
        if not cmds:
            continue
        norm_cmds = {str(c).lstrip("/").lower() for c in cmds}
        if cmd.lower() in norm_cmds and callable(entry.get("function")):
            target_fn = entry["function"]
            break

    if target_fn is None:
        return False, f"Обработчик команды /{cmd} не найден. Отправьте /{cmd} в чат."

    synth_msg = copy.copy(base_message)
    synth_msg.text = f"/{cmd}"
    synth_msg.content_type = "text"
    if from_user is not None:
        synth_msg.from_user = from_user

    target_fn(synth_msg)
    return True, cmd
