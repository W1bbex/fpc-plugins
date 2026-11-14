"""Admin alerts, seller start summaries, and buyer completion notifications."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

import requests

try:
    from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..core.buyer_messages import _render_template
from ..core.panel_api import (
    _build_transport_error_balance_note,
    _panel_api_json,
    _sanitize_secret_text,
    _validate_api_key_for_action,
)
from ..models import get_effective_funpay_status, safe_float
from ..storage import db_orders
from ..storage.defaults import DEFAULT_AFTER_COMPLETED_TEMPLATE
from ..storage.storage import get_order_data, load_config, update_order_fields
from .analytics import update_order_charge_and_net
from .currency import compute_order_profit_rub, convert_to_rub, normalize_currency_code

logger = logging.getLogger("FPC.autosmm")


def _send_admin_order_alert(
    order_id_funpay: str,
    reason: str,
    action: str,
    title: str = "⚠️ Заказ требует ручной проверки",
    bot_action: Optional[str] = None,
) -> bool:
    """Send a manual-intervention alert for an order to the configured admin chat."""
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id or rt.bot is None:
        return False

    if bot_action is None:
        bot_action = "автоматическая обработка завершена без дополнительных действий"

    alert_text = (
        f"{title}\n"
        f"Заказ: #{order_id_funpay}\n"
        f"Причина: {reason}\n"
        f"Что сделал бот: {bot_action}\n"
        f"Действие: {action}\n"
        f"Ссылка: https://funpay.com/orders/{order_id_funpay}/"
    )

    try:
        rt.bot.send_message(notification_chat_id, alert_text)
        return True
    except Exception as exc:
        rt.logger.error(f"Не удалось отправить admin alert по заказу #{order_id_funpay}: {exc}")
        return False


def _send_manual_check_alert_once(
    order_record: Dict,
    reason: str = "не удалось подтвердить состояние заказа",
) -> None:
    """Emit at most one manual-check alert per order record."""
    if not order_record or order_record.get("manual_check_alert_sent", False):
        return

    cfg = load_config()
    if not cfg.get("notification_chat_id") or rt.bot is None:
        return

    order_id_funpay = str(order_record.get("order_id") or "").strip()
    if not order_id_funpay:
        return

    claimed, snapshot = db_orders.claim_manual_check_alert(order_id_funpay)
    if not claimed:
        if snapshot is not None:
            order_record["manual_check_alert_sent"] = bool(snapshot.get("manual_check_alert_sent", True))
        return

    sent = _send_admin_order_alert(
        order_id_funpay=order_id_funpay,
        reason=reason,
        bot_action="автоматическая обработка остановлена",
        action="проверьте заказ вручную",
        title="⚠️ Заказ требует ручной проверки",
    )
    if sent:
        order_record["manual_check_alert_sent"] = True
    elif claimed:
        update_order_fields(order_id_funpay, manual_check_alert_sent=False)


def _format_alert_money(value: Any) -> str:
    """Format monetary values cleanly without trailing '.00' noise."""
    try:
        num = float(value)
    except (ValueError, TypeError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{num:.2f}".rstrip("0").rstrip(".")


def _fetch_start_notification_charge(
    order_id_funpay: str,
    panel_order_id: int,
    api_url: str,
    api_key: str,
    lot_price: float,
    service_number: Optional[int],
) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[float], Optional[float]]:
    """Query the panel status once after order start to persist charge and RUB profit."""
    if service_number is None:
        return None, None, None, None, None
    try:
        invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
        if invalid_key_payload:
            rt.logger.warning(
                f"{rt.LOGGER_PREFIX} Пропускаю action=status для уведомления о старте заказа "
                f"#{order_id_funpay}: {invalid_key_payload['detail']}"
            )
            return None, None, None, None, None

        _status_resp, status_data, _raw_text, invalid_json = _panel_api_json(
            api_url,
            {"key": api_key, "action": "status", "order": panel_order_id},
            timeout=10,
        )
        if invalid_json or not isinstance(status_data, dict):
            raise ValueError("панель не вернула JSON статуса")
        if status_data.get("error") or status_data.get("charge") in (None, ""):
            rt.logger.warning(
                f"START NOTIFICATION CHARGE UNAVAILABLE: order=#{order_id_funpay}, "
                f"panel_order_id={panel_order_id}, error={status_data.get('error')}"
            )
            return None, None, None, None, None

        charge = safe_float(status_data.get("charge"), 0.0)
        currency = normalize_currency_code(status_data.get("currency", "USD"))
        fx_info = compute_order_profit_rub(lot_price, charge, currency)
        try:
            update_order_charge_and_net(
                order_id_funpay,
                fx_info["spent"],
                currency=fx_info["currency"],
                net_profit=fx_info["net_profit"],
                spent_rub=fx_info["spent_rub"],
                fx_rate_to_rub=fx_info["fx_rate_to_rub"],
            )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            rt.logger.exception(f"Не удалось сохранить charge/chistota для заказа #{order_id_funpay}")
        return (
            fx_info["spent"],
            fx_info["currency"],
            fx_info["spent_rub"],
            fx_info["fx_rate_to_rub"],
            fx_info["net_profit"],
        )
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        rt.logger.error(
            f"Ошибка при получении данных action=status для уведомления о начале заказа #{order_id_funpay}: "
            f"{_sanitize_secret_text(exc, api_key)}"
        )
        return None, None, None, None, None


def _build_order_url_markup(order_url: str) -> Any:
    if InlineKeyboardMarkup is None or InlineKeyboardButton is None:
        return None
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Перейти к заказу", url=order_url))
    return kb


def _format_order_started_text(
    *,
    title: str,
    info_title: Optional[str],
    order_id_funpay: str,
    buyer_username: str,
    reason: Optional[str],
    action_text: Optional[str],
    lot_price: float,
    charge: Optional[float],
    currency: str,
    real_amount: int,
    order_url: str,
    spent_rub: Optional[float] = None,
    net_profit: Optional[float] = None,
) -> str:
    norm_cur = normalize_currency_code(currency)
    resolved_spent_rub = spent_rub if charge is not None else None
    if charge is not None and resolved_spent_rub is None:
        if norm_cur == "RUB":
            resolved_spent_rub = round(float(charge or 0.0), 4)
        else:
            resolved_spent_rub, _rate = convert_to_rub(charge, norm_cur, allow_network=False)

    computed_profit = (
        net_profit
        if net_profit is not None
        else (round(float(lot_price) - resolved_spent_rub, 2) if resolved_spent_rub is not None else None)
    )

    parts = [title, ""]
    if info_title:
        parts.extend([info_title, ""])
    parts.extend([f"Заказ: #{order_id_funpay}", f"Покупатель: {buyer_username}", ""])
    if reason:
        parts.append(f"Причина: {reason}")
    if action_text:
        parts.append(f"Действие: {action_text}")
    if reason or action_text:
        parts.append("")
    parts.append(f"💰 FunPay: {_format_alert_money(lot_price)} ₽")
    if charge is None:
        parts.append("💸 Сайт: стоимость уточняется")
    elif norm_cur == "RUB" or resolved_spent_rub is None:
        parts.append(f"💸 Сайт: {_format_alert_money(charge)} {norm_cur}")
    else:
        parts.append(f"💸 Сайт: {_format_alert_money(charge)} {norm_cur} (~{_format_alert_money(resolved_spent_rub)} ₽)")

    if computed_profit is not None:
        parts.append(f"📈 Профит: {_format_alert_money(computed_profit)} ₽")
    elif charge is None:
        parts.append("📈 Профит: уточняется")
    else:
        parts.append(f"📈 Профит: недоступен (валюта панели: {norm_cur})")
    parts.extend(["", f"📦 Итоговое количество: {real_amount}", f"🔗 {order_url}"])
    return "\n".join(parts)


def send_order_started_notification(
    c: "Cardinal",
    order_id_funpay: str,
    panel_order_id: int,
    api_url: str,
    api_key: str,
    lot_price: float,
    real_amount: int,
    service_number: Optional[int] = None,
    title: str = "🚀 AUTOSMM | Старт заказа",
    info_title: Optional[str] = None,
    reason: Optional[str] = None,
    action_text: Optional[str] = None,
) -> None:
    """Send an order-start notification with charge and profit breakdown to admin chat."""
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id:
        rt.logger.warning("Notification chat_id не задан в конфигурации.")
        return

    charge, currency, spent_rub, _fx_rate, net_profit = _fetch_start_notification_charge(
        order_id_funpay,
        panel_order_id,
        api_url,
        api_key,
        lot_price,
        service_number,
    )

    try:
        stored_order = get_order_data(order_id_funpay) or {}
        stored_spent = stored_order.get("spent")
        if charge is not None:
            resolved_charge: Optional[float] = charge
        elif stored_spent not in (None, "") and safe_float(stored_spent, 0.0) > 0:
            resolved_charge = safe_float(stored_spent, 0.0)
        else:
            resolved_charge = None
        resolved_currency = normalize_currency_code(currency or stored_order.get("currency", "RUB"))
        resolved_spent_rub = spent_rub if spent_rub is not None else (stored_order.get("spent_rub") if resolved_charge is not None else None)

        buyer_username = str(stored_order.get("buyer_username") or "").strip()
        if not buyer_username:
            try:
                order_data = c.account.get_order(order_id_funpay)
                buyer_username = getattr(order_data, "buyer_username", None) or "—"
            except Exception as exc:
                rt.logger.warning(f"Не удалось получить buyer_username для заказа #{order_id_funpay}: {exc}")
                buyer_username = "—"

        order_url = f"https://funpay.com/orders/{order_id_funpay}/"
        notification_text = _format_order_started_text(
            title=title,
            info_title=info_title,
            order_id_funpay=order_id_funpay,
            buyer_username=buyer_username,
            reason=reason,
            action_text=action_text,
            lot_price=lot_price,
            charge=resolved_charge,
            currency=resolved_currency,
            real_amount=real_amount,
            order_url=order_url,
            spent_rub=resolved_spent_rub,
            net_profit=net_profit,
        )
        c.telegram.bot.send_message(
            notification_chat_id,
            notification_text,
            reply_markup=_build_order_url_markup(order_url),
        )
    except Exception as exc:
        rt.logger.error(f"Ошибка при отправке уведомления о начале заказа: {exc}")


def _send_completion_notification_if_needed(c: "Cardinal", order_record: Dict) -> None:
    """Notify buyer in FunPay chat once a completed order awaits confirmation."""
    if not order_record:
        return
    if str(order_record.get("status", "") or "").lower() != "completed":
        return
    if bool(order_record.get("completed_notification_sent", False)):
        return
    order_id_funpay = str(order_record.get("order_id") or "").strip()
    if not order_id_funpay:
        return

    claimed, latest_snapshot = db_orders.claim_completion_notification(order_id_funpay)
    if not claimed:
        if latest_snapshot is not None:
            order_record["completed_notification_sent"] = bool(
                latest_snapshot.get("completed_notification_sent", False)
            )
            rt.logger.info(
                f"Completion-уведомление пропущено для заказа #{order_id_funpay}: "
                f"funpay_status={get_effective_funpay_status(latest_snapshot)}, "
                f"sent={order_record['completed_notification_sent']}"
            )
        return

    active_record = latest_snapshot or order_record
    buyer_chat_id = active_record.get("chat_id")
    if buyer_chat_id in (None, ""):
        update_order_fields(order_id_funpay, completed_notification_sent=False)
        return
    panel_order_id = active_record.get("id_zakaz")
    cfg = load_config()
    template_text = (cfg.get("messages") or {}).get("after_completed") or DEFAULT_AFTER_COMPLETED_TEMPLATE
    message = _render_template(
        template_text,
        active_record,
        {
            "order_id_funpay": order_id_funpay,
            "panel_order_id": panel_order_id,
            "twiboost_id": panel_order_id,
            "lot_title": active_record.get("lot_title") or active_record.get("service_name") or "—",
        },
    )

    try:
        c.send_message(buyer_chat_id, message)
        order_record["completed_notification_sent"] = True
        rt.logger.info(
            f"Уведомление о завершении отправлено покупателю {buyer_chat_id} (заказ #{panel_order_id})."
        )
    except Exception as exc:
        update_order_fields(order_id_funpay, completed_notification_sent=False)
        order_record["completed_notification_sent"] = False
        rt.logger.error(
            f"Не удалось отправить уведомление о завершении для заказа #{order_id_funpay}: {exc}"
        )


def _send_lot_match_manual_alert(order_id_funpay: Any, description: Any, resolve_result: Dict[str, Any]) -> None:
    """Alert admin chat when incoming order description matches zero or multiple lots."""
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id or rt.bot is None:
        return

    error_code = str(resolve_result.get("error") or "unknown")
    if error_code == "ambiguous_match":
        lines = [
            "⚠️ AutoSMM: найдено несколько подходящих лотов",
            "",
            f"Заказ FunPay: #{order_id_funpay}",
            "Описание заказа:",
            str(description or "—"),
            "",
            "AutoSMM не может безопасно выбрать услугу панели.",
            "",
            "Совпадения:",
        ]
        for idx, item in enumerate(resolve_result.get("matches") or [], start=1):
            lines.append(
                f"{idx}. {item.get('lot_key')} — {item.get('name') or '—'} | "
                f"ID услуги: {item.get('service_id') or '—'} | "
                f"Кол-во: {item.get('quantity') or '—'} | "
                f"Панель: {item.get('service_number') or '—'}"
            )
        lines.extend(["", "Действие:", "проверьте привязку лота вручную."])
    else:
        lines = [
            "⚠️ AutoSMM: не удалось определить лот",
            "",
            f"Заказ FunPay: #{order_id_funpay}",
            "Описание заказа:",
            str(description or "—"),
            "",
            "AutoSMM не нашёл подходящий лот в настройках.",
            "",
            "Действие:",
            "проверьте привязки лотов вручную.",
        ]

    try:
        rt.bot.send_message(notification_chat_id, "\n".join(lines))
    except Exception as exc:
        rt.logger.error(f"Не удалось отправить alert по lot matching для заказа #{order_id_funpay}: {exc}")


def _send_transport_error_after_add_alert(
    order_id_funpay: Any,
    service_number: Any,
    service_id: Any,
    real_amount: Any,
    link_: str,
    primary_error: Any,
    primary_detail: Any,
    balance_before: Optional[Dict[str, Any]] = None,
    balance_after: Optional[Dict[str, Any]] = None,
) -> None:
    """Alert admin chat after a transport error during panel order submission."""
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id or rt.bot is None:
        return

    balance_note, _risk_code = _build_transport_error_balance_note(balance_before, balance_after)
    text_ = (
        "⚠️ Заказ требует ручной проверки\n\n"
        f"Заказ FunPay: #{order_id_funpay}\n"
        f"Панель: #{service_number}\n"
        f"Услуга: {service_id}\n"
        f"Итоговое количество: {real_amount}\n"
        f"Ссылка: {link_}\n\n"
        "Причина:\n"
        "панель не вернула ID заказа после add-запроса.\n"
        f"Ошибка: {primary_error}\n"
        f"Детали: {str(primary_detail or '')[:300]}\n\n"
        f"{balance_note}\n\n"
        "Refund и резервная панель заблокированы.\n"
        "Проверьте панель вручную."
    )
    try:
        rt.bot.send_message(notification_chat_id, text_)
    except Exception as exc:
        rt.logger.error(f"Не удалось отправить transport-error alert по заказу #{order_id_funpay}: {exc}")
