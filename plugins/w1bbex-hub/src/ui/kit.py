"""Reusable Telegram UI primitives: styled buttons, keyboards, HTML escaping, and card builders."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import html
from html.parser import HTMLParser
import threading
from typing import Any, Iterable, Literal, Optional, Sequence

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .icons import clean_button_text, icon_id

ButtonStyle = Literal["primary", "success", "danger"]
VALID_BUTTON_STYLES: frozenset[str] = frozenset({"primary", "success", "danger"})
_MAX_CALLBACK_BYTES = 64
_COMPACT_CALLBACK_PREFIX = "hub:cb:"
_COMPACT_CALLBACK_LIMIT = 512
_COMPACT_CALLBACK_LOCK = threading.Lock()
_COMPACT_CALLBACKS: dict[str, str] = {}


def compact_callback_data(callback_data: Optional[str]) -> Optional[str]:
    """Tokenize callback_data only when UTF-8 length exceeds Telegram's 64-byte limit."""
    if not isinstance(callback_data, str):
        return callback_data
    if len(callback_data.encode("utf-8")) <= _MAX_CALLBACK_BYTES:
        return callback_data
    token = f"{_COMPACT_CALLBACK_PREFIX}{hashlib.sha256(callback_data.encode('utf-8')).hexdigest()[:16]}"
    with _COMPACT_CALLBACK_LOCK:
        _COMPACT_CALLBACKS[token] = callback_data
        while len(_COMPACT_CALLBACKS) > _COMPACT_CALLBACK_LIMIT:
            _COMPACT_CALLBACKS.pop(next(iter(_COMPACT_CALLBACKS)))
    return token


def resolve_callback_data(callback_data: Optional[str]) -> Optional[str]:
    """Resolve a tokenized callback string back to its original full callback_data."""
    if not isinstance(callback_data, str) or not callback_data.startswith(_COMPACT_CALLBACK_PREFIX):
        return callback_data
    with _COMPACT_CALLBACK_LOCK:
        return _COMPACT_CALLBACKS.get(callback_data, callback_data)


def esc(value: Any) -> str:
    """Escape dynamic text safely for Telegram HTML parse mode."""
    return html.escape(str(value), quote=True)


def bold(value: Any) -> str:
    """Wrap escaped text in HTML bold tags."""
    return f"<b>{esc(value)}</b>"


def code(value: Any) -> str:
    """Wrap escaped text in inline monospace HTML tags."""
    return f"<code>{esc(value)}</code>"


def italic(value: Any) -> str:
    """Wrap escaped text in HTML italic tags."""
    return f"<i>{esc(value)}</i>"


def link(url: str, label: str) -> str:
    """Render an HTML hyperlink with escaped URL and label."""
    return f'<a href="{esc(url)}">{esc(label)}</a>'


_TG_ALLOWED_HTML_TAGS: frozenset[str] = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "blockquote",
        "tg-spoiler",
        "tg-emoji",
        "a",
    }
)
_TG_SAFE_URL_PREFIXES: tuple[str, ...] = ("http://", "https://", "tg://")
_STD_HTML_ENTITIES: frozenset[str] = frozenset({"lt", "gt", "amp", "quot"})


