from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..models import (
    is_plausible_allowed_domain as _is_plausible_allowed_domain,
    normalize_allowed_domain as _normalize_allowed_domain,
)
from ..storage.storage import load_valid_links

logger = logging.getLogger("FPC.autosmm")


def _extract_username_from_text(text: str) -> Optional[str]:
    t = str(text or "").strip()
    if not t:
        return None
    if "://" in t or "/" in t:
        try:
            parsed = urlparse(t if "://" in t else f"https://{t}")
            path = parsed.path.strip("/")
            parts = [p for p in path.split("/") if p]
            if parts:
                candidate = parts[-1].lstrip("@")
                if re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", candidate):
                    return candidate
        except ValueError as exc:
            logger.debug(f"Некорректный URL при извлечении юзернейма '{t[:60]}': {exc}")
    if t.startswith("@"):
        candidate = t.lstrip("@").strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", candidate):
            return candidate
    if re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", t):
        return t
    return None

def _format_target_for_panel(target: str, lot_data: Optional[Dict]) -> str:
    if not isinstance(lot_data, dict):
        return target
    fmt = lot_data.get("link_format", "as_is")
    if fmt == "clean_username":
        uname = _extract_username_from_text(target)
        if uname:
            return uname
    elif fmt == "username_at":
        uname = _extract_username_from_text(target)
        if uname:
            return f"@{uname}"
    elif fmt == "full_url":
        if target.startswith("@") or ("/" not in target and "://" not in target):
            uname = _extract_username_from_text(target)
            lot_name = str(lot_data.get("name") or "").lower()
            if uname:
                if any(k in lot_name for k in ("телеграм", "тг", "telegram")):
                    return f"https://t.me/{uname}"
                if any(k in lot_name for k in ("инст", "instagram")):
                    return f"https://instagram.com/{uname}"
                if any(k in lot_name for k in ("тик ток", "tiktok")):
                    return f"https://www.tiktok.com/@{uname}"
                if any(k in lot_name for k in ("вк", "vk")):
                    return f"https://vk.com/{uname}"
                return f"https://t.me/{uname}"
    return target


ZERO_WIDTH_INVISIBLE_CHARS = {
    ord("\u200b"): None,
    ord("\u200c"): None,
    ord("\u200d"): None,
    ord("\u2060"): None,
    ord("\ufeff"): None,
    ord("\u180e"): None,
    ord("\u200e"): None,
    ord("\u200f"): None,
    ord("\u202a"): None,
    ord("\u202b"): None,
    ord("\u202c"): None,
    ord("\u202d"): None,
    ord("\u202e"): None,
}
INVALID_LINK_SCHEME_TEXT = """❌ Отправьте ссылку (начинающуюся с https://) или юзернейм (@username)."""
INVALID_LINK_DOMAIN_TEXT = """❌ Ссылка не принята.

Домен этой ссылки не разрешён продавцом.

Проверьте ссылку и отправьте её ещё раз.
Если ссылка верная — напишите продавцу."""
BUYER_LINK_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
BUYER_CONFIRM_YES_WORDS = {"+", "да", "подтвердить", "подтверждаю", "ок", "окей", "го", "давай", "запускай", "верно", "правильно", "всё верно", "все верно"}
BUYER_CONFIRM_NO_WORDS = {"-", "нет", "изменить", "сменить", "другая ссылка"}
BUYER_USERNAME_RE = re.compile(r"(?:^|\s)(@[A-Za-z0-9_.-]{2,64})\b")

def normalize_incoming_text(text: Any) -> str:
    value = str(text or "")
    value = value.translate(ZERO_WIDTH_INVISIBLE_CHARS)
    return value.strip()


