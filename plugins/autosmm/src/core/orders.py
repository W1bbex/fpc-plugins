"""Order status polling, batch panel checks, and order reference lookup."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..models import (
    is_order_refundedish as _is_order_refundedish,
    safe_float as _safe_float,
)
from ..services import analytics
from ..services.currency import compute_order_profit_rub, normalize_currency_code
from ..services.notifications import (
    _send_completion_notification_if_needed,
    _send_manual_check_alert_once,
)
from ..services.telemetry import _maybe_notify_telemetry_guest_expiring
from ..storage.storage import (
    ORDERS_LOCK,
    STATE_LOCK,
    _load_autosmm_state,
    _save_autosmm_state,
    get_order_data,
    load_orders_data,
    update_order_fields,
)
from .engine import (
    BUYER_STATE_COMPLETED,
    BUYER_STATE_FAILED,
    BUYER_STATE_PANEL_CREATED,
)
from .funpay_sync import FUNPAY_STATUS_SYNC_INTERVAL_SECONDS, _run_funpay_status_sync
from ..models import safe_int as _safe_int
from .panel_api import (
    _fetch_batch_order_status,
    _fetch_single_order_status,
)
from .panel_balances import get_cached_service_balance
from .panel_errors import _build_status_result_from_entry

STATUS_BATCH_SIZE = 50
STATUS_BATCH_INTERVAL_SECONDS = 300
INCORRECT_ORDER_ID_MAX_ATTEMPTS = 3

_STATUS_SEQ_LOCK = threading.Lock()
_STATUS_SEQ_COUNTER = 0


def _next_order_status_seq() -> int:
    global _STATUS_SEQ_COUNTER
    with _STATUS_SEQ_LOCK:
        _STATUS_SEQ_COUNTER += 1
        return _STATUS_SEQ_COUNTER


def _is_stale_status_update(
    latest: Mapping[str, Any],
    *,
    mapped_status: Optional[str] = None,
    request_seq: Optional[int] = None,
    allow_completed_regression: bool = False,
) -> Optional[str]:
    last_applied_seq = _safe_int(latest.get("status_applied_seq")) or 0
    if request_seq is not None and request_seq < last_applied_seq:
        return f"stale_seq ({request_seq} < {last_applied_seq})"
    current_status = str(latest.get("status") or "").strip().lower()
    if (
        not allow_completed_regression
        and current_status == "completed"
        and mapped_status in (None, "pending", "unknown")
    ):
        return f"completed_regression ({current_status} -> {mapped_status})"
    return None

_SOFT_STATUS_ERRORS = (
    "timeout",
    "rate_limited",
    "network_error",
    "http_error",
    "invalid_json",
    "user_inactive",
    "service_inactive",
    "insufficient_funds",
    "unsupported_action",
)


def _record_plugin_start_state() -> None:
    with STATE_LOCK:
        state = _load_autosmm_state()
        state["last_plugin_start_at"] = rt.now_str()
        _save_autosmm_state(state)


def _mark_order_unknown(
    order_record: Dict,
    stop_reason: str,
    log_detail: str,
    alert_reason: str = "не удалось подтвердить состояние заказа",
    *,
    request_seq: Optional[int] = None,
) -> None:
    order_id_funpay = str(order_record.get("order_id"))
    fields = {
        "status": "unknown",
        "status_stop_reason": stop_reason,
        "status_last_error": stop_reason,
        "status_last_checked_at": rt.now_str(),
        "status_applied_seq": request_seq if request_seq is not None else _next_order_status_seq(),
    }
    with ORDERS_LOCK:
        latest = get_order_data(order_id_funpay)
        if latest is None or _is_order_refundedish(latest):
            return
        stale_reason = _is_stale_status_update(latest, mapped_status="unknown", request_seq=request_seq)
        if stale_reason:
            rt.logger.info(f"ORDER UNKNOWN SKIPPED ({stale_reason}): order=#{order_id_funpay}")
            return
        rt.logger.error(f"ORDER STATUS UNKNOWN: order=#{order_id_funpay}, reason={stop_reason}, detail={log_detail}")
        update_order_fields(order_id_funpay, **fields)
        order_record.update(latest)
        order_record.update(fields)
    _send_manual_check_alert_once(order_record, alert_reason)


def _apply_status_result(
    c: "Cardinal",
    order_record: Dict,
    result: Dict,
    background_sync: bool = False,
    *,
    request_seq: Optional[int] = None,
    allow_completed_regression: bool = False,
) -> None:
    order_id_funpay = str(order_record.get("order_id"))
    latest_initial = get_order_data(order_id_funpay)
    if latest_initial is None or _is_order_refundedish(latest_initial):
        rt.logger.info(f"STATUS APPLY SKIPPED FOR REFUNDED/MISSING ORDER: order=#{order_id_funpay}")
        return

    mapped_status = str(result.get("status", "unknown") or "unknown").lower()
    charge = _safe_float(result.get("charge"), 0.0)
    raw_res_cur = result.get("currency")
    if raw_res_cur not in (None, ""):
        currency = normalize_currency_code(raw_res_cur)
    else:
        srv_num = _safe_int(order_record.get("service_number")) or 1
        srv_balance = get_cached_service_balance(srv_num)
        panel_currency = srv_balance.get("currency")
        if panel_currency not in (None, ""):
            currency = normalize_currency_code(panel_currency)
        else:
            currency = normalize_currency_code(order_record.get("currency") or "RUB")
    lot_summa = _safe_float(latest_initial.get("summa"), _safe_float(order_record.get("summa"), 0.0))

    fx_info: Optional[Dict[str, Any]] = None
    try:
        fx_info = compute_order_profit_rub(lot_summa, charge, currency)
    except (ValueError, TypeError) as ex:
        rt.logger.error(f"Не удалось рассчитать затраты/профит для заказа #{order_id_funpay}: {ex}")

    with ORDERS_LOCK:
        latest = get_order_data(order_id_funpay)
        if latest is None or _is_order_refundedish(latest):
            rt.logger.info(f"STATUS APPLY SKIPPED AFTER CONCURRENT REFUND: order=#{order_id_funpay}")
            return
        stale_reason = _is_stale_status_update(
            latest,
            mapped_status=mapped_status,
            request_seq=request_seq,
            allow_completed_regression=allow_completed_regression,
        )
        if stale_reason:
            rt.logger.info(f"STATUS APPLY SKIPPED ({stale_reason}): order=#{order_id_funpay}")
            return

        applied_seq = request_seq if request_seq is not None else _next_order_status_seq()

        progress_fields: Dict[str, Any] = {"panel_status": mapped_status}
        remains_value = _safe_int(result.get("remains"))
        total_quantity = _safe_int(
            latest.get("total_quantity")
            or latest.get("quantity")
            or order_record.get("total_quantity")
            or order_record.get("quantity")
        ) or 0
        if remains_value is not None:
            progress_fields["left_quantity"] = max(0, remains_value)
            if total_quantity:
                done_quantity = max(0, total_quantity - progress_fields["left_quantity"])
                progress_fields["done_quantity"] = done_quantity
                progress_fields["progress_percent"] = int(max(0, min(100, round((done_quantity / total_quantity) * 100))))
        elif mapped_status == "completed" and total_quantity:
            progress_fields["done_quantity"] = total_quantity
            progress_fields["left_quantity"] = 0
            progress_fields["progress_percent"] = 100

        if mapped_status == "completed":
            progress_fields["buyer_flow_state"] = BUYER_STATE_COMPLETED
        elif mapped_status in ("fail", "canceled", "partial", "unknown"):
            progress_fields["buyer_flow_state"] = BUYER_STATE_FAILED
        else:
            progress_fields["buyer_flow_state"] = BUYER_STATE_PANEL_CREATED

        fields = {
            "status": mapped_status,
            "status_last_checked_at": rt.now_str(),
            "status_last_error": None,
            "incorrect_order_id_attempts": 0,
            "status_applied_seq": applied_seq,
            "updated_at": rt.now_str(),
            **progress_fields,
        }
        fields["status_stop_reason"] = "unknown_status" if mapped_status == "unknown" else None

        update_order_fields(order_id_funpay, **fields)
        order_record.update(latest)
        order_record.update(fields)

    if fx_info is not None:
        try:
            analytics.update_order_charge_and_net(
                order_id_funpay,
                fx_info["spent"],
                currency=fx_info["currency"],
                net_profit=fx_info["net_profit"],
                spent_rub=fx_info["spent_rub"],
                fx_rate_to_rub=fx_info["fx_rate_to_rub"],
            )
        except (sqlite3.Error, OSError, ValueError, TypeError) as ex:
            rt.logger.error(f"Не удалось обновить затраты/профит для заказа #{order_id_funpay}: {ex}")

    if mapped_status == "completed":
        _send_completion_notification_if_needed(c, order_record)
    elif mapped_status == "partial":
        _send_manual_check_alert_once(order_record, "заказ выполнен частично (partial)")
    elif mapped_status == "unknown":
        _send_manual_check_alert_once(order_record, "не удалось подтвердить состояние заказа")


def _handle_incorrect_order_id(order_record: Dict[str, Any], *, request_seq: Optional[int] = None) -> None:
    """Retry bounded times on the order's assigned panel before stopping as unknown."""
    order_id_funpay = str(order_record.get("order_id"))
    reached_limit = False
    attempts = 0
    with ORDERS_LOCK:
        latest = get_order_data(order_id_funpay)
        if latest is None or _is_order_refundedish(latest):
            return
        stale_reason = _is_stale_status_update(latest, mapped_status="unknown", request_seq=request_seq)
        if stale_reason:
            rt.logger.info(f"INCORRECT ORDER ID SKIPPED ({stale_reason}): order=#{order_id_funpay}")
            return
        attempts = int(latest.get("incorrect_order_id_attempts", 0) or 0) + 1
        fields: Dict[str, Any] = {
            "incorrect_order_id_attempts": attempts,
            "status_last_error": "incorrect_order_id",
            "status_last_checked_at": rt.now_str(),
            "status_applied_seq": request_seq if request_seq is not None else _next_order_status_seq(),
        }
        if attempts >= INCORRECT_ORDER_ID_MAX_ATTEMPTS:
            reached_limit = True
            fields.update({
                "status": "unknown",
                "status_stop_reason": "incorrect_order_id",
            })
        update_order_fields(order_id_funpay, **fields)
        order_record.update(latest)
        order_record.update(fields)

    if reached_limit:
        rt.logger.warning(
            f"ORDER STATUS STOPPED: order #{order_id_funpay} переведён в unknown после {attempts} попыток lookup по incorrect order id"
        )
        _send_manual_check_alert_once(order_record, "не удалось подтвердить состояние заказа")
    else:
        rt.logger.warning(
            f"ORDER STATUS RETRY: order #{order_id_funpay}, попытка {attempts}/{INCORRECT_ORDER_ID_MAX_ATTEMPTS}"
        )


