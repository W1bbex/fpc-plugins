import importlib
import re
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import models, runtime as rt
from src.core import (
    buyer_flow,
    buyer_messages,
    engine,
    funpay_sync,
    links,
    orders,
    panel_balances,
    panel_submission,
)
from src.services import notifications
from src.storage import db, db_orders, storage
from src.tg import files, panels, templates


def _build_fake_funpay_modules():
    """Build minimal in-memory FunPayAPI stubs when FunPayAPI is not installed."""
    funpay = types.ModuleType("FunPayAPI")
    common = types.ModuleType("FunPayAPI.common")
    utils = types.ModuleType("FunPayAPI.common.utils")
    updater = types.ModuleType("FunPayAPI.updater")
    events = types.ModuleType("FunPayAPI.updater.events")
    enums = types.ModuleType("FunPayAPI.enums")

    class _FakeRegularExpressions:
        ORDER_ID = re.compile(r"#[A-Z0-9]{8}")

    utils.RegularExpressions = _FakeRegularExpressions
    events.NewMessageEvent = type("NewMessageEvent", (), {})
    events.NewOrderEvent = type("NewOrderEvent", (), {})
    events.OrderStatusChangedEvent = type("OrderStatusChangedEvent", (), {})
    enums.OrderStatuses = SimpleNamespace(
        PAID="PAID",
        CLOSED="CLOSED",
        REFUNDED="REFUNDED",
        PARTIALLY_REFUNDED="PARTIALLY_REFUNDED",
    )
    enums.MessageTypes = SimpleNamespace(
        ORDER_CONFIRMED="ORDER_CONFIRMED",
        ORDER_CONFIRMED_BY_ADMIN="ORDER_CONFIRMED_BY_ADMIN",
        REFUND="REFUND",
        REFUND_BY_ADMIN="REFUND_BY_ADMIN",
        PARTIAL_REFUND="PARTIAL_REFUND",
        ORDER_REOPENED="ORDER_REOPENED",
    )
    common.utils = utils
    updater.events = events
    funpay.common = common
    funpay.updater = updater
    funpay.enums = enums
    return {
        "FunPayAPI": funpay,
        "FunPayAPI.common": common,
        "FunPayAPI.common.utils": utils,
        "FunPayAPI.updater": updater,
        "FunPayAPI.updater.events": events,
        "FunPayAPI.enums": enums,
    }