class _TelegramHTMLSanitizer(HTMLParser):
    """Sanitize server-provided descriptions into safe, balanced Telegram HTML."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self._source = source
        self._line_starts: list[int] = [0]
        for idx, ch in enumerate(source):
            if ch == "\n":
                self._line_starts.append(idx + 1)
        self._out: list[str] = []
        # Stack entries: (tag_lower, was_emitted)
        self._stack: list[tuple[str, bool]] = []

    def _raw_endtag(self, tag: str) -> str:
        lineno, col = self.getpos()
        if 1 <= lineno <= len(self._line_starts):
            start = self._line_starts[lineno - 1] + col
            end = self._source.find(">", start)
            if end != -1:
                return self._source[start : end + 1]
        return f"</{tag}>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lower = tag.lower()
        raw_tag = self.get_starttag_text() or f"<{tag}>"
        if lower not in _TG_ALLOWED_HTML_TAGS:
            self._out.append(esc(raw_tag))
            return

        if lower == "blockquote":
            if any(open_tag == "blockquote" and emitted for open_tag, emitted in self._stack):
                self._stack.append(("blockquote", False))
                return
            expandable = any(k.lower() == "expandable" for k, _ in attrs)
            self._out.append("<blockquote expandable>" if expandable else "<blockquote>")
            self._stack.append(("blockquote", True))
            return

        if lower == "tg-emoji":
            emoji_id = next((v for k, v in attrs if k.lower() == "emoji-id" and isinstance(v, str)), "")
            if len(attrs) == 1 and emoji_id.isascii() and emoji_id.isdigit() and len(emoji_id) <= 32:
                self._out.append(f'<tg-emoji emoji-id="{emoji_id}">')
                self._stack.append((lower, True))
            else:
                self._out.append(esc(raw_tag))
                self._stack.append((lower, False))
            return

        if lower == "a":
            href = next((v for k, v in attrs if k.lower() == "href" and isinstance(v, str)), "")
            href_clean = href.strip()
            if href_clean.lower().startswith(_TG_SAFE_URL_PREFIXES):
                self._out.append(f'<a href="{esc(href_clean)}">')
                self._stack.append(("a", True))
            else:
                self._out.append(esc(raw_tag))
                self._stack.append(("a", False))
            return

        self._out.append(f"<{lower}>")
        self._stack.append((lower, True))

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        match_idx = -1
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx][0] == lower:
                match_idx = idx
                break
        if match_idx == -1:
            self._out.append(esc(self._raw_endtag(tag)))
            return

        while len(self._stack) - 1 > match_idx:
            unclosed_tag, emitted = self._stack.pop()
            if emitted:
                self._out.append(f"</{unclosed_tag}>")

        matched_tag, emitted = self._stack.pop()
        if emitted:
            self._out.append(f"</{matched_tag}>")
        elif matched_tag != "blockquote":
            self._out.append(esc(self._raw_endtag(tag)))

    def handle_data(self, data: str) -> None:
        self._out.append(esc(data))

    def handle_entityref(self, name: str) -> None:
        if name in _STD_HTML_ENTITIES:
            self._out.append(f"&{name};")
        else:
            self._out.append(f"&amp;{esc(name)};")

    def handle_charref(self, name: str) -> None:
        self._out.append(f"&#{esc(name)};")

    def result(self) -> str:
        while self._stack:
            unclosed_tag, emitted = self._stack.pop()
            if emitted:
                self._out.append(f"</{unclosed_tag}>")
        return "".join(self._out)


def sanitize_telegram_html(raw: Any) -> str:
    """Sanitize server-supplied description into safe Telegram HTML, escaping unknown tags."""
    text = str(raw or "").strip()
    if not text:
        return ""
    parser = _TelegramHTMLSanitizer(text)
    parser.feed(text)
    parser.close()
    return parser.result()


class StyledButton(InlineKeyboardButton):
    """InlineKeyboardButton subclass preserving Telegram Bot API `style` field in JSON payloads."""

    def __init__(
        self,
        text: str,
        callback_data: Optional[str] = None,
        url: Optional[str] = None,
        style: Optional[ButtonStyle] = None,
        icon_custom_emoji_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        normalized_style = style if style in VALID_BUTTON_STYLES else None
        super().__init__(text=text, callback_data=callback_data, url=url, **kwargs)
        self.style: Optional[str] = normalized_style
        self.icon_custom_emoji_id = icon_custom_emoji_id

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.style:
            payload["style"] = self.style
        if self.icon_custom_emoji_id:
            payload["icon_custom_emoji_id"] = self.icon_custom_emoji_id
        return payload


@dataclass(frozen=True)
class Btn:
    """Declarative inline button specification with optional semantic Bot API style."""

    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None
    style: Optional[ButtonStyle] = None
    icon: Optional[str] = None

    def to_button(self) -> InlineKeyboardButton:
        """Materialize a pyTelegramBotAPI InlineKeyboardButton preserving `style` when set."""
        cb_data = compact_callback_data(self.callback_data)
        if self.style in VALID_BUTTON_STYLES or self.icon:
            return StyledButton(
                text=clean_button_text(self.text),
                callback_data=cb_data,
                url=self.url,
                style=self.style,
                icon_custom_emoji_id=icon_id(self.icon),
            )
        return InlineKeyboardButton(
            text=self.text,
            callback_data=cb_data,
            url=self.url,
        )


class KeyboardBuilder:
    """Fluent inline keyboard builder supporting styled buttons and compact row composition."""

    def __init__(self, max_columns: int = 2) -> None:
        self._max_columns = max(1, max_columns)
        self._rows: list[list[InlineKeyboardButton]] = []

    def row(self, *buttons: Btn) -> KeyboardBuilder:
        """Append a full row of one or more buttons."""
        if buttons:
            self._rows.append([b.to_button() for b in buttons])
        return self

    def add(self, text: str, callback_data: Optional[str] = None, url: Optional[str] = None, style: Optional[ButtonStyle] = None) -> KeyboardBuilder:
        """Append a single full-width button row."""
        return self.row(Btn(text=text, callback_data=callback_data, url=url, style=style))

    def grid(self, buttons: Sequence[Btn], columns: Optional[int] = None) -> KeyboardBuilder:
        """Arrange a sequence of buttons cleanly in rows of `columns` width."""
        width = max(1, columns or self._max_columns)
        for idx in range(0, len(buttons), width):
            self.row(*buttons[idx : idx + width])
        return self

    def extend(self, rows: Iterable[Sequence[Btn]]) -> KeyboardBuilder:
        """Append multiple explicit button rows."""
        for row_btns in rows:
            if row_btns:
                self._rows.append([b.to_button() for b in row_btns])
        return self

    def build(self) -> InlineKeyboardMarkup:
        """Build final InlineKeyboardMarkup ready for send/edit."""
        markup = InlineKeyboardMarkup()
        for row_btns in self._rows:
            markup.row(*row_btns)
        return markup


def has_styled_buttons(markup: Any) -> bool:
    """Return True if any button in an InlineKeyboardMarkup carries a Bot API `style` attribute."""
    keyboard = getattr(markup, "keyboard", None)
    if not keyboard:
        return False
    return any(bool(getattr(btn, "style", None)) for row in keyboard for btn in row)


def strip_button_styles(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Remove unsupported colors while preserving icons and button actions."""
    return _strip_button_fields(markup, ("style",))