def _handle_panel_status_error(
    c: "Cardinal",
    order_record: Dict,
    service_number: int,
    error_code: str,
    detail: Any,
    background_sync: bool = False,
    *,
    request_seq: Optional[int] = None,
) -> None:
    if error_code == "incorrect_order_id":
        _handle_incorrect_order_id(order_record, request_seq=request_seq)
        return
    if error_code in _SOFT_STATUS_ERRORS:
        order_id_funpay = str(order_record.get("order_id"))
        with ORDERS_LOCK:
            latest = get_order_data(order_id_funpay)
            if latest is None or _is_order_refundedish(latest):
                return
            if _is_stale_status_update(latest, mapped_status=None, request_seq=request_seq):
                return
            update_order_fields(
                order_id_funpay,
                status_last_error=error_code,
                status_last_checked_at=rt.now_str(),
            )
        return
    _mark_order_unknown(order_record, "status_unknown", detail, request_seq=request_seq)


def _sync_single_order_status(c: "Cardinal", order_record: Dict, background_sync: bool = False) -> None:
    if not order_record or _is_order_refundedish(order_record):
        return
    if str(order_record.get("status", "unknown") or "unknown").lower() != "pending":
        return

    panel_order_id = _safe_int(order_record.get("id_zakaz"))
    if panel_order_id is None:
        return
    service_number = int(order_record.get("service_number") or 1)
    order_id_funpay = str(order_record.get("order_id"))

    req_seq = _next_order_status_seq()
    result = _fetch_single_order_status(service_number, panel_order_id)
    if result.get("ok"):
        _apply_status_result(c, order_record, result, background_sync=background_sync, request_seq=req_seq)
        return

    error_code = result.get("error", "unknown")
    detail = result.get("detail", "")
    rt.logger.warning(f"STATUS CHECK RESULT: order=#{order_id_funpay}, service=#{service_number}, error={error_code}, detail={detail}")
    _handle_panel_status_error(
        c,
        order_record,
        service_number,
        error_code,
        detail,
        background_sync=background_sync,
        request_seq=req_seq,
    )


