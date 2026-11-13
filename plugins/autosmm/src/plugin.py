"""AutoSMM plugin entrypoint: Cardinal hooks, FunPay event routing, and TG command registration."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any, Callable, List, Tuple

if TYPE_CHECKING:
    from cardinal import Cardinal

from telebot import types

from . import runtime as rt
from .core.buyer_flow import _recover_buyer_flow_runtime
from .core.funpay_events import auto_smm_handler
from .core.orders import start_order_checking
from .services.telemetry import _telemetry_gate_allows_menu
from .storage.storage import (
    CONFIG_LOCK,
    load_config,
    migrate_autosmm_files_from_legacy_if_needed,
)
from .tg.analytics import (
    analytics_less,
    analytics_more,
    analytics_period,
    show_orders,
)
from .tg.common import (
    STATE_RESTORE_BACKUP,
    h,
    header_no_action_callback,
    ui_input_back_callback,
    ui_input_cancel_callback,
)
from .tg.files import (
    _send_log_file_async,
    autosmm_cleanup_menu,
    export_errors,
    files_menu_callback,
    full_autosmm_delete_confirm,
    full_autosmm_delete_prompt,
    full_backup_confirm,
    full_backup_prompt,
    handle_backup_restore_upload,
    restore_backup_prompt,
)
from .tg.lots import (
    add_new_lot,
    change_fallback_id_prompt,
    change_fallback_snum_prompt,
    change_id,
    change_name,
    change_quantity,
    change_snum,
    confirm_delete_one_lot_callback,
    cycle_link_fmt_callback,
    delete_one_lot_callback,
    edit_lot_callback,
    edit_lot_from_search,
    lot_input_back_callback,
    lot_settings,
    page_navigation,
    return_to_lots,
    search_lot_prompt,
    search_page_navigation,
    search_results_back,
    show_lots_list,
)
from .tg.panels import (
    add_service,
    api_service_callback,
    api_settings_callback,
    check_balance,
    delete_service,
    edit_apiname,
    edit_apinum,
    edit_apikey,
    edit_apiurl,
    refresh_api_balances_callback,
)
from .tg.settings import (
    add_website_prompt,
    allowed_websites_page_callback,
    auto_smm_settings,
    buyer_flow_settings,
    confirm_delete_website_callback,
    disable_notifications,
    enable_auto_refunds,
    enable_buyer_cancel_before_panel,
    links_menu_callback,
    manage_websites,
    misc_settings_callback,
    remove_website_prompt,
    reset_valid_links_confirm,
    reset_valid_links_prompt,
    return_to_settings,
    seller_notifications_settings,
    send_test_notification,
    set_notification_chat_id,
    show_allowed_websites_list,
    toggle_auto_refunds,
    toggle_buyer_cancel_before_panel,
    toggle_confirm_link,
    toggle_fallback,
    use_current_chat_for_notifications,
)
from .tg.telemetry import (
    _show_telemetry_gate,
    telemetry_accept_callback,
    telemetry_guest_7d_callback,
    telemetry_refuse_callback,
)
from .tg.templates import (
    edit_messages_menu,
    edit_msg_completion,
    edit_msg_confirmation,
    edit_msg_link_confirm,
    edit_msg_payment,
    reset_message_texts_confirm,
    reset_message_texts_prompt,
    template_edit_callback,
    template_reset_confirm_callback,
    template_reset_prompt_callback,
    template_view_callback,
)

# =========================================================
# META / BOOTSTRAP
# =========================================================

NAME = rt.NAME
VERSION = rt.VERSION
DESCRIPTION = rt.DESCRIPTION
CREDITS = rt.CREDITS
UUID = rt.UUID
SETTINGS_PAGE = rt.SETTINGS_PAGE

LOGGER_PREFIX = rt.LOGGER_PREFIX
logger = logging.getLogger("FPC.autosmm")

rt.configure_paths(Path(__file__).resolve())
CARDINAL_ROOT = rt.CARDINAL_ROOT


def _bootstrap_runtime() -> None:
    """Create runtime storage dirs, run one-time legacy migrations, and attach file logger."""
    rt.ensure_storage_dirs()
    migrate_autosmm_files_from_legacy_if_needed()
    rt.init_logging()
    rt.prune_stale_contexts()


# =========================================================
# TELEGRAM INIT / BACKGROUND WORKERS
# =========================================================


def send_chat_id(message: types.Message) -> None:
    try:
        rt.bot.send_message(
            message.chat.id,
            f"🆔 <b>Chat ID</b>\n\n"
            f"Текущий chat_id:\n"
            f"<code>{h(message.chat.id)}</code>\n\n"
            f"<i>Используйте его в настройках уведомлений AutoSMM.</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning(f"CHATID COMMAND FAILED: {exc}")


def _start_background_threads(c_: Cardinal) -> None:
    if not rt.ORDER_CHECK_THREAD or not rt.ORDER_CHECK_THREAD.is_alive():
        rt.ORDER_CHECK_THREAD = threading.Thread(target=start_order_checking, args=(c_,), daemon=True)
        rt.ORDER_CHECK_THREAD.start()


_EXACT_CALLBACK_ROUTES: List[Tuple[str, Callable[[types.CallbackQuery], Any]]] = [
    ("manage_websites", manage_websites),
    ("show_allowed_websites_list", show_allowed_websites_list),
    ("add_website", add_website_prompt),
    ("reset_valid_links_prompt", reset_valid_links_prompt),
    ("reset_valid_links_confirm", reset_valid_links_confirm),
    ("lot_settings", lot_settings),
    ("show_lots_list", show_lots_list),
    ("search_lot", search_lot_prompt),
    ("search_results_back", search_results_back),
    ("lot_input_back", lot_input_back_callback),
    ("ui_input_back", ui_input_back_callback),
    ("ui_input_cancel", ui_input_cancel_callback),
    ("show_orders", show_orders),
    ("export_errors", export_errors),
    ("toggle_auto_refunds", toggle_auto_refunds),
    ("enable_auto_refunds", enable_auto_refunds),
    ("toggle_confirm_link", toggle_confirm_link),
    ("toggle_buyer_cancel_before_panel", toggle_buyer_cancel_before_panel),
    ("enable_buyer_cancel_before_panel", enable_buyer_cancel_before_panel),
    ("return_to_settings", return_to_settings),
    ("api_settings", api_settings_callback),
    ("refresh_api_balances", refresh_api_balances_callback),
    ("header_no_action", header_no_action_callback),
    ("add_new_lot", add_new_lot),
    ("edit_messages", edit_messages_menu),
    ("edit_msg_payment", edit_msg_payment),
    ("edit_msg_confirmation", edit_msg_confirmation),
    ("add_service", add_service),
    ("delete_service", delete_service),
    ("set_notification_chat_id", set_notification_chat_id),
    ("files_menu", files_menu_callback),
    ("misc_settings", misc_settings_callback),
    ("toggle_fallback", toggle_fallback),
    ("links_menu", links_menu_callback),
    ("autosmm_telemetry_accept", telemetry_accept_callback),
    ("autosmm_telemetry_refuse", telemetry_refuse_callback),
    ("autosmm_telemetry_guest_7d", telemetry_guest_7d_callback),
    ("buyer_flow_settings", buyer_flow_settings),
    ("seller_notifications_settings", seller_notifications_settings),
    ("use_current_chat_for_notifications", use_current_chat_for_notifications),
    ("send_test_notification", send_test_notification),
    ("disable_notifications", disable_notifications),
    ("full_backup_prompt", full_backup_prompt),
    ("full_backup_confirm", full_backup_confirm),
    ("restore_backup_prompt", restore_backup_prompt),
    ("autosmm_cleanup_menu", autosmm_cleanup_menu),
    ("full_autosmm_delete_prompt", full_autosmm_delete_prompt),
    ("full_autosmm_delete_confirm", full_autosmm_delete_confirm),
    ("edit_msg_link_confirm", edit_msg_link_confirm),
    ("edit_msg_completion", edit_msg_completion),
    ("reset_message_texts_prompt", reset_message_texts_prompt),
    ("reset_message_texts_confirm", reset_message_texts_confirm),
]

_PREFIX_CALLBACK_ROUTES: List[Tuple[str, Callable[[types.CallbackQuery], Any]]] = [
    ("change_fallback_snum_", change_fallback_snum_prompt),
    ("change_fallback_id_", change_fallback_id_prompt),
    ("delete_website_", remove_website_prompt),
    ("confirm_delete_website_", confirm_delete_website_callback),
    ("sites_page_", allowed_websites_page_callback),
    ("edit_lot_search_", edit_lot_from_search),
    ("search_page_", search_page_navigation),
    ("analytics_period_", analytics_period),
    ("analytics_more_", analytics_more),
    ("analytics_less_", analytics_less),
    ("return_to_lots", return_to_lots),
    ("change_name_", change_name),
    ("change_id_", change_id),
    ("change_quantity_", change_quantity),
    ("change_snum_", change_snum),
    ("cycle_link_fmt_", cycle_link_fmt_callback),
    ("delete_one_lot_", delete_one_lot_callback),
    ("confirm_delete_one_lot_", confirm_delete_one_lot_callback),
    ("api_service_", api_service_callback),
    ("edit_apiurl_", edit_apiurl),
    ("edit_apikey_", edit_apikey),
    ("edit_apiname_", edit_apiname),
    ("edit_apinum_", edit_apinum),
    ("check_balance_", check_balance),
    ("template_view_", template_view_callback),
    ("template_edit_", template_edit_callback),
    ("template_reset_prompt_", template_reset_prompt_callback),
    ("template_reset_confirm_", template_reset_confirm_callback),
]

_TELEMETRY_ALLOWED_CALLBACKS = {
    "autosmm_telemetry_accept",
    "autosmm_telemetry_refuse",
    "autosmm_telemetry_guest_7d",
}


def _register_telegram_callbacks(c_: Cardinal) -> None:
    def _add_cb(_fn: Callable[..., Any], *, func: Callable[[types.CallbackQuery], bool], **kwargs: Any) -> None:
        def _guarded_callback(call: types.CallbackQuery) -> Any:
            user = getattr(call, "from_user", None)
            user_id = getattr(user, "id", None)
            username = getattr(user, "username", "") or ""
            data = str(getattr(call, "data", "") or "")
            fn_name = getattr(_fn, "__name__", str(_fn))
            logger.debug(f"[TG CALLBACK] user={user_id} (@{username}) data='{data}' func={fn_name}")
            msg_obj = getattr(call, "message", None)
            msg_chat_id = getattr(getattr(msg_obj, "chat", None), "id", None)
            msg_id = getattr(msg_obj, "message_id", None)
            rt.bump_screen_revision(msg_chat_id, msg_id)
            if data not in _TELEMETRY_ALLOWED_CALLBACKS and not _telemetry_gate_allows_menu():
                try:
                    rt.bot.answer_callback_query(call.id)
                except Exception as ans_exc:
                    logger.debug(f"answer_callback_query failed: {ans_exc}")
                if msg_chat_id is not None and msg_id is not None:
                    _show_telemetry_gate(msg_chat_id, message_id=msg_id)
                return None
            return _fn(call)

        c_.telegram.cbq_handler(_guarded_callback, func=func, **kwargs)

    def autosmm_debug_handler(message: types.Message) -> None:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        username = getattr(user, "username", "") or ""
        logger.debug(f"[TG CMD] /autosmm_debug from user={user_id} (@{username})")
        _send_log_file_async(
            message.chat.id,
            f"📄 <b>AutoSMM Debug Log</b>\n📦 Версия: {VERSION}",
            on_empty_text="⚠️ Файл логов AutoSMM пуст или ещё не создан.",
        )

    c_.telegram.file_handler(STATE_RESTORE_BACKUP, handle_backup_restore_upload)

    for route_name, handler in _EXACT_CALLBACK_ROUTES:
        _add_cb(
            handler,
            func=lambda call, target=route_name: isinstance(getattr(call, "data", None), str) and call.data == target,
        )

    for prefix, handler in _PREFIX_CALLBACK_ROUTES:
        _add_cb(
            handler,
            func=lambda call, p=prefix: isinstance(getattr(call, "data", None), str) and call.data.startswith(p),
        )

    _add_cb(
        edit_lot_callback,
        func=lambda call: (
            isinstance(getattr(call, "data", None), str)
            and call.data.startswith("edit_lot_")
            and not call.data.startswith("edit_lot_search_")
        ),
    )
    _add_cb(
        page_navigation,
        func=lambda call: (
            isinstance(getattr(call, "data", None), str)
            and (call.data.startswith("prev_page_") or call.data.startswith("next_page_"))
        ),
    )

    c_.telegram.msg_handler(auto_smm_settings, commands=["autosmm"])
    c_.telegram.msg_handler(send_chat_id, commands=["chatid"])
    c_.telegram.msg_handler(autosmm_debug_handler, commands=["autosmm_debug"])


def init_commands(c_: Cardinal) -> None:
    """Initialize AutoSMM storage, configuration, Telegram commands, and handlers during PRE_INIT."""
    _bootstrap_runtime()
    logger.debug("AutoSMM init_commands() started")
    rt.cardinal_instance = c_
    tg = getattr(c_, "telegram", None)
    bot = getattr(tg, "bot", None)
    if c_ is None or tg is None or bot is None:
        logger.warning("Skipping AutoSMM Telegram registration: Telegram bot is disabled or unavailable")
        return
    rt.bot = bot

    with CONFIG_LOCK:
        cfg = load_config()
        rt.config.clear()
        rt.config.update(cfg)
        rt.lot_mapping.clear()
        rt.lot_mapping.update(cfg.get("lot_mapping", {}))
    rt.RUNNING = True
    c_.add_telegram_commands(
        UUID,
        [
            ("autosmm", "Открыть AutoSMM", True),
            ("chatid", "Показать chat_id", True),
        ],
    )
    _register_telegram_callbacks(c_)


def start_runtime(c_: Cardinal) -> None:
    """Recover buyer-flow runtime state and start background workers once Cardinal account init completes."""
    logger.debug("AutoSMM start_runtime() started")
    rt.cardinal_instance = c_
    tg = getattr(c_, "telegram", None)
    bot = getattr(tg, "bot", None)
    if c_ is None or tg is None or bot is None:
        logger.warning("Skipping AutoSMM start_runtime: Telegram bot is disabled or unavailable")
        return
    rt.bot = bot
    rt.RUNNING = True
    _recover_buyer_flow_runtime(c_)
    _start_background_threads(c_)


def stop_runtime(*_args: Any, **_kwargs: Any) -> None:
    """Signal AutoSMM background workers to stop and close plugin file log handlers."""
    rt.RUNNING = False
    logger.debug("AutoSMM stop_runtime() signaled background workers to stop")
    target_log = os.path.abspath(rt.LOG_PATH) if getattr(rt, "LOG_PATH", None) else ""
    for handler in list(rt.logger.handlers):
        if isinstance(handler, logging.FileHandler):
            if not target_log or os.path.abspath(getattr(handler, "baseFilename", "")) == target_log:
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass
                try:
                    rt.logger.removeHandler(handler)
                except ValueError:
                    pass


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_POST_INIT = [start_runtime]
BIND_TO_NEW_MESSAGE = [auto_smm_handler]
BIND_TO_NEW_ORDER = [auto_smm_handler]
BIND_TO_ORDER_STATUS_CHANGED = [auto_smm_handler]
BIND_TO_DELETE = [stop_runtime]
