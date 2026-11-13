"""Pure formatting helpers for W1bbex Hub status badges, tree lines, and button captions."""

from __future__ import annotations

from typing import Any, Optional

from .kit import code, esc

STATUS_BADGES: dict[str, tuple[str, str]] = {
    "active": ("✅", "Активен и работает"),
    "disabled": ("⏸", "Отключён в Cardinal"),
    "restart_install": ("🔄", "Ждёт перезапуск · установка"),
    "restart_update": ("🔄", "Ждёт перезапуск · обновление"),
    "restart_delete": ("🔄", "Ждёт перезапуск · удаление"),
    "broken_files": ("⚠️", "Файлы повреждены"),
    "installed": ("📦", "Установлен на диск"),
    "manual": ("🔧", "Установлен вручную"),
    "not_installed": ("☁️", "Не установлен"),
}

SHORT_STATUS_LABELS: dict[str, str] = {
    "active": "активен",
    "disabled": "отключён",
    "restart_install": "ждёт рестарт",
    "restart_update": "ждёт рестарт",
    "restart_delete": "ждёт рестарт",
    "broken_files": "ошибка файлов",
    "installed": "на диске",
    "manual": "вручную",
}


def format_command_lines(commands: list[tuple[str, str]]) -> list[str]:
    """Build clean individual HTML lines for a plugin's command list."""
    if not commands:
        return []
    lines: list[str] = []
    for cmd, desc in commands[:5]:
        normalized = cmd.strip().lstrip("/")
        if not normalized:
            continue
        clean_desc = " ".join(desc.split())
        if clean_desc:
            lines.append(f"/{esc(normalized)} — <i>{esc(clean_desc)}</i>")
        else:
            lines.append(f"/{esc(normalized)}")
    if len(commands) > 5:
        lines.append(f"<i>и ещё {len(commands) - 5}…</i>")
    return lines


def format_primary_command(commands: list[tuple[str, str]]) -> Optional[str]:
    """Return primary slash command preview (e.g. '/autosmm') without noisy auxiliary duplicates."""
    if not commands:
        return None
    cmd = commands[0][0].strip().lstrip("/")
    return f"/{cmd}" if cmd else None


def resolve_display_version(state: dict[str, Any]) -> str:
    """Pick the most informative current version string for display."""
    return str(
        state.get("loaded_version")
        or state.get("installed_version")
        or state.get("latest_version")
        or "1.0"
    )


def format_catalog_line(state: dict[str, Any]) -> str:
    """Render one crisp tree line for a plugin inside the catalog overview."""
    status = str(state.get("status_code") or "not_installed")
    name_html = f"<b>{esc(state.get('name') or state.get('id'))}</b>"
    latest = str(state.get("latest_version") or "1.0")
    primary_cmd = format_primary_command(state.get("commands") or [])
    cmd_part = f" · {esc(primary_cmd)}" if primary_cmd else ""

    if status == "not_installed":
        if state.get("is_free"):
            icon, access = "🆓", "бесплатный"
        elif state.get("has_key"):
            icon, access = "🔑", "ключ сохранён"
        else:
            icon, access = "📦", "по ключу"
        return f"{icon} {name_html} · {code(latest)} — {access}"

    if state.get("has_update") and not state.get("needs_restart"):
        curr = str(state.get("installed_version") or state.get("loaded_version") or "?")
        return f"⬆️ {name_html} · {code(f'{curr} → {latest}')} — есть обновление{cmd_part}"

    icon, _ = STATUS_BADGES.get(status, ("🔹", ""))
    short_label = SHORT_STATUS_LABELS.get(status, status)
    curr_ver = resolve_display_version(state)
    return f"{icon} {name_html} · {code(curr_ver)} — {short_label}{cmd_part}"


def format_catalog_button(state: dict[str, Any]) -> tuple[str, Optional[str]]:
    """Return concise inline button text and semantic Bot API style for a catalog item."""
    name = str(state.get("name") or state.get("id") or "")
    status = str(state.get("status_code") or "not_installed")
    latest = str(state.get("latest_version") or "1.0")
    curr = resolve_display_version(state)

    if state.get("needs_restart"):
        return f"🔄 {name} · рестарт", "success"
    if state.get("has_update"):
        return f"⬆️ {name} · {latest}", "success"
    if status == "broken_files":
        return f"⚠️ {name} · ошибка", "danger"
    if status == "active":
        return f"✅ {name} · {curr}", None
    if status == "disabled":
        return f"⏸ {name} · выкл", None
    if status == "manual":
        return f"🔧 {name} · {curr}", None
    if status == "installed":
        return f"📦 {name} · {curr}", None
    if state.get("has_key"):
        return f"🔑 {name} · готов", "primary"
    if state.get("is_free"):
        return f"🆓 {name} · {latest}", None
    return f"📦 {name} · {latest}", None


def license_summary_text(state: dict[str, Any]) -> str:
    """Render concise HTML license status for a plugin detail card."""
    if state.get("is_free"):
        return "🆓 Бесплатный плагин"
    if state.get("has_key"):
        return f"🔑 Ключ: {code(state.get('masked_key') or '')}"
    return "🔒 Требуется лицензионный ключ"
