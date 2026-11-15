"""FunPay order status synchronization, refund gating, and live chat status detection."""

from __future__ import annotations

from datetime import datetime
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

try:
    from FunPayAPI import enums
except ImportError:
    enums = None

try:
    from FunPayAPI.common.utils import RegularExpressions as _FunPayRegularExpressions
except ImportError:
    _FunPayRegularExpressions = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..models import (
    get_effective_funpay_status as _get_effective_funpay_status,
    is_order_refundedish as _is_order_refundedish,
    normalize_chat_id as _normalize_chat_id,
    normalize_funpay_status as _normalize_funpay_status,
    safe_int as _safe_int,
)
from ..storage.storage import (
    ORDERS_LOCK,
    get_order_data,
    load_orders_data,
    update_order_fields,
)
from .buyer_messages import _send_buyer_message
from .engine import BUYER_STATE_REFUNDED
from .links import normalize_incoming_text
from .refunds import _attempt_panel_cancel_after_funpay_refund

BUYER_REFUND_BLOCK_TEXT = "По этому заказу оформлен возврат.\nАвтоматическое выполнение недоступно."
BUYER_FUNPAY_UNAVAILABLE_TEXT = (
    "⏳ Не удалось проверить статус заказа на FunPay (сервис временно недоступен).\n"
    "Пожалуйста, повторите попытку через 1-2 минуты."
)


class RefundGateResult:
    """Tri-state result of refund gating: OK (proceed), REFUNDED (abort), or ERROR (check failed)."""

    __slots__ = ("blocked", "status", "error")

    def __init__(self, blocked: bool, status: str = "ok", error: Optional[str] = None) -> None:
        self.blocked = bool(blocked)
        self.status = str(status)  # "ok", "refunded", "error"
        self.error = error

    @property
    def is_refunded(self) -> bool:
        return self.status == "refunded"

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    def __bool__(self) -> bool:
        return self.blocked

    def __repr__(self) -> str:
        return f"RefundGateResult(blocked={self.blocked}, status={self.status!r}, error={self.error!r})"

FUNPAY_STATUS_SYNC_INTERVAL_SECONDS = 1800
FUNPAY_STATUS_RECENT_CHECK_SECONDS = 1800
FUNPAY_STATUS_RECOVERY_CLOSED_SECONDS = 43200
FUNPAY_STATUS_RECOVERY_MAX_ORDERS = 120
FUNPAY_STATUS_BATCH_SIZE = 10

_FALLBACK_ORDER_ID_RE = re.compile(r"#([A-Za-z0-9]+)")
_MESSAGE_TYPE_NAME_TO_FUNPAY_STATUS: Dict[str, str] = {
    "ORDER_CONFIRMED": "closed",
    "ORDER_CONFIRMED_BY_ADMIN": "closed",
    "REFUND": "refunded",
    "REFUND_BY_ADMIN": "refunded",
    "PARTIAL_REFUND": "partially_refunded",
    "ORDER_REOPENED": "open",
}


def _parse_dt_or_none(value: Any) -> Optional[datetime]:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw_value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _seconds_since_checked_at(value: Any) -> Optional[float]:
    dt_value = _parse_dt_or_none(value)
    if not dt_value:
        return None
    return max(0.0, (datetime.now() - dt_value).total_seconds())


def _extract_order_id_from_system_message(message_text: str) -> Optional[str]:
    text_ = normalize_incoming_text(message_text)
    if not text_:
        return None
    if _FunPayRegularExpressions is not None:
        try:
            match = _FunPayRegularExpressions().ORDER_ID.search(text_)
            if match:
                return match.group(0).lstrip("#")
        except Exception as ex:
            rt.logger.debug(f"RegularExpressions.ORDER_ID fallback: {ex}")
    fallback_match = _FALLBACK_ORDER_ID_RE.search(text_)
    if fallback_match:
        return fallback_match.group(1)
    return None


