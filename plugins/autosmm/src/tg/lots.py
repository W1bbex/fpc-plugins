"""Telegram UI screens and callbacks for AutoSMM lot mapping list, search, and deletion."""

from __future__ import annotations

import time
from typing import Optional

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..storage.storage import CONFIG_LOCK, load_config, reindex_lots
from .common import (
    _clear_lot_input_state,
    _clear_navigation_states,
    _clear_step_handler,
    _clear_ui_input_state,
    _encode_callback_value,
    _extract_call_ids,
    _get_lot_page_context,
    _is_delete_confirm_grace_active,
    _repeat_ui_input_flow,
    _safe_answer_callback,
    _safe_edit_message_text,
    _set_delete_confirm_grace,
    _set_lot_page_context,
    _start_ui_input_flow,
    _truncate_button_text,
    h,
    register_navigation_prefix,
    register_navigation_target,
)
from .lot_editor import _decode_prefixed_lot_key, _show_lot_card


def _open_lots_page(chat_id: int, message_id: int, page: int) -> None:
    page = max(0, int(page or 0))
    _set_lot_page_context(chat_id, page)
    _clear_step_handler(chat_id)
    _clear_navigation_states(chat_id)
    _safe_edit_message_text(
        "Выберите лот:",
        chat_id,
        message_id,
        reply_markup=generate_lots_keyboard(page),
    )


