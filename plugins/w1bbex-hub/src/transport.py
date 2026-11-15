"""Telegram HTML transport with graceful styling and custom emoji fallbacks."""

from __future__ import annotations

import html
import threading
from typing import Any, Callable

from .runtime import CB_ANSWERED, logger
from .ui.icons import without_custom_emojis
from .ui.kit import has_custom_button_icons, has_styled_buttons, strip_button_icons, strip_button_styles


def _is_authorized(tg: Any, user: Any) -> bool:
    """Return True when the Telegram user belongs to Cardinal's authorized admins."""
    if tg is None or user is None:
        return False
    user_id = getattr(user, "id", None)
    if user_id is None:
        return False
    auth_users = getattr(tg, "authorized_users", None)
    if isinstance(auth_users, (dict, set, list, tuple)):
        return user_id in auth_users
    return False


def _call_tg_html(invoke: Callable[[str, Any], Any], text: str, reply_markup: Any = None,
                  ignore_not_modified: bool = False) -> Any:
    """Retry only known Telegram presentation failures, preserving readable content."""
    for _ in range(4):
        try:
            return invoke(text, reply_markup)
        except Exception as exc:
            reason = str(exc).lower()
            if ignore_not_modified and "message is not modified" in reason:
                return None
            if any(word in reason for word in ("custom emoji", "custom_emoji", "premium", "emoticon", "document_invalid")):
                plain_text = without_custom_emojis(text)
                if plain_text != text or has_custom_button_icons(reply_markup):
                    text = plain_text
                    if reply_markup is not None:
                        reply_markup = strip_button_icons(reply_markup)
                    continue
            if "style" in reason and has_styled_buttons(reply_markup):
                reply_markup = strip_button_styles(reply_markup)
                continue
            if "can't parse entities" in reason and "<" in text:
                plain_text = without_custom_emojis(text)
                text = plain_text if plain_text != text else html.escape(text)
                continue
            raise
    raise RuntimeError("Telegram rejected message presentation after compatibility retries")


class TelegramTransport:
    """Reusable message methods; Cardinal lifecycle and routing remain in handlers."""

    def _get_acked_lock(self) -> threading.Lock:
        lock = vars(self).get("_acked_lock")
        if lock is None:
            lock = threading.Lock()
            vars(self)["_acked_lock"] = lock
        return lock

    def _ack(self, callback: Any, text: str | None = None) -> None:
        cb_id = getattr(callback, "id", None)
        lock = self._get_acked_lock()
        with lock:
            already_acked = getattr(callback, "_hub_acked", False) or (cb_id is not None and cb_id in self._acked_callbacks)
        if already_acked:
            return
        trimmed = text[:200] if text else None
        self.bot.answer_callback_query(callback.id, trimmed, show_alert=False)
        setattr(callback, "_hub_acked", True)
        if cb_id is not None:
            with lock:
                self._acked_callbacks.add(cb_id)

    def _alert(self, callback: Any, text: str, show_alert: bool = True) -> Any:
        cb_id = getattr(callback, "id", None)
        lock = self._get_acked_lock()
        with lock:
            is_acked = getattr(callback, "_hub_acked", False) or (cb_id is not None and cb_id in self._acked_callbacks)
        trimmed = (text or "")[:200]
        if is_acked:
            if show_alert and getattr(callback, "message", None):
                self._send_html(callback.message.chat.id, html.escape(text))
            return CB_ANSWERED
        try:
            self.bot.answer_callback_query(callback.id, trimmed, show_alert=show_alert)
            setattr(callback, "_hub_acked", True)
            if cb_id is not None:
                with lock:
                    self._acked_callbacks.add(cb_id)
        except Exception:
            if show_alert and getattr(callback, "message", None):
                try:
                    self._send_html(callback.message.chat.id, html.escape(text))
                except Exception:
                    logger.debug("Fallback alert send_html failed", exc_info=True)
        return CB_ANSWERED

    def _send_html(self, chat_id: int, text: str, **kw: Any) -> Any:
        markup = kw.pop("reply_markup", None)
        return _call_tg_html(
            lambda t, rm: self.bot.send_message(chat_id, t, parse_mode="HTML", **kw, reply_markup=rm),
            text, markup,
        )

    def _edit_html(self, text: str, chat_id: int, message_id: int, **kw: Any) -> Any:
        markup = kw.pop("reply_markup", None)
        return _call_tg_html(
            lambda t, rm: self.bot.edit_message_text(t, chat_id, message_id, parse_mode="HTML", **kw, reply_markup=rm),
            text, markup, ignore_not_modified=True,
        )

    def _reply_html(self, message: Any, text: str, **kw: Any) -> Any:
        markup = kw.pop("reply_markup", None)
        return _call_tg_html(
            lambda t, rm: self.bot.reply_to(message, t, parse_mode="HTML", **kw, reply_markup=rm),
            text, markup,
        )
