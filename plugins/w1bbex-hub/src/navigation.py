"""Hub navigation controllers for catalog pages, plugin cards, and settings."""

from __future__ import annotations

from typing import Any
import threading
import time

from .catalog import catalog_status
from .core import get_sys_info, is_newer_version
from .storage import load_data, update_data
from .release_notes import fetch_release_notes
from .runtime import VERSION, STOP_EVENT, UPDATE_WAKE_EVENT, logger
from .ui.outcomes import render_hub_release
from .ui.kit import Btn, CardBuilder, KeyboardBuilder, esc
from .ui.icons import tg_icon
from .ui import (
    render_environment, render_licenses, render_plugin_diagnostics,
    render_plugin_info, render_plugins_menu, render_settings,
)


class HubNavigation:
    """Navigation behavior layered over the existing state and transport methods."""

    STATE_AWAIT_KEY = "hub:await_key"
    STATE_AWAIT_INTERVAL = "hub:await_interval"
    HUB_STEP_STATES = frozenset({STATE_AWAIT_KEY, STATE_AWAIT_INTERVAL})

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
        text, keyboard = render_hub_release(VERSION, None, None, loading=True)
        self._edit_html(text, chat, message, reply_markup=keyboard)

        def refresh() -> None:
            latest, notes = fetch_release_notes()
            body, markup = render_hub_release(VERSION, latest, notes)
            with background.lock:
                should_edit = not STOP_EVENT.is_set() and background.views.get((chat, message)) == serial
            if should_edit:
                try:
                    self._edit_html(body, chat, message, reply_markup=markup)
                except Exception:
                    logger.exception("Failed refreshing Hub release details")

        threading.Thread(target=refresh, name="hub-release-details", daemon=True).start()

    def _schedule_menu(self, chat: int, message: int, user: Any, draw: Any, catalog_requested: bool = False) -> None:
        from . import handlers
        data = load_data()
        if not data.get("auto_check_updates", True) and not catalog_requested:
            return
        minutes = data.get("update_check_interval_minutes", 30)
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 10080:
            minutes = 30
        catalog_ok = {"fresh": True}
        def refresh() -> tuple[str, bool]:
            auto_check = bool(load_data().get("auto_check_updates", True))
            version = handlers.fetch_hub_latest_version(self.cardinal, user) if auto_check else self._menu_background.version
            if auto_check or catalog_requested:
                plugins = handlers.get_plugins()
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
        from . import handlers
        return handlers.get_main_menu_content(self.cardinal, user.id, getattr(user, "username", None),
                                              cached_only=True, version=self._menu_background.version)

    def plugins_menu(self, callback: Any, force_refresh: bool = False,
                     page: int = 0, view: str = "all") -> Any:
        states, _ = self._states(force_refresh=force_refresh, cached_only=not force_refresh)
        status = catalog_status()
        text, markup = render_plugins_menu(states, page, view, status)
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)
        self._catalog_views[(callback.message.chat.id, callback.message.id)] = view
        if len(self._catalog_views) > 128:
            self._catalog_views.pop(next(iter(self._catalog_views)))
        if force_refresh:
            notice = "Каталог обновлён" if status["available"] else "Не удалось обновить каталог"
            return self._alert(callback, notice, show_alert=False)
        def redraw() -> None:
            current, _ = self._states(cached_only=True)
            body, keyboard = render_plugins_menu(current, page, view, catalog_status())
            if body != text or keyboard.to_json() != markup.to_json():
                self._edit_html(body, callback.message.chat.id, callback.message.id, reply_markup=keyboard)
        if not load_data().get("auto_check_updates", True) and (view == "all" or catalog_status().get("loading")):
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
            previous = self._catalog_views.get((callback.message.chat.id, callback.message.id), "all")
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
        interval = data.get("update_check_interval_minutes", 30)
        if not isinstance(interval, int) or isinstance(interval, bool) or not 5 <= interval <= 10080:
            interval = 30
        text, markup = render_settings(bool(data.get("notify_updates", True)), get_sys_info(), bool(data.get("auto_check_updates", True)), interval)
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)

    def toggle_auto_check(self, callback: Any) -> Any:
        from . import handlers
        try:
            update_data(lambda data: data.__setitem__("auto_check_updates", not data.get("auto_check_updates", True)))
        except OSError:
            logger.exception("Failed saving automatic update check setting")
            return self._alert(callback, "Не удалось сохранить настройку")
        handlers.wake_update_checker()
        return self.settings_menu(callback)

    def check_interval_menu(self, callback: Any) -> None:
        data = load_data()
        enabled = bool(data.get("auto_check_updates", True))
        interval = data.get("update_check_interval_minutes", 30)
        card = CardBuilder(tg_icon("refresh"), "Частота проверки обновлений")
        card.paragraph(f"Сейчас: каждые {esc(interval)} мин" if enabled else "Сейчас: не проверять автоматически")
        card.paragraph("Выберите интервал или укажите свой: от 5 до 10 080 минут")
        kb = KeyboardBuilder()
        for options in ((30, 60), (360, 1440)):
            labels = {30: "30 минут", 60: "1 час", 360: "6 часов", 1440: "24 часа"}
            kb.row(*(Btn(labels[value], f"hub:set_interval:{value}", style="success" if enabled and interval == value else "primary") for value in options))
        kb.row(Btn("Свой интервал", "hub:custom_interval", icon="settings"))
        kb.row(Btn("Не проверять", "hub:set_interval:0", style="danger"))
        kb.row(Btn("Назад", "hub:settings", icon="back"))
        self._edit_html(card.build(), callback.message.chat.id, callback.message.id, reply_markup=kb.build())

    def _save_interval(self, minutes: int) -> None:
        from . import handlers
        def save(data: dict[str, Any]) -> None:
            data["auto_check_updates"] = minutes != 0
            if minutes:
                data["update_check_interval_minutes"] = minutes
        update_data(save)
        handlers.wake_update_checker()

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
        msg = self._send_html(callback.message.chat.id, "Введите интервал проверки в минутах: от <b>5</b> до <b>10 080</b>. Для отмены отправьте «отмена».")
        self._set_hub_state(
            callback.message.chat.id,
            getattr(msg, "id", getattr(callback.message, "id", 0)),
            self._step_user_id(callback),
            self.STATE_AWAIT_INTERVAL,
            {"callback": callback},
        )

    def _process_interval_input(self, message: Any, callback: Any) -> None:
        from . import handlers
        if not handlers._is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        chat_id = message.chat.id
        user_id = self._step_user_id(message)
        raw = str(getattr(message, "text", "") or "").strip()
        if raw.startswith("/"):
            self._clear_hub_state(chat_id, user_id)
            if raw.split()[0].split("@")[0].lower() == "/hub":
                self.hub_command(message)
            else:
                self.bot.send_message(chat_id, "Ввод интервала отменён · отправьте команду ещё раз")
            return
        if raw.lower() == "отмена":
            self._clear_hub_state(chat_id, user_id)
            self.bot.send_message(chat_id, "Ввод интервала отменён")
            self.check_interval_menu(callback)
            return
        if not raw.isascii() or not raw.isdigit() or len(raw) > 5 or not 5 <= int(raw) <= 10080:
            retry = self.bot.reply_to(message, "Введите целое число от 5 до 10 080 или отправьте «отмена».")
            self._set_hub_state(
                chat_id,
                getattr(retry, "id", getattr(message, "id", 0)),
                user_id,
                self.STATE_AWAIT_INTERVAL,
                {"callback": callback},
            )
            return
        self._clear_hub_state(chat_id, user_id)
        try:
            self._save_interval(int(raw))
        except OSError:
            logger.exception("Failed saving custom update interval")
            self.bot.reply_to(message, "Не удалось сохранить настройку. Попробуйте ещё раз.")
            return
        self.bot.reply_to(message, "Интервал проверки сохранён")
        self.check_interval_menu(callback)

    def check_updates_menu(self, callback: Any) -> None:
        chat, message = callback.message.chat.id, callback.message.id
        background = self._menu_background
        with background.lock:
            background.serial += 1
            serial = background.serial
            background.views[(chat, message)] = serial
        card = CardBuilder(tg_icon("refresh"), "Проверка обновлений")
        card.paragraph("Проверяем Hub и установленные плагины…")
        self._edit_html(card.build(), chat, message, reply_markup=KeyboardBuilder().row(Btn("Назад", "hub:settings", icon="back")).build())

        def check() -> None:
            try:
                latest, _ = fetch_release_notes()
                states, _ = self._states(force_refresh=True)
                available = catalog_status()["available"]
                newer = bool(latest and is_newer_version(latest, VERSION))
                updates = [state for state in states if state.get("has_update") and not state.get("needs_restart")] if available else []
                result = CardBuilder(tg_icon("refresh"), "Проверка обновлений")
                result.paragraph("Hub: " + (f"доступна версия <b>{esc(latest)}</b>" if newer else "последняя версия установлена" if latest else "не удалось проверить"))
                if not available:
                    result.paragraph("Плагины: не удалось проверить каталог")
                elif updates:
                    result.paragraph("Доступны обновления плагинов:\n" + "\n".join(f"• {esc(s['name'])}: {esc(s['latest_version'])}" for s in updates[:20]))
                else:
                    result.paragraph("Плагины: обновлений не найдено")
                kb = KeyboardBuilder()
                if newer:
                    kb.row(Btn("Что нового в Hub", "hub:update_hub", style="primary", icon="info"))
                if updates:
                    kb.row(Btn("Обновления плагинов", "hub:catalog:attention:0", style="primary", icon="apps"))
                kb.row(Btn("Назад", "hub:settings", style="danger", icon="back"))
                if latest:
                    with background.lock:
                        background.version = (latest, newer)
                        background.checked = time.monotonic()
                body, markup = result.build(), kb.build()
            except Exception:
                logger.exception("Manual update check failed")
                result = CardBuilder(tg_icon("alert"), "Не удалось проверить обновления")
                result.paragraph("Повторите попытку позже")
                body, markup = result.build(), KeyboardBuilder().row(Btn("Назад", "hub:settings", icon="back")).build()
            with background.lock:
                should_edit = not STOP_EVENT.is_set() and background.views.get((chat, message)) == serial
            if should_edit:
                try:
                    self._edit_html(body, chat, message, reply_markup=markup)
                except Exception:
                    logger.exception("Failed displaying manual update check")

        threading.Thread(target=check, name="hub-manual-update-check", daemon=True).start()

    def environment_menu(self, callback: Any) -> None:
        text, markup = render_environment(get_sys_info())
        self._edit_html(text, callback.message.chat.id, callback.message.id, reply_markup=markup)