def _format_search_results_text(term: str, *, has_results: bool, current_page: int, total_pages: int) -> str:
    max_term_len = 200
    display_term = term[:max_term_len] + ("..." if len(term) > max_term_len else "")
    page_suffix = f" (стр. {current_page + 1}/{total_pages})" if has_results and total_pages > 1 else ""

    def _build(candidate_term: str, truncated: bool = False) -> str:
        suffix = "..." if truncated else ""
        escaped = f"{h(candidate_term)}{suffix}"
        if has_results:
            return f"🔍 Результаты поиска для '{escaped}'{page_suffix}:"
        return f"❌ Лоты по запросу '{escaped}' не найдены."

    text_ = _build(display_term, truncated=False)
    if len(text_) <= 4096:
        return text_

    trimmed = display_term
    while trimmed and len(_build(trimmed, truncated=True)) > 4096:
        trimmed = trimmed[: max(0, len(trimmed) - max(1, len(trimmed) // 8))]
    return _build(trimmed, truncated=True)[:4096]


def _render_search_results(
    chat_id: int,
    search_term: str,
    message_id: Optional[int] = None,
    page: int = 0,
) -> None:
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    term = str(search_term or "").strip().lower()

    found_lots = []
    for lot_key, lot_data in lot_map.items():
        lot_name = str(lot_data.get("name", "")).lower()
        service_id = str(lot_data.get("service_id", "")).lower()
        lot_key_l = str(lot_key or "").lower()
        if term and (term in lot_name or term in service_id or term in lot_key_l):
            found_lots.append((lot_key, lot_data))

    per_page = 10
    total_pages = max(1, ((len(found_lots) - 1) // per_page) + 1) if found_lots else 1
    current_page = max(0, min(int(page or 0), total_pages - 1))
    rt.SEARCH_RESULT_CONTEXT[int(chat_id)] = {
        "term": term,
        "page": current_page,
        "created_ts": time.time(),
    }

    start_idx = current_page * per_page
    end_idx = start_idx + per_page
    chunk = found_lots[start_idx:end_idx]

    kb = InlineKeyboardMarkup(row_width=1)
    if chunk:
        for lot_key, lot_data in chunk:
            name_ = lot_data.get("name", "Без названия")
            sid_ = lot_data.get("service_id")
            qty_ = lot_data.get("quantity")
            snum_ = lot_data.get("service_number", 1)
            btn_text = _truncate_button_text(f"{name_} [ID={sid_}, Q={qty_}, панель={snum_}]")
            kb.add(InlineKeyboardButton(btn_text, callback_data=_encode_callback_value("asmm:edit_lot_search_", lot_key)))
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"asmm:search_page_{current_page - 1}"))
        if end_idx < len(found_lots):
            nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"asmm:search_page_{current_page + 1}"))
        if nav_buttons:
            kb.row(*nav_buttons)
    text_ = _format_search_results_text(
        term,
        has_results=bool(chunk),
        current_page=current_page,
        total_pages=total_pages,
    )

    kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="asmm:search_lot"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:lot_settings"))

    if message_id is not None:
        _safe_edit_message_text(text_, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
    else:
        rt.bot.send_message(chat_id, text_, parse_mode="HTML", reply_markup=kb)


def search_results_back(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        return
    ctx = rt.SEARCH_RESULT_CONTEXT.get(int(chat_id))
    if not ctx or not ctx.get("term"):
        lot_settings(call)
        return
    _clear_step_handler(chat_id)
    _clear_navigation_states(chat_id)
    _render_search_results(
        chat_id,
        ctx["term"],
        message_id,
        page=int(ctx.get("page") or 0),
    )


def search_page_navigation(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        return
    ctx = rt.SEARCH_RESULT_CONTEXT.get(int(chat_id))
    if not ctx or not ctx.get("term"):
        lot_settings(call)
        return
    try:
        page = int(str(call.data or "").rsplit("_", 1)[-1])
    except ValueError:
        page = 0
    _clear_step_handler(chat_id)
    _clear_navigation_states(chat_id)
    _render_search_results(chat_id, ctx["term"], message_id, page=page)


def edit_lot_from_search(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        return
    lot_key = _decode_prefixed_lot_key(call, "asmm:edit_lot_search_")
    _clear_step_handler(chat_id)
    _clear_navigation_states(chat_id)
    if not lot_key:
        lot_settings(call)
        return
    _show_lot_card(
        chat_id,
        lot_key,
        message_id,
        back_callback="asmm:search_results_back",
        back_label="◀️ Назад",
    )


def generate_lots_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    items = list(lot_map.items())

    per_page = 10
    total_pages = max(1, ((len(items) - 1) // per_page) + 1) if items else 1
    page = max(0, min(int(page or 0), total_pages - 1))

    start_ = page * per_page
    end_ = start_ + per_page
    chunk = items[start_:end_]

    kb_ = InlineKeyboardMarkup(row_width=1)
    for lot_key, lot_data in chunk:
        btn_text = _truncate_button_text(lot_data.get("name", "Без названия"))
        kb_.add(InlineKeyboardButton(btn_text, callback_data=_encode_callback_value("asmm:edit_lot_", lot_key)))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"asmm:prev_page_{page - 1}"))
    if end_ < len(items):
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"asmm:next_page_{page + 1}"))
    if nav_buttons:
        kb_.row(*nav_buttons)

    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:lot_settings"))
    return kb_


def edit_lot(call: types.CallbackQuery, lot_key: str) -> None:
    _safe_answer_callback(call)
    _clear_lot_input_state(call.message.chat.id)
    _show_lot_card(call.message.chat.id, lot_key, call.message.message_id)


def delete_one_lot(call: types.CallbackQuery, lot_key: str) -> None:
    removed = False
    with CONFIG_LOCK:
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key in lot_map:
            del lot_map[lot_key]
            cfg["lot_mapping"] = lot_map
            reindex_lots(cfg)
            total_lots = len(cfg.get("lot_mapping", {}))
            max_page = max(0, ((total_lots - 1) // 10)) if total_lots else 0
            page_ = min(_get_lot_page_context(call.message.chat.id), max_page)
            _set_lot_page_context(call.message.chat.id, page_)
            removed = True
        else:
            page_ = _get_lot_page_context(call.message.chat.id)
    if removed:
        _safe_answer_callback(call, "Лот удалён")
        _safe_edit_message_text(
            f"✅ Лот {h(lot_key)} удалён и лоты переиндексированы.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=generate_lots_keyboard(page_),
        )
    else:
        _safe_answer_callback(call, "Лот не найден")
        _safe_edit_message_text(
            f"❌ Лот {h(lot_key)} не найден.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=generate_lots_keyboard(page_),
        )


def lot_settings(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    rt.SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🔍 Поиск лота", callback_data="asmm:search_lot"),
        InlineKeyboardButton("📋 Список лотов", callback_data="asmm:show_lots_list"),
        InlineKeyboardButton("◀️ Назад", callback_data="asmm:return_to_settings"),
    )
    _safe_edit_message_text("Управление лотами:", call.message.chat.id, call.message.message_id, reply_markup=kb)


def show_lots_list(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    rt.SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)
    _open_lots_page(call.message.chat.id, call.message.message_id, 0)


def search_lot_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(
        call,
        "Введите название или часть названия лота для поиска:",
        process_lot_search,
        back_target="asmm:lot_settings",
    )


def process_lot_search(message: types.Message) -> None:
    search_term = str(getattr(message, "text", None) or "").strip()
    if not search_term:
        _repeat_ui_input_flow(message.chat.id, "❌ Поисковый запрос не может быть пустым.")
        return
    _clear_ui_input_state(message.chat.id)
    _render_search_results(message.chat.id, search_term)


def edit_lot_callback(call: types.CallbackQuery) -> None:
    lot_key = _decode_prefixed_lot_key(call, "asmm:edit_lot_")
    if not lot_key:
        _safe_answer_callback(call)
        lot_settings(call)
        return
    edit_lot(call, lot_key)


def page_navigation(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    try:
        page_ = int(call.data.split("_")[-1])
    except ValueError:
        page_ = 0
    _open_lots_page(call.message.chat.id, call.message.message_id, page_)


def return_to_lots(call: types.CallbackQuery, target: Optional[str] = None) -> None:
    _safe_answer_callback(call)
    raw = str(target or call.data or "")
    try:
        page_ = int(raw.rsplit("_", 1)[1]) if raw.startswith("asmm:return_to_lots_") else _get_lot_page_context(call.message.chat.id)
    except ValueError:
        page_ = _get_lot_page_context(call.message.chat.id)
    _open_lots_page(call.message.chat.id, call.message.message_id, page_)


def delete_one_lot_callback(call: types.CallbackQuery) -> None:
    lot_key = _decode_prefixed_lot_key(call, "asmm:delete_one_lot_")
    if not lot_key:
        _safe_answer_callback(call)
        lot_settings(call)
        return
    if _is_delete_confirm_grace_active(call.message.chat.id):
        delete_one_lot(call, lot_key)
        return
    _safe_answer_callback(call)
    cfg = load_config()
    lot_title = str(((cfg.get("lot_mapping", {}) or {}).get(lot_key) or {}).get("name") or lot_key)
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("✅ Удалить", callback_data=_encode_callback_value("asmm:confirm_delete_one_lot_", lot_key)))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data=_encode_callback_value("asmm:edit_lot_", lot_key)))
    txt_ = (
        f"⚠️ <b>Удалить лот?</b>\n\n"
        f"Лот:\n{h(lot_title)}\n\n"
        f"Будет удалена только связка AutoSMM.\n"
        f"FunPay-лот на сайте не удаляется.\n\n"
        f"После подтверждения следующие удаления в течение 30 секунд будут выполняться без повторного подтверждения."
    )
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def confirm_delete_one_lot_callback(call: types.CallbackQuery) -> None:
    lot_key = _decode_prefixed_lot_key(call, "asmm:confirm_delete_one_lot_")
    if not lot_key:
        _safe_answer_callback(call)
        lot_settings(call)
        return
    _set_delete_confirm_grace(call.message.chat.id, 30)
    delete_one_lot(call, lot_key)


register_navigation_target("asmm:lot_settings", lot_settings)
register_navigation_prefix("asmm:return_to_lots", return_to_lots)
