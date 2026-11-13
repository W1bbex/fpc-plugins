"""Telegram UI screens and callbacks for AutoSMM telemetry consent and 7-day guest gate."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cardinal import Cardinal

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..services.telemetry import (
    AUTOSMM_V008_NO_TELEMETRY_URL,
    TELEMETRY_SECURITY_URL,
    _activate_telemetry_guest_7d,
    _get_anonymous_install_id,
    _get_telemetry_consent_state,
    _maybe_send_telemetry_ping,
    _remember_telemetry_chat_id,
    _set_telemetry_consent,
    _telemetry_consent_accepted,
    _telemetry_guest_active,
    _telemetry_guest_used,
)
from .common import (
    _dispatch_navigation_callback,
    _edit_html_screen,
    _inline_kb,
    _safe_answer_callback,
    _safe_edit_message_text,
    clean_tg_text,
    h,
)


def _build_telemetry_consent_text(c: Optional[Cardinal] = None) -> str:
    try:
        anonymous_install_id = _get_anonymous_install_id(c or rt.cardinal_instance)
    except ValueError:
        anonymous_install_id = "будет сформирован после получения FunPay username и FunPay ID"

    return f"""📊 <b>Телеметрия AutoSMM</b>

<b>AutoSMM</b> — бесплатный плагин.

Для работы <b>AutoSMM v{h(rt.VERSION)}</b> требуется согласие на минимальную техническую статистику.

<b>Как формируется ID:</b>
<blockquote>• <i>AutoSMM локально берёт FunPay username;</i>
• <i>AutoSMM локально берёт FunPay ID;</i>
• <i>из них формируется короткий SHA-256 хеш;</i>
• <i>сырые FunPay username и FunPay ID на сервер не отправляются.</i>
</blockquote>

<b>Отправляется:</b>
<blockquote>• <i>название плагина;</i>
• <i>версия плагина;</i>
• <i>обезличенный технический ID;</i>
• <i>факт запуска.</i>
</blockquote>

<b>Не отправляются:</b>
<blockquote>• <i>FunPay username в открытом виде;</i>
• <i>FunPay ID в открытом виде;</i>
• <i>переписки;</i>
• <i>ссылки покупателей;</i>
• <i>заказы;</i>
• <i>API-ключи;</i>
• <i>cookies;</i>
• <i>настройки лотов.</i>
</blockquote>

На сервер отправляется только короткий хеш.

Пример обезличенного ID: <tg-spoiler>{h(anonymous_install_id)}</tg-spoiler>

<b>Продолжить использование AutoSMM?</b>

Если не хотите использовать телеметрию, можно остаться на <a href=\"{h(AUTOSMM_V008_NO_TELEMETRY_URL)}\">AutoSMM v0.0.8</a> без неё."""


def _build_telemetry_consent_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("✅ Согласиться и продолжить", callback_data="autosmm_telemetry_accept"),
        InlineKeyboardButton("🛡 Насколько это безопасно?", url=TELEMETRY_SECURITY_URL),
        InlineKeyboardButton(f"⛔ Не использовать v{rt.VERSION}", callback_data="autosmm_telemetry_refuse"),
    )
    return kb


def _build_telemetry_refuse_text() -> str:
    if _telemetry_guest_used() and not _telemetry_guest_active():
        return f"""⏳ <b>Гостевой режим AutoSMM завершён</b>

Вы уже использовали 7 дней без телеметрии.

Чтобы продолжить использовать <b>AutoSMM v{h(rt.VERSION)}</b>, примите минимальную техническую статистику.

Если не хотите использовать телеметрию, можно остаться на <a href=\"{h(AUTOSMM_V008_NO_TELEMETRY_URL)}\">AutoSMM v0.0.8</a> без неё."""
    return f"""⛔ <b>Телеметрия не принята</b>

AutoSMM v{h(rt.VERSION)} использует минимальную техническую статистику для бесплатной версии.

Вы можете один раз попробовать плагин <b>7 дней без телеметрии</b>, чтобы проверить работу перед решением.

<blockquote>• телеметрия в гостевом режиме не отправляется;
• предупредим за 48ч и за 12ч до окончания;
• через 7 дней AutoSMM снова попросит выбрать;
• повторно включить гостевой режим нельзя.
</blockquote>

Выберите действие:"""


def _build_telemetry_refuse_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    if not _telemetry_guest_used():
        kb.add(InlineKeyboardButton("🧪 Попробовать 7 дней без телеметрии", callback_data="autosmm_telemetry_guest_7d"))
    kb.add(
        InlineKeyboardButton("✅ Согласиться и продолжить", callback_data="autosmm_telemetry_accept"),
        InlineKeyboardButton("🛡 Насколько это безопасно?", url=TELEMETRY_SECURITY_URL),
        InlineKeyboardButton("📦 AutoSMM v0.0.8 без телеметрии", url=AUTOSMM_V008_NO_TELEMETRY_URL),
    )
    return kb


def _show_telemetry_gate(chat_id: int, message_id: Optional[int] = None) -> None:
    """Send or update the telemetry consent/guest gate prompt for the seller chat."""
    _remember_telemetry_chat_id(chat_id)
    if _get_telemetry_consent_state() is False:
        text_value, kb = _build_telemetry_refuse_text(), _build_telemetry_refuse_keyboard()
    else:
        text_value, kb = _build_telemetry_consent_text(rt.cardinal_instance), _build_telemetry_consent_keyboard()
    if message_id is None:
        rt.bot.send_message(chat_id, clean_tg_text(text_value), parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        _safe_edit_message_text(clean_tg_text(text_value), chat_id, message_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


def telemetry_accept_callback(call: types.CallbackQuery) -> None:
    """Persist telemetry consent, open settings, and immediately emit an initial ping."""
    _safe_answer_callback(call)
    _set_telemetry_consent(True)
    _dispatch_navigation_callback(call, "return_to_settings")
    _maybe_send_telemetry_ping(rt.cardinal_instance, force=True)


def telemetry_refuse_callback(call: types.CallbackQuery) -> None:
    """Persist refusal choice and offer one-time 7-day guest mode or legacy fallback."""
    _safe_answer_callback(call)
    _set_telemetry_consent(False)
    _edit_html_screen(call, _build_telemetry_refuse_text(), _build_telemetry_refuse_keyboard(), clean=True, disable_preview=True)


def telemetry_guest_7d_callback(call: types.CallbackQuery) -> None:
    """Activate the one-time 7-day guest pass when eligible."""
    _safe_answer_callback(call)
    if _telemetry_consent_accepted():
        _dispatch_navigation_callback(call, "return_to_settings")
        return
    chat_id = getattr(getattr(call, "message", None), "chat", None)
    guest_chat_id = getattr(chat_id, "id", None) if chat_id is not None else None
    if not _activate_telemetry_guest_7d(chat_id=guest_chat_id):
        _edit_html_screen(call, _build_telemetry_refuse_text(), _build_telemetry_refuse_keyboard(), clean=True, disable_preview=True)
        return
    text_value = f"""🧪 <b>Гостевой режим включён</b>

AutoSMM v{h(rt.VERSION)} доступен на <b>7 дней без телеметрии</b>.

Мы заранее напомним за <b>48ч</b> и за <b>12ч</b> до окончания. Через 7 дней плагин снова попросит выбрать: принять телеметрию или остаться на AutoSMM v0.0.8."""
    _edit_html_screen(call, text_value, _inline_kb(("🚀 Открыть AutoSMM", "return_to_settings")), clean=True, disable_preview=True)
