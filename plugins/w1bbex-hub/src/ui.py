"""Pure Telegram presentation formatters and inline keyboard builders for W1bbex Hub."""

from __future__ import annotations

import html
from typing import Any, Optional

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .runtime import DEFAULT_CHANNEL_URL, GITHUB_URL, SUPPORT_URL, VERSION

_STATUS_LABELS: dict[str, str] = {
    "active": "✅ Активен и работает",
    "disabled": "⛔ Отключён в Cardinal",
    "restart_install": "⏳ Установлен на диск (нужен рестарт)",
    "restart_update": "⏳ Обновление на диске (нужен рестарт)",
    "restart_delete": "🗑 Удалён с диска (нужен рестарт)",
    "manual": "🔧 Установлен вручную (вне Hub)",
    "broken_files": "⚠️ Лоадер отсутствует на диске",
    "installed": "📦 Установлен",
    "not_installed": "⬜ Не установлен",
}

_LIST_ICONS: dict[str, str] = {
    "active": "✅",
    "disabled": "⛔",
    "restart_install": "⏳",
    "restart_update": "⏳",
    "restart_delete": "🗑",
    "manual": "🔧",
    "broken_files": "⚠️",
    "installed": "📦",
    "not_installed": "⬜",
}

_INSTALLED_STATUSES = frozenset({
    "active",
    "disabled",
    "restart_install",
    "restart_update",
    "installed",
    "broken_files",
})


def esc(value: Any) -> str:
    """Escape dynamic text for Telegram HTML parse mode."""
    return html.escape(str(value), quote=True)


def build_kb_rows(rows: list[list[tuple[str, str, Optional[str]]]]) -> InlineKeyboardMarkup:
    """Construct an InlineKeyboardMarkup from multi-button rows of (text, cb, url)."""
    markup = InlineKeyboardMarkup()
    for row in rows:
        buttons = [
            InlineKeyboardButton(text, url=url)
            if url
            else InlineKeyboardButton(text, callback_data=cb_data)
            for text, cb_data, url in row
        ]
        if buttons:
            markup.row(*buttons)
    return markup


def build_kb(*rows: tuple[str, str, Optional[str]]) -> InlineKeyboardMarkup:
    """Construct a single-column InlineKeyboardMarkup from rows of (text, cb, url)."""
    return build_kb_rows([[row] for row in rows])


def _format_commands_hint(commands: list[tuple[str, str]]) -> str:
    if not commands:
        return ""
    return ", ".join(f"/{cmd.lstrip('/')}" for cmd, _ in commands)


def _catalog_button_label(state: dict[str, Any]) -> str:
    name = str(state.get("name") or state.get("id") or "")
    code = str(state.get("status_code") or "not_installed")
    latest = str(state.get("latest_version") or "")
    inst_ver = str(state.get("installed_version") or state.get("loaded_version") or "")

    if code in ("active", "installed") and state.get("has_update"):
        return f"🔄 {name} · {inst_ver} → {latest}"
    if code == "active":
        ver = str(state.get("loaded_version") or inst_ver)
        return f"✅ {name} · {ver}" if ver else f"✅ {name}"
    if code == "disabled":
        return f"⛔ {name} · выкл"
    if code in ("restart_install", "restart_update"):
        return f"⏳ {name} · рестарт"
    if code == "restart_delete":
        return f"🗑 {name} · рестарт"
    if code == "manual":
        return f"🔧 {name} · вручную"
    if code == "broken_files":
        return f"⚠️ {name} · повреждён"
    if code == "installed":
        return f"📦 {name} · {inst_ver}" if inst_ver else f"📦 {name}"
    if state.get("is_free"):
        return f"📦 {name} · Бесплатно"
    if state.get("has_key"):
        return f"🔑 {name} · Ключ есть"
    return f"🔒 {name}"