def _is_batch_transport_error(error_code: Any) -> bool:
    return str(error_code or "").strip().lower() in ("timeout", "rate_limited", "network_error", "invalid_json", "http_error", "unknown")


def _save_batch_failure_for_chunk(chunk: List[Dict], error_code: Any, detail: Any) -> None:
    batch_error = f"batch_{str(error_code or 'unknown').strip().lower() or 'unknown'}"
    checked_at = rt.now_str()
    for order_record in chunk:
        order_id_funpay = str(order_record.get("order_id") or "")
        if not order_id_funpay:
            continue
        latest = get_order_data(order_id_funpay)
        if latest is None or _is_order_refundedish(latest):
            continue
        update_order_fields(order_id_funpay, status_last_error=batch_error, status_last_checked_at=checked_at)
        order_record["status_last_error"] = batch_error
        order_record["status_last_checked_at"] = checked_at
    rt.logger.warning(f"BATCH STATUS SOFT FAIL: error={batch_error}, detail={detail}")


_NON_FALLBACK_BATCH_ERRORS = frozenset({
    "invalid_api_key",
    "invalid_key",
    "missing_api_key",
    "user_inactive",
    "service_inactive",
})


def _fallback_chunk_to_single_status(c: "Cardinal", chunk: List[Dict], error_code: Any, detail: Any) -> None:
    _save_batch_failure_for_chunk(chunk, error_code, detail)
    normalized_error = str(error_code or "").strip().lower()
    if normalized_error in _NON_FALLBACK_BATCH_ERRORS:
        return
    for order_record in chunk:
        if not rt.RUNNING:
            break
        try:
            _sync_single_order_status(c, order_record, background_sync=True)
        except Exception as ex:
            rt.logger.warning(
                f"SINGLE STATUS FALLBACK FAILED: order=#{order_record.get('order_id')}, error={ex}"
            )


