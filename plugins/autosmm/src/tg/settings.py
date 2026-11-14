"""Telegram UI screens and callbacks for main AutoSMM settings and allowed domains."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..core import panel_balances as core_panel_balances
from ..core.links import _is_plausible_allowed_domain, _normalize_allowed_domain
from ..services import analytics
from ..services.telemetry import (
    _format_telemetry_guest_remaining,
    _get_telemetry_guest_remaining_seconds,
    _maybe_send_telemetry_ping,
    _remember_telemetry_chat_id,
    _telemetry_consent_accepted,
    _telemetry_gate_allows_menu,
)
from ..storage.storage import CONFIG_LOCK, load_config, load_valid_links, reset_valid_links_to_defaults, save_config, save_valid_links
from .common import (
    _build_ui_input_keyboard, _clear_navigation_states, _clear_screen_state, _clear_step_handler,
    _clear_tg_state, _clear_ui_input_state, _decode_callback_value, _edit_html_screen, _edit_screen_if_revision_matches,
    _encode_callback_value, _extract_call_ids, _get_ui_input_state, _inline_kb, _is_delete_confirm_grace_active,
    _register_guarded_step_handler, _repeat_ui_input_flow, _safe_answer_callback, _safe_edit_message_text,
    _send_or_edit_text, _set_delete_confirm_grace, _start_ui_input_flow, clean_tg_text, h,
    register_navigation_prefix, register_navigation_target,
)
from .telemetry import _show_telemetry_gate


# =========================================================
# SETTINGS UI
# =========================================================


def _open_main_settings_menu(chat_id: int, message_id: Optional[int] = None) -> None:
    if not _telemetry_consent_accepted():
        _remember_telemetry_chat_id(chat_id)
    cfg = load_config()
    txt_, kb = build_main_menu_text_and_kb(cfg)
    target_msg_id = _send_or_edit_text(chat_id, txt_, kb, message_id=message_id)
    if target_msg_id is None or not (cfg.get("services") or {}):
        return
    expected_rev = (
        rt.bump_screen_revision(chat_id, target_msg_id)
        if message_id is None
        else rt.get_screen_revision(chat_id, target_msg_id)
    )
    core_panel_balances.schedule_panel_balances_refresh(
        cfg,
        force=False,
        max_age_seconds=core_panel_balances.PANEL_BALANCE_CACHE_TTL_SECONDS,
        on_complete=lambda updated, _txt: (
            _edit_screen_if_revision_matches(
                chat_id,
                target_msg_id,
                expected_rev,
                lambda: build_main_menu_text_and_kb(load_config()),
                parse_mode="HTML",
            )
            if updated
            else None
        ),
    )


def build_main_menu_text_and_kb(cfg: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """Собирает главное меню AutoSMM."""
    lmap = cfg.get("lot_mapping", {}) or {}
    auto_refunds_icon = "✅" if cfg.get("auto_refunds", False) else "❌"
    confirm_link_icon = "✅" if cfg.get("confirm_link", True) else "❌"
    fallback_icon = "✅" if cfg.get("fallback_enabled", False) else "❌"

    stats = analytics.get_statistics()
    day_profit = stats.get("day_chistota", 0) if stats else 0
    try:
        day_profit_fmt = f"{float(day_profit):.2f}"
    except Exception:
        day_profit_fmt = str(day_profit)

    total_balance_text = core_panel_balances.get_cached_total_balance_text(cfg)

    lines = [
        f"🚀 <b>AutoSMM v{h(rt.VERSION)}</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📦 <b>Лоты:</b> <code>{h(len(lmap))}</code>",
        "",
        f"<b>Автовозвраты:</b> {auto_refunds_icon}",
        f"<b>Подтверждение ссылки:</b> {confirm_link_icon}",
        f"<b>Резервная панель:</b> {fallback_icon}",
        "",
        f"💰 <b>Профит за 24ч:</b> <code>{h(day_profit_fmt)} ₽</code>",
        f"💳 <b>Баланс панелей:</b> <code>{h(total_balance_text)}</code>",
    ]
    if not _telemetry_consent_accepted():
        remaining_secs = _get_telemetry_guest_remaining_seconds()
        if remaining_secs is not None and remaining_secs > 0:
            lines.append(
                f"🧪 <b>Гостевой режим:</b> <code>осталось {h(_format_telemetry_guest_remaining(remaining_secs))}</code>"
            )
    lines.extend(["", "━━━━━━━━━━━━━━━━━━━━━"])
    txt_ = clean_tg_text("\n".join(lines))

    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🛍️ Каталог лотов", callback_data="asmm:lot_settings"),
        InlineKeyboardButton("➕ Создать новый лот", callback_data="asmm:add_new_lot"),
    )
    kb.add(InlineKeyboardButton("🔌 Интеграция API", callback_data="asmm:api_settings"))
    kb.add(InlineKeyboardButton("📊 Аналитика", callback_data="asmm:show_orders"))
    kb.row(
        InlineKeyboardButton("📁 Бэкап / файлы", callback_data="asmm:files_menu"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="asmm:misc_settings"),
    )
    kb.add(InlineKeyboardButton("📚 Полезные ресурсы", callback_data="asmm:links_menu"))
    return txt_, kb


def auto_smm_settings(message: types.Message) -> None:
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    if chat_id is not None:
        _clear_step_handler(chat_id)
        _clear_navigation_states(chat_id)
        _clear_tg_state(chat_id, user_id)
    if not _telemetry_gate_allows_menu():
        if chat_id is not None:
            _show_telemetry_gate(chat_id)
        return
    if chat_id is not None:
        _open_main_settings_menu(chat_id)
    _maybe_send_telemetry_ping(rt.cardinal_instance, force=False)


def misc_settings(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_screen_state(call)

    cfg = load_config()
    fallback_enabled = bool(cfg.get("fallback_enabled", False))
    txt_ = "⚙️ <b>Настройки AutoSMM</b>\n\nВыберите раздел настроек:"
    kb_ = _inline_kb(
        ("👤 Покупательский сценарий", "asmm:buyer_flow_settings"),
        ("💬 Тексты сообщений", "asmm:edit_messages"),
        ("🔔 Уведомления продавцу", "asmm:seller_notifications_settings"),
        ("🌐 Разрешённые сайты", "asmm:manage_websites"),
        (f"🔁 Резервная панель: {'✅' if fallback_enabled else '❌'}", "asmm:toggle_fallback"),
        ("◀️ Назад", "asmm:return_to_settings"),
    )
    _edit_html_screen(call, txt_, kb_)


def buyer_flow_settings(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_screen_state(call)
    cfg = load_config()
    confirm_link = bool(cfg.get("confirm_link", True))
    auto_refunds = bool(cfg.get("auto_refunds", False))
    allow_cancel = bool(cfg.get("allow_buyer_cancel_before_panel_order", False))
    txt_ = f"""👤 <b>Покупательский сценарий</b>

