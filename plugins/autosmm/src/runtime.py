"""Shared paths and mutable runtime state for the AutoSMM plugin."""

from __future__ import annotations

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Tuple

NAME = "AutoSMM"
VERSION = "1.0"
DESCRIPTION = "AutoSMM v1.0: PerfectPanel/Peakerr API support, atomic config storage, usernames support (@channel), flexible API keys."
CREDITS = "@w1bbex"
UUID = "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8"
SETTINGS_PAGE = False

LOGGER_PREFIX = "[AUTO autosmm]"
logger = logging.getLogger("FPC.autosmm")

PLUGIN_FILE_PATH = Path(__file__).resolve()
PLUGIN_DIR = PLUGIN_FILE_PATH.parent
CARDINAL_ROOT = Path.cwd()
BASE_STORAGE_DIR = CARDINAL_ROOT / "storage" / "w1bbex" / "AutoSMM"
AUTOSMM_DATA_DIR = str(BASE_STORAGE_DIR)
DB_PATH = str(BASE_STORAGE_DIR / "autosmm.db")
CONFIG_PATH = str(BASE_STORAGE_DIR / "auto_lots.json")
CONFIG_BAK_PATH = str(BASE_STORAGE_DIR / "auto_lots.json.bak")
VALID_LINKS_PATH = str(BASE_STORAGE_DIR / "valid_link.json")
ORDERS_PATH = str(BASE_STORAGE_DIR / "auto_smm_orders.json")
ORDERS_DATA_PATH = str(BASE_STORAGE_DIR / "orders_data.json")
ORDERS_RUNTIME_PATH = str(BASE_STORAGE_DIR / "orders_runtime.json")
AUTOSMM_STATE_PATH = str(BASE_STORAGE_DIR / "auto_smm_state.json")
LOGS_DIR = str(BASE_STORAGE_DIR / "logs")
BACKUPS_DIR = str(BASE_STORAGE_DIR / "backups")
LOG_DIR = LOGS_DIR
AUTOSMM_BACKUPS_DIR = BACKUPS_DIR
LOG_PATH = str(Path(LOGS_DIR) / "auto_smm.log")

LEGACY_CACHE_DIR = CARDINAL_ROOT / "storage" / "cache"
LEGACY_LOGS_DIR = CARDINAL_ROOT / "storage" / "logs"
LEGACY_CONFIG_PATH = str(LEGACY_CACHE_DIR / "auto_lots.json")
LEGACY_ORDERS_PATH = str(LEGACY_CACHE_DIR / "auto_smm_orders.json")
LEGACY_ORDERS_DATA_PATH = str(LEGACY_CACHE_DIR / "orders_data.json")
LEGACY_ORDERS_RUNTIME_PATH = str(LEGACY_CACHE_DIR / "orders_runtime.json")
LEGACY_VALID_LINKS_PATH = str(LEGACY_CACHE_DIR / "valid_link.json")
LEGACY_AUTOSMM_STATE_PATH = str(LEGACY_CACHE_DIR / "auto_smm_state.json")
LEGACY_AUTOSMM_BACKUPS_DIR = str(LEGACY_CACHE_DIR / "autosmm_backups")
LEGACY_LOG_PATH = str(LEGACY_LOGS_DIR / "auto_smm.log")

RUNNING = False
ORDER_CHECK_THREAD = None

bot: Any = None
cardinal_instance: Any = None
config: Dict[str, Any] = {}
lot_mapping: Dict[str, Any] = {}

waiting_for_link: Dict[str, Dict] = {}
BUYER_ORDER_SELECTION: Dict[str, str] = {}
LOT_PAGE_CONTEXT: Dict[int, int] = {}
WEBSITE_PAGE_CONTEXT: Dict[int, int] = {}
LOT_INPUT_STATES: Dict[int, Dict[str, Any]] = {}
LOT_CARD_CONTEXT: Dict[int, Dict[str, Any]] = {}
UI_INPUT_STATES: Dict[int, Dict[str, Any]] = {}
SEARCH_RESULT_CONTEXT: Dict[int, Dict[str, Any]] = {}
ANALYTICS_VIEW_MODES: Dict[Tuple[int, int], Any] = {}
DELETE_CONFIRM_GRACE_UNTIL: Dict[int, float] = {}
CALLBACK_TOKENS: Dict[str, str] = {}
PANEL_BALANCE_CACHE_LOCK = threading.RLock()
PANEL_BALANCE_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "text": "недоступно",
    "services": {},
    "generation": 0,
    "inflight": {},
}
CONTEXT_LOCK = threading.RLock()
SCREEN_REVISION_LOCK = threading.RLock()
SCREEN_REVISION: Dict[Tuple[int, int], int] = {}


