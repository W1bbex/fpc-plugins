"""FunPay refund execution, local refund state recording, and optional SMM panel cancellation."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Dict, Optional

import requests

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..models import (
    get_effective_funpay_status as _get_effective_funpay_status,
    is_order_refundedish as _is_order_refundedish,
)
from ..services.notifications import _send_admin_order_alert
from ..storage import db_orders
from ..storage.storage import (
    ORDERS_LOCK,
    claim_panel_refund_cancel,
    get_order_data,
    load_config,
    update_order_fields,
)
from .buyer_messages import _send_buyer_message
from .engine import (
    BUYER_STATE_FAILED,
    BUYER_STATE_REFUNDED,
)
from .panel_api import (
    _normalize_api_key,
    _panel_api_json,
    _sanitize_secret_text,
)


def _attempt_panel_cancel_after_funpay_refund(order_record: Optional[Dict], source: str = "funpay_refund") -> bool:
    """Attempt best-effort panel order cancellation once when FunPay records a full refund."""
    if not isinstance(order_record, dict):
        return False

    order_id_funpay = str(order_record.get("order_id") or "")
    if not order_id_funpay:
        return False
    if _get_effective_funpay_status(order_record) != "refunded":
        return False

    cfg = load_config()
    if not bool(cfg.get("try_panel_cancel_after_funpay_refund", False)):
        rt.logger.info(f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=disabled_by_config")
        return False

    if order_record.get("panel_refund_cancel_attempted_at"):
        return False

    panel_order_id = order_record.get("id_zakaz")
    service_number = order_record.get("service_number")
    if not panel_order_id or service_number in (None, ""):
        rt.logger.info(
            f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=missing_order_or_service"
        )
        return False

    service_cfg = (cfg.get("services") or {}).get(str(service_number))
    if not isinstance(service_cfg, dict):
        rt.logger.info(
            f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=service_not_found, service=#{service_number}"
        )
        return False

    api_url = str(service_cfg.get("api_url") or "").strip()
    api_key = _normalize_api_key(service_cfg.get("api_key"))
    if not api_url or not api_key:
        rt.logger.info(
            f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=service_credentials_missing, service=#{service_number}"
        )
        return False

    if not claim_panel_refund_cancel(order_id_funpay, attempted_at=rt.now_str()):
        return False

    try:
        response, payload, raw_text, _invalid_json = _panel_api_json(
            api_url,
            {
                "key": api_key,
                "action": "cancel",
                "orders": str(panel_order_id),
            },
            timeout=10,
        )
        response_text = (raw_text or "").strip()
        if len(response_text) > 300:
            response_text = response_text[:300] + "…"
        status_code = getattr(response, "status_code", None)
        status_label = f"HTTP {status_code}" if status_code is not None else "HTTP unknown"
        if payload is None:
            payload = response_text or status_label
        safe_result = _sanitize_secret_text(payload, api_key)
        update_order_fields(
            order_id_funpay,
            panel_refund_cancel_result=f"{status_label}: {safe_result}",
        )
        rt.logger.info(
            f"PANEL CANCEL ATTEMPT: order=#{order_id_funpay}, source={source}, service=#{service_number}, "
            f"panel_order={panel_order_id}, http_status={status_code}, result={safe_result}"
        )
        return True
    except (requests.RequestException, sqlite3.Error, OSError, ValueError, TypeError) as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        try:
            update_order_fields(order_id_funpay, panel_refund_cancel_result=f"error: {safe_ex}")
        except (sqlite3.Error, OSError, ValueError, TypeError) as db_ex:
            rt.logger.warning(
                f"Не удалось сохранить panel_refund_cancel_result для заказа #{order_id_funpay}: {db_ex}"
            )
        rt.logger.warning(
            f"PANEL CANCEL FAILED: order=#{order_id_funpay}, source={source}, service=#{service_number}, "
            f"panel_order={panel_order_id}, error={safe_ex}"
        )
        return False


def mark_order_refunded(order_id_funpay: str, detected_at: str | None = None, source: str | None = None) -> bool:
    """Mark an order as refunded locally and trigger optional panel cancellation outside the lock."""
    with ORDERS_LOCK:
        updated, order_snapshot, found = db_orders.mark_order_refunded_atomic(
            str(order_id_funpay),
            detected_at=detected_at,
            source=source,
        )
        if not found:
            rt.logger.warning(f"Заказ #{order_id_funpay} не найден в базе данных.")
            return False
        if not updated:
            return False
        with rt.CONTEXT_LOCK:
            rt.waiting_for_link.pop(str(order_id_funpay), None)

    # Release ORDERS_LOCK before performing external SMM panel HTTP calls.
    _attempt_panel_cancel_after_funpay_refund(order_snapshot, source=source or "refund_mark")
    return True


def refund_order(
    c: "Cardinal",
    order_id_funpay: str,
    buyer_chat_id: int,
    reason: str,
    detailed_reason: str = None,
    bot_action: Optional[str] = None,
    admin_action: Optional[str] = None,
    success_title: str = "↩️ Выполнен возврат средств",
    failure_title: str = "⚠️ Заказ требует ручной проверки",
) -> None:
    """Process FunPay refund (when enabled) and emit short seller alerts with full log context."""
    cfg = load_config()
    auto_refunds = bool(cfg.get("auto_refunds", False))

    if detailed_reason is None:
        detailed_reason = reason

    order_data = get_order_data(order_id_funpay)
    if order_data and _is_order_refundedish(order_data):
        rt.logger.info(f"Заказ #{order_id_funpay} уже был возвращен. Пропуск.")
        return

    success_bot_action = (
        f"{bot_action}; затем возврат выполнен автоматически" if bot_action else "возврат выполнен автоматически"
    )
    success_admin_action = "дополнительных действий не требуется"
    failure_bot_action = bot_action or "автоматический возврат не выполнен"
    failure_admin_action = admin_action or "выполните возврат вручную"

    if auto_refunds:
        try:
            c.account.refund(order_id_funpay)
        except Exception as ex:
            rt.logger.error(
                f"Не удалось вернуть средства для заказа #{order_id_funpay}: {ex}. "
                f"Технические детали: {detailed_reason}"
            )
            update_order_fields(order_id_funpay, buyer_flow_state=BUYER_STATE_FAILED, updated_at=rt.now_str())
            try:
                _send_admin_order_alert(
                    order_id_funpay=order_id_funpay,
                    reason=reason,
                    bot_action=failure_bot_action,
                    action=failure_admin_action,
                    title=failure_title,
                )
            except Exception as alert_ex:
                rt.logger.warning(f"Не удалось отправить admin-уведомление об ошибке возврата #{order_id_funpay}: {alert_ex}")
            return

        rt.logger.info(
            f"Заказ #{order_id_funpay} был отменён (refund) для покупателя {buyer_chat_id}. "
            f"Причина: {reason}. Технические детали: {detailed_reason}"
        )
        updated_ok = update_order_fields(
            order_id_funpay,
            funpay_status="refunded",
            buyer_flow_state=BUYER_STATE_REFUNDED,
            saved_link=None,
            status_stop_reason=None,
            refund_detected_at=rt.now_str(),
            refund_source="auto_refund",
            updated_at=rt.now_str(),
        )
        if not updated_ok:
            rt.logger.error(
                f"REFUND STATE PERSIST FAILED: заказ #{order_id_funpay} возвращён на FunPay, "
                f"но обновление SQLite вернуло False."
            )
        with rt.CONTEXT_LOCK:
            rt.waiting_for_link.pop(str(order_id_funpay), None)

        try:
            _send_buyer_message(c, buyer_chat_id, f"❌ Ваши средства возвращены по причине: {reason}")
        except Exception as msg_ex:
            rt.logger.warning(f"Не удалось уведомить покупателя о возврате #{order_id_funpay}: {msg_ex}")

        try:
            _send_admin_order_alert(
                order_id_funpay=order_id_funpay,
                reason=reason,
                bot_action=success_bot_action,
                action=success_admin_action,
                title=success_title,
            )
        except Exception as alert_ex:
            rt.logger.warning(f"Не удалось отправить admin-уведомление о возврате #{order_id_funpay}: {alert_ex}")
    else:
        rt.logger.warning(f"Для заказа #{order_id_funpay} требуется ручной возврат. Технические детали: {detailed_reason}")
        update_order_fields(order_id_funpay, buyer_flow_state=BUYER_STATE_FAILED, updated_at=rt.now_str())
        _send_admin_order_alert(
            order_id_funpay=order_id_funpay,
            reason=reason,
            bot_action=bot_action or "автоматический возврат отключён",
            action=admin_action or "выполните возврат вручную",
            title=failure_title,
        )