def _format_catalog_summary_line(state: dict[str, Any]) -> str:
    name = esc(state.get("name") or state.get("id") or "")
    code = str(state.get("status_code") or "not_installed")
    status_label = _STATUS_LABELS.get(code, code)
    inst_ver = state.get("installed_version") or state.get("loaded_version")
    latest = state.get("latest_version") or ""
    ver_note = f" (<code>{esc(inst_ver)}</code>)" if inst_ver else ""
    if state.get("has_update") and inst_ver and latest:
        ver_note = f" (<code>{esc(inst_ver)}</code> → <b>{esc(latest)}</b>)"

    meta_parts: list[str] = ["🆓 Бесплатный" if state.get("is_free") else ("🔑 Ключ есть" if state.get("has_key") else "🔒 По ключу")]
    if latest and not inst_ver:
        meta_parts.append(f"последн. <code>{esc(latest)}</code>")
    cmds = _format_commands_hint(state.get("commands") or [])
    if cmds:
        meta_parts.append(esc(cmds))
    if code == "manual" and state.get("manual_files"):
        names = ", ".join(esc(f.get("filename", "")) for f in state["manual_files"][:2])
        if names:
            meta_parts.append(names)

    return f"• <b>{name}</b> — {status_label}{ver_note}\n  <i>{' · '.join(meta_parts)}</i>"