def _check_pending_orders_batch(c: "Cardinal") -> None:
    try:
        all_orders = load_orders_data()
    except (sqlite3.Error, OSError, ValueError) as ex:
        rt.logger.error(f"Ошибка при загрузке orders для batch status: {ex}")
        return

    pending_orders = [
        r for r in all_orders
        if str(r.get("status", "unknown") or "unknown").lower() == "pending"
        and not _is_order_refundedish(r)
        and _safe_int(r.get("id_zakaz")) is not None
    ]
    if not pending_orders:
        return

    grouped: Dict[int, List[Dict]] = {}
    for order_record in pending_orders:
        grouped.setdefault(int(order_record.get("service_number") or 1), []).append(order_record)

    for service_number, orders_group in grouped.items():
        if rt.STOP_EVENT.is_set():
            return
        for start_idx in range(0, len(orders_group), STATUS_BATCH_SIZE):
            if rt.STOP_EVENT.is_set():
                return
            chunk = orders_group[start_idx:start_idx + STATUS_BATCH_SIZE]
            order_ids = [oid for oid in (_safe_int(r.get("id_zakaz")) for r in chunk) if oid is not None]
            if not order_ids:
                continue

            batch_seq = _next_order_status_seq()
            result = _fetch_batch_order_status(service_number, order_ids)
            if rt.STOP_EVENT.is_set():
                return
            if not result.get("ok"):
                error_code = result.get("error", "unknown")
                detail = result.get("detail", "")
                if _is_batch_transport_error(error_code):
                    rt.logger.warning(f"BATCH STATUS RETRY: service=#{service_number}, orders={order_ids}, error={error_code}, detail={detail}")
                    batch_seq = _next_order_status_seq()
                    retry_result = _fetch_batch_order_status(service_number, order_ids)
                    if rt.STOP_EVENT.is_set():
                        return
                    if not retry_result.get("ok"):
                        _fallback_chunk_to_single_status(
                            c,
                            chunk,
                            retry_result.get("error", error_code),
                            retry_result.get("detail", detail),
                        )
                        continue
                    result = retry_result
                else:
                    _fallback_chunk_to_single_status(c, chunk, error_code, detail)
                    continue

            entries = result.get("entries", {}) or {}
            for order_record in chunk:
                if rt.STOP_EVENT.is_set():
                    return
                order_oid = _safe_int(order_record.get("id_zakaz"))
                if order_oid is None:
                    continue
                order_key = str(order_oid)
                entry = entries.get(order_key)
                if entry is None:
                    rt.logger.warning(f"BATCH STATUS MISSING ENTRY: service=#{service_number}, order={order_key}. Перехожу на single status.")
                    _sync_single_order_status(c, order_record, background_sync=True)
                    continue

                entry_result = _build_status_result_from_entry(entry)
                if entry_result.get("ok"):
                    _apply_status_result(c, order_record, entry_result, background_sync=True, request_seq=batch_seq)
                    continue

                entry_error = entry_result.get("error", "unknown")
                rt.logger.warning(
                    f"BATCH STATUS ENTRY ERROR: order={order_record.get('order_id')}, "
                    f"service=#{service_number}, error={entry_error}, detail={entry_result.get('detail')}"
                )
                _handle_panel_status_error(
                    c,
                    order_record,
                    service_number,
                    entry_error,
                    entry_result.get("detail", ""),
                    background_sync=True,
                    request_seq=batch_seq,
                )


