"""Telegram transport orchestration, update scheduler, and Cardinal bindings."""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from telebot.types import CallbackQuery, InlineKeyboardMarkup, Message

from .catalog import catalog_status, merge_local_plugins
from .transport import TelegramTransport, _call_tg_html
from .navigation import HubNavigation
from .hub_updates import notify_hub_update
from .manual_updates import include_manual_installs, notification_chat
from .core import (
    collect_pending_updates,
    delete_plugin_files,
    download_and_update_hub,
    download_plugin,
    extract_py_plugin_metadata,
    fetch_hub_latest_version,
    find_loaded_plugin,
    get_disk_hub_pending_version,
    get_plugins,
    get_sys_info,
    inspect_plugin_state,
    invoke_plugin_command,
    is_newer_version,
    request_trial_key,
    scan_legacy_plugin_files,
    trigger_cardinal_restart,
)
from .menu_background import MenuBackground
from .runtime import (
    CB_ANSWERED,
    CACHE_LOCK,
    PLUGINS_CACHE,
    PATHS,
    RESTART_NOTICE_TTL_SECONDS,
    STOP_EVENT,
    UPDATE_WAKE_EVENT,
    UPDATE_CHECK_INITIAL_DELAY_SECONDS,
    UUID,
    VERSION,
    configure_file_logging,
    logger,
)
from .storage import (
    clear_pending_restart,
    load_data,
    peek_pending_restart,
    process_pending_deletions,
    save_pending_restart,
    update_data,
)
from .ui import (
    esc,
    has_styled_buttons,
    rebuild_notification_toggle_markup,
    render_activate_key_prompt,
    render_catalog_unavailable,
    render_delete_confirm,
    render_delete_result,
    render_hub_update_error,
    render_hub_update_result,
    render_install_result,
    render_key_activated,
    render_licenses,
    render_main_menu,
    render_missing_key_notice,
    render_plugin_info,
    render_plugins_menu,
    render_post_restart_report,
    render_resources,
    render_restart_in_progress,
    render_trial_activated,
    render_trial_prompt,
    render_update_notification,
    resolve_callback_data,
    strip_button_styles,
)

if TYPE_CHECKING:
    from cardinal import Cardinal


POST_RESTART_FALLBACK_DELAY_SECONDS = 8.0

_UPDATE_THREAD_LOCK = threading.Lock()
_UPDATE_THREAD: Optional[threading.Thread] = None
_UPDATE_STOP_EVENT: Optional[threading.Event] = None
_UPDATE_WAKE_EVENT: Optional[threading.Event] = None
_POST_RESTART_LOCK = threading.Lock()
_POST_RESTART_ACTIVE = False
_POST_RESTART_TIMER: Optional[threading.Timer] = None
_POST_INIT_EVENT = threading.Event()
_POST_RESTART_WAKE_EVENT = threading.Event()
_FALLBACK_THREAD_LOCK = threading.Lock()
_FALLBACK_THREAD: Optional[threading.Thread] = None
_FALLBACK_STOP_EVENT: Optional[threading.Event] = None


def wake_update_checker() -> None:
    """Wake both legacy global wake event and the active reloadable update loop."""
    UPDATE_WAKE_EVENT.set()
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_WAKE_EVENT is not None:
            _UPDATE_WAKE_EVENT.set()


def _is_authorized(tg: Any, user: Any) -> bool:
    if tg is None or user is None:
        return False
    auth_users = getattr(tg, "authorized_users", None)
    if isinstance(auth_users, (dict, set, list, tuple)):
        return getattr(user, "id", None) in auth_users
    return False


def _parse_cb_arg(data: Optional[str], prefix: str) -> Optional[str]:
    if not data or not data.startswith(prefix):
        return None
    return data[len(prefix):] or None


def _build_plugin_states(
    cardinal: Any,
    force_refresh: bool = False,
    cached_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if cached_only:
        with CACHE_LOCK:
            plugins = [dict(item) for item in PLUGINS_CACHE["items"]]
    else:
        plugins = get_plugins(force_refresh=force_refresh) if force_refresh else get_plugins()
    data = load_data()
    installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    legacy_entries = scan_legacy_plugin_files()
    states = [
        inspect_plugin_state(cardinal, p, installed, keys, legacy_entries=legacy_entries)
        for p in merge_local_plugins(plugins, installed)
    ]
    return states, data


def _save_restart_marker(
    action: str,
    chat_id: Any,
    plugin_id: str = "",
    plugin_name: str = "",
    target_version: str = "",
    expected_uuid: str = "",
    stub_path: str = "",
    bin_dir: str = "",
    message_id: Any = None,
) -> None:
    if not chat_id:
        return
    try:
        save_pending_restart(
            {
                "action": action,
                "chat_id": chat_id,
                "message_id": message_id,
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "target_version": target_version,
                "expected_uuid": expected_uuid,
                "stub_path": stub_path,
                "bin_dir": bin_dir,
            }
        )
    except OSError:
        logger.exception("Failed persisting pending_restart state")


def get_main_menu_content(cardinal: Any, user_id: Any, username: Any, *, cached_only: bool = False,
                          version: Optional[tuple[str, bool]] = None) -> tuple[str, InlineKeyboardMarkup]:
    tg_user = type("UserObj", (), {"id": user_id, "username": username})()
    latest_version, has_update = version if version is not None else fetch_hub_latest_version(cardinal, tg_user)
    states, data = _build_plugin_states(cardinal, cached_only=cached_only)
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    hub_pending_ver = get_disk_hub_pending_version()
    return render_main_menu(
        plugin_states=states,
        sys_info=get_sys_info(),
        notify_updates=bool(data.get("notify_updates", True)),
        hub_latest_version=latest_version,
        has_hub_update=has_update and not bool(hub_pending_ver),
        hub_pending_version=hub_pending_ver,
        keys_count=len(keys),
        catalog_status=catalog_status(),
    )


def run_update_check_cycle(cardinal: Any) -> int:
    """Check Hub and installed plugins without requiring the owner to open /hub."""
    data = load_data()
    if not data.get("auto_check_updates", True):
        return 0
    tg_id = notification_chat(cardinal, data)
    if not tg_id:
        return 0

    if not data.get("notify_updates", True):
        user = type("UserObj", (), {"id": tg_id, "username": ""})()
        fetch_hub_latest_version(cardinal, user)
        get_plugins()
        return 0

    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None)
    if tg is None or bot is None:
        return 0

    notification_data = {**data, "owner_tg_id": tg_id}
    sent_count = notify_hub_update(cardinal, notification_data)
    plugins = get_plugins()
    if not plugins:
        return sent_count

    pending = collect_pending_updates(include_manual_installs(cardinal, plugins, notification_data), plugins, time.time())
    if not pending:
        return sent_count
    notify_enabled = bool(data.get("notify_updates", True))
    for update in pending:
        text, kb = render_update_notification(update, notify_enabled=notify_enabled)
        try:
            _call_tg_html(
                lambda t, rm: bot.send_message(tg_id, t, parse_mode="HTML", reply_markup=rm),
                text,
                reply_markup=kb,
            )
        except Exception as exc:
            logger.error("Failed sending update notification for %s: %s", update["id"], exc)
            continue

        update_key = update["update_key"]
        try:
            update_data(lambda cur, k=update_key: cur.setdefault("notified_updates", {}).__setitem__(k, -1))
            sent_count += 1
        except OSError:
            logger.exception("Failed recording notified update state for %s", update_key)
    return sent_count