Подтверждение ссылки: {'✅' if confirm_link else '❌'}
Автовозвраты: {'✅' if auto_refunds else '❌'}
Отмена до запуска: {'✅' if allow_cancel else '❌'}

Здесь настраивается поведение покупателя после оплаты."""
    kb_ = _inline_kb(
        (f"✅ Подтверждение ссылки: {'✅' if confirm_link else '❌'}", "asmm:toggle_confirm_link"),
        (f"↩️ Автовозвраты: {'✅' if auto_refunds else '❌'}", "asmm:toggle_auto_refunds"),
        (f"⚠️ Отмена до запуска: {'✅' if allow_cancel else '❌'}", "asmm:toggle_buyer_cancel_before_panel"),
        ("◀️ Назад", "asmm:misc_settings"),
    )
    _edit_html_screen(call, txt_, kb_)


def seller_notifications_settings(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_screen_state(call)
    cfg = load_config()
    notif_chat_id = cfg.get("notification_chat_id")
    txt_ = f"""<b>🔔 Уведомления продавцу</b>

Чат уведомлений: {'задан' if notif_chat_id else 'не задан'}

AutoSMM может отправлять продавцу уведомления о новых заказах, ошибках панели и ручной проверке."""
    kb_ = _inline_kb(
        ("📌 Использовать этот чат", "asmm:use_current_chat_for_notifications"),
        ("📩 Указать другой чат", "asmm:set_notification_chat_id"),
        ("🧪 Отправить тестовое уведомление", "asmm:send_test_notification"),
        ("🔕 Отключить уведомления", "asmm:disable_notifications"),
        ("◀️ Назад", "asmm:misc_settings"),
    )
    _edit_html_screen(call, txt_, kb_)


def _update_config_value(key: str, value: Any) -> Dict[str, Any]:
    with CONFIG_LOCK:
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
        rt.config[key] = value
        return cfg


def use_current_chat_for_notifications(call: types.CallbackQuery) -> None:
    _update_config_value("notification_chat_id", call.message.chat.id)
    _safe_answer_callback(call, "Чат уведомлений установлен")
    seller_notifications_settings(call)


def send_test_notification(call: types.CallbackQuery) -> None:
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id:
        _safe_answer_callback(call, "Чат уведомлений не задан")
        seller_notifications_settings(call)
        return
    try:
        rt.bot.send_message(notification_chat_id, "🧪 Тестовое уведомление AutoSMM")
        _safe_answer_callback(call, "Отправлено")
    except Exception as ex:
        _safe_answer_callback(call, "Не удалось отправить")
        rt.logger.warning(f"Не удалось отправить тестовое уведомление AutoSMM: {ex}")
    seller_notifications_settings(call)


def disable_notifications(call: types.CallbackQuery) -> None:
    _update_config_value("notification_chat_id", None)
    _safe_answer_callback(call, "Отключено")
    seller_notifications_settings(call)


def _toggle_bool_setting(
    call: types.CallbackQuery,
    key: str,
    default: bool,
    refresh_screen: Callable[[types.CallbackQuery], Any],
) -> None:
    with CONFIG_LOCK:
        cfg = load_config()
        new_value = not bool(cfg.get(key, default))
        cfg[key] = new_value
        save_config(cfg)
        rt.config[key] = new_value
    _safe_answer_callback(call)
    refresh_screen(call)


def toggle_fallback(call: types.CallbackQuery) -> None:
    _toggle_bool_setting(call, "fallback_enabled", False, misc_settings)


def _toggle_danger_setting(
    call: types.CallbackQuery,
    key: str,
    disable_answer: str,
    confirm_label: str,
    confirm_cb: str,
    prompt_text: str,
) -> None:
    cfg = load_config()
    if bool(cfg.get(key, False)):
        _update_config_value(key, False)
        _safe_answer_callback(call, disable_answer)
        buyer_flow_settings(call)
        return
    _safe_answer_callback(call)
    _edit_html_screen(call, prompt_text, _inline_kb((confirm_label, confirm_cb), ("◀️ Назад", "asmm:buyer_flow_settings")))


def toggle_buyer_cancel_before_panel(call: types.CallbackQuery) -> None:
    _toggle_danger_setting(
        call,
        "allow_buyer_cancel_before_panel_order",
        "Отключено",
        "✅ Включить",
        "asmm:enable_buyer_cancel_before_panel",
        """⚠️ <b>Опасная настройка</b>

