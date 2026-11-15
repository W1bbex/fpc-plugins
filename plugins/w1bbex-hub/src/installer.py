"""Plugin archive/loader installation, Hub self-update, removal, and restart helpers."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
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
    is_newer_version,
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
    VERSION,
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


def clean_old_plugin(
    plugin_name: str,
    new_stub_path: Optional[str] = None,
    old_stub_path: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Remove old stub file when a plugin's stub path changes during an update."""
    if old_stub_path is None:
        installed = load_data().get("installed")
        entry = installed.get(plugin_name) if isinstance(installed, dict) else None
        old_stub_path = entry.get("stub_path") if isinstance(entry, dict) else None
    if old_stub_path and (
        new_stub_path is None
        or os.path.normcase(os.path.abspath(old_stub_path)) != os.path.normcase(os.path.abspath(new_stub_path))
    ):
        return remove_hub_stub(old_stub_path)
    return True, None


def archive_legacy_plugins(
    target_uuid: Optional[str],
    expected_credits: Optional[str] = "@w1bbex",
    exclude_paths: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """Move manual or legacy @w1bbex plugins matching target_uuid into plugins/w1bbex/archive/.

    Returns list of (original_path, archive_path) tuples for rollback safety.
    """
    matches = _iter_matching_legacy_files(target_uuid, expected_credits, exclude_paths)
    if not matches:
        return []

    archive_dir = os.path.join(PATHS.plugins_dir, "w1bbex", "archive")
    archived: list[tuple[str, str]] = []
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
            archived.append((file_path, dest))
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


@dataclass
class _InstallRollbackContext:
    save_path: str
    expected_loader_path: str
    prev_save_bytes: Optional[bytes] = None
    prev_loader_bytes: Optional[bytes] = None
    removed_candidates: list[tuple[str, bytes]] = field(default_factory=list)
    archived_legacy_pairs: list[tuple[str, str]] = field(default_factory=list)
    wrote_save: bool = False
    wrote_loader: bool = False
    plugin_name: Optional[str] = None
    prev_installed_entry: Optional[dict[str, Any]] = None
    persisted_installed: bool = False

    def _rollback_files(self) -> None:
        """Restore previous loader and binary files if modified."""
        if self.wrote_loader:
            try:
                if self.prev_loader_bytes is not None:
                    atomic_write_bytes(self.expected_loader_path, self.prev_loader_bytes)
                elif os.path.isfile(self.expected_loader_path):
                    os.remove(self.expected_loader_path)
            except OSError:
                logger.warning("Rollback failed for loader %s", self.expected_loader_path, exc_info=True)
        if self.wrote_save:
            try:
                if self.prev_save_bytes is not None:
                    atomic_write_bytes(self.save_path, self.prev_save_bytes)
                elif os.path.isfile(self.save_path):
                    os.remove(self.save_path)
            except OSError:
                logger.warning("Rollback failed for binary %s", self.save_path, exc_info=True)

    def _rollback_candidates_and_legacy(self) -> None:
        """Restore deleted candidate files and legacy archived plugins."""
        for cand_path, cand_bytes in self.removed_candidates:
            try:
                atomic_write_bytes(cand_path, cand_bytes)
            except OSError:
                logger.warning("Rollback failed to restore candidate %s", cand_path, exc_info=True)
        for orig_path, archived_dest in self.archived_legacy_pairs:
            try:
                if os.path.isfile(archived_dest) and not os.path.exists(orig_path):
                    shutil.move(archived_dest, orig_path)
                    logger.info("Restored archived legacy plugin %s <- %s", orig_path, archived_dest)
            except OSError:
                logger.warning("Rollback failed to restore legacy plugin %s from %s", orig_path, archived_dest, exc_info=True)

    def _rollback_metadata(self) -> None:
        """Restore previous installed record in config if modified."""
        if not self.persisted_installed or not self.plugin_name:
            return
        target_name = self.plugin_name
        prev_entry = (
            dict(self.prev_installed_entry)
            if isinstance(self.prev_installed_entry, dict) and self.prev_installed_entry
            else None
        )
        try:
            def _restore_fn(d: dict[str, Any]) -> None:
                installed = d.setdefault("installed", {})
                if not isinstance(installed, dict):
                    return
                if prev_entry:
                    installed[target_name] = prev_entry
                else:
                    installed.pop(target_name, None)

            update_data(_restore_fn)
            logger.info("Rolled back installed metadata for plugin %s", target_name)
        except OSError:
            logger.warning("Rollback failed for installed metadata %s", target_name, exc_info=True)

    def rollback(self) -> None:
        """Rollback all filesystem modifications and metadata under the installation lock."""
        with PLUGIN_FS_LOCK:
            self._rollback_metadata()
            self._rollback_files()
            self._rollback_candidates_and_legacy()


def _fetch_download_response(
    cardinal: Any,
    plugin_name: str,
    license_key: Optional[str],
    tg_user: Any,
) -> tuple[Optional[requests.Response], Optional[str]]:
    """Execute network request for plugin download and validate HTTP status and JSON errors."""
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
        return None, f"Ошибка сети: {exc}"

    if resp.status_code != 200:
        err_json = safe_json(resp)
        return None, (
            str(err_json["error"])
            if (err_json and err_json.get("error"))
            else f"Ошибка сервера: HTTP {resp.status_code}"
        )

    content_type = resp.headers.get("Content-Type", "").lower()
    if "application/json" in content_type or resp.content.lstrip().startswith(b"{"):
        json_err = safe_json(resp)
        if json_err is not None and not json_err.get("success", True):
            return None, str(json_err.get("error", "Неизвестная ошибка API."))
    if not resp.content:
        return None, "Сервер вернул пустой файл сборки."

    return resp, None


def _resolve_download_filename(plugin_name: str, raw_filename: Optional[str]) -> str:
    """Derive safe filename with valid plugin extension from header or fallback."""
    raw = raw_filename or f"{plugin_name}_Release.py"
    base_filename = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not base_filename
        or not base_filename.lower().endswith(SUPPORTED_PLUGIN_EXTENSIONS)
        or base_filename in (".", "..")
    ):
        return f"{sanitize_segment(plugin_name, 'plugin')}.py"
    ext = next(e for e in SUPPORTED_PLUGIN_EXTENSIONS if base_filename.lower().endswith(e.lower()))
    return f"{sanitize_segment(base_filename[:-len(ext)], plugin_name)}{ext.lower()}"