def _resolve_cardinal_root(entry: Path) -> Path:
    """Resolve the Cardinal root directory from top-level or plugins/w1bbex locations."""
    plugin_dir = entry.parent
    if plugin_dir.name.lower() == "plugins":
        return plugin_dir.parent
    lowered_parts = [part.lower() for part in entry.parts]
    for idx in range(len(lowered_parts) - 2):
        if lowered_parts[idx] == "plugins" and lowered_parts[idx + 1] == "w1bbex":
            return Path(*entry.parts[:idx])
    return Path.cwd()


def configure_paths(anchor_file: Optional[Path] = None) -> None:
    """Resolve storage, log, backup, and legacy paths from plugin entry file."""
    entry = Path(anchor_file or __file__).resolve()
    plugin_dir = entry.parent
    cardinal_root = _resolve_cardinal_root(entry)
    base_storage_dir = cardinal_root / "storage" / "w1bbex" / "AutoSMM"
    logs_dir = str(base_storage_dir / "logs")
    backups_dir = str(base_storage_dir / "backups")
    legacy_cache_dir = cardinal_root / "storage" / "cache"
    legacy_logs_dir = cardinal_root / "storage" / "logs"

    globals().update(
        PLUGIN_FILE_PATH=entry,
        PLUGIN_DIR=plugin_dir,
        CARDINAL_ROOT=cardinal_root,
        BASE_STORAGE_DIR=base_storage_dir,
        AUTOSMM_DATA_DIR=str(base_storage_dir),
        DB_PATH=str(base_storage_dir / "autosmm.db"),
        CONFIG_PATH=str(base_storage_dir / "auto_lots.json"),
        CONFIG_BAK_PATH=str(base_storage_dir / "auto_lots.json.bak"),
        VALID_LINKS_PATH=str(base_storage_dir / "valid_link.json"),
        ORDERS_PATH=str(base_storage_dir / "auto_smm_orders.json"),
        ORDERS_DATA_PATH=str(base_storage_dir / "orders_data.json"),
        ORDERS_RUNTIME_PATH=str(base_storage_dir / "orders_runtime.json"),
        AUTOSMM_STATE_PATH=str(base_storage_dir / "auto_smm_state.json"),
        LOGS_DIR=logs_dir,
        BACKUPS_DIR=backups_dir,
        LOG_DIR=logs_dir,
        AUTOSMM_BACKUPS_DIR=backups_dir,
        LOG_PATH=str(Path(logs_dir) / "auto_smm.log"),
        LEGACY_CACHE_DIR=legacy_cache_dir,
        LEGACY_LOGS_DIR=legacy_logs_dir,
        LEGACY_CONFIG_PATH=str(legacy_cache_dir / "auto_lots.json"),
        LEGACY_ORDERS_PATH=str(legacy_cache_dir / "auto_smm_orders.json"),
        LEGACY_ORDERS_DATA_PATH=str(legacy_cache_dir / "orders_data.json"),
        LEGACY_ORDERS_RUNTIME_PATH=str(legacy_cache_dir / "orders_runtime.json"),
        LEGACY_VALID_LINKS_PATH=str(legacy_cache_dir / "valid_link.json"),
        LEGACY_AUTOSMM_STATE_PATH=str(legacy_cache_dir / "auto_smm_state.json"),
        LEGACY_AUTOSMM_BACKUPS_DIR=str(legacy_cache_dir / "autosmm_backups"),
        LEGACY_LOG_PATH=str(legacy_logs_dir / "auto_smm.log"),
    )


def ensure_storage_dirs() -> None:
    """Create plugin data, backup, and log directories if absent."""
    for directory in (AUTOSMM_DATA_DIR, AUTOSMM_BACKUPS_DIR, LOG_DIR):
        os.makedirs(directory, exist_ok=True)


