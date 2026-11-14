"""Compact navigation, catalog, plugin cards, and on-demand diagnostics."""

from __future__ import annotations

import os
import html
import re
from typing import Any, Optional

from telebot.types import InlineKeyboardMarkup

from ..runtime import VERSION
from .formatters import (
    STATUS_BADGES, catalog_caption, description_preview, format_command_lines,
    format_primary_command,
)
from .icons import tg_icon
from .kit import Btn, CardBuilder, KeyboardBuilder, code, esc, sanitize_telegram_html
from .outcomes import (
    rebuild_notification_toggle_markup, render_activate_key_prompt,
    render_catalog_unavailable, render_delete_confirm, render_delete_result,
    render_hub_update_error, render_hub_update_result, render_install_result,
    render_key_activated, render_licenses, render_missing_key_notice,
    render_post_restart_report, render_resources, render_restart_in_progress,
    render_trial_activated, render_trial_prompt, render_update_notification,
)

CATALOG_PAGE_SIZE = 5


def _catalog_notice(card: CardBuilder, status: Optional[dict[str, Any]]) -> None:
    if status and status.get("loading"):
        card.paragraph("<i>Обновляем каталог в фоне</i>")
    if status and not status.get("available", True):
        detail = "Показан сохранённый список" if status.get("stale") else "Установленные плагины доступны в разделе «Мои плагины»"
        card.paragraph(f"{tg_icon('alert')} <b>Нет связи с каталогом</b>\n{detail}")


