"""Read bounded release metadata from the same artifact offered for download."""

from __future__ import annotations

import ast
import requests

from .runtime import API_BASE, NO_PROXY, logger


def fetch_release_notes() -> tuple[str | None, str | None]:
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
            return fields.get("VERSION"), fields.get("RELEASE_NOTES")
    except (requests.RequestException, OSError) as exc:
        logger.warning("Could not fetch Hub release notes: %s", exc)
        return None, None
