"""Full backup creation, validation, and atomic restoration helpers for AutoSMM."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import sqlite3
from typing import Any, Dict, Optional, Tuple
import uuid

from .. import runtime as rt
from ..models import normalize_api_url
from . import db_state
from . import migrations
from .storage import (
    CONFIG_LOCK,
    ORDERS_LOCK,
    STATE_LOCK,
    _safe_load_json,
    _safe_write_json,
    _write_bytes_atomic,
    load_config,
)

logger = logging.getLogger("FPC.autosmm")

AUTOSMM_BACKUP_VERSION = 2
SUPPORTED_BACKUP_VERSIONS = (1, 2)

AUTOSMM_BACKUP_DEFAULTS: Dict[str, Any] = {
    "auto_lots.json": {},
    "valid_link.json": [],
}

LEGACY_BACKUP_DEFAULTS: Dict[str, Any] = {
    "auto_lots.json": {},
    "auto_smm_orders.json": [],
    "orders_data.json": [],
    "orders_runtime.json": {},
    "valid_link.json": [],
    "auto_smm_state.json": {},
}


def get_backup_file_map() -> Dict[str, str]:
    """Return mapping of JSON config backup filenames to active storage paths."""
    return {
        "auto_lots.json": rt.CONFIG_PATH,
        "valid_link.json": rt.VALID_LINKS_PATH,
    }


def _backup_default_for_file(file_name: str) -> Any:
    default_value = AUTOSMM_BACKUP_DEFAULTS.get(file_name, {})
    if isinstance(default_value, (dict, list)):
        return json.loads(json.dumps(default_value, ensure_ascii=False))
    return default_value


def _read_backup_file_payload(file_name: str, path: str) -> Any:
    if not os.path.exists(path):
        return _backup_default_for_file(file_name)
    return _safe_load_json(path, _backup_default_for_file(file_name))


def _build_full_backup_payload(reason: str = "manual_export") -> Dict[str, Any]:
    with CONFIG_LOCK, ORDERS_LOCK, STATE_LOCK:
        snapshot = db_state.export_snapshot()
        return {
            "autosmm_backup_version": AUTOSMM_BACKUP_VERSION,
            "plugin": {
                "name": rt.NAME,
                "version": rt.VERSION,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason,
            },
            "files": {
                file_name: _read_backup_file_payload(file_name, file_path)
                for file_name, file_path in get_backup_file_map().items()
            },
            "orders": snapshot["orders"],
            "state": snapshot["state"],
        }


def _backup_filename(prefix: str = "autosmm_backup") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    return f"{prefix}_{rt.VERSION}_{stamp}_{uuid.uuid4().hex[:6]}.json"


_BACKUP_FILE_EXPECTED_TYPES: Dict[str, Any] = {
    "auto_lots.json": dict,
    "valid_link.json": list,
    "orders_data.json": list,
    "orders_runtime.json": dict,
    "auto_smm_orders.json": list,
    "auto_smm_state.json": dict,
}


MAX_BACKUP_FILES_TO_KEEP = 20


def _prune_old_backups(backups_dir: str, max_keep: int = MAX_BACKUP_FILES_TO_KEEP) -> None:
    if max_keep < 1 or not os.path.isdir(backups_dir):
        return
    try:
        entries = [
            os.path.join(backups_dir, name)
            for name in os.listdir(backups_dir)
            if name.startswith(("autosmm_backup_", "autosmm_emergency_backup_")) and name.endswith(".json")
        ]
        entries = [path for path in entries if os.path.isfile(path)]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale_path in entries[max_keep:]:
            try:
                os.remove(stale_path)
            except OSError:
                pass
    except OSError:
        pass


def _create_full_backup(reason: str = "manual_export") -> Optional[str]:
    try:
        os.makedirs(rt.AUTOSMM_BACKUPS_DIR, exist_ok=True)
        prefix = "autosmm_emergency_backup" if str(reason or "").startswith("before_") else "autosmm_backup"
        backup_path = os.path.join(rt.AUTOSMM_BACKUPS_DIR, _backup_filename(prefix))
        payload = _build_full_backup_payload(reason=reason)
        if not _safe_write_json(backup_path, payload):
            return None
        _prune_old_backups(rt.AUTOSMM_BACKUPS_DIR)
        return backup_path
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        logger.error(f"Не удалось создать полный бэкап AutoSMM: {exc}")
        return None


def _validate_backup_wrapper(payload: Any) -> Tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "Файл не похож на полный бэкап AutoSMM."
    version = payload.get("autosmm_backup_version")
    if version not in SUPPORTED_BACKUP_VERSIONS:
        return False, "Неподдерживаемая версия бэкапа AutoSMM."
    files = payload.get("files")
    if not isinstance(files, dict):
        return False, "В бэкапе нет раздела files."

    allowed_files = (
        set(LEGACY_BACKUP_DEFAULTS.keys())
        if version == 1
        else set(get_backup_file_map().keys())
    )
    unknown_files = sorted(set(files.keys()) - allowed_files)
    if unknown_files:
        return False, "Бэкап содержит неизвестные файлы. Восстановление отменено."

    for file_name, expected_type in _BACKUP_FILE_EXPECTED_TYPES.items():
        if file_name in files and not isinstance(files[file_name], expected_type):
            return False, f"В бэкапе некорректный формат файла {file_name}."

    cfg_payload = files.get("auto_lots.json")
    if isinstance(cfg_payload, dict):
        services_payload = cfg_payload.get("services")
        if "services" in cfg_payload and not isinstance(services_payload, dict):
            return False, "В бэкапе некорректный раздел services в auto_lots.json."
        if isinstance(services_payload, dict):
            for srv_key, srv_cfg in services_payload.items():
                if not isinstance(srv_cfg, dict):
                    return False, f"В бэкапе некорректная запись сервиса #{srv_key} в auto_lots.json."
                raw_api_url = str(srv_cfg.get("api_url") or "").strip()
                if raw_api_url:
                    try:
                        normalize_api_url(raw_api_url)
                    except ValueError as exc:
                        return False, f"В бэкапе запрещённый или некорректный api_url для сервиса #{srv_key}: {exc}."
        if "lot_mapping" in cfg_payload and not isinstance(cfg_payload["lot_mapping"], dict):
            return False, "В бэкапе некорректный раздел lot_mapping в auto_lots.json."

    if version == 2:
        if not isinstance(payload.get("orders"), list):
            return False, "В бэкапе некорректный раздел orders."
        if not isinstance(payload.get("state"), dict):
            return False, "В бэкапе некорректный раздел state."
    return True, "OK"


def _snapshot_files(paths: Any) -> Dict[str, Optional[bytes]]:
    snapshots: Dict[str, Optional[bytes]] = {}
    for file_path in paths:
        if os.path.exists(file_path):
            with open(file_path, "rb") as fh:
                snapshots[file_path] = fh.read()
        else:
            snapshots[file_path] = None
    return snapshots


def _rollback_files(snapshots: Dict[str, Optional[bytes]]) -> None:
    for file_path, raw_bytes in snapshots.items():
        try:
            if raw_bytes is None:
                if os.path.exists(file_path):
                    os.remove(file_path)
            else:
                _write_bytes_atomic(file_path, raw_bytes)
        except OSError as exc:
            logger.error(f"Не удалось откатить файл {file_path} после сбоя восстановления: {exc}")


def _restore_backup_wrapper(payload: Dict[str, Any]) -> Tuple[bool, str]:
    ok, error = _validate_backup_wrapper(payload)
    if not ok:
        return False, error
    emergency_backup = _create_full_backup(reason="before_restore")
    if not emergency_backup:
        return False, "Не удалось создать аварийный бэкап. Восстановление отменено."

    version = payload.get("autosmm_backup_version")
    files = payload.get("files") or {}
    if version == 1:
        raw_orders = files.get("orders_data.json", [])
        runtime_map = files.get("orders_runtime.json", {})
        orders_log = files.get("auto_smm_orders.json", [])
        state_val = files.get("auto_smm_state.json", {})
        orders_snapshot = migrations.merge_legacy_sources(
            raw_orders=raw_orders if isinstance(raw_orders, list) else [],
            runtime_map=runtime_map if isinstance(runtime_map, dict) else {},
            log_entries=orders_log if isinstance(orders_log, list) else [],
        )
        state_snapshot = state_val if isinstance(state_val, dict) else {}
    else:
        orders_snapshot = payload.get("orders", [])
        state_snapshot = payload.get("state", {})

    try:
        file_map = get_backup_file_map()
        with CONFIG_LOCK, ORDERS_LOCK, STATE_LOCK:
            snapshot_paths = list(file_map.values())
            if rt.CONFIG_BAK_PATH:
                snapshot_paths.append(rt.CONFIG_BAK_PATH)
            snapshots = _snapshot_files(snapshot_paths)
            try:
                for file_name, file_path in file_map.items():
                    value = files.get(file_name, _backup_default_for_file(file_name))
                    if file_name == "auto_lots.json" and isinstance(value, dict):
                        value = json.loads(json.dumps(value, ensure_ascii=False))
                        services_map = value.get("services")
                        if isinstance(services_map, dict):
                            for srv_cfg in services_map.values():
                                if isinstance(srv_cfg, dict):
                                    raw_url = str(srv_cfg.get("api_url") or "").strip()
                                    if raw_url:
                                        srv_cfg["api_url"] = normalize_api_url(raw_url)
                    parent = os.path.dirname(file_path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    if not _safe_write_json(file_path, value):
                        raise OSError(f"Не удалось записать {file_name}")
                    if file_name == "auto_lots.json" and rt.CONFIG_BAK_PATH:
                        if not _safe_write_json(rt.CONFIG_BAK_PATH, value):
                            raise OSError("Не удалось обновить auto_lots.json.bak")

                db_state.restore_snapshot(orders=orders_snapshot, state=state_snapshot)
            except Exception:
                _rollback_files(snapshots)
                raise

            rt.clear_memory_contexts()
            rt.invalidate_panel_balance_cache()
            try:
                load_config()
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.warning(f"Предупреждение при перечитывании конфига после восстановления бэкапа: {exc}")
        return True, (
            "✅ Бэкап AutoSMM восстановлен.\n\n"
            "Рекомендуется перезапустить Cardinal, чтобы все состояния точно перечитались."
        )
    except (OSError, sqlite3.Error, RuntimeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error(f"Ошибка восстановления бэкапа AutoSMM: {exc}")
        return False, f"❌ Не удалось восстановить бэкап: {exc}"
