import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core import panel_api, panel_errors


class PanelApiTests(unittest.TestCase):
    def test_create_order_uses_passed_configuration(self):
        cfg = {"services": {"1": {"api_url": "https://example.invalid", "api_key": "valid-key-123"}}}
        response = SimpleNamespace(status_code=200)
        with patch.object(panel_api, "_panel_api_json", return_value=(response, {"order": 42}, "", False)) as request:
            ok, order_id, _ = panel_api._panel_send_order(None, 1, 10, "https://example.com", 100, config=cfg)
        self.assertTrue(ok)
        self.assertEqual(order_id, 42)
        self.assertEqual(request.call_args.args[1]["action"], "add")

    def test_add_timeout_does_not_retry_with_get(self):
        error = panel_api.requests.exceptions.Timeout("timeout")
        with patch.object(panel_api.requests, "post", side_effect=error), patch.object(panel_api.requests, "get") as get:
            with self.assertRaises(panel_api.requests.exceptions.Timeout):
                panel_api._panel_api_request("https://example.invalid", {"action": "add", "key": "test"})
        get.assert_not_called()

    def test_batch_result_preserves_order_identifiers(self):
        payload = {"123": {"status": "Completed"}, "456": {"status": "Pending"}}
        result = panel_errors._extract_batch_status_entries(payload, [123, 456])
        self.assertEqual(result, payload)
        self.assertEqual(panel_errors._build_status_result_from_entry(result["123"])["status"], "completed")

    def test_add_uses_get_when_post_method_is_rejected(self):
        rejected = SimpleNamespace(status_code=405, text="", json=lambda: {})
        accepted = SimpleNamespace(status_code=200, text="", json=lambda: {"order": 42})
        with patch.object(panel_api.requests, "post", return_value=rejected), patch.object(panel_api.requests, "get", return_value=accepted) as get:
            self.assertIs(panel_api._panel_api_request("https://example.invalid", {"action": "add"}), accepted)
        get.assert_called_once()

    def test_single_status_accepts_panel_order_id_and_uses_modern_user_agent(self):
        cfg = {"services": {"1": {"api_url": "https://example.invalid", "api_key": "valid-key-123"}}}
        response = SimpleNamespace(status_code=200)
        with patch.object(panel_api, "_panel_api_json", return_value=(response, {"status": "Completed"}, "", False)) as request:
            result = panel_api._panel_single_status(1, panel_order_id=9001, config=cfg)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(request.call_args.args[1]["order"], 9001)
        self.assertNotIn("MSIE 5.01", panel_api.PANEL_API_HEADERS.get("User-Agent", ""))


class PanelApiHardeningTests(unittest.TestCase):
    def test_html_error_page_with_invalid_url_text_is_not_classified_as_panel_error(self):
        from unittest import mock

        cfg = {"services": {"1": {"api_url": "https://panel.example/api/v2", "api_key": "TOPSECRET_KEY_1"}}}
        raw_html = "<html><body>502 Bad Gateway: invalid url / service not found</body></html>"
        classified = panel_errors._classify_panel_payload_result(
            action="status",
            payload=None,
            raw_text=raw_html,
            text_for_match=raw_html.lower(),
            http_status=502,
            invalid_json=True,
        )
        self.assertEqual(classified.get("error"), "invalid_json")
        self.assertNotEqual(classified.get("error_class"), "PANEL_ERROR")

        with mock.patch.object(
            panel_api.requests,
            "post",
            return_value=mock.MagicMock(
                status_code=502,
                text=raw_html,
                json=mock.MagicMock(side_effect=ValueError("No JSON")),
            ),
        ):
            res = panel_api._panel_single_status(1, 999, config=cfg)
        self.assertFalse(res.get("ok"))
        self.assertNotEqual(res.get("error"), "incorrect_order_id")

    def test_missing_service_config_returns_service_inactive_instead_of_keyerror(self):
        res = panel_api._panel_single_status(
            99,
            100,
            config={"services": {"1": {"api_url": "https://p.example/api/v2", "api_key": "k"}}},
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "service_inactive")

    def test_api_key_is_redacted_from_error_details_and_logs(self):
        from unittest import mock
        import requests

        secret = "SUPER_SECRET_API_KEY_987"
        cfg = {"services": {"1": {"api_url": "https://panel.example/api/v2", "api_key": secret}}}
        with mock.patch.object(
            panel_api.requests,
            "post",
            side_effect=requests.RequestException(
                f"Failed request to https://panel.example/api/v2?key={secret}&action=status"
            ),
        ):
            with self.assertLogs(level="ERROR") as captured:
                res = panel_api._panel_single_status(1, 123, config=cfg)
        combined = repr(res) + "\n" + "\n".join(captured.output)
        self.assertNotIn(secret, combined)
        sanitized = panel_errors._sanitize_secret_text(
            f"key={secret}&action=cancel",
            secret,
        )
        self.assertNotIn(secret, sanitized)

