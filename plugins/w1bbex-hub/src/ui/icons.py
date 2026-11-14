"""Adaptive icons from the shared w1bbex_icons_by_w1bbex_icons_bot registry."""

from __future__ import annotations

import re

# Telegram requires the matching standard emoji inside each custom emoji entity.
ICONS: dict[str, tuple[str, str]] = {
    "hub": ("5422813535992333007", "🤖"),
    "rocket": ("5458489123490804864", "🚀"),
    "python": ("5458605177802108299", "💻"),
    "linux": ("5422483755518439914", "🐧"),
    "windows": ("5420383490740888463", "🪟"),
    "apps": ("5420629626726689399", "🗂"),
    "plugin": ("5420226299232822020", "🔌"),
    "settings": ("5420219070802864426", "⚙"),
    "back": ("5420414676498424358", "⬅️"),
    "refresh": ("5420621517828435405", "🔄"),
    "check": ("5422357565084314125", "✅"),
    "alert": ("5420301585714554665", "⚠️"),
    "key": ("5422719355949462263", "🔑"),
    "info": ("5420370627313839231", "ℹ️"),
    "trash": ("5420174416027888017", "🗑"),
    "clock": ("5420099516093214877", "🕒"),
    "bell": ("5420489889965715228", "🔔"),
    "bell_off": ("5420136375502547440", "🔕"),
    "link": ("5422467821189770302", "🔗"),
    "download": ("5420446497911121797", "📥"),
    "cross": ("5420325693365987467", "❌"),
}
_CUSTOM_EMOJI = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.DOTALL)
_LEADING_EMOJI = re.compile(r"^[\s\u200d\ufe0f\u2300-\u27ff\U0001f000-\U0001faff]+")


def icon_id(name: str | None) -> str | None:
    return ICONS[name][0] if name in ICONS else None


def tg_icon(name: str) -> str:
    emoji_id, alternate = ICONS[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{alternate}</tg-emoji>'


def without_custom_emojis(text: str) -> str:
    """Keep the interface readable when a bot cannot send custom emojis."""
    return _CUSTOM_EMOJI.sub("", text)


def clean_button_text(text: str) -> str:
    """Avoid duplicate standard emoji prefixes before a custom button icon."""
    return _LEADING_EMOJI.sub("", text).strip()
