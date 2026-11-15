"""Coalesced background refresh for open Hub screens."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .runtime import CACHE_TTL_SECONDS, STOP_EVENT, VERSION, logger


class MenuBackground:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.version = (VERSION, False)
        self.checked = 0.0
        self.running = False
        self.serial = 0
        self.views: dict[tuple[int, int], int] = {}
        self.watchers: dict[tuple[int, int], tuple[int, Callable[[], None]]] = {}

    def invalidate(self, chat: int, message: int) -> None:
        with self.lock:
            self.views.pop((chat, message), None)
            self.watchers.pop((chat, message), None)

    def schedule(self, chat: int, message: int, refresh: Callable[[], tuple[str, bool]],
                 redraw: Callable[[], None], ttl: float = CACHE_TTL_SECONDS,
                 is_fresh: Callable[[], bool] | None = None) -> None:
        with self.lock:
            self.serial += 1
            key = (chat, message)
            self.views[key] = self.serial
            if len(self.views) > 128:
                oldest = next(iter(self.views))
                if oldest != key:
                    self.views.pop(oldest, None)
                    self.watchers.pop(oldest, None)
            if self.running:
                self.watchers[key] = (self.serial, redraw)
                return
            if time.monotonic() - self.checked < ttl and self.checked:
                return
            self.watchers[key] = (self.serial, redraw)
            self.running = True

        def worker() -> None:
            try:
                result = refresh()
                fresh = True if is_fresh is None else bool(is_fresh())
                with self.lock:
                    self.version = result
                    if fresh:
                        self.checked = time.monotonic()
                    self.running = False
                    watchers, self.watchers = self.watchers, {}
                for watched_key, (serial, draw) in watchers.items():
                    with self.lock:
                        should_draw = not STOP_EVENT.is_set() and self.views.get(watched_key) == serial
                    if should_draw:
                        try:
                            draw()
                        except Exception:
                            logger.exception("Failed refreshing open Hub menu")
            except Exception:
                logger.exception("Background menu refresh failed")
                with self.lock:
                    self.running = False
                    self.watchers.clear()

        threading.Thread(target=worker, name="hub-menu-refresh", daemon=True).start()