def _verify_downloaded_content(
    resp: requests.Response,
    filename: str,
    plugin_info: Optional[dict[str, Any]],
) -> Optional[str]:
    """Validate SHA-256 and python syntax for downloaded payload."""
    expected_sha = (
        resp.headers.get("X-File-SHA256")
        or resp.headers.get("X-SHA256")
        or (plugin_info.get("sha256") if isinstance(plugin_info, dict) else None)
    )
    if expected_sha:
        actual_sha = hashlib.sha256(resp.content).hexdigest().lower()
        if actual_sha != str(expected_sha).strip().lower():
            return "Контрольная сумма (SHA-256) загруженного файла не совпадает."

    if filename.lower().endswith(".py"):
        try:
            source_text = resp.content.decode("utf-8")
            ast.parse(source_text, filename=filename)
        except UnicodeDecodeError:
            return "Загруженный .py файл поврежден (некорректная кодировка UTF-8)."
        except SyntaxError as exc:
            return f"Загруженный .py файл содержит синтаксическую ошибку (строка {exc.lineno})."
    return None


def _resolve_target_paths_and_meta(
    plugin_name: str,
    filename: str,
    plugin_info: Optional[dict[str, Any]],
    old_entry: dict[str, Any],
) -> tuple[str, str, str, str, str, str, str, str]:
    """Compute labels, versions, directories, paths, and loader target location."""
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
    expected_loader_path = os.path.join(PATHS.plugins_dir, f"{sanitize_segment(plugin_label, 'plugin')}.py")
    return plugin_label, version, folder_name, plugin_dir, save_path, api_name, api_desc, expected_loader_path