def _links_extract_buyer_target(text_value: Any, *, allowed_domains) -> Optional[str]:
    text = normalize_incoming_text(text_value)
    if not text:
        return None
    m_url = BUYER_LINK_RE.search(text)
    if m_url:
        cand = m_url.group(1).strip().rstrip('.,!?;:)"\'>')
        lower_cand = cand.lower()
        if lower_cand.startswith('http://') or lower_cand.startswith('https://'):
            cand = 'https://' + cand.split('://', 1)[1]
        return cand
    for allowed_domain in _provided_links(allowed_domains):
        domain = _normalize_allowed_domain(allowed_domain)
        if not domain:
            continue
        patt = f'(?i)(?:^|\\s)({re.escape(domain)}/\\S+)'
        m_dom = re.search(patt, text)
        if m_dom:
            cand = m_dom.group(1).strip().rstrip('.,!?;:)"\'>')
            return f'https://{cand}'
    m_user = BUYER_USERNAME_RE.search(text)
    if m_user:
        return m_user.group(1).strip()
    words = text.split()
    if len(words) == 1:
        token = words[0].strip().strip('"\'')
        lowered = token.lower()
        if lowered not in BUYER_CONFIRM_YES_WORDS and lowered not in BUYER_CONFIRM_NO_WORDS and (lowered not in {'!смм', '!ссылка', '!отмена', '!команды', '!помощь', 'чек', 'рефилл', 'статус', 'ок', 'да', 'нет'}):
            if re.fullmatch('[A-Za-z0-9_.-]{3,64}', token) and (not token.isdigit()):
                return token
    return None


def _links_is_valid_link(link: str, *, allowed_domains) -> Tuple[bool, str]:
    raw_link = str(link or '').strip()
    if raw_link.lower().startswith('https://'):
        raw_link = 'https://' + raw_link[8:]
    else:
        return (False, INVALID_LINK_SCHEME_TEXT)
    try:
        parsed = urlparse(raw_link)
    except ValueError:
        return (False, '❌ Не удалось прочитать ссылку.')
    hostname = (parsed.hostname or '').strip().lower().strip('.')
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    if not hostname:
        return (False, '❌ Не удалось определить домен ссылки.')
    for allowed_domain in _provided_links(allowed_domains):
        domain = _normalize_allowed_domain(allowed_domain)
        if not domain:
            continue
        if hostname == domain or hostname.endswith('.' + domain):
            return (True, f'✅ Ссылка корректна ({domain}).')
    return (False, INVALID_LINK_DOMAIN_TEXT)


def _provided_links(allowed_domains):
    return allowed_domains()


def _links_extract_http_link(text_value: Any, *, allowed_domains) -> Optional[str]:
    return _links_extract_buyer_target(text_value, allowed_domains=allowed_domains)


def _links_looks_like_link_without_https(text_value: Any, *, allowed_domains) -> bool:
    text = str(text_value or '').strip()
    if not text or _links_extract_buyer_target(text, allowed_domains=allowed_domains):
        return False
    if re.search('(?i)\\bwww\\.[a-z0-9.-]+\\.[a-z]{2,}\\S*', text):
        return True
    if re.search('(?i)\\b[a-z0-9-]+(?:\\.[a-z0-9-]+)+/\\S*', text):
        return True
    return False


def _links_is_valid_buyer_target(target: str, *, allowed_domains) -> Tuple[bool, str]:
    target = str(target or '').strip()
    if not target:
        return (False, '❌ Укажите ссылку или юзернейм.')
    if target.lower().startswith('https://'):
        return _links_is_valid_link('https://' + target[8:], allowed_domains=allowed_domains)
    if target.startswith('@'):
        uname = target[1:].strip()
        if not re.fullmatch('[A-Za-z0-9_.-]{2,64}', uname):
            return (False, '❌ Некорректный формат юзернейма. Разрешены латинские буквы, цифры и символы _ . -')
        return (True, '✅ Юзернейм корректен.')
    if re.fullmatch('[A-Za-z0-9_.-]{3,64}', target):
        return (True, '✅ Юзернейм корректен.')
    return (False, '❌ Ссылка или юзернейм не приняты. Отправьте ссылку (https://...) или юзернейм (@channel).')


def _default_allowed_domains() -> List[str]:
    """Load persisted allowed domains from storage."""
    return load_valid_links()


def _extract_buyer_target(text_value: Any) -> Optional[str]:
    return _links_extract_buyer_target(text_value, allowed_domains=_default_allowed_domains)


def _extract_http_link(text_value: Any) -> Optional[str]:
    return _links_extract_http_link(text_value, allowed_domains=_default_allowed_domains)


def _looks_like_link_without_https(text_value: Any) -> bool:
    return _links_looks_like_link_without_https(text_value, allowed_domains=_default_allowed_domains)


def is_valid_link(link: str) -> Tuple[bool, str]:
    return _links_is_valid_link(link, allowed_domains=_default_allowed_domains)


def is_valid_buyer_target(target: str) -> Tuple[bool, str]:
    return _links_is_valid_buyer_target(target, allowed_domains=_default_allowed_domains)
