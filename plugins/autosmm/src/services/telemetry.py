from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import re
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

try:
    from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..storage.storage import STATE_LOCK, _load_autosmm_state, _save_autosmm_state, load_config, update_state_fields

TELEMETRY_URL = "https://api.w1bbex.ru/v1/autosmm/telemetry"
TELEMETRY_PING_INTERVAL_SECONDS = 86400
TELEMETRY_TIMEOUT_SECONDS = 5
TELEMETRY_GUEST_DAYS = 7
TELEMETRY_SECURITY_URL = "https://teletype.in/@w1bbex/AutoSMM-Security"
AUTOSMM_V008_NO_TELEMETRY_URL = "https://t.me/w1bbexplugins/25"

_GUEST_WARN_LOCK = threading.Lock()

def _normalize_funpay_username(value: Any) -> str:
    return str(value or "").strip().lower()

def _normalize_funpay_id(value: Any) -> str:
    return str(value or "").strip()

def _looks_like_funpay_user_id(value: Any) -> bool:
    raw = _normalize_funpay_id(value)
    return raw.isdigit() and 3 <= len(raw) <= 20

def _looks_like_funpay_username(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if len(raw) > 64:
        return False
    if raw.startswith("<") or raw.startswith("{"):
        return False
    return True

def _safe_get_identity_field(obj: Any, field_name: str) -> Any:
    if obj is None:
        return None
    try:
        if isinstance(obj, dict):
            return obj.get(field_name)
        value = getattr(obj, field_name, None)
        if callable(value):
            return None
        return value
    except Exception as exc:
        rt.logger.debug(f"Не удалось прочитать поле identity '{field_name}': {exc}")
        return None

def _extract_identity_pair_from_obj(obj: Any, source_name: str) -> Optional[Dict[str, str]]:
    """Extract a seller FunPay identity only when username and user_id come from the same object."""
    if obj is None:
        return None

    username_value = None
    for field_name in ("username", "user_name", "login", "name"):
        candidate = _safe_get_identity_field(obj, field_name)
        if _looks_like_funpay_username(candidate):
            username_value = candidate
            break

    user_id_value = None
    for field_name in ("user_id", "account_id", "id"):
        candidate = _safe_get_identity_field(obj, field_name)
        if _looks_like_funpay_user_id(candidate):
            user_id_value = candidate
            break

    if not _looks_like_funpay_username(username_value) or not _looks_like_funpay_user_id(user_id_value):
        return None

    return {
        "source": str(source_name or "unknown"),
        "username": _normalize_funpay_username(username_value),
        "user_id": _normalize_funpay_id(user_id_value),
    }

def build_anonymous_install_id(funpay_username: str, funpay_id: str) -> str:
    username = _normalize_funpay_username(funpay_username)
    user_id = _normalize_funpay_id(funpay_id)

    if not username or not user_id:
        raise ValueError("FunPay username and FunPay ID are required for AutoSMM telemetry id")

    raw = f"autosmm:v1:{username}:{user_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def _looks_like_anonymous_install_id(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{16}", str(value or "").strip().lower()))

def _parse_autosmm_dt(value: Any) -> Optional[datetime]:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw_value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _telemetry_get_funpay_identity(c: Optional[Cardinal] = None, *, fallback) -> Tuple[str, str, str]:
    """Return stable seller FunPay identity as (username, user_id, source)."""
    account = getattr(c or fallback, "account", None)
    if account is None:
        return ("", "", "unavailable")
    pair = _extract_identity_pair_from_obj(account, "account")
    if pair:
        return (pair["username"], pair["user_id"], pair["source"])
    return ("", "", "unavailable")


def _set_telemetry_state_fields(**updates: Any) -> None:
    """Atomically apply state field updates in kv_state."""
    update_state_fields(**updates)


def _get_telemetry_consent_state() -> Optional[bool]:
    state = _load_autosmm_state()
    if "telemetry_consent" not in state:
        return None
    return bool(state.get("telemetry_consent"))


def _set_telemetry_consent(value: bool) -> None:
    _set_telemetry_state_fields(telemetry_consent=bool(value))


def _telemetry_consent_accepted() -> bool:
    return _get_telemetry_consent_state() is True


def _telemetry_guest_used() -> bool:
    state = _load_autosmm_state()
    return state.get("telemetry_guest_used") is True


def _get_telemetry_guest_remaining_seconds(
    state: Optional[Dict[str, Any]] = None,
    now_dt: Optional[datetime] = None,
) -> Optional[float]:
    if state is None:
        state = _load_autosmm_state()
    guest_until = _parse_autosmm_dt(state.get("telemetry_guest_until"))
    if not guest_until:
        return None
    return (guest_until - (now_dt or datetime.now())).total_seconds()


