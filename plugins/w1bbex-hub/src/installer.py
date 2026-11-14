"""Plugin archive/loader installation, Hub self-update, removal, and restart helpers."""

from __future__ import annotations

import ast
from datetime import datetime
import hashlib
import os
import shutil
import sys
import threading
import uuid
from typing import Any, Callable, Optional

import requests

from .core import (
    _iter_matching_legacy_files,
    extract_py_plugin_metadata,
    plugin_label_from_info,
    sanitize_segment,
)
from .hub_api import (
    _extract_json_error,
    get_funpay_identity,
    get_plugins,
    get_sys_info,
    get_telegram_identity,
    safe_json,
)
from .runtime import (
    API_BASE,
    CREDITS,
    LOADER_HEADER,
    NO_PROXY,
    PATHS,
    PLUGIN_FS_LOCK,
    SUPPORTED_PLUGIN_EXTENSIONS,
    UUID as HUB_UUID,
    logger,
)
from .storage import (
    atomic_write_bytes,
    load_data,
    queue_pending_deletion,
    update_data,
)


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
