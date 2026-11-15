"""Hub navigation controllers for catalog pages, plugin cards, and settings."""

from __future__ import annotations

from typing import Any
import threading
import time

from . import hub_api
from .catalog import catalog_status
from .core import get_disk_hub_pending_version, is_newer_version
from .lifecycle import wake_update_checker
from .storage import load_data, update_data
from .release_notes import fetch_release_notes
from .runtime import VERSION, STOP_EVENT, logger
from .transport import _is_authorized
from .ui.outcomes import render_hub_release
from .ui import (
    render_check_interval, render_check_updates_error, render_check_updates_in_progress,
    render_check_updates_result, render_custom_interval_prompt, render_environment,
    render_licenses, render_main_menu, render_plugin_diagnostics, render_plugin_info,
    render_plugins_menu, render_settings,
)


def get_main_menu_content(
    cardinal: Any,
    user_id: Any,
    username: Any,
    *,
    cached_only: bool = False,
    version: tuple[str, bool] | None = None,
) -> tuple[str, Any]:
    """Render main Hub screen from current plugin states and cached/live Hub version."""
    tg_user = type("UserObj", (), {"id": user_id, "username": username})()
    latest_version, has_update = (
        version if version is not None else hub_api.fetch_hub_latest_version(cardinal, tg_user)
    )
    states, data = hub_api._build_plugin_states(cardinal, cached_only=cached_only)
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    hub_pending_ver = get_disk_hub_pending_version()
    return render_main_menu(
        plugin_states=states,
        sys_info=hub_api.get_sys_info(),
        notify_updates=bool(data.get("notify_updates", True)),
        hub_latest_version=latest_version,
        has_hub_update=has_update and not bool(hub_pending_ver),
        hub_pending_version=hub_pending_ver,
        keys_count=len(keys),
        catalog_status=catalog_status(),
    )


def _valid_interval(data: dict[str, Any]) -> int:
    minutes = data.get("update_check_interval_minutes", 30)
    return minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and 5 <= minutes <= 10080 else 30


