from datetime import datetime, timedelta
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.services import telemetry
from src.storage import storage


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None):
        self.status_code = status_code
        self._json_payload = json_payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if isinstance(self._json_payload, Exception):
            raise self._json_payload
        return self._json_payload


class TelemetryIdentityTests(unittest.TestCase):
    def test_identity_uses_supplied_fallback_without_plugin_global(self):
        fallback = SimpleNamespace(account=SimpleNamespace(username="Example", id=123))
        username, user_id, source = telemetry._telemetry_get_funpay_identity(None, fallback=fallback)
        self.assertEqual((username, user_id), ("example", "123"))
        self.assertEqual(source, "account")

    def test_identifier_is_stable_and_has_expected_format(self):
        first = telemetry.build_anonymous_install_id("Example", "123")
        self.assertEqual(first, telemetry.build_anonymous_install_id("example", "123"))
        self.assertTrue(telemetry._looks_like_anonymous_install_id(first))

    def test_identifier_requires_both_fields(self):
        with self.assertRaises(ValueError):
            telemetry.build_anonymous_install_id("", "123")

    def test_concurrent_telemetry_state_updates_preserve_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage._save_autosmm_state({"telemetry_consent": True})
                errors = []

                def writer(idx: int) -> None:
                    try:
                        telemetry._set_telemetry_state_fields(**{f"k_{idx}": idx})
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(15)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                state = storage._load_autosmm_state()
                self.assertTrue(state.get("telemetry_consent"))
                for i in range(15):
                    self.assertEqual(state.get(f"k_{i}"), i)

    def test_ping_targets_w1bbex_api_and_checks_http_and_json_ok(self):
        self.assertEqual(telemetry.TELEMETRY_URL, "https://api.w1bbex.ru/v1/autosmm/telemetry")
        cardinal = SimpleNamespace(account=SimpleNamespace(username="SellerOne", id=987654))
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                with mock.patch("src.services.telemetry.requests.post", return_value=_FakeResponse(404, {})) as post_mock:
                    telemetry._send_telemetry_ping_background(cardinal)
                    self.assertEqual(post_mock.call_args.args[0], "https://api.w1bbex.ru/v1/autosmm/telemetry")
                    state = storage._load_autosmm_state()
                    self.assertIsNone(state.get("telemetry_last_ping_ok_at"))
                    self.assertIn("404", str(state.get("telemetry_last_ping_error")))

                with mock.patch(
                    "src.services.telemetry.requests.post",
                    return_value=_FakeResponse(200, {"ok": False, "error": "bad_payload"}),
                ):
                    telemetry._send_telemetry_ping_background(cardinal)
                    state = storage._load_autosmm_state()
                    self.assertIsNone(state.get("telemetry_last_ping_ok_at"))
                    self.assertIn("bad_payload", str(state.get("telemetry_last_ping_error")))

                with mock.patch(
                    "src.services.telemetry.requests.post",
                    return_value=_FakeResponse(200, {"ok": True}),
                ):
                    telemetry._send_telemetry_ping_background(cardinal)
                    state = storage._load_autosmm_state()
                    self.assertTrue(state.get("telemetry_last_ping_ok_at"))
                    self.assertIsNone(state.get("telemetry_last_ping_error"))

    def test_guest_mode_lasts_seven_days_and_notifies_at_48h_and_12h(self):
        base_dt = datetime(2025, 1, 1, 12, 0, 0)
        sent_messages = []

        class _FakeBot:
            def send_message(self, chat_id, text, **kwargs):
                sent_messages.append((chat_id, text, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            cfg_path = Path(tmp) / "auto_lots.json"
            bak_path = Path(tmp) / "auto_lots.json.bak"
            cfg_path.write_text('{"lot_mapping": {}}', encoding="utf-8")
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                mock.patch.object(rt, "bot", _FakeBot()),
            ):
                activated = telemetry._activate_telemetry_guest_7d(chat_id=5050, now_dt=base_dt)
                self.assertTrue(activated)
                self.assertFalse(telemetry._activate_telemetry_guest_7d(chat_id=5050, now_dt=base_dt))

                state = storage._load_autosmm_state()
                self.assertEqual(state.get("telemetry_guest_chat_id"), 5050)
                self.assertEqual(
                    state.get("telemetry_guest_until"),
                    (base_dt + timedelta(days=7)).isoformat(timespec="seconds"),
                )

                # >48h remaining: silent
                self.assertIsNone(
                    telemetry._maybe_notify_telemetry_guest_expiring(now_dt=base_dt + timedelta(days=4, hours=23))
                )
                self.assertEqual(len(sent_messages), 0)

                # <=48h remaining: fires 48h warning once
                t_47h = base_dt + timedelta(days=5, hours=1)
                self.assertEqual(telemetry._maybe_notify_telemetry_guest_expiring(now_dt=t_47h), "48h")
                self.assertIsNone(telemetry._maybe_notify_telemetry_guest_expiring(now_dt=t_47h + timedelta(hours=2)))
                self.assertEqual(len(sent_messages), 1)
                self.assertEqual(sent_messages[0][0], 5050)
                self.assertIn("1д 23ч", sent_messages[0][1])

                # <=12h remaining: fires urgent 12h warning once
                t_11h = base_dt + timedelta(days=6, hours=13)
                self.assertEqual(telemetry._maybe_notify_telemetry_guest_expiring(now_dt=t_11h), "12h")
                self.assertIsNone(telemetry._maybe_notify_telemetry_guest_expiring(now_dt=t_11h + timedelta(hours=1)))
                self.assertEqual(len(sent_messages), 2)
                self.assertIn("менее чем через 12 часов", sent_messages[1][1])
                self.assertIn("11ч", sent_messages[1][1])

    def test_guest_expiry_retries_after_send_error_and_skips_48h_when_already_under_12h(self):
        base_dt = datetime(2025, 1, 1, 12, 0, 0)
        attempts = []

        class _FlakyBot:
            def __init__(self):
                self.fail_next = True

            def send_message(self, chat_id, text, **kwargs):
                attempts.append((chat_id, text))
                if self.fail_next:
                    raise RuntimeError("telegram timeout")

        flaky_bot = _FlakyBot()
        cardinal = SimpleNamespace(telegram=SimpleNamespace(authorized_users={777888: {}}, bot=flaky_bot))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            cfg_path = Path(tmp) / "auto_lots.json"
            bak_path = Path(tmp) / "auto_lots.json.bak"
            cfg_path.write_text('{"lot_mapping": {}}', encoding="utf-8")
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                mock.patch.object(rt, "bot", None),
            ):
                telemetry._activate_telemetry_guest_7d(now_dt=base_dt)
                t_10h = base_dt + timedelta(days=6, hours=14)

                # First attempt fails: warning must not be marked sent
                self.assertIsNone(telemetry._maybe_notify_telemetry_guest_expiring(cardinal, now_dt=t_10h))
                state = storage._load_autosmm_state()
                self.assertIsNone(state.get("telemetry_guest_warn_12h_sent_at"))

                # Retry succeeds: sends 12h warning and suppresses delayed 48h warning
                flaky_bot.fail_next = False
                self.assertEqual(telemetry._maybe_notify_telemetry_guest_expiring(cardinal, now_dt=t_10h), "12h")
                self.assertIsNone(telemetry._maybe_notify_telemetry_guest_expiring(cardinal, now_dt=t_10h + timedelta(minutes=30)))
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[-1][0], 777888)
                state = storage._load_autosmm_state()
                self.assertTrue(state.get("telemetry_guest_warn_48h_sent_at"))
                self.assertTrue(state.get("telemetry_guest_warn_12h_sent_at"))
