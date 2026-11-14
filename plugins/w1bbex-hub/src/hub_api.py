"""Hub HTTP API client, host/identity helpers, and live plugin state aggregation."""

from __future__ import annotations

import json
from typing import Any, Optional
import platform
import sys

import requests

from .catalog import fetch_catalog, merge_local_plugins
from .core import inspect_plugin_state, is_newer_version, scan_legacy_plugin_files
from .runtime import API_BASE, CACHE_LOCK, NO_PROXY, PLUGINS_CACHE, VERSION, logger
from .storage import load_data


def safe_json(resp: Any) -> Optional[dict[str, Any]]:
    """Parse HTTP JSON safely, returning None on non-JSON or non-dict payloads."""
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError, AttributeError):
        return None


def _extract_json_error(raw_text: str) -> Optional[str]:
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        return str(parsed.get("error") or "Ошибка сервера.")
    return None


def get_plugins(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch catalog metadata through the shared status-aware cache."""
    return fetch_catalog(force_refresh=force_refresh)


def get_sys_info() -> dict[str, str]:
    """Collect normalized host platform metadata."""
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "hub_version": VERSION,
    }


def get_funpay_identity(cardinal: Any) -> dict[str, str]:
    """Extract FunPay identity cleanly without turning missing IDs into 'None'."""
    account = getattr(cardinal, "account", None)
    user_id = ""
    username = "-"
    if account is not None:
        raw_id = getattr(account, "id", None)
        if raw_id in (None, ""):
            raw_id = getattr(account, "user_id", None)
        if raw_id not in (None, ""):
            user_id = str(raw_id).strip()
        raw_name = getattr(account, "username", None)
        if raw_name not in (None, ""):
            username = str(raw_name).strip() or "-"
    return {
        "id": user_id,
        "funpay_user_id": user_id,
        "username": username,
        "funpay_username": username,
    }


def get_telegram_identity(user: Any) -> dict[str, str]:
    """Extract Telegram identity safely without turning None into 'None'."""
    raw_id = getattr(user, "id", None)
    raw_name = getattr(user, "username", None)
    tg_id = str(raw_id).strip() if raw_id not in (None, "") else ""
    tg_user = str(raw_name).strip().lstrip("@") if raw_name not in (None, "") else ""
    return {
        "id": tg_id,
        "telegram_user_id": tg_id,
        "username": tg_user,
        "telegram_username": tg_user,
    }


def fetch_hub_latest_version(cardinal: Any, tg_user: Any) -> tuple[str, bool]:
    """Query /hub/v1/init and return (latest_version, has_newer_hub_version)."""
    fp_ident = get_funpay_identity(cardinal)
    tg_ident = get_telegram_identity(tg_user)
    payload = {
        "funpay_id": fp_ident["id"] or "unknown",
        "funpay_username": fp_ident["username"],
        "telegram_id": tg_ident["id"],
        "telegram_username": tg_ident["username"],
        "current_version": VERSION,
    }
    try:
        resp = requests.post(f"{API_BASE}/init", json=payload, timeout=5, proxies=NO_PROXY)
        if resp.status_code == 200:
            data = safe_json(resp)
            if data:
                latest = str(data.get("latest_version", VERSION))
                return latest, is_newer_version(latest, VERSION)
    except requests.RequestException as exc:
        logger.error("Hub init error: %s", exc)
    return VERSION, False


def request_trial_key(cardinal: Any, plugin_name: str, tg_user: Any) -> tuple[bool, str]:
    """Call /hub/v1/trial and return (success, key_or_error_message)."""
    fp_ident = get_funpay_identity(cardinal)
    tg_ident = get_telegram_identity(tg_user)
    payload = {
        "plugin_name": plugin_name,
        "funpay_id": fp_ident["id"] or "unknown",
        "funpay_username": fp_ident["username"],
        "telegram_id": tg_ident["id"],
        "telegram_username": tg_ident["username"],
    }
    try:
        resp = requests.post(f"{API_BASE}/trial", json=payload, timeout=10, proxies=NO_PROXY)
    except requests.RequestException as exc:
        return False, f"Ошибка сети: {exc}"

    data = safe_json(resp)
    if resp.status_code != 200 or not data:
        return False, str((data or {}).get("error", f"HTTP {resp.status_code}"))
    if not data.get("success", False):
        return False, str(data.get("error", "Ошибка проверки"))

    trial_key = str(data.get("key", "")).strip()
    if not trial_key:
        return False, "Сервер вернул пустой ключ"
    return True, trial_key


def _build_plugin_states(
    cardinal: Any,
    force_refresh: bool = False,
    cached_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build synchronized plugin state snapshots merged with locally installed entries."""
    if cached_only:
        with CACHE_LOCK:
            plugins = [dict(item) for item in PLUGINS_CACHE["items"]]
    else:
        plugins = get_plugins(force_refresh=force_refresh) if force_refresh else get_plugins()
    data = load_data()
    installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    legacy_entries = scan_legacy_plugin_files()
    states = [
        inspect_plugin_state(cardinal, p, installed, keys, legacy_entries=legacy_entries)
        for p in merge_local_plugins(plugins, installed)
    ]
    return states, data
