"""Runtime metadata, resolved filesystem paths, locks, and logging for W1bbex Hub."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
from typing import Any, Optional

NAME = "W1bbex Hub"
VERSION = "0.3.0"
DESCRIPTION = "Официальный менеджер и загрузчик плагинов от W1bbex."
CREDITS = "@W1bbex"
PLUGIN_ID = "w1bbex_hub"
SETTINGS_PAGE = False
UUID = "e3f1c9d2-b4a1-4a3b-9a8c-1e2f3a4b5c6d"
COMMANDS = (("hub", "Управление W1bbex Hub и плагинами"),)

API_BASE = "https://api.w1bbex.ru/hub/v1"
NO_PROXY = {"http": None, "https": None}
LOADER_HEADER = "# Автоматически сгенерированный лоадер W1bbex Hub"
DEFAULT_CHANNEL_URL = "https://t.me/w1bbexplugins"
GITHUB_URL = "https://github.com/W1bbex"
SUPPORT_URL = "https://t.me/w1bbex"
RESTART_NOTICE_TTL_SECONDS = 1800.0
SUPPORTED_PLUGIN_EXTENSIONS = (".py", ".pyd", ".so")
CACHE_TTL_SECONDS = 600.0
CACHE_RETRY_COOLDOWN_SECONDS = 30.0
UPDATE_CHECK_INTERVAL_SECONDS = 1800.0
UPDATE_CHECK_INITIAL_DELAY_SECONDS = 10.0

DELETE_LOCK = threading.RLock()
PLUGIN_FS_LOCK = threading.RLock()
CACHE_LOCK = threading.RLock()
DATA_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
CB_ANSWERED = object()

PLUGINS_CACHE: dict[str, Any] = {
    "items": [],
    "fetched_at": 0.0,
    "last_attempt_at": 0.0,
}

logger = logging.getLogger("FPC.W1bbexHub")


@dataclass
class HubPaths:
    """Mutable container holding dynamically anchored Hub paths."""

    hub_file: str
    cardinal_root: str
    plugins_dir: str
    hub_folder: str
    data_file: str
    pending_delete_file: str
    log_dir: str
    log_file: str


def resolve_paths(
    anchor_file: Optional[str | Path] = None,
    cardinal_root: Optional[str | Path] = None,
) -> HubPaths:
    """Compute portable Hub paths relative to the Cardinal root directory."""
    entry = Path(anchor_file or __file__).resolve()
    entry_dir = entry.parent

    if cardinal_root is not None:
        root_path = Path(cardinal_root).resolve()
        plugins_dir = root_path / "plugins"
    elif entry_dir.name.lower() == "plugins":
        plugins_dir = entry_dir
        root_path = entry_dir.parent
    else:
        root_path = Path.cwd().resolve()
        plugins_dir = root_path / "plugins"

    hub_folder = root_path / "storage" / "w1bbex" / "Hub"
    log_dir = hub_folder / "logs"
    return HubPaths(
        hub_file=str(entry),
        cardinal_root=str(root_path),
        plugins_dir=str(plugins_dir),
        hub_folder=str(hub_folder),
        data_file=str(hub_folder / "config.json"),
        pending_delete_file=str(hub_folder / "pending_delete.json"),
        log_dir=str(log_dir),
        log_file=str(log_dir / "w1bbex_hub.log"),
    )


PATHS = resolve_paths()


def configure_paths(
    anchor_file: Optional[str | Path] = None,
    cardinal_root: Optional[str | Path] = None,
) -> HubPaths:
    """Reconfigure shared runtime PATHS in place."""
    resolved = resolve_paths(anchor_file=anchor_file, cardinal_root=cardinal_root)
    PATHS.hub_file = resolved.hub_file
    PATHS.cardinal_root = resolved.cardinal_root
    PATHS.plugins_dir = resolved.plugins_dir
    PATHS.hub_folder = resolved.hub_folder
    PATHS.data_file = resolved.data_file
    PATHS.pending_delete_file = resolved.pending_delete_file
    PATHS.log_dir = resolved.log_dir
    PATHS.log_file = resolved.log_file
    return PATHS


def configure_file_logging() -> None:
    """Attach rotating file logging under storage/w1bbex/Hub/logs once per log file."""
    with LOG_LOCK:
        target_abs = os.path.abspath(PATHS.log_file)
        for handler in logger.handlers:
            if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "") == target_abs:
                return
        try:
            os.makedirs(PATHS.log_dir, exist_ok=True)
            handler = RotatingFileHandler(
                PATHS.log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            logger.addHandler(handler)
            if logger.level == logging.NOTSET:
                logger.setLevel(logging.INFO)
        except OSError:
            logger.warning("Failed to attach rotating file handler at %s", PATHS.log_file, exc_info=True)
