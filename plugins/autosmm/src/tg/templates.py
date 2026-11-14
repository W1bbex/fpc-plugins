"""Telegram UI screens and callbacks for editing buyer message templates."""

from __future__ import annotations

import html
from typing import Any, Dict, Optional, Tuple

from telebot import types

from .. import runtime as rt
from ..storage.defaults import (
    DEFAULT_AFTER_COMPLETED_TEMPLATE,
    DEFAULT_AFTER_CONFIRMATION_TEMPLATE,
    DEFAULT_AFTER_PAYMENT_TEMPLATE,
    DEFAULT_LINK_CONFIRM_TEMPLATE,
    _ensure_config_defaults,
)
from ..storage.storage import CONFIG_LOCK, load_config, save_config
from .common import (
    _clear_navigation_states,
    _clear_step_handler,
    _clear_ui_input_state,
    _edit_html_screen,
    _extract_call_ids,
    _inline_kb,
    _repeat_ui_input_flow,
    _safe_answer_callback,
    _safe_edit_message_text,
    _start_ui_input_flow,
    h,
    register_navigation_prefix,
    register_navigation_target,
)

_VAR_LABELS = {
    "buyer_username": "покупатель",
    "lot_title": "название лота",
    "order_id_funpay": "ID заказа FunPay",
    "order_price": "цена",
    "order_amount": "количество FunPay",
    "total_quantity": "итоговое количество услуги",
    "done_quantity": "выполнено",
    "left_quantity": "осталось",
    "progress_percent": "прогресс в процентах",
    "currency": "валюта",
    "link": "ссылка покупателя",
    "panel_order_id": "ID заказа в панели",
}
_PROGRESS_VARS = ("total_quantity", "done_quantity", "left_quantity", "progress_percent")


def _template_vars(*keys: str) -> list[str]:
    return [f"• {{{k}}} — {_VAR_LABELS[k]}" for k in keys]


TEMPLATE_EDITOR_DEFS = {
    "after_payment": {
        "title": "💸 Текст после оплаты",
        "button": "💸 После оплаты",
        "default": DEFAULT_AFTER_PAYMENT_TEMPLATE,
        "variables": _template_vars(
            "buyer_username",
            "lot_title",
            "order_id_funpay",
            "order_price",
            "order_amount",
            *_PROGRESS_VARS,
            "currency",
        ),
    },
    "buyer_link_received_confirm": {
        "title": "✅ Подтверждение ссылки",
        "button": "✅ Подтверждение ссылки",
        "default": DEFAULT_LINK_CONFIRM_TEMPLATE,
        "variables": _template_vars(
            "order_id_funpay",
            "lot_title",
            "link",
            "buyer_username",
            "order_amount",
            "order_price",
            *_PROGRESS_VARS,
            "currency",
        ),
    },
    "after_confirmation": {
        "title": "🚀 Текст после запуска заказа",
        "button": "🚀 После запуска заказа",
        "default": DEFAULT_AFTER_CONFIRMATION_TEMPLATE,
        "variables": _template_vars(
            "lot_title",
            "panel_order_id",
            "link",
            "order_id_funpay",
            *_PROGRESS_VARS,
        ),
    },
    "after_completed": {
        "title": "🎉 Текст после завершения",
        "button": "🎉 После завершения",
        "default": DEFAULT_AFTER_COMPLETED_TEMPLATE,
        "variables": _template_vars(
            "lot_title",
            "panel_order_id",
            "order_id_funpay",
            *_PROGRESS_VARS,
        ),
    },
}


