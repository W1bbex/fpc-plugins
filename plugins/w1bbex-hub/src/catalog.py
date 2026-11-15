"""Catalog transport status and local metadata fallbacks for offline management."""

from __future__ import annotations

import time
from typing import Any

import requests

from .runtime import (
    API_BASE, CACHE_LOCK, CACHE_RETRY_COOLDOWN_SECONDS, CACHE_TTL_SECONDS,
    NO_PROXY, PLUGINS_CACHE, logger,
)


def fetch_catalog(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return cached items while tracking whether the last request actually succeeded."""
    now = time.time()
    with CACHE_LOCK:
        items = PLUGINS_CACHE["items"]
        checked = float(PLUGINS_CACHE.get("checked_at", 0))
        if not force_refresh:
            if checked and not PLUGINS_CACHE.get("error") and now - checked < CACHE_TTL_SECONDS:
                return [dict(item) for item in items]
            if now - float(PLUGINS_CACHE["last_attempt_at"]) < CACHE_RETRY_COOLDOWN_SECONDS:
                return [dict(item) for item in items]
            if items and now - float(PLUGINS_CACHE["fetched_at"]) < CACHE_TTL_SECONDS:
                return [dict(item) for item in items]
        PLUGINS_CACHE["last_attempt_at"] = now
        req_gen = int(PLUGINS_CACHE.get("request_generation", 0)) + 1
        PLUGINS_CACHE["request_generation"] = req_gen

    fetched = None
    try:
        response = requests.get(f"{API_BASE}/plugins", timeout=5, proxies=NO_PROXY)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("plugins"), list):
            if all(isinstance(item, dict) and item.get("id") for item in payload["plugins"]):
                fetched = payload["plugins"]
    except (requests.RequestException, ValueError):
        logger.warning("Catalog request failed", exc_info=True)

    with CACHE_LOCK:
        if req_gen == PLUGINS_CACHE.get("request_generation"):
            PLUGINS_CACHE["checked_at"] = time.time()
            PLUGINS_CACHE["error"] = fetched is None
            if fetched is not None:
                PLUGINS_CACHE["items"] = fetched
                PLUGINS_CACHE["fetched_at"] = time.time()
        return [dict(item) for item in PLUGINS_CACHE["items"]]


def catalog_status() -> dict[str, Any]:
    """Expose availability separately from an empty catalog or stale cached items."""
    with CACHE_LOCK:
        return {
            "available": not bool(PLUGINS_CACHE.get("error")),
            "loading": not bool(PLUGINS_CACHE.get("checked_at")),
            "stale": bool(PLUGINS_CACHE.get("error") and PLUGINS_CACHE["items"]),
            "fetched_at": float(PLUGINS_CACHE["fetched_at"]),
        }


def merge_local_plugins(plugins: list[dict[str, Any]], installed: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep local installations visible even if absent from the remote catalog."""
    result = [dict(plugin, catalog_known=True) for plugin in plugins]
    known = {str(plugin["id"]) for plugin in plugins}
    for pid, entry in installed.items():
        if pid in known or not isinstance(entry, dict):
            continue
        saved = entry.get("catalog")
        info = dict(saved) if isinstance(saved, dict) else {}
        info.update(id=pid, name=entry.get("name") or pid, latest_version="", catalog_known=False)
        result.append(info)
    return result