Если включить эту опцию, покупатель сможет отменить заказ командой !отмена, пока выполнение ещё не запущено.

После запуска выполнения автоматическая отмена будет недоступна.

Включайте только если готовы к автоматическим отменам.""",
    )


def enable_buyer_cancel_before_panel(call: types.CallbackQuery) -> None:
    _update_config_value("allow_buyer_cancel_before_panel_order", True)
    _safe_answer_callback(call, "Включено")
    buyer_flow_settings(call)


def toggle_auto_refunds(call: types.CallbackQuery) -> None:
    _toggle_danger_setting(
        call,
        "auto_refunds",
        "Автовозвраты выключены",
        "✅ Включить автовозвраты",
        "asmm:enable_auto_refunds",
        """⚠️ <b>Автовозвраты</b>

Если включить эту настройку, AutoSMM сможет автоматически вернуть деньги покупателю, если заказ не удалось безопасно запустить.

Не включайте автовозвраты, пока не проверили свои панели и лоты.""",
    )


def enable_auto_refunds(call: types.CallbackQuery) -> None:
    _update_config_value("auto_refunds", True)
    _safe_answer_callback(call, "Автовозвраты включены")
    buyer_flow_settings(call)


def toggle_confirm_link(call: types.CallbackQuery) -> None:
    _toggle_bool_setting(call, "confirm_link", True, buyer_flow_settings)


def links_menu(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    txt_ = "📚 <b>Полезные ресурсы</b>\n\nПолезные ссылки по AutoSMM и SMM-панелям."
    kb_ = InlineKeyboardMarkup(row_width=2)
    kb_.row(
        InlineKeyboardButton("🛠 Поддержка", url="https://t.me/w1bbex"),
        InlineKeyboardButton("📢 Канал плагинов", url="https://t.me/w1bbexplugins"),
    )
    kb_.row(
        InlineKeyboardButton("🌐 SmmWay", url="https://smmway.ru/ref3967025"),
        InlineKeyboardButton("🌐 KuzyaBoost", url="https://kuzya-boost.ru/ref4251277"),
    )
    kb_.row(
        InlineKeyboardButton("💻 GitHub", url="https://github.com/W1bbex"),
        InlineKeyboardButton("❓ FAQ", url="https://teletype.in/@w1bbex/FAQ-AutoSMM"),
    )
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:return_to_settings"))
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


# =========================================================
# ALLOWED DOMAINS UI
# =========================================================


def add_website(message: types.Message, new_site: str) -> None:
    normalized_site = _normalize_allowed_domain(new_site)
    if not normalized_site or not _is_plausible_allowed_domain(normalized_site):
        rt.bot.send_message(message.chat.id, "❌ Не удалось определить корректный домен сайта.")
        return
    added = False
    with CONFIG_LOCK:
        valid_links = load_valid_links()
        if normalized_site not in valid_links:
            valid_links.append(normalized_site)
            save_valid_links(valid_links)
            added = True
    if added:
        rt.bot.send_message(message.chat.id, f"✅ Сайт {normalized_site} успешно добавлен в список.")
    else:
        rt.bot.send_message(message.chat.id, f"❌ Сайт {normalized_site} уже есть в списке.")


def manage_websites(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_screen_state(call)

    txt_ = (
        "🌐 <b>Разрешённые сайты</b>\n\n"
        "Здесь настраиваются сайты, ссылки с которых покупатели могут отправлять после оплаты."
    )
    kb_ = _inline_kb(
        ("📋 Показать список", "asmm:show_allowed_websites_list"),
        ("➕ Добавить сайт", "asmm:add_website"),
        ("🗑 Удалить сайт", "asmm:show_allowed_websites_list"),
        ("🔄 Сбросить список", "asmm:reset_valid_links_prompt"),
        ("◀️ Назад", "asmm:misc_settings"),
    )
    _edit_html_screen(call, txt_, kb_)


def reset_valid_links_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    kb_ = _inline_kb(("✅ Сбросить", "asmm:reset_valid_links_confirm"), ("◀️ Назад", "asmm:manage_websites"))
    txt_ = """Список разрешённых сайтов будет заменён на стандартный набор AutoSMM.
