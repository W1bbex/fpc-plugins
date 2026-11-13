import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core import engine


class LotMatchingTests(unittest.TestCase):
    def test_refund_state_overrides_active_panel_order(self):
        order = {"funpay_status": "refunded", "panel_order_id": 42, "buyer_flow_state": "waiting_link"}
        self.assertEqual(engine._get_buyer_flow_state(order), "refunded")

    def test_panel_order_prevents_waiting_for_link(self):
        order = {"funpay_status": "open", "panel_order_id": 42}
        self.assertEqual(engine._get_buyer_flow_state(order), "panel_created")

    def test_one_match_uses_order_quantity(self):
        mapping = {"lot_1": {"name": "Подписчики", "quantity": 100, "service_id": 7, "service_number": 1}}
        result = engine._engine_resolve_lot_by_description("Заказ: Подписчики", 2, mapping=mapping)
        self.assertTrue(result["ok"])
        self.assertEqual(result["real_amount"], 200)

    def test_ambiguous_matches_do_not_choose_a_lot(self):
        mapping = {"a": {"name": "Telegram"}, "b": {"name": "Telegram подписчики"}}
        result = engine._engine_resolve_lot_by_description("Telegram подписчики", 1, mapping=mapping)
        self.assertEqual(result["error"], "ambiguous_match")

    def test_fallback_targets_follow_matched_lot_key_and_title(self):
        cfg = {
            "lot_mapping": {
                "lot_1": {
                    "name": "Telegram Boost",
                    "service_id": 55,
                    "service_number": 1,
                    "fallback_service_id": 101,
                    "fallback_service_number": 2,
                },
                "lot_2": {
                    "name": "Instagram Boost",
                    "service_id": 55,
                    "service_number": 1,
                    "fallback_service_id": 202,
                    "fallback_service_number": 3,
                },
            }
        }
        fb_id, fb_srv = engine._get_lot_fallback_targets(
            cfg, 55, 1, lot_key="lot_2", lot_title="Instagram Boost"
        )
        self.assertEqual((fb_id, fb_srv), (202, 3))

        reindexed_cfg = {
            "lot_mapping": {
                "lot_1": cfg["lot_mapping"]["lot_2"],
            }
        }
        fb_shifted = engine._get_lot_fallback_targets(
            reindexed_cfg, 55, 1, lot_key="lot_2", lot_title="Instagram Boost"
        )
        self.assertEqual(fb_shifted, (202, 3))

        missing_lot_fb = engine._get_lot_fallback_targets(
            cfg, 55, 1, lot_key="lot_deleted", lot_title="Deleted Lot"
        )
        self.assertEqual(missing_lot_fb, (None, None))


class EngineConcurrencyTests(unittest.TestCase):
    def test_lot_matching_remains_safe_during_concurrent_mapping_reload(self):
        import threading
        from src import runtime as rt
        from src.core import engine
        from src.storage import storage

        original_mapping = dict(rt.lot_mapping)
        errors = []
        stop_event = threading.Event()
        valid_lot = {
            "funpay_name": "Telegram Channel Subscribers",
            "id_yslygi": 101,
            "api_name": "panel1",
        }

        def _mutator():
            try:
                for idx in range(250):
                    with storage.CONFIG_LOCK:
                        rt.lot_mapping.clear()
                        rt.lot_mapping[f"lot_{idx % 5 + 1}"] = dict(valid_lot)
            except Exception as exc:
                errors.append(exc)
            finally:
                stop_event.set()

        def _reader():
            try:
                while not stop_event.is_set():
                    matches = engine._find_lot_matches_by_description(
                        "Telegram Channel Subscribers"
                    )
                    self.assertIsInstance(matches, list)
                    for lot_key, lot_data in matches:
                        self.assertIsInstance(lot_key, str)
                        self.assertIsInstance(lot_data, dict)
            except Exception as exc:
                errors.append(exc)

        try:
            with storage.CONFIG_LOCK:
                rt.lot_mapping.clear()
                rt.lot_mapping["lot_1"] = dict(valid_lot)

            writers = [threading.Thread(target=_mutator)]
            readers = [threading.Thread(target=_reader) for _ in range(3)]
            for th in writers + readers:
                th.start()
            for th in writers + readers:
                th.join(timeout=5)
            self.assertEqual(errors, [])
        finally:
            with storage.CONFIG_LOCK:
                rt.lot_mapping.clear()
                rt.lot_mapping.update(original_mapping)