def init_logging() -> None:
    """Attach a rotating file handler for LOG_PATH when not already present."""
    logger.setLevel(logging.DEBUG)
    target_log = os.path.abspath(LOG_PATH)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == target_log:
            handler.setLevel(logging.DEBUG)
            return

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=15 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s"))
        logger.addHandler(file_handler)
    except (OSError, ValueError) as ex:
        logger.warning(f"Не удалось инициализировать RotatingFileHandler для {LOG_PATH}: {ex}")


def bump_screen_revision(chat_id: Optional[int], message_id: Optional[int]) -> int:
    """Increment and return the UI revision counter for a Telegram message."""
    if chat_id is None or message_id is None:
        return 0
    key = (int(chat_id), int(message_id))
    with SCREEN_REVISION_LOCK:
        rev = int(SCREEN_REVISION.get(key) or 0) + 1
        SCREEN_REVISION[key] = rev
        if len(SCREEN_REVISION) > 2000:
            for stale_key in list(SCREEN_REVISION.keys())[:-1000]:
                SCREEN_REVISION.pop(stale_key, None)
        return rev


def get_screen_revision(chat_id: Optional[int], message_id: Optional[int]) -> int:
    """Return the current UI revision counter for a Telegram message."""
    if chat_id is None or message_id is None:
        return 0
    key = (int(chat_id), int(message_id))
    with SCREEN_REVISION_LOCK:
        return int(SCREEN_REVISION.get(key) or 0)


def prune_stale_contexts(now_ts: Optional[float] = None, max_ui_age_seconds: int = 3600) -> int:
    """Evict expired ephemeral Telegram UI states and stale runtime buyer contexts from memory."""
    now_value = float(now_ts if now_ts is not None else time.time())
    cutoff = now_value - max(60, int(max_ui_age_seconds))
    buyer_cutoff = now_value - 7 * 86400
    removed = 0

    with CONTEXT_LOCK:
        for mapping in (
            LOT_INPUT_STATES,
            UI_INPUT_STATES,
            LOT_CARD_CONTEXT,
            SEARCH_RESULT_CONTEXT,
            ANALYTICS_VIEW_MODES,
        ):
            for key in list(mapping.keys()):
                ctx = mapping.get(key)
                if not isinstance(ctx, dict):
                    continue
                created_ts = ctx.get("created_ts")
                if created_ts is None:
                    continue
                try:
                    if float(created_ts) < cutoff:
                        mapping.pop(key, None)
                        removed += 1
                except (TypeError, ValueError):
                    mapping.pop(key, None)
                    removed += 1

        for key in list(waiting_for_link.keys()):
            ctx = waiting_for_link.get(key)
            if not isinstance(ctx, dict):
                continue
            created_ts = ctx.get("created_ts") or ctx.get("updated_ts")
            if created_ts is None:
                continue
            try:
                if float(created_ts) < buyer_cutoff:
                    waiting_for_link.pop(key, None)
                    removed += 1
            except (TypeError, ValueError):
                waiting_for_link.pop(key, None)
                removed += 1

        while len(BUYER_ORDER_SELECTION) > 5000:
            oldest_key = next(iter(BUYER_ORDER_SELECTION))
            BUYER_ORDER_SELECTION.pop(oldest_key, None)
            removed += 1

        for key in list(DELETE_CONFIRM_GRACE_UNTIL.keys()):
            try:
                if float(DELETE_CONFIRM_GRACE_UNTIL.get(key) or 0.0) < now_value:
                    DELETE_CONFIRM_GRACE_UNTIL.pop(key, None)
                    removed += 1
            except (TypeError, ValueError):
                DELETE_CONFIRM_GRACE_UNTIL.pop(key, None)
                removed += 1

    return removed


def clear_memory_contexts() -> None:
    """Reset in-memory buyer and Telegram UI wizard states."""
    with CONTEXT_LOCK:
        for container in (
            waiting_for_link,
            BUYER_ORDER_SELECTION,
            LOT_PAGE_CONTEXT,
            WEBSITE_PAGE_CONTEXT,
            LOT_INPUT_STATES,
            LOT_CARD_CONTEXT,
            UI_INPUT_STATES,
            SEARCH_RESULT_CONTEXT,
            ANALYTICS_VIEW_MODES,
            DELETE_CONFIRM_GRACE_UNTIL,
            CALLBACK_TOKENS,
        ):
            container.clear()
    with SCREEN_REVISION_LOCK:
        SCREEN_REVISION.clear()


def now_str() -> str:
    """Return local timestamp formatted consistently for order and log records."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