Ваши добавленные вручную домены будут удалены.

Продолжить?"""
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, reply_markup=kb_)


def reset_valid_links_confirm(call: types.CallbackQuery) -> None:
    reset_valid_links_to_defaults()
    _safe_answer_callback(call, "Список сброшен")
    manage_websites(call)


def show_allowed_websites_list(
    call: types.CallbackQuery,
    page: Optional[int] = None,
    *,
    preserve_page: bool = False,
) -> None:
    _safe_answer_callback(call)
    _clear_screen_state(call)

    valid_links = load_valid_links()
    per_page = 20
    total_pages = max(1, ((len(valid_links) - 1) // per_page) + 1) if valid_links else 1
    chat_id = int(call.message.chat.id)
    if page is None:
        raw_page = int(rt.WEBSITE_PAGE_CONTEXT.get(chat_id, 0) or 0) if preserve_page else 0
    else:
        raw_page = int(page or 0)
    current_page = max(0, min(raw_page, total_pages - 1))
    rt.WEBSITE_PAGE_CONTEXT[chat_id] = current_page

    start = current_page * per_page
    end = start + per_page
    chunk = valid_links[start:end]

    kb_ = InlineKeyboardMarkup(row_width=2)
    if chunk:
        buttons = [
            InlineKeyboardButton(
                f"🗑 {site}",
                callback_data=_encode_callback_value("asmm:delete_website_", site),
            )
            for site in chunk
        ]
        for idx in range(0, len(buttons), 2):
            kb_.row(*buttons[idx : idx + 2])
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"asmm:sites_page_{current_page - 1}"))
        if end < len(valid_links):
            nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"asmm:sites_page_{current_page + 1}"))
        if nav_buttons:
            kb_.row(*nav_buttons)
        page_suffix = f" (стр. {current_page + 1}/{total_pages})" if total_pages > 1 else ""
        text_ = f"🌐 <b>Разрешённые сайты</b>{page_suffix}\n\nНажмите на сайт, чтобы удалить его из списка."
    else:
        text_ = "🌐 <b>Разрешённые сайты</b>\n\nСписок разрешённых сайтов пуст."

    kb_.add(
        InlineKeyboardButton("➕ Добавить сайт", callback_data="asmm:add_website"),
        InlineKeyboardButton("🔄 Сбросить список", callback_data="asmm:reset_valid_links_prompt"),
        InlineKeyboardButton("◀️ Назад", callback_data="asmm:manage_websites"),
    )
    _edit_html_screen(call, text_, kb_)


def allowed_websites_page_callback(call: types.CallbackQuery) -> None:
    try:
        page = int(str(call.data or "").rsplit("_", 1)[-1])
    except ValueError:
        page = 0
    show_allowed_websites_list(call, page=page)


def _nav_sites_page(call: types.CallbackQuery, target: str) -> None:
    try:
        page = int(str(target or "").rsplit("_", 1)[-1])
    except ValueError:
        page = int(rt.WEBSITE_PAGE_CONTEXT.get(call.message.chat.id, 0) or 0)
    show_allowed_websites_list(call, page=page)


def add_website_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(
        call,
        "Введите ссылку для добавления (например, example.com):",
        process_add_website,
        back_target="asmm:manage_websites",
    )


def process_add_website(message: types.Message) -> None:
    new_site = str(getattr(message, "text", None) or "").strip()
    if not new_site:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: ссылка не может быть пустой.")
        return
    normalized_site = _normalize_allowed_domain(new_site)
    if not normalized_site or not _is_plausible_allowed_domain(normalized_site):
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: не удалось определить корректный домен сайта.")
        return
    add_website(message, new_site)
    _clear_ui_input_state(message.chat.id)
    rt.bot.send_message(message.chat.id, "Возврат к списку сайтов.", reply_markup=_inline_kb(("◀️ Назад", "asmm:manage_websites")))


def _delete_allowed_website_and_refresh(call: types.CallbackQuery, domain: str) -> None:
    domain = _normalize_allowed_domain(domain)
    removed = False
    with CONFIG_LOCK:
        valid_links = load_valid_links()
        if domain in valid_links:
            valid_links.remove(domain)
            save_valid_links(valid_links)
            removed = True
    if removed:
        _safe_answer_callback(call, "Удалено")
    else:
        _safe_answer_callback(call, "Сайт уже отсутствует")
    show_allowed_websites_list(call, preserve_page=True)


def remove_website_prompt(call: types.CallbackQuery) -> None:
    data = str(getattr(call, "data", "") or "")
    raw_arg = data.split("asmm:delete_website_", 1)[1] if "asmm:delete_website_" in data else ""
    site_to_remove = _normalize_allowed_domain(_decode_callback_value(raw_arg))
    if not site_to_remove:
        _safe_answer_callback(call)
        show_allowed_websites_list(call, preserve_page=True)
        return
    if _is_delete_confirm_grace_active(call.message.chat.id):
        _delete_allowed_website_and_refresh(call, site_to_remove)
        return
    _safe_answer_callback(call)
    back_page = max(0, int(rt.WEBSITE_PAGE_CONTEXT.get(call.message.chat.id, 0) or 0))
    kb_ = _inline_kb(
        ("✅ Удалить", _encode_callback_value("asmm:confirm_delete_website_", site_to_remove)),
        ("◀️ Назад", f"asmm:sites_page_{back_page}"),
    )
    txt_ = f"""⚠️ <b>Удалить сайт из разрешённых?</b>

