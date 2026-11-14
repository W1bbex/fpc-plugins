"""Outcome, license, and notification views for plugin lifecycle operations."""

from __future__ import annotations

import os
from typing import Any, Optional
from telebot.types import InlineKeyboardMarkup
from ..runtime import DEFAULT_CHANNEL_URL, GITHUB_URL, SUPPORT_URL
from .formatters import format_command_lines, format_primary_command, mask_key
from .icons import tg_icon
from .kit import Btn, CardBuilder, KeyboardBuilder, bold, code, esc


def render_delete_confirm(state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Render two-step confirmation card before removing a plugin from disk."""
    pid = str(state["id"])
    name = str(state["name"])
    stub_name = os.path.basename(str(state.get("stub_path") or f"{pid}.py"))
    bin_dir_name = os.path.basename(str(state.get("bin_dir") or name))

    card = CardBuilder(tg_icon("alert"), f"Удалить плагин {name}?")
    card.tree(
        "<b>Будет удалено с диска</b>",
        [
            f"Лоадер: {code(f'plugins/{stub_name}')}",
            f"Файлы плагина: {code(f'plugins/w1bbex/{bin_dir_name}/')}",
        ],
    )

    kept_lines = ["Настройки и база данных в <code>storage/w1bbex/</code>"]
    if state.get("has_key"):
        kept_lines.append("Сохранённый лицензионный ключ Hub")
    card.tree("<b>Останется без изменений</b>", kept_lines)
    card.paragraph("<i>После удаления потребуется перезапуск Cardinal для выгрузки из памяти.</i>")

    kb = KeyboardBuilder()
    kb.row(
        Btn("Да, удалить", f"hub:delete:{pid}", style="danger"),
        Btn("Отмена", f"hub:info:{pid}"),
    )
    return card.build(), kb.build()


def render_install_result(
    plugin_id: str,
    plugin_label: str,
    success: bool,
    message: str,
    is_update: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render outcome screen after downloading/updating a plugin."""
    kb = KeyboardBuilder()
    if success:
        action = "update" if is_update else "install"
        verb = "обновлён на диске" if is_update else "установлен на диск"
        card = CardBuilder(tg_icon("check"), f"{plugin_label} {verb}")
        card.paragraph("Изменения сохранены · перезапустите Cardinal для запуска новой версии")
        kb.row(Btn("Перезапустить Cardinal", f"hub:restart:{action}:{plugin_id}", style="success"))
        kb.row(
            Btn("Карточка плагина", f"hub:info:{plugin_id}"),
            Btn("К каталогу", "hub:plugins_menu"),
        )
        kb.row(Btn("Главная", "hub:main"))
    else:
        card = CardBuilder(tg_icon("cross"), f"Не удалось установить {plugin_label}")
        card.quote(esc(message[:3500]))
        kb.row(Btn("Повторить", f"hub:install:{plugin_id}", style="primary"))
        kb.row(
            Btn("К плагину", f"hub:info:{plugin_id}"),
            Btn("Главная", "hub:main"),
        )
    return card.build(), kb.build()


def render_delete_result(
    result: dict[str, Any],
    was_loaded_in_ram: bool,
    had_storage_dir: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render outcome screen after deleting plugin loader/binaries."""
    pid = str(result.get("plugin_id") or "")
    pname = str(result.get("plugin_name") or pid)
    stub_name = result.get("stub_name") or f"{pname}.py"
    bin_dir_name = result.get("bin_dir_name") or pname

    lines = [f"Лоадер: {code(f'plugins/{stub_name}')} — удалён"]
    if result.get("bin_deferred"):
        lines.append(f"Бинарники: {code(f'plugins/w1bbex/{bin_dir_name}/')} — в очереди до перезапуска")
    else:
        lines.append(f"Бинарники: {code(f'plugins/w1bbex/{bin_dir_name}/')} — удалены")
    if had_storage_dir:
        lines.append(f"Данные сохранены: {code(f'storage/w1bbex/{bin_dir_name}/')}")
    if result.get("kept_key"):
        lines.append("Лицензионный ключ: сохранён в Hub")

    card = CardBuilder(tg_icon("trash"), f"Плагин {pname} удалён с диска")
    card.tree("<b>Результат</b>", lines)

    kb = KeyboardBuilder()
    if was_loaded_in_ram or result.get("bin_deferred"):
        card.paragraph("Перезапустите Cardinal, чтобы полностью выгрузить плагин из памяти.")
        kb.row(Btn("Перезапустить Cardinal", f"hub:restart:delete:{pid}", style="success"))

    kb.row(
        Btn("К каталогу", "hub:plugins_menu"),
        Btn("Главная", "hub:main"),
    )
    return card.build(), kb.build()


def render_hub_release(current: str, latest: Optional[str], notes: Optional[str], loading: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    card = CardBuilder(tg_icon("refresh"), "Что нового в W1bbex Hub")
    card.paragraph(f"Текущая версия: {code(current)}\nНовая версия: {code(latest) if latest else 'уточняется' if loading else 'неизвестна'}")
    card.quote(esc(("Загружаем описание…" if loading else (notes or "Описание изменений пока недоступно"))[:2500]))
    kb = KeyboardBuilder()
    if not loading:
        if latest:
            kb.row(Btn("Обновить Hub", "hub:download_hub", style="success", icon="download"))
        else:
            kb.row(Btn("Повторить проверку", "hub:update_hub", style="primary", icon="refresh"))
    kb.row(Btn("Назад", "hub:main", style="danger", icon="back"))
    return card.build(), kb.build()


def render_hub_update_result(disk_ver: Optional[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Render confirmation card after W1bbex Hub downloads its own update."""
    card = CardBuilder(tg_icon("check"), "W1bbex Hub обновлён на диске")
    details = [
        f"Версия на диске: {code(f'v{disk_ver}')}" if disk_ver else "Новая версия: <b>загружена на диск</b>",
        "Применение: требуется перезапуск Cardinal",
    ]
    card.tree("<b>Подробности</b>", details)
    card.paragraph("Нажмите кнопку ниже — после запуска Hub автоматически проверит активную версию.")

    kb = KeyboardBuilder()
    kb.row(Btn("Перезапустить Cardinal сейчас", "hub:restart:hub", style="success"))
    kb.row(Btn("Главная", "hub:main"))
    return card.build(), kb.build()


def render_hub_update_error(message: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render error card when W1bbex Hub self-update fails."""
    card = CardBuilder(tg_icon("cross"), "Не удалось обновить W1bbex Hub")
    card.quote(esc(message[:3500]))
    kb = KeyboardBuilder()
    kb.row(Btn("Повторить", "hub:download_hub", style="primary"))
    kb.row(Btn("Главная", "hub:main"))
    return card.build(), kb.build()


def render_resources() -> tuple[str, InlineKeyboardMarkup]:
    """Render official community, repository, and support links."""
    card = CardBuilder(tg_icon("link"), "Ресурсы W1bbex Hub")
    card.tree(
        "<b>Официальные ссылки</b>",
        [
            "Канал с релизами и новостями плагинов",
            "Исходный код, документация и отчёты",
            "Личная поддержка и помощь с ключами",
        ],
    )
    kb = KeyboardBuilder()
    kb.row(
        Btn("Канал", url=DEFAULT_CHANNEL_URL, style="primary"),
        Btn("GitHub", url=GITHUB_URL),
    )
    kb.row(Btn("Написать в поддержку", url=SUPPORT_URL))
    kb.row(Btn("Главная", "hub:main"))
    return card.build(), kb.build()


def render_post_restart_report(
    pending: dict[str, Any],
    state: Optional[dict[str, Any]],
    running_hub_version: str,
    disk_still_exists: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render verified status report in Telegram immediately after Cardinal restarts."""
    action = str(pending.get("action") or "general")
    pid = str(pending.get("plugin_id") or "")
    pname = str(pending.get("plugin_name") or (state or {}).get("name") or pid or "Плагин")

    kb = KeyboardBuilder()

    if action == "hub":
        target_ver = str(pending.get("target_version") or "")
        if pending.get("hub_mismatch"):
            card = CardBuilder(tg_icon("alert"), "Cardinal перезапущен · Hub не обновился")
            card.tree(
                "<b>Проверка версий</b>",
                [
                    f"В памяти: {code(f'v{running_hub_version}')}",
                    f"Ожидалась: {code(f'v{target_ver}')}",
                ],
            )
            card.paragraph("Попробуйте повторить обновление в главном меню Hub.")
            kb.row(Btn("Главная", "hub:main", style="primary"))
            return card.build(), kb.build()

        card = CardBuilder(tg_icon("check"), f"W1bbex Hub обновлён до v{running_hub_version}")
        card.paragraph("Cardinal успешно перезапущен и загрузил новую версию менеджера.")
        kb.row(
            Btn("Каталог плагинов", "hub:plugins_menu", style="primary"),
            Btn("Главная", "hub:main"),
        )
        return card.build(), kb.build()

    if action == "delete" and pid:
        loaded_still = bool(state and state.get("loaded_version"))
        if loaded_still or disk_still_exists:
            card = CardBuilder(tg_icon("alert"), f"Удаление {pname} завершилось частично")
            notes: list[str] = []
            if loaded_still:
                notes.append("Плагин всё ещё числится в памяти Cardinal")
            if disk_still_exists:
                notes.append("Часть файлов осталась заблокирована ОС")
            card.tree("<b>Что осталось</b>", notes)
            kb.row(
                Btn("Карточка плагина", f"hub:info:{pid}", style="primary"),
                Btn("Главная", "hub:main"),
            )
            return card.build(), kb.build()

        card = CardBuilder(tg_icon("check"), f"Плагин {pname} полностью удалён")
        card.paragraph("Файлы плагина удалены с диска, память Cardinal очищена.")
        kb.row(
            Btn("К каталогу плагинов", "hub:plugins_menu", style="primary"),
            Btn("Главная", "hub:main"),
        )
        return card.build(), kb.build()

    if pid and state is not None:
        loaded_ver = state.get("loaded_version")
        if loaded_ver and not state.get("needs_restart"):
            card = CardBuilder(tg_icon("check"), f"Cardinal перезапущен · {pname} v{loaded_ver}")
            cmd_lines = format_command_lines(state.get("commands") or [])
            if cmd_lines:
                card.tree("<b>Доступные команды</b>", cmd_lines)
            else:
                card.paragraph(f"Плагин {bold(pname)} успешно загружен и готов к работе.")

            primary_cmd = format_primary_command(state.get("commands") or [])
            if primary_cmd:
                kb.row(Btn(f"Открыть {pname} ({primary_cmd})", f"hub:open:{pid}", style="success"))

            loaded_uuid = state.get("loaded_uuid")
            if loaded_uuid:
                cb_prefix = "47" if state.get("settings_page") else "45"
                kb.row(Btn("В настройках Cardinal", f"{cb_prefix}:{loaded_uuid}:0", style="primary"))

            kb.row(
                Btn("Карточка плагина", f"hub:info:{pid}"),
                Btn("Плагины", "hub:plugins_menu"),
            )
            kb.row(Btn("Главная", "hub:main"))
            return card.build(), kb.build()

        card = CardBuilder(tg_icon("alert"), f"Cardinal перезапущен · {pname} не загружен")
        card.paragraph("Плагин не прошёл загрузку при старте. Откройте карточку для диагностики или повторите установку.")
        kb.row(
            Btn("Карточка плагина", f"hub:info:{pid}", style="primary"),
            Btn("Плагины", "hub:plugins_menu"),
        )
        kb.row(Btn("Главная", "hub:main"))
        return card.build(), kb.build()

    card = CardBuilder(tg_icon("check"), "FunPay Cardinal успешно перезапущен")
    kb.row(Btn("Открыть меню Hub", "hub:main", style="primary"))
    return card.build(), kb.build()


def render_licenses(keys: dict[str, str], names: Optional[dict[str, str]] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Render saved license keys overview with safe key masking."""
    card = CardBuilder(tg_icon("key"), "Сохранённые ключи")
    card.paragraph("Действительность, срок и привязка проверяются сервером при установке")
    if not keys:
        card.quote("Пока нет ключей · добавьте свой или запросите пробный доступ в карточке плагина")
    else:
        lines = []
        for pid, key in sorted(keys.items()):
            lines.append(f"{bold((names or {}).get(pid, pid))} — {code(mask_key(key))}")
        card.tree(f"<b>Сохранённые ключи ({len(keys)})</b>", lines)

    kb = KeyboardBuilder()
    kb.row(Btn("Добавить ключ", "hub:activate_key", style="primary", icon="key"))
    kb.row(Btn("Главная", "hub:main"))
    return card.build(), kb.build()


def render_trial_prompt(plugin_info: dict[str, Any], plugin_name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render Trial activation confirmation screen."""
    label = str(plugin_info.get("name", plugin_name))
    card = CardBuilder(tg_icon("clock"), f"Пробный доступ · {label}")
    card.tree(
        "<b>Условия</b>",
        [
            "Доступ выдаётся для текущего аккаунта FunPay",
            "Срок и возможность выдачи определяет сервер",
        ],
    )
    card.paragraph("<i>После получения ключа вы сможете сразу установить плагин.</i>")

    kb = KeyboardBuilder()
    kb.row(Btn("Получить пробный доступ", f"hub:trial_confirm:{plugin_name}", style="success", icon="clock"))
    kb.row(Btn("Отмена", f"hub:info:{plugin_name}"))
    return card.build(), kb.build()


def render_trial_activated(plugin_name: str, plugin_label: str, key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render success screen immediately after receiving a Trial key."""
    card = CardBuilder(tg_icon("check"), f"Пробный ключ получен · {plugin_label}")
    card.tree("<b>Ключ сохранён</b>", [f"Ключ: {code(mask_key(key))}"])
    card.paragraph("Нажмите кнопку ниже, чтобы скачать и установить плагин.")
    kb = KeyboardBuilder()
    kb.row(Btn(f"Установить {plugin_label}", f"hub:install:{plugin_name}", style="success"))
    kb.row(Btn("Карточка плагина", f"hub:info:{plugin_name}"))
    return card.build(), kb.build()


def render_activate_key_prompt() -> tuple[str, InlineKeyboardMarkup]:
    """Render prompt asking the owner to paste a license key in chat."""
    card = CardBuilder(tg_icon("key"), "Добавить ключ")
    card.paragraph("Отправьте ключ следующим сообщением · Hub определит плагин по префиксу")
    card.paragraph("Сервер проверит ключ при установке плагина")
    card.quote("Для отмены нажмите кнопку ниже или отправьте «отмена».")
    kb = KeyboardBuilder()
    kb.row(Btn("Отмена", "hub:cancel_step"))
    return card.build(), kb.build()


def render_key_activated(plugin_id: str, plugin_label: str, key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render key activation success card with direct download button."""
    card = CardBuilder(tg_icon("check"), "Ключ сохранён")
    card.tree(
        "<b>Детали</b>",
        [
            f"Плагин: {bold(plugin_label)}",
            f"Ключ: {code(mask_key(key))}",
        ],
    )
    kb = KeyboardBuilder()
    card.paragraph("Ключ ещё не проверен · сервер проверит его при установке")
    kb.row(Btn(f"Установить {plugin_label}", f"hub:install:{plugin_id}", style="success", icon="download"))
    kb.row(Btn("Главная", "hub:main"))
    return card.build(), kb.build()


def render_missing_key_notice(plugin_id: str, plugin_label: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render screen when user attempts to install a paid plugin without a saved key."""
    card = CardBuilder(tg_icon("key"), f"Нужен ключ для {plugin_label}")
    card.paragraph(
        "Добавьте ключ или запросите пробный доступ"
    )
    kb = KeyboardBuilder()
    kb.row(
        Btn("Пробный доступ", f"hub:trial:{plugin_id}", icon="clock"),
        Btn("Добавить ключ", "hub:activate_key", style="primary", icon="key"),
    )
    kb.row(Btn("Назад", f"hub:info:{plugin_id}"))
    return card.build(), kb.build()


def render_catalog_unavailable(plugin_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render offline/outage notice when catalog check fails before install."""
    card = CardBuilder(tg_icon("alert"), "Каталог плагинов временно недоступен")
    card.paragraph("Не удалось проверить статус плагина на сервере. Повторите попытку чуть позже.")
    kb = KeyboardBuilder()
    kb.row(Btn("Повторить", f"hub:install:{plugin_id}", style="primary"))
    kb.row(Btn("Главная", "hub:main"))
    return card.build(), kb.build()


def render_restart_in_progress() -> str:
    """Render transient status text right before triggering Cardinal process restart."""
    return CardBuilder(tg_icon("refresh"), "Перезапускаю FunPay Cardinal…").build()


def render_update_notification(
    update: dict[str, Any],
    notify_enabled: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render a release notification with the correct Hub or plugin update action."""
    urgency = str(update.get("urgency") or "medium").lower()
    urgency_badge = {
        "critical": "Критическое обновление",
        "high": "Важное обновление",
    }.get(urgency, "Доступно обновление")

    ver_transition = f"{update['curr_ver']} → {update['latest']}"
    changelog = str(update.get("changelog") or "Без описания").strip()
    if len(changelog) > 1800:
        changelog = changelog[:1799].rstrip() + "…"

    def _build_update_card(changelog_text: str) -> str:
        card = CardBuilder(tg_icon("refresh"), f"{update['name']} · {update['latest']}")
        card.tree(
            f"<b>{esc(urgency_badge)}</b>",
            [f"Версия: {code(ver_transition)}"],
        )
        if changelog_text:
            card.quote(esc(changelog_text))
        return card.build()

    rendered_text = _build_update_card(changelog)
    if len(rendered_text) > 4096:
        limit = len(changelog)
        while len(rendered_text) > 4096 and limit > 0:
            excess = len(rendered_text) - 4096
            limit = max(0, limit - max(excess, limit // 4 + 1))
            trimmed = (changelog[:limit].rstrip() + "…") if limit > 0 else ""
            rendered_text = _build_update_card(trimmed)

    kb = KeyboardBuilder()
    if update.get("target") == "hub":
        kb.row(Btn(f"Обновить Hub до {update['latest']}", "hub:update_hub", style="success", icon="download"))
    else:
        kb.row(Btn(f"Обновить до {update['latest']}", f"hub:install:{update['id']}", style="success"))
    toggle_label = "Все уведомления: вкл" if notify_enabled else "Все уведомления: выкл"
    kb.row(
        Btn("Напомнить завтра", f"hub:remind:{update['update_key']}"),
        Btn(toggle_label, "hub:toggle_notify_from_msg", style="success" if notify_enabled else "danger",
            icon="bell" if notify_enabled else "bell_off"),
    )
    return rendered_text, kb.build()


def rebuild_notification_toggle_markup(
    markup: Any,
    notify_enabled: bool,
) -> Optional[InlineKeyboardMarkup]:
    """Rebuild update notification markup with refreshed toggle label while keeping button styles."""
    keyboard = getattr(markup, "keyboard", None)
    if not keyboard:
        return None
    kb = KeyboardBuilder()
    toggle_label = "Все уведомления: вкл" if notify_enabled else "Все уведомления: выкл"
    for row in keyboard:
        row_btns: list[Btn] = []
        for btn in row:
            cb_data = getattr(btn, "callback_data", None)
            text = str(getattr(btn, "text", ""))
            style: Any = getattr(btn, "style", None)
            if cb_data == "hub:toggle_notify_from_msg":
                text = toggle_label
                style = "success" if notify_enabled else "danger"
            elif cb_data == "hub:update_hub" or (isinstance(cb_data, str) and cb_data.startswith("hub:install:")):
                style = "success"
            row_btns.append(
                Btn(
                    text=text,
                    callback_data=cb_data,
                    url=getattr(btn, "url", None),
                    style=style,
                    icon=("bell" if notify_enabled else "bell_off") if cb_data == "hub:toggle_notify_from_msg" else None,
                )
            )
        kb.row(*row_btns)
    return kb.build()
