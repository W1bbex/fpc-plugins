import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core import buyer_messages


class BuyerMessageTests(unittest.TestCase):
    def test_template_supports_missing_fields(self):
        message = buyer_messages._render_template("Заказ {order_id_funpay}: {unknown}", {"order_id": "A42"})
        self.assertIn("A42", message)
        self.assertNotIn("{unknown}", message)

    def test_buyer_order_list_displays_order_and_state(self):
        order = {"order_id": "ABC123", "funpay_status": "open", "buyer_flow_state": "waiting_link", "lot_title": "Подписчики"}
        rendered = buyer_messages._format_buyer_smm_orders([order])
        self.assertIn("ABC123", rendered)
        self.assertIn("Подписчики", rendered)

    def test_message_splits_without_losing_lines(self):
        self.assertEqual(buyer_messages.split_message_by_lines("a\nb\nc", max_lines=2), ["a\nb", "c"])

    def test_order_reference_matches_case_insensitively(self):
        self.assertTrue(buyer_messages._matches_order_ref("ABC123", "#abc123"))
        self.assertFalse(buyer_messages._matches_order_ref("ABC123", "#abc124"))

    def test_template_resolves_legacy_order_id_alias_from_record(self):
        rendered = buyer_messages._render_template(
            "ID={panel_order_id}, legacy={twiboost_id}",
            {"order_id": "FP1", "id_zakaz": 777},
        )
        self.assertEqual(rendered, "ID=777, legacy=777")


class BuyerMessagesFormattingTests(unittest.TestCase):
    def test_zero_created_at_returns_dash_and_zero_numbers_are_preserved(self):
        from src.core import buyer_messages

        self.assertEqual(buyer_messages._format_created_at({"created_at": 0}), "—")
        self.assertEqual(buyer_messages._format_created_at({"created_at": None}), "—")

        ctx = buyer_messages._build_template_context(
            order_record={"order_id": "FP-ZERO-1", "summa": 0.0, "amount": 0, "created_at": 0, "service_id": 0},
        )
        self.assertEqual(buyer_messages._format_created_at(ctx), "—")
        rendered = buyer_messages._render_template(
            "sum={order_price};amt={order_amount};id={order_id_funpay}",
            {"order_id": "FP-ZERO-1", "summa": 0, "amount": 0, "created_at": 0},
        )
        self.assertEqual(rendered, "sum=0;amt=0;id=FP-ZERO-1")

