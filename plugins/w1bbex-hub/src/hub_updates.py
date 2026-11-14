"""Background notifications for new Hub releases, independent of plugin installs."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from .core import get_disk_hub_pending_version, is_newer_version
from .hub_api import fetch_hub_latest_version
from .runtime import VERSION, logger
from .storage import update_data
from .transport import _call_tg_html
from .ui import render_update_notification


def notify_hub_update(cardinal: Any, data: dict[str, Any]) -> int:
    """Notify once per release; failed deliveries remain eligible for retry."""
    owner_chat = data.get("owner_tg_id")
    bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
    if not owner_chat or bot is None or not data.get("notify_updates", True):
        return 0

    latest, has_update = fetch_hub_latest_version(cardinal, SimpleNamespace(id=owner_chat, username=""))
    if not has_update:
        return 0
    staged = get_disk_hub_pending_version()
    if staged and not is_newer_version(latest, staged):
        return 0

    update_key = f"hub:{latest}"
    notified = data.get("notified_updates")
    reminder = notified.get(update_key) if isinstance(notified, dict) else None
    if reminder == -1 or (isinstance(reminder, (int, float)) and time.time() < reminder):
        return 0

    update = {
        "target": "hub", "id": "w1bbex_hub", "name": "W1bbex Hub",
        "latest": latest, "curr_ver": VERSION, "update_key": update_key,
        "changelog": "Доступна новая версия менеджера плагинов. Нажмите «Обновить Hub», чтобы загрузить её",
    }
    text, markup = render_update_notification(update, notify_enabled=True)
    try:
        _call_tg_html(
            lambda body, keyboard: bot.send_message(owner_chat, body, parse_mode="HTML", reply_markup=keyboard),
            text, reply_markup=markup,
        )
    except Exception:
        logger.exception("Failed sending Hub update notification")
        return 0

    try:
        update_data(lambda current: current.setdefault("notified_updates", {}).__setitem__(update_key, -1))
    except OSError:
        logger.exception("Failed recording Hub update notification")
    return 1
