"""All Telegram HTML view renderers and screen keyboards for W1bbex Hub."""

from __future__ import annotations

import os
from typing import Any, Optional

from telebot.types import InlineKeyboardMarkup

from ..runtime import DEFAULT_CHANNEL_URL, GITHUB_URL, SUPPORT_URL, VERSION
from .formatters import (
    STATUS_BADGES,
    format_catalog_button,
    format_catalog_line,
    format_command_lines,
    format_primary_command,
    license_summary_text,
    resolve_display_version,
)
from .kit import Btn, CardBuilder, KeyboardBuilder, bold, code, esc, sanitize_telegram_html


def render_main_menu(
    plugin_states: list[dict[str, Any]],
    sys_info: dict[str, str],
    notify_updates: bool,
    hub_latest_version: str,
    has_hub_update: bool,
    hub_pending_version: Optional[str],
    keys_count: int,
    notice_banner: Optional[str] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the W1bbex Hub main dashboard and primary navigation."""
    total_count = len(plugin_states)
    restart_count = sum(1 for s in plugin_states if s["needs_restart"])
    updates_count = sum(1 for s in plugin_states if s["has_update"] and not s["needs_restart"])

    card = CardBuilder("👨‍💻", "W1bbex Hub", f"v{VERSION}")
    if notice_banner:
        card.paragraph(notice_banner)

    os_label = esc(sys_info.get("os", "Unknown"))
    py_label = esc(sys_info.get("python_version", "?"))
    card.quote(f"🖥 OS: {os_label}\n🐍 Python: {py_label}")

    alert_lines: list[str] = []
    if restart_count:
        alert_lines.append(f"🔄 Ожидают перезапуск: <b>{restart_count}</b>")
    if updates_count:
        alert_lines.append(f"⬆️ Доступны обновления плагинов: <b>{updates_count}</b>")
    if alert_lines:
        card.paragraph("\n".join(alert_lines))

    kb = KeyboardBuilder()
    if hub_pending_version:
        card.tree(
            "🔄 <b>Обновление Hub готово к запуску</b>",
            [
                f"Установлено на диск: {code(f'v{hub_pending_version}')}",
                f"Текущая в памяти: {code(f'v{VERSION}')}",
            ],
        )
        kb.row(Btn(f"🔄 Перезагрузить Cardinal (Hub {hub_pending_version})", "hub:restart:hub", style="success"))
    elif has_hub_update:
        card.tree(
            "⬆️ <b>Доступно обновление Hub</b>",
            [
                f"Версия: {code(f'v{VERSION} → v{hub_latest_version}')}",
                "Нажмите кнопку ниже для загрузки.",
            ],
        )
        kb.row(Btn(f"⬆️ Обновить Hub до {hub_latest_version}", "hub:update_hub", style="success"))

    kb.row(Btn(f"🧩 Каталог плагинов ({total_count})", "hub:plugins_menu", style="success"))
    kb.row(
        Btn("🔑 Активировать ключ", "hub:activate_key", style="primary"),
        Btn("📜 Лицензии", "hub:licenses", style="primary"),
    )
    notify_btn = (
        Btn("Уведомления: ✅", "hub:toggle_notify", style="success")
        if notify_updates
        else Btn("Уведомления: ❌", "hub:toggle_notify", style="danger")
    )
    kb.row(
        notify_btn,
        Btn("🌐 Ресурсы", "hub:resources", style="primary"),
    )
    return card.build(), kb.build()


def render_plugins_menu(plugin_states: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    """Render grouped catalog overview and per-plugin action buttons."""
    card = CardBuilder("🧩", "Каталог плагинов")
    kb = KeyboardBuilder()

    if not plugin_states:
        card.quote("Не удалось загрузить каталог плагинов или список пуст. Попробуйте обновить позже.")
    else:
        my_states = [s for s in plugin_states if s["status_code"] != "not_installed"]
        available_states = [s for s in plugin_states if s["status_code"] == "not_installed"]

        if my_states:
            card.tree(
                f"<b>Мои плагины ({len(my_states)})</b>",
                [format_catalog_line(s) for s in my_states],
            )
        if available_states:
            card.tree(
                f"<b>Доступны в каталоге ({len(available_states)})</b>",
                [format_catalog_line(s) for s in available_states],
            )
        card.paragraph("<i>Выберите плагин ниже для установки или настройки.</i>")

        for state in my_states + available_states:
            label, style = format_catalog_button(state)
            kb.row(Btn(label, f"hub:info:{state['id']}", style=style))

    kb.row(
        Btn("🔄 Обновить список", "hub:plugins_refresh", style="primary"),
        Btn("⬅️ Главное меню", "hub:main"),
    )
    return card.build(), kb.build()


def render_plugin_info(state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Render detailed plugin status card with state-aware colored controls."""
    pid = str(state["id"])
    name = str(state["name"])
    latest = str(state["latest_version"])
    status = str(state["status_code"])
    status_icon, status_label = STATUS_BADGES.get(status, ("🔹", status))
    display_ver = resolve_display_version(state)

    card = CardBuilder("🔌", name, f"v{display_ver}")
    desc = str(state.get("description") or "").strip()
    if desc:
        safe_desc = sanitize_telegram_html(desc)
        if safe_desc != esc(desc):
            card.paragraph(safe_desc)
        else:
            card.quote(safe_desc)

    details: list[str] = [
        f"Статус: {status_icon} {esc(status_label)}",
        f"Лицензия: {license_summary_text(state)}",
        f"В каталоге: {code(f'v{latest}')}",
    ]

    if state["installed_entry"]:
        if state["loaded_version"] and state["installed_version"] and state["loaded_version"] != state["installed_version"]:
            details.append(
                f"На диске / в памяти: {code(state['installed_version'])} / {code(state['loaded_version'])}"
            )
        elif state["installed_version"]:
            details.append(f"Установлено через Hub: {code(state['installed_version'])}")

    cmd_lines = format_command_lines(state.get("commands") or [])

    manual_files = state.get("manual_files") or []
    if manual_files:
        files_fmt = ", ".join(code(os.path.basename(f)) for f in manual_files[:3])
        extra = f" (+{len(manual_files) - 3})" if len(manual_files) > 3 else ""
        details.append(f"Сторонние файлы: {files_fmt}{extra}")

    if state["storage_dir_exists"]:
        details.append("Данные: <i>сохраняются при удалении плагина</i>")

    card.tree("<b>Информация</b>", details)
    card.tree("<b>Команды</b>", cmd_lines)

    if state["needs_restart"]:
        card.paragraph("🔄 <b>Требуется перезапуск Cardinal</b> для применения изменений.")
    elif status == "broken_files":
        card.paragraph("⚠️ <b>Не найден файл-загрузчик</b> — переустановите плагин кнопкой ниже.")
    elif status == "manual":
        card.paragraph("💡 При установке через Hub ручная копия будет аккуратно заменена загрузчиком.")
    elif state["has_update"] and state["installed_entry"]:
        card.paragraph(f"✨ Доступно обновление до {code(f'v{latest}')}.")

    kb = KeyboardBuilder()
    loaded_uuid = state.get("loaded_uuid")
    settings_page = bool(state.get("settings_page"))

    if state["needs_restart"]:
        kb.row(Btn("🔄 Перезагрузить Cardinal", f"hub:restart:{state['restart_action']}:{pid}", style="success"))
    elif status == "active":
        open_row: list[Btn] = []
        primary_cmd = format_primary_command(state.get("commands") or [])
        if primary_cmd:
            open_row.append(Btn(f"🚀 Открыть {primary_cmd}", f"hub:open:{pid}", style="success"))
        if loaded_uuid:
            cb_prefix = "47" if settings_page else "45"
            open_row.append(Btn("⚙️ Настройки Cardinal", f"{cb_prefix}:{loaded_uuid}:0", style="primary"))
        kb.row(*open_row)
    elif status == "disabled" and loaded_uuid:
        cb_prefix = "47" if settings_page else "45"
        kb.row(Btn("⚙️ Включить в Cardinal", f"{cb_prefix}:{loaded_uuid}:0", style="primary"))
    elif status == "manual" and loaded_uuid:
        cb_prefix = "47" if settings_page else "45"
        kb.row(Btn("⚙️ Настройки Cardinal", f"{cb_prefix}:{loaded_uuid}:0"))

    if state["installed_entry"]:
        action_row: list[Btn] = []
        if state["has_update"] and not state["needs_restart"]:
            action_row.append(Btn(f"⬆️ Обновить до {latest}", f"hub:install:{pid}", style="success"))
        elif status == "broken_files" or not state["needs_restart"]:
            action_row.append(Btn("🔄 Переустановить", f"hub:install:{pid}"))
        action_row.append(Btn("🗑 Удалить", f"hub:delete_confirm:{pid}", style="danger"))
        kb.row(*action_row)
    elif status != "restart_delete":
        if state["is_free"] or state["has_key"]:
            verb = "🔄 Заменить из Hub" if status == "manual" else "📥 Установить"
            kb.row(Btn(f"{verb} · {latest}", f"hub:install:{pid}", style="success"))
        else:
            kb.row(
                Btn("🎁 Получить Trial", f"hub:trial:{pid}", style="success"),
                Btn("🔑 Ввести ключ", "hub:activate_key", style="primary"),
            )

    kb.row(
        Btn("⬅️ К каталогу", "hub:plugins_menu"),
        Btn("🏠 Главное меню", "hub:main"),
    )
    return card.build(), kb.build()


def render_delete_confirm(state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Render two-step confirmation card before removing a plugin from disk."""
    pid = str(state["id"])
    name = str(state["name"])
    stub_name = os.path.basename(str(state.get("stub_path") or f"{pid}.py"))
    bin_dir_name = os.path.basename(str(state.get("bin_dir") or name))

    card = CardBuilder("⚠️", f"Удалить плагин {name}?")
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
        Btn("🗑 Да, удалить", f"hub:delete:{pid}", style="danger"),
        Btn("⬅️ Отмена", f"hub:info:{pid}"),
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
        card = CardBuilder("✅", f"{plugin_label} {verb}")
        card.quote(esc(message))
        card.paragraph("🔄 <b>Перезагрузите Cardinal</b>, чтобы применить изменения.")
        kb.row(Btn("🔄 Перезагрузить Cardinal", f"hub:restart:{action}:{plugin_id}", style="success"))
        kb.row(
            Btn("📄 Карточка плагина", f"hub:info:{plugin_id}"),
            Btn("📦 К каталогу", "hub:plugins_menu"),
        )
        kb.row(Btn("🏠 Главное меню", "hub:main"))
    else:
        card = CardBuilder("❌", f"Не удалось установить {plugin_label}")
        card.quote(esc(message))
        kb.row(Btn("🔄 Повторить", f"hub:install:{plugin_id}", style="primary"))
        kb.row(
            Btn("⬅️ К плагину", f"hub:info:{plugin_id}"),
            Btn("🏠 Главное меню", "hub:main"),
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
        lines.append(f"Бинарники: {code(f'plugins/w1bbex/{bin_dir_name}/')} — в очереди до рестарта")
    else:
        lines.append(f"Бинарники: {code(f'plugins/w1bbex/{bin_dir_name}/')} — удалены")
    if had_storage_dir:
        lines.append(f"Данные сохранены: {code(f'storage/w1bbex/{bin_dir_name}/')}")
    if result.get("kept_key"):
        lines.append("Лицензионный ключ: сохранён в Hub")

    card = CardBuilder("🗑", f"Плагин {pname} удалён с диска")
    card.tree("<b>Результат</b>", lines)

    kb = KeyboardBuilder()
    if was_loaded_in_ram or result.get("bin_deferred"):
        card.paragraph("🔄 Перезагрузите Cardinal, чтобы полностью выгрузить плагин из памяти.")
        kb.row(Btn("🔄 Перезагрузить Cardinal", f"hub:restart:delete:{pid}", style="success"))

    kb.row(
        Btn("📦 К каталогу", "hub:plugins_menu"),
        Btn("🏠 Главное меню", "hub:main"),
    )
    return card.build(), kb.build()


def render_hub_update_result(disk_ver: Optional[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Render confirmation card after W1bbex Hub downloads its own update."""
    card = CardBuilder("✅", "W1bbex Hub обновлён на диске")
    details = [
        f"Версия на диске: {code(f'v{disk_ver}')}" if disk_ver else "Новая версия: <b>загружена на диск</b>",
        "Применение: требуется перезапуск Cardinal",
    ]
    card.tree("<b>Подробности</b>", details)
    card.paragraph("🔄 Нажмите кнопку ниже — после запуска Hub автоматически проверит активную версию.")

    kb = KeyboardBuilder()
    kb.row(Btn("🔄 Перезагрузить Cardinal сейчас", "hub:restart:hub", style="success"))
    kb.row(Btn("🏠 Главное меню", "hub:main"))
    return card.build(), kb.build()


def render_hub_update_error(message: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render error card when W1bbex Hub self-update fails."""
    card = CardBuilder("❌", "Не удалось обновить W1bbex Hub")
    card.quote(esc(message))
    kb = KeyboardBuilder()
    kb.row(Btn("🔄 Повторить", "hub:update_hub", style="primary"))
    kb.row(Btn("🏠 Главное меню", "hub:main"))
    return card.build(), kb.build()


def render_resources() -> tuple[str, InlineKeyboardMarkup]:
    """Render official community, repository, and support links."""
    card = CardBuilder("🌐", "Ресурсы W1bbex Hub")
    card.tree(
        "<b>Официальные ссылки</b>",
        [
            "📢 Канал с релизами и новостями плагинов",
            "🐙 Исходный код, документация и отчёты",
            "🆘 Личная поддержка и помощь с ключами",
        ],
    )
    kb = KeyboardBuilder()
    kb.row(
        Btn("📢 Канал", url=DEFAULT_CHANNEL_URL, style="primary"),
        Btn("🐙 GitHub", url=GITHUB_URL),
    )
    kb.row(Btn("🆘 Написать в поддержку", url=SUPPORT_URL))
    kb.row(Btn("⬅️ Главное меню", "hub:main"))
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
            card = CardBuilder("⚠️", "Cardinal перезапущен · Hub не обновился")
            card.tree(
                "<b>Проверка версий</b>",
                [
                    f"В памяти: {code(f'v{running_hub_version}')}",
                    f"Ожидалась: {code(f'v{target_ver}')}",
                ],
            )
            card.paragraph("Попробуйте повторить обновление в главном меню Hub.")
            kb.row(Btn("🏠 Главное меню Hub", "hub:main", style="primary"))
            return card.build(), kb.build()

        card = CardBuilder("✅", f"W1bbex Hub обновлён до v{running_hub_version}")
        card.paragraph("Cardinal успешно перезапущен и загрузил новую версию менеджера.")
        kb.row(
            Btn("🧩 Каталог плагинов", "hub:plugins_menu", style="primary"),
            Btn("🏠 Главное меню Hub", "hub:main"),
        )
        return card.build(), kb.build()

    if action == "delete" and pid:
        loaded_still = bool(state and state.get("loaded_version"))
        if loaded_still or disk_still_exists:
            card = CardBuilder("⚠️", f"Удаление {pname} завершилось частично")
            notes: list[str] = []
            if loaded_still:
                notes.append("Плагин всё ещё числится в памяти Cardinal")
            if disk_still_exists:
                notes.append("Часть файлов осталась заблокирована ОС")
            card.tree("<b>Что осталось</b>", notes)
            kb.row(
                Btn("📄 Карточка плагина", f"hub:info:{pid}", style="primary"),
                Btn("🏠 Главное меню", "hub:main"),
            )
            return card.build(), kb.build()

        card = CardBuilder("✅", f"Плагин {pname} полностью удалён")
        card.paragraph("Файлы плагина удалены с диска, память Cardinal очищена.")
        kb.row(
            Btn("📦 К каталогу плагинов", "hub:plugins_menu", style="primary"),
            Btn("🏠 Главное меню", "hub:main"),
        )
        return card.build(), kb.build()

    if pid and state is not None:
        loaded_ver = state.get("loaded_version")
        if loaded_ver and not state.get("needs_restart"):
            card = CardBuilder("✅", f"Cardinal перезапущен · {pname} v{loaded_ver}")
            cmd_lines = format_command_lines(state.get("commands") or [])
            if cmd_lines:
                card.tree("<b>Доступные команды</b>", cmd_lines)
            else:
                card.paragraph(f"Плагин {bold(pname)} успешно загружен и готов к работе.")

            primary_cmd = format_primary_command(state.get("commands") or [])
            if primary_cmd:
                kb.row(Btn(f"🚀 Открыть {pname} ({primary_cmd})", f"hub:open:{pid}", style="success"))

            loaded_uuid = state.get("loaded_uuid")
            if loaded_uuid:
                cb_prefix = "47" if state.get("settings_page") else "45"
                kb.row(Btn("⚙️ В настройках Cardinal", f"{cb_prefix}:{loaded_uuid}:0", style="primary"))

            kb.row(
                Btn("📦 Карточка плагина", f"hub:info:{pid}"),
                Btn("🔌 Плагины", "hub:plugins_menu"),
            )
            kb.row(Btn("🏠 Главное меню Hub", "hub:main"))
            return card.build(), kb.build()

        card = CardBuilder("⚠️", f"Cardinal перезапущен · {pname} не загружен")
        card.paragraph("Плагин не прошёл загрузку при старте. Откройте карточку для диагностики или повторите установку.")
        kb.row(
            Btn("📄 Карточка плагина", f"hub:info:{pid}", style="primary"),
            Btn("🔌 Плагины", "hub:plugins_menu"),
        )
        kb.row(Btn("🏠 Главное меню", "hub:main"))
        return card.build(), kb.build()

    card = CardBuilder("✅", "FunPay Cardinal успешно перезапущен")
    kb.row(Btn("🏠 Открыть меню Hub", "hub:main", style="primary"))
    return card.build(), kb.build()


def render_licenses(keys: dict[str, str]) -> tuple[str, InlineKeyboardMarkup]:
    """Render saved license keys overview with safe key masking."""
    card = CardBuilder("📜", "Ваши лицензии")
    if not keys:
        card.quote("Пока нет сохранённых ключей. Используйте активацию по ключу или Trial в карточке плагина.")
    else:
        lines = []
        for pid, key in sorted(keys.items()):
            masked = f"{key[:8]}…{key[-4:]}" if len(key) > 14 else key
            lines.append(f"{bold(pid)} — {code(masked)}")
        card.tree(f"<b>Сохранённые ключи ({len(keys)})</b>", lines)

    kb = KeyboardBuilder()
    kb.row(Btn("🔑 Добавить или обновить ключ", "hub:activate_key", style="primary"))
    kb.row(Btn("⬅️ Главное меню", "hub:main"))
    return card.build(), kb.build()


def render_trial_prompt(plugin_info: dict[str, Any], plugin_name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render Trial activation confirmation screen."""
    label = str(plugin_info.get("name", plugin_name))
    hours = int(plugin_info.get("trial_hours") or 24)
    card = CardBuilder("🎁", f"Пробный доступ · {label}")
    card.tree(
        "<b>Условия</b>",
        [
            f"Длительность: <b>{hours} ч.</b>",
            "Активация: мгновенно для текущего аккаунта FunPay",
        ],
    )
    card.paragraph("<i>После получения ключа вы сможете сразу установить плагин.</i>")

    kb = KeyboardBuilder()
    kb.row(Btn("🎁 Активировать Trial", f"hub:trial_confirm:{plugin_name}", style="success"))
    kb.row(Btn("⬅️ Отмена", f"hub:info:{plugin_name}"))
    return card.build(), kb.build()


def render_trial_activated(plugin_name: str, plugin_label: str, key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render success screen immediately after receiving a Trial key."""
    card = CardBuilder("✅", f"Trial выдан · {plugin_label}")
    card.tree("<b>Ключ сохранён</b>", [f"Лицензия: {code(key)}"])
    card.paragraph("Нажмите кнопку ниже, чтобы скачать и установить плагин.")
    kb = KeyboardBuilder()
    kb.row(Btn(f"📥 Установить {plugin_label}", f"hub:install:{plugin_name}", style="success"))
    kb.row(Btn("⬅️ Карточка плагина", f"hub:info:{plugin_name}"))
    return card.build(), kb.build()


def render_activate_key_prompt() -> tuple[str, InlineKeyboardMarkup]:
    """Render prompt asking the owner to paste a license key in chat."""
    card = CardBuilder("🔑", "Активация лицензионного ключа")
    card.paragraph("Отправьте ваш ключ следующим сообщением — Hub автоматически определит нужный плагин.")
    card.quote("Для отмены нажмите кнопку ниже или отправьте «отмена».")
    kb = KeyboardBuilder()
    kb.row(Btn("⬅️ Отмена", "hub:cancel_step"))
    return card.build(), kb.build()


def render_key_activated(plugin_id: str, plugin_label: str, key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render key activation success card with direct download button."""
    card = CardBuilder("✅", "Ключ активирован")
    card.tree(
        "<b>Детали</b>",
        [
            f"Плагин: {bold(plugin_label)}",
            f"Ключ: {code(key)}",
        ],
    )
    kb = KeyboardBuilder()
    kb.row(Btn(f"📥 Скачать {plugin_label}", f"hub:install:{plugin_id}", style="success"))
    kb.row(Btn("🏠 Главное меню", "hub:main"))
    return card.build(), kb.build()


def render_missing_key_notice(plugin_id: str, plugin_label: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render screen when user attempts to install a paid plugin without a saved key."""
    card = CardBuilder("🔒", f"Нужен ключ для {plugin_label}")
    card.paragraph(
        f"Активируйте ключ через главное меню или командой {code(f'!hub_key {plugin_id} ВАШ_КЛЮЧ')}."
    )
    kb = KeyboardBuilder()
    kb.row(
        Btn("🎁 Получить Trial", f"hub:trial:{plugin_id}", style="success"),
        Btn("🔑 Ввести ключ", "hub:activate_key", style="primary"),
    )
    kb.row(Btn("⬅️ Назад", f"hub:info:{plugin_id}"))
    return card.build(), kb.build()


def render_catalog_unavailable(plugin_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render offline/outage notice when catalog check fails before install."""
    card = CardBuilder("⚠️", "Каталог плагинов временно недоступен")
    card.paragraph("Не удалось проверить статус плагина на сервере. Повторите попытку чуть позже.")
    kb = KeyboardBuilder()
    kb.row(Btn("🔄 Повторить", f"hub:install:{plugin_id}", style="primary"))
    kb.row(Btn("🏠 Главное меню", "hub:main"))
    return card.build(), kb.build()


def render_restart_in_progress() -> str:
    """Render transient status text right before triggering Cardinal process restart."""
    return CardBuilder("🔄", "Перезагружаю FunPay Cardinal…").build()


def render_update_notification(
    update: dict[str, Any],
    notify_enabled: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render push notification card for a newer plugin version."""
    urgency = str(update.get("urgency") or "medium").lower()
    urgency_badge = {
        "critical": "🚨 Критическое обновление",
        "high": "🔥 Важное обновление",
    }.get(urgency, "✨ Доступно обновление")

    ver_transition = f"{update['curr_ver']} → {update['latest']}"
    card = CardBuilder("🆕", f"{update['name']} · {update['latest']}")
    card.tree(
        f"<b>{esc(urgency_badge)}</b>",
        [f"Версия: {code(ver_transition)}"],
    )
    changelog = str(update.get("changelog") or "Без описания").strip()
    card.quote(esc(changelog))

    kb = KeyboardBuilder()
    kb.row(Btn(f"⬆️ Обновить до {update['latest']}", f"hub:install:{update['id']}", style="success"))
    toggle_label = "Уведомления: ✅" if notify_enabled else "Уведомления: ❌"
    toggle_style = "success" if notify_enabled else "danger"
    kb.row(
        Btn("⏳ Напомнить завтра", f"hub:remind:{update['update_key']}"),
        Btn(toggle_label, "hub:toggle_notify_from_msg", style=toggle_style),
    )
    return card.build(), kb.build()


def rebuild_notification_toggle_markup(
    markup: Any,
    notify_enabled: bool,
) -> Optional[InlineKeyboardMarkup]:
    """Rebuild update notification markup with refreshed toggle label while keeping button styles."""
    keyboard = getattr(markup, "keyboard", None)
    if not keyboard:
        return None
    kb = KeyboardBuilder()
    toggle_label = "Уведомления: ✅" if notify_enabled else "Уведомления: ❌"
    toggle_style = "success" if notify_enabled else "danger"
    for row in keyboard:
        row_btns: list[Btn] = []
        for btn in row:
            cb_data = getattr(btn, "callback_data", None)
            text = str(getattr(btn, "text", ""))
            style: Any = getattr(btn, "style", None)
            if cb_data == "hub:toggle_notify_from_msg":
                text = toggle_label
                style = toggle_style
            elif isinstance(cb_data, str) and cb_data.startswith("hub:install:"):
                style = "success"
            row_btns.append(
                Btn(
                    text=text,
                    callback_data=cb_data,
                    url=getattr(btn, "url", None),
                    style=style,
                )
            )
        kb.row(*row_btns)
    return kb.build()
