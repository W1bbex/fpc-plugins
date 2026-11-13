"""One-shot legacy JSON-to-SQLite migration and v1 backup merge helpers."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional

from .. import runtime as rt
from ..models import get_effective_funpay_status, safe_float
from . import db, db_state

logger = logging.getLogger("FPC.autosmm")


def _load_json_safe(path: Optional[str], default: Any) -> Any:
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(f"MIGRATION: не удалось прочитать {path}: {exc}")
        return default


def _merge_single_log_entry(
    by_id: Dict[str, Dict[str, Any]],
    ordered_ids: List[str],
    log_rec: Mapping[str, Any],
) -> None:
    oid = str(log_rec.get("order_id") or "").strip()
    if not oid:
        return
    if oid in by_id:
        existing = by_id[oid]
        if not existing.get("date") and log_rec.get("date"):
            existing["date"] = str(log_rec["date"])
        if not existing.get("service_name") and log_rec.get("service_name"):
            existing["service_name"] = str(log_rec["service_name"])
        if safe_float(existing.get("summa"), 0.0) == 0.0 and log_rec.get("summa") is not None:
            existing["summa"] = safe_float(log_rec.get("summa"), 0.0)
        if safe_float(existing.get("chistota"), 0.0) == 0.0 and log_rec.get("chistota") is not None:
            existing["chistota"] = safe_float(log_rec.get("chistota"), 0.0)
        if bool(log_rec.get("completed_notification_sent", False)):
            existing["completed_notification_sent"] = True
        return

    ordered_ids.append(oid)
    by_id[oid] = {
        "order_id": oid,
        "date": log_rec.get("date"),
        "service_name": log_rec.get("service_name") or "",
        "status": "unknown",
        "funpay_status": None,
        "summa": safe_float(log_rec.get("summa"), 0.0),
        "spent": safe_float(log_rec.get("spent"), 0.0),
        "chistota": safe_float(log_rec.get("chistota"), 0.0),
        "currency": str(log_rec.get("currency") or "RUB"),
        "completed_notification_sent": bool(log_rec.get("completed_notification_sent", False)),
        "created_at": 0,
    }


def merge_legacy_sources(
    *,
    raw_orders: List[Mapping[str, Any]],
    runtime_map: Dict[str, Any],
    log_entries: List[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Combine legacy orders_data, orders_runtime, and auto_smm_orders into normalized dicts."""
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []

    for rec in raw_orders:
        if not isinstance(rec, Mapping):
            continue
        oid = str(rec.get("order_id") or "").strip()
        if not oid:
            continue
        item = dict(rec)
        item["order_id"] = oid
        if oid not in by_id:
            ordered_ids.append(oid)
        by_id[oid] = item

    for oid_raw, rt_fields in (runtime_map or {}).items():
        if not isinstance(rt_fields, Mapping):
            continue
        oid = str(oid_raw or "").strip()
        if oid and oid in by_id:
            for key, value in rt_fields.items():
                if value not in (None, "") or key not in by_id[oid]:
                    by_id[oid][key] = value

    for log_rec in log_entries:
        if isinstance(log_rec, Mapping):
            _merge_single_log_entry(by_id, ordered_ids, log_rec)

    for oid in ordered_ids:
        entry = by_id[oid]
        effective_fp = get_effective_funpay_status(entry)
        if effective_fp:
            entry["funpay_status"] = effective_fp
        entry.pop("is_refunded", None)

    return [by_id[oid] for oid in ordered_ids]


def _archive_migrated_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    target = f"{path}.migrated.bak"
    if os.path.exists(target):
        target = f"{path}.migrated.{int(time.time())}.bak"
    try:
        os.replace(path, target)
    except OSError as exc:
        logger.warning(f"MIGRATION: не удалось переместить {path} в {target}: {exc}")


def migrate_legacy_json_to_sqlite(
    *,
    db_path: Optional[str] = None,
    orders_data_path: Optional[str] = None,
    orders_runtime_path: Optional[str] = None,
    orders_log_path: Optional[str] = None,
    state_path: Optional[str] = None,
) -> bool:
    """Import legacy JSON order/runtime/state files into SQLite once and archive originals."""
    data_file = orders_data_path if orders_data_path is not None else rt.ORDERS_DATA_PATH
    runtime_file = orders_runtime_path if orders_runtime_path is not None else rt.ORDERS_RUNTIME_PATH
    log_file = orders_log_path if orders_log_path is not None else rt.ORDERS_PATH
    state_file = state_path if state_path is not None else rt.AUTOSMM_STATE_PATH

    candidate_files = [p for p in (data_file, runtime_file, log_file, state_file) if p and os.path.exists(p)]
    if not candidate_files:
        db.init_db(db_path)
        return False

    raw_orders_val = _load_json_safe(data_file, [])
    raw_orders = raw_orders_val if isinstance(raw_orders_val, list) else []

    runtime_val = _load_json_safe(runtime_file, {})
    runtime_map = runtime_val if isinstance(runtime_val, dict) else {}

    log_val = _load_json_safe(log_file, [])
    log_entries = log_val if isinstance(log_val, list) else []

    state_val = _load_json_safe(state_file, {})
    legacy_state = state_val if isinstance(state_val, dict) else {}

    merged_orders = merge_legacy_sources(
        raw_orders=raw_orders,
        runtime_map=runtime_map,
        log_entries=log_entries,
    )

    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = db_state.import_legacy_snapshot(merged_orders, legacy_state, migrated_at=ts_now, db_path=db_path)

    for src_path in candidate_files:
        _archive_migrated_file(src_path)

    logger.info(
        f"AUTO SMM SQLITE MIGRATION: импортировано заказов={inserted}, ключей состояния={len(legacy_state)}."
    )
    return True