class AutoSmmRegressionsTests(unittest.TestCase):
    def test_bare_domains_do_not_bypass_link_validation(self):
        domains = lambda: ["t.me"]
        self.assertIsNone(links._links_extract_buyer_target("evil.com", allowed_domains=domains))
        self.assertIsNone(links._links_extract_buyer_target("www.evil.com", allowed_domains=domains))
        self.assertTrue(links._links_looks_like_link_without_https("evil.com", allowed_domains=domains))

        ok_domain, _ = links._links_is_valid_buyer_target("evil.com", allowed_domains=domains)
        self.assertFalse(ok_domain)

        ok_handle, _ = links._links_is_valid_buyer_target("@john.doe", allowed_domains=domains)
        self.assertTrue(ok_handle)

        extracted_allowed = links._links_extract_buyer_target("t.me/channel", allowed_domains=domains)
        self.assertEqual(extracted_allowed, "https://t.me/channel")
        self.assertTrue(links._links_is_valid_buyer_target(extracted_allowed, allowed_domains=domains)[0])

    def test_lot_match_result_to_dict_keeps_error_and_matches(self):
        res = models.LotMatchResult(ok=False)
        payload = res.to_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "no_match")
        self.assertEqual(payload["matches"], [])

    def test_batch_status_error_falls_back_to_single_sync_except_auth_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            orig_running = rt.RUNNING
            try:
                rt.RUNNING = True
                with (
                    mock.patch.object(rt, "DB_PATH", str(db_path)),
                    mock.patch.object(
                        orders,
                        "_fetch_batch_order_status",
                        return_value={"ok": False, "error": "unsupported_action", "detail": "disabled"},
                    ),
                    mock.patch.object(
                        orders,
                        "_fetch_single_order_status",
                        return_value={"ok": True, "status": "completed", "charge": 5.0, "currency": "RUB", "remains": 0},
                    ) as single_mock,
                    mock.patch.object(orders, "_send_completion_notification_if_needed"),
                ):
                    storage.save_orders_data(
                        [
                            {
                                "order_id": "FP-BATCH-FB",
                                "chat_id": 100,
                                "id_zakaz": 888,
                                "panel_order_id": 888,
                                "service_number": 1,
                                "status": "pending",
                                "summa": 50.0,
                                "quantity": 100,
                                "funpay_status": "open",
                            }
                        ]
                    )
                    orders._check_pending_orders_batch(SimpleNamespace())
                    single_mock.assert_called_once_with(1, 888)
                    row = storage.get_order_data("FP-BATCH-FB")
                    self.assertEqual(row["status"], "completed")

                with (
                    mock.patch.object(rt, "DB_PATH", str(db_path)),
                    mock.patch.object(
                        orders,
                        "_fetch_batch_order_status",
                        return_value={"ok": False, "error": "invalid_api_key", "detail": "bad key"},
                    ),
                    mock.patch.object(orders, "_fetch_single_order_status") as single_auth_mock,
                ):
                    storage.save_orders_data(
                        [
                            {
                                "order_id": "FP-BATCH-AUTH",
                                "chat_id": 100,
                                "id_zakaz": 889,
                                "panel_order_id": 889,
                                "service_number": 1,
                                "status": "pending",
                                "funpay_status": "open",
                            }
                        ]
                    )
                    orders._check_pending_orders_batch(SimpleNamespace())
                    single_auth_mock.assert_not_called()
            finally:
                rt.RUNNING = orig_running

    def test_order_ref_lookup_scopes_to_caller_on_panel_id_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.save_orders_data(
                    [
                        {"order_id": "FP-BUYER-1", "buyer_id": 101, "chat_id": 1001, "panel_order_id": 777, "id_zakaz": 777},
                        {"order_id": "FP-BUYER-2", "buyer_id": 202, "chat_id": 2002, "panel_order_id": 777, "id_zakaz": 777},
                    ]
                )
                self.assertIsNone(orders._find_order_by_any_order_ref("777"))
                scoped = orders._find_order_by_any_order_ref(
                    "777",
                    buyer_id=101,
                    buyer_chat_id=1001,
                    access_checker=buyer_flow._buyer_can_access_order,
                )
                self.assertIsNotNone(scoped)
                self.assertEqual(scoped["order_id"], "FP-BUYER-1")

    def test_inflight_balance_worker_does_not_pop_newer_worker_entry(self):
        cfg = {"services": {"1": {"name": "P1", "api_url": "https://p1.example/api/v2", "api_key": "valid-key-123"}}}
        entered_first = threading.Event()
        release_first = threading.Event()
        hold_new = threading.Event()
        release_new = threading.Event()
        call_count = 0

        def controlled_api(api_url, payload, timeout=6):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                entered_first.set()
                release_first.wait(timeout=3)
            else:
                hold_new.set()
                release_new.wait(timeout=3)
            return SimpleNamespace(status_code=200), {"balance": "30.0", "currency": "RUB"}, "", False

        orig_cache = {k: (dict(v) if isinstance(v, dict) else v) for k, v in rt.PANEL_BALANCE_CACHE.items()}
        try:
            panel_balances.invalidate_panel_balance_cache()
            done1 = threading.Event()
            started1 = panel_balances.schedule_panel_balances_refresh(
                cfg, force=True, api_json_fn=controlled_api, on_complete=lambda *_: done1.set()
            )
            self.assertTrue(started1)
            self.assertTrue(entered_first.wait(timeout=2))

            panel_balances.invalidate_panel_balance_cache()
            done2 = threading.Event()
            started2 = panel_balances.schedule_panel_balances_refresh(
                cfg, force=True, api_json_fn=controlled_api, on_complete=lambda *_: done2.set()
            )
            self.assertTrue(started2)
            self.assertTrue(hold_new.wait(timeout=2))

            release_first.set()
            self.assertTrue(done1.wait(timeout=2))
            with rt.PANEL_BALANCE_CACHE_LOCK:
                self.assertIn("all", rt.PANEL_BALANCE_CACHE.get("inflight") or {})

            release_new.set()
            self.assertTrue(done2.wait(timeout=2))
        finally:
            rt.PANEL_BALANCE_CACHE.clear()
            rt.PANEL_BALANCE_CACHE.update(orig_cache)

    def test_manual_alert_skips_when_unclaimed_even_without_snapshot_and_charge_skips_errors(self):
        sent = []
        fake_bot = SimpleNamespace(send_message=lambda cid, text, **kw: sent.append((cid, text)))
        with (
            mock.patch.object(rt, "bot", fake_bot),
            mock.patch.object(notifications, "load_config", return_value={"notification_chat_id": 123}),
            mock.patch.object(db_orders, "claim_manual_check_alert", return_value=(False, None)),
        ):
            notifications._send_manual_check_alert_once({"order_id": "FP-MISS", "manual_check_alert_sent": False}, "err")
        self.assertEqual(sent, [])

        with (
            mock.patch.object(notifications, "_validate_api_key_for_action", return_value=None),
            mock.patch.object(
                notifications,
                "_panel_api_json",
                return_value=(SimpleNamespace(status_code=200), {"error": "Pending charge unavailable"}, "", False),
            ),
            mock.patch.object(notifications, "update_order_charge_and_net") as update_charge_mock,
        ):
            res = notifications._fetch_start_notification_charge(
                "FP-NOCHARGE", 111, "https://p.example/api/v2", "k", 100.0, 1
            )
        self.assertEqual(res, (None, None, None, None, None))
        update_charge_mock.assert_not_called()

    def test_funpay_terminal_refund_regression_blocked_and_negative_chistota_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                db.init_db(str(db_path))
                db_orders.upsert_order("users-123-456", "FPSTRCHAT", 10, "completed", -15.0, "https://t.me/a", 100, 1, funpay_status="refunded")
                rec = db_orders.get_order("FPSTRCHAT")
                self.assertEqual(rec["chat_id"], "users-123-456")

                db_orders.upsert_order("users-123-456", "FPSTRCHAT", 10, "completed", 100.0, "https://t.me/a", 100, 1, funpay_status="open")
                rec_after = db_orders.get_order("FPSTRCHAT")
                self.assertEqual(rec_after["funpay_status"], "refunded")
                self.assertAlmostEqual(rec_after["chistota"], -15.0)

                res = funpay_sync._apply_funpay_status_update(
                    "FPSTRCHAT",
                    rec_after,
                    "open",
                    checked_at="check-1",
                    source="test",
                    refund_source="test",
                )
                self.assertEqual(res["funpay_status"], "refunded")
                self.assertEqual(db_orders.get_order("FPSTRCHAT")["funpay_status"], "refunded")

                wrong_chat_msg = SimpleNamespace(
                    author_id=0,
                    i_am_seller=True,
                    chat_id="users-999-888",
                    type=SimpleNamespace(name="ORDER_REOPENED"),
                    text="Заказ #FPSTRCHAT",
                )
                self.assertFalse(funpay_sync._sync_funpay_status_from_event(None, wrong_chat_msg))

    def test_persist_created_order_preserves_refunded_and_refill_rejects_empty_api_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                db.init_db(str(db_path))
                db_orders.upsert_order(
                    77,
                    "FP-REF-CR",
                    None,
                    "canceled",
                    0.0,
                    "",
                    100,
                    1,
                    is_refunded=True,
                    funpay_status="refunded",
                    panel_status="canceled",
                )
                panel_submission._persist_created_order_records(
                    77, "FP-REF-CR", 903, "https://t.me/c", 100, 1, 50.0, False, None
                )
                row = db_orders.get_order("FP-REF-CR")
                self.assertEqual(row["buyer_flow_state"], engine.BUYER_STATE_REFUNDED)
                self.assertEqual(row["panel_status"], "canceled")
                self.assertEqual(row["status"], "canceled")

                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP-REFILL-EMPTY",
                            "buyer_id": 555,
                            "chat_id": 5550,
                            "panel_order_id": 321,
                            "id_zakaz": 321,
                            "service_number": 1,
                            "funpay_status": "open",
                        }
                    ]
                )
                sent_buyer = []
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=lambda oid: SimpleNamespace(status="PAID")),
                    send_message=lambda cid, text: sent_buyer.append((cid, text)),
                )
                stubs = _build_fake_funpay_modules() if "FunPayAPI" not in sys.modules else {}
                orig_events_mod = sys.modules.pop("src.core.funpay_events", None)
                try:
                    with mock.patch.dict(sys.modules, stubs):
                        funpay_events = importlib.import_module("src.core.funpay_events")
                        with (
                            mock.patch.object(
                                funpay_events,
                                "load_config",
                                return_value={"services": {"1": {"api_url": "", "api_key": "k"}}},
                            ),
                            mock.patch.object(funpay_events, "_panel_api_json") as panel_json_mock,
                        ):
                            handled = funpay_events._handle_buyer_refill_command(
                                cardinal, "рефилл 321", 555, 5550
                            )
                        self.assertTrue(handled)
                        panel_json_mock.assert_not_called()
                        self.assertEqual(sent_buyer, [(5550, funpay_events._REFILL_UNAVAILABLE_TEXT)])
                finally:
                    sys.modules.pop("src.core.funpay_events", None)
                    if orig_events_mod is not None:
                        sys.modules["src.core.funpay_events"] = orig_events_mod

    def test_partial_panel_status_marks_failed_and_alerts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(orders, "_send_manual_check_alert_once") as alert_mock,
                mock.patch("src.services.currency.get_rate_to_rub", return_value=1.0),
            ):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP-PARTIAL-1",
                            "chat_id": 100,
                            "id_zakaz": 606,
                            "panel_order_id": 606,
                            "service_number": 1,
                            "status": "pending",
                            "summa": 100.0,
                            "quantity": 100,
                            "funpay_status": "open",
                        }
                    ]
                )
                rec = storage.get_order_data("FP-PARTIAL-1")
                orders._apply_status_result(
                    SimpleNamespace(),
                    rec,
                    {"ok": True, "status": "partial", "charge": 10.0, "currency": "RUB", "remains": 40},
                )
                stored = storage.get_order_data("FP-PARTIAL-1")
                self.assertEqual(stored["buyer_flow_state"], engine.BUYER_STATE_FAILED)
                alert_mock.assert_called_once()
                self.assertEqual(buyer_messages._buyer_status_label(stored), "выполнен частично (требует проверки)")

    def test_tg_templates_length_cap_and_panels_html_escape_and_delete_running_restore(self):
        raw_html = "<tag&attr>" * 1000
        body_esc, suffix = templates._truncate_escaped_pre_body(
            raw_html,
            lambda b, w="": f"<b>Title</b>\n<pre>{b}</pre>{w}\n<b>Footer</b>",
        )
        full_msg = f"<b>Title</b>\n<pre>{body_esc}</pre>{suffix}\n<b>Footer</b>"
        self.assertLessEqual(len(full_msg), 4096)
        self.assertIn("&lt;tag&amp;attr&gt;", body_esc)
        with mock.patch.object(templates, "load_config", return_value={"messages": {"after_payment": raw_html}}):
            rendered_view = templates._render_template_view_text("after_payment")
        self.assertLessEqual(len(rendered_view), 4096)
        self.assertIn("&lt;tag&amp;attr&gt;", rendered_view)

        panel_cfg = {"services": {"1": {"name": "SMM & <Top>", "api_url": "https://p.example/api/v2", "api_key": "secret123"}}}
        with mock.patch.object(
            panels.core_panel_balances,
            "get_cached_service_balance",
            return_value={"status_emoji": "✅", "balance_text": "100 ₽"},
        ):
            menu_text, _kb = panels.render_api_menu(panel_cfg, use_cache_only=True)
            card_text, _kb2 = panels.render_api_service_card(panel_cfg, 1, use_cache_only=True)
        self.assertIn("SMM &amp; &lt;Top&gt;", menu_text)
        self.assertNotIn("<Top>", menu_text)
        self.assertIn("SMM &amp; &lt;Top&gt;", card_text)
        self.assertNotIn("<Top>", card_text)

        orig_running = rt.RUNNING
        sent_files = []
        fake_bot = SimpleNamespace(send_message=lambda cid, text, **kw: sent_files.append((cid, text)))
        msg = SimpleNamespace(chat=SimpleNamespace(id=50), from_user=SimpleNamespace(id=10))
        try:
            rt.RUNNING = True
            with (
                mock.patch.object(rt, "bot", fake_bot),
                mock.patch.object(files, "_is_authorized_tg_user", return_value=True),
                mock.patch.object(files, "_create_full_backup", return_value=None),
            ):
                files.auto_smm_delete(msg)
            self.assertTrue(rt.RUNNING)
            self.assertEqual(len(sent_files), 1)
            with (
                mock.patch.object(rt, "bot", fake_bot),
                mock.patch.object(files, "_is_authorized_tg_user", return_value=True),
                mock.patch.object(files, "_create_full_backup", side_effect=OSError("disk full")),
            ):
                files.auto_smm_delete(msg)
            self.assertTrue(rt.RUNNING)
            self.assertEqual(len(sent_files), 2)
        finally:
            rt.RUNNING = orig_running

    def test_init_commands_tolerates_missing_telegram(self):
        stubs = _build_fake_funpay_modules() if "FunPayAPI" not in sys.modules else {}
        orig_plugin = sys.modules.pop("src.plugin", None)
        orig_events = sys.modules.pop("src.core.funpay_events", None)
        try:
            with mock.patch.dict(sys.modules, stubs):
                plugin_mod = importlib.import_module("src.plugin")
                plugin_mod.init_commands(SimpleNamespace(telegram=None))
        finally:
            sys.modules.pop("src.plugin", None)
            sys.modules.pop("src.core.funpay_events", None)
            if orig_plugin is not None:
                sys.modules["src.plugin"] = orig_plugin
            if orig_events is not None:
                sys.modules["src.core.funpay_events"] = orig_events

    def test_callback_routes_and_navigation_use_clean_asmm_namespace(self):
        from src.tg import common as tg_common

        stubs = _build_fake_funpay_modules() if "FunPayAPI" not in sys.modules else {}
        orig_plugin = sys.modules.pop("src.plugin", None)
        orig_events = sys.modules.pop("src.core.funpay_events", None)
        try:
            with mock.patch.dict(sys.modules, stubs):
                plugin_mod = importlib.import_module("src.plugin")
                all_routes = [r for r, _ in plugin_mod._EXACT_CALLBACK_ROUTES] + [
                    r for r, _ in plugin_mod._PREFIX_CALLBACK_ROUTES
                ]
                for route in all_routes:
                    self.assertTrue(route.startswith("asmm:"), route)
                    self.assertNotIn("autosmm", route)
                    self.assertLessEqual(len(route.encode("utf-8")), 64, route)
                for route in plugin_mod._TELEMETRY_ALLOWED_CALLBACKS:
                    self.assertTrue(route.startswith("asmm:"), route)
                    self.assertNotIn("autosmm", route)
                for target in tg_common._NAVIGATION_TARGETS:
                    self.assertTrue(target.startswith("asmm:"), target)
                    self.assertNotIn("autosmm", target)
                for prefix, _handler in tg_common._NAVIGATION_PREFIXES:
                    self.assertTrue(prefix.startswith("asmm:"), prefix)
                    self.assertNotIn("autosmm", prefix)
        finally:
            sys.modules.pop("src.plugin", None)
            sys.modules.pop("src.core.funpay_events", None)
            if orig_plugin is not None:
                sys.modules["src.plugin"] = orig_plugin
            if orig_events is not None:
                sys.modules["src.core.funpay_events"] = orig_events


if __name__ == "__main__":
    unittest.main()
