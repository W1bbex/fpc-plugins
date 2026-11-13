import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.services import currency
from src.storage import db_state


def _make_response(status_code: int = 200, json_data=None, content: bytes = b""):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.content = content
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


class CurrencyTests(unittest.TestCase):
    def setUp(self):
        currency.reset_memory_cache()

    def tearDown(self):
        currency.reset_memory_cache()

    def test_cbr_json_parsing_and_profit_computation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "autosmm.db")
            with mock.patch.object(rt, "DB_PATH", db_path), mock.patch.object(
                currency.requests,
                "get",
                return_value=_make_response(
                    200,
                    json_data={
                        "Valute": {
                            "USD": {"Nominal": 1, "Value": 91.5},
                            "EUR": {"Nominal": 1, "Value": 99.2},
                            "KZT": {"Nominal": 100, "Value": 19.0},
                        }
                    },
                ),
            ) as mock_get:
                self.assertEqual(currency.get_rate_to_rub("RUB", config_loader=lambda: {}), 1.0)
                mock_get.assert_not_called()

                rate_usd = currency.get_rate_to_rub("usd", config_loader=lambda: {})
                self.assertAlmostEqual(rate_usd, 91.5)

                rate_kzt = currency.get_rate_to_rub("KZT", config_loader=lambda: {})
                self.assertAlmostEqual(rate_kzt, 0.19)

                calc = currency.compute_order_profit_rub(500.0, 2.0, "USD", config_loader=lambda: {})
                self.assertAlmostEqual(calc["spent_rub"], 183.0)
                self.assertAlmostEqual(calc["fx_rate_to_rub"], 91.5)
                self.assertAlmostEqual(calc["net_profit"], 317.0)
                self.assertEqual(mock_get.call_count, 1)

    def test_cbr_xml_fallback_parses_windows1251_bytes_and_nominal(self):
        xml_payload = (
            '<?xml version="1.0" encoding="windows-1251"?>'
            '<ValCurs name="Foreign Currency Market">'
            '<Valute ID="R01235"><CharCode>USD</CharCode><Nominal>1</Nominal><Value>92,50</Value></Valute>'
            '<Valute ID="R01375"><CharCode>CNY</CharCode><Nominal>10</Nominal><Value>125,00</Value></Valute>'
            "</ValCurs>"
        ).encode("windows-1251")

        def _side_effect(url, timeout=8):
            if "cbr-xml-daily.ru" in url:
                raise requests.Timeout("primary down")
            if "XML_daily.asp" in url:
                return _make_response(200, content=xml_payload)
            raise AssertionError(f"Unexpected URL {url}")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "autosmm.db")
            with mock.patch.object(rt, "DB_PATH", db_path), mock.patch.object(
                currency.requests,
                "get",
                side_effect=_side_effect,
            ):
                self.assertAlmostEqual(currency.get_rate_to_rub("USD", config_loader=lambda: {}), 92.5)
                self.assertAlmostEqual(currency.get_rate_to_rub("CNY", config_loader=lambda: {}), 12.5)

    def test_open_er_api_fallback_inverts_rub_base_rates(self):
        def _side_effect(url, timeout=8):
            if "open.er-api.com" in url:
                return _make_response(200, json_data={"result": "success", "rates": {"RUB": 1.0, "USD": 0.01}})
            raise requests.ConnectionError("cbr unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "autosmm.db")
            with mock.patch.object(rt, "DB_PATH", db_path), mock.patch.object(
                currency.requests,
                "get",
                side_effect=_side_effect,
            ):
                self.assertAlmostEqual(currency.get_rate_to_rub("USD", config_loader=lambda: {}), 100.0)

    def test_sqlite_ttl_cache_and_stale_offline_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "autosmm.db")
            with mock.patch.object(rt, "DB_PATH", db_path):
                with mock.patch.object(
                    currency.requests,
                    "get",
                    return_value=_make_response(200, json_data={"Valute": {"USD": {"Nominal": 1, "Value": 90.0}}}),
                ) as mock_get:
                    self.assertAlmostEqual(currency.get_rate_to_rub("USD", config_loader=lambda: {}), 90.0)
                    self.assertEqual(mock_get.call_count, 1)

                currency.reset_memory_cache()
                with mock.patch.object(currency.requests, "get", side_effect=AssertionError("should use SQLite cache")):
                    self.assertAlmostEqual(currency.get_rate_to_rub("USD", config_loader=lambda: {}), 90.0)

                db_state.update_state_fields(
                    **{
                        currency.FX_STATE_KEY: {
                            "fetched_at": time.time() - currency.DEFAULT_FX_CACHE_TTL_SECONDS - 60,
                            "source": "cbr_json",
                            "rates": {"USD": 88.5, "RUB": 1.0},
                        }
                    }
                )
                currency.reset_memory_cache()
                with mock.patch.object(currency.requests, "get", side_effect=requests.ConnectionError("offline")):
                    self.assertAlmostEqual(currency.get_rate_to_rub("USD", config_loader=lambda: {}), 88.5)

    def test_manual_override_and_unconverted_foreign_profit_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "autosmm.db")
            with mock.patch.object(rt, "DB_PATH", db_path), mock.patch.object(
                currency.requests,
                "get",
                side_effect=requests.ConnectionError("offline"),
            ):
                manual = currency.compute_order_profit_rub(
                    500.0,
                    2.0,
                    "USD",
                    config_loader=lambda: {"fx_rates": {"usd": 95.0}},
                )
                self.assertAlmostEqual(manual["spent_rub"], 190.0)
                self.assertAlmostEqual(manual["fx_rate_to_rub"], 95.0)
                self.assertAlmostEqual(manual["net_profit"], 310.0)

                unknown = currency.compute_order_profit_rub(
                    500.0,
                    2.0,
                    "USD",
                    config_loader=lambda: {},
                )
                self.assertIsNone(unknown["spent_rub"])
                self.assertIsNone(unknown["fx_rate_to_rub"])
                self.assertIsNone(unknown["net_profit"])

    def test_xml_zero_nominal_is_skipped_without_division_error(self):
        xml_payload = (
            '<?xml version="1.0" encoding="windows-1251"?>'
            '<ValCurs name="Foreign Currency Market">'
            '<Valute ID="R01235"><CharCode>USD</CharCode><Nominal>0</Nominal><Value>95,0000</Value></Valute>'
            '<Valute ID="R01239"><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>102,5000</Value></Valute>'
            "</ValCurs>"
        ).encode("windows-1251")
        with mock.patch.object(
            currency.requests,
            "get",
            return_value=_make_response(200, content=xml_payload),
        ):
            rates = currency._fetch_rates_cbr_xml(currency.requests.get)
        self.assertNotIn("USD", rates)
        self.assertAlmostEqual(rates["EUR"], 102.5)


if __name__ == "__main__":
    unittest.main()
