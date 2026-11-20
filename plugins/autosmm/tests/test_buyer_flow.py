import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.core import buyer_flow, buyer_messages, engine, panel_submission
from src.storage import storage


class BuyerFlowTests(unittest.TestCase):
    def test_concurrent_upsert_buyer_flow_order_record_preserves_all_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.save_orders_data([])
                errors = []

                def writer(idx: int) -> None:
                    try:
                        buyer_flow._upsert_buyer_flow_order_record(
                            chat_id=1000 + idx,
                            buyer_id=2000 + idx,
                            buyer_username=f"user_{idx}",
                            order_id_funpay=f"FP{idx}",
                            lot_title=f"Lot {idx}",
                            order_price=50 + idx,
                            order_amount=1,
                            service_id=10 + idx,
                            real_amount=100 + idx,
                            service_number=1,
                        )
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                orders = storage.load_orders_data()
                self.assertEqual(len(orders), 12)
                for idx in range(12):
                    record = storage.get_order_data(f"FP{idx}")
                    self.assertIsNotNone(record)
                    self.assertEqual(record["buyer_flow_state"], engine.BUYER_STATE_WAITING_LINK)
                    self.assertEqual(record["total_quantity"], 100 + idx)

    def test_block_fallback_and_refund_persists_decision_and_triggers_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(panel_submission, "refund_order") as mock_refund,
            ):
                storage.save_orders_data([{"order_id": "FP77", "funpay_status": "open"}])
                dummy_cardinal = SimpleNamespace()
                panel_submission._block_fallback_and_refund(
                    dummy_cardinal,
                    "FP77",
                    555,
                    1,
                    "service_inactive",
                    "fallback_disabled",
                    detailed_reason="Fallback disabled",
                    bot_action="stopped",
                    log_manual_check=True,
                )

                stored = storage.get_order_data("FP77")
                self.assertFalse(stored["fallback_used"])
                self.assertEqual(stored["fallback_reason"], "fallback_disabled")
                mock_refund.assert_called_once()
                self.assertEqual(mock_refund.call_args.args[:3], (dummy_cardinal, "FP77", 555))

    def test_transition_waiting_state_updates_runtime_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            waiting = {}
            selection = {}
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "VALID_LINKS_PATH", str(Path(tmp) / "valid_link.json")),
                mock.patch.object(rt, "waiting_for_link", waiting),
                mock.patch.object(rt, "BUYER_ORDER_SELECTION", selection),
            ):
                storage.save_orders_data([{"order_id": "FP88", "funpay_status": "open"}])
                ctx = {"order_id_funpay": "FP88", "buyer_id": 42, "chat_id": 777}
                buyer_flow._transition_waiting_state(
                    ctx, "FP88", 42, 777, engine.BUYER_STATE_WAITING_CONFIRM, saved_link_marker="https://t.me/test"
                )
                self.assertIn("FP88", waiting)
                self.assertEqual(selection, {"buyer:42": "FP88"})
                self.assertEqual(storage.get_order_data("FP88")["saved_link"], "https://t.me/test")

                buyer_flow._transition_waiting_state(
                    ctx, "FP88", 42, 777, engine.BUYER_STATE_LINK_PAUSED, saved_link_marker=None, select_order=False
                )
                self.assertEqual(selection, {})
                self.assertIsNone(storage.get_order_data("FP88").get("saved_link"))
                self.assertEqual(storage.get_order_data("FP88")["buyer_flow_state"], engine.BUYER_STATE_LINK_PAUSED)

    def test_fallback_timeout_routes_to_manual_check_without_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            waiting = {}
            selection = {}
            cfg = {
                "fallback_enabled": True,
                "services": {
                    "1": {"api_url": "https://p1.example/api/v2", "api_key": "k1", "name": "P1"},
                    "2": {"api_url": "https://p2.example/api/v2", "api_key": "k2", "name": "P2"},
                },
                "lot_mapping": {
                    "lot_1": {
                        "name": "Lot 1",
                        "service_id": 55,
                        "service_number": 1,
                        "fallback_service_id": 202,
                        "fallback_service_number": 2,
                    }
                },
            }
            send_calls = []

            def fake_send(**kwargs):
                send_calls.append(kwargs)
                if kwargs["service_number"] == 1:
                    return False, None, {"error": "insufficient_funds", "detail": "not enough balance"}
                return False, None, {"error": "timeout", "detail": "read timed out"}

            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "VALID_LINKS_PATH", str(Path(tmp) / "valid_link.json")),
                mock.patch.object(rt, "waiting_for_link", waiting),
                mock.patch.object(rt, "BUYER_ORDER_SELECTION", selection),
                mock.patch.object(panel_submission, "load_config", return_value=cfg),
                mock.patch.object(panel_submission, "send_order_to_service", side_effect=fake_send),
                mock.patch.object(
                    panel_submission,
                    "_get_panel_balance_for_risk_check",
                    return_value={"ok": True, "balance": 50.0, "currency": "RUB"},
                ),
                mock.patch.object(panel_submission, "refund_order") as mock_refund,
                mock.patch.object(panel_submission, "_send_buyer_message") as mock_buyer_msg,
                mock.patch.object(panel_submission, "_send_transport_error_after_add_alert") as mock_alert,
                mock.patch.object(panel_submission, "_TRANSPORT_RISK_DELAY_SECONDS", 0.0),
            ):
                buyer_flow._upsert_buyer_flow_order_record(
                    chat_id=777,
                    buyer_id=42,
                    buyer_username="buyer",
                    order_id_funpay="FP_FB_TO",
                    lot_title="Lot 1",
                    order_price=100.0,
                    order_amount=1,
                    service_id=55,
                    real_amount=100,
                    service_number=1,
                    lot_key="lot_1",
                    state=engine.BUYER_STATE_WAITING_CONFIRM,
                    saved_link="https://t.me/channel",
                )
                ctx = buyer_flow._build_waiting_context_from_order(storage.get_order_data("FP_FB_TO"))
                ctx["link"] = "https://t.me/channel"
                dummy_cardinal = SimpleNamespace(account=SimpleNamespace(refund=mock.MagicMock()))

                panel_submission.process_link_without_confirmation(dummy_cardinal, ctx)

                self.assertEqual([c["service_number"] for c in send_calls], [1, 2])
                mock_refund.assert_not_called()
                dummy_cardinal.account.refund.assert_not_called()
                stored = storage.get_order_data("FP_FB_TO")
                self.assertEqual(stored["buyer_flow_state"], engine.BUYER_STATE_FAILED)
                self.assertEqual(stored["panel_status"], "manual_check")
                self.assertEqual(stored["status"], "unknown")
                self.assertEqual(stored["status_stop_reason"], "transport_error_after_add")
                self.assertEqual(stored["service_number"], 2)
                self.assertEqual(stored["fallback_reason"], "insufficient_funds")
                deadline = time.monotonic() + 2.0
                while not mock_alert.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                mock_alert.assert_called_once()
                self.assertEqual(mock_alert.call_args.kwargs["service_number"], 2)
                self.assertEqual(
                    mock_buyer_msg.call_args.args[2],
                    buyer_messages.BUYER_ADD_TRANSPORT_MANUAL_TEXT,
                )

    def test_persist_failure_after_add_blocks_duplicate_reconfirm_and_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            waiting = {}
            selection = {}
            cfg = {
                "fallback_enabled": False,
                "services": {"1": {"api_url": "https://p1.example/api/v2", "api_key": "k1", "name": "P1"}},
                "lot_mapping": {"lot_1": {"name": "Lot 1", "service_id": 55, "service_number": 1}},
            }
            add_calls = []

            def fake_add(**kwargs):
                add_calls.append(kwargs)
                return True, 991001, {"order": 991001}

            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "VALID_LINKS_PATH", str(Path(tmp) / "valid_link.json")),
                mock.patch.object(rt, "waiting_for_link", waiting),
                mock.patch.object(rt, "BUYER_ORDER_SELECTION", selection),
                mock.patch.object(panel_submission, "load_config", return_value=cfg),
                mock.patch.object(panel_submission, "send_order_to_service", side_effect=fake_add),
                mock.patch.object(
                    panel_submission,
                    "_get_panel_balance_for_risk_check",
                    return_value={"ok": True, "balance": 10.0, "currency": "RUB"},
                ),
                mock.patch.object(
                    panel_submission,
                    "upsert_order_data",
                    side_effect=sqlite3.OperationalError("simulated write failure"),
                ),
                mock.patch.object(panel_submission, "_send_admin_order_alert") as mock_admin_alert,
                mock.patch.object(panel_submission, "_send_buyer_message"),
                mock.patch.object(buyer_flow, "_send_buyer_message"),
                mock.patch.object(buyer_flow, "_apply_refund_gate", return_value=False),
                mock.patch.object(panel_submission, "_apply_refund_gate", return_value=False),
            ):
                buyer_flow._upsert_buyer_flow_order_record(
                    chat_id=777,
                    buyer_id=42,
                    buyer_username="buyer",
                    order_id_funpay="FP_DUP1",
                    lot_title="Lot 1",
                    order_price=50.0,
                    order_amount=1,
                    service_id=55,
                    real_amount=100,
                    service_number=1,
                    lot_key="lot_1",
                    state=engine.BUYER_STATE_WAITING_CONFIRM,
                    saved_link="https://t.me/test_chan",
                )
                ctx = buyer_flow._build_waiting_context_from_order(storage.get_order_data("FP_DUP1"))
                ctx["link"] = "https://t.me/test_chan"
                waiting["FP_DUP1"] = dict(ctx)
                dummy_cardinal = SimpleNamespace()

                panel_submission.process_link_without_confirmation(dummy_cardinal, dict(ctx))

                self.assertEqual(len(add_calls), 1)
                self.assertNotIn("FP_DUP1", waiting)
                stored = storage.get_order_data("FP_DUP1")
                self.assertTrue(stored["panel_add_submitted"])
                self.assertEqual(stored["buyer_flow_state"], engine.BUYER_STATE_FAILED)
                self.assertEqual(stored["panel_status"], "manual_check")
                self.assertEqual(stored["status_stop_reason"], "persist_failed_after_add")
                self.assertEqual(stored["candidate_panel_order_id"], 991001)
                mock_admin_alert.assert_called_once()

                handled = buyer_flow._handle_buyer_flow_message(dummy_cardinal, "да", 42, 777)
                self.assertFalse(handled)
                panel_submission.process_link_without_confirmation(dummy_cardinal, dict(ctx))
                self.assertEqual(len(add_calls), 1)

            add_calls.clear()
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "VALID_LINKS_PATH", str(Path(tmp) / "valid_link.json")),
                mock.patch.object(rt, "waiting_for_link", {}),
                mock.patch.object(rt, "BUYER_ORDER_SELECTION", {}),
                mock.patch.object(panel_submission, "load_config", return_value=cfg),
                mock.patch.object(panel_submission, "send_order_to_service", side_effect=fake_add),
                mock.patch.object(
                    panel_submission,
                    "_get_panel_balance_for_risk_check",
                    return_value={"ok": True, "balance": 10.0, "currency": "RUB"},
                ),
                mock.patch.object(panel_submission, "_send_buyer_message"),
                mock.patch.object(panel_submission, "send_order_started_notification"),
                mock.patch.object(panel_submission, "check_order_status"),
                mock.patch.object(panel_submission, "_apply_refund_gate", return_value=False),
            ):
                buyer_flow._upsert_buyer_flow_order_record(
                    chat_id=778,
                    buyer_id=43,
                    buyer_username="buyer2",
                    order_id_funpay="FP_RACE",
                    lot_title="Lot 1",
                    order_price=50.0,
                    order_amount=1,
                    service_id=55,
                    real_amount=100,
                    service_number=1,
                    lot_key="lot_1",
                    state=engine.BUYER_STATE_WAITING_CONFIRM,
                    saved_link="https://t.me/race_chan",
                )
                race_ctx = buyer_flow._build_waiting_context_from_order(storage.get_order_data("FP_RACE"))
                race_ctx["link"] = "https://t.me/race_chan"
                dummy_cardinal = SimpleNamespace()
                barrier = threading.Barrier(4)
                errors = []

                def worker():
                    try:
                        barrier.wait(timeout=5)
                        panel_submission.process_link_without_confirmation(dummy_cardinal, dict(race_ctx))
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)

                self.assertEqual(errors, [])
                self.assertEqual(len(add_calls), 1)
                self.assertEqual(storage.get_order_data("FP_RACE")["panel_order_id"], 991001)

    def test_submission_picks_fallback_from_matched_lot_among_identical_primaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            cfg = {
                "fallback_enabled": True,
                "services": {
                    "1": {"api_url": "https://p1.example/api/v2", "api_key": "k1", "name": "P1"},
                    "2": {"api_url": "https://p2.example/api/v2", "api_key": "k2", "name": "P2"},
                    "3": {"api_url": "https://p3.example/api/v2", "api_key": "k3", "name": "P3"},
                },
                "lot_mapping": {
                    "lot_1": {
                        "name": "First Lot",
                        "service_id": 55,
                        "service_number": 1,
                        "fallback_service_id": 101,
                        "fallback_service_number": 2,
                    },
                    "lot_2": {
                        "name": "Second Lot",
                        "service_id": 55,
                        "service_number": 1,
                        "fallback_service_id": 202,
                        "fallback_service_number": 3,
                    },
                },
            }
            calls = []

            def fake_add(**kwargs):
                calls.append((kwargs["service_number"], kwargs["service_id"]))
                if kwargs["service_number"] == 1:
                    return False, None, {"error": "service_inactive", "detail": "inactive"}
                return True, 5050, {"order": 5050}

            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "VALID_LINKS_PATH", str(Path(tmp) / "valid_link.json")),
                mock.patch.object(rt, "waiting_for_link", {}),
                mock.patch.object(rt, "BUYER_ORDER_SELECTION", {}),
                mock.patch.object(panel_submission, "load_config", return_value=cfg),
                mock.patch.object(panel_submission, "send_order_to_service", side_effect=fake_add),
                mock.patch.object(
                    panel_submission,
                    "_get_panel_balance_for_risk_check",
                    return_value={"ok": True, "balance": 10.0, "currency": "RUB"},
                ),
                mock.patch.object(panel_submission, "send_order_started_notification"),
                mock.patch.object(panel_submission, "check_order_status"),
                mock.patch.object(panel_submission, "_send_buyer_message"),
                mock.patch.object(panel_submission, "_apply_refund_gate", return_value=False),
            ):
                buyer_flow._upsert_buyer_flow_order_record(
                    chat_id=900,
                    buyer_id=77,
                    buyer_username="buyer3",
                    order_id_funpay="FP_LOT2",
                    lot_title="Second Lot",
                    order_price=80.0,
                    order_amount=1,
                    service_id=55,
                    real_amount=200,
                    service_number=1,
                    lot_key="lot_2",
                    state=engine.BUYER_STATE_WAITING_CONFIRM,
                    saved_link="https://t.me/second_lot",
                )
                ctx = buyer_flow._build_waiting_context_from_order(storage.get_order_data("FP_LOT2"))
                ctx["link"] = "https://t.me/second_lot"

                panel_submission.process_link_without_confirmation(SimpleNamespace(), ctx)

                self.assertEqual(calls, [(1, 55), (3, 202)])
                stored = storage.get_order_data("FP_LOT2")
                self.assertEqual(stored["panel_order_id"], 5050)
                self.assertEqual(stored["service_number"], 3)
                self.assertTrue(stored["fallback_used"])



