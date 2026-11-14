"""Telegram UI screens and callbacks for AutoSMM order analytics and period switching."""

from __future__ import annotations

from typing import Optional, Tuple

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..services import analytics
from .common import _is_message_not_modified_error, _safe_answer_callback

_ANALYTICS_PERIOD_MAP = {
    "day": "24h", "week": "7d", "month": "30d", "all": "all",
    "24h": "24h", "7d": "7d", "30d": "30d",
}
_ANALYTICS_VALID_UI_PERIODS = ("day", "week", "month", "all")


def build_analytics_keyboard(period: str = "24h", mode: str = "main") -> InlineKeyboardMarkup:
    """Build InlineKeyboardMarkup for the analytics screen."""
    period_norm = analytics._period_info(period)[0]
    if period_norm == "7d":
        period_callback_suffix = "week"
    elif period_norm == "30d":
        period_callback_suffix = "month"
    elif period_norm == "all":
        period_callback_suffix = "all"
    else:
        period_callback_suffix = "day"

    is_more = str(mode or "").strip().lower() == "more"
    toggle_callback = (
        f"asmm:analytics_less_{period_callback_suffix}"
        if is_more
        else f"asmm:analytics_more_{period_callback_suffix}"
    )
    toggle_text = "◀️ Основное" if is_more else "Еще"

    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🕒 24ч", callback_data="asmm:analytics_period_day"),
        InlineKeyboardButton("📅 7д", callback_data="asmm:analytics_period_week"),
    )
    kb.row(
        InlineKeyboardButton("🗓 30д", callback_data="asmm:analytics_period_month"),
        InlineKeyboardButton("♾ Всё", callback_data="asmm:analytics_period_all"),
    )
    kb.row(
        InlineKeyboardButton(toggle_text, callback_data=toggle_callback),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"asmm:analytics_period_{period_callback_suffix}"),
    )
    kb.row(
        InlineKeyboardButton("◀️ Назад", callback_data="asmm:return_to_settings"),
    )
    return kb


def _parse_analytics_period(call: types.CallbackQuery, prefix: str) -> str:
    data = str(call.data or "")
    if prefix not in data:
        return "day"
    period = data.split(prefix, 1)[1].strip()
    return period if period in _ANALYTICS_VALID_UI_PERIODS else "day"


def _render_analytics(call: types.CallbackQuery, period: str = "day", mode: Optional[str] = None) -> None:
    msg = getattr(call, "message", None)
    chat_id = getattr(getattr(msg, "chat", None), "id", None)
    message_id = getattr(msg, "message_id", None)
    if chat_id is None:
        _safe_answer_callback(call)
        return
    period_norm = _ANALYTICS_PERIOD_MAP.get(str(period or "day").strip().lower(), "24h")
    if mode is None:
        mode = analytics._get_analytics_view_mode(chat_id, message_id)
    else:
        analytics._set_analytics_view_mode(chat_id, message_id, mode)

    try:
        txt = analytics.format_analytics(period=period_norm, mode=mode)
    except Exception as e:
        rt.logger.error(f"Ошибка формирования аналитики: {e}")
        txt = "❌ Ошибка формирования аналитики."

    kb = build_analytics_keyboard(period=period_norm, mode=mode)
    if message_id is not None:
        try:
            rt.bot.edit_message_text(txt, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
            _safe_answer_callback(call)
            return
        except Exception as e:
            if _is_message_not_modified_error(e):
                _safe_answer_callback(call)
                return
            rt.logger.error(f"Ошибка при редактировании сообщения аналитики: {e}")

    try:
        sent = rt.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
        if sent is not None:
            analytics._set_analytics_view_mode(chat_id, getattr(sent, "message_id", 0), mode)
    except Exception as e2:
        rt.logger.error(f"Ошибка при отправке сообщения аналитики: {e2}")

    _safe_answer_callback(call)


def show_orders(call: types.CallbackQuery) -> None:
    """Render the default 24h analytics screen."""
    _render_analytics(call, period="day", mode="main")


def analytics_period(call: types.CallbackQuery) -> None:
    """Switch the displayed analytics period from callback data."""
    _render_analytics(call, period=_parse_analytics_period(call, "asmm:analytics_period_"))


def analytics_more(call: types.CallbackQuery) -> None:
    """Expand analytics screen details."""
    _render_analytics(call, period=_parse_analytics_period(call, "asmm:analytics_more_"), mode="more")


def analytics_less(call: types.CallbackQuery) -> None:
    """Collapse analytics screen to the primary summary."""
    _render_analytics(call, period=_parse_analytics_period(call, "asmm:analytics_less_"), mode="main")
