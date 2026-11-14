"""Shared Telegram UI helpers, input-state management, and screen navigation."""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt

_NAVIGATION_TARGETS: Dict[str, Callable[[types.CallbackQuery], Any]] = {}
_NAVIGATION_PREFIXES: List[Tuple[str, Callable[[types.CallbackQuery, str], Any]]] = []
_CALLBACK_TOKENS_LOCK = rt.CONTEXT_LOCK
_CALLBACK_TOKENS_MAX = 1000

STATE_RESTORE_BACKUP = "autosmm:restore_backup"


def h(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _extract_call_ids(call: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    msg = getattr(call, "message", None)
    chat_id = getattr(getattr(msg, "chat", None), "id", None)
    message_id = getattr(msg, "message_id", None)
    user_id = getattr(getattr(call, "from_user", None), "id", None)
    return chat_id, message_id, user_id


def clean_tg_text(text: Any) -> str:
    value = str(text or "").strip()
    return re.sub(r"\n{3,}", "\n\n", value)


def _safe_answer_callback(call: types.CallbackQuery, text: Optional[str] = None) -> None:
    if not rt.bot:
        return
    try:
        if text is None:
            rt.bot.answer_callback_query(call.id)
        else:
            rt.bot.answer_callback_query(call.id, text)
    except Exception as exc:
        rt.logger.debug(f"answer_callback_query failed: {exc}")


def _is_message_not_modified_error(exc: Any) -> bool:
    return "message is not modified" in str(exc or "").lower()


def _safe_edit_message_text(
    text_value: str,
    chat_id: int,
    message_id: int,
    **kwargs: Any,
) -> Any:
    try:
        return rt.bot.edit_message_text(text_value, chat_id, message_id, **kwargs)
    except Exception as exc:
        if _is_message_not_modified_error(exc):
            rt.logger.debug(f"Telegram edit_message_text ignored unchanged message: {exc}")
            return None
        raise


def _send_or_edit_text(
    chat_id: int,
    text_value: str,
    kb: InlineKeyboardMarkup,
    message_id: Optional[int] = None,
) -> Optional[int]:
    cleaned = clean_tg_text(text_value)
    if message_id is None:
        sent = rt.bot.send_message(chat_id, cleaned, parse_mode="HTML", reply_markup=kb)
        return getattr(sent, "message_id", None)
    _safe_edit_message_text(cleaned, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
    return int(message_id)


def _edit_screen_if_revision_matches(
    chat_id: int,
    message_id: Optional[int],
    expected_revision: int,
    render_fn: Callable[[], Tuple[str, InlineKeyboardMarkup]],
    *,
    parse_mode: Optional[str] = "HTML",
) -> bool:
    """Re-render and edit a Telegram message only if the user has not navigated away."""
    if message_id is None or not rt.bot:
        return False
    if rt.get_screen_revision(chat_id, message_id) != expected_revision:
        return False
    text_value, kb = render_fn()
    if parse_mode == "HTML":
        text_value = clean_tg_text(text_value)
    if rt.get_screen_revision(chat_id, message_id) != expected_revision:
        return False
    edit_kwargs: Dict[str, Any] = {"reply_markup": kb}
    if parse_mode is not None:
        edit_kwargs["parse_mode"] = parse_mode
    try:
        _safe_edit_message_text(text_value, chat_id, message_id, **edit_kwargs)
        return True
    except Exception as exc:
        rt.logger.debug(f"Async screen refresh skipped (chat={chat_id}, msg={message_id}): {exc}")
        return False


def _is_delete_confirm_grace_active(chat_id: Any) -> bool:
    try:
        return time.time() < float(rt.DELETE_CONFIRM_GRACE_UNTIL.get(int(chat_id), 0) or 0)
    except (TypeError, ValueError):
        return False


def _set_delete_confirm_grace(chat_id: Any, seconds: int = 30) -> None:
    try:
        rt.DELETE_CONFIRM_GRACE_UNTIL[int(chat_id)] = time.time() + int(seconds or 30)
    except (TypeError, ValueError) as exc:
        rt.logger.debug(f"Invalid chat_id/seconds for delete confirm grace: {exc}")


def _truncate_button_text(value: Any, limit: int = 48) -> str:
    text_value = re.sub(r"\s+", " ", str(value or "Без названия")).strip() or "Без названия"
    if len(text_value) <= limit:
        return text_value
    return text_value[: max(1, limit - 1)].rstrip() + "…"


def _not_configured(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value or raw_value.lower() in ("none", "null"):
        return "не настроена"
    return raw_value


def _set_lot_page_context(chat_id: int, page: int) -> int:
    page = max(0, int(page or 0))
    rt.LOT_PAGE_CONTEXT[int(chat_id)] = page
    return page


def _get_lot_page_context(chat_id: int) -> int:
    return max(0, int(rt.LOT_PAGE_CONTEXT.get(int(chat_id), 0) or 0))


def _clear_step_handler(chat_id: int) -> None:
    try:
        rt.bot.clear_step_handler_by_chat_id(chat_id)
    except Exception as exc:
        rt.logger.debug(f"clear_step_handler_by_chat_id({chat_id}) failed: {exc}")


def _set_lot_card_context(chat_id: int, lot_key: str, back_callback: str, back_label: str) -> None:
    rt.LOT_CARD_CONTEXT[int(chat_id)] = {
        "lot_key": lot_key,
        "back_callback": back_callback,
        "back_label": back_label,
        "created_ts": time.time(),
    }


def _get_lot_card_context(chat_id: int) -> Optional[Dict[str, Any]]:
    return rt.LOT_CARD_CONTEXT.get(int(chat_id))


def _set_lot_input_state(
    chat_id: int,
    lot_key: str,
    mode: str,
    prompt_text: str,
    next_handler: Any,
    *,
    initiator_user_id: Optional[int] = None,
) -> None:
    rt.prune_stale_contexts()
    card_ctx = _get_lot_card_context(chat_id) or {}
    rt.LOT_INPUT_STATES[int(chat_id)] = {
        "mode": mode,
        "lot_key": lot_key,
        "from": "lot_card",
        "back_target": card_ctx.get("back_callback") or "asmm:lot_settings",
        "back_label": card_ctx.get("back_label") or "◀️ Назад",
        "page": _get_lot_page_context(chat_id),
        "prompt_text": prompt_text,
        "next_handler": next_handler,
        "initiator_user_id": initiator_user_id,
        "created_ts": time.time(),
    }


def _get_lot_input_state(chat_id: int) -> Optional[Dict[str, Any]]:
    return rt.LOT_INPUT_STATES.get(int(chat_id))


def _clear_lot_input_state(chat_id: int) -> None:
    rt.LOT_INPUT_STATES.pop(int(chat_id), None)


def _set_ui_input_state(
    chat_id: int,
    prompt_text: str,
    next_handler: Any,
    *handler_args: Any,
    back_target: str = "asmm:return_to_settings",
    cancel_target: Optional[str] = None,
    initiator_user_id: Optional[int] = None,
) -> None:
    rt.prune_stale_contexts()
    rt.UI_INPUT_STATES[int(chat_id)] = {
        "prompt_text": prompt_text,
        "next_handler": next_handler,
        "handler_args": tuple(handler_args),
        "back_target": back_target,
        "cancel_target": cancel_target,
        "initiator_user_id": initiator_user_id,
        "created_ts": time.time(),
    }


def _get_ui_input_state(chat_id: int) -> Optional[Dict[str, Any]]:
    return rt.UI_INPUT_STATES.get(int(chat_id))


def _clear_ui_input_state(chat_id: int) -> None:
    rt.UI_INPUT_STATES.pop(int(chat_id), None)


def _clear_navigation_states(chat_id: int) -> None:
    _clear_lot_input_state(chat_id)
    _clear_ui_input_state(chat_id)
    rt.LOT_CARD_CONTEXT.pop(int(chat_id), None)


def _clear_autosmm_memory_contexts() -> None:
    rt.clear_memory_contexts()


def _build_ui_input_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    state = _get_ui_input_state(chat_id) or {}
    back_target = state.get("back_target", "asmm:return_to_settings")
    cancel_target = state.get("cancel_target")

    kb_ = InlineKeyboardMarkup(row_width=2)
    if cancel_target and cancel_target != back_target:
        kb_.row(
            InlineKeyboardButton("◀️ Назад", callback_data="asmm:ui_input_back"),
            InlineKeyboardButton("✖️ Отмена", callback_data="asmm:ui_input_cancel"),
        )
    else:
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:ui_input_back"))
    return kb_


def register_navigation_target(target: str, handler: Callable[[types.CallbackQuery], Any]) -> None:
    """Register an exact callback navigation destination."""
    _NAVIGATION_TARGETS[str(target)] = handler


def register_navigation_prefix(prefix: str, handler: Callable[[types.CallbackQuery, str], Any]) -> None:
    """Register a prefix-matched callback navigation destination."""
    clean_prefix = str(prefix)
    for idx, (existing_prefix, _) in enumerate(_NAVIGATION_PREFIXES):
        if existing_prefix == clean_prefix:
            _NAVIGATION_PREFIXES[idx] = (clean_prefix, handler)
            return
    _NAVIGATION_PREFIXES.append((clean_prefix, handler))


def _dispatch_navigation_callback(call: types.CallbackQuery, target: str) -> None:
    _safe_answer_callback(call)
    target = str(target or "asmm:return_to_settings")
    handler = _NAVIGATION_TARGETS.get(target)
    if handler is not None:
        handler(call)
        return
    for prefix, prefix_handler in _NAVIGATION_PREFIXES:
        if target.startswith(prefix):
            prefix_handler(call, target)
            return
    fallback = _NAVIGATION_TARGETS.get("asmm:return_to_settings")
    if fallback is not None:
        fallback(call)


def ui_input_back_callback(call: types.CallbackQuery) -> None:
    chat_id, _, _ = _extract_call_ids(call)
    if chat_id is None:
        _safe_answer_callback(call)
        return
    state = _get_ui_input_state(chat_id)
    _clear_step_handler(chat_id)
    _clear_ui_input_state(chat_id)
    _dispatch_navigation_callback(call, state.get("back_target") if state else "asmm:return_to_settings")


def ui_input_cancel_callback(call: types.CallbackQuery) -> None:
    chat_id, _, _ = _extract_call_ids(call)
    if chat_id is None:
        _safe_answer_callback(call)
        return
    state = _get_ui_input_state(chat_id)
    _clear_step_handler(chat_id)
    _clear_ui_input_state(chat_id)
    fallback_target = state.get("back_target") if state else "asmm:return_to_settings"
    target = state.get("cancel_target") if state and state.get("cancel_target") else fallback_target
    _dispatch_navigation_callback(call, target)


def _is_step_sender_allowed(message: Any, expected_user_id: Optional[int]) -> bool:
    sender = getattr(message, "from_user", None)
    sender_id = getattr(sender, "id", None)
    if expected_user_id is not None and sender_id != expected_user_id:
        return False
    if sender_id is None:
        tg = getattr(rt.cardinal_instance, "telegram", None)
        auth_users = getattr(tg, "authorized_users", None)
        if isinstance(auth_users, (dict, set, list, tuple)) and len(auth_users) > 0:
            return False
        return expected_user_id is None
    return _is_authorized_tg_user(sender_id)


def _register_guarded_step_handler(
    anchor_message: Any,
    next_handler: Any,
    *handler_args: Any,
    expected_user_id: Optional[int] = None,
    allowed_commands: Optional[Any] = None,
) -> None:
    allowed = {
        str(cmd).strip().lower()
        for cmd in (allowed_commands or getattr(next_handler, "allowed_step_commands", ()) or ())
        if str(cmd).strip()
    }

    def _guarded_step(message: types.Message) -> Any:
        if not _is_step_sender_allowed(message, expected_user_id):
            sender_id = getattr(getattr(message, "from_user", None), "id", None)
            rt.logger.warning(
                f"[TG STEP IGNORED] chat_id={getattr(getattr(message, 'chat', None), 'id', None)}, "
                f"expected_user={expected_user_id}, actual_user={sender_id}"
            )
            rt.bot.register_next_step_handler(message, _guarded_step)
            return None
        raw_text = getattr(message, "text", None)
        if isinstance(raw_text, str) and raw_text.lstrip().startswith("/"):
            cmd_token = raw_text.lstrip().split(None, 1)[0].split("@", 1)[0].lower()
            if cmd_token not in allowed:
                chat_id = getattr(getattr(message, "chat", None), "id", None)
                sender_id = getattr(getattr(message, "from_user", None), "id", None)
                if chat_id is not None:
                    _clear_step_handler(chat_id)
                    _clear_navigation_states(chat_id)
                _clear_tg_state(chat_id, sender_id)
                return None
        return next_handler(message, *handler_args)

    rt.bot.register_next_step_handler(anchor_message, _guarded_step)


def _encode_callback_value(prefix: str, raw_value: str, *, max_bytes: int = 64) -> str:
    value = str(raw_value or "")
    full = f"{prefix}{value}"
    if len(full.encode("utf-8")) <= max_bytes and not value.startswith("t_"):
        return full
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    token = f"t_{digest}"
    with _CALLBACK_TOKENS_LOCK:
        if token in rt.CALLBACK_TOKENS:
            rt.CALLBACK_TOKENS.pop(token, None)
        while len(rt.CALLBACK_TOKENS) >= _CALLBACK_TOKENS_MAX:
            oldest_key = next(iter(rt.CALLBACK_TOKENS))
            rt.CALLBACK_TOKENS.pop(oldest_key, None)
        rt.CALLBACK_TOKENS[token] = value
    return f"{prefix}{token}"


def _decode_callback_value(raw_arg: str) -> str:
    arg = str(raw_arg or "")
    if arg.startswith("t_"):
        with _CALLBACK_TOKENS_LOCK:
            return str(rt.CALLBACK_TOKENS.get(arg) or "")
    return arg


def _start_ui_input_flow(
    call: types.CallbackQuery,
    prompt_text: str,
    next_handler: Any,
    *handler_args: Any,
    back_target: str = "asmm:return_to_settings",
    cancel_target: Optional[str] = None,
) -> None:
    chat_id, message_id, initiator_user_id = _extract_call_ids(call)
    if chat_id is None:
        return
    _clear_step_handler(chat_id)
    _clear_lot_input_state(chat_id)
    _set_ui_input_state(
        chat_id,
        prompt_text,
        next_handler,
        *handler_args,
        back_target=back_target,
        cancel_target=cancel_target,
        initiator_user_id=initiator_user_id,
    )
    if message_id is not None:
        msg_ = _safe_edit_message_text(
            prompt_text,
            chat_id,
            message_id,
            parse_mode="HTML",
            reply_markup=_build_ui_input_keyboard(chat_id),
        ) or call.message
    else:
        msg_ = rt.bot.send_message(
            chat_id,
            prompt_text,
            parse_mode="HTML",
            reply_markup=_build_ui_input_keyboard(chat_id),
        )
    rt.logger.debug(
        f"[TG STEP START] chat_id={chat_id}, prompt='{prompt_text}', "
        f"handler={getattr(next_handler, '__name__', str(next_handler))}"
    )
    _register_guarded_step_handler(msg_, next_handler, *handler_args, expected_user_id=initiator_user_id)


def _repeat_ui_input_flow(chat_id: int, error_text: str = "") -> None:
    state = _get_ui_input_state(chat_id)
    if not state:
        return
    text_ = f"{error_text}\n\n{state['prompt_text']}" if error_text else state["prompt_text"]
    msg_ = rt.bot.send_message(
        chat_id,
        text_,
        parse_mode="HTML",
        reply_markup=_build_ui_input_keyboard(chat_id),
    )
    _register_guarded_step_handler(
        msg_,
        state["next_handler"],
        *state.get("handler_args", ()),
        expected_user_id=state.get("initiator_user_id"),
    )


def _set_tg_state(
    chat_id: int,
    message_id: int,
    user_id: int,
    state: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    tg = getattr(rt.cardinal_instance, "telegram", None)
    set_state = getattr(tg, "set_state", None)
    if callable(set_state):
        try:
            set_state(chat_id, message_id, user_id, state, data)
        except Exception as exc:
            rt.logger.debug(f"set_state({chat_id}, {user_id}, {state}) failed: {exc}")


def _clear_tg_state(chat_id: Optional[int], user_id: Optional[int]) -> None:
    if chat_id is None or user_id is None:
        return
    tg = getattr(rt.cardinal_instance, "telegram", None)
    clear_state = getattr(tg, "clear_state", None)
    if callable(clear_state):
        try:
            clear_state(chat_id, user_id, del_msg=False)
        except Exception as exc:
            rt.logger.debug(f"clear_state({chat_id}, {user_id}) failed: {exc}")


def _is_authorized_tg_user(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    tg = getattr(rt.cardinal_instance, "telegram", None)
    auth_users = getattr(tg, "authorized_users", None)
    if isinstance(auth_users, (dict, set, list, tuple)):
        return user_id in auth_users
    return False


def _clear_screen_state(call: types.CallbackQuery) -> None:
    chat_id, _, user_id = _extract_call_ids(call)
    if chat_id is not None:
        _clear_step_handler(chat_id)
        _clear_navigation_states(chat_id)
    _clear_tg_state(chat_id, user_id)


def _inline_kb(*buttons: Tuple[str, str], row_width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=row_width)
    if buttons:
        kb.add(*(InlineKeyboardButton(label, callback_data=cb) for label, cb in buttons))
    return kb


def _edit_html_screen(
    call: types.CallbackQuery,
    text_value: str,
    kb: InlineKeyboardMarkup,
    *,
    clean: bool = False,
    disable_preview: bool = False,
) -> None:
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None or message_id is None:
        return
    payload = clean_tg_text(text_value) if clean else text_value
    _safe_edit_message_text(
        payload,
        chat_id,
        message_id,
        parse_mode="HTML",
        reply_markup=kb,
        **({"disable_web_page_preview": True} if disable_preview else {}),
    )
