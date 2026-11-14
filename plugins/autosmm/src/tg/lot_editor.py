"""Telegram UI card rendering and step-edit flows for individual AutoSMM lots."""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..storage.storage import CONFIG_LOCK, load_config, reindex_lots, save_config
from .common import (
    _clear_lot_input_state,
    _clear_step_handler,
    _clear_ui_input_state,
    _decode_callback_value,
    _dispatch_navigation_callback,
    _encode_callback_value,
    _extract_call_ids,
    _get_lot_card_context,
    _get_lot_input_state,
    _get_lot_page_context,
    _inline_kb,
    _not_configured,
    _register_guarded_step_handler,
    _repeat_ui_input_flow,
    _safe_answer_callback,
    _safe_edit_message_text,
    _set_lot_card_context,
    _set_lot_input_state,
    _set_lot_page_context,
    _start_ui_input_flow,
    h,
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

    has_fallback = (ld_.get("fallback_service_number") is not None) or (ld_.get("fallback_service_id") is not None)
    buttons = [
        InlineKeyboardButton("Изменить название", callback_data=_encode_callback_value("asmm:change_name_", lot_key)),
        InlineKeyboardButton("Изменить ID услуги", callback_data=_encode_callback_value("asmm:change_id_", lot_key)),
        InlineKeyboardButton("Изменить количество", callback_data=_encode_callback_value("asmm:change_quantity_", lot_key)),
        InlineKeyboardButton("Изменить номер панели", callback_data=_encode_callback_value("asmm:change_snum_", lot_key)),
        InlineKeyboardButton(f"🔗 Формат ссылки: {cur_fmt_label}", callback_data=_encode_callback_value("asmm:cycle_link_fmt_", lot_key)),
        InlineKeyboardButton("Изменить номер резервной панели", callback_data=_encode_callback_value("asmm:change_fallback_snum_", lot_key)),
        InlineKeyboardButton("Изменить ID резервной услуги", callback_data=_encode_callback_value("asmm:change_fallback_id_", lot_key)),
    ]
    if has_fallback:
        buttons.append(InlineKeyboardButton("❌ Сбросить резервную панель", callback_data=_encode_callback_value("asmm:clear_fallback_", lot_key)))
    buttons.extend([
        InlineKeyboardButton("❌ Удалить лот", callback_data=_encode_callback_value("asmm:delete_one_lot_", lot_key)),
        InlineKeyboardButton(back_label, callback_data=back_callback),
    ])

    kb_ = InlineKeyboardMarkup(row_width=1)
    for btn in buttons:
        kb_.add(btn)

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
        _dispatch_navigation_callback(call, "asmm:lot_settings")


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
    raw_text = str(getattr(message, "text", None) or "").strip().lower()
    if raw_text in ("0", "-", "отключить", "сброс", "нет", "none", "off", "откл", "отмена"):
        with CONFIG_LOCK:
            cfg = load_config()
            lot_map = cfg.get("lot_mapping", {})
            if lot_key in lot_map:
                target_lot = lot_map[lot_key]
                target_lot["fallback_service_number"] = None
                target_lot["fallback_service_id"] = None
                cfg["lot_mapping"] = lot_map
                reindex_lots(cfg)
        _finish_lot_edit(message, lot_key, f"✅ Резервная панель для {h(lot_key)} отключена.")
        return

    new_snum = _parse_int_or_repeat(
        message,
        "❌ Ошибка: введите номер резервной панели (положительное число) или 0 / «отключить» для сброса.",
        min_value=1,
    )
    if new_snum is not None:
        _apply_lot_field_update(
            message, lot_key, "fallback_service_number", new_snum,
            f"✅ Номер резервной панели для {h(lot_key)} изменён на {h(new_snum)}.", require_service_exists=True,
        )


def process_fallback_id_change(message: types.Message, lot_key: str) -> None:
    raw_text = str(getattr(message, "text", None) or "").strip().lower()
    if raw_text in ("0", "-", "отключить", "сброс", "нет", "none", "off", "откл", "отмена"):
        with CONFIG_LOCK:
            cfg = load_config()
            lot_map = cfg.get("lot_mapping", {})
            if lot_key in lot_map:
                target_lot = lot_map[lot_key]
                target_lot["fallback_service_id"] = None
                cfg["lot_mapping"] = lot_map
                reindex_lots(cfg)
        _finish_lot_edit(message, lot_key, f"✅ ID резервной услуги для {h(lot_key)} сброшен.")
        return

    new_id = _parse_int_or_repeat(
        message,
        "❌ Ошибка: введите ID резервной услуги (положительное число) или 0 / «отключить» для сброса.",
        min_value=1,
    )
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


def _open_lot_edit_prompt(
    call: types.CallbackQuery,
    prefix: str,
    mode: str,
    prompt_prefix: str,
    next_handler: Callable[[types.Message, str], None],
) -> None:
    _safe_answer_callback(call)
    lot_key = _decode_prefixed_lot_key(call, prefix)
    if not lot_key:
        _dispatch_navigation_callback(call, "asmm:lot_settings")
        return
    _start_lot_edit_flow(call, lot_key, mode, f"{prompt_prefix} {h(lot_key)}:", next_handler)


def change_name(call: types.CallbackQuery) -> None:
    _open_lot_edit_prompt(call, "asmm:change_name_", "change_name", "Введите новое название для", process_name_change)


def change_id(call: types.CallbackQuery) -> None:
    _open_lot_edit_prompt(call, "asmm:change_id_", "change_id", "Введите новый ID услуги для", process_id_change)


def change_quantity(call: types.CallbackQuery) -> None:
    _open_lot_edit_prompt(call, "asmm:change_quantity_", "change_quantity", "Введите новое количество для", process_quantity_change)


def change_snum(call: types.CallbackQuery) -> None:
    _open_lot_edit_prompt(call, "asmm:change_snum_", "change_snum", "Введите номер панели для", process_service_num_change)


def change_fallback_snum_prompt(call: types.CallbackQuery) -> None:
    _open_lot_edit_prompt(
        call,
        "asmm:change_fallback_snum_",
        "change_fallback_snum",
        "Введите номер резервной панели для",
        process_fallback_snum_change,
    )


def change_fallback_id_prompt(call: types.CallbackQuery) -> None:
    _open_lot_edit_prompt(
        call,
        "asmm:change_fallback_id_",
        "change_fallback_id",
        "Введите ID резервной услуги для",
        process_fallback_id_change,
    )


def clear_fallback_callback(call: types.CallbackQuery) -> None:
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        _safe_answer_callback(call)
        return
    lot_key = _decode_prefixed_lot_key(call, "asmm:clear_fallback_")
    if not lot_key:
        _dispatch_navigation_callback(call, "asmm:lot_settings")
        return

    with CONFIG_LOCK:
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key in lot_map:
            target_lot = lot_map[lot_key]
            target_lot["fallback_service_number"] = None
            target_lot["fallback_service_id"] = None
            cfg["lot_mapping"] = lot_map
            reindex_lots(cfg)

    _safe_answer_callback(call, "Резервная панель сброшена")
    _show_lot_card(chat_id, lot_key, message_id)


def add_new_lot(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(call, "Введите ID лота для добавления:", process_new_lot_id_step, back_target="asmm:return_to_settings")
