import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.core import engine, orders, refunds
from src.services import currency
from src.storage import storage


class OrdersTests(unittest.TestCase):
    def test_record_plugin_start_state_preserves_unrelated_keys_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage._save_autosmm_state({"telemetry_consent": True})
                errors = []

                def writer(idx: int) -> None:
                    try:
                        with storage.STATE_LOCK:
                            state = storage._load_autosmm_state()
                            state[f"flag_{idx}"] = idx
                            storage._save_autosmm_state(state)
                        orders._record_plugin_start_state()
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                state = storage._load_autosmm_state()
                self.assertTrue(state.get("telemetry_consent"))
                self.assertTrue(bool(state.get("last_plugin_start_at")))
                for idx in range(10):
                    self.assertEqual(state.get(f"flag_{idx}"), idx)

    def test_mark_order_refunded_updates_state_clears_waiting_and_releases_lock_before_panel_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            waiting = {"FP100": {"order_id_funpay": "FP100"}}
            lock_held_during_cancel = []

            def fake_cancel(order_snapshot, source="refund_mark"):
                lock_held_during_cancel.append((storage.ORDERS_LOCK._is_owned(), order_snapshot.get("order_id"), source))
                return True

            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "waiting_for_link", waiting),
                mock.patch.object(refunds, "_attempt_panel_cancel_after_funpay_refund", side_effect=fake_cancel) as mock_cancel,
            ):
                storage.save_orders_data([{"order_id": "FP100", "funpay_status": "open", "id_zakaz": 555}])
                changed = refunds.mark_order_refunded("FP100", detected_at="2026-01-01 00:00:00", source="funpay_sync")

                self.assertTrue(changed)
                self.assertNotIn("FP100", waiting)
                stored = storage.get_order_data("FP100")
                self.assertEqual(stored["funpay_status"], "refunded")
                self.assertEqual(stored["buyer_flow_state"], engine.BUYER_STATE_REFUNDED)
                self.assertEqual(stored["refund_source"], "funpay_sync")
                self.assertEqual(lock_held_during_cancel, [(False, "FP100", "funpay_sync")])

                changed_again = refunds.mark_order_refunded("FP100", source="duplicate_sync")
                self.assertFalse(changed_again)
                self.assertEqual(mock_cancel.call_count, 1)

    def test_incorrect_order_id_never_polls_foreign_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            polled = []

            def fake_single_status(service_number, panel_order_id):
                polled.append((service_number, panel_order_id))
                return {"ok": False, "error": "incorrect_order_id", "detail": "Incorrect order ID"}

            cfg = {
                "services": {
                    "1": {"name": "Primary", "api_url": "https://p1.example/api/v2", "api_key": "k1"},
                    "2": {"name": "OtherPanel", "api_url": "https://p2.example/api/v2", "api_key": "k2"},
                }
            }
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(orders, "_fetch_single_order_status", side_effect=fake_single_status),
                mock.patch.object(orders, "_send_manual_check_alert_once") as mock_alert,
            ):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP-REBIND",
                            "buyer_chat_id": 123,
                            "id_zakaz": 999,
                            "panel_order_id": 999,
                            "service_number": 1,
                            "status": "pending",
                            "link": "https://t.me/test",
                            "funpay_status": "open",
                        }
                    ]
                )
                for _ in range(orders.INCORRECT_ORDER_ID_MAX_ATTEMPTS):
                    record = storage.get_order_data("FP-REBIND")
                    orders._sync_single_order_status(mock.MagicMock(), record)

                self.assertTrue(all(srv == 1 for srv, _ in polled))
                self.assertEqual(len(polled), orders.INCORRECT_ORDER_ID_MAX_ATTEMPTS)
                mock_alert.assert_called_once()
                final_record = storage.get_order_data("FP-REBIND")
                self.assertEqual(final_record["status"], "unknown")
                self.assertEqual(final_record["status_stop_reason"], "incorrect_order_id")

    def test_apply_status_result_persists_fx_conversion_to_rub(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(orders, "_send_completion_notification_if_needed"),
                mock.patch.object(currency, "get_rate_to_rub", return_value=90.0),
            ):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP-USD",
                            "buyer_chat_id": 123,
                            "id_zakaz": 777,
                            "panel_order_id": 777,
                            "service_number": 1,
                            "status": "pending",
                            "summa": 500.0,
                            "real_amount": 100,
                            "funpay_status": "open",
                        }
                    ]
                )
                orders._apply_status_result(
                    mock.MagicMock(),
                    storage.get_order_data("FP-USD"),
                    {"ok": True, "status": "completed", "charge": 2.0, "currency": "USD", "remains": 0},
                )
                rec = storage.get_order_data("FP-USD")
                self.assertAlmostEqual(rec["spent"], 2.0)
                self.assertEqual(rec["currency"], "USD")
                self.assertAlmostEqual(rec["spent_rub"], 180.0)
                self.assertAlmostEqual(rec["fx_rate_to_rub"], 90.0)
                self.assertAlmostEqual(rec["chistota"], 320.0)

            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(orders, "_send_completion_notification_if_needed"),
                mock.patch.object(currency, "get_rate_to_rub", return_value=None),
            ):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP-NOFX",
                            "buyer_chat_id": 123,
                            "id_zakaz": 778,
                            "panel_order_id": 778,
                            "service_number": 1,
                            "status": "pending",
                            "summa": 500.0,
                            "real_amount": 100,
                            "funpay_status": "open",
                        }
                    ]
                )
                orders._apply_status_result(
                    mock.MagicMock(),
                    storage.get_order_data("FP-NOFX"),
                    {"ok": True, "status": "completed", "charge": 2.0, "currency": "XYZ", "remains": 0},
                )
                rec_missing = storage.get_order_data("FP-NOFX")
                self.assertIsNone(rec_missing.get("spent_rub"))
                self.assertIsNone(rec_missing.get("fx_rate_to_rub"))
                self.assertEqual(rec_missing["chistota"], 0.0)

    def test_pending_status_checks_skip_orders_without_panel_order_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(orders, "_fetch_single_order_status") as mock_single,
                mock.patch.object(
                    orders,
                    "_fetch_batch_order_status",
                    return_value={"ok": True, "entries": {"777": {"status": "In progress", "remains": 40, "charge": 5.0}}},
                ) as mock_batch,
                mock.patch.object(orders, "_apply_status_result") as mock_apply,
            ):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP-NONE",
                            "chat_id": 100,
                            "service_number": 1,
                            "status": "pending",
                            "funpay_status": "open",
                        },
                        {
                            "order_id": "FP-REAL",
                            "chat_id": 100,
                            "id_zakaz": 777,
                            "panel_order_id": 777,
                            "service_number": 1,
                            "status": "pending",
                            "funpay_status": "open",
                        },
                    ]
                )
                dummy_cardinal = mock.MagicMock()

                orders._sync_single_order_status(dummy_cardinal, storage.get_order_data("FP-NONE"))
                mock_single.assert_not_called()

                orders._check_pending_orders_batch(dummy_cardinal)
                mock_batch.assert_called_once_with(1, [777])
                mock_apply.assert_called_once()
                self.assertEqual(mock_apply.call_args.args[1]["order_id"], "FP-REAL")
                untouched = storage.get_order_data("FP-NONE")
                self.assertEqual(untouched["status"], "pending")
                self.assertIsNone(untouched.get("id_zakaz"))

    def test_refund_order_persists_refunded_state_before_notification_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            orig_waiting = dict(rt.waiting_for_link)
            try:
                with (
                    mock.patch.object(rt, "DB_PATH", str(db_path)),
                    mock.patch.object(refunds, "load_config", return_value={"auto_refunds": True}),
                ):
                    storage.save_orders_data(
                        [
                            {
                                "order_id": "FP-REF-MSG",
                                "chat_id": 321,
                                "funpay_status": "open",
                                "buyer_flow_state": engine.BUYER_STATE_WAITING_CONFIRM,
                                "saved_link": "https://t.me/test",
                                "status_stop_reason": "panel_add_in_flight",
                            },
                            {
                                "order_id": "FP-REF-ALERT",
                                "chat_id": 322,
                                "funpay_status": "open",
                                "buyer_flow_state": engine.BUYER_STATE_WAITING_CONFIRM,
                                "saved_link": "https://t.me/test2",
                            },
                        ]
                    )
                    rt.waiting_for_link["FP-REF-MSG"] = {"order_id_funpay": "FP-REF-MSG"}
                    rt.waiting_for_link["FP-REF-ALERT"] = {"order_id_funpay": "FP-REF-ALERT"}

                    cardinal_msg_fail = SimpleNamespace(
                        account=SimpleNamespace(refund=mock.MagicMock()),
                        send_message=mock.MagicMock(side_effect=RuntimeError("chat send failed")),
                    )
                    with mock.patch.object(refunds, "_send_admin_order_alert") as mock_admin_ok:
                        refunds.refund_order(cardinal_msg_fail, "FP-REF-MSG", 321, "insufficient_funds")

                    stored1 = storage.get_order_data("FP-REF-MSG")
                    self.assertEqual(stored1["funpay_status"], "refunded")
                    self.assertEqual(stored1["buyer_flow_state"], engine.BUYER_STATE_REFUNDED)
                    self.assertIsNone(stored1.get("saved_link"))
                    self.assertIsNone(stored1.get("status_stop_reason"))
                    self.assertNotIn("FP-REF-MSG", rt.waiting_for_link)
                    mock_admin_ok.assert_called_once()

                    cardinal_alert_fail = SimpleNamespace(
                        account=SimpleNamespace(refund=mock.MagicMock()),
                        send_message=mock.MagicMock(),
                    )
                    with mock.patch.object(refunds, "_send_admin_order_alert", side_effect=RuntimeError("tg down")):
                        refunds.refund_order(cardinal_alert_fail, "FP-REF-ALERT", 322, "service_inactive")

                    stored2 = storage.get_order_data("FP-REF-ALERT")
                    self.assertEqual(stored2["funpay_status"], "refunded")
                    self.assertEqual(stored2["buyer_flow_state"], engine.BUYER_STATE_REFUNDED)
                    self.assertNotIn("FP-REF-ALERT", rt.waiting_for_link)
            finally:
                rt.waiting_for_link.clear()
                rt.waiting_for_link.update(orig_waiting)