def _stage_candidate_binaries(plugin_dir: str, save_path: str, ctx: _InstallRollbackContext) -> None:
    """Remove previous incompatible binaries in the plugin directory and record rollback bytes."""
    save_abs = os.path.normcase(os.path.abspath(save_path))
    if not os.path.isdir(plugin_dir):
        return
    for entry_name in os.listdir(plugin_dir):
        if entry_name.lower().endswith(SUPPORTED_PLUGIN_EXTENSIONS):
            candidate = os.path.join(plugin_dir, entry_name)
            if os.path.normcase(os.path.abspath(candidate)) != save_abs and os.path.isfile(candidate):
                with open(candidate, "rb") as cf:
                    candidate_bytes = cf.read()
                os.remove(candidate)
                ctx.removed_candidates.append((candidate, candidate_bytes))


def _capture_rollback_snapshots(save_path: str, ctx: _InstallRollbackContext) -> None:
    """Snapshot existing target binary and loader before overwriting."""
    if os.path.isfile(save_path):
        with open(save_path, "rb") as prev_f:
            ctx.prev_save_bytes = prev_f.read()
    if os.path.isfile(ctx.expected_loader_path):
        with open(ctx.expected_loader_path, "rb") as prev_l:
            ctx.prev_loader_bytes = prev_l.read()


def _archive_and_generate_loader(
    save_path: str,
    filename: str,
    plugin_info: Optional[dict[str, Any]],
    plugin_name: str,
    plugin_label: str,
    folder_name: str,
    api_name: str,
    version: str,
    api_desc: str,
    ctx: _InstallRollbackContext,
) -> str:
    """Extract metadata, archive matching legacy copies, and generate executable loader."""
    extracted_meta = extract_py_plugin_metadata(save_path) if save_path.lower().endswith(".py") else {}
    target_uuid = (plugin_info.get("uuid") if plugin_info else None) or extracted_meta.get("uuid")
    target_credits = (
        (plugin_info.get("credits") if plugin_info else None)
        or extracted_meta.get("credits")
        or "@w1bbex"
    )
    ctx.archived_legacy_pairs = archive_legacy_plugins(target_uuid=target_uuid, expected_credits=target_credits)

    loader_path = generate_loader(
        plugin_name,
        plugin_label,
        folder_name,
        api_name,
        version,
        api_desc,
        target_filename=filename,
    )
    ctx.wrote_loader = True
    return loader_path


def _persist_installed_record(
    plugin_name: str,
    plugin_label: str,
    version: str,
    loader_path: str,
    plugin_dir: str,
    plugin_info: Optional[dict[str, Any]],
    old_entry: dict[str, Any],
) -> None:
    """Write installed plugin metadata and catalog fields to storage."""
    existing_catalog = old_entry.get("catalog") if isinstance(old_entry.get("catalog"), dict) else {}
    catalog_meta = {}
    for k in ("description", "uuid", "credits", "is_free", "prefix"):
        val = (plugin_info or {}).get(k)
        if val is None:
            val = existing_catalog.get(k)
        catalog_meta[k] = val

    update_data(
        lambda d: d.setdefault("installed", {}).__setitem__(
            plugin_name,
            {
                "name": plugin_label,
                "version": version,
                "stub_path": loader_path,
                "bin_dir": plugin_dir,
                "catalog": catalog_meta,
            },
        )
    )


def _commit_installed_plugin(
    resp_content: bytes,
    save_path: str,
    plugin_dir: str,
    filename: str,
    plugin_info: Optional[dict[str, Any]],
    plugin_name: str,
    plugin_label: str,
    folder_name: str,
    api_name: str,
    version: str,
    api_desc: str,
    ctx: _InstallRollbackContext,
) -> None:
    """Stage binary on disk, archive duplicates, create loader, and commit storage record."""
    with PLUGIN_FS_LOCK:
        installed_map = load_data().get("installed")
        old_entry = installed_map.get(plugin_name) if isinstance(installed_map, dict) else {}
        if not isinstance(old_entry, dict):
            old_entry = {}
        old_bin_dir = old_entry.get("bin_dir")
        old_stub = old_entry.get("stub_path")

        _capture_rollback_snapshots(save_path, ctx)

        atomic_write_bytes(save_path, resp_content)
        ctx.wrote_save = True

        _stage_candidate_binaries(plugin_dir, save_path, ctx)

        loader_path = _archive_and_generate_loader(
            save_path,
            filename,
            plugin_info,
            plugin_name,
            plugin_label,
            folder_name,
            api_name,
            version,
            api_desc,
            ctx,
        )

        ctx.plugin_name = plugin_name
        ctx.prev_installed_entry = dict(old_entry) if isinstance(old_entry, dict) and old_entry else None

        _persist_installed_record(
            plugin_name,
            plugin_label,
            version,
            loader_path,
            plugin_dir,
            plugin_info,
            old_entry,
        )
        ctx.persisted_installed = True

        clean_old_plugin(plugin_name, new_stub_path=loader_path, old_stub_path=old_stub)
        if old_bin_dir and os.path.normcase(os.path.abspath(old_bin_dir)) != os.path.normcase(
            os.path.abspath(plugin_dir)
        ):
            queue_pending_deletion(old_bin_dir)


