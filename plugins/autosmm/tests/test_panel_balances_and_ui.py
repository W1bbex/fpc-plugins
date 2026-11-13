import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.core import buyer_flow, links, panel_api, panel_balances, panel_submission, refunds
from src.models import OrderRecord
from src.services import analytics as analytics_svc, currency
from src.storage import db, db_orders, storage
from src.tg import (
    analytics as tg_analytics,
    common as tg_common,
    lots as tg_lots,
    panels as tg_panels,
    settings as tg_settings,
    templates as tg_templates,
)


def _make_fake_bot():
    calls = {"messages": [], "edits": [], "answers": []}
    next_id = [100]

    def send_message(chat_id, text, **kwargs):
        next_id[0] += 1
        calls["messages"].append((chat_id, text, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=next_id[0], text=text)

    def edit_message_text(text, chat_id, message_id, **kwargs):
        calls["edits"].append((chat_id, message_id, text, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id, text=text)

    def answer_callback_query(call_id, text=None, **kwargs):
        calls["answers"].append((call_id, text))

    fake_bot = SimpleNamespace(
        send_message=send_message,
        edit_message_text=edit_message_text,
        answer_callback_query=answer_callback_query,
        clear_step_handler_by_chat_id=lambda cid: None,
        register_next_step_handler=lambda *a, **kw: None,
    )
    return fake_bot, calls


class PanelBalancesCacheTests(unittest.TestCase):
    def setUp(self):
        rt.clear_memory_contexts()
        panel_balances.invalidate_panel_balance_cache()

    def tearDown(self):
        rt.clear_memory_contexts()
        panel_balances.invalidate_panel_balance_cache()

    def test_cached_menu_renders_without_network_and_respects_12h_ttl(self):
        cfg = {
            "services": {
                "1": {"name": "Panel 1", "api_url": "https://p1.example/api/v2", "api_key": "REALKEY12345"},
            },
            "lot_mapping": {},
        }
        with mock.patch.object(panel_balances, "_panel_api_json", side_effect=AssertionError("unexpected network I/O")):
            cached_text = panel_balances.get_cached_total_balance_text(cfg)
            api_text, _kb = tg_panels.render_api_menu(cfg, use_cache_only=True)
            card_text, _card_kb = tg_panels.render_api_service_card(cfg, 1, use_cache_only=True)

        self.assertEqual(cached_text, "недоступно")
        self.assertIn("Общий баланс: недоступно", api_text)
        self.assertIn("Баланс:", card_text)
        self.assertTrue(panel_balances.is_panel_balance_cache_stale(max_age_seconds=43200))

        poll_calls = []

        def fake_api_json(api_url, params, timeout=6):
            poll_calls.append((api_url, params, timeout))
            return None, {"balance": "250.50", "currency": "RUB"}, "", False

        updated, total_text = panel_balances.refresh_panel_balances_sync(
            cfg,
            force=False,
            max_age_seconds=43200,
            api_json_fn=fake_api_json,
        )
        self.assertTrue(updated)
        self.assertEqual(total_text, "250.50 ₽")
        self.assertEqual(len(poll_calls), 1)
        self.assertFalse(panel_balances.is_panel_balance_cache_stale(max_age_seconds=43200))

        # Subsequent call within 12h TTL skips network polling.
        updated_again, cached_again = panel_balances.refresh_panel_balances_sync(
            cfg,
            force=False,
            max_age_seconds=43200,
            api_json_fn=fake_api_json,
        )
        self.assertFalse(updated_again)
        self.assertEqual(cached_again, "250.50 ₽")
        self.assertEqual(len(poll_calls), 1)

        # Simulated cache timestamp older than 12 hours becomes stale and polls again.
        rt.PANEL_BALANCE_CACHE["timestamp"] = time.time() - 43205
        self.assertTrue(panel_balances.is_panel_balance_cache_stale(max_age_seconds=43200))
        updated_expired, _ = panel_balances.refresh_panel_balances_sync(
            cfg,
            force=False,
            max_age_seconds=43200,
            api_json_fn=fake_api_json,
        )
        self.assertTrue(updated_expired)
        self.assertEqual(len(poll_calls), 2)

    def test_inflight_dedup_and_screen_revision_prevents_stale_ui_overwrite(self):
        fake_bot, calls = _make_fake_bot()
        cfg = {
            "services": {
                "1": {"name": "Panel 1", "api_url": "https://p1.example/api/v2", "api_key": "REALKEY12345"},
            },
            "lot_mapping": {},
        }
        gate = threading.Event()
        done_first = threading.Event()
        done_second = threading.Event()
        api_calls = []

        def slow_api_json(api_url, params, timeout=3):
            api_calls.append(api_url)
            gate.wait(timeout=2.0)
            return None, {"balance": "777.00", "currency": "RUB"}, "", False

        orig_bot = rt.bot
        try:
            rt.bot = fake_bot
            rev1 = rt.bump_screen_revision(500, 10)
            self.assertEqual(rev1, 1)

            def on_screen_one(updated, _txt):
                tg_common._edit_screen_if_revision_matches(
                    500,
                    10,
                    rev1,
                    lambda: ("MAIN MENU SHOULD NOT OVERWRITE", tg_common._inline_kb(("Back", "x"))),
                )
                done_first.set()

            started_first = panel_balances.schedule_panel_balances_refresh(
                cfg,
                force=True,
                on_complete=on_screen_one,
                api_json_fn=slow_api_json,
            )
            self.assertTrue(started_first)

            # User clicks into another screen before the slow balance poll resolves.
            rev2 = rt.bump_screen_revision(500, 10)
            self.assertEqual(rev2, 2)

            def on_screen_two(updated, _txt):
                tg_common._edit_screen_if_revision_matches(
                    500,
                    10,
                    rev2,
                    lambda: tg_panels.render_api_menu(cfg, use_cache_only=True),
                    parse_mode=None,
                )
                done_second.set()

            started_second = panel_balances.schedule_panel_balances_refresh(
                cfg,
                force=True,
                on_complete=on_screen_two,
                api_json_fn=slow_api_json,
            )
            # Concurrent request reuses the in-flight batch without spawning a second HTTP call.
            self.assertFalse(started_second)

            gate.set()
            self.assertTrue(done_first.wait(timeout=2.0))
            self.assertTrue(done_second.wait(timeout=2.0))
            self.assertEqual(len(api_calls), 1)
            self.assertEqual(len(calls["edits"]), 1)
            self.assertIn("777.00 ₽", calls["edits"][0][2])
            self.assertNotIn("MAIN MENU SHOULD NOT OVERWRITE", calls["edits"][0][2])
        finally:
            gate.set()
            rt.bot = orig_bot

    def test_invalidate_cache_generation_drops_stale_inflight_write(self):
        cfg = {
            "services": {
                "1": {"name": "Panel 1", "api_url": "https://p1.example/api/v2", "api_key": "REALKEY12345"},
            },
        }
        started_http = threading.Event()
        allow_finish = threading.Event()
        finished = threading.Event()

        def slow_poll(api_url, params, timeout=6):
            started_http.set()
            allow_finish.wait(timeout=2.0)
            return None, {"balance": "999.00", "currency": "RUB"}, "", False

        outcome = []

        def run_sync():
            outcome.append(panel_balances.refresh_panel_balances_sync(cfg, force=True, api_json_fn=slow_poll))
            finished.set()

        t = threading.Thread(target=run_sync, daemon=True)
        t.start()
        self.assertTrue(started_http.wait(timeout=2.0))
        panel_balances.invalidate_panel_balance_cache()
        allow_finish.set()
        self.assertTrue(finished.wait(timeout=2.0))
        self.assertEqual(outcome[0][0], False)
        self.assertEqual(rt.PANEL_BALANCE_CACHE["text"], "недоступно")

    def test_finalize_created_order_schedules_single_panel_balance_refresh(self):
        with (
            mock.patch.object(panel_submission, "_persist_created_order_records"),
            mock.patch.object(panel_submission, "schedule_single_panel_balance_refresh") as mock_refresh,
            mock.patch.object(panel_submission, "get_order_data", return_value={"order_id": "FP1"}),
            mock.patch.object(panel_submission, "_is_order_refundedish", return_value=False),
            mock.patch.object(panel_submission, "send_order_started_notification"),
            mock.patch.object(panel_submission, "check_order_status"),
            mock.patch.object(panel_submission, "_send_buyer_order_confirmation"),
        ):
            panel_submission._finalize_created_order(
                c=SimpleNamespace(),
                cfg={"services": {"2": {"api_url": "https://p.example/api/v2", "api_key": "K1"}}},
                order_id_funpay="FP1",
                buyer_chat_id=100,
                panel_order_id=999,
                link_="https://t.me/test",
                lot_price=10.0,
                real_amount=100,
                service_number=2,
            )
        mock_refresh.assert_called_once()
        self.assertEqual(mock_refresh.call_args.args[0], 2)


class PaginationAndUiFixesTests(unittest.TestCase):
    def setUp(self):
        rt.clear_memory_contexts()

    def tearDown(self):
        rt.clear_memory_contexts()

    def test_allowed_websites_pagination_limits_buttons_and_preserves_page_on_back(self):
        fake_bot, calls = _make_fake_bot()
        domains = [f"site{idx}.example" for idx in range(45)]
        orig_bot = rt.bot
        try:
            rt.bot = fake_bot
            with mock.patch.object(tg_settings, "load_valid_links", return_value=list(domains)):
                call_list = SimpleNamespace(
                    id="cb1",
                    data="asmm:show_allowed_websites_list",
                    message=SimpleNamespace(chat=SimpleNamespace(id=300), message_id=50),
                )
                tg_settings.show_allowed_websites_list(call_list)
                self.assertEqual(rt.WEBSITE_PAGE_CONTEXT[300], 0)
                _, _, text0, kwargs0 = calls["edits"][-1]
                kb0 = kwargs0["reply_markup"]
                callbacks0 = [btn.callback_data for row in kb0.keyboard for btn in row]
                self.assertIn("стр. 1/3", text0)
                self.assertIn("asmm:sites_page_1", callbacks0)
                self.assertNotIn("asmm:sites_page_-1", callbacks0)
                self.assertLessEqual(len(callbacks0), 30)

                call_page1 = SimpleNamespace(
                    id="cb2",
                    data="asmm:sites_page_1",
                    message=SimpleNamespace(chat=SimpleNamespace(id=300), message_id=50),
                )
                tg_settings.allowed_websites_page_callback(call_page1)
                self.assertEqual(rt.WEBSITE_PAGE_CONTEXT[300], 1)
                _, _, text1, kwargs1 = calls["edits"][-1]
                kb1 = kwargs1["reply_markup"]
                callbacks1 = [btn.callback_data for row in kb1.keyboard for btn in row]
                self.assertIn("стр. 2/3", text1)
                self.assertIn("asmm:sites_page_0", callbacks1)
                self.assertIn("asmm:sites_page_2", callbacks1)

                call_del = SimpleNamespace(
                    id="cb3",
                    data="asmm:delete_website_site25.example",
                    message=SimpleNamespace(chat=SimpleNamespace(id=300), message_id=50),
                )
                with mock.patch.object(tg_settings, "_is_delete_confirm_grace_active", return_value=False):
                    tg_settings.remove_website_prompt(call_del)
                kb_del = calls["edits"][-1][3]["reply_markup"]
                del_callbacks = [btn.callback_data for row in kb_del.keyboard for btn in row]
                self.assertIn("asmm:sites_page_1", del_callbacks)
        finally:
            rt.bot = orig_bot

    def test_search_results_pagination_and_stale_context_pruning(self):
        fake_bot, calls = _make_fake_bot()
        lot_map = {
            f"lot_{idx}": {"name": f"Promo Lot {idx}", "service_id": 100 + idx, "quantity": 50, "service_number": 1}
            for idx in range(25)
        }
        orig_bot = rt.bot
        try:
            rt.bot = fake_bot
            with mock.patch.object(tg_lots, "load_config", return_value={"lot_mapping": lot_map}):
                tg_lots._render_search_results(400, "promo", message_id=77, page=0)
                ctx = rt.SEARCH_RESULT_CONTEXT[400]
                self.assertEqual(ctx["page"], 0)
                self.assertIsInstance(ctx["created_ts"], float)
                kb0 = calls["edits"][-1][3]["reply_markup"]
                cbs0 = [btn.callback_data for row in kb0.keyboard for btn in row]
                self.assertIn("asmm:search_page_1", cbs0)
                self.assertLessEqual(len(cbs0), 15)

                call_page1 = SimpleNamespace(
                    id="cbs1",
                    data="asmm:search_page_1",
                    message=SimpleNamespace(chat=SimpleNamespace(id=400), message_id=77),
                )
                tg_lots.search_page_navigation(call_page1)
                self.assertEqual(rt.SEARCH_RESULT_CONTEXT[400]["page"], 1)
                tg_lots.search_results_back(call_page1)
                self.assertEqual(rt.SEARCH_RESULT_CONTEXT[400]["page"], 1)
                self.assertIn("стр. 2/3", calls["edits"][-1][2])

            analytics_svc._set_analytics_view_mode(400, 77, "more")
            self.assertEqual(analytics_svc._get_analytics_view_mode(400, 77), "more")
            rt.SEARCH_RESULT_CONTEXT[400]["created_ts"] = time.time() - 7200
            rt.ANALYTICS_VIEW_MODES[(400, 77)]["created_ts"] = time.time() - 7200
            removed = rt.prune_stale_contexts(max_ui_age_seconds=3600)
            self.assertGreaterEqual(removed, 2)
            self.assertNotIn(400, rt.SEARCH_RESULT_CONTEXT)
            self.assertNotIn((400, 77), rt.ANALYTICS_VIEW_MODES)
        finally:
            rt.bot = orig_bot

    def test_analytics_2x2_grid_and_template_edit_back_returns_to_template(self):
        kb = tg_analytics.build_analytics_keyboard("24h", "main")
        self.assertEqual(len(kb.keyboard[0]), 2)
        self.assertEqual(len(kb.keyboard[1]), 2)

        fake_bot, calls = _make_fake_bot()
        orig_bot = rt.bot
        try:
            rt.bot = fake_bot
            call = SimpleNamespace(
                id="cbt",
                data="asmm:template_edit_after_payment",
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(id=555), message_id=66),
            )
            with mock.patch.object(tg_templates, "load_config", return_value={"messages": {"after_payment": "hi"}}):
                tg_templates.template_edit_callback(call)
            state = tg_common._get_ui_input_state(555)
            self.assertEqual(state["back_target"], "asmm:template_view_after_payment")

            back_call = SimpleNamespace(
                id="cbb",
                data="asmm:ui_input_back",
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(id=555), message_id=66),
            )
            with mock.patch.object(tg_templates, "load_config", return_value={"messages": {"after_payment": "hi"}}):
                tg_common.ui_input_back_callback(back_call)
            self.assertIn("после оплаты", calls["edits"][-1][2])
        finally:
            rt.bot = orig_bot