class OrdersConcurrencyAndAlertsTests(unittest.TestCase):
    def test_apply_status_result_aborts_if_order_became_refunded_in_db(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.core import orders
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    100,
                    "FP-REF-RACE",
                    555,
                    "pending",
                    100.0,
                    "https://t.me/ch",
                    100,
                    1,
                    funpay_status="paid",
                )
                stale_snapshot = dict(db_orders.get_order("FP-REF-RACE"))
                db_orders.update_order_fields("FP-REF-RACE", funpay_status="refunded")

                cardinal = SimpleNamespace(account=SimpleNamespace(), telegram=SimpleNamespace())
                with mock.patch.object(orders, "_send_completion_notification_if_needed") as notify_mock:
                    orders._apply_status_result(
                        cardinal,
                        stale_snapshot,
                        {"status": "completed", "remains": "0", "charge": "1.0", "currency": "USD"},
                    )
                notify_mock.assert_not_called()
                latest = db_orders.get_order("FP-REF-RACE")
                self.assertEqual(latest["funpay_status"], "refunded")
                self.assertEqual(latest["status"], "pending")
        finally:
            rt.DB_PATH = orig_db

    def test_claim_completion_notification_allows_single_winner_under_concurrency(self):
        import tempfile
        import threading
        from pathlib import Path
        from src import runtime as rt
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    100,
                    "FP-CLAIM-COMP",
                    556,
                    "completed",
                    100.0,
                    "https://t.me/ch",
                    100,
                    1,
                    funpay_status="paid",
                )
                results = []
                lock = threading.Lock()
                barrier = threading.Barrier(6)

                def _worker():
                    barrier.wait(timeout=3)
                    won, _snap = db_orders.claim_completion_notification("FP-CLAIM-COMP")
                    with lock:
                        results.append(bool(won))

                threads = [threading.Thread(target=_worker) for _ in range(6)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=5)
                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), 5)
        finally:
            rt.DB_PATH = orig_db

    def test_manual_check_alert_is_not_claimed_without_bot_or_chat_and_dedups_once_sent(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.services import notifications
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        orig_bot = rt.bot
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    100,
                    "FP-MAN-ALERT",
                    557,
                    "manual_check",
                    100.0,
                    "https://t.me/ch",
                    100,
                    1,
                    funpay_status="paid",
                )

                rt.bot = None
                rec = db_orders.get_order("FP-MAN-ALERT")
                with mock.patch.object(notifications, "load_config", return_value={"notification_chat_id": ""}):
                    notifications._send_manual_check_alert_once(rec, "timeout")
                row = db_orders.get_order("FP-MAN-ALERT")
                self.assertFalse(row.get("manual_check_alert_sent"))

                sent = []
                rt.bot = SimpleNamespace(
                    send_message=lambda cid, text, **kw: sent.append((cid, text))
                )
                with mock.patch.object(
                    notifications, "load_config", return_value={"notification_chat_id": "12345"}
                ):
                    notifications._send_manual_check_alert_once(dict(row), "timeout")
                    notifications._send_manual_check_alert_once(dict(row), "timeout")
                self.assertEqual(len(sent), 1)
                self.assertTrue(db_orders.get_order("FP-MAN-ALERT").get("manual_check_alert_sent"))
        finally:
            rt.DB_PATH = orig_db
            rt.bot = orig_bot

    def test_refunds_handles_db_error_during_failure_recovery(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest import mock
        import requests
        from src import runtime as rt
        from src.core import refunds
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    100,
                    "FP-REF-DBERR",
                    99,
                    "pending",
                    100.0,
                    "https://t.me/ch",
                    100,
                    1,
                    funpay_status="refunded",
                )
                rec = db_orders.get_order("FP-REF-DBERR")
                with (
                    mock.patch.object(
                        refunds,
                        "_panel_api_json",
                        side_effect=requests.RequestException("panel timeout"),
                    ),
                    mock.patch.object(
                        refunds,
                        "update_order_fields",
                        side_effect=sqlite3.OperationalError("db readonly"),
                    ),
                    mock.patch.object(
                        refunds,
                        "load_config",
                        return_value={
                            "try_panel_cancel_after_funpay_refund": True,
                            "services": {"1": {"api_url": "https://p.example/api/v2", "api_key": "KEY123"}},
                        },
                    ),
                ):
                    ok = refunds._attempt_panel_cancel_after_funpay_refund(rec, source="funpay_refund")
                self.assertFalse(ok)
        finally:
            rt.DB_PATH = orig_db
