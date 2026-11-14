"""Hub Telegram controller wiring, routing table, and Cardinal command registration."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from telebot.types import CallbackQuery

from . import hub_api, lifecycle
from .hub_actions import HubActions
from .navigation import HubNavigation
from .runtime import CB_ANSWERED, UUID, VERSION, configure_file_logging, logger
from .storage import update_data
from .transport import TelegramTransport, _call_tg_html, _is_authorized
from .ui import rebuild_notification_toggle_markup, render_resources, resolve_callback_data

if TYPE_CHECKING:
    from cardinal import Cardinal


def _parse_cb_arg(data: Optional[str], prefix: str) -> Optional[str]:
    if not data or not data.startswith(prefix):
        return None
    return data[len(prefix):] or None


class HubController(HubNavigation, HubActions, TelegramTransport):
    """Handles Telegram commands, callback queries, and step flows for W1bbex Hub."""

    def __init__(self, cardinal: Any) -> None:
        self.cardinal = cardinal
        self.tg = cardinal.telegram
        self.bot = self.tg.bot
        self._acked_callbacks: set[str] = set()
        from .menu_background import MenuBackground
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
        return hub_api._build_plugin_states(self.cardinal, force_refresh=force_refresh, cached_only=cached_only)

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


def custom_init(cardinal: Cardinal) -> None:
    """Cardinal BIND_TO_PRE_INIT entry point."""
    lifecycle.prepare_lifecycle_for_init()
    controller = init_cardinal(cardinal)
    lifecycle.start_lifecycle_workers(cardinal, arm_fallback=controller is not None)
