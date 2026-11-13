import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.services import analytics, currency
from src.storage import storage


class AnalyticsTests(unittest.TestCase):
    def test_formatted_summary_uses_provided_statistics(self):
        rendered = analytics._analytics_format_analytics("all", get_period=lambda period: {"period_label": "За всё время", "revenue": 100, "profit": 60}, count_awaiting=lambda period: 0)
        self.assertIn("За всё время", rendered)
        self.assertIn("60", rendered)

    def test_period_summary_distinguishes_closed_and_refunded_orders(self):
        orders = [{"order_id": "A", "status": "completed", "funpay_status": "closed", "summa": 100, "spent": 40}, {"order_id": "B", "status": "completed", "funpay_status": "refunded", "summa": 50, "spent": 20}]
        result = analytics._analytics_get_period_analytics("all", load_merged=lambda: orders)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["refunded"], 1)
        self.assertEqual(result["profit"], 60)

    def test_merges_order_with_log_without_duplicates(self):
        orders = [{"order_id": "A42", "created_at": 123, "status": "pending", "funpay_status": "open"}]
        log = [{"order_id": "A42", "service_name": "Подписчики", "summa": 100}, {"order_id": "B11", "summa": 50}]
        result = analytics._analytics_merged_orders(load_orders=lambda: orders, load_log=lambda: log)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["service_name"], "Подписчики")
        self.assertEqual(result[1]["order_id"], "B11")

    def test_period_alias_and_profit(self):
        self.assertEqual(analytics._period_info("week")[0], "7d")
        self.assertEqual(analytics._calc_order_net_profit({"summa": 100, "spent": 40}), 60)

    def test_profit_requires_closed_completed_order(self):
        self.assertTrue(analytics._is_completed_for_profit({}, "completed", "closed"))
        self.assertFalse(analytics._is_completed_for_profit({}, "completed", "open"))

    def test_concurrent_charge_and_net_updates_preserve_all_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(rt, "DB_PATH", str(root / "autosmm.db")):
                initial_orders = [
                    {"order_id": f"ORD-{i}", "summa": 100.0 + i, "status": "in progress", "date": "2026-01-01 00:00:00"}
                    for i in range(12)
                ]
                storage.save_orders_data(initial_orders)

                errors = []

                def worker(idx: int) -> None:
                    try:
                        analytics.update_order_charge_and_net(
                            f"ORD-{idx}",
                            spent=10.0 + idx,
                            currency="RUB",
                            net_profit=90.0,
                        )
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                final_orders = {item["order_id"]: item for item in storage.load_orders_data()}
                final_log = {item["order_id"]: item for item in storage.load_orders_log()}
                for i in range(12):
                    oid = f"ORD-{i}"
                    self.assertEqual(final_orders[oid]["spent"], 10.0 + i)
                    self.assertEqual(final_orders[oid]["chistota"], 90.0)
                    self.assertEqual(final_log[oid]["spent"], 10.0 + i)
                    self.assertEqual(final_log[oid]["chistota"], 90.0)

    def test_analytics_module_has_no_telebot_dependency(self):
        self.assertFalse(hasattr(analytics, "build_analytics_keyboard"))
        self.assertFalse(hasattr(analytics, "InlineKeyboardMarkup"))

    def test_period_analytics_converts_foreign_currency_and_flags_unconverted(self):
        orders = [
            {
                "order_id": "USD-OK",
                "status": "completed",
                "funpay_status": "closed",
                "summa": 300.0,
                "spent": 2.0,
                "currency": "USD",
                "spent_rub": 180.0,
            },
            {
                "order_id": "USD-MISSING",
                "status": "completed",
                "funpay_status": "closed",
                "summa": 200.0,
                "spent": 1.0,
                "currency": "XYZ",
            },
        ]
        with mock.patch.object(currency, "get_rate_to_rub", return_value=None):
            stats = analytics._analytics_get_period_analytics("all", load_merged=lambda: orders)
            self.assertAlmostEqual(stats["revenue"], 500.0)
            self.assertAlmostEqual(stats["profit"], 120.0)
            self.assertEqual(stats["unconverted_fx"], 1)

            rendered = analytics._analytics_format_analytics(
                "all",
                get_period=lambda period: stats,
                count_awaiting=lambda period: 0,
            )
            self.assertIn("Без курса в ₽: 1", rendered)

            clean_stats = dict(stats, unconverted_fx=0)
            rendered_clean = analytics._analytics_format_analytics(
                "all",
                get_period=lambda period: clean_stats,
                count_awaiting=lambda period: 0,
            )
            self.assertNotIn("Без курса в ₽", rendered_clean)


class AnalyticsPeriodAndFxTests(unittest.TestCase):
    def test_update_charge_clears_stale_rub_when_fx_unavailable(self):
        from src.services import analytics

        order = {
            "order_id": "FP-FX-STALE",
            "summa": 500.0,
            "spent_rub": 180.0,
            "fx_rate_to_rub": 90.0,
            "chistota": 320.0,
        }
        analytics._apply_charge_and_net(
            order,
            spent_f=2.0,
            currency="USD",
            net_profit=None,
            spent_rub=None,
            fx_rate_to_rub=None,
        )
        self.assertNotIn("spent_rub", order)
        self.assertNotIn("fx_rate_to_rub", order)
        self.assertEqual(order["chistota"], 0.0)

    def test_period_analytics_reads_config_once_and_guards_nonfinite_numbers(self):
        import time
        from src.services import analytics, currency

        self.assertEqual(analytics._fmt_rub(float("inf")), "0")
        self.assertEqual(analytics._fmt_rub(float("nan")), "0")

        now_ts = int(time.time())
        orders = [
            {
                "order_id": f"FP-FX-{idx}",
                "status": "completed",
                "funpay_status": "closed",
                "summa": 300.0,
                "chistota": 300.0,
                "spent": 1.0,
                "currency": "USD",
                "spent_rub": None,
                "created_at": now_ts - 60,
            }
            for idx in range(5)
        ]
        orders.append(
            {
                "order_id": "FP-WAIT",
                "status": "completed",
                "funpay_status": "open",
                "summa": 100.0,
                "chistota": 100.0,
                "spent": 0.5,
                "currency": "USD",
                "spent_rub": 50.0,
                "created_at": now_ts - 30,
            }
        )

        load_calls = 0

        def _count_load_config():
            nonlocal load_calls
            load_calls += 1
            return {"fx_rates": {"USD": 100.0}}

        with (
            mock.patch.object(analytics, "load_config", side_effect=_count_load_config),
            mock.patch.object(
                currency,
                "get_rate_to_rub",
                side_effect=lambda code, config_loader=None, **_kw: float(
                    (config_loader() if callable(config_loader) else {}).get("fx_rates", {}).get(code, 100.0)
                ),
            ),
        ):
            summary = analytics._analytics_get_period_analytics("7d", load_merged=lambda: orders)
            self.assertEqual(load_calls, 1)
            self.assertEqual(summary["awaiting_confirmation"], 1)
            formatted = analytics._analytics_format_analytics(
                "7d",
                "more",
                get_period=lambda period=None: summary,
                count_awaiting=lambda period=None: (_ for _ in ()).throw(
                    AssertionError("count_awaiting should be reused from summary")
                ),
            )
            self.assertIsInstance(formatted, str)
            self.assertIn("Ожидание", formatted)

