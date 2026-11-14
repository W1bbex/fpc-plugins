"""Plain, consistent status captions, safe summaries, and masked license keys."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from typing import Any, Optional

from .kit import code, esc

STATUS_BADGES: dict[str, tuple[str, str]] = {
    "active": ("", "Работает"),
    "disabled": ("", "Отключён в Cardinal"),
    "restart_install": ("", "Установлен · нужен перезапуск"),
    "restart_update": ("", "Обновлён · нужен перезапуск"),
    "restart_delete": ("", "Удалён с диска · нужен перезапуск"),
    "broken_files": ("", "Не найден загрузчик"),
    "installed": ("", "Установлен на диск"),
    "manual": ("", "Установлен вручную"),
    "not_installed": ("", "Не установлен"),
}


class _PlainDescription(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(" ")


def description_preview(raw: Any, limit: int = 100) -> str:
    parser = _PlainDescription()
    parser.feed(str(raw or ""))
    text = " ".join(unescape("".join(parser.parts)).split()) or "Описание пока не добавлено"
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def mask_key(key: Any) -> str:
    value = str(key or "")
    return f"{value[:4]}…{value[-4:]}" if len(value) > 10 else "•" * max(4, len(value))


def format_command_lines(commands: list[tuple[str, str]]) -> list[str]:
    lines = []
    for cmd, desc in commands[:5]:
        name = cmd.strip().lstrip("/")
        if name:
            label = " ".join(desc.split())
            lines.append(f"/{esc(name)} — <i>{esc(label)}</i>" if label else f"/{esc(name)}")
    if len(commands) > 5:
        lines.append(f"<i>и ещё {len(commands) - 5}…</i>")
    return lines


def format_primary_command(commands: list[tuple[str, str]]) -> Optional[str]:
    cmd = commands[0][0].strip().lstrip("/") if commands else ""
    return f"/{cmd}" if cmd else None


def resolve_display_version(state: dict[str, Any]) -> str:
    return str(state.get("loaded_version") or state.get("installed_version") or state.get("latest_version") or "?").lstrip("v")


def catalog_caption(state: dict[str, Any]) -> str:
    if state.get("has_update") and not state.get("needs_restart"):
        return f"Обновление {state.get('latest_version') or ''}"
    if state.get("status_code") == "not_installed":
        return "Бесплатный" if state.get("is_free") else "Ключ сохранён" if state.get("has_key") else "Нужен ключ"
    return STATUS_BADGES.get(str(state.get("status_code")), ("", "Неизвестный статус"))[1]


def format_catalog_line(state: dict[str, Any]) -> str:
    command = format_primary_command(state.get("commands") or [])
    return f"<b>{esc(state.get('name') or state.get('id'))}</b> · {esc(catalog_caption(state))}" + (f" · {esc(command)}" if command else "")


def format_catalog_button(state: dict[str, Any]) -> tuple[str, Optional[str]]:
    return f"{state.get('name') or state.get('id')} · {catalog_caption(state)}", None


def license_summary_text(state: dict[str, Any]) -> str:
    if state.get("is_free"):
        return "Бесплатный плагин"
    if state.get("has_key"):
        return "Ключ сохранён · проверяется при установке"
    return "Для установки нужен лицензионный ключ"