def download_plugin(
    cardinal: Any,
    plugin_name: str,
    license_key: Optional[str],
    tg_user: Any,
) -> tuple[bool, str]:
    """Download, verify, install, and persist metadata for a plugin build (.py, .pyd, .so)."""
    resp, err = _fetch_download_response(cardinal, plugin_name, license_key, tg_user)
    if err or resp is None:
        return False, err or "Ошибка загрузки плагина"

    raw_filename = resp.headers.get("X-Filename", f"{plugin_name}_Release.py")
    filename = _resolve_download_filename(plugin_name, raw_filename)

    plugin_info = next((x for x in get_plugins() if x.get("id") == plugin_name), None)
    installed_map = load_data().get("installed")
    old_entry = installed_map.get(plugin_name) if isinstance(installed_map, dict) else {}
    if not isinstance(old_entry, dict):
        old_entry = {}

    verify_err = _verify_downloaded_content(resp, filename, plugin_info)
    if verify_err:
        return False, verify_err

    (
        plugin_label,
        version,
        folder_name,
        plugin_dir,
        save_path,
        api_name,
        api_desc,
        expected_loader_path,
    ) = _resolve_target_paths_and_meta(plugin_name, filename, plugin_info, old_entry)

    ctx = _InstallRollbackContext(save_path=save_path, expected_loader_path=expected_loader_path)

    try:
        _commit_installed_plugin(
            resp.content,
            save_path,
            plugin_dir,
            filename,
            plugin_info,
            plugin_name,
            plugin_label,
            folder_name,
            api_name,
            version,
            api_desc,
            ctx,
        )
    except Exception as exc:
        ctx.rollback()
        if isinstance(exc, PermissionError):
            logger.exception("Plugin binary locked during reinstall of %s", plugin_name)
            return (
                False,
                "Файл плагина занят процессом. Удалите плагин в Hub, выполните /restart и установите снова.",
            )
        logger.exception("Failed saving plugin %s", plugin_name)
        return False, f"Ошибка сохранения файлов на диск: {exc}"

    archived_names = [os.path.basename(orig) for orig, _ in ctx.archived_legacy_pairs]
    archived_note = (
        f" В архив (plugins/w1bbex/archive): {', '.join(archived_names)}."
        if archived_names
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


def download_and_update_hub(target_path: Optional[str] = None, *, force: bool = False) -> tuple[bool, str]:
    """Download latest W1bbexHub.py, validate syntax/metadata, and write atomically."""
    try:
        resp = requests.get(f"{API_BASE}/download_client", timeout=15, proxies=NO_PROXY)
    except requests.RequestException as exc:
        logger.error("Hub self-update network error: %s", exc)
        return False, "Ошибка сети при скачивании обновления Hub."

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

    new_version: Optional[str] = None
    try:
        source_tree = ast.parse(resp.content.decode("utf-8", errors="replace"))
        for node in source_tree.body:
            target_name: Optional[str] = None
            val_node: Optional[ast.AST] = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                val_node = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_name = node.target.id
                val_node = node.value
            if target_name == "VERSION" and isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                new_version = val_node.value.strip()
                break
    except Exception:
        new_version = None

    if not force and new_version and is_newer_version(VERSION, new_version):
        return False, f"Скачанная версия Hub ({new_version}) старее текущей ({VERSION})."

    dest = os.path.abspath(target_path or PATHS.hub_file)
    try:
        atomic_write_bytes(dest, resp.content)
    except OSError as exc:
        logger.exception("Failed writing Hub update: %s", exc)
        return False, "Ошибка записи файла обновления на диск."
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