class BuyerFlowEdgeTests(unittest.TestCase):
    def test_duplicate_new_order_event_does_not_overwrite_refunded_or_active_states(self):
        import tempfile
        from pathlib import Path
        from src import runtime as rt
        from src.core import buyer_flow, engine
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        orig_wait = dict(rt.waiting_for_link)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)

                db_orders.upsert_order(
                    101,
                    "FP-DUP-REF",
                    None,
                    "cancelled",
                    0.0,
                    "",
                    100,
                    1,
                    is_refunded=True,
                    funpay_status="refunded",
                    buyer_id=77,
                    buyer_username="buyer1",
                    buyer_flow_state=engine.BUYER_STATE_REFUNDED,
                )
                buyer_flow._upsert_buyer_flow_order_record(
                    101,
                    77,
                    "buyer1",
                    "FP-DUP-REF",
                    "Lot",
                    50.0,
                    1,
                    10,
                    100,
                    1,
                    funpay_status="open",
                    state=engine.BUYER_STATE_WAITING_LINK,
                )
                ref_row = db_orders.get_order("FP-DUP-REF")
                self.assertEqual(ref_row["funpay_status"], "refunded")
                self.assertNotEqual(engine._get_buyer_flow_state(ref_row), engine.BUYER_STATE_WAITING_LINK)

                db_orders.upsert_order(
                    101,
                    "FP-DUP-CONF",
                    None,
                    "pending",
                    50.0,
                    "",
                    100,
                    1,
                    funpay_status="open",
                    buyer_id=77,
                    buyer_username="buyer1",
                    buyer_flow_state=engine.BUYER_STATE_WAITING_CONFIRM,
                )
                buyer_flow._upsert_buyer_flow_order_record(
                    101,
                    77,
                    "buyer1",
                    "FP-DUP-CONF",
                    "Lot",
                    50.0,
                    1,
                    10,
                    100,
                    1,
                    funpay_status="open",
                    state=engine.BUYER_STATE_WAITING_LINK,
                )
                conf_row = db_orders.get_order("FP-DUP-CONF")
                self.assertEqual(engine._get_buyer_flow_state(conf_row), engine.BUYER_STATE_WAITING_CONFIRM)

                db_orders.upsert_order(
                    101,
                    "FP-DUP-ACT",
                    501,
                    "pending",
                    50.0,
                    "https://t.me/ch",
                    100,
                    1,
                    funpay_status="open",
                    buyer_id=77,
                    buyer_username="buyer1",
                    buyer_flow_state=engine.BUYER_STATE_PANEL_CREATED,
                )
                buyer_flow._upsert_buyer_flow_order_record(
                    101,
                    77,
                    "buyer1",
                    "FP-DUP-ACT",
                    "Lot",
                    50.0,
                    1,
                    10,
                    100,
                    1,
                    funpay_status="open",
                    state=engine.BUYER_STATE_WAITING_LINK,
                )
                act_row = db_orders.get_order("FP-DUP-ACT")
                self.assertEqual(engine._get_buyer_flow_state(act_row), engine.BUYER_STATE_PANEL_CREATED)
                self.assertEqual(act_row["panel_order_id"], 501)
        finally:
            rt.DB_PATH = orig_db
            rt.waiting_for_link.clear()
            rt.waiting_for_link.update(orig_wait)

    def test_duplicate_order_event_keeps_manual_check_after_panel_add(self):
        order_id = "FP-MANUAL"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rt, "DB_PATH", str(Path(tmp) / "autosmm.db")):
            storage.save_orders_data([{
                "order_id": order_id,
                "buyer_flow_state": engine.BUYER_STATE_WAITING_LINK,
                "panel_status": "manual_check",
                "status_stop_reason": "transport_error_after_add",
                "order_price": 50,
            }])
            before = storage.get_order_data(order_id)
            self.assertEqual(before["panel_status"], "manual_check")
            self.assertEqual(before["status_stop_reason"], "transport_error_after_add")

            updated = buyer_flow._upsert_buyer_flow_order_record(
                chat_id=101,
                buyer_id=77,
                buyer_username="buyer1",
                order_id_funpay=order_id,
                lot_title="Lot",
                order_price=100,
                order_amount=1,
                service_id=10,
                real_amount=100,
                service_number=1,
            )

            self.assertFalse(updated)
            self.assertEqual(storage.get_order_data(order_id), before)

    def test_persist_created_order_records_updates_chistota_and_preserves_refunded(self):
        import tempfile
        from pathlib import Path
        from src import runtime as rt
        from src.core import panel_submission
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    77,
                    "FP-CHIST-1",
                    None,
                    "waiting_link",
                    0.0,
                    "",
                    100,
                    1,
                    funpay_status="open",
                    summa=0.0,
                )
                panel_submission._persist_created_order_records(
                    77,
                    "FP-CHIST-1",
                    901,
                    "https://t.me/ch",
                    100,
                    1,
                    120.0,
                    False,
                    None,
                )
                row1 = db_orders.get_order("FP-CHIST-1")
                self.assertAlmostEqual(row1["chistota"], 120.0)
                self.assertEqual(row1["panel_order_id"], 901)

                db_orders.upsert_order(
                    77,
                    "FP-CHIST-REF",
                    None,
                    "waiting_link",
                    0.0,
                    "",
                    100,
                    1,
                    is_refunded=True,
                    funpay_status="refunded",
                    summa=0.0,
                )
                panel_submission._persist_created_order_records(
                    77,
                    "FP-CHIST-REF",
                    902,
                    "https://t.me/ch",
                    100,
                    1,
                    120.0,
                    False,
                    None,
                )
                row2 = db_orders.get_order("FP-CHIST-REF")
                self.assertEqual(row2["funpay_status"], "refunded")
        finally:
            rt.DB_PATH = orig_db

    def test_chitchat_messages_without_links_are_ignored_when_no_waiting_orders(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.core import buyer_flow

        orig_wait = dict(rt.waiting_for_link)
        try:
            rt.waiting_for_link.clear()
            get_order_mock = mock.MagicMock()
            send_msg_mock = mock.MagicMock()
            cardinal = SimpleNamespace(
                account=SimpleNamespace(get_order=get_order_mock, send_message=send_msg_mock)
            )
            with mock.patch.object(buyer_flow, "_get_waiting_candidates_for_message", return_value=[]):
                for text in ("thanks", "hello", "ok."):
                    handled = buyer_flow._handle_buyer_flow_message(cardinal, text, 555, 8888)
                    self.assertFalse(handled)
            get_order_mock.assert_not_called()
            send_msg_mock.assert_not_called()
        finally:
            rt.waiting_for_link.clear()
            rt.waiting_for_link.update(orig_wait)

    def test_cancel_command_does_not_revert_or_report_failure_if_db_write_fails_after_refund(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.core import buyer_flow, engine
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        orig_wait = dict(rt.waiting_for_link)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    9001,
                    "FP-CANCEL-DBFAIL",
                    None,
                    "waiting_link",
                    50.0,
                    "",
                    100,
                    1,
                    funpay_status="open",
                    buyer_id=77,
                    buyer_username="buyer1",
                    buyer_flow_state=engine.BUYER_STATE_LINK_PAUSED,
                )
                rt.waiting_for_link.clear()
                rt.waiting_for_link["FP-CANCEL-DBFAIL"] = {
                    "chat_id": 9001,
                    "buyer_id": 77,
                    "order_id_funpay": "FP-CANCEL-DBFAIL",
                    "state": engine.BUYER_STATE_LINK_PAUSED,
                }
                refund_calls = []
                sent_messages = []
                cardinal = SimpleNamespace(
                    send_message=lambda cid, txt, **kw: sent_messages.append((cid, txt)),
                    account=SimpleNamespace(
                        refund=lambda oid: refund_calls.append(oid),
                        send_message=lambda cid, txt, **kw: sent_messages.append((cid, txt)),
                    ),
                )
                order_rec = db_orders.get_order("FP-CANCEL-DBFAIL")
                with (
                    mock.patch.object(buyer_flow, "_resolve_buyer_order_for_action", return_value=order_rec),
                    mock.patch.object(buyer_flow, "_apply_refund_gate", return_value=False),
                    mock.patch.object(buyer_flow, "load_config", return_value={"allow_buyer_cancel_before_panel_order": True}),
                    mock.patch.object(buyer_flow, "_can_buyer_refund_pre_panel", return_value=(True, {})),
                    mock.patch.object(buyer_flow, "_send_admin_order_alert"),
                    mock.patch.object(buyer_flow, "update_order_fields", side_effect=sqlite3.OperationalError("db busy")),
                ):
                    handled = buyer_flow._handle_buyer_cancel_command(cardinal, "!отмена", 77, 9001)
                self.assertTrue(handled)
                self.assertEqual(refund_calls, ["FP-CANCEL-DBFAIL"])
                self.assertNotIn("FP-CANCEL-DBFAIL", rt.waiting_for_link)
                combined = "\n".join(txt for _, txt in sent_messages).lower()
                self.assertNotIn("не удалось оформить возврат", combined)
                self.assertIn("отменён", combined)
        finally:
            rt.DB_PATH = orig_db
            rt.waiting_for_link.clear()
            rt.waiting_for_link.update(orig_wait)

    def test_startup_recovery_does_not_block_and_verifies_refunds_async(self):
        import tempfile
        import threading
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.core import buyer_flow, engine
        from src.storage import db, db_orders

        orig_db = rt.DB_PATH
        orig_wait = dict(rt.waiting_for_link)
        orig_running = rt.RUNNING
        rt.STOP_EVENT.clear()
        rt.RUNNING = True
        gate = threading.Event()
        batch_called = threading.Event()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    7001,
                    "FP-RECOV-ASYNC",
                    None,
                    "waiting_link",
                    100.0,
                    "",
                    200,
                    1,
                    funpay_status="open",
                    buyer_id=55,
                    buyer_username="buyer_async",
                    buyer_flow_state=engine.BUYER_STATE_WAITING_LINK,
                )

                def slow_batch(cardinal_arg, order_ids):
                    batch_called.set()
                    gate.wait(timeout=2.0)
                    return {"FP-RECOV-ASYNC": {"ok": True, "funpay_status": "refunded"}}

                cardinal = SimpleNamespace(account=SimpleNamespace(id=1), send_message=mock.MagicMock())
                with mock.patch.object(buyer_flow, "_fetch_funpay_statuses_batch", side_effect=slow_batch):
                    t0 = time.monotonic()
                    buyer_flow._recover_buyer_flow_runtime(cardinal)
                    elapsed = time.monotonic() - t0
                    self.assertLess(elapsed, 0.25)
                    self.assertIn("FP-RECOV-ASYNC", rt.waiting_for_link)
                    self.assertTrue(batch_called.wait(timeout=1.5))
                    gate.set()
                    rt.join_background_threads(timeout=2.0)

                stored = db_orders.get_order("FP-RECOV-ASYNC")
                self.assertEqual(stored["funpay_status"], "refunded")
                self.assertEqual(stored["buyer_flow_state"], "refunded")
                self.assertNotIn("FP-RECOV-ASYNC", rt.waiting_for_link)
        finally:
            gate.set()
            rt.join_background_threads(timeout=2.0)
            rt.RUNNING = orig_running
            rt.STOP_EVENT.clear()
            rt.DB_PATH = orig_db
            rt.waiting_for_link.clear()
            rt.waiting_for_link.update(orig_wait)