def strip_button_icons(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Remove custom emoji without losing semantic button colors."""
    return _strip_button_fields(markup, ("icon_custom_emoji_id",))


def _strip_button_fields(markup: InlineKeyboardMarkup, fields: tuple[str, ...]) -> InlineKeyboardMarkup:
    stripped = InlineKeyboardMarkup()
    for row in getattr(markup, "keyboard", []) or []:
        plain_row = []
        for button in row:
            cloned = copy.copy(button)
            for field in fields:
                setattr(cloned, field, None)
            plain_row.append(cloned)
        if plain_row:
            stripped.row(*plain_row)
    return stripped


def has_custom_button_icons(markup: Any) -> bool:
    return any(
        bool(getattr(button, "icon_custom_emoji_id", None))
        for row in getattr(markup, "keyboard", []) or [] for button in row
    )


def render_tree_lines(items: Sequence[str], indent: str = " ") -> list[str]:
    """Format items as a compact box-drawing hierarchy using ├ and └ connectors."""
    if not items:
        return []
    last_idx = len(items) - 1
    return [
        f"{indent}{'└' if idx == last_idx else '├'} {line}"
        for idx, line in enumerate(items)
    ]


class CardBuilder:
    """Structured Telegram HTML message builder following clean tree hierarchy."""

    def __init__(self, icon: str, heading: str, version_badge: Optional[str] = None, header_suffix: Optional[str] = None) -> None:
        header = f"{icon} <b>{esc(heading)}</b>" if icon else f"<b>{esc(heading)}</b>"
        if version_badge:
            header = f"{header} <code>{esc(version_badge)}</code>"
        if header_suffix:
            header = f"{header} · {header_suffix}"
        self._blocks: list[str] = [header]

    def quote(self, html_body: str, expandable: bool = False) -> CardBuilder:
        """Append a pre-formatted HTML blockquote block when non-empty."""
        stripped = html_body.strip()
        if stripped:
            tag = "<blockquote expandable>" if expandable else "<blockquote>"
            self._blocks.append(f"{tag}{stripped}</blockquote>")
        return self

    def paragraph(self, html_text: str) -> CardBuilder:
        """Append an HTML paragraph block when non-empty."""
        stripped = html_text.strip()
        if stripped:
            self._blocks.append(stripped)
        return self

    def tree(self, title_html: Optional[str], items_html: Sequence[str]) -> CardBuilder:
        """Append a section with optional bold header and ├/└ tree items."""
        cleaned = [item for item in items_html if item]
        if not cleaned:
            return self
        lines: list[str] = []
        if title_html:
            lines.append(title_html)
        lines.extend(render_tree_lines(cleaned))
        self._blocks.append("\n".join(lines))
        return self

    def build(self) -> str:
        """Join non-empty blocks with clean spacing."""
        return "\n\n".join(block for block in self._blocks if block)
