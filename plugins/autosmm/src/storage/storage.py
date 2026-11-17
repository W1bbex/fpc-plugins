"""Storage coordinator for JSON configuration/whitelists and SQLite order/state persistence."""

from __future__ import annotations

from datetime import datetime
import copy
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
import uuid

from .. import runtime as rt
from ..models import (
    OrderRecord,
    is_plausible_allowed_domain,
    normalize_allowed_domain,
    optional_float,
    safe_float,
    safe_int,
)
from . import db, db_orders, db_state
from . import migrations
from .defaults import (
    DEFAULT_VALID_LINKS,
    LEGACY_DEFAULT_VALID_LINKS,
    _ensure_config_defaults,
    create_default_config,
)

logger = logging.getLogger("FPC.autosmm")


class _DynamicDbLock:
    """Reentrant lock proxy resolving db.get_db_lock() dynamically for the active rt.DB_PATH."""

    def __init__(self) -> None:
        self._local = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        target_lock = db.get_db_lock()
        acquired = (
            target_lock.acquire(blocking=blocking)
            if timeout == -1
            else target_lock.acquire(blocking=blocking, timeout=timeout)
        )
        if acquired:
            stack = getattr(self._local, "stack", None)
            if stack is None:
                stack = []
                self._local.stack = stack
            stack.append(target_lock)
        return acquired

    def release(self) -> None:
        stack = getattr(self._local, "stack", None)
        if not stack:
            raise RuntimeError("cannot release un-acquired lock")
        target_lock = stack.pop()
        target_lock.release()

    def __enter__(self) -> "_DynamicDbLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def _is_owned(self) -> bool:
        stack = getattr(self._local, "stack", None)
        if stack:
            return stack[-1]._is_owned()
        return db.get_db_lock()._is_owned()


CONFIG_LOCK = threading.RLock()
ORDERS_LOCK = _DynamicDbLock()
STATE_LOCK = _DynamicDbLock()
VALID_LINKS_MIGRATION_STATE_KEY = "valid_links_defaults_migrated"

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[str, threading.RLock] = {}