def check_order_status(
    c: "Cardinal",
    panel_order_id: int,
    buyer_chat_id: int,
    link: str,
    order_id_funpay: str,
    attempt: int = 1,
) -> None:
    """Perform a single status sync right after order creation; further checks run in batch."""
    order_record = get_order_data(order_id_funpay)
    if not order_record:
        rt.logger.warning(f"Заказ {order_id_funpay} не найден при check_order_status.")
        return

    _sync_single_order_status(c, order_record)


def start_order_checking(c: "Cardinal") -> None:
    """Run startup recovery sync followed by the periodic batch status polling loop."""
    rt.logger.info("Запуск batch status мониторинга pending-заказов")
    _record_plugin_start_state()

    try:
        _run_funpay_status_sync(c, recovery=True)
    except Exception as ex:
        rt.logger.error(f"Ошибка в recovery-check funpay status: {ex}")

    last_funpay_status_sync_ts = time.time()

    while rt.RUNNING and not rt.STOP_EVENT.is_set():
        try:
            _check_pending_orders_batch(c)
        except Exception as ex:
            rt.logger.error(f"Ошибка в batch status мониторинге: {ex}")
        if not rt.RUNNING or rt.STOP_EVENT.is_set():
            break

        rt.prune_stale_contexts()

        try:
            if time.time() - last_funpay_status_sync_ts >= FUNPAY_STATUS_SYNC_INTERVAL_SECONDS:
                _run_funpay_status_sync(c, recovery=False)
                last_funpay_status_sync_ts = time.time()
        except Exception as ex:
            rt.logger.error(f"Ошибка в background funpay status sync: {ex}")

        try:
            _maybe_notify_telemetry_guest_expiring(c)
        except Exception as ex:
            rt.logger.error(f"Ошибка проверки гостевого режима телеметрии: {ex}")

        rt.wait_with_stop(STATUS_BATCH_INTERVAL_SECONDS, step=1.0)


def _normalize_order_ref_for_lookup(raw_ref: Any) -> str:
    return str(raw_ref or "").strip().lstrip("#").upper()


def _find_order_by_any_order_ref(
    raw_ref: Any,
    *,
    buyer_id: Any = None,
    buyer_chat_id: Any = None,
    access_checker: Optional[Any] = None,
) -> Optional[Dict]:
    """Find a unique order matching either FunPay ID or panel ID without guessing on collisions."""
    ref = _normalize_order_ref_for_lookup(raw_ref)
    if not ref:
        return None
    all_orders = [order for order in load_orders_data() if isinstance(order, dict)]

    def _caller_can_access(order_record: Dict) -> bool:
        if buyer_id in (None, "") and buyer_chat_id in (None, ""):
            return True
        if callable(access_checker):
            return bool(access_checker(order_record, buyer_id, buyer_chat_id))
        buyer_match = buyer_id not in (None, "") and str(order_record.get("buyer_id")) == str(buyer_id)
        chat_match = buyer_chat_id not in (None, "") and str(order_record.get("chat_id")) == str(buyer_chat_id)
        return bool(buyer_match or chat_match)

    def _collect_matches(fields: Tuple[str, ...], normalize_value: bool) -> List[Dict]:
        result: List[Dict] = []
        for order_record in all_orders:
            for field_name in fields:
                raw_val = order_record.get(field_name)
                value = _normalize_order_ref_for_lookup(raw_val) if normalize_value else str(raw_val or "").strip()
                if value and value == ref:
                    if _caller_can_access(order_record):
                        result.append(order_record)
                    break
        return result

    searches = (
        (("panel_order_id", "twiboost_id", "id_zakaz"), False),
        (("order_id", "order_id_funpay"), True),
    ) if ref.isdigit() else (
        (("order_id", "order_id_funpay"), True),
        (("panel_order_id", "twiboost_id", "id_zakaz"), False),
    )
    for field_group, should_normalize in searches:
        matches = _collect_matches(field_group, should_normalize)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            rt.logger.warning("ORDER REF AMBIGUOUS: ref=%s matched %s local records", ref, len(matches))
            return None
    return None
