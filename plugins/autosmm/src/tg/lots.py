"""Telegram UI screens and callbacks for AutoSMM lot mapping management."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..storage.storage import CONFIG_LOCK, load_config, reindex_lots, save_config
from .common import (
    _clear_lot_input_state,
    _clear_navigation_states,
    _clear_step_handler,
    _clear_ui_input_state,
    _decode_callback_value,
    _encode_callback_value,
    _extract_call_ids,
    _get_lot_card_context,
    _get_lot_input_state,
    _get_lot_page_context,
    _inline_kb,
    _is_delete_confirm_grace_active,
    _not_configured,
    _register_guarded_step_handler,
    _repeat_ui_input_flow,
    _safe_answer_callback,
    _safe_edit_message_text,
    _set_delete_confirm_grace,
    _set_lot_card_context,
    _set_lot_input_state,
    _set_lot_page_context,
    _start_ui_input_flow,
    _truncate_button_text,
    h,
    register_navigation_prefix,
    register_navigation_target,
)


def _build_return_to_lots_callback(chat_id: int) -> str:
    return f"asmm:return_to_lots_{_get_lot_page_context(chat_id)}"


def _build_lot_input_keyboard() -> InlineKeyboardMarkup:
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:lot_input_back"))
    return kb_


def _decode_prefixed_lot_key(call: types.CallbackQuery, prefix: str) -> str:
    data = str(getattr(call, "data", "") or "")
    raw_arg = data.split(prefix, 1)[1] if prefix in data else ""
    return _decode_callback_value(raw_arg)


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


def _show_lot_card(
    chat_id: int,
    lot_key: str,
    message_id: Optional[int] = None,
    back_callback: Optional[str] = None,
    back_label: Optional[str] = None,
) -> None:
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        text_ = f"❌ Лот {h(lot_key)} не найден."
        fallback_back = back_callback or _build_return_to_lots_callback(chat_id)
        kb_missing = _inline_kb((back_label or "◀️ Назад", fallback_back))
        if message_id is not None:
            _safe_edit_message_text(text_, chat_id, message_id, parse_mode="HTML", reply_markup=kb_missing)
        else:
            rt.bot.send_message(chat_id, text_, parse_mode="HTML", reply_markup=kb_missing)
        return

    if not back_callback:
        back_callback = _build_return_to_lots_callback(chat_id)
    if not back_label:
        back_label = "◀️ К списку" if str(back_callback).startswith("asmm:return_to_lots") else "◀️ Назад"

    _set_lot_card_context(chat_id, lot_key, back_callback, back_label)

    ld_ = lot_map[lot_key]
    fallback_number = _not_configured(ld_.get("fallback_service_number"))
    fallback_id = _not_configured(ld_.get("fallback_service_id"))
    cur_fmt = ld_.get("link_format", "as_is")
    link_format_labels = {
        "as_is": "Как введено (ссылка / юзер)",
        "clean_username": "Только username (без @)",
        "username_at": "Только username (@handle)",
        "full_url": "Полная ссылка (https://...)",
    }
    cur_fmt_label = link_format_labels.get(cur_fmt, "Как введено")

    txt_ = "\n".join([
        f"<b>{h(lot_key)}</b>",
        "",
        "<b>Название:</b>",
        h(ld_.get("name") or "Без названия"),
        "",
        f"Номер панели: <code>{h(ld_.get('service_number', 1))}</code>",
        f"ID услуги: <code>{h(ld_.get('service_id', '—'))}</code>",
        f"Кол-во: <code>{h(ld_.get('quantity', '—'))}</code>",
        f"Формат для панели: <code>{h(cur_fmt_label)}</code>",
        "",
        "━━━━━━━━━━━━━━",
        "<b>Резервная панель</b>",
        "",
        f"Номер панели: <code>{h(fallback_number)}</code>",
        f"ID услуги: <code>{h(fallback_id)}</code>",
    ]).strip()

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("Изменить название", callback_data=_encode_callback_value("asmm:change_name_", lot_key)),
        InlineKeyboardButton("Изменить ID услуги", callback_data=_encode_callback_value("asmm:change_id_", lot_key)),
        InlineKeyboardButton("Изменить количество", callback_data=_encode_callback_value("asmm:change_quantity_", lot_key)),
        InlineKeyboardButton("Изменить номер панели", callback_data=_encode_callback_value("asmm:change_snum_", lot_key)),
        InlineKeyboardButton(f"🔗 Формат ссылки: {cur_fmt_label}", callback_data=_encode_callback_value("asmm:cycle_link_fmt_", lot_key)),
        InlineKeyboardButton("Изменить номер резервной панели", callback_data=_encode_callback_value("asmm:change_fallback_snum_", lot_key)),
        InlineKeyboardButton("Изменить ID резервной услуги", callback_data=_encode_callback_value("asmm:change_fallback_id_", lot_key)),
        InlineKeyboardButton("❌ Удалить лот", callback_data=_encode_callback_value("asmm:delete_one_lot_", lot_key)),
        InlineKeyboardButton(back_label, callback_data=back_callback),
    )

    if message_id is not None:
        _safe_edit_message_text(txt_, chat_id, message_id, parse_mode="HTML", reply_markup=kb_)
    else:
        rt.bot.send_message(chat_id, txt_, parse_mode="HTML", reply_markup=kb_)


def cycle_link_fmt_callback(call: types.CallbackQuery) -> None:
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        _safe_answer_callback(call)
        return
    lot_key = _decode_prefixed_lot_key(call, "asmm:cycle_link_fmt_")
    order_formats = ["as_is", "clean_username", "username_at", "full_url"]
    format_short_names = {
        "as_is": "Как введено",
        "clean_username": "Только username",
        "username_at": "Username (@...)",
        "full_url": "Полная ссылка",
    }
    new_fmt: Optional[str] = None
    with CONFIG_LOCK:
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key in lot_map:
            current_fmt = lot_map[lot_key].get("link_format", "as_is")
            next_idx = (order_formats.index(current_fmt) + 1) % len(order_formats) if current_fmt in order_formats else 0
            new_fmt = order_formats[next_idx]
            lot_map[lot_key]["link_format"] = new_fmt
            cfg["lot_mapping"] = lot_map
            save_config(cfg)

    if new_fmt is None:
        _safe_answer_callback(call, "❌ Лот не найден")
        return

    _safe_answer_callback(call, f"Формат: {format_short_names.get(new_fmt, new_fmt)}")
    card_ctx = _get_lot_card_context(chat_id) or {}
    _show_lot_card(
        chat_id,
        lot_key,
        message_id,
        back_callback=card_ctx.get("back_callback"),
        back_label=card_ctx.get("back_label"),
    )


def _start_lot_edit_flow(call: types.CallbackQuery, lot_key: str, mode: str, prompt_text: str, next_handler: Any) -> None:
    chat_id, message_id, initiator_user_id = _extract_call_ids(call)
    if chat_id is None:
        return
    _clear_step_handler(chat_id)
    _clear_ui_input_state(chat_id)
    _set_lot_input_state(chat_id, lot_key, mode, prompt_text, next_handler, initiator_user_id=initiator_user_id)
    if message_id is not None:
        msg_ = _safe_edit_message_text(
            prompt_text,
            chat_id,
            message_id,
            parse_mode="HTML",
            reply_markup=_build_lot_input_keyboard(),
        ) or call.message
    else:
        msg_ = rt.bot.send_message(
            chat_id,
            prompt_text,
            parse_mode="HTML",
            reply_markup=_build_lot_input_keyboard(),
        )
    rt.logger.debug(
        f"[TG STEP START] chat_id={chat_id}, lot={lot_key}, mode={mode}, "
        f"prompt='{prompt_text}', handler={getattr(next_handler, '__name__', str(next_handler))}"
    )
    _register_guarded_step_handler(msg_, next_handler, lot_key, expected_user_id=initiator_user_id)


def lot_input_back_callback(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        return
    state = _get_lot_input_state(chat_id)
    _clear_step_handler(chat_id)
    _clear_lot_input_state(chat_id)
    if state and state.get("lot_key"):
        _show_lot_card(
            chat_id,
            state["lot_key"],
            message_id,
            back_callback=state.get("back_target"),
            back_label=state.get("back_label"),
        )
    else:
        lot_settings(call)


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


def _finish_lot_edit(message: types.Message, lot_key: str, success_text: str) -> None:
    rt.logger.debug(
        f"[TG STEP FINISH] chat_id={message.chat.id}, lot={lot_key}, "
        f"input='{getattr(message, 'text', '')}', result='{success_text}'"
    )
    state = _get_lot_input_state(message.chat.id) or {}
    _clear_lot_input_state(message.chat.id)
    rt.bot.send_message(message.chat.id, success_text, parse_mode="HTML")
    _show_lot_card(
        message.chat.id,
        lot_key,
        back_callback=state.get("back_target"),
        back_label=state.get("back_label"),
    )


def _repeat_lot_edit_flow(chat_id: int, error_text: str = "") -> None:
    rt.logger.debug(f"[TG STEP REPEAT] chat_id={chat_id}, error='{error_text}'")
    state = _get_lot_input_state(chat_id)
    if not state:
        return
    text_ = f"{error_text}\n\n{state['prompt_text']}" if error_text else state["prompt_text"]
    msg_ = rt.bot.send_message(chat_id, text_, parse_mode="HTML", reply_markup=_build_lot_input_keyboard())
    _register_guarded_step_handler(
        msg_,
        state["next_handler"],
        state["lot_key"],
        expected_user_id=state.get("initiator_user_id"),
    )


def _apply_lot_field_update(
    message: types.Message,
    lot_key: str,
    field_name: str,
    value: Any,
    success_text: str,
    require_service_exists: bool = False,
) -> None:
    error_action: Optional[Tuple[str, str]] = None
    updated_key = lot_key
    with CONFIG_LOCK:
        cfg = load_config()
        if require_service_exists and str(value) not in cfg.get("services", {}):
            error_action = ("repeat", f"❌ Ошибка: панель #{h(value)} не существует.")
        else:
            lot_map = cfg.get("lot_mapping", {})
            if lot_key not in lot_map:
                error_action = ("missing", f"❌ Лот {h(lot_key)} не найден.")
            else:
                target_lot = lot_map[lot_key]
                target_lot[field_name] = value
                cfg["lot_mapping"] = lot_map
                reindex_lots(cfg)
                updated_key = next((k for k, v in (cfg.get("lot_mapping") or {}).items() if v is target_lot), lot_key)

    if error_action is not None:
        kind, msg_text = error_action
        if kind == "repeat":
            _repeat_lot_edit_flow(message.chat.id, msg_text)
        else:
            _clear_lot_input_state(message.chat.id)
            rt.bot.send_message(
                message.chat.id,
                msg_text,
                parse_mode="HTML",
                reply_markup=_inline_kb(("◀️ Назад", "asmm:lot_settings")),
            )
        return
    _finish_lot_edit(message, updated_key, success_text)


def _parse_int_or_repeat(message: types.Message, error_text: str, *, min_value: Optional[int] = 1) -> Optional[int]:
    raw_text = str(getattr(message, "text", None) or "").strip()
    try:
        parsed = int(raw_text)
        if min_value is not None and parsed < min_value:
            raise ValueError
        return parsed
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, error_text)
        return None


def process_name_change(message: types.Message, lot_key: str) -> None:
    new_name = str(getattr(message, "text", None) or "").strip()
    if not new_name:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: Название не может быть пустым.")
        return
    _apply_lot_field_update(
        message,
        lot_key,
        "name",
        new_name,
        f"✅ Название лота {h(lot_key)} изменено на {h(new_name)}.",
    )


def process_id_change(message: types.Message, lot_key: str) -> None:
    new_id = _parse_int_or_repeat(message, "❌ Ошибка: ID услуги должно быть положительным числом.", min_value=1)
    if new_id is not None:
        _apply_lot_field_update(message, lot_key, "service_id", new_id, f"✅ ID услуги для {h(lot_key)} изменён на {h(new_id)}.")


def process_quantity_change(message: types.Message, lot_key: str) -> None:
    new_q = _parse_int_or_repeat(message, "❌ Ошибка: Количество должно быть положительным числом.", min_value=1)
    if new_q is not None:
        _apply_lot_field_update(message, lot_key, "quantity", new_q, f"✅ Количество для {h(lot_key)} изменено на {h(new_q)}.")


def process_service_num_change(message: types.Message, lot_key: str) -> None:
    new_snum = _parse_int_or_repeat(message, "❌ Ошибка: Введите номер сервиса (положительное число).", min_value=1)
    if new_snum is not None:
        _apply_lot_field_update(
            message, lot_key, "service_number", new_snum,
            f"✅ Номер сервиса для {h(lot_key)} изменён на {h(new_snum)}.", require_service_exists=True,
        )


def process_fallback_snum_change(message: types.Message, lot_key: str) -> None:
    new_snum = _parse_int_or_repeat(message, "❌ Ошибка: введите номер резервной панели положительным числом.", min_value=1)
    if new_snum is not None:
        _apply_lot_field_update(
            message, lot_key, "fallback_service_number", new_snum,
            f"✅ Номер резервной панели для {h(lot_key)} изменён на {h(new_snum)}.", require_service_exists=True,
        )


def process_fallback_id_change(message: types.Message, lot_key: str) -> None:
    new_id = _parse_int_or_repeat(message, "❌ Ошибка: ID резервной услуги должен быть положительным числом.", min_value=1)
    if new_id is not None:
        _apply_lot_field_update(message, lot_key, "fallback_service_id", new_id, f"✅ ID резервной услуги для {h(lot_key)} изменён на {h(new_id)}.")


def process_new_lot_id_step(message: types.Message) -> None:
    raw_text = str(getattr(message, "text", None) or "").strip()
    try:
        lot_id = int(raw_text)
        if lot_id <= 0:
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: ID лота должно быть положительным числом.")
        return

    try:
        lot_fields = rt.cardinal_instance.account.get_lot_fields(lot_id)
        name = lot_fields.fields.get("fields[summary][ru]", "Без названия")
    except Exception as e:
        _repeat_ui_input_flow(message.chat.id, f"❌ Не удалось получить данные лота: {h(e)}")
        return

    with CONFIG_LOCK:
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})

        lot_indices = []
        for lot_key in lot_map.keys():
            if str(lot_key).startswith("lot_"):
                try:
                    lot_indices.append(int(str(lot_key).split("_", 1)[1]))
                except Exception:
                    continue
        new_lot_key = f"lot_{max(lot_indices, default=0) + 1}"

        new_lot_entry = {
            "name": name,
            "service_id": 1,
            "quantity": 1,
            "service_number": 1,
            "fallback_service_number": None,
            "fallback_service_id": None,
        }
        lot_map[new_lot_key] = new_lot_entry
        cfg["lot_mapping"] = lot_map
        reindex_lots(cfg)
        final_lot_key = next((k for k, v in (cfg.get("lot_mapping") or {}).items() if v is new_lot_entry), new_lot_key)
        page_ = max(0, ((len(cfg.get("lot_mapping") or lot_map) - 1) // 10))
    _clear_ui_input_state(message.chat.id)
    _set_lot_page_context(message.chat.id, page_)
    rt.bot.send_message(message.chat.id, f"✅ Добавлен новый лот {h(final_lot_key)} с названием: {h(name)}", parse_mode="HTML")
    _show_lot_card(
        message.chat.id,
        final_lot_key,
        back_callback=f"asmm:return_to_lots_{page_}",
        back_label="◀️ К списку",
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


def change_name(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, "asmm:change_name_")
    if not lot_key:
        lot_settings(call)
        return
    _start_lot_edit_flow(call, lot_key, "change_name", f"Введите новое название для {h(lot_key)}:", process_name_change)


def change_id(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, "asmm:change_id_")
    if not lot_key:
        lot_settings(call)
        return
    _start_lot_edit_flow(call, lot_key, "change_id", f"Введите новый ID услуги для {h(lot_key)}:", process_id_change)


def change_quantity(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, "asmm:change_quantity_")
    if not lot_key:
        lot_settings(call)
        return
    _start_lot_edit_flow(call, lot_key, "change_quantity", f"Введите новое количество для {h(lot_key)}:", process_quantity_change)


def change_snum(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, "asmm:change_snum_")
    if not lot_key:
        lot_settings(call)
        return
    _start_lot_edit_flow(call, lot_key, "change_snum", f"Введите номер панели для {h(lot_key)}:", process_service_num_change)


def change_fallback_snum_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, "asmm:change_fallback_snum_")
    if not lot_key:
        lot_settings(call)
        return
    _start_lot_edit_flow(call, lot_key, "change_fallback_snum", f"Введите номер резервной панели для {h(lot_key)}:", process_fallback_snum_change)


def change_fallback_id_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, "asmm:change_fallback_id_")
    if not lot_key:
        lot_settings(call)
        return
    _start_lot_edit_flow(call, lot_key, "change_fallback_id", f"Введите ID резервной услуги для {h(lot_key)}:", process_fallback_id_change)


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


def add_new_lot(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(call, "Введите ID лота для добавления:", process_new_lot_id_step, back_target="asmm:return_to_settings")


register_navigation_target("asmm:lot_settings", lot_settings)
register_navigation_target("asmm:add_new_lot", add_new_lot)
register_navigation_target("asmm:search_lot", search_lot_prompt)
register_navigation_target("asmm:search_results_back", search_results_back)
register_navigation_prefix("asmm:return_to_lots", return_to_lots)