class RegressionContractsTests(unittest.TestCase):
    def test_claim_panel_refund_cancel_is_atomic_and_auto_refunds_defaults_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            orig_db = rt.DB_PATH
            try:
                rt.DB_PATH = str(db_path)
                db.init_db(str(db_path))
                storage.upsert_order_data(
                    111,
                    "FP-RACE-CANCEL",
                    9001,
                    "pending",
                    50.0,
                    "https://t.me/test",
                    100,
                    1,
                    funpay_status="refunded",
                )
                cancel_requests = []
                barrier = threading.Barrier(2)

                def fake_api_json(api_url, params, timeout=10):
                    cancel_requests.append(params.get("orders"))
                    return None, {"cancel": [{"order": 9001, "cancel": 1}]}, "", False

                cfg = {
                    "try_panel_cancel_after_funpay_refund": True,
                    "services": {"1": {"api_url": "https://p.example/api/v2", "api_key": "REALKEY123"}},
                }

                def worker():
                    rec = db_orders.get_order("FP-RACE-CANCEL")
                    barrier.wait()
                    refunds._attempt_panel_cancel_after_funpay_refund(rec, source="test_race")

                with (
                    mock.patch.object(refunds, "load_config", return_value=cfg),
                    mock.patch.object(refunds, "_panel_api_json", side_effect=fake_api_json),
                ):
                    t1 = threading.Thread(target=worker)
                    t2 = threading.Thread(target=worker)
                    t1.start()
                    t2.start()
                    t1.join(timeout=3.0)
                    t2.join(timeout=3.0)

                self.assertEqual(cancel_requests, ["9001"])

                cardinal = SimpleNamespace(account=SimpleNamespace(refund=mock.MagicMock()))
                with (
                    mock.patch.object(refunds, "load_config", return_value={}),
                    mock.patch.object(refunds, "get_order_data", return_value={"order_id": "FP-DEF"}),
                    mock.patch.object(refunds, "_send_admin_order_alert"),
                ):
                    refunds.refund_order(cardinal, "FP-DEF", 111, "reason")
                cardinal.account.refund.assert_not_called()
            finally:
                rt.DB_PATH = orig_db

    def test_balance_risk_check_rejects_none_and_transport_alert_is_nonblocking(self):
        cfg = {"services": {"1": {"api_url": "https://p.example/api/v2", "api_key": "REALKEY123"}}}
        with (
            mock.patch.object(panel_api, "load_config", return_value=cfg),
            mock.patch.object(
                panel_api,
                "_panel_api_json",
                side_effect=[
                    (None, {"balance": None, "currency": "RUB"}, "", False),
                    (None, {"balance": "   ", "currency": "RUB"}, "", False),
                ],
            ),
        ):
            res_none = panel_api._get_panel_balance_for_risk_check(1)
            res_empty = panel_api._get_panel_balance_for_risk_check(1)
        self.assertFalse(res_none["ok"])
        self.assertFalse(res_empty["ok"])

        gate = threading.Event()
        buyer_sent = []
        alerts_sent = []
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            orig_db = rt.DB_PATH
            try:
                rt.DB_PATH = str(db_path)
                db.init_db(str(db_path))

                def delayed_risk(_svc_num):
                    gate.wait(timeout=2.0)
                    return {"ok": True, "balance": 10.0, "currency": "RUB"}

                with (
                    mock.patch.object(panel_submission.time, "sleep"),
                    mock.patch.object(panel_submission, "_get_panel_balance_for_risk_check", side_effect=delayed_risk),
                    mock.patch.object(panel_submission, "_send_buyer_message", side_effect=lambda *a: buyer_sent.append(a)),
                    mock.patch.object(panel_submission, "_send_transport_error_after_add_alert", side_effect=lambda **kw: alerts_sent.append(kw)),
                ):
                    worker = panel_submission._handle_transport_error_after_add(
                        SimpleNamespace(),
                        "FP-NONBLOCK",
                        888,
                        1,
                        55,
                        100,
                        "https://t.me/test",
                        "timeout",
                        "timed out",
                        wait_before_balance_check=True,
                    )
                    # DB state and buyer message must already be persisted while background check is waiting.
                    stored = db_orders.get_order("FP-NONBLOCK")
                    self.assertIsNotNone(stored)
                    self.assertEqual(stored["panel_status"], "manual_check")
                    self.assertEqual(len(buyer_sent), 1)
                    self.assertEqual(len(alerts_sent), 0)
                    gate.set()
                    self.assertIsNotNone(worker)
                    worker.join(timeout=2.0)
                    self.assertEqual(len(alerts_sent), 1)
            finally:
                gate.set()
                rt.DB_PATH = orig_db

    def test_order_record_defaults_progress_columns_links_64_fx_zero_and_single_get_order(self):
        rec = OrderRecord.from_dict(
            {
                "order_id": "FP-MOD",
                "summa": None,
                "order_price": 75.5,
                "chistota": None,
                "service_number": "",
            }
        )
        self.assertEqual(rec.summa, 75.5)
        self.assertEqual(rec.chistota, 75.5)
        self.assertEqual(rec.service_number, 1)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            orig_db = rt.DB_PATH
            try:
                rt.DB_PATH = str(db_path)
                storage.upsert_order_data(1, "FP-ZERO-PROG", None, "pending", 10.0, "https://t.me/a", 50, 1)
                row = db_orders.get_order("FP-ZERO-PROG")
                expected_progress = {
                    "total_quantity": 50,
                    "done_quantity": 0,
                    "left_quantity": 50,
                    "progress_percent": 0,
                }
                for key, expected_val in expected_progress.items():
                    self.assertIn(key, row)
                    self.assertEqual(row[key], expected_val)
            finally:
                rt.DB_PATH = orig_db

        long_user = "a" * 55
        self.assertEqual(links._extract_buyer_target(long_user), long_user)

        with mock.patch.object(currency, "get_rate_to_rub", return_value=None) as rate_mock:
            rub, fx = currency.convert_to_rub(0.0, "USD", allow_network=True, config_loader=lambda: {})
        self.assertEqual(rub, 0.0)
        self.assertIsNone(fx)
        self.assertFalse(rate_mock.call_args.kwargs["allow_network"])

        get_order_calls = []
        order_obj = SimpleNamespace(status="PAID")

        def fake_get_order(oid):
            get_order_calls.append(oid)
            return order_obj

        refund_calls = []
        cardinal = SimpleNamespace(
            account=SimpleNamespace(
                get_order=fake_get_order,
                refund=lambda oid: refund_calls.append(oid),
                send_message=lambda *a, **kw: None,
            )
        )
        with (
            mock.patch.object(
                buyer_flow,
                "_resolve_buyer_order_for_action",
                return_value={"order_id": "FP-ONE-GET", "buyer_id": 7, "chat_id": 9, "funpay_status": "open"},
            ),
            mock.patch.object(buyer_flow, "load_config", return_value={"allow_buyer_cancel_before_panel_order": True}),
            mock.patch.object(buyer_flow, "get_order_data", return_value={"order_id": "FP-ONE-GET", "buyer_id": 7, "chat_id": 9, "funpay_status": "open"}),
            mock.patch.object(buyer_flow, "update_order_fields"),
            mock.patch.object(buyer_flow, "_send_buyer_message"),
            mock.patch.object(buyer_flow, "_transition_waiting_state"),
        ):
            buyer_flow._handle_buyer_cancel_command(cardinal, "!отмена", 7, 9)
        self.assertEqual(len(get_order_calls), 1)
        self.assertEqual(refund_calls, ["FP-ONE-GET"])


if __name__ == "__main__":
    unittest.main()
