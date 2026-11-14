"""Regression tests for buyer-flow completion guards, numeric coercion edges, and service sorting."""

from __future__ import annotations

from pathlib import Path
import sys
from unittest import TestCase, mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


class ResilienceEdgeCasesTests(TestCase):
    def test_completed_order_ignores_trailing_mention_or_link_messages(self):
        from src.core import buyer_flow

        completed_order = {
            "order_id": "FP-DONE-1",
            "chat_id": 500,
            "buyer_id": 77,
            "status": "completed",
            "buyer_flow_state": buyer_flow.BUYER_STATE_COMPLETED,
            "panel_order_id": 9001,
        }
        cardinal = mock.MagicMock()
        with (
            mock.patch.object(buyer_flow, "_get_waiting_candidates_for_message", return_value=[]),
            mock.patch.object(buyer_flow, "_get_buyer_orders", return_value=[completed_order]),
            mock.patch.object(buyer_flow, "_send_buyer_message") as send_mock,
            mock.patch.object(buyer_flow, "_apply_refund_gate", return_value=False) as refund_mock,
        ):
            handled = buyer_flow._handle_buyer_flow_message(cardinal, "@new_channel", 77, 500)
        self.assertFalse(handled)
        send_mock.assert_not_called()
        refund_mock.assert_not_called()

    def test_upsert_buyer_flow_order_record_preserves_zero_order_price(self):
        from src.core import buyer_flow

        captured: list[dict] = []
        with (
            mock.patch.object(buyer_flow.db_orders, "get_order", return_value={"order_id": "FP-FREE-1", "summa": 99.0}),
            mock.patch.object(
                buyer_flow.db_orders,
                "upsert_order_dict",
                side_effect=lambda rec: captured.append(dict(rec)) or rec,
            ),
        ):
            ok = buyer_flow._upsert_buyer_flow_order_record(
                chat_id=10,
                buyer_id=20,
                buyer_username="buyer",
                order_id_funpay="FP-FREE-1",
                lot_title="Promo",
                order_price=0.0,
                order_amount=1,
                service_id=5,
                real_amount=100,
                service_number=1,
                state=buyer_flow.BUYER_STATE_WAITING_LINK,
            )
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["summa"], 0.0)
        self.assertEqual(captured[0]["order_price"], 0.0)

    def test_analytics_charge_ignores_bool_currency_argument(self):
        from src.services import analytics

        saved_batches: list[list[dict]] = []

        class _Store:
            @staticmethod
            def load_orders_data():
                return [{"order_id": "FP-BOOL-1", "chistota": 50.0, "summa": 50.0}]

            @staticmethod
            def save_orders_data(orders_data):
                saved_batches.append([dict(item) for item in orders_data])

        with mock.patch.object(analytics, "_resolve_charge_fx", return_value=(10.0, 90.0)):
            analytics._analytics_update_order_charge_and_net(
                "FP-BOOL-1",
                10.0,
                True,
                order_store=_Store(),
                load_log=lambda: [],
                write_log=lambda _path, _entries: True,
            )
        self.assertEqual(len(saved_batches), 1)
        self.assertEqual(saved_batches[0][0]["currency"], "USD")
        self.assertEqual(saved_batches[0][0]["chistota"], 40.0)

    def test_fallback_block_spec_rejects_zero_or_invalid_target_ids(self):
        from src.core import panel_submission

        for bad_id, bad_srv in [(0, 2), (10, 0), (10, False), ("", 2)]:
            spec = panel_submission._get_fallback_block_spec(
                primary_error="insufficient_funds",
                primary_detail="low balance",
                service_number=1,
                fallback_enabled=True,
                existing_order={},
                fallback_service_id=bad_id,
                fallback_service_number=bad_srv,
            )
            self.assertIsNotNone(spec)
            status, _detail, _short, stop_retry = spec
            self.assertEqual(status, "fallback_not_configured")
            self.assertFalse(stop_retry)

    def test_sort_service_items_orders_numeric_and_non_numeric_keys(self):
        from src.storage import storage

        ordered = storage.sort_service_items({"10": {"name": "Ten"}, "2": {"name": "Two"}, "alpha": {"name": "Alpha"}})
        self.assertEqual([k for k, _ in ordered], ["2", "10", "alpha"])

    def test_delete_service_prompts_confirmation_with_bound_lots_quote_and_confirm_deletes(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from src import plugin, runtime as rt
        from src.storage import storage
        from src.tg import panels as tg_panels

        orig_cfg_path = rt.CONFIG_PATH
        orig_bot = rt.bot
        sent_messages = []
        edited_messages = []
        answered_callbacks = []

        class _Bot:
            def clear_step_handler_by_chat_id(self, _chat_id):
                return None

            def send_message(self, chat_id, text, **kwargs):
                sent_messages.append((chat_id, text, kwargs))
                return SimpleNamespace(message_id=700 + len(sent_messages))

            def edit_message_text(self, text, chat_id, message_id, **kwargs):
                edited_messages.append((chat_id, message_id, text, kwargs))
                return SimpleNamespace(message_id=message_id)

            def answer_callback_query(self, call_id, text=None, **kwargs):
                answered_callbacks.append((call_id, text))

        with tempfile.TemporaryDirectory() as tmpdir:
            rt.CONFIG_PATH = str(Path(tmpdir) / "config.json")
            rt.bot = _Bot()
            try:
                lot_mapping = {
                    "lot_1": {"name": "Primary <Lot>", "service_number": 2, "service_id": 10, "quantity": 100},
                    "lot_2": {"name": "Fallback Lot", "service_number": 1, "fallback_service_number": 2, "service_id": 20, "quantity": 50},
                    "lot_3": {"name": "Other Lot", "service_number": 1, "service_id": 30, "quantity": 10},
                }
                for idx in range(4, 90):
                    lot_mapping[f"lot_{idx}"] = {
                        "name": f"Long bound lot title number {idx} with extra description",
                        "service_number": 2,
                        "service_id": idx,
                        "quantity": 10,
                    }
                storage.save_config(
                    {
                        "services": {
                            "1": {"name": "Main #1", "api_url": "https://one.example/api/v2", "api_key": "KEY1_123456"},
                            "2": {"name": "Panel Two #2", "api_url": "https://two.example/api/v2", "api_key": "KEY2_123456"},
                        },
                        "lot_mapping": lot_mapping,
                    }
                )

                tg_panels.process_delete_service(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="2"))
                self.assertIn("2", storage.load_config()["services"])
                self.assertEqual(len(sent_messages), 1)
                _chat, text, kwargs = sent_messages[0]
                self.assertIn("Вы уверены", text)
                self.assertIn("<blockquote expandable>", text)
                self.assertIn("Primary &lt;Lot&gt;", text)
                self.assertIn("Fallback Lot", text)
                self.assertIn("резервная", text)
                self.assertNotIn("Other Lot", text)
                self.assertLessEqual(len(text), 4096)
                cb_values = [btn.callback_data for row in kwargs["reply_markup"].keyboard for btn in row]
                self.assertIn("asmm:confirm_delete_service_2", cb_values)
                self.assertIn("asmm:api_settings", cb_values)

                with mock.patch.object(tg_panels.core_panel_balances, "schedule_panel_balances_refresh", return_value=None):
                    tg_panels.confirm_delete_service_callback(
                        SimpleNamespace(
                            id="cb_confirm_srv",
                            data="asmm:confirm_delete_service_2",
                            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=701),
                            from_user=SimpleNamespace(id=42),
                        )
                    )
                self.assertNotIn("2", storage.load_config()["services"])
                self.assertTrue(any("asmm:confirm_delete_service_" == prefix for prefix, _ in plugin._PREFIX_CALLBACK_ROUTES))
            finally:
                rt.CONFIG_PATH = orig_cfg_path
                rt.bot = orig_bot

    def test_manage_websites_has_no_duplicate_button_and_lots_keyboard_returns_to_lot_settings(self):
        from types import SimpleNamespace

        from src import runtime as rt
        from src.tg import lots as tg_lots, settings as tg_settings

        orig_bot = rt.bot
        edited = []

        class _Bot:
            def answer_callback_query(self, *_a, **_kw):
                return None

            def edit_message_text(self, text, chat_id, message_id, **kwargs):
                edited.append((chat_id, message_id, text, kwargs))

        rt.bot = _Bot()
        try:
            tg_settings.manage_websites(
                SimpleNamespace(
                    id="cb_sites",
                    message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                    from_user=SimpleNamespace(id=42),
                )
            )
            self.assertEqual(len(edited), 1)
            kb_sites = edited[0][3]["reply_markup"]
            callbacks = [btn.callback_data for row in kb_sites.keyboard for btn in row]
            self.assertEqual(callbacks.count("asmm:show_allowed_websites_list"), 1)
            self.assertEqual(
                callbacks,
                [
                    "asmm:show_allowed_websites_list",
                    "asmm:add_website",
                    "asmm:reset_valid_links_prompt",
                    "asmm:misc_settings",
                ],
            )
        finally:
            rt.bot = orig_bot

        kb_lots = tg_lots.generate_lots_keyboard(0)
        lot_callbacks = [btn.callback_data for row in kb_lots.keyboard for btn in row]
        self.assertIn("asmm:lot_settings", lot_callbacks)
        self.assertNotIn("asmm:return_to_settings", lot_callbacks)
