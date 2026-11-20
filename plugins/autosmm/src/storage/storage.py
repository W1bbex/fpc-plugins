"""Storage entry points and operations that coordinate configuration and order state."""

from __future__ import annotations

import copy
import logging
import os
import shutil
import sqlite3
import threading
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import runtime as rt
from ..models import (
    OrderRecord,
    is_plausible_allowed_domain,
    normalize_allowed_domain,
    safe_int,
)
from . import db, db_orders, db_state
from .config_storage import (
    CONFIG_LOCK,
    _safe_load_json,
    _safe_write_json,
    _sync_runtime_config_refs as _sync_runtime_config_refs,
    _write_bytes_atomic as _write_bytes_atomic,
    load_config,
    reindex_lots as reindex_lots,
    save_config,
    update_config as update_config,
)
from .defaults import (
    DEFAULT_VALID_LINKS,
    LEGACY_DEFAULT_VALID_LINKS,
)
from .migrations import migrate_autosmm_files_from_legacy_if_needed as migrate_autosmm_files_from_legacy_if_needed
from .order_log import (
    load_orders_log as load_orders_log,
    save_orders_log as save_orders_log,
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


ORDERS_LOCK = _DynamicDbLock()
STATE_LOCK = _DynamicDbLock()
VALID_LINKS_MIGRATION_STATE_KEY = "valid_links_defaults_migrated"

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