def render_main_menu(
    plugin_states: list[dict[str, Any]], sys_info: dict[str, str], notify_updates: bool,
    hub_latest_version: str, has_hub_update: bool, hub_pending_version: Optional[str],
    keys_count: int, notice_banner: Optional[str] = None,
    catalog_status: Optional[dict[str, Any]] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show plugin health, a compact environment summary, and colored controls."""
    mine = [state for state in plugin_states if state["status_code"] != "not_installed"]
    restart = sum(bool(state["needs_restart"]) for state in mine)
    updates = sum(bool(state["has_update"] and not state["needs_restart"]) for state in mine)
    broken = sum(state["status_code"] == "broken_files" for state in mine)
    card = CardBuilder(tg_icon("hub"), "W1bbex Hub", f"v{VERSION}")
    if notice_banner:
        card.paragraph(notice_banner)
    os_name = sys_info.get("os", "неизвестно")
    os_icon = "windows" if "windows" in os_name.lower() else "linux" if "linux" in os_name.lower() else None
    card.quote(f"ОС: {esc(os_name)}{' ' + tg_icon(os_icon) if os_icon else ''}\nPython: {esc(sys_info.get('python_version', 'неизвестно'))} {tg_icon('python')}")
    if mine:
        card.paragraph(f"Установлено плагинов: <b>{len(mine)}</b>")
    else:
        card.paragraph("Установите первый плагин из каталога")
    attention = []
    if restart:
        attention.append(f"Ожидают перезапуск: <b>{restart}</b>")
    if updates:
        attention.append(f"Доступны обновления: <b>{updates}</b>")
    if broken:
        attention.append(f"Нужна проверка файлов: <b>{broken}</b>")
    if attention:
        card.paragraph("\n".join(attention))
    _catalog_notice(card, catalog_status)

    kb = KeyboardBuilder()
    if hub_pending_version:
        card.paragraph(f"Hub {code(hub_pending_version)} загружен · нужен перезапуск")
        kb.row(Btn("Перезапустить Cardinal", "hub:restart:hub", style="success", icon="refresh"))
    elif has_hub_update:
        card.paragraph(f"Доступен Hub {code(hub_latest_version)}")
    if not hub_pending_version and (restart or updates or broken):
        kb.row(Btn("Требуют внимания", "hub:catalog:attention:0", style="primary", icon="alert"))
    elif not hub_pending_version:
        kb.row(Btn("Мои плагины" if mine else "Каталог плагинов", "hub:catalog:mine:0" if mine else "hub:plugins_menu", style="success", icon="apps"))

    if mine and (hub_pending_version or restart or updates or broken):
        kb.row(Btn(f"Мои плагины · {len(mine)}", "hub:catalog:mine:0", style="primary", icon="plugin"),
               Btn("Каталог", "hub:plugins_menu", style="primary", icon="apps"))
    elif mine or hub_pending_version:
        kb.row(Btn("Каталог плагинов", "hub:plugins_menu", style="primary", icon="apps"))
    kb.row(Btn(f"Ключи · {keys_count}", "hub:licenses", style="primary", icon="key"),
           Btn("Настройки", "hub:settings", style="primary", icon="settings"))
    kb.row(Btn("Уведомления включены" if notify_updates else "Уведомления выключены", "hub:toggle_notify_main",
               style="success" if notify_updates else "danger", icon="bell" if notify_updates else "bell_off"))
    if has_hub_update and not hub_pending_version:
        kb.row(Btn(f"Обновить W1bbex Hub до {hub_latest_version}", "hub:update_hub", style="danger", icon="download"))
    return card.build(), kb.build()


def render_plugins_menu(
    plugin_states: list[dict[str, Any]], page: int = 0, view: str = "all",
    catalog_status: Optional[dict[str, Any]] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show bounded catalog pages with descriptions and numbered selection controls."""
    if view not in {"all", "mine", "attention"}:
        view = "all"
    states = list(plugin_states)
    if view == "mine":
        states = [s for s in states if s["status_code"] != "not_installed"]
    elif view == "all":
        states = [s for s in states if s["status_code"] == "not_installed"]
    elif view == "attention":
        states = [s for s in states if s["needs_restart"] or s["has_update"] or s["status_code"] == "broken_files"]
    states.sort(key=lambda s: (s["status_code"] == "not_installed", str(s["name"]).casefold()))
    pages = max(1, (len(states) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    visible = states[page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE]
    title = {"all": "Каталог плагинов", "mine": "Мои плагины", "attention": "Требуют внимания"}[view]
    card = CardBuilder(tg_icon("apps"), title)
    _catalog_notice(card, catalog_status)
    kb = KeyboardBuilder()
    if not visible:
        message = {"mine": "У вас пока нет установленных плагинов", "attention": "Нет плагинов, требующих внимания"}.get(view, "Нет новых плагинов для установки")
        if view == "all" and catalog_status and catalog_status.get("loading"):
            message = "Загружаем список плагинов"
        if view == "all" and catalog_status and not catalog_status.get("available", True):
            message = "Список не загружен · повторите попытку позже"
        card.paragraph(message)
    else:
        card.paragraph(f"Плагинов: <b>{len(states)}</b> · выберите плагин")
        for state in visible:
            icon = "rocket" if str(state["id"]).lower() == "autosmm" else "plugin"
            if view == "mine":
                style = ("success" if state.get("loaded_enabled") else "danger") if state.get("loaded_uuid") else None
                label = str(state["name"])
            else:
                style, label = "primary", f"{state['name']} · {catalog_caption(state)}"
            kb.row(Btn(label, f"hub:info:{state['id']}", style=style, icon=icon))
    if pages > 1:
        card.paragraph(f"Страница {page + 1} из {pages} · Всего {len(states)}")
        buttons = []
        if page:
            buttons.append(Btn("Назад", f"hub:catalog:{view}:{page - 1}", icon="back"))
        if page + 1 < pages:
            buttons.append(Btn("Далее", f"hub:catalog:{view}:{page + 1}"))
        kb.row(*buttons)
    kb.row(Btn("Назад", "hub:main", style="danger", icon="back"))
    return card.build(), kb.build()


def render_plugin_info(state: dict[str, Any], expanded: bool = False, catalog_view: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    """Put status and next action above description; keep technical data opt-in."""
    pid, name = str(state["id"]), str(state["name"])
    latest, status = str(state["latest_version"]), str(state["status_code"])
    icon = "rocket" if pid.lower() == "autosmm" else "plugin"

    def _compose_info_card(desc_mode: str, preview_limit: int = 250) -> tuple[str, str]:
        card = CardBuilder(tg_icon(icon), name)
        if state.get("loaded_uuid") and not state.get("loaded_enabled"):
            card.paragraph("Отключён в Cardinal")
        if state["needs_restart"]:
            card.paragraph("Перезапустите Cardinal, чтобы применить изменения")
        elif status == "broken_files":
            card.paragraph("Файл загрузчика не найден · переустановите плагин")
        elif state["has_update"]:
            card.paragraph(f"Доступно обновление до {code(latest)}")
        if not state.get("catalog_known", True):
            card.paragraph("<i>Плагина нет в текущем каталоге · показано локальное состояние</i>")
        raw_desc = str(state.get("description") or "").strip()
        clean_desc = ""
        if raw_desc:
            first, separator, rest = raw_desc.partition("\n")
            plain = re.sub(r"\W+", "", html.unescape(re.sub(r"<[^>]*>", "", first))).lower()
            normalized_name = re.sub(r"\W+", "", name).lower()
            if separator and re.fullmatch(re.escape(normalized_name) + r"(?:v?\d{1,16})?", plain):
                raw_desc = rest.lstrip("\n")
            clean_desc = raw_desc
        if clean_desc:
            if desc_mode == "html":
                safe = sanitize_telegram_html(clean_desc)
                card.paragraph(safe) if safe != esc(clean_desc) else card.quote(safe)
            elif desc_mode == "preview" and preview_limit > 0:
                card.paragraph(esc(description_preview(clean_desc, preview_limit)))
        if state["needs_restart"]:
            pass
        elif state["has_update"] and (state["installed_entry"] or status == "manual"):
            if status == "manual":
                card.paragraph("Ручная копия будет заменена установкой через Hub")
        elif status != "restart_delete" and not (status == "manual" and state["has_update"]) and not state["installed_entry"]:
            if (state["is_free"] or state["has_key"]) and status == "manual":
                card.paragraph("Ручная копия будет заменена установкой через Hub")
        return card.build(), clean_desc

    desc_val = str(state.get("description") or "").strip()
    if not desc_val:
        rendered_text, desc = _compose_info_card("none")
    elif expanded and len(desc_val) > 3500:
        rendered_text, desc = _compose_info_card("preview", 2600)
    elif len(desc_val) <= 3500:
        rendered_text, desc = _compose_info_card("html")
    else:
        rendered_text, desc = _compose_info_card("preview", 250)

    if len(rendered_text) > 4096 and desc:
        preview_limit = 2600 if expanded else 250
        while preview_limit > 0:
            rendered_text, _ = _compose_info_card("preview", preview_limit)
            if len(rendered_text) <= 4096:
                break
            preview_limit = preview_limit // 2
        if len(rendered_text) > 4096:
            rendered_text, _ = _compose_info_card("none")

    commands = state.get("commands") or []

    kb = KeyboardBuilder()
    uid = state.get("loaded_uuid")
    if state["needs_restart"]:
        kb.row(Btn("Перезапустить Cardinal", f"hub:restart:{state['restart_action']}:{pid}", style="success", icon="refresh"))
    elif state["has_update"] and (state["installed_entry"] or status == "manual"):
        kb.row(Btn(f"Обновить до {latest}", f"hub:install:{pid}", style="success", icon="download"))
    elif status == "broken_files":
        kb.row(Btn("Восстановить плагин", f"hub:install:{pid}", style="primary", icon="refresh"))
    elif status in {"active", "manual"} and state.get("loaded_enabled") and format_primary_command(commands):
        kb.row(Btn("Открыть плагин", f"hub:open:{pid}", style="success", icon="plugin"))
    if state["has_update"] and not state["needs_restart"] and status == "active" and format_primary_command(commands):
        kb.row(Btn("Открыть плагин", f"hub:open:{pid}", icon="plugin"))
    if uid and not state["needs_restart"]:
        enabled = bool(state.get("loaded_enabled"))
        kb.row(Btn("Отключить" if enabled else "Включить", f"hub:plugin_{'disable' if enabled else 'enable'}:{pid}",
                   style="danger" if enabled else "success"))
    if state["installed_entry"]:
        buttons = []
        if not state["needs_restart"] and status != "broken_files" and not state["has_update"]:
            buttons.append(Btn("Переустановить", f"hub:install:{pid}", icon="refresh"))
        buttons.append(Btn("Удалить", f"hub:delete_confirm:{pid}", style="danger", icon="trash"))
        kb.row(*buttons)
    elif status != "restart_delete" and not (status == "manual" and state["has_update"]):
        if state["is_free"] or state["has_key"]:
            label = "Заменить ручную установку" if status == "manual" else "Установить плагин"
            kb.row(Btn(label, f"hub:install:{pid}", style="success", icon="download"))
        elif state.get("catalog_known", True):
            kb.row(Btn("Пробный доступ", f"hub:trial:{pid}", icon="clock"),
                   Btn("Добавить ключ", "hub:activate_key", icon="key"))
    if desc and (len(desc) > 3500 or len(rendered_text) >= 4096):
        kb.row(Btn("Свернуть описание" if expanded else "Полное описание", f"hub:{'info' if expanded else 'description'}:{pid}"))
    kb.row(Btn("Диагностика", f"hub:diagnostics:{pid}", icon="info"))
    destination = f"hub:catalog:{catalog_view}:0" if catalog_view in {"mine", "attention"} else "hub:plugins_menu"
    kb.row(Btn("Назад", destination, style="danger", icon="back"))
    return rendered_text, kb.build()


def render_plugin_diagnostics(state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Show file, runtime, and command details without crowding the normal card."""
    card = CardBuilder(tg_icon("info"), f"Диагностика · {state['name']}")
    details = [f"Загружена: {code(state.get('loaded_version') or 'нет')}",
               f"На диске: {code(state.get('installed_version') or 'нет')}",
               f"В каталоге: {code(state.get('latest_version') or 'неизвестно')}"]
    files = state.get("manual_files") or []
    if files:
        details.append("Ручные копии: " + ", ".join(code(os.path.basename(str(item.get("path", "")))) for item in files[:3]))
    if state.get("storage_dir_exists"):
        details.append("Настройки и база сохраняются при удалении")
    card.tree("<b>Версии и файлы</b>", details)
    card.tree("<b>Команды</b>", format_command_lines(state.get("commands") or []))
    return card.build(), KeyboardBuilder().row(Btn("К плагину", f"hub:info:{state['id']}", icon="back")).build()


def render_settings(notify_updates: bool, sys_info: dict[str, str], auto_check: bool = True, interval_minutes: int = 30) -> tuple[str, InlineKeyboardMarkup]:
    card = CardBuilder(tg_icon("settings"), "Настройки Hub")
    card.paragraph(f"Автопроверка: каждые {interval_minutes} мин" if auto_check else "Автоматическая проверка отключена · проверяйте обновления вручную")
    label = "Уведомления включены" if notify_updates else "Уведомления выключены"
    kb = KeyboardBuilder().row(Btn(label, "hub:toggle_notify", style="success" if notify_updates else "danger",
                                    icon="bell" if notify_updates else "bell_off"))
    kb.row(Btn(f"Автопроверка · {interval_minutes} мин" if auto_check else "Автопроверка выключена", "hub:check_interval",
               style="primary" if auto_check else "danger", icon="refresh"))
    kb.row(Btn("Проверить обновления", "hub:check_updates", style="primary", icon="refresh"))
    kb.row(Btn("Окружение", "hub:environment", icon="info"), Btn("Ссылки и поддержка", "hub:resources", icon="link"))
    kb.row(Btn("Главная", "hub:main", icon="back"))
    return card.build(), kb.build()


def render_environment(sys_info: dict[str, str]) -> tuple[str, InlineKeyboardMarkup]:
    card = CardBuilder(tg_icon("info"), "Окружение")
    card.tree(None, [f"Hub: {code(VERSION)}", f"ОС: {esc(sys_info.get('os', 'неизвестно'))}",
                     f"Python: {esc(sys_info.get('python_version', 'неизвестно'))}"])
    return card.build(), KeyboardBuilder().row(Btn("Настройки", "hub:settings", icon="back")).build()
