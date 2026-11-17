"""Persisted status ordering and consistency across committed states."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from src import runtime as rt
from src.core import orders
from src.storage import db, storage


@pytest.mark.parametrize("startup", ["init_commands", "start_runtime"])
def test_fresh_process_accepts_status_after_persisted_sequence(tmp_path, startup):
    db.init_db(rt.DB_PATH)
    storage.upsert_order_data(77, "RESTART", 9001, "pending", 100, "https://example.test", 100, 1)
    storage.update_order_fields("RESTART", status_applied_seq=1000)
    script = textwrap.dedent(
        """
        import json
        from pathlib import Path
        import socket
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock, patch

        sys.path.insert(0, sys.argv[1])
        for name in ('FunPayAPI', 'FunPayAPI.updater', 'FunPayAPI.updater.events', 'FunPayAPI.enums'):
            sys.modules[name] = ModuleType(name)
        events = sys.modules['FunPayAPI.updater.events']
        for name in ('NewMessageEvent', 'NewOrderEvent', 'OrderStatusChangedEvent'):
            setattr(events, name, type(name, (), {}))
        from src import plugin, runtime as rt
        from src.core import orders
        from src.storage import storage

        rt.configure_paths(Path(sys.argv[2]) / 'plugins' / 'AutoSMM.py')
        rt.DB_PATH = sys.argv[3]
        cardinal = SimpleNamespace(telegram=SimpleNamespace(bot=SimpleNamespace()), add_telegram_commands=Mock())
        result = {'ok': True, 'status': 'completed', 'charge': 20, 'currency': 'RUB', 'remains': 0}
        notifications = []

        def poll(_cardinal):
            orders._sync_single_order_status(_cardinal, storage.get_order_data('RESTART'))

        def notify(*args):
            row = storage.get_order_data('RESTART')
            notifications.append((row['status'], row['spent'], row['chistota']))

        with (
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Network is forbidden')),
            patch.object(orders, '_fetch_single_order_status', return_value=result),
            patch.object(orders, '_send_completion_notification_if_needed', side_effect=notify),
            patch.object(plugin, '_register_telegram_callbacks', side_effect=poll),
            patch.object(plugin, '_recover_buyer_flow_runtime', side_effect=poll),
            patch.object(plugin, '_start_background_threads'),
        ):
            getattr(plugin, sys.argv[4])(cardinal)
        row = storage.get_order_data('RESTART')
        assert row['status'] == 'completed', row
        assert row['status_applied_seq'] > 1000, row
        assert notifications == [('completed', 20.0, 80.0)], notifications
        print(json.dumps({'status': row['status'], 'sequence': row['status_applied_seq']}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-u", "-c", script, str(Path(__file__).resolve().parents[1]),
         str(tmp_path / "child"), rt.DB_PATH, startup],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_CURRENT_TEST": "status_restart"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert storage.get_order_data("RESTART")["status"] == "completed"


def test_status_finances_are_consistent_at_every_commit(monkeypatch):
    storage.upsert_order_data(77, "ATOMIC", 9002, "pending", 100, "https://example.test", 100, 1)
    storage.update_order_fields("ATOMIC", spent=50, spent_rub=50, chistota=50)
    committed_states = []
    original_update = orders.update_order_fields

    def observe_commit(order_id, **fields):
        updated = original_update(order_id, **fields)
        row = db_read = storage.get_order_data(order_id)
        committed_states.append((db_read["status"], row["spent"], row["chistota"]))
        return updated

    monkeypatch.setattr(orders, "update_order_fields", observe_commit)
    monkeypatch.setattr(orders, "_send_completion_notification_if_needed", lambda *_: None)
    orders._apply_status_result(
        None, storage.get_order_data("ATOMIC"),
        {"ok": True, "status": "completed", "charge": 20, "currency": "RUB", "remains": 0},
        request_seq=10,
    )
    assert committed_states
    assert all(state == ("completed", 20.0, 80.0) for state in committed_states)


def test_failed_status_write_does_not_emit_completion(monkeypatch):
    storage.upsert_order_data(77, "WRITE-FAIL", 9003, "pending", 100, "https://example.test", 100, 1)
    notifications = []
    monkeypatch.setattr(orders, "update_order_fields", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orders, "_send_completion_notification_if_needed", lambda *_: notifications.append(True))
    orders._apply_status_result(
        None, storage.get_order_data("WRITE-FAIL"),
        {"ok": True, "status": "completed", "charge": 20, "currency": "RUB", "remains": 0},
        request_seq=10,
    )
    assert notifications == []
    assert storage.get_order_data("WRITE-FAIL")["status"] == "pending"


def test_stale_poll_never_overwrites_newer_finances(monkeypatch):
    storage.upsert_order_data(77, "RACE-FIN", 9004, "pending", 100, "https://example.test", 100, 1)
    monkeypatch.setattr(orders, "_send_completion_notification_if_needed", lambda *_: None)
    orders._apply_status_result(
        None,
        storage.get_order_data("RACE-FIN"),
        {"ok": True, "status": "completed", "charge": 20, "currency": "RUB", "remains": 0},
        request_seq=3,
    )
    orders._apply_status_result(
        None,
        storage.get_order_data("RACE-FIN"),
        {"ok": True, "status": "pending", "charge": 50, "currency": "RUB", "remains": 40},
        request_seq=2,
    )
    row = storage.get_order_data("RACE-FIN")
    assert row["status"] == "completed"
    assert row["status_applied_seq"] == 3
    assert row["spent"] == 20.0
    assert row["chistota"] == 80.0
