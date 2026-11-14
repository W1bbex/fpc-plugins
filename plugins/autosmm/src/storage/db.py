"""SQLite persistence connection, schema initialization, migrations, and path locking."""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, Iterator, Optional, Tuple

from .. import runtime as rt

logger = logging.getLogger("FPC.autosmm")

SCHEMA_VERSION = "2"
DEFAULT_CONNECT_TIMEOUT = 15.0

_DB_LOCKS_GUARD = threading.Lock()
_DB_LOCKS: Dict[str, threading.RLock] = {}
_SCHEMA_CACHE_LOCK = threading.Lock()
_INITIALIZED_DB_SIGNATURES: Dict[str, Tuple[int, int, int]] = {}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    chat_id INTEGER,
    buyer_id TEXT,
    buyer_id_json TEXT,
    buyer_username TEXT DEFAULT '',
    lot_title TEXT DEFAULT '',
    service_name TEXT DEFAULT '',
    service_id INTEGER,
    service_number INTEGER DEFAULT 1,
    panel_order_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    panel_status TEXT,
    funpay_status TEXT,
    buyer_flow_state TEXT,
    customer_url TEXT DEFAULT '',
    saved_link TEXT,
    quantity INTEGER NOT NULL DEFAULT 0,
    total_quantity INTEGER NOT NULL DEFAULT 0,
    done_quantity INTEGER NOT NULL DEFAULT 0,
    left_quantity INTEGER NOT NULL DEFAULT 0,
    progress_percent INTEGER NOT NULL DEFAULT 0,
    order_amount_json TEXT,
    order_price_json TEXT,
    summa REAL NOT NULL DEFAULT 0.0,
    spent REAL NOT NULL DEFAULT 0.0,
    spent_rub REAL,
    fx_rate_to_rub REAL,
    chistota REAL NOT NULL DEFAULT 0.0,
    currency TEXT NOT NULL DEFAULT 'RUB',
    fallback_used INTEGER,
    fallback_reason TEXT,
    completed_notification_sent INTEGER NOT NULL DEFAULT 0,
    status_last_checked_at TEXT,
    status_last_error TEXT,
    incorrect_order_id_attempts INTEGER NOT NULL DEFAULT 0,
    status_stop_reason TEXT,
    manual_check_alert_sent INTEGER NOT NULL DEFAULT 0,
    funpay_status_checked_at TEXT,
    refund_detected_at TEXT,
    refund_source TEXT,
    panel_refund_cancel_attempted_at TEXT,
    panel_refund_cancel_result TEXT,
    last_prompt_ts TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    date TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_funpay_status ON orders(funpay_status);
CREATE INDEX IF NOT EXISTS idx_orders_panel_order_id ON orders(panel_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_buyer_chat_created ON orders(buyer_id, chat_id, created_at);
"""


def _resolve_db_path(db_path: Optional[str] = None) -> str:
    return os.path.abspath(str(db_path or rt.DB_PATH))


def get_db_lock(db_path: Optional[str] = None) -> threading.RLock:
    """Return a per-database-path reentrant lock to serialize writers safely."""
    key = os.path.normcase(_resolve_db_path(db_path))
    with _DB_LOCKS_GUARD:
        lock = _DB_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DB_LOCKS[key] = lock
        return lock


def _migrate_schema_columns(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "spent_rub" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN spent_rub REAL")
    if "fx_rate_to_rub" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN fx_rate_to_rub REAL")
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )


def _has_current_schema_version(conn: sqlite3.Connection) -> bool:
    has_meta = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if has_meta is None:
        return False
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return row is not None and str(row[0]) == SCHEMA_VERSION


def _ensure_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    if row is not None:
        if not _has_current_schema_version(conn):
            _migrate_schema_columns(conn)
        return
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )


def _stat_signature(path: str) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    ino = int(getattr(st, "st_ino", 0) or 0)
    ctime = int(getattr(st, "st_ctime_ns", 0) or int(getattr(st, "st_ctime", 0)))
    if ino:
        return (int(st.st_dev), ino, ctime)
    mtime = int(getattr(st, "st_mtime_ns", 0) or int(getattr(st, "st_mtime", 0)))
    return (int(st.st_size), mtime, ctime)


def _mark_db_initialized(target: str) -> None:
    key = os.path.normcase(target)
    sig = _stat_signature(target)
    if sig is None:
        return
    with _SCHEMA_CACHE_LOCK:
        _INITIALIZED_DB_SIGNATURES[key] = sig


def invalidate_schema_cache(db_path: Optional[str] = None) -> None:
    """Forget cached schema initialization state so the next access re-verifies the DB file."""
    with _SCHEMA_CACHE_LOCK:
        if db_path is None:
            _INITIALIZED_DB_SIGNATURES.clear()
            return
        _INITIALIZED_DB_SIGNATURES.pop(os.path.normcase(_resolve_db_path(db_path)), None)


def _is_db_schema_cached(target: str) -> bool:
    key = os.path.normcase(target)
    current_sig = _stat_signature(target)
    if current_sig is None:
        return False
    with _SCHEMA_CACHE_LOCK:
        return _INITIALIZED_DB_SIGNATURES.get(key) == current_sig


def _ensure_db_initialized(target: str) -> None:
    if _is_db_schema_cached(target):
        return
    init_db(target)


@contextmanager
def _connect(db_path: Optional[str] = None, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    target = _resolve_db_path(db_path)
    if read_only:
        _ensure_db_initialized(target)
        conn = sqlite3.connect(target, timeout=DEFAULT_CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA query_only=ON;")
            yield conn
        finally:
            conn.close()
        return

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with get_db_lock(target):
        conn = sqlite3.connect(target, timeout=DEFAULT_CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            _ensure_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _mark_db_initialized(target)


def init_db(db_path: Optional[str] = None) -> None:
    """Ensure the SQLite database file, schema tables, and indexes exist."""
    with _connect(db_path, read_only=False):
        pass


def _json_dumps_optional(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _json_loads_optional(raw_value: Optional[str], default: Any = None) -> Any:
    if raw_value in (None, ""):
        return default
    try:
        return json.loads(raw_value)
    except (ValueError, TypeError):
        return default