def _update_checker_loop(
    cardinal: Any,
    stop_event: threading.Event,
    wake_event: Optional[threading.Event] = None,
) -> None:
    """Daemon loop that sleeps cleanly even when no plugins are installed."""
    wake = wake_event or UPDATE_WAKE_EVENT
    if stop_event.wait(UPDATE_CHECK_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        try:
            run_update_check_cycle(cardinal)
        except Exception:
            logger.exception("Update checker error")
        try:
            data = load_data()
        except OSError:
            logger.exception("Update checker failed reading Hub config")
            data = {}
        raw_interval = data.get("update_check_interval_minutes", 30)
        interval = raw_interval if isinstance(raw_interval, int) and not isinstance(raw_interval, bool) and 5 <= raw_interval <= 10080 else 30
        wake.wait(interval * 60)
        wake.clear()
        if stop_event.is_set():
            break


def process_post_restart_notice(cardinal: Any, retry_delay: float = 2.0) -> bool:
    """Inspect pending_restart, verify Cardinal/disk state, notify the owner, then clear marker."""
    global _POST_RESTART_ACTIVE
    configure_file_logging()

    with _POST_RESTART_LOCK:
        if _POST_RESTART_ACTIVE:
            return False
        try:
            pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
        except OSError:
            logger.exception("Failed reading pending_restart during post_init")
            return False
        if not pending:
            return False
        _POST_RESTART_ACTIVE = True

    try:
        try:
            data = load_data()
            chat_id = pending.get("chat_id") or data.get("owner_tg_id")
            if not chat_id:
                clear_pending_restart()
                return False

            bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
            if bot is None:
                logger.warning("Post-restart Hub report deferred: Telegram bot unavailable")
                return False

            action = str(pending.get("action") or "general")
            pid = str(pending.get("plugin_id") or "")
            installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
            keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}

            state: Optional[dict[str, Any]] = None
            disk_still_exists = False

            if action == "hub":
                target_ver = str(pending.get("target_version") or "")
                pending["hub_mismatch"] = bool(target_ver and is_newer_version(target_ver, VERSION))
            elif pid:
                catalog = get_plugins()
                p_info = next((x for x in catalog if x.get("id") == pid), None) or {
                    "id": pid,
                    "name": pending.get("plugin_name") or pid,
                    "uuid": pending.get("expected_uuid") or "",
                    "latest_version": pending.get("target_version") or "",
                }
                state = inspect_plugin_state(cardinal, p_info, installed, keys)
                if action == "delete":
                    disk_still_exists = any(
                        bool(path and os.path.exists(str(path)))
                        for path in (pending.get("stub_path"), pending.get("bin_dir"))
                    )

            if (action == "hub" and not pending.get("hub_mismatch")) or (action == "general" and not pid):
                banner = (
                    f"<b>W1bbex Hub обновлён до v{esc(VERSION)}</b>"
                    if action == "hub"
                    else "<b>FunPay Cardinal перезапущен</b>"
                )
                plugin_states, cur_data = _build_plugin_states(cardinal)
                cur_keys = cur_data.get("keys") if isinstance(cur_data.get("keys"), dict) else {}
                text, kb = render_main_menu(
                    plugin_states=plugin_states,
                    sys_info=get_sys_info(),
                    notify_updates=bool(cur_data.get("notify_updates", True)),
                    hub_latest_version=VERSION,
                    has_hub_update=False,
                    hub_pending_version=get_disk_hub_pending_version(),
                    keys_count=len(cur_keys),
                    notice_banner=banner,
                    catalog_status=catalog_status(),
                )
            else:
                text, kb = render_post_restart_report(
                    pending=pending,
                    state=state,
                    running_hub_version=VERSION,
                    disk_still_exists=disk_still_exists,
                )
        except Exception:
            logger.exception("Failed preparing post-restart Hub report")
            return False

        old_msg_id = pending.get("message_id")
        if old_msg_id:
            try:
                bot.delete_message(chat_id, old_msg_id)
            except Exception:
                logger.debug("Could not delete transient restart message %s", old_msg_id, exc_info=True)

        for attempt in range(2):
            try:
                _call_tg_html(
                    lambda t, rm: bot.send_message(
                        chat_id,
                        t,
                        parse_mode="HTML",
                        reply_markup=rm,
                        disable_web_page_preview=True,
                    ),
                    text,
                    reply_markup=kb,
                )
                try:
                    clear_pending_restart()
                except OSError:
                    logger.exception("Failed clearing pending_restart after successful delivery")
                logger.info("Sent post-restart Hub notice action=%s pid=%s chat_id=%s", action, pid or "-", chat_id)
                return True
            except Exception:
                if attempt == 0 and retry_delay > 0:
                    logger.warning("Retrying post-restart Hub notice delivery to chat %s", chat_id, exc_info=True)
                    time.sleep(retry_delay)
                else:
                    logger.exception("Failed sending post-restart Hub report to chat %s", chat_id)
        return False
    finally:
        with _POST_RESTART_LOCK:
            _POST_RESTART_ACTIVE = False