def render_main_menu(
    plugin_states: list[dict[str, Any]],
    sys_info: dict[str, str],
    notify_updates: bool,
    hub_latest_version: str,
    has_hub_update: bool,
    hub_pending_version: Optional[str],
    keys_count: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render main Hub dashboard text and interactive inline keyboard."""
    installed_states = [s for s in plugin_states if s.get("installed_entry") is not None]
    active_count = sum(1 for s in plugin_states if s.get("status_code") == "active")
    restart_states = [s for s in plugin_states if s.get("needs_restart")]
    update_states = [s for s in plugin_states if s.get("has_update") and not s.get("needs_restart")]

    alerts: list[str] = []
    for st in update_states:
        alerts.append(
            f"🚨 Доступно обновление <b>{esc(st.get('latest_version', ''))}</b> "
            f"для <b>{esc(st.get('name', st.get('id', '')))}</b>."
        )
    if hub_pending_version:
        alerts.append(
            f"⏳ Скачано обновление W1bbex HUB <b>v{esc(hub_pending_version)}</b> — перезагрузите Cardinal."
        )
    elif has_hub_update:
        alerts.append(f"🚨 Доступно обновление <b>v{esc(hub_latest_version)}</b> для W1bbex HUB.")

    if restart_states:
        pending_names = ", ".join(esc(s.get("name", s.get("id", ""))) for s in restart_states)
        alerts.append(f"⏳ Ожидают перезапуска: <b>{pending_names}</b>.")

    alerts_block = ("\n".join(alerts) + "\n\n") if alerts else "✅ <i>Все установленные компоненты актуальны.</i>\n\n"

    visible_modules = [
        s for s in plugin_states if s.get("installed_entry") is not None or s.get("status_code") in ("manual", "restart_delete")
    ]
    if visible_modules:
        module_lines = ["<b>Модули:</b>"]
        for st in visible_modules[:8]:
            icon = _LIST_ICONS.get(str(st.get("status_code")), "📦")
            ver = st.get("installed_version") or st.get("loaded_version")
            ver_txt = f" <code>{esc(ver)}</code>" if ver else ""
            cmds = _format_commands_hint(st.get("commands") or [])
            cmd_txt = f" — {esc(cmds)}" if cmds else ""
            module_lines.append(f"• {icon} <b>{esc(st.get('name', st.get('id', '')))}</b>{ver_txt}{cmd_txt}")
        modules_block = "\n".join(module_lines) + "\n\n"
    else:
        modules_block = "<i>Модули из каталога пока не установлены — откройте «Плагины».</i>\n\n"

    notify_state = "вкл" if notify_updates else "выкл"
    notify_icon = "🔔" if notify_updates else "🔕"
    stats_block = (
        f"<blockquote>"
        f"📦 Плагины: <b>{len(installed_states)}</b> из <b>{len(plugin_states)}</b> (активны: <b>{active_count}</b>)\n"
        f"🔑 Ключей: <b>{keys_count}</b>  •  {notify_icon} Уведомления: <b>{notify_state}</b>\n"
        f"🖥 {esc(sys_info.get('os', ''))} ({esc(sys_info.get('arch', ''))})  •  🐍 Python {esc(sys_info.get('python_version', ''))}"
        f"</blockquote>"
    )

    rows: list[list[tuple[str, str, Optional[str]]]] = []
    if hub_pending_version:
        rows.append([(f"🔄 Перезагрузить (Hub v{hub_pending_version})", "hub:restart:hub", None)])
    elif has_hub_update:
        rows.append([(f"[UPDATE] 👨‍💻 W1bbex HUB (v{hub_latest_version})", "hub:update_hub", None)])

    if restart_states and not hub_pending_version:
        rows.append([("🔄 Перезагрузить Cardinal", "hub:restart:general", None)])

    rows.append([(f"🔌 Плагины ({len(installed_states)}/{len(plugin_states)})", "hub:plugins_menu", None)])
    rows.append([
        ("🔑 Активировать ключ", "hub:activate_key", None),
        ("🪪 Лицензии", "hub:licenses", None),
    ])
    rows.append([
        (f"{notify_icon} Уведомления", "hub:toggle_notify", None),
        ("📚 Ресурсы", "hub:resources", None),
    ])

    text = f"👨‍💻 <b>W1bbex Hub v{esc(VERSION)}</b>\n━━━━━━━━━━━━━━━━━━━━━\n{alerts_block}{modules_block}{stats_block}"
    return text, build_kb_rows(rows)


def render_plugins_menu(plugin_states: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    """Render catalog list with live installation, restart, and update statuses."""
    rows: list[list[tuple[str, str, Optional[str]]]] = []
    any_restart = any(st.get("needs_restart") for st in plugin_states)

    if plugin_states:
        lines = [_format_catalog_summary_line(st) for st in plugin_states]
        body = "\n\n".join(lines)
        for st in plugin_states:
            pid = str(st.get("id") or "")
            rows.append([(_catalog_button_label(st), f"hub:info:{pid}", None)])
    else:
        body = "<i>Каталог плагинов временно пуст или не удалось связаться с сервером.</i>"

    if any_restart:
        rows.append([("🔄 Перезагрузить Cardinal сейчас", "hub:restart:general", None)])

    rows.append([
        ("🔄 Обновить список", "hub:plugins_refresh", None),
        ("🏠 В меню", "hub:main", None),
    ])
    text = f"🧩 <b>Каталог плагинов W1bbex</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n{body}"
    return text, build_kb_rows(rows)


def render_plugin_info(state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Render detailed plugin card with live state, quick-open commands, and actions."""
    pid = str(state.get("id") or "")
    name = str(state.get("name") or pid)
    code = str(state.get("status_code") or "not_installed")
    status_label = _STATUS_LABELS.get(code, code)
    is_free = bool(state.get("is_free", False))
    has_key = bool(state.get("has_key", False))
    masked_key = str(state.get("masked_key") or "")
    latest = str(state.get("latest_version") or "v1.0")
    inst_ver = state.get("installed_version")
    loaded_ver = state.get("loaded_version")
    loaded_uuid = state.get("loaded_uuid")
    commands: list[tuple[str, str]] = state.get("commands") or []

    header_lines = [
        f"🧩 <b>{esc(name)}</b>",
        f"• Статус: <b>{status_label}</b>",
    ]
    if inst_ver and loaded_ver and inst_ver != loaded_ver:
        header_lines.append(f"• Версия: в памяти <code>{esc(loaded_ver)}</code> → на диске <b>{esc(inst_ver)}</b>")
    elif inst_ver:
        header_lines.append(f"• Установлена: <code>{esc(inst_ver)}</code> (последняя: <code>{esc(latest)}</code>)")
    elif loaded_ver:
        header_lines.append(f"• В памяти: <code>{esc(loaded_ver)}</code> (в каталоге: <code>{esc(latest)}</code>)")
    else:
        header_lines.append(f"• Версия в каталоге: <code>{esc(latest)}</code>")

    if is_free:
        header_lines.append("• Лицензия: <b>🆓 Бесплатный плагин</b>")
    elif has_key:
        header_lines.append(f"• Ключ: <code>{esc(masked_key)}</code>")
    else:
        header_lines.append("• Лицензия: 🔒 Требуется ключ или Trial (7 дней)")

    if commands and code == "active":
        cmd_list = ", ".join(f"/{esc(cmd.lstrip('/'))}" for cmd, _ in commands)
        header_lines.append(f"• Команды: {cmd_list}")

    if state.get("manual_files"):
        names = ", ".join(f"<code>{esc(f.get('filename', ''))}</code>" for f in state["manual_files"][:3])
        header_lines.append(f"• В папке plugins/: {names} <i>(будет перенесён в архив при установке)</i>")

    rows: list[list[tuple[str, str, Optional[str]]]] = []
    if code == "active":
        if commands:
            primary_cmd = commands[0][0].lstrip("/")
            rows.append([(f"🚀 Открыть {name} (/{primary_cmd})", f"hub:open:{pid}", None)])
        elif state.get("settings_page") and loaded_uuid:
            rows.append([(f"⚙️ Настройки {name}", f"47:{loaded_uuid}:0", None)])
        if loaded_uuid:
            rows.append([("⚙️ В настройках Cardinal", f"45:{loaded_uuid}:0", None)])
    elif code == "disabled" and loaded_uuid:
        rows.append([("⚙️ Включить в настройках Cardinal", f"45:{loaded_uuid}:0", None)])

    if state.get("needs_restart"):
        r_action = str(state.get("restart_action") or "general")
        rows.append([("🔄 Перезагрузить сейчас", f"hub:restart:{r_action}:{pid}", None)])

    if code in _INSTALLED_STATUSES:
        primary_action = (
            (f"⬆️ Обновить до {latest}", f"hub:install:{pid}", None)
            if state.get("has_update")
            else ("🔁 Переустановить", f"hub:install:{pid}", None)
        )
        rows.append([primary_action, ("🗑 Удалить", f"hub:delete:{pid}", None)])
    else:
        install_label = "🔄 Перевести на Hub" if code == "manual" else "⬇️ Установить"
        rows.append([(install_label, f"hub:install:{pid}", None)])
        if not is_free and not has_key:
            rows.append([("🎁 Получить Trial (7 дней)", f"hub:trial:{pid}", None)])

    rows.append([
        ("⬅️ К плагинам", "hub:plugins_menu", None),
        ("🏠 В меню", "hub:main", None),
    ])

    desc = str(state.get("description") or "").strip()
    body = f"{chr(10).join(header_lines)}\n━━━━━━━━━━━━━━━━━━━━━\n\n{desc}" if desc else "\n".join(header_lines)
    return body, build_kb_rows(rows)


def render_install_result(
    plugin_id: str,
    plugin_label: str,
    success: bool,
    message: str,
    is_update: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render install/update outcome with immediate restart callback and Hub navigation."""
    if success:
        action = "update" if is_update else "install"
        verb = "обновлён" if is_update else "установлен"
        text = (
            f"✅ <b>{esc(plugin_label)} {verb} на диск!</b>\n\n"
            f"{esc(message)}\n\n"
            "Нажмите <b>«Перезагрузить сейчас»</b> — после запуска Cardinal я сразу проверю статус "
            "и верну меню с быстрым запуском плагина."
        )
        rows = [
            [("🔄 Перезагрузить сейчас", f"hub:restart:{action}:{plugin_id}", None)],
            [
                ("📦 Карточка плагина", f"hub:info:{plugin_id}", None),
                ("🔌 Каталог", "hub:plugins_menu", None),
            ],
            [("🏠 В главное меню", "hub:main", None)],
        ]
    else:
        text = f"❌ <b>Ошибка загрузки {esc(plugin_label)}:</b>\n{esc(message)}"
        rows = [
            [
                ("🔁 Попробовать снова", f"hub:install:{plugin_id}", None),
                ("📦 К плагину", f"hub:info:{plugin_id}", None),
            ],
            [("🏠 В главное меню", "hub:main", None)],
        ]
    return text, build_kb_rows(rows)


def render_delete_result(
    result: dict[str, Any],
    was_loaded_in_ram: bool,
    had_storage_dir: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render transparent breakdown of deleted files, kept storage, and RAM state."""
    pid = str(result.get("plugin_id") or "")
    pname = str(result.get("plugin_name") or pid)
    stub_name = str(result.get("stub_name") or "")
    bin_dir_name = str(result.get("bin_dir_name") or "")

    lines = [f"🗑 <b>Удаление {esc(pname)} выполнено:</b>", ""]
    if stub_name:
        lines.append(f"• ✅ Лоадер <code>plugins/{esc(stub_name)}</code> — удалён")
    if bin_dir_name:
        if result.get("bin_deferred"):
            lines.append(f"• ⏳ Папка <code>plugins/w1bbex/{esc(bin_dir_name)}/</code> — занята процессом, удалится при перезапуске")
        else:
            lines.append(f"• ✅ Папка <code>plugins/w1bbex/{esc(bin_dir_name)}/</code> — удалена с диска")
    if had_storage_dir:
        lines.append(f"• 💾 Данные <code>storage/w1bbex/{esc(pname)}/</code> — сохранены (не затрагиваются при удалении)")
    if result.get("kept_key"):
        lines.append("• 🔑 Лицензионный ключ — сохранён в Hub")
    if was_loaded_in_ram:
        lines.append("\n⚡ <i>Плагин ещё остаётся в оперативной памяти до перезапуска Cardinal.</i>")

    rows = [
        [("🔄 Перезагрузить сейчас", f"hub:restart:delete:{pid}", None)],
        [
            ("📦 Карточка плагина", f"hub:info:{pid}", None),
            ("🔌 Каталог", "hub:plugins_menu", None),
        ],
        [("🏠 В главное меню", "hub:main", None)],
    ]
    return "\n".join(lines), build_kb_rows(rows)


def render_hub_update_result(target_version: Optional[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Render Hub self-update completion screen with immediate restart button."""
    ver_note = f" (<b>v{esc(target_version)}</b>)" if target_version else ""
    text = (
        f"✅ <b>W1bbex Hub успешно скачан{ver_note}!</b>\n\n"
        "Новый файл уже записан на диск. Перезагрузите Cardinal, чтобы запустить обновлённый Hub."
    )
    rows = [
        [("🔄 Перезагрузить сейчас", "hub:restart:hub", None)],
        [("🏠 В главное меню", "hub:main", None)],
    ]
    return text, build_kb_rows(rows)


def render_resources() -> tuple[str, InlineKeyboardMarkup]:
    """Render useful links screen including W1bbex GitHub, channel, and support."""
    text = (
        "📚 <b>Полезные ресурсы W1bbex</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        "• <b>GitHub:</b> исходный код плагинов и документация\n"
        "• <b>Канал плагинов:</b> релизы, инструкции и новости\n"
        "• <b>Поддержка:</b> помощь по плагинам и ключам"
    )
    rows: list[list[tuple[str, str, Optional[str]]]] = [
        [("💻 GitHub", "", GITHUB_URL), ("📢 Канал плагинов", "", DEFAULT_CHANNEL_URL)],
        [("🛠 Поддержка", "", SUPPORT_URL)],
        [("🏠 В главное меню", "hub:main", None)],
    ]
    return text, build_kb_rows(rows)


def render_post_restart_report(
    pending: dict[str, Any],
    state: Optional[dict[str, Any]],
    running_hub_version: str,
    disk_still_exists: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render verified post-restart summary with immediate plugin interaction and Hub return."""
    action = str(pending.get("action") or "general")
    pid = str(pending.get("plugin_id") or "")
    pname = str(pending.get("plugin_name") or (state.get("name") if state else "") or pid)
    target_ver = str(pending.get("target_version") or "")
    rows: list[list[tuple[str, str, Optional[str]]]] = []

    if action == "hub":
        if target_ver and target_ver != running_hub_version and pending.get("hub_mismatch"):
            text = (
                f"⚠️ <b>Cardinal перезапущен</b>\n\n"
                f"Ожидалась версия Hub <code>v{esc(target_ver)}</code>, "
                f"но в памяти запущена <code>v{esc(running_hub_version)}</code>."
            )
        else:
            text = (
                f"✅ <b>W1bbex Hub успешно обновлён до v{esc(running_hub_version)}!</b>\n\n"
                "Все функции, каталог и уведомления готовы к работе."
            )
        rows.append([("🏠 Открыть W1bbex Hub", "hub:main", None), ("🔌 Плагины", "hub:plugins_menu", None)])
        return text, build_kb_rows(rows)

    if action == "delete":
        still_loaded = bool(state and state.get("loaded_uuid"))
        if not still_loaded and not disk_still_exists:
            text = (
                f"✅ <b>Cardinal перезапущен — {esc(pname)} полностью удалён!</b>\n\n"
                "Плагин выгружен из памяти и очищен с диска.\n"
                "<i>Пользовательские настройки в storage/w1bbex/ не удалялись.</i>"
            )
        else:
            details: list[str] = []
            if still_loaded:
                details.append("• Плагин всё ещё присутствует в <code>cardinal.plugins</code> (возможно, есть дубль в <code>plugins/</code>).")
            if disk_still_exists:
                details.append("• Остались незавершённые файлы на диске.")
            text = (
                f"⚠️ <b>Cardinal перезапущен</b>, но удаление <b>{esc(pname)}</b> завершилось не полностью:\n\n"
                + "\n".join(details)
            )
        rows.append([("🔌 Каталог плагинов", "hub:plugins_menu", None), ("🏠 В меню Hub", "hub:main", None)])
        return text, build_kb_rows(rows)

    if action in ("install", "update") and state:
        code = str(state.get("status_code") or "")
        loaded_uuid = state.get("loaded_uuid")
        loaded_ver = str(state.get("loaded_version") or state.get("installed_version") or "")
        commands: list[tuple[str, str]] = state.get("commands") or []

        if code == "active":
            cmd_lines = "".join(
                f"\n• /{esc(cmd.lstrip('/'))} — <i>{esc(desc)}</i>"
                for cmd, desc in commands
            )
            cmd_section = f"\n\n<b>Доступные команды:</b>{cmd_lines}" if cmd_lines else ""
            text = (
                f"✅ <b>Cardinal перезапущен — {esc(pname)} (v{esc(loaded_ver)}) готов к работе!</b>"
                f"{cmd_section}\n\n"
                "Можете открыть интерфейс плагина прямо отсюда или вернуться в Hub:"
            )
            if commands:
                primary_cmd = commands[0][0].lstrip("/")
                rows.append([(f"🚀 Открыть {pname} (/{primary_cmd})", f"hub:open:{pid}", None)])
            elif state.get("settings_page") and loaded_uuid:
                rows.append([(f"⚙️ Открыть настройки {pname}", f"47:{loaded_uuid}:0", None)])
            if loaded_uuid:
                rows.append([("⚙️ В настройках Cardinal", f"45:{loaded_uuid}:0", None)])
        elif code == "disabled":
            text = (
                f"⛔ <b>Cardinal перезапущен — {esc(pname)} (v{esc(loaded_ver)}) загружен, но отключён.</b>\n\n"
                "Включите его в настройках плагинов Cardinal, чтобы команды заработали."
            )
            if loaded_uuid:
                rows.append([("⚙️ Включить в Cardinal", f"45:{loaded_uuid}:0", None)])
        else:
            text = (
                f"⚠️ <b>Cardinal перезапущен, но {esc(pname)} не запущен ({_STATUS_LABELS.get(code, esc(code))}).</b>\n\n"
                "Проверьте логи Cardinal — возможно, не хватает зависимостей или произошла ошибка загрузки."
            )

        rows.append([("📦 Карточка плагина", f"hub:info:{pid}", None), ("🔌 Плагины", "hub:plugins_menu", None)])
        rows.append([("🏠 Главное меню Hub", "hub:main", None)])
        return text, build_kb_rows(rows)

    text = f"✅ <b>FunPay Cardinal успешно перезагружен!</b>\n\nW1bbex Hub <code>v{esc(running_hub_version)}</code> активен."
    rows.append([("🔌 Каталог плагинов", "hub:plugins_menu", None), ("🏠 Главное меню Hub", "hub:main", None)])
    return text, build_kb_rows(rows)


def render_licenses(keys: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Render saved license keys screen."""
    if not keys:
        text = "У вас пока нет сохраненных лицензий."
    else:
        lines = [f"📦 <b>{esc(k)}</b>: <code>{esc(v)}</code>" for k, v in keys.items()]
        text = "<b>Ваши лицензии:</b>\n\n" + "\n".join(lines)
    return text, build_kb(("🏠 В меню", "hub:main", None))


def render_trial_prompt(plugin_info: dict[str, Any], plugin_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Render channel subscription prompt for requesting a trial key."""
    channel_url = str(plugin_info.get("channel_url") or DEFAULT_CHANNEL_URL)
    kb = build_kb(
        ("📢 Подписаться на канал", "", channel_url),
        ("✅ Я подписался — выдать Trial", f"hub:trial_confirm:{plugin_id}", None),
        ("🔙 Назад", f"hub:info:{plugin_id}", None),
    )
    text = (
        f"🎁 <b>Trial {esc(plugin_info.get('name', plugin_id))}</b>\n\n"
        "Для получения бесплатного триала на <b>7 дней</b> необходимо:\n\n"
        f"📍 Подписаться на канал: {esc(channel_url)}\n\nПосле подписки нажмите кнопку ниже:"
    )
    return text, kb


def render_update_notification(update: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Render background plugin update notification message and actions."""
    urg_texts = {
        "high": "🔴 Срочное обновление",
        "medium": "🟡 Важное обновление",
        "low": "🟢 Не важное обновление",
    }
    pid = str(update["id"])
    pname = str(update["name"])
    latest = str(update["latest"])
    curr_ver = str(update["curr_ver"])
    changelog = esc(update.get("changelog") or "Без описания")
    update_key = str(update["update_key"])

    text = (
        f"🔔 <b>Доступно обновление для {esc(pname)}!</b>\n\n"
        f"{urg_texts.get(str(update.get('urgency', 'medium')), '🟡 Важное обновление')}\n"
        f"Новая версия: <b>{esc(latest)}</b>\nВаша версия: <b>{esc(curr_ver)}</b>\n\n"
        f"<b>Что нового:</b>\n<blockquote>{changelog}</blockquote>"
    )
    kb = build_kb(
        (f"🔄 Обновить {pname}", f"hub:install:{pid}", None),
        ("⏳ Напомнить завтра", f"hub:remind:{update_key}", None),
        ("🔕 Выключить уведомления Hub'a", "hub:toggle_notify_from_msg", None),
    )
    return text, kb
