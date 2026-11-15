"""Buyer chat flow, commands (!smm/!link/!cancel), and waiting-state recovery."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..models import (
    is_order_refundedish as _is_order_refundedish,
    normalize_funpay_status as _normalize_funpay_status,
    safe_float,
    safe_int,
)
from ..services.notifications import _send_admin_order_alert
from ..storage import db_orders
from ..storage.defaults import DEFAULT_BUYER_COMMANDS_TEMPLATE, DEFAULT_LINK_CONFIRM_TEMPLATE
from ..storage.storage import (
    ORDERS_LOCK,
    get_order_data,
    load_config,
    load_orders_data,
    update_order_fields,
)
from .buyer_messages import (
    BUYER_ACTIVE_PRE_PANEL_STATES,
    BUYER_AUTO_CANCEL_FAILED_TEXT,
    BUYER_AUTO_CANCEL_UNAVAILABLE_TEXT,
    BUYER_CONFIRM_REMINDER_SECONDS,
    BUYER_PANEL_CREATED_BLOCK_TEXT,
    CONFIRM_REMINDER_TEXT,
    ORDER_NOT_FOUND_TEXT,
    _buyer_selection_key,
    _format_buyer_smm_orders,
    _format_order_not_found_for_buyer,
    _format_short_order_list,
    _matches_order_ref,
    _render_template,
    _send_buyer_message,
)
from .engine import (
    BUYER_STATE_COMPLETED,
    BUYER_STATE_FAILED,
    BUYER_STATE_LINK_PAUSED,
    BUYER_STATE_PANEL_CREATED,
    BUYER_STATE_REFUNDED,
    BUYER_STATE_WAITING_CONFIRM,
    BUYER_STATE_WAITING_LINK,
    _get_buyer_flow_state,
    _get_panel_order_id,
    _has_panel_order,
    _state_from_step,
    _step_from_state,
)
from .links import (
    BUYER_CONFIRM_NO_WORDS,
    BUYER_CONFIRM_YES_WORDS,
    INVALID_LINK_SCHEME_TEXT,
    _extract_buyer_target,
    _extract_http_link,
    _looks_like_link_without_https,
    is_valid_buyer_target,
)
from .funpay_sync import (
    FUNPAY_STATUS_BATCH_SIZE,
    _apply_funpay_status_update,
    _apply_refund_gate,
    _fetch_funpay_status_via_cardinal,
    _fetch_funpay_statuses_batch,
    _seconds_since_checked_at,
)
from .panel_submission import process_link_without_confirmation


def _upsert_buyer_flow_order_record(
    chat_id: int,
    buyer_id: Any,
    buyer_username: Any,
    order_id_funpay: str,
    lot_title: str,
    order_price: Any,
    order_amount: Any,
    service_id: int,
    real_amount: int,
    service_number: int,
    funpay_status: str = "open",
    state: str = BUYER_STATE_WAITING_LINK,
    saved_link: Optional[str] = None,
    lot_key: Optional[str] = None,
) -> bool:
    """Create or update a FunPay order record and its buyer-flow runtime state atomically."""
    order_id_str = str(order_id_funpay)
    now_str_val = rt.now_str()
    with ORDERS_LOCK:
        base = db_orders.get_order(order_id_str) or {}
        if base:
            base_state = _get_buyer_flow_state(base)
            stop_reason = str(base.get("status_stop_reason") or "").strip().lower()
            panel_status_val = str(base.get("panel_status") or "").strip().lower()
            protected_states = {
                BUYER_STATE_REFUNDED,
                BUYER_STATE_PANEL_CREATED,
                BUYER_STATE_COMPLETED,
                BUYER_STATE_FAILED,
                BUYER_STATE_WAITING_CONFIRM,
                BUYER_STATE_LINK_PAUSED,
            }
            if (
                _is_order_refundedish(base)
                or _has_panel_order(base)
                or bool(base.get("panel_add_submitted"))
                or panel_status_val == "manual_check"
                or stop_reason in ("panel_add_in_flight", "transport_error_after_add", "persist_failed_after_add")
                or base_state in protected_states
            ):
                return False

        created_at = int(safe_float(base.get("created_at"), time.time()))
        resolved_lot_key = lot_key if lot_key not in (None, "") else base.get("lot_key")
        resolved_funpay_status = (
            "refunded"
            if _is_order_refundedish(base)
            else (_normalize_funpay_status(base.get("funpay_status")) or _normalize_funpay_status(funpay_status) or "open")
        )
        effective_saved_link = saved_link if saved_link not in (None, "") else base.get("saved_link")
        record_update: Dict[str, Any] = {
            "chat_id": chat_id,
            "buyer_id": buyer_id,
            "buyer_username": buyer_username or "",
            "order_id": order_id_str,
            "lot_title": lot_title or base.get("lot_title") or base.get("service_name") or "",
            "service_name": lot_title or base.get("service_name") or "—",
            "service_id": service_id,
            "quantity": int(real_amount or 0),
            "total_quantity": int(real_amount or 0),
            "done_quantity": 0,
            "left_quantity": int(real_amount or 0),
            "progress_percent": 0,
            "order_amount": order_amount,
            "order_price": order_price,
            "service_number": service_number,
            "funpay_status": resolved_funpay_status,
            "buyer_flow_state": state,
            "saved_link": effective_saved_link,
            "spent": safe_float(base.get("spent", 0.0), 0.0),
            "summa": safe_float(order_price if order_price not in (None, "") else base.get("summa", 0.0), 0.0),
            "chistota": safe_float(base.get("chistota", 0.0), 0.0),
            "currency": str(base.get("currency", "RUB") or "RUB"),
            "created_at": created_at,
            "date": base.get("date") or base.get("date_str") or now_str_val,
            "last_prompt_ts": now_str_val,
            "updated_at": now_str_val,
            "completed_notification_sent": bool(base.get("completed_notification_sent", False)),
            "id_zakaz": base.get("id_zakaz"),
            "panel_order_id": base.get("panel_order_id"),
            "status": base.get("status") or "pending",
            "customer_url": effective_saved_link or base.get("customer_url") or "",
        }
        merged = dict(base)
        merged.update(record_update)
        if resolved_lot_key not in (None, ""):
            merged["lot_key"] = resolved_lot_key
        if effective_saved_link in (None, ""):
            merged.pop("saved_link", None)
        db_orders.upsert_order_dict(merged)
        return True


def _save_waiting_context(data: Dict, state: Optional[str] = None, saved_link_marker: Any = "__keep__") -> None:
    if not isinstance(data, dict):
        return
    order_id = str(data.get("order_id_funpay") or data.get("order_id") or "")
    if not order_id:
        return
    if state:
        data["state"] = state
        data["step"] = _step_from_state(state)
    else:
        state = data.get("state") or _state_from_step(data.get("step"))
    fields: Dict[str, Any] = {"buyer_flow_state": state, "updated_at": rt.now_str()}
    if saved_link_marker != "__keep__":
        fields["saved_link"] = saved_link_marker
        if saved_link_marker in (None, ""):
            data.pop("link", None)
        else:
            data["link"] = saved_link_marker
    elif data.get("link"):
        fields["saved_link"] = data.get("link")
    update_order_fields(order_id, **fields)


def _build_waiting_context_from_order(order_record: Dict) -> Optional[Dict[str, Any]]:
    if not isinstance(order_record, dict):
        return None
    order_id = str(order_record.get("order_id") or "")
    if not order_id:
        return None
    state = _get_buyer_flow_state(order_record)
    now_ts = time.time()
    raw_created = order_record.get("created_at")
    try:
        created_ts = float(raw_created) if raw_created not in (None, "") else now_ts
    except (TypeError, ValueError):
        created_ts = now_ts
    if created_ts <= 0:
        created_ts = now_ts
    return {
        "buyer_id": order_record.get("buyer_id"),
        "chat_id": order_record.get("chat_id"),
        "buyer_username": order_record.get("buyer_username") or "",
        "service_id": order_record.get("service_id"),
        "real_amount": int(order_record.get("quantity") or order_record.get("total_quantity") or 0),
        "order_id_funpay": order_id,
        "price": order_record.get("summa") or order_record.get("order_price") or 0,
        "service_number": order_record.get("service_number", 1),
        "lot_key": order_record.get("lot_key"),
        "lot_title": order_record.get("lot_title") or order_record.get("service_name") or "",
        "state": state,
        "step": _step_from_state(state),
        "link": order_record.get("saved_link") or order_record.get("customer_url") or "",
        "created_ts": created_ts,
        "updated_ts": now_ts,
    }


def _transition_waiting_state(
    ctx: Dict,
    order_id: str,
    buyer_id: Any,
    chat_id: Any,
    state: str,
    saved_link_marker: Any = "__keep__",
    select_order: bool = True,
) -> None:
    """Update waiting context, persist buyer flow state, and update active selection."""
    local_ctx = dict(ctx)
    now_ts = time.time()
    local_ctx.setdefault("created_ts", now_ts)
    local_ctx["updated_ts"] = now_ts
    _save_waiting_context(local_ctx, state=state, saved_link_marker=saved_link_marker)
    with rt.CONTEXT_LOCK:
        rt.waiting_for_link[order_id] = dict(local_ctx)
    ctx.clear()
    ctx.update(local_ctx)
    if select_order:
        _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
    else:
        _clear_selected_order_for_buyer(buyer_id, chat_id)


def _set_selected_order_for_buyer(buyer_id: Any, chat_id: Any, order_id: Any) -> None:
    if order_id not in (None, ""):
        with rt.CONTEXT_LOCK:
            rt.BUYER_ORDER_SELECTION[_buyer_selection_key(buyer_id, chat_id)] = str(order_id)


def _clear_selected_order_for_buyer(buyer_id: Any, chat_id: Any) -> None:
    with rt.CONTEXT_LOCK:
        rt.BUYER_ORDER_SELECTION.pop(_buyer_selection_key(buyer_id, chat_id), None)


def _get_buyer_orders(buyer_id: Any, chat_id: Any) -> List[Dict]:
    result = [
        order_record for order_record in load_orders_data()
        if isinstance(order_record, dict) and _buyer_owns_order(order_record, buyer_id, chat_id)
    ]
    result.sort(key=lambda item: safe_float(item.get("created_at"), 0.0), reverse=True)
    return result


def _resolve_buyer_order_for_action(
    c: "Cardinal",
    buyer_id: Any,
    chat_id: Any,
    raw_ref: Optional[str],
    command_name: str,
) -> Optional[Dict]:
    records = _get_buyer_orders(buyer_id, chat_id)
    if raw_ref:
        for order_record in records:
            if _matches_order_ref(order_record.get("order_id"), raw_ref):
                return order_record
        _send_buyer_message(c, chat_id, _format_order_not_found_for_buyer(raw_ref, records, command_name=command_name))
        return None

    active = [
        r for r in records
        if _get_buyer_flow_state(r) in BUYER_ACTIVE_PRE_PANEL_STATES
        and not _has_panel_order(r)
        and not _is_order_refundedish(r)
    ]
    if command_name in ("!ссылка", "!отмена"):
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            _send_buyer_message(c, chat_id, _format_short_order_list(active, command_name=command_name))
            return None
        _send_buyer_message(c, chat_id, ORDER_NOT_FOUND_TEXT)
        return None

    if len(records) == 1:
        return records[0]
    if records:
        _send_buyer_message(c, chat_id, _format_short_order_list(records, command_name=command_name))
        return None
    _send_buyer_message(c, chat_id, "У вас пока нет SMM-заказов.")
    return None


def _buyer_owns_order(order_record: Dict, buyer_id: Any, chat_id: Any) -> bool:
    if not isinstance(order_record, dict):
        return False
    buyer_match = buyer_id not in (None, "") and str(order_record.get("buyer_id")) == str(buyer_id)
    chat_match = chat_id not in (None, "") and str(order_record.get("chat_id")) == str(chat_id)
    return bool(buyer_match or chat_match)


_buyer_can_access_order = _buyer_owns_order


def _handle_buyer_smm_command(c: "Cardinal", msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    if not re.match(r"^!(?:смм|smm)\s*$", msg_text.strip(), re.IGNORECASE):
        return False
    _send_buyer_message(c, chat_id, _format_buyer_smm_orders(_get_buyer_orders(buyer_id, chat_id)[:5]))
    return True


def _handle_buyer_commands_command(c: "Cardinal", msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    if not re.match(r"^!(?:команды|help|помощь)\s*$", msg_text.strip(), re.IGNORECASE):
        return False
    cfg = load_config()
    template_text = (cfg.get("messages") or {}).get("buyer_commands") or DEFAULT_BUYER_COMMANDS_TEMPLATE
    _send_buyer_message(c, chat_id, _render_template(template_text, {}, {}))
    return True


def _handle_buyer_link_command(c: "Cardinal", msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    match = re.match(r"^!ссылка(?:\s+(.+))?$", msg_text.strip(), re.IGNORECASE)
    if not match:
        return False
    raw_ref = (match.group(1) or "").strip() or None
    order_record = _resolve_buyer_order_for_action(c, buyer_id, chat_id, raw_ref, command_name="!ссылка")
    if not order_record:
        return True
    order_id = str(order_record.get("order_id"))

    with rt.get_order_lock(order_id):
        fresh_record = get_order_data(order_id) or order_record
        if _apply_refund_gate(c, fresh_record, notify_chat_id=chat_id, force_check=True):
            return True
        state = _get_buyer_flow_state(fresh_record)
        if (
            _has_panel_order(fresh_record)
            or bool(fresh_record.get("panel_add_submitted"))
            or state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED, BUYER_STATE_REFUNDED)
        ):
            _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
            return True
        with rt.CONTEXT_LOCK:
            cached = rt.waiting_for_link.get(order_id)
            ctx = dict(cached) if isinstance(cached, dict) else None
        if ctx is None:
            ctx = _build_waiting_context_from_order(fresh_record) or {}
        if state == BUYER_STATE_LINK_PAUSED:
            _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_LINK, saved_link_marker=None)
            _send_buyer_message(c, chat_id, f"▶️ Ожидание ссылки возобновлено.\n\n🧾 Заказ: #{order_id}\n\nОтправьте ссылку для запуска заказа.\nСсылка должна начинаться с:\nhttps://")
            return True
        if state == BUYER_STATE_WAITING_CONFIRM:
            _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_LINK, saved_link_marker=None)
            _send_buyer_message(c, chat_id, f"Текущая ссылка сброшена.\n\n🧾 Заказ: #{order_id}\n\nОтправьте новую ссылку.\nСсылка должна начинаться с:\nhttps://")
            return True
        if state == BUYER_STATE_WAITING_LINK:
            if raw_ref:
                _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_LINK, select_order=True)
                _send_buyer_message(c, chat_id, "Отправьте ссылку для запуска заказа.")
                return True
            _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_LINK_PAUSED, saved_link_marker=None, select_order=False)
            _send_buyer_message(c, chat_id, f"⏸ Ожидание ссылки приостановлено.\n\n🧾 Заказ: #{order_id}\n\nКогда будете готовы отправить ссылку:\n!ссылка {order_id}")
            return True
        _send_buyer_message(c, chat_id, "Отправьте ссылку для запуска заказа.")
        return True


def _is_post_panel_or_refunded(order_record: Dict) -> bool:
    state = _get_buyer_flow_state(order_record)
    return _has_panel_order(order_record) or state in (
        BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED, BUYER_STATE_REFUNDED
    )


def _can_buyer_refund_pre_panel(c: "Cardinal", order_record: Dict, buyer_id: Any, chat_id: Any) -> Tuple[bool, Dict]:
    if not isinstance(order_record, dict):
        return False, {}

    order_id = str(order_record.get("order_id") or order_record.get("order_id_funpay") or "")
    if not order_id:
        return False, order_record

    latest = get_order_data(order_id) or order_record
    if not _buyer_owns_order(latest, buyer_id, chat_id) or _is_post_panel_or_refunded(latest):
        return False, latest

    # Force-check FunPay before any refund attempt. If the check is unavailable, do not refund.
    result = _fetch_funpay_status_via_cardinal(c, order_id) if c else {"ok": False, "error": "no_cardinal"}
    if not result.get("ok"):
        rt.logger.warning(f"BUYER CANCEL SAFETY BLOCKED: order=#{order_id}, reason=funpay_force_check_failed, detail={result.get('detail') or result.get('error')}")
        return False, latest

    funpay_status = _normalize_funpay_status(result.get("funpay_status"))
    if funpay_status:
        fields: Dict[str, Any] = {"funpay_status": funpay_status, "funpay_status_checked_at": rt.now_str()}
        if funpay_status in ("refunded", "partially_refunded"):
            fields.update({"buyer_flow_state": BUYER_STATE_REFUNDED, "saved_link": None, "updated_at": rt.now_str()})
            with rt.CONTEXT_LOCK:
                rt.waiting_for_link.pop(order_id, None)
        update_order_fields(order_id, **fields)
        latest = get_order_data(order_id) or latest

    if funpay_status != "open":
        rt.logger.warning(f"BUYER CANCEL SAFETY BLOCKED: order=#{order_id}, reason=funpay_status_{funpay_status}")
        return False, latest

    return (not _is_post_panel_or_refunded(latest)), latest


def _handle_buyer_cancel_command(c: "Cardinal", msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    match = re.match(r"^!отмена(?:\s+(.+))?$", msg_text.strip(), re.IGNORECASE)
    if not match:
        return False
    raw_ref = (match.group(1) or "").strip() or None
    order_record = _resolve_buyer_order_for_action(c, buyer_id, chat_id, raw_ref, command_name="!отмена")
    if not order_record:
        return True
    order_id = str(order_record.get("order_id"))
    with rt.get_order_lock(order_id):
        fresh_record = get_order_data(order_id) or order_record
        allow_buyer_cancel = bool(load_config().get("allow_buyer_cancel_before_panel_order", False))
        if not allow_buyer_cancel:
            if _apply_refund_gate(c, fresh_record, notify_chat_id=chat_id, force_check=True):
                return True
        elif _is_order_refundedish(fresh_record):
            if _apply_refund_gate(c, fresh_record, notify_chat_id=chat_id, force_check=False):
                return True

        state = _get_buyer_flow_state(fresh_record)
        if _has_panel_order(fresh_record) or state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED):
            _send_buyer_message(c, chat_id, "Заказ уже передан в работу.\nАвтоматическая отмена недоступна.")
            return True
        with rt.CONTEXT_LOCK:
            cached = rt.waiting_for_link.get(order_id)
            ctx = dict(cached) if isinstance(cached, dict) else None
        if not ctx:
            ctx = _build_waiting_context_from_order(fresh_record) or {}
        ctx.update({"order_id_funpay": order_id, "buyer_id": buyer_id, "chat_id": chat_id})

        if allow_buyer_cancel:
            can_refund, fresh_order = _can_buyer_refund_pre_panel(c, fresh_record, buyer_id, chat_id)
            if not can_refund:
                if _is_order_refundedish(fresh_order):
                    _apply_refund_gate(c, fresh_order, notify_chat_id=chat_id, force_check=False)
                elif _is_post_panel_or_refunded(fresh_order):
                    _send_buyer_message(c, chat_id, "Заказ уже передан в работу.\nАвтоматическая отмена недоступна.")
                else:
                    _send_buyer_message(c, chat_id, BUYER_AUTO_CANCEL_UNAVAILABLE_TEXT)
                return True
            try:
                c.account.refund(order_id)
            except Exception as ex:
                rt.logger.error(f"BUYER CANCEL REFUND FAILED: order=#{order_id}, error={ex}")
                _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_LINK_PAUSED, saved_link_marker=None, select_order=False)
                _send_admin_order_alert(
                    order_id_funpay=order_id,
                    reason=f"покупатель запросил !отмена, но автоматический refund не сработал: {ex}",
                    bot_action="refund не выполнен, ожидание ссылки приостановлено",
                    action="проверьте заказ и при необходимости отмените вручную",
                    title="⚠️ AutoSMM: не удалось выполнить buyer cancel",
                )
                _send_buyer_message(c, chat_id, BUYER_AUTO_CANCEL_FAILED_TEXT)
                return True

            try:
                update_order_fields(
                    order_id,
                    funpay_status="refunded",
                    buyer_flow_state=BUYER_STATE_REFUNDED,
                    saved_link=None,
                    refund_detected_at=rt.now_str(),
                    refund_source="buyer_cancel_command",
                    updated_at=rt.now_str(),
                )
            except (sqlite3.Error, OSError, ValueError, TypeError) as db_ex:
                rt.logger.exception(f"BUYER CANCEL DB UPDATE FAILED AFTER REFUND: order=#{order_id}, error={db_ex}")
                _send_admin_order_alert(
                    order_id_funpay=order_id,
                    reason=f"FunPay refund выполнен по команде !отмена, но не удалось обновить SQLite: {db_ex}",
                    bot_action="ожидание в памяти очищено, запись в БД требует проверки",
                    action="перезапустите синхронизацию или проверьте БД",
                    title="⚠️ AutoSMM: ошибка записи БД после возврата",
                )

            with rt.CONTEXT_LOCK:
                rt.waiting_for_link.pop(order_id, None)
            _clear_selected_order_for_buyer(buyer_id, chat_id)
            _send_buyer_message(c, chat_id, "Ввод ссылки отменён.\n\nЗаказ FunPay отменён.")
            return True

        _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_LINK_PAUSED, saved_link_marker=None, select_order=False)
        _send_buyer_message(c, chat_id, "Ввод ссылки отменён.\n\nЗаказ FunPay не отменён.\nЕсли хотите отменить заказ, напишите продавцу.\nЧтобы снова отправить ссылку, напишите: !ссылка")
        return True


def _get_waiting_candidates_for_message(buyer_id: Any, chat_id: int) -> List[Dict]:
    return [r for r in _get_buyer_orders(buyer_id, chat_id) if _get_buyer_flow_state(r) in (BUYER_STATE_WAITING_LINK, BUYER_STATE_WAITING_CONFIRM) and not _has_panel_order(r)]


def _choose_waiting_candidate(buyer_id: Any, chat_id: int, candidates: List[Dict]) -> Optional[Dict]:
    with rt.CONTEXT_LOCK:
        selected = rt.BUYER_ORDER_SELECTION.get(_buyer_selection_key(buyer_id, chat_id))
    if selected:
        for order_record in candidates:
            if str(order_record.get("order_id")) == str(selected):
                return order_record
    return candidates[0] if len(candidates) == 1 else None


def _send_link_confirmation_prompt(c: "Cardinal", chat_id: int, order_id: str, link_value: str) -> None:
    update_order_fields(order_id, last_prompt_ts=rt.now_str(), updated_at=rt.now_str())
    order_record = get_order_data(order_id) or {}
    cfg = load_config()
    template_text = (cfg.get("messages") or {}).get("buyer_link_received_confirm") or DEFAULT_LINK_CONFIRM_TEMPLATE
    message_text = _render_template(
        template_text,
        order_record,
        {
            "order_id_funpay": order_id,
            "link": link_value,
            "lot_title": order_record.get("lot_title") or order_record.get("service_name") or "—",
        },
    )
    _send_buyer_message(c, chat_id, message_text)


def _maybe_send_confirm_reminder(c: "Cardinal", chat_id: int, order_record: Dict) -> bool:
    order_id = str(order_record.get("order_id") or "")
    age = _seconds_since_checked_at(order_record.get("last_prompt_ts"))
    if age is not None and age < BUYER_CONFIRM_REMINDER_SECONDS:
        return False
    if order_id:
        update_order_fields(order_id, last_prompt_ts=rt.now_str(), updated_at=rt.now_str())
    _send_buyer_message(c, chat_id, CONFIRM_REMINDER_TEXT)
    return True


def _normalize_buyer_confirm_action(text_value: Any) -> str:
    return re.sub(r"\s+", " ", str(text_value or "").strip().lower()).strip(".!?,;: ")


def _handle_waiting_link_message(c: "Cardinal", order_record: Dict, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    order_id = str(order_record.get("order_id"))
    link_ = _extract_buyer_target(msg_text)
    rt.logger.debug(f"[FP LINK TARGET] order=#{order_id}, raw='{msg_text}' -> extracted='{link_}'")
    if not link_:
        if _looks_like_link_without_https(msg_text):
            _send_buyer_message(c, chat_id, INVALID_LINK_SCHEME_TEXT)
            return True
        return False
    with rt.get_order_lock(order_id):
        fresh_record = get_order_data(order_id) or order_record
        if _apply_refund_gate(c, fresh_record, notify_chat_id=chat_id, force_check=True):
            return True
        if _has_panel_order(fresh_record) or bool(fresh_record.get("panel_add_submitted")):
            _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
            return True
        state = _get_buyer_flow_state(fresh_record)
        if state not in (BUYER_STATE_WAITING_LINK, BUYER_STATE_WAITING_CONFIRM):
            if state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED):
                _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
            return True
        ok, reason = is_valid_buyer_target(link_)
        if not ok:
            _send_buyer_message(c, chat_id, reason)
            return True
        with rt.CONTEXT_LOCK:
            cached = rt.waiting_for_link.get(order_id)
            ctx = dict(cached) if isinstance(cached, dict) else None
        if not ctx:
            ctx = _build_waiting_context_from_order(fresh_record) or {}
        ctx.update({"buyer_id": buyer_id, "chat_id": chat_id, "order_id_funpay": order_id, "link": link_})
        if load_config().get("confirm_link", True):
            _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_CONFIRM, saved_link_marker=link_)
            _send_link_confirmation_prompt(c, chat_id, order_id, link_)
            return True
        now_ts = time.time()
        ctx.setdefault("created_ts", now_ts)
        ctx["updated_ts"] = now_ts
        local_ctx = dict(ctx)
        _save_waiting_context(local_ctx, state=BUYER_STATE_WAITING_LINK, saved_link_marker=link_)
        with rt.CONTEXT_LOCK:
            rt.waiting_for_link[order_id] = dict(local_ctx)
        process_link_without_confirmation(c, dict(local_ctx))
        _clear_selected_order_for_buyer(buyer_id, chat_id)
        return True


def _handle_waiting_confirm_message(c: "Cardinal", order_record: Dict, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    order_id = str(order_record.get("order_id"))
    lowered = _normalize_buyer_confirm_action(msg_text)
    link_ = _extract_buyer_target(msg_text)

    if not link_ and lowered not in BUYER_CONFIRM_YES_WORDS and lowered not in BUYER_CONFIRM_NO_WORDS:
        if _looks_like_link_without_https(msg_text):
            _send_buyer_message(c, chat_id, INVALID_LINK_SCHEME_TEXT)
            return True
        return _maybe_send_confirm_reminder(c, chat_id, order_record)

    with rt.get_order_lock(order_id):
        fresh_record = get_order_data(order_id) or order_record
        if _apply_refund_gate(c, fresh_record, notify_chat_id=chat_id, force_check=True):
            return True
        if _has_panel_order(fresh_record) or bool(fresh_record.get("panel_add_submitted")):
            _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
            return True
        state = _get_buyer_flow_state(fresh_record)
        if state not in (BUYER_STATE_WAITING_LINK, BUYER_STATE_WAITING_CONFIRM):
            if state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED):
                _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
            return True
        with rt.CONTEXT_LOCK:
            cached = rt.waiting_for_link.get(order_id)
            ctx = dict(cached) if isinstance(cached, dict) else None
        if not ctx:
            ctx = _build_waiting_context_from_order(fresh_record) or {}
        ctx.update({"buyer_id": buyer_id, "chat_id": chat_id, "order_id_funpay": order_id, "link": fresh_record.get("saved_link") or ctx.get("link") or fresh_record.get("customer_url") or ""})

        if lowered in BUYER_CONFIRM_YES_WORDS:
            if not ctx.get("link"):
                _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_LINK, saved_link_marker=None)
                _send_buyer_message(c, chat_id, f"Ссылка/юзернейм не найдены.\n\n🧾 Заказ: #{order_id}\n\nОтправьте ссылку (https://...) или юзернейм (@channel) для запуска заказа.")
                return True
            process_link_without_confirmation(c, ctx)
            _clear_selected_order_for_buyer(buyer_id, chat_id)
            return True

        if lowered in BUYER_CONFIRM_NO_WORDS:
            _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_LINK, saved_link_marker=None)
            _send_buyer_message(c, chat_id, f"Текущие данные сброшены.\n\n🧾 Заказ: #{order_id}\n\nОтправьте новую ссылку (https://...) или юзернейм (@channel).")
            return True

        if link_:
            ok, reason = is_valid_buyer_target(link_)
            if not ok:
                _send_buyer_message(c, chat_id, reason)
                return True
            ctx["link"] = link_
            _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_CONFIRM, saved_link_marker=link_)
            _send_link_confirmation_prompt(c, chat_id, order_id, link_)
            return True

        return _maybe_send_confirm_reminder(c, chat_id, fresh_record)


def _handle_buyer_flow_message(c: "Cardinal", msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    candidates = _get_waiting_candidates_for_message(buyer_id, chat_id)
    if not candidates:
        extracted_target = _extract_http_link(msg_text)
        if extracted_target and ("://" in extracted_target or extracted_target.startswith("@")):
            recent_orders = _get_buyer_orders(buyer_id, chat_id)
            if recent_orders:
                latest_order = recent_orders[0]
                if _get_buyer_flow_state(latest_order) == BUYER_STATE_COMPLETED:
                    return False
                if _apply_refund_gate(c, latest_order, notify_chat_id=chat_id, force_check=True):
                    return True
                if _has_panel_order(latest_order) or _get_buyer_flow_state(latest_order) in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_FAILED):
                    _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
                    return True
                if _get_buyer_flow_state(latest_order) == BUYER_STATE_LINK_PAUSED:
                    order_id = str(latest_order.get("order_id"))
                    with rt.CONTEXT_LOCK:
                        cached = rt.waiting_for_link.get(order_id)
                        ctx = dict(cached) if isinstance(cached, dict) else None
                    if ctx is None:
                        ctx = _build_waiting_context_from_order(latest_order) or {}
                    _transition_waiting_state(ctx, order_id, buyer_id, chat_id, BUYER_STATE_WAITING_LINK, select_order=True)
                    latest_order["buyer_flow_state"] = BUYER_STATE_WAITING_LINK
                    candidates = [latest_order]
        if not candidates:
            return False

    order_record = _choose_waiting_candidate(buyer_id, chat_id, candidates)
    if not order_record:
        lowered = _normalize_buyer_confirm_action(msg_text)
        if not _extract_http_link(msg_text) and lowered not in BUYER_CONFIRM_YES_WORDS and lowered not in BUYER_CONFIRM_NO_WORDS:
            return False
        _send_buyer_message(c, chat_id, _format_short_order_list(candidates, command_name="!ссылка"))
        return True

    state = _get_buyer_flow_state(order_record)
    if state == BUYER_STATE_WAITING_LINK:
        return _handle_waiting_link_message(c, order_record, msg_text, buyer_id, chat_id)
    if state == BUYER_STATE_WAITING_CONFIRM:
        return _handle_waiting_confirm_message(c, order_record, msg_text, buyer_id, chat_id)
    return False


def _verify_recovered_orders_refund_async(c: "Cardinal", order_ids: List[str]) -> None:
    if not order_ids or not rt.RUNNING or rt.STOP_EVENT.is_set():
        return
    try:
        for start in range(0, len(order_ids), FUNPAY_STATUS_BATCH_SIZE):
            if not rt.RUNNING or rt.STOP_EVENT.is_set():
                return
            chunk = order_ids[start : start + FUNPAY_STATUS_BATCH_SIZE]
            batch_results = _fetch_funpay_statuses_batch(c, chunk)
            checked_at = rt.now_str()
            for order_id in chunk:
                if not rt.RUNNING or rt.STOP_EVENT.is_set():
                    return
                res = batch_results.get(order_id) or {}
                if not res.get("ok"):
                    continue
                with rt.get_order_lock(order_id):
                    latest = get_order_data(order_id)
                    if not latest or _has_panel_order(latest):
                        continue
                    if _get_buyer_flow_state(latest) not in BUYER_ACTIVE_PRE_PANEL_STATES:
                        continue
                    _apply_funpay_status_update(
                        order_id,
                        latest,
                        str(res.get("funpay_status") or ""),
                        checked_at=checked_at,
                        source="startup_recovery_async",
                        refund_source="startup_recovery_async",
                        swallow_save_error=True,
                    )
    except Exception as exc:
        rt.logger.warning(f"BUYER FLOW ASYNC REFUND CHECK FAILED: {exc}")


def _recover_buyer_flow_runtime(c: Optional["Cardinal"] = None) -> None:
    """Restore in-memory waiting contexts on startup without synchronous FunPay HTTP blocking."""
    with rt.CONTEXT_LOCK:
        rt.waiting_for_link.clear()
    recovered = 0
    pending_refund_check_ids: List[str] = []
    try:
        all_orders = load_orders_data()
    except (sqlite3.Error, OSError, ValueError, TypeError) as ex:
        rt.logger.error(f"BUYER FLOW RECOVERY FAILED: cannot load orders_data: {ex}")
        return
    for order_record in all_orders:
        if not isinstance(order_record, dict):
            continue
        order_id = str(order_record.get("order_id") or "")
        if not order_id:
            continue
        state = _get_buyer_flow_state(order_record)
        if (
            order_record.get("status_stop_reason") == "panel_add_in_flight"
            and not _has_panel_order(order_record)
            and not _is_order_refundedish(order_record)
        ):
            rt.logger.warning(
                f"BUYER FLOW RECOVERY: order #{order_id} was interrupted mid-submit (panel_add_in_flight); "
                "manual check required"
            )
            continue
        if state not in BUYER_ACTIVE_PRE_PANEL_STATES:
            continue
        # Evaluate only locally persisted refund state during startup; schedule remote FunPay verification async.
        if _apply_refund_gate(None, order_record, notify_chat_id=None, force_check=False):
            continue
        ctx = _build_waiting_context_from_order(order_record)
        if ctx:
            with rt.CONTEXT_LOCK:
                rt.waiting_for_link[order_id] = ctx
            recovered += 1
            pending_refund_check_ids.append(order_id)
    if recovered:
        rt.logger.info(f"BUYER FLOW RECOVERY: restored {recovered} active waiting orders")
    if c is not None and pending_refund_check_ids and rt.RUNNING and not rt.STOP_EVENT.is_set():
        worker = threading.Thread(
            target=_verify_recovered_orders_refund_async,
            args=(c, pending_refund_check_ids),
            name="autosmm-recovery-refund-check",
            daemon=True,
        )
        rt.register_background_thread(worker)
        worker.start()