class HubNavigation:
    """Navigation behavior layered over the existing state and transport methods."""

    STATE_AWAIT_KEY = "hub:await_key"
    STATE_AWAIT_INTERVAL = "hub:await_interval"
    HUB_STEP_STATES = frozenset({STATE_AWAIT_KEY, STATE_AWAIT_INTERVAL})

    def _get_views_lock(self) -> threading.Lock:
        lock = vars(self).get("_views_lock")
        if lock is None:
            lock = threading.Lock()
            vars(self)["_views_lock"] = lock
        return lock

    def _set_catalog_view(self, chat_id: int, message_id: int, view: str) -> None:
        with self._get_views_lock():
            views = vars(self).setdefault("_catalog_views", {})
            views[(chat_id, message_id)] = view
            if len(views) > 128:
                views.pop(next(iter(views)))

    def _get_catalog_view(self, chat_id: int, message_id: int) -> str:
        with self._get_views_lock():
            return vars(self).get("_catalog_views", {}).get((chat_id, message_id), "all")

    def _try_begin_action(self, key: str) -> bool:
        lock = vars(self).get("_actions_lock")
        if lock is None:
            lock = threading.Lock()
            vars(self)["_actions_lock"] = lock
        in_flight = vars(self).setdefault("_in_flight_actions", set())
        with lock:
            if key in in_flight:
                return False
            in_flight.add(key)
            return True

    def _is_action_in_flight(self, key: str) -> bool:
        lock = vars(self).get("_actions_lock")
        in_flight = vars(self).get("_in_flight_actions")
        if lock is None or in_flight is None:
            return False
        with lock:
            return key in in_flight

    def _end_action(self, key: str) -> None:
        lock = vars(self).get("_actions_lock")
        in_flight = vars(self).get("_in_flight_actions")
        if lock is not None and isinstance(in_flight, set):
            with lock:
                in_flight.discard(key)

    def _safe_refresh_callback_screen(self, callback: Any, edit_fn: Any, fallback_fn: Any) -> None:
        try:
            edit_fn(callback)
        except Exception:
            logger.debug("Original callback message unavailable; sending fresh menu", exc_info=True)
            fallback_fn()

    @staticmethod
    def _step_user_id(event: Any) -> int:
        user = getattr(event, "from_user", None)
        uid = getattr(user, "id", None)
        if uid is not None:
            return int(uid)
        msg = getattr(event, "message", event)
        chat = getattr(msg, "chat", None)
        return int(getattr(chat, "id", 0))

    def _set_hub_state(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        state: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        set_state = getattr(self.tg, "set_state", None)
        if callable(set_state):
            set_state(chat_id, message_id, user_id, state, data or {})
        else:
            vars(self).setdefault("_hub_states", {})[(chat_id, user_id)] = {
                "state": state,
                "mid": message_id,
                "data": data or {},
            }

    def _get_hub_state(self, chat_id: int, user_id: int) -> dict[str, Any] | None:
        get_state = getattr(self.tg, "get_state", None)
        if callable(get_state):
            entry = get_state(chat_id, user_id)
        else:
            entry = vars(self).get("_hub_states", {}).get((chat_id, user_id))
        if isinstance(entry, dict) and entry.get("state") in self.HUB_STEP_STATES:
            return entry
        return None

    def _clear_hub_state(self, chat_id: int, user_id: int) -> None:
        if self._get_hub_state(chat_id, user_id) is None:
            return
        clear_state = getattr(self.tg, "clear_state", None)
        if callable(clear_state):
            clear_state(chat_id, user_id, False)
        else:
            vars(self).get("_hub_states", {}).pop((chat_id, user_id), None)

    def hub_release_menu(self, callback: Any) -> None:
        chat, message = callback.message.chat.id, callback.message.id
        background = self._menu_background
        with background.lock:
            background.serial += 1
            serial = background.serial
            background.views[(chat, message)] = serial
            cached_ver, _ = background.version if background.version else (None, False)
        text, keyboard = render_hub_release(VERSION, cached_ver if cached_ver else None, None, loading=True)
        self._edit_html(text, chat, message, reply_markup=keyboard)

        def refresh() -> None:
            latest, notes = fetch_release_notes()
            resolved_latest = latest or cached_ver
            body, markup = render_hub_release(VERSION, resolved_latest, notes)
            with background.lock:
                should_edit = not STOP_EVENT.is_set() and background.views.get((chat, message)) == serial
            if should_edit:
                try:
                    self._edit_html(body, chat, message, reply_markup=markup)
                except Exception:
                    logger.exception("Failed refreshing Hub release details")

        threading.Thread(target=refresh, name="hub-release-details", daemon=True).start()

    def _schedule_menu(self, chat: int, message: int, user: Any, draw: Any, catalog_requested: bool = False) -> None:
        data = load_data()
        if not data.get("auto_check_updates", True) and not catalog_requested:
            return
        minutes = _valid_interval(data)
        catalog_ok = {"fresh": True}
        def refresh() -> tuple[str, bool]:
            auto_check = bool(load_data().get("auto_check_updates", True))
            version = hub_api.fetch_hub_latest_version(self.cardinal, user) if auto_check else self._menu_background.version
            if auto_check or catalog_requested:
                plugins = hub_api.get_plugins()
                status = catalog_status()
                catalog_ok["fresh"] = bool(status.get("available")) or bool(plugins)
            return version
        self._menu_background.schedule(
            chat,
            message,
            refresh,
            draw,
            ttl=180 if catalog_requested else minutes * 60,
            is_fresh=lambda: catalog_ok["fresh"],
        )

    def _main_content(self, user: Any) -> Any:
        return get_main_menu_content(self.cardinal, user.id, getattr(user, "username", None),
                                     cached_only=True, version=self._menu_background.version)

    def plugins_menu(self, callback: Any, force_refresh: bool = False,
                     page: int = 0, view: str = "all") -> Any:
        return self._render_plugins_menu(callback, force_refresh=force_refresh, page=page, view=view)

    def _render_plugins_menu(self, callback: Any, force_refresh: bool = False,
                             page: int = 0, view: str = "all") -> Any:
        states, _ = self._states(force_refresh=force_refresh, cached_only=not force_refresh)
        status = catalog_status()
        text, markup = render_plugins_menu(states, page, view, status)
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)
        self._set_catalog_view(callback.message.chat.id, callback.message.id, view)
        if force_refresh:
            notice = "Каталог обновлён" if status["available"] else "Не удалось обновить каталог"
            return self._alert(callback, notice, show_alert=False)
        def redraw() -> None:
            current, _ = self._states(cached_only=True)
            body, keyboard = render_plugins_menu(current, page, view, catalog_status())
            if body != text or keyboard.to_json() != markup.to_json():
                self._edit_html(body, callback.message.chat.id, callback.message.id, reply_markup=keyboard)
        if view == "all" or catalog_status().get("loading") or not catalog_status().get("available", True):
            self._schedule_menu(callback.message.chat.id, callback.message.id, callback.from_user, redraw, catalog_requested=True)
        else:
            self._schedule_menu(callback.message.chat.id, callback.message.id, callback.from_user, redraw)

    def catalog_page(self, callback: Any, arg: str | None, refresh: bool = False) -> Any:
        view, _, raw_page = (arg or "all:0").partition(":")
        page = int(raw_page) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) < 8 else 0
        return self.plugins_menu(callback, force_refresh=refresh, page=page, view=view)

    def plugin_info_menu(self, callback: Any, plugin_name: str | None,
                         view: str = "info") -> Any:
        states, _ = self._states(cached_only=True)
        state = next((item for item in states if item["id"] == plugin_name), None)
        if state is None:
            return self._alert(callback, "Плагин не найден · обновите каталог")
        if view == "diagnostics":
            text, markup = render_plugin_diagnostics(state)
        else:
            previous = self._get_catalog_view(callback.message.chat.id, callback.message.id)
            text, markup = render_plugin_info(state, expanded=view == "description", catalog_view=previous)
        self._edit_html(text, callback.message.chat.id, callback.message.id,
                        reply_markup=markup, disable_web_page_preview=True)

    def set_plugin_enabled(self, callback: Any, plugin_name: str | None, enabled: bool) -> Any:
        states, _ = self._states(cached_only=True)
        state = next((item for item in states if item["id"] == plugin_name), None)
        uid = state.get("loaded_uuid") if state else None
        plugins = getattr(self.cardinal, "plugins", {})
        plugin = plugins.get(uid) if isinstance(plugins, dict) else None
        if not plugin or state.get("needs_restart"):
            return self._alert(callback, "Плагин недоступен · обновите карточку")
        if bool(plugin.enabled) != enabled:
            self.cardinal.toggle_plugin(uid)
        return self.plugin_info_menu(callback, plugin_name)

    def show_licenses(self, callback: Any) -> None:
        states, data = self._states(cached_only=True)
        names = {state["id"]: state["name"] for state in states}
        text, markup = render_licenses(data.get("keys") or {}, names)
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)

    def settings_menu(self, callback: Any) -> None:
        data = load_data()
        interval = _valid_interval(data)
        text, markup = render_settings(bool(data.get("notify_updates", True)), hub_api.get_sys_info(), bool(data.get("auto_check_updates", True)), interval)
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)

    def toggle_auto_check(self, callback: Any) -> Any:
        try:
            update_data(lambda data: data.__setitem__("auto_check_updates", not data.get("auto_check_updates", True)))
        except OSError:
            logger.exception("Failed saving automatic update check setting")
            return self._alert(callback, "Не удалось сохранить настройку")
        wake_update_checker()
        return self.settings_menu(callback)

    @staticmethod
    def _build_check_interval_card(data: dict[str, Any]) -> tuple[str, Any]:
        enabled = bool(data.get("auto_check_updates", True))
        interval = _valid_interval(data)
        return render_check_interval(enabled, interval)

    def check_interval_menu(self, callback: Any) -> None:
        text, markup = self._build_check_interval_card(load_data())
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)

    def _save_interval(self, minutes: int) -> None:
        def save(data: dict[str, Any]) -> None:
            data["auto_check_updates"] = minutes != 0
            if minutes:
                data["update_check_interval_minutes"] = minutes
        update_data(save)
        wake_update_checker()

    def set_interval_callback(self, callback: Any, raw: str | None) -> Any:
        if not raw or not raw.isascii() or not raw.isdigit() or len(raw) > 5:
            return self._alert(callback, "Неверный интервал")
        minutes = int(raw)
        if minutes != 0 and not 5 <= minutes <= 10080:
            return self._alert(callback, "Укажите от 5 до 10 080 минут")
        try:
            self._save_interval(minutes)
        except OSError:
            logger.exception("Failed saving update interval")
            return self._alert(callback, "Не удалось сохранить настройку")
        return self.check_interval_menu(callback)

    def custom_interval_prompt(self, callback: Any) -> None:
        text, prompt_kb = render_custom_interval_prompt()
        msg = self._send_html(
            callback.message.chat.id,
            text,
            reply_markup=prompt_kb,
        )
        self._set_hub_state(
            callback.message.chat.id,
            getattr(msg, "id", getattr(callback.message, "id", 0)),
            self._step_user_id(callback),
            self.STATE_AWAIT_INTERVAL,
            {"callback": callback, "prompt_msg_id": getattr(msg, "id", None)},
        )

    def _refresh_check_interval_after_step(self, chat_id: int, callback: Any) -> None:
        def _send_fresh() -> None:
            text, markup = self._build_check_interval_card(load_data())
            self._send_html(chat_id, text, reply_markup=markup)

        self._safe_refresh_callback_screen(callback, self.check_interval_menu, _send_fresh)

    def _process_interval_input(self, message: Any, callback: Any, *, prompt_msg_id: Any = None) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        chat_id = message.chat.id
        user_id = self._step_user_id(message)
        raw = str(getattr(message, "text", "") or "").strip()
        if raw.startswith("/"):
            self._clear_hub_state(chat_id, user_id)
            if prompt_msg_id:
                try:
                    self.bot.delete_message(chat_id, prompt_msg_id)
                except Exception:
                    logger.debug("Could not delete interval prompt message %s", prompt_msg_id, exc_info=True)
            if raw.split()[0].split("@")[0].lower() == "/hub":
                self.hub_command(message)
            else:
                self.bot.send_message(chat_id, "Ввод интервала отменён · отправьте команду ещё раз")
            return
        if raw.lower() == "отмена":
            self._clear_hub_state(chat_id, user_id)
            if prompt_msg_id:
                try:
                    self.bot.delete_message(chat_id, prompt_msg_id)
                except Exception:
                    logger.debug("Could not delete interval prompt message %s", prompt_msg_id, exc_info=True)
            self.bot.send_message(chat_id, "Ввод интервала отменён")
            self._refresh_check_interval_after_step(chat_id, callback)
            return
        if not raw.isascii() or not raw.isdigit() or len(raw) > 5 or not 5 <= int(raw) <= 10080:
            retry = self.bot.reply_to(message, "Введите целое число от 5 до 10 080 или отправьте «отмена».")
            self._set_hub_state(
                chat_id,
                getattr(retry, "id", getattr(message, "id", 0)),
                user_id,
                self.STATE_AWAIT_INTERVAL,
                {"callback": callback, "prompt_msg_id": getattr(retry, "id", prompt_msg_id)},
            )
            return
        self._clear_hub_state(chat_id, user_id)
        try:
            self._save_interval(int(raw))
        except OSError:
            logger.exception("Failed saving custom update interval")
            self.bot.reply_to(message, "Не удалось сохранить настройку. Попробуйте ещё раз.")
            return
        if prompt_msg_id:
            try:
                self.bot.delete_message(chat_id, prompt_msg_id)
            except Exception:
                logger.debug("Could not delete interval prompt message %s", prompt_msg_id, exc_info=True)
        self.bot.reply_to(message, "Интервал проверки сохранён")
        self._refresh_check_interval_after_step(chat_id, callback)

    def check_updates_menu(self, callback: Any) -> Any:
        chat, message = callback.message.chat.id, callback.message.id
        action_key = f"check_updates:{chat}:{message}"
        if not self._try_begin_action(action_key):
            return self._alert(callback, "Проверка обновлений уже выполняется")
        try:
            background = self._menu_background
            with background.lock:
                background.serial += 1
                serial = background.serial
                background.views[(chat, message)] = serial
            card_text, card_markup = render_check_updates_in_progress()
            self._edit_html(card_text, chat, message, reply_markup=card_markup)

            def check() -> None:
                try:
                    latest, _ = fetch_release_notes()
                    states, _ = self._states(force_refresh=True)
                    available = catalog_status()["available"]
                    newer = bool(latest and is_newer_version(latest, VERSION))
                    updates = [state for state in states if state.get("has_update") and not state.get("needs_restart")] if available else []
                    body, markup = render_check_updates_result(latest, newer, available, updates)
                    if latest:
                        with background.lock:
                            background.version = (latest, newer)
                            background.checked = time.monotonic()
                except Exception:
                    logger.exception("Manual update check failed")
                    body, markup = render_check_updates_error()
                finally:
                    self._end_action(action_key)
                with background.lock:
                    should_edit = not STOP_EVENT.is_set() and background.views.get((chat, message)) == serial
                if should_edit:
                    try:
                        self._edit_html(body, chat, message, reply_markup=markup)
                    except Exception:
                        logger.exception("Failed displaying manual update check")

            threading.Thread(target=check, name="hub-manual-update-check", daemon=True).start()
        except Exception:
            self._end_action(action_key)
            raise

    def environment_menu(self, callback: Any) -> None:
        text, markup = render_environment(hub_api.get_sys_info())
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)