def _resolve_message_type_funpay_status(msg_type: Any) -> Optional[str]:
    if msg_type is None:
        return None
    message_types = getattr(enums, "MessageTypes", None)
    if message_types is not None:
        for enum_name, normalized in _MESSAGE_TYPE_NAME_TO_FUNPAY_STATUS.items():
            candidate = getattr(message_types, enum_name, None)
            if candidate is not None and msg_type == candidate:
                return normalized
    type_name = getattr(msg_type, "name", None)
    if isinstance(type_name, str):
        return _MESSAGE_TYPE_NAME_TO_FUNPAY_STATUS.get(type_name)
    return None


def _extract_funpay_status_from_event_message(message: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extract FunPay order status strictly from FunPay system messages (author_id == 0)."""
    if message is None or getattr(message, "author_id", None) != 0:
        return None, None
    # Reject system events where the current account acted as the buyer rather than seller.
    if getattr(message, "i_am_seller", None) is False:
        return None, None

    funpay_status = _resolve_message_type_funpay_status(getattr(message, "type", None))
    if not funpay_status:
        return None, None

    order_id = _extract_order_id_from_system_message(getattr(message, "text", "") or "")
    if not order_id:
        return None, None
    return funpay_status, order_id


def _apply_funpay_status_update(
    order_id_funpay: str,
    order_record: Dict[str, Any],
    new_funpay_status: str,
    *,
    checked_at: str,
    source: str,
    refund_source: str,
    swallow_save_error: bool = False,
) -> Dict[str, Any]:
    """Persist verified FunPay order status and trigger optional panel cancellation on full refund."""
    normalized_status = _normalize_funpay_status(new_funpay_status)
    if not normalized_status:
        return dict(order_record)

    with ORDERS_LOCK:
        previous_funpay_status = _get_effective_funpay_status(get_order_data(order_id_funpay)) or _get_effective_funpay_status(order_record)
        if previous_funpay_status == "refunded" and normalized_status != "refunded":
            rt.logger.info(
                f"FUNPAY STATUS REGRESSION BLOCKED: order=#{order_id_funpay}, "
                f"{previous_funpay_status}->{normalized_status}, source={source}"
            )
            return dict(order_record)
        if previous_funpay_status == "partially_refunded" and normalized_status in ("open", "closed"):
            rt.logger.info(
                f"FUNPAY STATUS REGRESSION BLOCKED: order=#{order_id_funpay}, "
                f"{previous_funpay_status}->{normalized_status}, source={source}"
            )
            return dict(order_record)

        fields: Dict[str, Any] = {
            "funpay_status": normalized_status,
            "funpay_status_checked_at": checked_at,
        }
        if normalized_status in ("refunded", "partially_refunded"):
            fields.update(
                {
                    "refund_detected_at": checked_at,
                    "refund_source": refund_source,
                    "buyer_flow_state": BUYER_STATE_REFUNDED,
                    "saved_link": None,
                    "updated_at": checked_at,
                }
            )

        try:
            update_order_fields(order_id_funpay, **fields)
        except (sqlite3.Error, OSError) as ex:
            if not swallow_save_error:
                raise
            rt.logger.warning(f"FUNPAY STATUS SAVE FAILED: order=#{order_id_funpay}, source={source}, error={ex}")
            if normalized_status in ("refunded", "partially_refunded"):
                with rt.CONTEXT_LOCK:
                    rt.waiting_for_link.pop(str(order_id_funpay), None)
            fallback_record = dict(order_record)
            fallback_record.update(fields)
            return fallback_record

        if normalized_status in ("refunded", "partially_refunded"):
            with rt.CONTEXT_LOCK:
                rt.waiting_for_link.pop(str(order_id_funpay), None)

        updated_order = get_order_data(order_id_funpay) or dict(order_record)
        updated_order.update(fields)

    if normalized_status == "refunded" and previous_funpay_status != "refunded":
        try:
            _attempt_panel_cancel_after_funpay_refund(updated_order, source=source)
        except (requests.RequestException, sqlite3.Error, OSError, ValueError, TypeError) as ex:
            rt.logger.warning(f"PANEL CANCEL AFTER REFUND FAILED: order=#{order_id_funpay}, source={source}, error={ex}")

    return updated_order


def _sync_funpay_status_from_event(c: Optional["Cardinal"], message: Any) -> bool:
    """Apply FunPay status updates from authentic FunPay system chat messages."""
    del c
    funpay_status, order_id = _extract_funpay_status_from_event_message(message)
    if not funpay_status or not order_id:
        return False

    with rt.get_order_lock(order_id):
        order_data = get_order_data(order_id)
        if not order_data:
            rt.logger.info(f"FUNPAY STATUS LIVE: заказ #{order_id} не найден в БД AutoSMM (status={funpay_status})")
            return True

        stored_chat_id = _normalize_chat_id(order_data.get("chat_id"))
        msg_chat_id = _normalize_chat_id(getattr(message, "chat_id", None))
        if stored_chat_id is not None and msg_chat_id is not None and stored_chat_id != msg_chat_id:
            rt.logger.warning(
                f"FUNPAY STATUS LIVE REJECTED: chat_id mismatch for order=#{order_id} "
                f"(stored={stored_chat_id}, event={msg_chat_id})"
            )
            return False

        _apply_funpay_status_update(
            order_id,
            order_data,
            funpay_status,
            checked_at=rt.now_str(),
            source="funpay_live",
            refund_source="funpay_message",
        )
        rt.logger.info(f"FUNPAY STATUS LIVE: заказ #{order_id} обновлён до '{funpay_status}'")
        return True


def _sync_funpay_status_from_order_event(c: "Cardinal", event: Any) -> bool:
    """Synchronize AutoSMM order status when Cardinal emits OrderStatusChangedEvent."""
    order_obj = getattr(event, "order", None)
    order_id = str(getattr(order_obj, "id", "") or "").strip().lstrip("#")
    if not order_id:
        return False

    with rt.get_order_lock(order_id):
        order_data = get_order_data(order_id)
        if not order_data:
            return False

        mapped_status = _map_cardinal_order_status_to_funpay_status(getattr(order_obj, "status", None))
        if not mapped_status:
            return False

        # Cardinal's OrderShortcut from get_sales() folds PARTIALLY_REFUNDED into REFUNDED;
        # confirm via full order fetch before irreversible full-refund cancellation.
        if mapped_status == "refunded":
            confirm = _fetch_funpay_status_via_cardinal(c, order_id)
            if not confirm.get("ok"):
                rt.logger.warning(
                    f"FUNPAY ORDER STATUS EVENT CONFIRM FAILED: order=#{order_id}, "
                    f"error={confirm.get('error')}, detail={confirm.get('detail')}"
                )
                return False
            mapped_status = str(confirm.get("funpay_status") or mapped_status)

        _apply_funpay_status_update(
            order_id,
            order_data,
            mapped_status,
            checked_at=rt.now_str(),
            source="order_status_event",
            refund_source="order_status_event",
        )
        rt.logger.info(f"FUNPAY ORDER STATUS EVENT: заказ #{order_id} обновлён до '{mapped_status}'")
        return True


def _map_cardinal_order_status_to_funpay_status(order_status: Any) -> Optional[str]:
    order_statuses = getattr(enums, "OrderStatuses", None)
    if order_statuses is not None:
        status_mapping = {
            getattr(order_statuses, "PAID", None): "open",
            getattr(order_statuses, "CLOSED", None): "closed",
            getattr(order_statuses, "REFUNDED", None): "refunded",
            getattr(order_statuses, "PARTIALLY_REFUNDED", None): "partially_refunded",
        }
        for candidate, normalized in status_mapping.items():
            if candidate is not None and order_status == candidate:
                return normalized

    raw_status = str(order_status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw_status.endswith(".paid") or raw_status == "paid":
        return "open"
    if raw_status.endswith(".closed") or raw_status == "closed":
        return "closed"
    if raw_status.endswith(".refunded") or raw_status == "refunded":
        return "refunded"
    if "partially_refunded" in raw_status:
        return "partially_refunded"
    return None


def _parse_fetched_order_status(order_data: Any, order_id_funpay: str) -> Dict[str, Any]:
    if order_data is None:
        return {
            "ok": False,
            "error": "order_not_found",
            "detail": f"Заказ #{order_id_funpay} не найден в ответе FunPay",
        }
    normalized_status = _map_cardinal_order_status_to_funpay_status(getattr(order_data, "status", None))
    if not normalized_status:
        return {
            "ok": False,
            "error": "unknown_funpay_status",
            "detail": f"Не удалось сопоставить order.status для заказа #{order_id_funpay}",
        }
    return {"ok": True, "funpay_status": normalized_status}


def _fetch_funpay_status_via_cardinal(c: "Cardinal", order_id_funpay: str) -> Dict[str, Any]:
    """Fetch current FunPay order status via Cardinal account client."""
    account = getattr(c, "account", None)
    get_order_fn = getattr(account, "get_order", None)
    if not callable(get_order_fn):
        return {
            "ok": False,
            "error": "cardinal_client_unavailable",
            "detail": "c.account.get_order is not available",
        }
    try:
        order_data = get_order_fn(order_id_funpay)
        return _parse_fetched_order_status(order_data, order_id_funpay)
    except Exception as ex:
        return {"ok": False, "error": "cardinal_error", "detail": str(ex)}


def _fetch_funpay_statuses_batch(c: "Cardinal", order_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch FunPay order statuses via get_orders_by_ids (up to 10 per call) with single-order fallback."""
    clean_ids = [str(oid).strip().lstrip("#") for oid in order_ids if str(oid).strip()]
    if not clean_ids:
        return {}

    account = getattr(c, "account", None)
    batch_fn = getattr(account, "get_orders_by_ids", None)
    results: Dict[str, Dict[str, Any]] = {}

    if callable(batch_fn):
        for start in range(0, len(clean_ids), FUNPAY_STATUS_BATCH_SIZE):
            chunk = clean_ids[start : start + FUNPAY_STATUS_BATCH_SIZE]
            try:
                raw_batch = batch_fn(*chunk)
                if not isinstance(raw_batch, dict):
                    raise TypeError(f"Unexpected get_orders_by_ids return type: {type(raw_batch).__name__}")
                normalized_map = {str(k).strip().lstrip("#"): v for k, v in raw_batch.items()}
                for oid in chunk:
                    if oid in normalized_map:
                        results[oid] = _parse_fetched_order_status(normalized_map[oid], oid)
                    else:
                        results[oid] = _fetch_funpay_status_via_cardinal(c, oid)
            except Exception as ex:
                rt.logger.warning(f"FUNPAY BATCH FETCH FAILED (chunk={chunk}): {ex}")
                for oid in chunk:
                    results[oid] = _fetch_funpay_status_via_cardinal(c, oid)
        return results

    for oid in clean_ids:
        results[oid] = _fetch_funpay_status_via_cardinal(c, oid)
    return results


def _refresh_funpay_status_for_order(
    c: Optional["Cardinal"],
    order_record: Optional[Dict],
    force_check: bool = False,
) -> Dict:
    """Refresh FunPay status for one stored order without blocking or inventing state on errors."""
    if not isinstance(order_record, dict):
        return {}

    order_id_funpay = str(
        order_record.get("order_id")
        or order_record.get("order_id_funpay")
        or ""
    ).strip().lstrip("#")
    if not order_id_funpay:
        return dict(order_record)

    latest_order = get_order_data(order_id_funpay) or dict(order_record)

    if not force_check and not _funpay_status_needs_recheck(latest_order, FUNPAY_STATUS_RECENT_CHECK_SECONDS):
        cached = dict(latest_order)
        cached["_funpay_check_ok"] = True
        return cached

    account = getattr(c, "account", None)
    get_order_fn = getattr(account, "get_order", None)
    if c is None or not callable(get_order_fn):
        cached = dict(latest_order)
        cached["_funpay_check_ok"] = None
        return cached

    result = _fetch_funpay_status_via_cardinal(c, order_id_funpay)
    checked_at = rt.now_str()

    if not result.get("ok"):
        # Do not advance funpay_status_checked_at on failed API checks so retries are not skipped.
        rt.logger.warning(
            f"FUNPAY STATUS REFRESH FAILED: order=#{order_id_funpay}, "
            f"error={result.get('error')}, detail={result.get('detail')}"
        )
        failed_copy = dict(latest_order)
        failed_copy["_funpay_check_ok"] = False
        failed_copy["_funpay_check_error"] = str(result.get("error") or "fetch_failed")
        return failed_copy

    updated = _apply_funpay_status_update(
        order_id_funpay,
        latest_order,
        str(result.get("funpay_status") or ""),
        checked_at=checked_at,
        source="refund_gate_refresh",
        refund_source="refund_gate_refresh",
        swallow_save_error=True,
    )
    if isinstance(updated, dict):
        updated["_funpay_check_ok"] = True
    return updated


def _apply_refund_gate(
    c: Optional["Cardinal"],
    order_record: Optional[Dict],
    notify_chat_id: Optional[int] = None,
    force_check: bool = False,
    block_on_error: bool = False,
) -> RefundGateResult:
    """Evaluate FunPay refund status. Return RefundGateResult (falsy if OK to proceed, truthy if blocked)."""
    if not isinstance(order_record, dict):
        return RefundGateResult(blocked=False, status="ok")

    latest_order = _refresh_funpay_status_for_order(c, order_record, force_check=force_check)
    if not isinstance(latest_order, dict) or not latest_order:
        latest_order = order_record

    order_id_funpay = str(
        latest_order.get("order_id")
        or latest_order.get("order_id_funpay")
        or order_record.get("order_id")
        or order_record.get("order_id_funpay")
        or ""
    ).strip().lstrip("#")

    # If FunPay status could not be verified and caller requested strict blocking, do not proceed with paid submission
    if block_on_error and latest_order.get("_funpay_check_ok") is False:
        rt.logger.warning(
            f"REFUND GATE BLOCKED ON ERROR: order=#{order_id_funpay}, "
            f"error={latest_order.get('_funpay_check_error')}"
        )
        if notify_chat_id is not None and c is not None:
            try:
                _send_buyer_message(c, notify_chat_id, BUYER_FUNPAY_UNAVAILABLE_TEXT)
            except Exception as ex:
                rt.logger.warning(f"REFUND GATE NOTIFY FAILED: order=#{order_id_funpay}, error={ex}")
        return RefundGateResult(
            blocked=True,
            status="error",
            error=str(latest_order.get("_funpay_check_error") or "fetch_failed"),
        )

    if not _is_order_refundedish(latest_order):
        return RefundGateResult(blocked=False, status="ok")

    if order_id_funpay:
        try:
            update_order_fields(
                order_id_funpay,
                buyer_flow_state=BUYER_STATE_REFUNDED,
                saved_link=None,
                updated_at=rt.now_str(),
            )
        except (sqlite3.Error, OSError) as ex:
            rt.logger.warning(f"REFUND GATE SAVE FAILED: order=#{order_id_funpay}, error={ex}")
        with rt.CONTEXT_LOCK:
            rt.waiting_for_link.pop(order_id_funpay, None)

    if notify_chat_id is not None and c is not None:
        try:
            _send_buyer_message(c, notify_chat_id, BUYER_REFUND_BLOCK_TEXT)
        except Exception as ex:
            rt.logger.warning(f"REFUND GATE NOTIFY FAILED: order=#{order_id_funpay}, error={ex}")

    return RefundGateResult(blocked=True, status="refunded")


def _funpay_status_needs_recheck(order_record: Dict, min_age_seconds: int) -> bool:
    checked_age = _seconds_since_checked_at(order_record.get("funpay_status_checked_at"))
    if checked_age is None:
        return True
    return checked_age >= int(min_age_seconds)


def _collect_funpay_status_candidates(all_orders: List[Dict], recovery: bool = False) -> List[Dict]:
    open_candidates: List[Dict] = []
    closed_candidates: List[Dict] = []

    for order_record in all_orders:
        order_id = str(order_record.get("order_id") or "")
        if not order_id:
            continue

        effective_funpay_status = _get_effective_funpay_status(order_record)
        if effective_funpay_status in ("refunded", "partially_refunded"):
            continue

        if effective_funpay_status in (None, "open"):
            if _funpay_status_needs_recheck(order_record, FUNPAY_STATUS_RECENT_CHECK_SECONDS):
                open_candidates.append(order_record)
            continue

        if recovery and effective_funpay_status == "closed":
            if _funpay_status_needs_recheck(order_record, FUNPAY_STATUS_RECOVERY_CLOSED_SECONDS):
                closed_candidates.append(order_record)

    def _sort_key(order_record: Dict):
        checked_dt = _parse_dt_or_none(order_record.get("funpay_status_checked_at"))
        created_at = int(order_record.get("created_at") or 0)
        return (checked_dt is not None, checked_dt or datetime.min, created_at)

    open_candidates = sorted(open_candidates, key=_sort_key)
    if not recovery:
        return open_candidates

    closed_candidates = sorted(closed_candidates, key=_sort_key)
    return (open_candidates + closed_candidates)[:FUNPAY_STATUS_RECOVERY_MAX_ORDERS]


def _run_funpay_status_sync(c: "Cardinal", recovery: bool = False) -> None:
    """Synchronize FunPay order statuses in batches for open (and on recovery, closed) orders."""
    try:
        all_orders = load_orders_data()
    except (sqlite3.Error, OSError, ValueError) as ex:
        rt.logger.error(f"Ошибка при загрузке orders для funpay status sync: {ex}")
        return

    candidates = _collect_funpay_status_candidates(all_orders, recovery=recovery)
    if not candidates:
        return

    sync_type = "recovery-check" if recovery else "background funpay sync"
    rt.logger.info(f"FUNPAY STATUS SYNC: {sync_type}, orders={len(candidates)}")

    for start in range(0, len(candidates), FUNPAY_STATUS_BATCH_SIZE):
        if not rt.RUNNING:
            break

        chunk_records = candidates[start : start + FUNPAY_STATUS_BATCH_SIZE]
        chunk_ids = [str(rec.get("order_id") or "").strip().lstrip("#") for rec in chunk_records]
        batch_results = _fetch_funpay_statuses_batch(c, chunk_ids)
        checked_at = rt.now_str()

        for order_record, order_id_funpay in zip(chunk_records, chunk_ids):
            if not rt.RUNNING:
                break
            if not order_id_funpay:
                continue

            result = batch_results.get(order_id_funpay) or {
                "ok": False,
                "error": "missing_batch_result",
                "detail": "Заказ отсутствует в результатах batch-проверки",
            }
            if result.get("ok"):
                new_funpay_status = str(result.get("funpay_status") or "")
                fresh_record = get_order_data(order_id_funpay) or order_record
                if _get_effective_funpay_status(fresh_record) == "refunded" and new_funpay_status == "open":
                    continue
                _apply_funpay_status_update(
                    order_id_funpay,
                    fresh_record,
                    new_funpay_status,
                    checked_at=checked_at,
                    source=sync_type,
                    refund_source=sync_type,
                )
                rt.logger.info(
                    f"FUNPAY STATUS SYNC: order=#{order_id_funpay}, status={new_funpay_status}, source={sync_type}"
                )
                continue

            # Keep previous funpay_status_checked_at untouched so failed orders are retried next cycle.
            rt.logger.warning(
                f"FUNPAY STATUS SYNC FAILED: order=#{order_id_funpay}, source={sync_type}, "
                f"error={result.get('error')}, detail={result.get('detail')}"
            )
