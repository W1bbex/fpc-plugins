"""Read bounded release metadata from the same artifact offered for download."""

from __future__ import annotations

import ast
import threading
import time
from typing import Any

import requests

from .runtime import API_BASE, NO_PROXY, logger

_RELEASE_CACHE_LOCK = threading.Lock()
_CACHED_RELEASE: dict[str, Any] = {"version": None, "notes": None, "ts": 0.0}
_CACHE_TTL: float = 300.0


def clear_release_notes_cache() -> None:
    """Clear in-memory cached release metadata."""
    with _RELEASE_CACHE_LOCK:
        _CACHED_RELEASE["version"] = None
        _CACHED_RELEASE["notes"] = None
        _CACHED_RELEASE["ts"] = 0.0


def fetch_release_notes(force_refresh: bool = False) -> tuple[str | None, str | None]:
    now = time.monotonic()
    if not force_refresh:
        with _RELEASE_CACHE_LOCK:
            if _CACHED_RELEASE["version"] is not None and (now - _CACHED_RELEASE["ts"]) < _CACHE_TTL:
                return _CACHED_RELEASE["version"], _CACHED_RELEASE["notes"]

    try:
        with requests.get(f"{API_BASE}/download_client", stream=True, timeout=5, proxies=NO_PROXY) as response:
            response.raise_for_status()
            prefix = b""
            for chunk in response.iter_content(chunk_size=1024):
                prefix += chunk
                if len(prefix) >= 16384 or b"\nRELEASE_NOTES = " in prefix and b"\nDESCRIPTION = " in prefix:
                    break
            fields = {}
            for line in prefix.decode("utf-8", errors="replace").splitlines():
                if line.startswith(("VERSION = ", "RELEASE_NOTES = ")):
                    name, value = line.split(" = ", 1)
                    try:
                        parsed = ast.literal_eval(value)
                    except (SyntaxError, ValueError):
                        continue
                    if isinstance(parsed, str):
                        fields[name] = parsed
            ver = fields.get("VERSION")
            notes = fields.get("RELEASE_NOTES")
            if ver:
                with _RELEASE_CACHE_LOCK:
                    _CACHED_RELEASE["version"] = ver
                    _CACHED_RELEASE["notes"] = notes
                    _CACHED_RELEASE["ts"] = now
            return ver, notes
    except (requests.RequestException, OSError) as exc:
        logger.warning("Could not fetch Hub release notes: %s", exc)
        return None, None
