"""SQLite order persistence, row serialization, and atomic order state transitions."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .. import runtime as rt
from ..models import (
    BuyerFlowState,
    OrderRecord,
    SmmOrderDTO,
    extract_progress_values,
    get_effective_funpay_status,
    is_refunded_funpay_status,
    normalize_funpay_status,
    optional_float,
    resolve_panel_order_id,
    safe_float,
    normalize_chat_id,
    safe_int,
)
from .db import _connect, _json_dumps_optional, _json_loads_optional

logger = logging.getLogger("FPC.autosmm")

_KNOWN_ORDER_KEYS = {
    "order_id", "chat_id", "buyer_id", "buyer_username", "lot_title",
    "service_name", "service_id", "service_number", "panel_order_id",
    "id_zakaz", "twiboost_id", "status", "panel_status", "funpay_status",
    "is_refunded", "buyer_flow_state", "customer_url", "saved_link",
    "quantity", "total_quantity", "done_quantity", "left_quantity",
    "progress_percent", "order_amount", "order_price", "summa", "spent",
    "spent_rub", "fx_rate_to_rub", "chistota", "currency", "fallback_used",
    "fallback_reason", "completed_notification_sent", "status_last_checked_at",
    "status_last_error", "incorrect_order_id_attempts", "status_stop_reason",
    "manual_check_alert_sent", "funpay_status_checked_at", "refund_detected_at",
    "refund_source", "panel_refund_cancel_attempted_at",
    "panel_refund_cancel_result", "last_prompt_ts", "created_at",
    "updated_at", "date", "date_str",
}

_NULLABLE_TEXT_FIELDS = (
    "buyer_username", "lot_title", "service_name", "panel_status",
    "funpay_status", "buyer_flow_state", "customer_url", "saved_link",
    "fallback_reason", "status_last_checked_at", "status_last_error",
    "status_stop_reason", "funpay_status_checked_at", "refund_detected_at",
    "refund_source", "panel_refund_cancel_attempted_at",
    "panel_refund_cancel_result", "last_prompt_ts", "updated_at", "date",
)

_ROW_OPTIONAL_TEXT_KEYS = tuple(
    k for k in _NULLABLE_TEXT_FIELDS if k not in ("customer_url", "fallback_reason", "date")
)

_UPSERT_ORDER_SQL = """
INSERT INTO orders (
    order_id, chat_id, buyer_id, buyer_id_json, buyer_username, lot_title, service_name,
    service_id, service_number, panel_order_id, status, panel_status, funpay_status,
    buyer_flow_state, customer_url, saved_link, quantity, total_quantity, done_quantity,
    left_quantity, progress_percent, order_amount_json, order_price_json, summa, spent,
    spent_rub, fx_rate_to_rub, chistota, currency, fallback_used, fallback_reason,
    completed_notification_sent, status_last_checked_at, status_last_error,
    incorrect_order_id_attempts, status_stop_reason, manual_check_alert_sent,
    funpay_status_checked_at, refund_detected_at, refund_source,
    panel_refund_cancel_attempted_at, panel_refund_cancel_result, last_prompt_ts,
    created_at, updated_at, date, extra_json
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
ON CONFLICT(order_id) DO UPDATE SET
    chat_id = excluded.chat_id,
    buyer_id = excluded.buyer_id,
    buyer_id_json = excluded.buyer_id_json,
    buyer_username = excluded.buyer_username,
    lot_title = excluded.lot_title,
    service_name = excluded.service_name,
    service_id = excluded.service_id,
    service_number = excluded.service_number,
    panel_order_id = excluded.panel_order_id,
    status = excluded.status,
    panel_status = excluded.panel_status,
    funpay_status = excluded.funpay_status,
    buyer_flow_state = excluded.buyer_flow_state,
    customer_url = excluded.customer_url,
    saved_link = excluded.saved_link,
    quantity = excluded.quantity,
    total_quantity = excluded.total_quantity,
    done_quantity = excluded.done_quantity,
    left_quantity = excluded.left_quantity,
    progress_percent = excluded.progress_percent,
    order_amount_json = excluded.order_amount_json,
    order_price_json = excluded.order_price_json,
    summa = excluded.summa,
    spent = excluded.spent,
    spent_rub = excluded.spent_rub,
    fx_rate_to_rub = excluded.fx_rate_to_rub,
    chistota = excluded.chistota,
    currency = excluded.currency,
    fallback_used = excluded.fallback_used,
    fallback_reason = excluded.fallback_reason,
    completed_notification_sent = excluded.completed_notification_sent,
    status_last_checked_at = excluded.status_last_checked_at,
    status_last_error = excluded.status_last_error,
    incorrect_order_id_attempts = excluded.incorrect_order_id_attempts,
    status_stop_reason = excluded.status_stop_reason,
    manual_check_alert_sent = excluded.manual_check_alert_sent,
    funpay_status_checked_at = excluded.funpay_status_checked_at,
    refund_detected_at = excluded.refund_detected_at,
    refund_source = excluded.refund_source,
    panel_refund_cancel_attempted_at = excluded.panel_refund_cancel_attempted_at,
    panel_refund_cancel_result = excluded.panel_refund_cancel_result,
    last_prompt_ts = excluded.last_prompt_ts,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    date = excluded.date,
    extra_json = excluded.extra_json
"""


def _normalize_fallback_pair(fallback_used: Any, fallback_reason: Any) -> Tuple[Optional[int], Optional[str]]:
    if fallback_used is True:
        reason = None if fallback_reason in (None, "") else str(fallback_reason)
        return 1, reason
    if fallback_reason not in (None, ""):
        return 0, str(fallback_reason)
    return None, None


def _decode_order_row_metadata(data: Dict[str, Any], row: sqlite3.Row) -> None:
    for key in _ROW_OPTIONAL_TEXT_KEYS:
        val = row[key]
        if val not in (None, ""):
            data[key] = val

    panel_order_id = row["panel_order_id"]
    if panel_order_id is not None:
        panel_oid = int(panel_order_id)
        data["panel_order_id"] = panel_oid
        data["id_zakaz"] = panel_oid
    else:
        data["id_zakaz"] = None

    for key in ("total_quantity", "done_quantity", "left_quantity", "progress_percent"):
        data[key] = int(row[key] or 0)

    for col, target in (("order_amount_json", "order_amount"), ("order_price_json", "order_price")):
        parsed = _json_loads_optional(row[col], None)
        if parsed is not None:
            data[target] = parsed

    if row["fallback_used"] is not None:
        data["fallback_used"] = bool(row["fallback_used"])
        if row["fallback_reason"] not in (None, ""):
            data["fallback_reason"] = row["fallback_reason"]
    elif row["fallback_reason"] not in (None, ""):
        data["fallback_used"] = False
        data["fallback_reason"] = row["fallback_reason"]

    if row["date"] not in (None, ""):
        data["date"] = row["date"]
        data["date_str"] = row["date"]


def _row_to_order_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "order_id": str(row["order_id"]),
        "status": str(row["status"] or "pending"),
        "customer_url": str(row["customer_url"] or ""),
        "quantity": int(row["quantity"] or 0),
        "service_number": int(row["service_number"] if row["service_number"] is not None else 1),
        "summa": float(row["summa"] or 0.0),
        "spent": float(row["spent"] or 0.0),
        "chistota": float(row["chistota"] or 0.0),
        "currency": str(row["currency"] or "RUB"),
        "created_at": int(row["created_at"] or 0),
        "completed_notification_sent": bool(row["completed_notification_sent"]),
        "incorrect_order_id_attempts": int(row["incorrect_order_id_attempts"] or 0),
        "manual_check_alert_sent": bool(row["manual_check_alert_sent"]),
    }
    if row["spent_rub"] is not None:
        data["spent_rub"] = float(row["spent_rub"])
    if row["fx_rate_to_rub"] is not None:
        data["fx_rate_to_rub"] = float(row["fx_rate_to_rub"])
    if row["chat_id"] is not None:
        data["chat_id"] = normalize_chat_id(row["chat_id"])
    if row["service_id"] is not None:
        data["service_id"] = int(row["service_id"])

    buyer_id = _json_loads_optional(row["buyer_id_json"], None)
    if buyer_id is None and row["buyer_id"] is not None:
        buyer_id = row["buyer_id"]
    if buyer_id is not None:
        data["buyer_id"] = buyer_id

    _decode_order_row_metadata(data, row)

    extra = _json_loads_optional(row["extra_json"], {})
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key not in data and value is not None:
                data[key] = value
    return data


def _prepare_order_params(raw: Mapping[str, Any]) -> Optional[Tuple[Any, ...]]:
    order_id = str(raw.get("order_id") or "").strip()
    if not order_id:
        return None

    buyer_raw = raw.get("buyer_id")
    raw_summa = raw.get("summa")
    if raw_summa is None:
        raw_summa = raw.get("order_price", 0.0)
    summa = safe_float(raw_summa, 0.0)
    chistota_raw = raw.get("chistota")
    chistota = summa if chistota_raw is None else safe_float(chistota_raw, 0.0)
    fallback_used_int, fallback_reason = _normalize_fallback_pair(
        raw.get("fallback_used"),
        raw.get("fallback_reason"),
    )

    progress = extract_progress_values(raw)
    total_qty = safe_int(raw.get("total_quantity"))
    done_qty = safe_int(raw.get("done_quantity"))
    left_qty = safe_int(raw.get("left_quantity"))
    prog_pct = safe_int(raw.get("progress_percent"))

    date_val = raw.get("date") or raw.get("date_str")
    extra = {k: v for k, v in raw.items() if k not in _KNOWN_ORDER_KEYS and v is not None}

    def _clean(key: str) -> Optional[str]:
        val = raw.get(key)
        return None if val in (None, "") else str(val)

    return (
        order_id,
        normalize_chat_id(raw.get("chat_id")),
        None if buyer_raw is None else str(buyer_raw),
        _json_dumps_optional(buyer_raw),
        str(raw.get("buyer_username") or ""),
        str(raw.get("lot_title") or ""),
        str(raw.get("service_name") or ""),
        safe_int(raw.get("service_id")),
        safe_int(raw.get("service_number")) if raw.get("service_number") is not None else 1,
        resolve_panel_order_id(raw),
        str(raw.get("status") or "pending"),
        _clean("panel_status"),
        get_effective_funpay_status(raw),
        _clean("buyer_flow_state"),
        str(raw.get("customer_url") or ""),
        _clean("saved_link"),
        safe_int(raw.get("quantity")) or 0,
        total_qty if total_qty is not None else progress["total_quantity"],
        done_qty if done_qty is not None else progress["done_quantity"],
        left_qty if left_qty is not None else progress["left_quantity"],
        prog_pct if prog_pct is not None else progress["progress_percent"],
        _json_dumps_optional(raw.get("order_amount")),
        _json_dumps_optional(raw.get("order_price")),
        summa,
        safe_float(raw.get("spent"), 0.0),
        optional_float(raw.get("spent_rub")),
        optional_float(raw.get("fx_rate_to_rub")),
        chistota,
        str(raw.get("currency") or "RUB"),
        fallback_used_int,
        fallback_reason,
        1 if bool(raw.get("completed_notification_sent", False)) else 0,
        _clean("status_last_checked_at"),
        _clean("status_last_error"),
        safe_int(raw.get("incorrect_order_id_attempts")) or 0,
        _clean("status_stop_reason"),
        1 if bool(raw.get("manual_check_alert_sent", False)) else 0,
        _clean("funpay_status_checked_at"),
        _clean("refund_detected_at"),
        _clean("refund_source"),
        _clean("panel_refund_cancel_attempted_at"),
        _clean("panel_refund_cancel_result"),
        _clean("last_prompt_ts"),
        safe_int(raw.get("created_at")) or 0,
        _clean("updated_at"),
        None if date_val in (None, "") else str(date_val),
        json.dumps(extra, ensure_ascii=False),
    )


def get_order(order_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch a single order by FunPay order ID."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return None
    with _connect(db_path, read_only=True) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        return _row_to_order_dict(row) if row is not None else None


def list_orders(
    db_path: Optional[str] = None,
    *,
    status: Optional[str] = None,
    exclude_statuses: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Return orders ordered by creation timestamp, optionally filtered by status."""
    clauses: List[str] = []
    params: List[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(str(status))
    if exclude_statuses:
        excluded = [str(item) for item in exclude_statuses]
        placeholders = ", ".join("?" for _ in excluded)
        clauses.append(f"status NOT IN ({placeholders})")
        params.extend(excluded)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM orders {where_sql} ORDER BY created_at ASC, rowid ASC"
    with _connect(db_path, read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_order_dict(row) for row in rows]


def get_order_record(order_id: str, db_path: Optional[str] = None) -> Optional[OrderRecord]:
    """Fetch a single order and return a strongly typed OrderRecord."""
    data = get_order(order_id, db_path=db_path)
    return OrderRecord.from_dict(data) if data is not None else None


def list_order_records(
    db_path: Optional[str] = None,
    *,
    status: Optional[str] = None,
    exclude_statuses: Optional[Iterable[str]] = None,
) -> List[OrderRecord]:
    """Return orders as strongly typed OrderRecords."""
    return [
        OrderRecord.from_dict(d)
        for d in list_orders(db_path=db_path, status=status, exclude_statuses=exclude_statuses)
    ]


def replace_all_orders(orders: Iterable[Mapping[str, Any]], db_path: Optional[str] = None) -> bool:
    """Replace all rows in the orders table within one atomic transaction."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM orders")
        params_list = [
            prepared
            for item in (orders or [])
            if isinstance(item, Mapping) and (prepared := _prepare_order_params(item)) is not None
        ]
        if params_list:
            conn.executemany(_UPSERT_ORDER_SQL, params_list)
    return True


def upsert_order_dict(order: Mapping[str, Any], db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Insert or update an order row from a full dictionary."""
    prepared = _prepare_order_params(order)
    if prepared is None:
        return None
    target_id = str(prepared[0])
    with _connect(db_path) as conn:
        conn.execute(_UPSERT_ORDER_SQL, prepared)
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        return _row_to_order_dict(row) if row is not None else None


def _apply_fallback_fields(target: Dict[str, Any], fallback_used: Any, fallback_reason: Any) -> None:
    if fallback_used is True:
        target["fallback_used"] = True
        if fallback_reason not in (None, ""):
            target["fallback_reason"] = fallback_reason
        else:
            target.pop("fallback_reason", None)
    elif fallback_reason not in (None, ""):
        target["fallback_used"] = False
        target["fallback_reason"] = fallback_reason
    else:
        target.pop("fallback_used", None)
        target.pop("fallback_reason", None)


def upsert_order(
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
    db_path: Optional[str] = None,
    **extra_fields: Any,
) -> Dict[str, Any]:
    """Create or update an order while preserving existing accounting/runtime attributes."""
    target_id = str(order_id or "").strip()
    now_ts = int(time.time())
    with _connect(db_path) as conn:
        existing_row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        existing = _row_to_order_dict(existing_row) if existing_row is not None else {}

        existing_fp = normalize_funpay_status(existing.get("funpay_status"))
        if is_refunded_funpay_status(existing_fp) or is_refunded:
            normalized_funpay_status = "refunded"
        else:
            normalized_funpay_status = (
                normalize_funpay_status(funpay_status)
                or existing_fp
                or "open"
            )

        existing_summa = safe_float(existing.get("summa"), 0.0)
        existing_spent = safe_float(existing.get("spent"), 0.0)
        existing_chistota = safe_float(existing.get("chistota"), 0.0)
        incoming_chistota = safe_float(chistota, 0.0)

        merged = dict(existing)
        merged.update(
            {
                "order_id": target_id,
                "chat_id": normalize_chat_id(chat_id),
                "panel_order_id": panel_order_id,
                "id_zakaz": panel_order_id,
                "status": status,
                "customer_url": customer_url,
                "quantity": quantity,
                "service_number": service_number,
                "funpay_status": normalized_funpay_status,
                "summa": existing_summa if existing_summa > 0.0 else incoming_chistota,
                "spent": existing_spent,
                "chistota": existing_chistota if (existing_chistota != 0.0 or existing_spent > 0.0) else incoming_chistota,
                "currency": str(existing.get("currency") or "RUB"),
                "created_at": safe_int(existing.get("created_at")) or now_ts,
                "completed_notification_sent": bool(existing.get("completed_notification_sent", False)),
            }
        )
        merged.pop("is_refunded", None)
        _apply_fallback_fields(merged, fallback_used, fallback_reason)

        for key, value in extra_fields.items():
            if value is not None:
                merged[key] = value

        prepared = _prepare_order_params(merged)
        if prepared is None:
            raise ValueError(f"Invalid order_id for upsert_order: {order_id!r}")
        conn.execute(_UPSERT_ORDER_SQL, prepared)
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        result = _row_to_order_dict(row) if row is not None else merged
    logger.info(
        f"Данные заказа #{target_id} (panel order ID: {panel_order_id}) сохранены/обновлены. "
        f"fallback_used={(bool(fallback_used) if fallback_used or fallback_reason not in (None, '') else 'not_set')}, "
        f"fallback_reason={fallback_reason}"
    )
    return result


def _apply_partial_order_field(order: Dict[str, Any], key: str, value: Any) -> None:
    if key == "funpay_status":
        norm_status = normalize_funpay_status(value)
        if norm_status:
            order["funpay_status"] = norm_status
        elif value is None:
            order.pop("funpay_status", None)
        return
    if key in ("id_zakaz", "twiboost_id", "panel_order_id"):
        oid_val = safe_int(value)
        if oid_val is None:
            order.pop("panel_order_id", None)
            order.pop("id_zakaz", None)
        else:
            order["panel_order_id"] = oid_val
            order["id_zakaz"] = oid_val
        return
    if key in ("date", "date_str"):
        if value in (None, ""):
            order.pop("date", None)
            order.pop("date_str", None)
        else:
            order["date"] = str(value)
            order["date_str"] = str(value)
        return
    if value is None or (key in _NULLABLE_TEXT_FIELDS and value == ""):
        order.pop(key, None)
    else:
        order[key] = value


def update_order_fields(order_id: str, db_path: Optional[str] = None, **fields: Any) -> bool:
    """Partially update selected fields on an existing order row."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return False
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        if row is None:
            logger.warning(f"Заказ #{target_id} не найден в базе данных.")
            return False

        order = _row_to_order_dict(row)
        for key, value in fields.items():
            if key not in ("fallback_used", "fallback_reason", "is_refunded"):
                _apply_partial_order_field(order, key, value)

        if "fallback_used" in fields or "fallback_reason" in fields:
            _apply_fallback_fields(order, fields.get("fallback_used"), fields.get("fallback_reason"))

        prepared = _prepare_order_params(order)
        if prepared is None:
            return False
        conn.execute(_UPSERT_ORDER_SQL, prepared)
    return True


def get_max_status_applied_seq(db_path: Optional[str] = None) -> int:
    """Return the highest persisted status_applied_seq across all orders."""
    max_seq = 0
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT extra_json FROM orders WHERE extra_json LIKE '%status_applied_seq%'"
        ).fetchall()
    for row in rows:
        extra = _json_loads_optional(row["extra_json"], {})
        if isinstance(extra, dict):
            seq = safe_int(extra.get("status_applied_seq"))
            if seq is not None and seq > max_seq:
                max_seq = seq
    return max_seq


def mark_order_refunded_atomic(
    order_id: str,
    *,
    detected_at: Optional[str] = None,
    source: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], bool]:
    """Atomically mark an order refunded and return (updated, snapshot, found)."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return False, None, False

    ts_now = detected_at or rt.now_str()
    src = source or "refund_mark"
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        if row is None:
            return False, None, False

        current = _row_to_order_dict(row)
        if is_refunded_funpay_status(get_effective_funpay_status(current)):
            return False, current, True

        current["funpay_status"] = "refunded"
        current["buyer_flow_state"] = BuyerFlowState.REFUNDED.value
        current.pop("saved_link", None)
        current["refund_detected_at"] = ts_now
        current["refund_source"] = src
        current["updated_at"] = ts_now

        prepared = _prepare_order_params(current)
        if prepared is None:
            return False, current, True
        conn.execute(_UPSERT_ORDER_SQL, prepared)
        updated_row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        snapshot = _row_to_order_dict(updated_row) if updated_row is not None else current
    return True, snapshot, True


def claim_completion_notification(
    order_id: str,
    db_path: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Atomically claim one-shot buyer completion notification for a completed open order."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return False, None
    ts_now = rt.now_str()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        if row is None:
            return False, None
        current = _row_to_order_dict(row)
        if str(current.get("status") or "").strip().lower() != "completed":
            return False, current
        if bool(current.get("completed_notification_sent", False)):
            return False, current
        if get_effective_funpay_status(current) != "open":
            return False, current
        current["completed_notification_sent"] = True
        current["buyer_flow_state"] = BuyerFlowState.COMPLETED.value
        current["panel_status"] = "completed"
        current["updated_at"] = ts_now
        prepared = _prepare_order_params(current)
        if prepared is None:
            return False, current
        conn.execute(_UPSERT_ORDER_SQL, prepared)
        updated_row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        snapshot = _row_to_order_dict(updated_row) if updated_row is not None else current
    return True, snapshot


def claim_manual_check_alert(
    order_id: str,
    db_path: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Atomically claim one-shot admin manual-check alert for an order."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return False, None
    ts_now = rt.now_str()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        if row is None:
            return False, None
        current = _row_to_order_dict(row)
        if bool(current.get("manual_check_alert_sent", False)):
            return False, current
        current["manual_check_alert_sent"] = True
        current["updated_at"] = ts_now
        prepared = _prepare_order_params(current)
        if prepared is None:
            return False, current
        conn.execute(_UPSERT_ORDER_SQL, prepared)
        updated_row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        snapshot = _row_to_order_dict(updated_row) if updated_row is not None else current
    return True, snapshot


def claim_order_for_panel_submission(
    order_id: str,
    *,
    saved_link: str,
    service_number: Any,
    seed_fields: Optional[Mapping[str, Any]] = None,
    attempted_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Atomically claim a pre-panel order and persist in-flight submission state before API add."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return False, "invalid_order_id", None

    ts_now = attempted_at or rt.now_str()
    svc_num = safe_int(service_number) if safe_int(service_number) is not None else 1
    terminal_or_blocked_states = {
        BuyerFlowState.PANEL_CREATED.value,
        BuyerFlowState.COMPLETED.value,
        BuyerFlowState.FAILED.value,
        BuyerFlowState.REFUNDED.value,
    }
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        if row is not None:
            current = _row_to_order_dict(row)
            if is_refunded_funpay_status(get_effective_funpay_status(current)):
                return False, "refunded", current
            if resolve_panel_order_id(current) is not None:
                return False, "panel_created", current
            flow_state = str(current.get("buyer_flow_state") or "").strip().lower()
            stop_reason = str(current.get("status_stop_reason") or "").strip().lower()
            panel_status_val = str(current.get("panel_status") or "").strip().lower()
            if (
                bool(current.get("panel_add_submitted"))
                or flow_state in terminal_or_blocked_states
                or panel_status_val == "manual_check"
                or stop_reason in ("panel_add_in_flight", "transport_error_after_add", "persist_failed_after_add")
            ):
                return False, "already_submitted", current
            merged = dict(current)
        else:
            seed = dict(seed_fields or {})
            merged = {
                "order_id": target_id,
                "chat_id": normalize_chat_id(seed.get("chat_id")),
                "buyer_id": seed.get("buyer_id"),
                "buyer_username": str(seed.get("buyer_username") or ""),
                "lot_title": str(seed.get("lot_title") or ""),
                "service_name": str(seed.get("lot_title") or seed.get("service_name") or "—"),
                "service_id": safe_int(seed.get("service_id")),
                "service_number": svc_num,
                "quantity": safe_int(seed.get("quantity")) or 0,
                "total_quantity": safe_int(seed.get("quantity")) or 0,
                "left_quantity": safe_int(seed.get("quantity")) or 0,
                "summa": safe_float(seed.get("summa"), 0.0),
                "spent": 0.0,
                "chistota": 0.0,
                "currency": "RUB",
                "funpay_status": "open",
                "created_at": safe_int(seed.get("created_at")) or int(time.time()),
                "date": str(seed.get("date") or ts_now),
            }
            for optional_key in ("lot_key", "order_amount", "order_price"):
                if seed.get(optional_key) is not None:
                    merged[optional_key] = seed[optional_key]

        merged.update(
            {
                "order_id": target_id,
                "panel_add_submitted": True,
                "panel_add_attempted_at": ts_now,
                "panel_add_service_number": svc_num,
                "buyer_flow_state": BuyerFlowState.FAILED.value,
                "status": "unknown",
                "panel_status": "manual_check",
                "status_stop_reason": "panel_add_in_flight",
                "saved_link": saved_link,
                "customer_url": saved_link,
                "updated_at": ts_now,
            }
        )
        prepared = _prepare_order_params(merged)
        if prepared is None:
            return False, "invalid_order_id", None
        conn.execute(_UPSERT_ORDER_SQL, prepared)
        updated_row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (target_id,)).fetchone()
        snapshot = _row_to_order_dict(updated_row) if updated_row is not None else merged
    return True, "claimed", snapshot


def rename_orders_service_number(
    old_service_number: int,
    new_service_number: int,
    db_path: Optional[str] = None,
) -> int:
    """Update order service_number references atomically when a panel number is renamed."""
    old_num = safe_int(old_service_number)
    new_num = safe_int(new_service_number)
    if old_num is None or new_num is None or old_num == new_num:
        return 0
    ts_now = rt.now_str()
    updated = 0
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        for row in rows:
            order = _row_to_order_dict(row)
            changed = False
            if safe_int(order.get("service_number")) == old_num:
                order["service_number"] = new_num
                changed = True
            if safe_int(order.get("panel_add_service_number")) == old_num:
                order["panel_add_service_number"] = new_num
                changed = True
            if not changed:
                continue
            order["updated_at"] = ts_now
            prepared = _prepare_order_params(order)
            if prepared is not None:
                conn.execute(_UPSERT_ORDER_SQL, prepared)
                updated += 1
    return updated


def claim_panel_refund_cancel(
    order_id: str,
    attempted_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Atomically claim the one-shot panel cancel attempt slot after a FunPay refund."""
    target_id = str(order_id or "").strip()
    if not target_id:
        return False
    ts_value = str(attempted_at or rt.now_str())
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE orders
            SET panel_refund_cancel_attempted_at = ?, updated_at = ?
            WHERE order_id = ?
              AND (panel_refund_cancel_attempted_at IS NULL OR TRIM(panel_refund_cancel_attempted_at) = '')
            """,
            (ts_value, ts_value, target_id),
        )
        return int(cursor.rowcount or 0) > 0


def delete_all_orders(db_path: Optional[str] = None) -> int:
    """Delete all rows from orders while preserving kv_state entries."""
    with _connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM orders")
        return int(cursor.rowcount or 0)