Сайт:
{h(site_to_remove)}

Покупатели больше не смогут отправлять ссылки с этого домена.

После подтверждения следующие удаления в течение 30 секунд будут выполняться без повторного подтверждения."""
    _edit_html_screen(call, txt_, kb_)


def confirm_delete_website_callback(call: types.CallbackQuery) -> None:
    data = str(getattr(call, "data", "") or "")
    raw_arg = data.split("asmm:confirm_delete_website_", 1)[1] if "asmm:confirm_delete_website_" in data else ""
    domain = _decode_callback_value(raw_arg)
    _set_delete_confirm_grace(call.message.chat.id, 30)
    _delete_allowed_website_and_refresh(call, domain)


# =========================================================
# NOTIFICATIONS INPUT FLOWS & NAVIGATION
# =========================================================


def return_to_settings(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    rt.SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)
    _clear_screen_state(call)
    _open_main_settings_menu(call.message.chat.id, message_id=call.message.message_id)


def set_notification_chat_id(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(
        call,
        "Отправьте ID Telegram-чата для уведомлений.\n\nМожно нажать «Использовать этот чат» в предыдущем меню.",
        process_notification_chat_id,
        back_target="asmm:seller_notifications_settings",
    )


def process_notification_chat_id(message: types.Message) -> None:
    raw_text = str(getattr(message, "text", None) or "").strip()
    cmd_token = raw_text.split(None, 1)[0].split("@", 1)[0].lower() if raw_text else ""
    if cmd_token == "/chatid":
        rt.bot.send_message(
            message.chat.id,
            f"ID этого чата: {message.chat.id}\n\nОтправьте ID чата числом или нажмите «Использовать этот чат» в меню уведомлений.",
            reply_markup=_build_ui_input_keyboard(message.chat.id),
        )
        state = _get_ui_input_state(message.chat.id)
        if state:
            try:
                _register_guarded_step_handler(
                    message,
                    state["next_handler"],
                    *state.get("handler_args", ()),
                    expected_user_id=state.get("initiator_user_id"),
                )
            except Exception as exc:
                rt.logger.debug(f"Не удалось перерегистрировать step handler для chat_id={message.chat.id}: {exc}")
        return
    try:
        new_chat_id = int(raw_text)
        if new_chat_id == 0:
            raise ValueError("zero chat_id")
        _update_config_value("notification_chat_id", new_chat_id)
        _clear_ui_input_state(message.chat.id)
        rt.bot.send_message(
            message.chat.id,
            f"✅ Чат уведомлений установлен: {new_chat_id}",
            reply_markup=_inline_kb(("◀️ Назад", "asmm:seller_notifications_settings")),
        )
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: отправьте корректный ID чата числом (не 0).")


process_notification_chat_id.allowed_step_commands = ("/chatid",)


def misc_settings_callback(call: types.CallbackQuery) -> None:
    misc_settings(call)


def links_menu_callback(call: types.CallbackQuery) -> None:
    links_menu(call)


for _nav_key, _nav_handler in (
    ("asmm:return_to_settings", return_to_settings),
    ("asmm:manage_websites", manage_websites),
    ("asmm:show_allowed_websites_list", show_allowed_websites_list),
    ("asmm:misc_settings", misc_settings),
    ("asmm:links_menu", links_menu),
    ("asmm:seller_notifications_settings", seller_notifications_settings),
    ("asmm:buyer_flow_settings", buyer_flow_settings),
):
    register_navigation_target(_nav_key, _nav_handler)
register_navigation_prefix("asmm:sites_page_", _nav_sites_page)