def _format_telemetry_guest_remaining(remaining_seconds: float) -> str:
    total_minutes = max(1, int(remaining_seconds // 60))
    days, rem_minutes = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem_minutes, 60)
    if days > 0:
        return f"{days}д {hours}ч" if hours else f"{days}д"
    if hours > 0:
        return f"{hours}ч {minutes}мин" if minutes else f"{hours}ч"
    return f"{minutes}мин"


def _remember_telemetry_chat_id(chat_id: Optional[int]) -> None:
    if chat_id is None:
        return
    try:
        normalized_chat_id = int(chat_id)
    except (TypeError, ValueError):
        return
    with STATE_LOCK:
        state = _load_autosmm_state()
        if state.get("telemetry_guest_chat_id") == normalized_chat_id:
            return
        state["telemetry_guest_chat_id"] = normalized_chat_id
        _save_autosmm_state(state)


def _telemetry_guest_active() -> bool:
    remaining = _get_telemetry_guest_remaining_seconds()
    return bool(remaining is not None and remaining > 0)


def _telemetry_gate_allows_menu() -> bool:
    return _telemetry_consent_accepted() or _telemetry_guest_active()


def _activate_telemetry_guest_7d(
    chat_id: Optional[int] = None,
    now_dt: Optional[datetime] = None,
) -> bool:
    with STATE_LOCK:
        state = _load_autosmm_state()
        if state.get("telemetry_consent") is True:
            return True
        if state.get("telemetry_guest_used") is True:
            return False
        started_dt = now_dt or datetime.now()
        state["telemetry_guest_used"] = True
        state["telemetry_guest_started_at"] = started_dt.isoformat(timespec="seconds")
        state["telemetry_guest_until"] = (
            started_dt + timedelta(days=TELEMETRY_GUEST_DAYS)
        ).isoformat(timespec="seconds")
        state["telemetry_guest_warn_48h_sent_at"] = None
        state["telemetry_guest_warn_12h_sent_at"] = None
        if chat_id is not None:
            try:
                state["telemetry_guest_chat_id"] = int(chat_id)
            except (TypeError, ValueError):
                pass
        _save_autosmm_state(state)
        return True


def _resolve_telemetry_warning_chat_id(c: Optional[Cardinal], state: Dict[str, Any]) -> Optional[int]:
    candidates: List[Any] = [state.get("telemetry_guest_chat_id")]
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if isinstance(cfg, dict):
        candidates.append(cfg.get("notification_chat_id"))
    telegram_obj = getattr(c or rt.cardinal_instance, "telegram", None)
    auth_users = getattr(telegram_obj, "authorized_users", None)
    if isinstance(auth_users, dict):
        candidates.extend(auth_users.keys())
    for raw_id in candidates:
        if raw_id in (None, ""):
            continue
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            continue
    return None


def _build_telemetry_guest_warning_markup() -> Any:
    if InlineKeyboardMarkup is None or InlineKeyboardButton is None:
        return None
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("✅ Согласиться и продолжить", callback_data="asmm:telemetry_accept"),
        InlineKeyboardButton("🛡 Насколько это безопасно?", url=TELEMETRY_SECURITY_URL),
    )
    return kb


def _build_telemetry_guest_warning_text(tier: str, remaining_seconds: float) -> str:
    remaining_label = _format_telemetry_guest_remaining(remaining_seconds)
    header = (
        "🚨 <b>Гостевой режим AutoSMM заканчивается менее чем через 12 часов</b>"
        if tier == "12h"
        else "⏳ <b>Напоминание о гостевом режиме AutoSMM</b>"
    )
    return (
        f"{header}\n\n"
        f"До окончания {TELEMETRY_GUEST_DAYS}-дневного гостевого режима осталось: <b>{remaining_label}</b>.\n\n"
        f"После завершения меню настроек <code>/autosmm</code> попросит принять минимальную "
        f"техническую статистику (или перейти на AutoSMM v0.0.8)."
    )