class HubController(HubNavigation, TelegramTransport):
    """Handles Telegram commands, callback queries, and step flows for W1bbex Hub."""

    def __init__(self, cardinal: Any) -> None:
        self.cardinal = cardinal
        self.tg = cardinal.telegram
        self.bot = self.tg.bot
        self._acked_callbacks: set[str] = set()
        self._menu_background = MenuBackground()
        self._views_lock = threading.Lock()
        self._catalog_views: dict[tuple[int, int], str] = {}
        self._hub_states: dict[tuple[int, int], dict[str, Any]] = {}
        self._actions_lock = threading.Lock()
        self._in_flight_actions: set[str] = set()
        self._exact_routes: dict[str, Callable[[CallbackQuery], Any]] = {}
        self._prefix_routes: tuple[tuple[str, Callable[[CallbackQuery, Optional[str]], Any]], ...] = ()

    def set_routes(
        self,
        exact_routes: dict[str, Callable[[CallbackQuery], Any]],
        prefix_routes: tuple[tuple[str, Callable[[CallbackQuery, Optional[str]], Any]], ...],
    ) -> None:
        """Store route tables so compact callback tokens can dispatch through the same handlers."""
        self._exact_routes = exact_routes
        self._prefix_routes = prefix_routes

    def dispatch_compact_callback(self, c: CallbackQuery, _arg: Optional[str] = None) -> Any:
        """Resolve a tokenized callback_data and execute the matching Hub route."""
        resolved = resolve_callback_data(getattr(c, "data", None))
        if not resolved or resolved == getattr(c, "data", None):
            return self._alert(c, "Кнопка устарела · откройте /hub заново.")
        fn = self._exact_routes.get(resolved)
        if fn is not None:
            return fn(c)
        for prefix, handler in self._prefix_routes:
            if resolved.startswith(prefix):
                return handler(c, _parse_cb_arg(resolved, prefix))
        return self._alert(c, "Действие не найдено · откройте /hub заново.")

    def _states(self, force_refresh: bool = False, cached_only: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return _build_plugin_states(self.cardinal, force_refresh=force_refresh, cached_only=cached_only)

    def wrap_cb(self, func: Callable[[CallbackQuery], Any]) -> Callable[[CallbackQuery], None]:
        def wrapper(c: CallbackQuery) -> None:
            cb_id = getattr(c, "id", None)
            try:
                if not _is_authorized(self.tg, getattr(c, "from_user", None)):
                    self._ack(c)
                    return
                if getattr(c, "message", None) is None:
                    self._alert(c, "Сообщение устарело · откройте /hub")
                    return
                self._menu_background.invalidate(c.message.chat.id, c.message.id)
                self._clear_hub_state(c.message.chat.id, self._step_user_id(c))
                func(c)
            except Exception:
                logger.exception("Hub callback failed")
                if getattr(c, "message", None):
                    self._alert(c, "Не удалось выполнить действие · откройте /hub и повторите попытку")
            finally:
                if cb_id is not None:
                    if cb_id not in self._acked_callbacks:
                        try:
                            self._ack(c)
                        except Exception:
                            logger.debug("Could not acknowledge callback", exc_info=True)
                    self._acked_callbacks.discard(cb_id)
        return wrapper

    def main_menu(self, c: CallbackQuery) -> None:
        text, kb = self._main_content(c.from_user)
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb)
        def redraw() -> None:
            new_text, new_kb = self._main_content(c.from_user)
            if new_text != text or new_kb.to_json() != kb.to_json():
                self._edit_html(new_text, c.message.chat.id, c.message.id, reply_markup=new_kb)
        self._schedule_menu(c.message.chat.id, c.message.id, c.from_user, redraw)

    def resources_menu(self, c: CallbackQuery) -> None:
        text, kb = render_resources()
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb, disable_web_page_preview=True)

    def cancel_step(self, c: CallbackQuery) -> None:
        self._clear_hub_state(c.message.chat.id, self._step_user_id(c))
        self.main_menu(c)

    def activate_key_handler(self, c: CallbackQuery) -> None:
        text, kb = render_activate_key_prompt()
        msg = self._send_html(c.message.chat.id, text, reply_markup=kb)
        self._set_hub_state(
            c.message.chat.id,
            getattr(msg, "id", getattr(c.message, "id", 0)),
            self._step_user_id(c),
            self.STATE_AWAIT_KEY,
            {"callback": c, "prompt_msg_id": getattr(msg, "id", None)},
        )

    def _is_hub_step_message(self, message: Message) -> bool:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return False
        chat = getattr(message, "chat", None)
        if chat is None:
            return False
        return self._get_hub_state(chat.id, self._step_user_id(message)) is not None

    def _handle_step_message(self, message: Message) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        chat_id = message.chat.id
        user_id = self._step_user_id(message)
        entry = self._get_hub_state(chat_id, user_id)
        if not entry:
            return
        state = entry.get("state")
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        orig_callback = data.get("callback")
        if orig_callback is None:
            self._clear_hub_state(chat_id, user_id)
            return
        prompt_msg_id = data.get("prompt_msg_id")
        if state == self.STATE_AWAIT_KEY:
            self._process_key_input(message, orig_callback, prompt_msg_id=prompt_msg_id)
        elif state == self.STATE_AWAIT_INTERVAL:
            self._process_interval_input(message, orig_callback, prompt_msg_id=prompt_msg_id)

    def _delete_prompt_message(self, chat_id: int, prompt_msg_id: Any) -> None:
        if prompt_msg_id:
            try:
                self.bot.delete_message(chat_id, prompt_msg_id)
            except Exception:
                logger.debug("Could not delete prompt message %s", prompt_msg_id, exc_info=True)

    def _process_key_input(
        self,
        message: Message,
        orig_callback: CallbackQuery,
        *,
        prompt_msg_id: Any = None,
    ) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        chat_id = message.chat.id
        user_id = self._step_user_id(message)
        key = message.text.strip() if message.text else ""
        if key.startswith("/"):
            self._clear_hub_state(chat_id, user_id)
            self._delete_prompt_message(chat_id, prompt_msg_id)
            if key.split()[0].split("@")[0].lower() == "/hub":
                self.hub_command(message)
            else:
                self._reply_html(message, "Ввод ключа отменён · отправьте команду ещё раз")
            return
        if key.lower() == "отмена":
            self._clear_hub_state(chat_id, user_id)
            self._delete_prompt_message(chat_id, prompt_msg_id)
            self.bot.send_message(chat_id, "Действие отменено.")
            self._safe_refresh_callback_screen(
                orig_callback,
                self.main_menu,
                lambda: self._send_html(chat_id, *self._main_content(message.from_user)),
            )
            return
        if not key:
            self._delete_prompt_message(chat_id, prompt_msg_id)
            retry_msg = self.bot.reply_to(message, "Пустой ключ. Попробуйте еще раз или отправьте 'отмена'.")
            self._set_hub_state(
                chat_id,
                getattr(retry_msg, "id", getattr(message, "id", 0)),
                user_id,
                self.STATE_AWAIT_KEY,
                {"callback": orig_callback, "prompt_msg_id": getattr(retry_msg, "id", None)},
            )
            return

        local = load_data().get("installed") or {}
        plugins = merge_local_plugins(get_plugins(), local)
        candidates = sorted(
            (p for p in plugins if p.get("prefix")),
            key=lambda p: len(str(p.get("prefix", ""))),
            reverse=True,
        )
        matched = next((p for p in candidates if key.upper().startswith(str(p["prefix"]).upper())), None)
        if not matched:
            self._delete_prompt_message(chat_id, prompt_msg_id)
            prefixes = "".join(
                f"• <code>{esc(p['prefix'])}...</code> — {esc(p.get('name', ''))}\n"
                for p in plugins
                if p.get("prefix")
            )
            retry_msg = self._reply_html(
                message,
                (f"<b>Не удалось определить плагин</b>\nПроверьте префикс ключа:\n{prefixes}"
                 if prefixes else "Каталог недоступен · повторите ввод позже или отправьте «отмена»"),
            )
            self._set_hub_state(
                chat_id,
                getattr(retry_msg, "id", getattr(message, "id", 0)),
                user_id,
                self.STATE_AWAIT_KEY,
                {"callback": orig_callback, "prompt_msg_id": getattr(retry_msg, "id", None)},
            )
            return

        self._clear_hub_state(chat_id, user_id)
        self._delete_prompt_message(chat_id, prompt_msg_id)
        plugin_name = matched["id"]
        plugin_label = str(matched.get("name", plugin_name))
        try:
            update_data(lambda d: d.setdefault("keys", {}).__setitem__(plugin_name, key))
        except OSError as exc:
            logger.exception("Failed saving key for %s", plugin_name)
            self._reply_html(message, f"<b>Ошибка сохранения ключа:</b> {esc(exc)}")
            return

        text, kb = render_key_activated(plugin_name, plugin_label, key)
        self._reply_html(message, text, reply_markup=kb)

    def _run_install_plugin(
        self,
        c: CallbackQuery,
        plugin_name: str,
        msg_obj: Any,
        action_key: str,
        *,
        background: bool = False,
        catalog: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        def _can_edit() -> bool:
            return not background or not STOP_EVENT.is_set()

        try:
            catalog = catalog if catalog is not None else get_plugins()
            plugin_info = next((x for x in catalog if x.get("id") == plugin_name), None)
            plugin_label = str(plugin_info.get("name", plugin_name)) if plugin_info else plugin_name
            is_free = bool(plugin_info.get("is_free")) if plugin_info else False

            data = load_data()
            installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
            was_installed = plugin_name in installed
            key = (data.get("keys") or {}).get(plugin_name)
            if not plugin_info and not catalog and not key:
                text, kb = render_catalog_unavailable(plugin_name)
                if _can_edit():
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                return
            if not key and not is_free:
                text, kb = render_missing_key_notice(plugin_name, plugin_label)
                if _can_edit():
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                return

            if _can_edit():
                self._edit_html(
                    f"<i>Скачиваю и устанавливаю {esc(plugin_label)}...</i>",
                    msg_obj.chat.id,
                    msg_obj.id,
                )
            success, msg = download_plugin(self.cardinal, plugin_name, key, c.from_user)
            if success:
                after_installed = load_data().get("installed")
                after_entry = after_installed.get(plugin_name) if isinstance(after_installed, dict) else None
                if not isinstance(after_entry, dict):
                    after_entry = {}
                _save_restart_marker(
                    action="update" if was_installed else "install",
                    chat_id=msg_obj.chat.id,
                    plugin_id=plugin_name,
                    plugin_name=plugin_label,
                    target_version=str(after_entry.get("version") or (plugin_info or {}).get("latest_version") or ""),
                    expected_uuid=str((plugin_info or {}).get("uuid") or ""),
                )

            text, kb = render_install_result(plugin_name, plugin_label, success, msg, is_update=was_installed)
            if _can_edit():
                self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
        except Exception:
            if not background:
                raise
            logger.exception("Async install failed for %s", plugin_name)
            if _can_edit():
                try:
                    self._edit_html(
                        "<b>Ошибка установки плагина</b>\nПовторите попытку позже или откройте /hub.",
                        msg_obj.chat.id,
                        msg_obj.id,
                    )
                except Exception:
                    logger.debug("Could not display install error", exc_info=True)
        finally:
            self._end_action(action_key)

    def install_plugin(self, c: CallbackQuery, plugin_name: Optional[str], *, background: bool = False) -> Any:
        if not plugin_name:
            return self._alert(c, "Некорректный идентификатор плагина")
        msg_obj = getattr(c, "message", None)
        if msg_obj is None:
            return self._alert(c, "Сообщение устарело. Откройте /hub заново.")

        action_key = f"install:{plugin_name}"
        if not self._try_begin_action(action_key):
            return self._alert(c, "Установка уже выполняется · подождите")

        if background:
            with CACHE_LOCK:
                cached = PLUGINS_CACHE.get("items")
                cached_list = list(cached) if isinstance(cached, list) else []
            cached_info = next((x for x in cached_list if isinstance(x, dict) and x.get("id") == plugin_name), None)
            plugin_label = str(cached_info.get("name", plugin_name)) if cached_info else plugin_name
            try:
                self._ack(c, f"Устанавливаю {plugin_label}")
            except Exception:
                logger.debug("Early callback ack failed for install:%s", plugin_name, exc_info=True)
            threading.Thread(
                target=self._run_install_plugin,
                args=(c, plugin_name, msg_obj, action_key),
                kwargs={"background": True},
                name=f"hub-install-{plugin_name}",
                daemon=True,
            ).start()
            return CB_ANSWERED

        try:
            catalog = get_plugins()
            plugin_info = next((x for x in catalog if x.get("id") == plugin_name), None)
            plugin_label = str(plugin_info.get("name", plugin_name)) if plugin_info else plugin_name
            data = load_data()
            key = (data.get("keys") or {}).get(plugin_name)
            is_free = bool(plugin_info.get("is_free")) if plugin_info else False
            if (plugin_info or catalog or key) and (key or is_free):
                try:
                    self._ack(c, f"Устанавливаю {plugin_label}")
                except Exception:
                    logger.debug("Early callback ack failed for install:%s", plugin_name, exc_info=True)
        except Exception:
            self._end_action(action_key)
            raise

        self._run_install_plugin(c, plugin_name, msg_obj, action_key, background=False, catalog=catalog)
        return CB_ANSWERED

    def trial_request(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        p = next((x for x in get_plugins() if x.get("id") == plugin_name), None)
        if not p or not plugin_name:
            return self._alert(c, "Плагин не найден")
        if p.get("is_free"):
            return self._alert(c, "Этот плагин бесплатный · пробный ключ не требуется")
        text, kb = render_trial_prompt(p, plugin_name)
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb, disable_web_page_preview=True)
        return None

    def _run_trial_confirm(self, c: CallbackQuery, plugin_name: str, msg_obj: Any, action_key: str) -> None:
        try:
            ok, result = request_trial_key(self.cardinal, plugin_name, c.from_user)
            if not ok:
                if not STOP_EVENT.is_set():
                    self._alert(c, f"{result}")
                return

            p = next((x for x in get_plugins() if x.get("id") == plugin_name), None)
            plugin_label = str(p.get("name", plugin_name)) if p else plugin_name
            try:
                update_data(lambda d: d.setdefault("keys", {}).__setitem__(plugin_name, result))
            except OSError as exc:
                logger.exception("Failed saving trial key for %s", plugin_name)
                if not STOP_EVENT.is_set():
                    self._alert(c, f"Ошибка сохранения ключа: {exc}")
                return

            text, kb = render_trial_activated(plugin_name, plugin_label, result)
            if not STOP_EVENT.is_set():
                self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
        except Exception:
            logger.exception("Trial request failed for %s", plugin_name)
        finally:
            self._end_action(action_key)

    def trial_confirm(self, c: CallbackQuery, plugin_name: Optional[str], *, background: bool = False) -> Any:
        if not plugin_name:
            return self._alert(c, "Некорректный идентификатор плагина")
        msg_obj = getattr(c, "message", None)
        if msg_obj is None:
            return self._alert(c, "Сообщение устарело. Откройте /hub заново.")

        action_key = f"trial:{plugin_name}"
        if not self._try_begin_action(action_key):
            return self._alert(c, "Запрос пробного ключа уже выполняется · подождите")

        if background:
            try:
                self._ack(c, "Запрашиваю пробный ключ…")
            except Exception:
                logger.debug("Early callback ack failed for trial:%s", plugin_name, exc_info=True)
            threading.Thread(
                target=self._run_trial_confirm,
                args=(c, plugin_name, msg_obj, action_key),
                name=f"hub-trial-{plugin_name}",
                daemon=True,
            ).start()
            return CB_ANSWERED

        self._run_trial_confirm(c, plugin_name, msg_obj, action_key)
        return None

    def delete_confirm_handler(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        if not plugin_name:
            return self._alert(c, "Плагин не установлен или уже удален")

        data = load_data()
        installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
        if plugin_name not in installed:
            return self._alert(c, "Плагин не установлен или уже удален")

        p_info = next((x for x in get_plugins() if x.get("id") == plugin_name), None) or {
            "id": plugin_name,
            "name": installed[plugin_name].get("name") or plugin_name,
        }
        state = inspect_plugin_state(self.cardinal, p_info, installed, data.get("keys"))
        text, kb = render_delete_confirm(state)
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb)
        return None

    def delete_plugin_handler(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        if not plugin_name:
            return self._alert(c, "Плагин не установлен или уже удален")

        data = load_data()
        installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
        if plugin_name not in installed:
            return self._alert(c, "Плагин не установлен или уже удален")

        p_info = next((x for x in get_plugins() if x.get("id") == plugin_name), None) or {
            "id": plugin_name,
            "name": installed[plugin_name].get("name") or plugin_name,
        }
        pre_state = inspect_plugin_state(self.cardinal, p_info, installed, data.get("keys"))

        try:
            result = delete_plugin_files(plugin_name)
        except OSError as exc:
            logger.exception("Failed deleting plugin %s", plugin_name)
            return self._alert(c, f"Ошибка удаления: {exc}")

        if not result.get("ok"):
            return self._alert(c, f"Отказ: {result.get('error')}")

        _save_restart_marker(
            action="delete",
            chat_id=c.message.chat.id,
            plugin_id=plugin_name,
            plugin_name=str(result.get("plugin_name") or plugin_name),
            expected_uuid=str(p_info.get("uuid") or pre_state.get("loaded_uuid") or ""),
            stub_path=str(result.get("stub_path") or ""),
            bin_dir=str(result.get("bin_dir") or ""),
        )

        text, kb = render_delete_result(
            result=result,
            was_loaded_in_ram=bool(pre_state.get("loaded_uuid")),
            had_storage_dir=bool(pre_state.get("storage_dir_exists")),
        )
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb)
        return None

    def open_plugin_handler(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        if not plugin_name:
            return self._alert(c, "Плагин не указан")

        p_info = next((x for x in get_plugins() if x.get("id") == plugin_name), None) or {
            "id": plugin_name,
            "name": plugin_name,
        }
        installed = load_data().get("installed") or {}
        _uid, loaded_plugin = find_loaded_plugin(self.cardinal, p_info, installed.get(plugin_name))
        if loaded_plugin is None:
            return self._alert(c, "Плагин ещё не загружен в память Cardinal. Сначала перезапустите бота.")

        ok, detail = invoke_plugin_command(self.cardinal, loaded_plugin, c.message, c.from_user)
        if not ok:
            return self._alert(c, detail)

        self._ack(c, f"Открываю /{detail}")
        return CB_ANSWERED

    def restart_cardinal_handler(self, c: CallbackQuery, arg: Optional[str]) -> Any:
        parts = (arg or "general").split(":", 1)
        action = parts[0] or "general"
        pid = parts[1] if len(parts) > 1 else ""
        msg_id = getattr(getattr(c, "message", None), "id", None)

        existing = load_data().get("pending_restart")
        if (
            isinstance(existing, dict)
            and existing.get("action") == action
            and str(existing.get("plugin_id") or "") == pid
        ):
            payload = dict(existing)
            payload["chat_id"] = c.message.chat.id
            payload["message_id"] = msg_id
            payload["created_at"] = time.time()
            try:
                save_pending_restart(payload)
            except OSError:
                logger.exception("Failed updating pending_restart before reboot")
        else:
            plugin_label = ""
            expected_uuid = ""
            target_version = ""
            if action == "hub":
                target_version = get_disk_hub_pending_version() or ""
            elif pid:
                p_info = next((x for x in get_plugins() if x.get("id") == pid), None)
                installed_map = load_data().get("installed")
                installed_entry = installed_map.get(pid) if isinstance(installed_map, dict) else None
                if not isinstance(installed_entry, dict):
                    installed_entry = {}
                plugin_label = str((p_info or {}).get("name") or installed_entry.get("name") or pid)
                expected_uuid = str((p_info or {}).get("uuid") or "")
                target_version = str(installed_entry.get("version") or (p_info or {}).get("latest_version") or "")
            _save_restart_marker(
                action=action,
                chat_id=c.message.chat.id,
                message_id=msg_id,
                plugin_id=pid,
                plugin_name=plugin_label,
                target_version=target_version,
                expected_uuid=expected_uuid,
            )

        logger.info(
            "Restarting Cardinal via Hub (action=%s pid=%s chat_id=%s msg_id=%s)",
            action,
            pid or "-",
            c.message.chat.id,
            msg_id,
        )
        self._edit_html(
            render_restart_in_progress(),
            c.message.chat.id,
            c.message.id,
        )
        self._ack(c, "Перезапуск запущен")
        trigger_cardinal_restart()
        return CB_ANSWERED

    def toggle_notify(self, c: CallbackQuery, return_to_main: bool = False) -> None:
        try:
            update_data(lambda d: d.__setitem__("notify_updates", not d.get("notify_updates", True)))
        except OSError:
            logger.exception("Failed toggling notify_updates")
            return self._alert(c, "Не удалось сохранить настройку")
        self.main_menu(c) if return_to_main else self.settings_menu(c)

    def remind_handler(self, c: CallbackQuery, pid_ver: Optional[str]) -> Any:
        if not pid_ver:
            return self._alert(c, "Некорректные данные")
        try:
            update_data(lambda d: d.setdefault("notified_updates", {}).__setitem__(pid_ver, time.time() + 86400))
        except OSError as exc:
            return self._alert(c, f"Ошибка сохранения: {exc}")

        self._alert(c, "Напоминание отложено на 24 часа")
        try:
            self.bot.delete_message(c.message.chat.id, c.message.id)
        except Exception:
            logger.debug("Could not delete reminder message", exc_info=True)
        return CB_ANSWERED

    def toggle_notify_msg_handler(self, c: CallbackQuery) -> Any:
        try:
            updated, _ = update_data(lambda d: d.__setitem__("notify_updates", not d.get("notify_updates", True)))
        except OSError as exc:
            return self._alert(c, f"Ошибка сохранения: {exc}")

        enabled = updated.get("notify_updates", True)
        self._alert(c, "Уведомления включены" if enabled else "Уведомления выключены", show_alert=False)
        rebuilt = rebuild_notification_toggle_markup(c.message.reply_markup, enabled)
        if rebuilt is not None:
            try:
                _call_tg_html(
                    lambda _text, markup: self.bot.edit_message_reply_markup(
                        c.message.chat.id, c.message.id, reply_markup=markup),
                    "", reply_markup=rebuilt, ignore_not_modified=True,
                )
            except Exception:
                logger.exception("Could not update notification button markup")
                return self._alert(c, "Настройка сохранена · откройте /hub, чтобы увидеть текущее состояние")
        return CB_ANSWERED

    def _run_hub_update(self, msg_obj: Any, action_key: str) -> None:
        try:
            self._edit_html("<i>Скачивание обновления Hub...</i>", msg_obj.chat.id, msg_obj.id)
            ok, message = download_and_update_hub()
            if ok:
                disk_ver = None
                if PATHS.hub_file and os.path.isfile(PATHS.hub_file):
                    disk_ver = extract_py_plugin_metadata(PATHS.hub_file).get("version")
                _save_restart_marker(
                    action="hub",
                    chat_id=msg_obj.chat.id,
                    message_id=msg_obj.id,
                    target_version=str(disk_ver or ""),
                )
                logger.info("Downloaded Hub update v%s; saved pending_restart marker", disk_ver or "?")
                text, kb = render_hub_update_result(str(disk_ver) if disk_ver else None)
                if not STOP_EVENT.is_set():
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                return
            text, kb = render_hub_update_error(message)
            if not STOP_EVENT.is_set():
                self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
        except Exception:
            logger.exception("Async Hub update failed")
            if not STOP_EVENT.is_set():
                try:
                    text, kb = render_hub_update_error("Неожиданная ошибка при загрузке обновления Hub.")
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                except Exception:
                    logger.debug("Could not display Hub update error", exc_info=True)
        finally:
            self._end_action(action_key)

    def hub_update_handler(self, c: CallbackQuery, *, background: bool = False) -> Any:
        msg_obj = getattr(c, "message", None)
        if msg_obj is None:
            return self._alert(c, "Сообщение устарело. Откройте /hub заново.")
        action_key = "update_hub"
        if not self._try_begin_action(action_key):
            return self._alert(c, "Обновление Hub уже выполняется · подождите")
        try:
            self._ack(c, "Загружаю обновление Hub")
        except Exception:
            logger.debug("Early callback ack failed for hub_update_handler", exc_info=True)

        if background:
            threading.Thread(
                target=self._run_hub_update,
                args=(msg_obj, action_key),
                name="hub-self-update",
                daemon=True,
            ).start()
            return CB_ANSWERED

        self._run_hub_update(msg_obj, action_key)
        return CB_ANSWERED

    def hub_command(self, message: Message) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        self._clear_hub_state(message.chat.id, self._step_user_id(message))
        chat_type = str(getattr(message.chat, "type", "") or "").lower()
        is_private_chat = (chat_type == "private") or (not chat_type and isinstance(message.chat.id, int) and message.chat.id > 0)
        if is_private_chat:
            try:
                update_data(lambda d: d.__setitem__("owner_tg_id", message.chat.id))
            except OSError:
                logger.exception("Failed saving owner_tg_id")
        text, kb = self._main_content(message.from_user)
        sent = self._send_html(message.chat.id, text, reply_markup=kb)
        msg_id = getattr(sent, "message_id", None) or getattr(sent, "id", None)
        if msg_id:
            def redraw() -> None:
                new_text, new_kb = self._main_content(message.from_user)
                if new_text != text or new_kb.to_json() != kb.to_json():
                    self._edit_html(new_text, message.chat.id, msg_id, reply_markup=new_kb)
            self._schedule_menu(message.chat.id, msg_id, message.from_user, redraw)

    def set_key_command(self, message: Message) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        self._clear_hub_state(message.chat.id, self._step_user_id(message))
        parts = (message.text or "").strip().split(maxsplit=2)
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            self._reply_html(
                message,
                "Использование: <code>!hub_key [plugin] [key]</code>\nПример: <code>!hub_key autooffline TRIAL-123</code>",
            )
            return

        raw_plugin, key = parts[1].strip(), parts[2].strip()
        catalog = get_plugins()
        p = next((x for x in catalog if str(x.get("id") or "").lower() == raw_plugin.lower()), None)
        if p is not None:
            plugin = str(p["id"])
        else:
            installed_map = load_data().get("installed")
            matched_local = (
                next((k for k in installed_map if str(k).lower() == raw_plugin.lower()), None)
                if isinstance(installed_map, dict)
                else None
            )
            plugin = str(matched_local or raw_plugin)
        try:
            update_data(lambda d: d.setdefault("keys", {}).__setitem__(plugin, key))
        except OSError as exc:
            logger.exception("Failed saving manual key for %s", plugin)
            self._reply_html(message, f"<b>Ошибка сохранения ключа:</b> {esc(exc)}")
            return

        plugin_label = str(p.get("name", plugin)) if p else plugin
        text, kb = render_key_activated(plugin, plugin_label, key)
        self._reply_html(message, text, reply_markup=kb)


def init_cardinal(cardinal: Optional[Cardinal]) -> Optional[HubController]:
    """Register Cardinal commands, callback routes, and message handlers."""
    configure_file_logging()
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None)
    if cardinal is None or tg is None or bot is None:
        logger.warning("Skipping W1bbex Hub Telegram registration: Telegram bot is disabled or unavailable")
        return None
    controller = HubController(cardinal)

    try:
        cardinal.add_telegram_commands(UUID, [("hub", "Управление W1bbex Hub", True)])
    except Exception:
        logger.exception("Не удалось зарегистрировать команды W1bbex Hub")

    exact_routes = {
        "hub:main": controller.main_menu,
        "hub:plugins_menu": lambda c: controller.plugins_menu(c, force_refresh=False),
        "hub:cancel_step": controller.cancel_step,
        "hub:activate_key": controller.activate_key_handler,
        "hub:licenses": controller.show_licenses,
        "hub:resources": controller.resources_menu,
        "hub:settings": controller.settings_menu,
        "hub:toggle_auto_check": controller.toggle_auto_check,
        "hub:check_updates": controller.check_updates_menu,
        "hub:check_interval": controller.check_interval_menu,
        "hub:custom_interval": controller.custom_interval_prompt,
        "hub:environment": controller.environment_menu,
        "hub:update_hub": controller.hub_release_menu,
        "hub:download_hub": lambda c: controller.hub_update_handler(c, background=True),
        "hub:toggle_notify": controller.toggle_notify,
        "hub:toggle_notify_main": lambda c: controller.toggle_notify(c, return_to_main=True),
        "hub:toggle_notify_from_msg": controller.toggle_notify_msg_handler,
    }
    prefix_routes = (
        ("hub:cb:", controller.dispatch_compact_callback),
        ("hub:set_interval:", controller.set_interval_callback),
        ("hub:plugin_enable:", lambda c, arg: controller.set_plugin_enabled(c, arg, True)),
        ("hub:plugin_disable:", lambda c, arg: controller.set_plugin_enabled(c, arg, False)),
        ("hub:info:", controller.plugin_info_menu),
        ("hub:description:", lambda c, arg: controller.plugin_info_menu(c, arg, view="description")),
        ("hub:diagnostics:", lambda c, arg: controller.plugin_info_menu(c, arg, view="diagnostics")),
        ("hub:catalog:", controller.catalog_page),
        ("hub:install:", lambda c, arg: controller.install_plugin(c, arg, background=True)),
        ("hub:trial:", controller.trial_request),
        ("hub:trial_confirm:", lambda c, arg: controller.trial_confirm(c, arg, background=True)),
        ("hub:delete_confirm:", controller.delete_confirm_handler),
        ("hub:delete:", controller.delete_plugin_handler),
        ("hub:remind:", controller.remind_handler),
        ("hub:restart:", controller.restart_cardinal_handler),
        ("hub:open:", controller.open_plugin_handler),
    )
    controller.set_routes(exact_routes, prefix_routes)
    try:
        for cb_name, fn in exact_routes.items():
            tg.cbq_handler(
                controller.wrap_cb(fn),
                func=lambda c, name=cb_name: isinstance(getattr(c, "data", None), str) and c.data == name,
            )
        for prefix, fn in prefix_routes:
            tg.cbq_handler(
                controller.wrap_cb(lambda c, p=prefix, h=fn: h(c, _parse_cb_arg(c.data, p))),
                func=lambda c, p=prefix: isinstance(getattr(c, "data", None), str) and c.data.startswith(p),
            )
    except Exception:
        logger.exception("Hub callback registration error")

    register_msg = getattr(tg, "msg_handler", None)
    if callable(register_msg):
        register_msg(controller.hub_command, commands=["hub"])
        register_msg(
            controller.set_key_command,
            func=lambda m: bool(getattr(m, "text", None) and m.text.startswith("!hub_key")),
        )
        register_msg(controller._handle_step_message, func=controller._is_hub_step_message)
    else:
        bot.message_handler(commands=["hub"])(controller.hub_command)
        bot.message_handler(func=lambda m: bool(getattr(m, "text", None) and m.text.startswith("!hub_key")))(
            controller.set_key_command
        )
        bot.message_handler(func=controller._is_hub_step_message)(controller._handle_step_message)
    logger.info("Initialized W1bbex Hub v%s (pre_init)", VERSION)
    return controller


def _post_restart_fallback_watcher(
    cardinal: Any,
    delay_seconds: float = POST_RESTART_FALLBACK_DELAY_SECONDS,
    post_init_event: Optional[threading.Event] = None,
    wake_event: Optional[threading.Event] = None,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    """Wait briefly for Cardinal BIND_TO_POST_INIT; send notice early if another plugin stalls startup."""
    post_ev = post_init_event or _POST_INIT_EVENT
    wake_ev = wake_event or _POST_RESTART_WAKE_EVENT
    stop_ev = stop_event or STOP_EVENT

    if delay_seconds > 0 and not post_ev.is_set() and not stop_ev.is_set():
        wake_ev.wait(timeout=delay_seconds)

    if stop_ev.is_set() or post_ev.is_set():
        return False

    try:
        pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
    except OSError:
        logger.exception("Failed checking pending_restart in fallback watcher")
        return False
    if not pending:
        return False

    logger.info(
        "BIND_TO_POST_INIT did not reach W1bbex Hub within %.1fs; dispatching post-restart notice from pre_init fallback",
        delay_seconds,
    )
    return process_post_restart_notice(cardinal)


def _arm_post_restart_fallback(cardinal: Any, stop_event: Optional[threading.Event] = None) -> bool:
    """Spawn a daemon fallback watcher when pending_restart exists at pre_init."""
    global _FALLBACK_THREAD, _FALLBACK_STOP_EVENT
    try:
        pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
    except OSError:
        logger.exception("Failed checking pending_restart on pre_init")
        return False
    if not pending:
        return False

    run_stop = stop_event or threading.Event()
    with _FALLBACK_THREAD_LOCK:
        if _FALLBACK_STOP_EVENT is not None:
            _FALLBACK_STOP_EVENT.set()
            _POST_RESTART_WAKE_EVENT.set()
        _FALLBACK_STOP_EVENT = run_stop
        if _FALLBACK_THREAD is not None and _FALLBACK_THREAD.is_alive() and not stop_event:
            return False
        thread = threading.Thread(
            target=_post_restart_fallback_watcher,
            args=(cardinal, POST_RESTART_FALLBACK_DELAY_SECONDS, _POST_INIT_EVENT, _POST_RESTART_WAKE_EVENT, run_stop),
            name="W1bbexHubPostRestartFallback",
            daemon=True,
        )
        _FALLBACK_THREAD = thread
        thread.start()
        logger.info(
            "Armed pre_init post-restart fallback watcher (action=%s pid=%s delay=%.1fs)",
            pending.get("action"),
            pending.get("plugin_id") or "-",
            POST_RESTART_FALLBACK_DELAY_SECONDS,
        )
        return True


def custom_init(cardinal: Cardinal) -> None:
    """Cardinal BIND_TO_PRE_INIT entry point."""
    global _UPDATE_THREAD, _UPDATE_STOP_EVENT, _UPDATE_WAKE_EVENT, _POST_RESTART_TIMER
    configure_file_logging()
    process_pending_deletions()
    with _POST_RESTART_LOCK:
        if _POST_RESTART_TIMER is not None:
            _POST_RESTART_TIMER.cancel()
            _POST_RESTART_TIMER = None
    _POST_INIT_EVENT.clear()
    _POST_RESTART_WAKE_EVENT.clear()
    UPDATE_WAKE_EVENT.clear()
    STOP_EVENT.clear()
    controller = init_cardinal(cardinal)
    run_stop = threading.Event()
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_STOP_EVENT is not None:
            _UPDATE_STOP_EVENT.set()
        if _UPDATE_WAKE_EVENT is not None:
            _UPDATE_WAKE_EVENT.set()
        can_reuse = _UPDATE_THREAD is not None and _UPDATE_THREAD.is_alive() and _UPDATE_STOP_EVENT is None
        if not can_reuse:
            run_wake = threading.Event()
            _UPDATE_STOP_EVENT = run_stop
            _UPDATE_WAKE_EVENT = run_wake
            thread = threading.Thread(
                target=_update_checker_loop,
                args=(cardinal, run_stop, run_wake),
                name="W1bbexHubUpdateChecker",
                daemon=True,
            )
            _UPDATE_THREAD = thread
            thread.start()
    if controller is not None:
        _arm_post_restart_fallback(cardinal, stop_event=run_stop)


def stop_hub(*_args: Any, **_kwargs: Any) -> None:
    """Cardinal BIND_TO_DELETE entry point signaling background loops to stop."""
    global _POST_RESTART_TIMER
    STOP_EVENT.set()
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_STOP_EVENT is not None:
            _UPDATE_STOP_EVENT.set()
        if _UPDATE_WAKE_EVENT is not None:
            _UPDATE_WAKE_EVENT.set()
    with _FALLBACK_THREAD_LOCK:
        if _FALLBACK_STOP_EVENT is not None:
            _FALLBACK_STOP_EVENT.set()
    with _POST_RESTART_LOCK:
        if _POST_RESTART_TIMER is not None:
            _POST_RESTART_TIMER.cancel()
            _POST_RESTART_TIMER = None
    UPDATE_WAKE_EVENT.set()
    _POST_RESTART_WAKE_EVENT.set()


def post_init_cardinal(cardinal: Cardinal) -> None:
    """Cardinal BIND_TO_POST_INIT entry point sending verified post-restart UI report."""
    global _POST_RESTART_TIMER
    configure_file_logging()
    _POST_INIT_EVENT.set()
    _POST_RESTART_WAKE_EVENT.set()
    try:
        pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
    except OSError:
        logger.exception("Failed checking pending_restart on post_init")
        return
    if not pending:
        return
    logger.info(
        "Detected pending_restart on post_init (action=%s pid=%s); scheduling report",
        pending.get("action"),
        pending.get("plugin_id") or "-",
    )

    def _run_notice() -> None:
        global _POST_RESTART_TIMER
        try:
            process_post_restart_notice(cardinal)
        finally:
            with _POST_RESTART_LOCK:
                _POST_RESTART_TIMER = None

    with _POST_RESTART_LOCK:
        if _POST_RESTART_TIMER is not None:
            _POST_RESTART_TIMER.cancel()
        timer = threading.Timer(1.0, _run_notice)
        timer.daemon = True
        _POST_RESTART_TIMER = timer
        timer.start()