def edit_messages_menu(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    text_ = """💬 <b>Тексты сообщений</b>

Здесь настраиваются сообщения, которые видит покупатель.

Полный список переменных:
https://teletype.in/@w1bbex/AutoSMM"""
    kb = _inline_kb(
        ("💸 После оплаты", "asmm:edit_msg_payment"),
        ("✅ Подтверждение ссылки", "asmm:edit_msg_link_confirm"),
        ("🚀 После запуска заказа", "asmm:edit_msg_confirmation"),
        ("🎉 После завершения", "asmm:edit_msg_completion"),
        ("🔄 Сбросить тексты", "asmm:reset_message_texts_prompt"),
        ("◀️ Назад", "asmm:misc_settings"),
    )
    _edit_html_screen(call, text_, kb)


def _get_template_editor_def(template_key: str) -> Optional[Dict[str, Any]]:
    return TEMPLATE_EDITOR_DEFS.get(str(template_key or ""))


def _get_current_template_text(template_key: str) -> str:
    cfg = load_config()
    messages = cfg.get("messages") or {}
    meta = _get_template_editor_def(template_key) or {}
    return str(messages.get(template_key) or meta.get("default") or "")


def _truncate_escaped_pre_body(raw_text: str, suffix_builder: Any) -> Tuple[str, str]:
    escaped_full = html.escape(raw_text)
    if len(suffix_builder(escaped_full, "")) <= 4096:
        return escaped_full, ""
    warning = "\n\n⚠️ Текст очень длинный, показана часть шаблона."
    lo, hi = 0, len(raw_text)
    best_escaped = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate_escaped = html.escape(raw_text[:mid].rstrip())
        if len(suffix_builder(candidate_escaped, warning)) <= 4096:
            best_escaped = candidate_escaped
            lo = mid + 1
        else:
            hi = mid - 1
    return best_escaped, warning


def _render_template_view_text(template_key: str) -> str:
    meta = _get_template_editor_def(template_key) or {}
    title = meta.get("title", "Текст сообщения")
    raw_template = _get_current_template_text(template_key)
    variables = "\n".join(meta.get("variables") or [])
    escaped_title = html.escape(title)
    escaped_vars = html.escape(variables)

    def _assemble(pre_body: str, warning: str) -> str:
        return f"""{escaped_title}

Текущий текст:

<pre>{pre_body}</pre>{warning}

Доступные переменные:
{escaped_vars}

Полный список переменных:
https://teletype.in/@w1bbex/AutoSMM""".strip()

    pre_body, warning = _truncate_escaped_pre_body(raw_template, _assemble)
    return _assemble(pre_body, warning)


def _show_template_editor_screen(chat_id: int, message_id: Optional[int], template_key: str) -> None:
    if template_key not in TEMPLATE_EDITOR_DEFS:
        return
    kb = _inline_kb(
        ("✏️ Изменить текст", f"asmm:template_edit_{template_key}"),
        ("🔄 Сбросить этот текст", f"asmm:template_reset_prompt_{template_key}"),
        ("◀️ Назад", "asmm:edit_messages"),
    )
    text_ = _render_template_view_text(template_key)
    if message_id is None:
        rt.bot.send_message(chat_id, text_, parse_mode="HTML", reply_markup=kb)
    else:
        _safe_edit_message_text(text_, chat_id, message_id, parse_mode="HTML", reply_markup=kb)


def _extract_template_meta(call: types.CallbackQuery, prefix: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    template_key = call.data.split(prefix, 1)[1]
    meta = _get_template_editor_def(template_key)
    if not meta:
        edit_messages_menu(call)
    return template_key, meta


def template_view_callback(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    template_key, meta = _extract_template_meta(call, "asmm:template_view_")
    if not meta:
        return
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, template_key)


def template_edit_callback(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    template_key, meta = _extract_template_meta(call, "asmm:template_edit_")
    if not meta:
        return
    _start_ui_input_flow(
        call,
        f"Отправьте новый текст для:\n{h(meta.get('title'))}\n\nТекущий текст можно скопировать с прошлого экрана.",
        process_template_text_change,
        template_key,
        back_target=f"asmm:template_view_{template_key}",
    )


def template_reset_prompt_callback(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    template_key, meta = _extract_template_meta(call, "asmm:template_reset_prompt_")
    if not meta:
        return
    kb = _inline_kb(
        ("✅ Сбросить этот текст", f"asmm:template_reset_confirm_{template_key}"),
        ("◀️ Назад", f"asmm:template_view_{template_key}"),
    )
    _safe_edit_message_text(
        f"Сбросить стандартный текст?\n\n{meta.get('title')}",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb,
    )


def template_reset_confirm_callback(call: types.CallbackQuery) -> None:
    template_key, meta = _extract_template_meta(call, "asmm:template_reset_confirm_")
    if not meta:
        return
    with CONFIG_LOCK:
        cfg = load_config()
        cfg.setdefault("messages", {})[template_key] = meta.get("default") or ""
        save_config(cfg)
    _safe_answer_callback(call, "Текст сброшен")
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, template_key)


def process_template_text_change(message: types.Message, template_key: str) -> None:
    new_text = str(getattr(message, "text", None) or "").strip()
    if not new_text:
        _repeat_ui_input_flow(message.chat.id, "❌ Текст не может быть пустым.")
        return
    if template_key not in TEMPLATE_EDITOR_DEFS:
        _clear_ui_input_state(message.chat.id)
        rt.bot.send_message(message.chat.id, "❌ Неизвестный текст сообщения.", reply_markup=_inline_kb(("◀️ Назад", "asmm:edit_messages")))
        return
    with CONFIG_LOCK:
        cfg = load_config()
        cfg.setdefault("messages", {})[template_key] = new_text
        save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    rt.bot.send_message(message.chat.id, "✅ Текст обновлён.")
    _show_template_editor_screen(message.chat.id, None, template_key)


def edit_msg_payment(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "after_payment")


def edit_msg_confirmation(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "after_confirmation")


def edit_msg_link_confirm(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "buyer_link_received_confirm")


def edit_msg_completion(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "after_completed")


def reset_message_texts_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    kb = _inline_kb(("✅ Сбросить тексты", "asmm:reset_message_texts_confirm"), ("◀️ Назад", "asmm:edit_messages"))
    _safe_edit_message_text("Сбросить стандартные тексты AutoSMM?", call.message.chat.id, call.message.message_id, reply_markup=kb)


def reset_message_texts_confirm(call: types.CallbackQuery) -> None:
    with CONFIG_LOCK:
        cfg = load_config()
        cfg.setdefault("messages", {})
        for key, meta in TEMPLATE_EDITOR_DEFS.items():
            cfg["messages"][key] = meta.get("default") or ""
        _ensure_config_defaults(cfg)
        save_config(cfg)
    _safe_answer_callback(call, "Тексты сброшены")
    edit_messages_menu(call)


def _nav_template_view(call: types.CallbackQuery, target: str) -> None:
    _safe_answer_callback(call)
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        return
    template_key = str(target or "").split("asmm:template_view_", 1)[1] if "asmm:template_view_" in str(target or "") else ""
    if template_key in TEMPLATE_EDITOR_DEFS:
        _show_template_editor_screen(chat_id, message_id, template_key)
    else:
        edit_messages_menu(call)


register_navigation_target("asmm:edit_messages", edit_messages_menu)
register_navigation_prefix("asmm:template_view_", _nav_template_view)
