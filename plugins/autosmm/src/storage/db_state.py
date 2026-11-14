"""SQLite key-value runtime state and atomic snapshot import/export operations."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Iterable, Mapping, Optional

from .. import runtime as rt
from .db import _connect
from .db_orders import _UPSERT_ORDER_SQL, _prepare_order_params, _row_to_order_dict

logger = logging.getLogger("FPC.autosmm")


def _decode_state_rows(rows: Iterable[sqlite3.Row]) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    for row in rows:
        try:
            state[str(row["key"])] = json.loads(row["value_json"])
        except (ValueError, TypeError):
            logger.warning(f"Некорректный JSON в kv_state для ключа '{row['key']}', пропуск.")
    return state


def load_state(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Load all persisted key-value runtime state entries."""
    with _connect(db_path, read_only=True) as conn:
        return _decode_state_rows(conn.execute("SELECT key, value_json FROM kv_state").fetchall())


def save_state(state: Mapping[str, Any], db_path: Optional[str] = None) -> bool:
    """Replace the entire kv_state store atomically."""
    ts_now = rt.now_str()
    rows_to_write = [
        (str(key), json.dumps(value, ensure_ascii=False), ts_now)
        for key, value in (state or {}).items()
    ]
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM kv_state")
        if rows_to_write:
            conn.executemany(
                "INSERT INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                rows_to_write,
            )
    return True


def update_state_fields(db_path: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    """Atomically upsert one or more kv_state keys and return the updated full state."""
    ts_now = rt.now_str()
    with _connect(db_path) as conn:
        for key, value in fields.items():
            conn.execute(
                """
                INSERT INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (str(key), json.dumps(value, ensure_ascii=False), ts_now),
            )
        return _decode_state_rows(conn.execute("SELECT key, value_json FROM kv_state").fetchall())


def export_snapshot(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Export orders and kv_state in a single read transaction for backups."""
    with _connect(db_path, read_only=True) as conn:
        order_rows = conn.execute("SELECT * FROM orders ORDER BY created_at ASC, rowid ASC").fetchall()
        state_rows = conn.execute("SELECT key, value_json FROM kv_state").fetchall()
        return {
            "orders": [_row_to_order_dict(row) for row in order_rows],
            "state": _decode_state_rows(state_rows),
        }


def restore_snapshot(
    orders: Iterable[Mapping[str, Any]],
    state: Optional[Mapping[str, Any]] = None,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """Replace orders and optional kv_state atomically from a snapshot."""
    ts_now = rt.now_str()
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM orders")
        params_list = [
            prepared
            for item in (orders or [])
            if isinstance(item, Mapping) and (prepared := _prepare_order_params(item)) is not None
        ]
        if params_list:
            conn.executemany(_UPSERT_ORDER_SQL, params_list)

        if state is not None:
            conn.execute("DELETE FROM kv_state")
            rows_to_write = [
                (str(key), json.dumps(value, ensure_ascii=False), ts_now)
                for key, value in state.items()
            ]
            if rows_to_write:
                conn.executemany(
                    "INSERT INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                    rows_to_write,
                )
    return True


def import_legacy_snapshot(
    orders: Iterable[Mapping[str, Any]],
    legacy_state: Mapping[str, Any],
    *,
    migrated_at: str,
    db_path: Optional[str] = None,
) -> int:
    """Insert missing legacy orders and state keys atomically without overwriting newer SQLite rows."""
    with _connect(db_path) as conn:
        existing_ids = {str(r[0]) for r in conn.execute("SELECT order_id FROM orders").fetchall()}
        to_insert = [
            prepared
            for order in orders
            if isinstance(order, Mapping)
            and str(order.get("order_id") or "").strip() not in existing_ids
            and (prepared := _prepare_order_params(order)) is not None
        ]
        if to_insert:
            conn.executemany(_UPSERT_ORDER_SQL, to_insert)

        existing_keys = {str(r[0]) for r in conn.execute("SELECT key FROM kv_state").fetchall()}
        for key, value in (legacy_state or {}).items():
            key_str = str(key)
            if key_str not in existing_keys:
                conn.execute(
                    "INSERT INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                    (key_str, json.dumps(value, ensure_ascii=False), migrated_at),
                )
        conn.execute(
            """
            INSERT INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            ("legacy_json_migrated_at", json.dumps(migrated_at), migrated_at),
        )
        return len(to_insert)