def _get_path_lock(path: str) -> threading.RLock:
    """Return a per-file reentrant lock so concurrent writers never collide on os.replace."""
    key = os.path.normcase(os.path.abspath(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _make_temp_path(path: str) -> str:
    """Build a collision-free temporary file path in the target directory."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"


def _replace_with_retry(temp_path: str, abs_path: str, attempts: int = 5) -> None:
    """Retry os.replace briefly on transient Windows sharing/permission locks."""
    for attempt in range(attempts):
        try:
            os.replace(temp_path, abs_path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.01 * (attempt + 1))


def _write_bytes_atomic(path: str, raw_bytes: bytes) -> None:
    """Atomically write raw bytes using a unique temp file, flush/fsync, and os.replace."""
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = _make_temp_path(abs_path)
    with _get_path_lock(abs_path):
        try:
            with open(temp_path, "wb") as fh:
                fh.write(raw_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(temp_path, abs_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError as exc:
                    logger.debug(f"Не удалось удалить временный файл {temp_path}: {exc}")


def _write_json_atomic(path: str, data: Any) -> None:
    """Atomically persist JSON using UTF-8 serialization and atomic byte replacement."""
    encoded = json.dumps(data, indent=4, ensure_ascii=False).encode("utf-8")
    _write_bytes_atomic(path, encoded)


def _safe_load_json(path: str, default: Any) -> Any:
    """Load JSON safely, archiving corrupt files instead of crashing."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error(f"Ошибка при чтении {os.path.basename(path)}: {exc}")
        backup_path = f"{path}.bak.{int(time.time())}"
        try:
            shutil.copy2(path, backup_path)
            logger.info(f"Создана резервная копия поврежденного файла: {backup_path}")
        except OSError as bak_exc:
            logger.error(f"Не удалось создать резервную копию {os.path.basename(path)}: {bak_exc}")
        return default
    except OSError as exc:
        logger.error(f"Ошибка при чтении {os.path.basename(path)}: {exc}")
        return default


def _safe_write_json(path: str, data: Any) -> bool:
    """Write JSON atomically and return explicit success status."""
    try:
        _write_json_atomic(path, data)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.exception(f"Ошибка при сохранении {os.path.basename(path)}: {exc}")
        return False


def _sync_runtime_config_refs(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        return
    with CONFIG_LOCK:
        rt.config.clear()
        rt.config.update(copy.deepcopy(cfg))
        lot_map = cfg.get("lot_mapping")
        rt.lot_mapping.clear()
        if isinstance(lot_map, dict):
            rt.lot_mapping.update(copy.deepcopy(lot_map))


def load_config() -> Dict[str, Any]:
    """Load auto_lots.json with automatic .bak recovery and default creation."""
    logger.debug("Загрузка конфигурации (файл настроек)...")
    with CONFIG_LOCK:
        raw_data = None
        if os.path.exists(rt.CONFIG_PATH):
            try:
                with open(rt.CONFIG_PATH, "r", encoding="utf-8") as fh:
                    file_content = fh.read()
                if file_content.strip():
                    raw_data = json.loads(file_content)
                else:
                    logger.error(f"Файл конфигурации {rt.CONFIG_PATH} пуст.")
            except json.JSONDecodeError as exc:
                logger.error(f"Ошибка при чтении JSON {rt.CONFIG_PATH}: {exc}")
            except OSError as exc:
                logger.error(f"Ошибка при открытии {rt.CONFIG_PATH}: {exc}")
        else:
            logger.debug(f"Файл конфигурации {rt.CONFIG_PATH} не найден. Проверяем резервную копию.")

        if (not isinstance(raw_data, dict) or not raw_data) and os.path.exists(rt.CONFIG_BAK_PATH):
            try:
                if os.path.getsize(rt.CONFIG_BAK_PATH) > 0:
                    logger.warning(f"Попытка восстановить конфиг из резервной копии {rt.CONFIG_BAK_PATH}...")
                    with open(rt.CONFIG_BAK_PATH, "r", encoding="utf-8") as fh:
                        bak_content = fh.read().strip()
                    if bak_content:
                        bak_data = json.loads(bak_content)
                        if isinstance(bak_data, dict) and bak_data:
                            logger.info(f"Конфигурация успешно восстановлена из {rt.CONFIG_BAK_PATH}!")
                            raw_data = bak_data
                            save_config(raw_data)
            except (OSError, ValueError) as bak_exc:
                logger.error(f"Ошибка восстановления из {rt.CONFIG_BAK_PATH}: {bak_exc}")

        if not isinstance(raw_data, dict) or not raw_data:
            corrupt_path = f"{rt.CONFIG_PATH}.corrupt_{int(time.time())}"
            if os.path.exists(rt.CONFIG_PATH):
                try:
                    shutil.copy2(rt.CONFIG_PATH, corrupt_path)
                    logger.warning(f"Поврежденный конфиг скопирован в {corrupt_path}")
                except OSError as copy_exc:
                    logger.warning(f"Не удалось скопировать поврежденный конфиг в {corrupt_path}: {copy_exc}")
            logger.error("Не удалось прочитать конфиг. Создаем конфигурацию по умолчанию.")
            cfg = create_default_config()
            save_config(cfg)
            return cfg

        if _ensure_config_defaults(raw_data):
            save_config(raw_data)
        _sync_runtime_config_refs(raw_data)
        return raw_data


def save_config(cfg: Dict[str, Any]) -> None:
    """Persist auto_lots.json atomically and sync auto_lots.json.bak."""
    logger.debug("Сохранение конфигурации (файл настроек)...")
    with CONFIG_LOCK:
        try:
            _write_json_atomic(rt.CONFIG_PATH, cfg)
            _sync_runtime_config_refs(cfg)
            logger.debug("Конфигурация сохранена атомарно.")
        except Exception as exc:
            logger.exception(f"Ошибка при атомарном сохранении конфигурации: {exc}")
            raise
        try:
            _write_json_atomic(rt.CONFIG_BAK_PATH, cfg)
        except (OSError, ValueError, TypeError) as bak_err:
            logger.warning(f"Не удалось создать backup конфига: {bak_err}")


def update_config(mutator: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    """Read-modify-write auto_lots.json atomically under CONFIG_LOCK."""
    with CONFIG_LOCK:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return cfg


def reindex_lots(cfg: Dict[str, Any]) -> None:
    """Renumber lot keys sequentially as lot_1..lot_N and save configuration."""
    with CONFIG_LOCK:
        lot_map = cfg.get("lot_mapping", {})

        def _lot_key_sort(item: Any) -> Any:
            key = str(item[0])
            match = re.match(r"^lot_(\d+)$", key)
            return (0, int(match.group(1))) if match else (1, key)

        sorted_lots = sorted(lot_map.items(), key=_lot_key_sort)
        cfg["lot_mapping"] = {
            f"lot_{idx}": lot_data
            for idx, (_lot_key, lot_data) in enumerate(sorted_lots, start=1)
        }
        save_config(cfg)
        logger.info("Лоты были переиндексированы.")


def _path_has_content(path_value: str) -> bool:
    try:
        return os.path.exists(path_value) and os.path.getsize(path_value) > 0
    except OSError:
        return os.path.exists(path_value)


def _copy_storage_file_if_needed(new_path: str, old_path: str, copied: List[str], source_label: str) -> None:
    try:
        if _path_has_content(new_path) or not os.path.exists(old_path):
            return
        parent = os.path.dirname(new_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(old_path, new_path)
        copied.append(f"{os.path.basename(old_path)} -> {os.path.basename(new_path)} ({source_label})")
    except OSError as exc:
        logger.warning(f"AUTO SMM STORAGE MIGRATION: не удалось скопировать {old_path} -> {new_path}: {exc}")


def _nested_storage_file_map() -> Dict[str, str]:
    base = rt.BASE_STORAGE_DIR
    return {
        rt.CONFIG_PATH: str(base / "config" / "auto_lots.json"),
        rt.VALID_LINKS_PATH: str(base / "config" / "valid_link.json"),
        rt.ORDERS_PATH: str(base / "orders" / "auto_smm_orders.json"),
        rt.ORDERS_DATA_PATH: str(base / "orders" / "orders_data.json"),
        rt.ORDERS_RUNTIME_PATH: str(base / "runtime" / "orders_runtime.json"),
        rt.AUTOSMM_STATE_PATH: str(base / "runtime" / "auto_smm_state.json"),
    }


def _legacy_storage_file_map() -> Dict[str, str]:
    return {
        rt.CONFIG_PATH: rt.LEGACY_CONFIG_PATH,
        rt.VALID_LINKS_PATH: rt.LEGACY_VALID_LINKS_PATH,
        rt.ORDERS_PATH: rt.LEGACY_ORDERS_PATH,
        rt.ORDERS_DATA_PATH: rt.LEGACY_ORDERS_DATA_PATH,
        rt.ORDERS_RUNTIME_PATH: rt.LEGACY_ORDERS_RUNTIME_PATH,
        rt.AUTOSMM_STATE_PATH: rt.LEGACY_AUTOSMM_STATE_PATH,
    }


def _sqlite_order_file_targets() -> set[str]:
    return {
        os.path.normcase(os.path.abspath(str(path)))
        for path in (rt.ORDERS_PATH, rt.ORDERS_DATA_PATH, rt.ORDERS_RUNTIME_PATH, rt.AUTOSMM_STATE_PATH)
        if path
    }


def migrate_autosmm_files_from_legacy_if_needed() -> None:
    """Migrate legacy file layouts and import JSON orders/state into SQLite once."""
    try:
        rt.ensure_storage_dirs()
        db.init_db()
        sqlite_targets = _sqlite_order_file_targets()
        already_migrated = bool(db_state.load_state().get("legacy_json_migrated_at"))

        copied: List[str] = []
        nested_sqlite_sources: List[str] = []
        for new_path, old_path in _nested_storage_file_map().items():
            is_sqlite_src = os.path.normcase(os.path.abspath(str(new_path))) in sqlite_targets
            if is_sqlite_src and already_migrated:
                if os.path.exists(old_path):
                    migrations._archive_migrated_file(old_path)
                continue
            _copy_storage_file_if_needed(new_path, old_path, copied, "nested")
            if is_sqlite_src and os.path.exists(old_path):
                nested_sqlite_sources.append(old_path)
        if copied:
            logger.debug(f"AUTO SMM STORAGE MIGRATION: nested -> flat: {', '.join(copied)}")

        legacy_copied: List[str] = []
        legacy_sqlite_sources: List[str] = []
        for new_path, legacy_path in _legacy_storage_file_map().items():
            is_sqlite_src = os.path.normcase(os.path.abspath(str(new_path))) in sqlite_targets
            if is_sqlite_src and already_migrated:
                if os.path.exists(legacy_path):
                    migrations._archive_migrated_file(legacy_path)
                continue
            _copy_storage_file_if_needed(new_path, legacy_path, legacy_copied, "storage/cache")
            if is_sqlite_src and os.path.exists(legacy_path):
                legacy_sqlite_sources.append(legacy_path)

        migrated_to_sqlite = migrations.migrate_legacy_json_to_sqlite()
        if migrated_to_sqlite or already_migrated:
            for source_path in (*nested_sqlite_sources, *legacy_sqlite_sources):
                migrations._archive_migrated_file(source_path)

        if legacy_copied:
            db_state.update_state_fields(
                legacy_files_migrated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                legacy_files_migrated_from="storage/cache",
            )
            logger.debug(f"AUTO SMM LEGACY MIGRATION: скопированы файлы: {', '.join(legacy_copied)}")
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        logger.exception(f"AUTO SMM LEGACY MIGRATION FAILED: {exc}")


def _load_autosmm_state() -> Dict[str, Any]:
    return db_state.load_state()


def _save_autosmm_state(state: Dict[str, Any]) -> bool:
    return db_state.save_state(state)


def update_state_fields(**fields: Any) -> Dict[str, Any]:
    """Atomically upsert state keys in SQLite kv_state."""
    return db_state.update_state_fields(**fields)


def _normalize_valid_links_list(links: Any, include_defaults: bool = False) -> List[str]:
    normalized: List[str] = []
    seen = set()

    def add_domain(domain_value: Any) -> None:
        domain = normalize_allowed_domain(domain_value)
        if not domain or domain in seen or not is_plausible_allowed_domain(domain):
            return
        seen.add(domain)
        normalized.append(domain)

    if isinstance(links, list):
        for item in links:
            add_domain(item)
    elif isinstance(links, str):
        add_domain(links)

    if include_defaults:
        for item in DEFAULT_VALID_LINKS:
            add_domain(item)

    return normalized


def _valid_links_defaults_migrated() -> bool:
    return bool(db_state.load_state().get(VALID_LINKS_MIGRATION_STATE_KEY, False))


def _set_valid_links_defaults_migrated() -> None:
    if _valid_links_defaults_migrated():
        return
    db_state.update_state_fields(**{VALID_LINKS_MIGRATION_STATE_KEY: True})


def _looks_like_legacy_default_whitelist(links: List[str]) -> bool:
    if not links:
        return False
    normalized = set(_normalize_valid_links_list(links, include_defaults=False))
    legacy = set(_normalize_valid_links_list(LEGACY_DEFAULT_VALID_LINKS, include_defaults=False))
    defaults = set(_normalize_valid_links_list(DEFAULT_VALID_LINKS, include_defaults=False))
    if normalized == legacy:
        return True
    if legacy.issubset(normalized) and not defaults.issubset(normalized) and len(normalized - legacy) <= 5:
        return True
    return False


def load_valid_links() -> List[str]:
    """Load trusted domains without forcing deleted defaults back into the user's list."""
    with CONFIG_LOCK:
        if not os.path.exists(rt.VALID_LINKS_PATH):
            links = _normalize_valid_links_list(DEFAULT_VALID_LINKS, include_defaults=False)
            if save_valid_links(links):
                _set_valid_links_defaults_migrated()
            return links

        raw_links = _safe_load_json(rt.VALID_LINKS_PATH, [])
        links = _normalize_valid_links_list(raw_links, include_defaults=False)
        raw_needs_normalization = not isinstance(raw_links, list) or raw_links != links

        if not _valid_links_defaults_migrated():
            saved_ok = True
            if _looks_like_legacy_default_whitelist(links):
                links = _normalize_valid_links_list(links + DEFAULT_VALID_LINKS, include_defaults=False)
                saved_ok = save_valid_links(links)
            elif raw_needs_normalization:
                saved_ok = save_valid_links(links)
            if saved_ok:
                _set_valid_links_defaults_migrated()
            return links

        if raw_needs_normalization:
            save_valid_links(links)
        return links


def save_valid_links(links: List[str]) -> bool:
    """Persist normalized allowed-link whitelist to valid_link.json."""
    normalized_links = _normalize_valid_links_list(links, include_defaults=False)
    with CONFIG_LOCK:
        return _safe_write_json(rt.VALID_LINKS_PATH, normalized_links)


def reset_valid_links_to_defaults() -> List[str]:
    """Reset the domain whitelist to built-in defaults and mark migration complete."""
    links = _normalize_valid_links_list(DEFAULT_VALID_LINKS, include_defaults=False)
    with CONFIG_LOCK:
        if save_valid_links(links):
            _set_valid_links_defaults_migrated()
            return links
    return []


def load_orders_data() -> List[Dict[str, Any]]:
    """Return all orders stored in SQLite."""
    return db_orders.list_orders()


def save_orders_data(orders_list: List[Dict[str, Any]]) -> bool:
    """Replace all orders stored in SQLite within a single atomic transaction."""
    return db_orders.replace_all_orders(orders_list)


def get_order_data(order_id_funpay: str) -> Optional[Dict[str, Any]]:
    """Return a single order dictionary by FunPay order ID, or None."""
    return db_orders.get_order(order_id_funpay)


def get_order_record(order_id_funpay: str) -> Optional[OrderRecord]:
    """Return a strongly typed OrderRecord by FunPay order ID, or None."""
    return db_orders.get_order_record(order_id_funpay)


def list_order_records(*, status: Optional[str] = None) -> List[OrderRecord]:
    """Return all orders as strongly typed OrderRecords."""
    return db_orders.list_order_records(status=status)


def upsert_order_data(
    chat_id: int,
    order_id: str,
    panel_order_id: Optional[int],
    status: str,
    chistota: float,
    customer_url: str,
    quantity: int,
    service_number: int,
    is_refunded: bool = False,
    fallback_used: Optional[bool] = False,
    fallback_reason: Optional[str] = None,
    funpay_status: Optional[str] = None,
    **extra_fields: Any,
) -> None:
    """Insert or update an order record in SQLite."""
    db_orders.upsert_order(
        chat_id=chat_id,
        order_id=order_id,
        panel_order_id=panel_order_id,
        status=status,
        chistota=chistota,
        customer_url=customer_url,
        quantity=quantity,
        service_number=service_number,
        is_refunded=is_refunded,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        funpay_status=funpay_status,
        **extra_fields,
    )


def update_order_fields(order_id_funpay: str, **fields: Any) -> bool:
    """Update specific fields on an existing order in SQLite."""
    return db_orders.update_order_fields(order_id_funpay, **fields)


def get_max_status_applied_seq() -> int:
    """Return the highest persisted status_applied_seq across stored orders."""
    return db_orders.get_max_status_applied_seq()


def claim_order_for_panel_submission(
    order_id_funpay: str,
    *,
    saved_link: str,
    service_number: Any,
    seed_fields: Optional[Mapping[str, Any]] = None,
    attempted_at: Optional[str] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Atomically mark an order as submitted to panel before firing the external add request."""
    return db_orders.claim_order_for_panel_submission(
        order_id_funpay,
        saved_link=saved_link,
        service_number=service_number,
        seed_fields=seed_fields,
        attempted_at=attempted_at,
    )


def _service_sort_key(item: Tuple[str, Any]) -> Tuple[int, str]:
    key = str(item[0] or "")
    try:
        return (0, f"{int(key):09d}")
    except (TypeError, ValueError):
        return (1, key)


def sort_service_items(services: Optional[Dict[str, Any]]) -> List[Tuple[str, Any]]:
    """Return service entries ordered deterministically by numeric panel ID then string key."""
    return sorted((services or {}).items(), key=_service_sort_key)


def rename_panel_service(
    old_service_idx: int,
    new_service_idx: int,
    new_service_name: str,
) -> Tuple[bool, str]:
    """Rename a panel service number and migrate lot mappings, SQLite orders, and runtime refs."""
    old_key, new_key = str(old_service_idx), str(new_service_idx)
    with CONFIG_LOCK, ORDERS_LOCK:
        cfg = load_config()
        services = cfg.get("services", {}) or {}
        if old_key not in services:
            return False, "❌ Сервис не найден."
        if new_key != old_key and new_key in services:
            return False, f"❌ Сервис #{new_service_idx} уже существует."

        old_cfg = copy.deepcopy(cfg)
        service_cfg = dict(services.pop(old_key))
        service_cfg["name"] = new_service_name
        services[new_key] = service_cfg
        cfg["services"] = dict(sorted(services.items(), key=_service_sort_key))

        if new_service_idx != old_service_idx:
            lot_map = cfg.get("lot_mapping")
            if isinstance(lot_map, dict):
                for lot_data in lot_map.values():
                    if not isinstance(lot_data, dict):
                        continue
                    if safe_int(lot_data.get("service_number")) == old_service_idx:
                        lot_data["service_number"] = new_service_idx
                    if safe_int(lot_data.get("fallback_service_number")) == old_service_idx:
                        lot_data["fallback_service_number"] = new_service_idx

            save_config(cfg)
            try:
                db_orders.rename_orders_service_number(old_service_idx, new_service_idx)
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                logger.exception(
                    f"Ошибка миграции заказов при смене номера панели #{old_service_idx} -> #{new_service_idx}: {exc}"
                )
                try:
                    save_config(old_cfg)
                except Exception as rb_exc:
                    logger.exception(f"Не удалось откатить конфигурацию после сбоя миграции БД: {rb_exc}")
                return False, f"❌ Не удалось обновить заказы при смене номера панели: {exc}"

            with rt.CONTEXT_LOCK:
                for ctx in list(rt.waiting_for_link.values()):
                    if isinstance(ctx, dict) and safe_int(ctx.get("service_number")) == old_service_idx:
                        ctx["service_number"] = new_service_idx
        else:
            save_config(cfg)

    return True, "OK"


def claim_panel_refund_cancel(
    order_id_funpay: str,
    attempted_at: Optional[str] = None,
) -> bool:
    """Atomically claim the one-shot panel cancel attempt slot after a FunPay refund."""
    return db_orders.claim_panel_refund_cancel(order_id_funpay, attempted_at=attempted_at)


def delete_orders_data() -> int:
    """Delete all stored orders from SQLite while keeping config and kv_state."""
    return db_orders.delete_all_orders()


def load_orders_log() -> List[Dict[str, Any]]:
    """Return log-style order accounting entries derived from SQLite."""
    entries: List[Dict[str, Any]] = []
    for rec in db_orders.list_orders():
        date_val = rec.get("date") or rec.get("date_str") or ""
        service_name = rec.get("service_name") or ""
        if not date_val and not service_name and safe_float(rec.get("summa"), 0.0) == 0.0:
            continue
        entries.append(
            {
                "date": date_val,
                "order_id": rec.get("order_id"),
                "summa": safe_float(rec.get("summa"), 0.0),
                "spent": safe_float(rec.get("spent"), 0.0),
                "currency": str(rec.get("currency") or "RUB"),
                "spent_rub": optional_float(rec.get("spent_rub")),
                "fx_rate_to_rub": optional_float(rec.get("fx_rate_to_rub")),
                "service_name": service_name,
                "chistota": safe_float(rec.get("chistota"), 0.0),
                "completed_notification_sent": bool(rec.get("completed_notification_sent", False)),
            }
        )
    return entries


def save_orders_log(log_entries: List[Dict[str, Any]]) -> bool:
    """Merge a list of legacy log entries into SQLite orders."""
    try:
        with ORDERS_LOCK:
            for entry in log_entries or []:
                if not isinstance(entry, dict):
                    continue
                oid = str(entry.get("order_id") or "").strip()
                if not oid:
                    continue
                existing = db_orders.get_order(oid)
                spent_val = optional_float(entry.get("spent"))
                spent_rub_val = optional_float(entry.get("spent_rub"))
                fx_rate_val = optional_float(entry.get("fx_rate_to_rub"))
                summa_val = optional_float(entry.get("summa"))
                chistota_val = optional_float(entry.get("chistota"))
                currency_val = str(entry.get("currency") or "").strip().upper()
                if existing is not None:
                    db_orders.update_order_fields(
                        oid,
                        date=entry.get("date") or existing.get("date"),
                        summa=summa_val if summa_val is not None else existing.get("summa"),
                        spent=spent_val if spent_val is not None else existing.get("spent"),
                        currency=currency_val or existing.get("currency") or "RUB",
                        spent_rub=spent_rub_val if spent_rub_val is not None else existing.get("spent_rub"),
                        fx_rate_to_rub=fx_rate_val if fx_rate_val is not None else existing.get("fx_rate_to_rub"),
                        service_name=entry.get("service_name") or existing.get("service_name"),
                        chistota=chistota_val if chistota_val is not None else existing.get("chistota"),
                        completed_notification_sent=bool(
                            entry.get("completed_notification_sent", existing.get("completed_notification_sent", False))
                        ),
                    )
                else:
                    db_orders.upsert_order_dict(
                        {
                            "order_id": oid,
                            "date": entry.get("date"),
                            "summa": summa_val if summa_val is not None else 0.0,
                            "spent": spent_val if spent_val is not None else 0.0,
                            "currency": currency_val or "RUB",
                            "spent_rub": spent_rub_val,
                            "fx_rate_to_rub": fx_rate_val,
                            "service_name": entry.get("service_name") or "",
                            "chistota": chistota_val if chistota_val is not None else 0.0,
                            "completed_notification_sent": bool(entry.get("completed_notification_sent", False)),
                            "status": "unknown",
                            "funpay_status": None,
                        }
                    )
        return True
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        logger.error(f"Ошибка при сохранении журнала заказов в SQLite: {exc}")
        return False


def wipe_all_autosmm_data() -> Tuple[int, List[str]]:
    """Remove all AutoSMM database, config, and legacy JSON storage files safely under storage locks."""
    removed = 0
    failed: List[str] = []
    with CONFIG_LOCK, ORDERS_LOCK, STATE_LOCK:
        for path_value in (
            rt.DB_PATH,
            f"{rt.DB_PATH}-wal",
            f"{rt.DB_PATH}-shm",
            rt.CONFIG_PATH,
            rt.CONFIG_BAK_PATH,
            rt.VALID_LINKS_PATH,
            rt.ORDERS_PATH,
            rt.ORDERS_DATA_PATH,
            rt.ORDERS_RUNTIME_PATH,
            rt.AUTOSMM_STATE_PATH,
        ):
            try:
                if os.path.exists(path_value):
                    os.remove(path_value)
                    removed += 1
            except OSError as exc:
                failed.append(path_value)
                logger.error(f"Не удалось удалить файл {path_value}: {exc}")
        rt.clear_memory_contexts()
        rt.invalidate_panel_balance_cache()
        db.invalidate_schema_cache(rt.DB_PATH)
    return removed, failed


def read_log_export_bytes() -> Tuple[str, bytes, float]:
    """Snapshot the active plugin log file safely via a temporary copy and return `(filename, content, size_kb)`."""
    import tempfile

    log_path = str(rt.LOG_PATH or "").strip()
    if not log_path or not os.path.exists(log_path):
        raise FileNotFoundError(log_path or "auto_smm.log")
    filename = os.path.basename(log_path) or "auto_smm.log"
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_export = os.path.join(tmp_dir, filename)
        shutil.copy2(log_path, tmp_export)
        with open(tmp_export, "rb") as log_file:
            data = log_file.read()
    return filename, data, round(len(data) / 1024, 1)
