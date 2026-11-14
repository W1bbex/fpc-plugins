"""Discover verifiable legacy installs for the existing update notification cycle."""
from __future__ import annotations

from typing import Any

from .core import inspect_plugin_state


def notification_chat(cardinal: Any, data: dict[str, Any]) -> Any:
    if data.get("owner_tg_id"):
        return data["owner_tg_id"]
    users = getattr(getattr(cardinal, "telegram", None), "authorized_users", None)
    if isinstance(users, (dict, set, list, tuple)) and len(users) == 1:
        user_id = next(iter(users))
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0:
            return user_id
    return None


def include_manual_installs(cardinal: Any, plugins: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    """Build a temporary notification snapshot without changing install ownership."""
    installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
    combined = dict(installed)
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    for plugin in plugins:
        pid = plugin.get("id")
        if not pid or pid in combined or not plugin.get("uuid"):
            continue
        state = inspect_plugin_state(cardinal, plugin, installed, keys)
        if state["status_code"] == "manual" and state["manual_files"] and state["manual_version"]:
            combined[pid] = {"name": state["name"], "version": state["manual_version"]}
    return {**data, "installed": combined}