def _maybe_notify_telemetry_guest_expiring(
    c: Optional[Cardinal] = None,
    now_dt: Optional[datetime] = None,
) -> Optional[str]:
    """Notify admin once at <=48h and <=12h before the 7-day telemetry guest window expires."""
    if not _GUEST_WARN_LOCK.acquire(blocking=False):
        return None

    try:
        ref_dt = now_dt or datetime.now()
        with STATE_LOCK:
            state = _load_autosmm_state()
            if state.get("telemetry_consent") is True or state.get("telemetry_guest_used") is not True:
                return None
            remaining = _get_telemetry_guest_remaining_seconds(state, now_dt=ref_dt)
            if remaining is None or remaining <= 0:
                return None

            tier: Optional[str] = None
            if remaining <= 12 * 3600 and not state.get("telemetry_guest_warn_12h_sent_at"):
                tier = "12h"
            elif 12 * 3600 < remaining <= 48 * 3600 and not state.get("telemetry_guest_warn_48h_sent_at"):
                tier = "48h"
            if not tier:
                return None
            state_snapshot = dict(state)

        bot = rt.bot or getattr(getattr(c or rt.cardinal_instance, "telegram", None), "bot", None)
        target_chat_id = _resolve_telemetry_warning_chat_id(c, state_snapshot) if bot is not None else None
        if bot is None or target_chat_id is None:
            return None

        try:
            bot.send_message(
                target_chat_id,
                _build_telemetry_guest_warning_text(tier, remaining),
                parse_mode="HTML",
                reply_markup=_build_telemetry_guest_warning_markup(),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            rt.logger.warning(f"TELEMETRY GUEST WARNING ({tier}) FAILED: {exc}")
            return None

        sent_at = ref_dt.isoformat(timespec="seconds")
        with STATE_LOCK:
            latest_state = _load_autosmm_state()
            if not latest_state.get("telemetry_guest_warn_48h_sent_at"):
                latest_state["telemetry_guest_warn_48h_sent_at"] = sent_at
            if tier == "12h":
                latest_state["telemetry_guest_warn_12h_sent_at"] = sent_at
            _save_autosmm_state(latest_state)
        return tier
    finally:
        _GUEST_WARN_LOCK.release()


def _get_funpay_identity(c: Optional[Cardinal] = None) -> Tuple[str, str, str]:
    return _telemetry_get_funpay_identity(c, fallback=rt.cardinal_instance)


def _get_anonymous_install_id(c: Optional[Cardinal] = None) -> str:
    with STATE_LOCK:
        state = _load_autosmm_state()
        cached_hash = str(state.get("telemetry_identity_hash") or "").strip().lower()
    if cached_hash and not _looks_like_anonymous_install_id(cached_hash):
        cached_hash = ""
    if cached_hash:
        return cached_hash

    # Resolve identity outside STATE_LOCK in case account inspection touches network/API state.
    username, user_id, source = _get_funpay_identity(c)
    new_hash = build_anonymous_install_id(username, user_id)

    _set_telemetry_state_fields(
        telemetry_identity_hash=new_hash,
        telemetry_identity_source=source,
    )
    return new_hash


def _build_telemetry_payload(c: Optional[Cardinal] = None) -> Dict[str, str]:
    return {
        "plugin_name": rt.NAME,
        "version": rt.VERSION,
        "anonymous_install_id": _get_anonymous_install_id(c),
    }


def _send_telemetry_ping_background(c: Optional[Cardinal] = None) -> None:
    try:
        payload = _build_telemetry_payload(c or rt.cardinal_instance)
    except ValueError:
        _set_telemetry_state_fields(telemetry_last_ping_error="cannot resolve FunPay username/id")
        rt.logger.warning("TELEMETRY ID FAILED: cannot resolve FunPay username/id")
        return

    try:
        resp = requests.post(TELEMETRY_URL, json=payload, timeout=TELEMETRY_TIMEOUT_SECONDS)
        raise_for_status = getattr(resp, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        status_code = getattr(resp, "status_code", 200)
        if isinstance(status_code, int) and status_code >= 400:
            raise requests.HTTPError(f"HTTP {status_code}", response=resp)
        resp_json = None
        json_fn = getattr(resp, "json", None)
        if callable(json_fn):
            try:
                resp_json = json_fn()
            except ValueError:
                resp_json = None
        if isinstance(resp_json, dict) and resp_json.get("ok") is False:
            raise RuntimeError(str(resp_json.get("error") or "telemetry server rejected ping"))
        _set_telemetry_state_fields(
            telemetry_last_ping_ok_at=rt.now_str(),
            telemetry_last_ping_error=None,
        )
    except Exception as ex:
        _set_telemetry_state_fields(telemetry_last_ping_error=str(ex)[:300])
        rt.logger.warning(f"TELEMETRY PING FAILED: {ex}")


def _maybe_send_telemetry_ping(c: Optional[Cardinal] = None, force: bool = False) -> None:
    with STATE_LOCK:
        state = _load_autosmm_state()
        if state.get("telemetry_consent") is not True:
            return
        if not force:
            last_attempt = _parse_autosmm_dt(state.get("telemetry_last_ping_attempt_at"))
            if last_attempt and (datetime.now() - last_attempt).total_seconds() < TELEMETRY_PING_INTERVAL_SECONDS:
                return
        state["telemetry_last_ping_attempt_at"] = rt.now_str()
        _save_autosmm_state(state)

    try:
        threading.Thread(
            target=_send_telemetry_ping_background,
            args=(c or rt.cardinal_instance,),
            daemon=True,
        ).start()
    except Exception as ex:
        _set_telemetry_state_fields(telemetry_last_ping_error=str(ex)[:300])
        rt.logger.warning(f"TELEMETRY THREAD FAILED: {ex}")
