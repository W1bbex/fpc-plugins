import importlib.util
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


class _FakeMarkup:
    def __init__(self, row_width=3):
        self.row_width = row_width
        self.keyboard = []

    def add(self, *buttons):
        for btn in buttons:
            self.keyboard.append([btn])

    def row(self, *buttons):
        self.keyboard.append(list(buttons))


class _FakeButton:
    def __init__(self, text, callback_data=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.url = url


_INSTALLED_STUB_KEYS = []


def _ensure_telebot_stubs() -> None:
    if "telebot" in sys.modules:
        return
    try:
        import telebot  # noqa: F401
        return
    except ImportError:
        pass
    telebot_mod = types.ModuleType("telebot")
    telebot_types_mod = types.ModuleType("telebot.types")
    telebot_types_mod.InlineKeyboardMarkup = _FakeMarkup
    telebot_types_mod.InlineKeyboardButton = _FakeButton
    telebot_types_mod.CallbackQuery = object
    telebot_types_mod.Message = object
    telebot_mod.types = telebot_types_mod
    sys.modules["telebot"] = telebot_mod
    sys.modules["telebot.types"] = telebot_types_mod
    _INSTALLED_STUB_KEYS.extend(["telebot", "telebot.types"])


_ensure_telebot_stubs()
sys.path.insert(0, str(ROOT))

from src import runtime as rt  # noqa: E402
from src.storage import db, db_orders, storage  # noqa: E402
from src.tg import analytics as tg_analytics, common, files, panels, settings, telemetry as tg_telemetry, templates  # noqa: E402


def tearDownModule() -> None:
    for key in _INSTALLED_STUB_KEYS:
        sys.modules.pop(key, None)


def _make_fake_bot():
    calls = {"answers": [], "messages": [], "edits": [], "documents": []}

    class _FakeBot:
        def answer_callback_query(self, call_id, text=None):
            calls["answers"].append((call_id, text))

        def send_message(self, chat_id, text, **kwargs):
            calls["messages"].append((chat_id, text, kwargs))
            return types.SimpleNamespace(message_id=901)

        def edit_message_text(self, text, chat_id, message_id, **kwargs):
            calls["edits"].append((chat_id, message_id, text, kwargs))
            return types.SimpleNamespace(message_id=message_id)

        def send_document(self, chat_id, document, **kwargs):
            data = document.read() if hasattr(document, "read") else document
            calls["documents"].append((chat_id, data, kwargs))

        def clear_step_handler_by_chat_id(self, chat_id):
            pass

    return _FakeBot(), calls


def _make_call(chat_id=1001, message_id=55, user_id=77, data=""):
    return types.SimpleNamespace(
        id="cb-1",
        data=data,
        from_user=types.SimpleNamespace(id=user_id, username="seller"),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id), message_id=message_id),
    )


class TgFilesAndPanelsTests(unittest.TestCase):
    def test_delete_orders_data_wipes_orders_and_preserves_kv_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.upsert_order_data(1, "FP1", 99, "pending", 10.0, "https://t.me/test", 50, 1)
                storage.update_state_fields(telemetry_consent=True)
                deleted = storage.delete_orders_data()

                self.assertEqual(deleted, 1)
                self.assertEqual(storage.load_orders_data(), [])
                self.assertTrue(storage._load_autosmm_state().get("telemetry_consent"))

    def test_auto_smm_delete_creates_backup_before_removing_storage_files(self):
        fake_bot, calls = _make_fake_bot()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            backup_path = tmp_dir / "emergency_backup.json"
            backup_path.write_text('{"version": 2}', encoding="utf-8")

            db_path = tmp_dir / "autosmm.db"
            db.init_db(str(db_path))

            targets = {
                "DB_PATH": db_path,
                "CONFIG_PATH": tmp_dir / "auto_lots.json",
                "CONFIG_BAK_PATH": tmp_dir / "auto_lots.json.bak",
                "ORDERS_PATH": tmp_dir / "auto_smm_orders.json",
                "ORDERS_DATA_PATH": tmp_dir / "orders_data.json",
                "ORDERS_RUNTIME_PATH": tmp_dir / "orders_runtime.json",
                "VALID_LINKS_PATH": tmp_dir / "valid_link.json",
                "AUTOSMM_STATE_PATH": tmp_dir / "auto_smm_state.json",
            }
            for attr, path in targets.items():
                if attr != "DB_PATH":
                    path.write_text("{}", encoding="utf-8")

            orig_running = rt.RUNNING
            orig_waiting = dict(rt.waiting_for_link)
            try:
                rt.RUNNING = True
                rt.waiting_for_link["FP9"] = {"chat_id": 9}
                with mock.patch.object(rt, "bot", fake_bot), \
                     mock.patch.object(files, "_create_full_backup", return_value=str(backup_path)):
                    patchers = [mock.patch.object(rt, attr, str(path)) for attr, path in targets.items()]
                    for patcher in patchers:
                        patcher.start()
                    try:
                        files.auto_smm_delete(types.SimpleNamespace(chat=types.SimpleNamespace(id=321)))
                    finally:
                        for patcher in patchers:
                            patcher.stop()

                self.assertFalse(rt.RUNNING)
                self.assertEqual(rt.waiting_for_link, {})
                for path in targets.values():
                    self.assertFalse(path.exists(), f"{path.name} should have been removed")
                self.assertEqual(len(calls["documents"]), 1)
                self.assertIn("emergency_backup.json", calls["messages"][-1][1])
            finally:
                rt.RUNNING = orig_running
                rt.waiting_for_link.clear()
                rt.waiting_for_link.update(orig_waiting)

    def test_auto_smm_delete_aborts_if_backup_creation_fails(self):
        fake_bot, calls = _make_fake_bot()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "auto_lots.json"
            cfg_path.write_text('{"lot_mapping": {}}', encoding="utf-8")
            with mock.patch.object(rt, "bot", fake_bot), \
                 mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)), \
                 mock.patch.object(files, "_create_full_backup", return_value=None):
                files.auto_smm_delete(types.SimpleNamespace(chat=types.SimpleNamespace(id=321)))

            self.assertTrue(cfg_path.exists())
            self.assertIn("Очистка отменена", calls["messages"][-1][1])

    def test_panels_url_normalization_and_balance_cache_invalidation(self):
        self.assertEqual(panels.normalize_api_url("smmway.ru"), "https://smmway.ru/api/v2")
        self.assertEqual(
            panels.normalize_api_url("[panel](http://www.example.com/custom/api/v2/extra)"),
            "https://example.com/api/v2",
        )
        with self.assertRaises(ValueError):
            panels.normalize_api_url("")
        with self.assertRaises(ValueError):
            panels.normalize_api_url("localhost")

        orig_cache = dict(rt.PANEL_BALANCE_CACHE)
        try:
            rt.PANEL_BALANCE_CACHE["timestamp"] = 123456.0
            rt.PANEL_BALANCE_CACHE["text"] = "500 ₽"
            panels._invalidate_balance_cache()
            self.assertEqual(rt.PANEL_BALANCE_CACHE["timestamp"], 0.0)
            self.assertEqual(rt.PANEL_BALANCE_CACHE["text"], "недоступно")
        finally:
            rt.PANEL_BALANCE_CACHE.clear()
            rt.PANEL_BALANCE_CACHE.update(orig_cache)

    def test_settings_registers_navigation_targets(self):
        self.assertIn("asmm:return_to_settings", common._NAVIGATION_TARGETS)
        self.assertIn("asmm:misc_settings", common._NAVIGATION_TARGETS)
        self.assertIn("asmm:edit_messages", common._NAVIGATION_TARGETS)
        self.assertIn("asmm:manage_websites", common._NAVIGATION_TARGETS)
        self.assertIn("asmm:links_menu", common._NAVIGATION_TARGETS)
        self.assertIn("asmm:seller_notifications_settings", common._NAVIGATION_TARGETS)
        self.assertIn("asmm:files_menu", common._NAVIGATION_TARGETS)

    def test_settings_builds_analytics_keyboard_and_parses_period_callbacks(self):
        from types import SimpleNamespace

        self.assertEqual(tg_analytics._parse_analytics_period(SimpleNamespace(data="asmm:analytics_period_week"), "asmm:analytics_period_"), "week")
        self.assertEqual(tg_analytics._parse_analytics_period(SimpleNamespace(data="asmm:analytics_period_month"), "asmm:analytics_period_"), "month")
        self.assertEqual(tg_analytics._parse_analytics_period(SimpleNamespace(data="asmm:analytics_period_all"), "asmm:analytics_period_"), "all")
        self.assertEqual(tg_analytics._parse_analytics_period(SimpleNamespace(data="unknown"), "asmm:analytics_period_"), "day")
        kb = tg_analytics.build_analytics_keyboard("7d", "more")
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.keyboard for btn in row]
        self.assertIn("asmm:analytics_less_week", callbacks)
        self.assertIn("asmm:return_to_settings", callbacks)

    def test_start_runtime_starts_only_order_checker_thread(self):
        orig_modules = dict(sys.modules)
        if "FunPayAPI" not in sys.modules:
            import re as _re

            funpay = types.ModuleType("FunPayAPI")
            common = types.ModuleType("FunPayAPI.common")
            utils = types.ModuleType("FunPayAPI.common.utils")
            utils.RegularExpressions = type(
                "_RegularExpressions",
                (),
                {"ORDER_ID": _re.compile(r"#[A-Z0-9]{8}")},
            )
            common.utils = utils
            updater = types.ModuleType("FunPayAPI.updater")
            events = types.ModuleType("FunPayAPI.updater.events")
            events.NewMessageEvent = type("NewMessageEvent", (), {})
            events.NewOrderEvent = type("NewOrderEvent", (), {})
            events.OrderStatusChangedEvent = type("OrderStatusChangedEvent", (), {})
            updater.events = events
            enums = types.ModuleType("FunPayAPI.enums")
            enums.OrderStatuses = types.SimpleNamespace(
                PAID="PAID",
                CLOSED="CLOSED",
                REFUNDED="REFUNDED",
                PARTIALLY_REFUNDED="PARTIALLY_REFUNDED",
            )
            enums.MessageTypes = types.SimpleNamespace(
                ORDER_CONFIRMED="ORDER_CONFIRMED",
                ORDER_CONFIRMED_BY_ADMIN="ORDER_CONFIRMED_BY_ADMIN",
                REFUND="REFUND",
                REFUND_BY_ADMIN="REFUND_BY_ADMIN",
                PARTIAL_REFUND="PARTIAL_REFUND",
                ORDER_REOPENED="ORDER_REOPENED",
            )
            funpay.common = common
            funpay.updater = updater
            funpay.enums = enums
            sys.modules.update(
                {
                    "FunPayAPI": funpay,
                    "FunPayAPI.common": common,
                    "FunPayAPI.common.utils": utils,
                    "FunPayAPI.updater": updater,
                    "FunPayAPI.updater.events": events,
                    "FunPayAPI.enums": enums,
                }
            )

        try:
            from src import plugin
            from src.core import funpay_events

            fake_cardinal = types.SimpleNamespace(
                telegram=types.SimpleNamespace(bot=types.SimpleNamespace())
            )
            spawned_targets = []

            def _fake_thread(*args, **kwargs):
                spawned_targets.append(kwargs.get("target"))
                return types.SimpleNamespace(start=lambda: None, is_alive=lambda: True, join=lambda timeout=None: None)

            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "autosmm.db"
                db.init_db(str(db_path))
                orig_db = rt.DB_PATH
                orig_log = rt.LOG_PATH
                orig_running = rt.RUNNING
                orig_thread = rt.ORDER_CHECK_THREAD
                try:
                    rt.DB_PATH = str(db_path)
                    rt.ORDER_CHECK_THREAD = None
                    with (
                        mock.patch.object(plugin.threading, "Thread", side_effect=_fake_thread),
                        mock.patch.object(plugin, "_recover_buyer_flow_runtime"),
                        mock.patch.object(rt, "bot", fake_cardinal.telegram.bot),
                    ):
                        plugin.start_runtime(fake_cardinal)
                    self.assertIs(spawned_targets[0], plugin.start_order_checking)
                    self.assertEqual(
                        [getattr(target, "__name__", None) for target in spawned_targets],
                        ["start_order_checking"] + ["_shard_worker_loop"] * funpay_events._EVENT_WORKER_COUNT,
                    )
                    self.assertFalse(hasattr(plugin, "start_auto_lots_sender"))
                    self.assertFalse(hasattr(rt, "AUTO_LOTS_SEND_THREAD"))

                    # Track shutdown ordering: background thread joins before rt.LOG_PATH FileHandler closes.
                    join_events = []
                    bg_started = __import__("threading").Event()
                    rt.LOG_PATH = str(Path(tmp) / "autosmm_stop_test.log")
                    fh = logging.FileHandler(rt.LOG_PATH, encoding="utf-8")
                    rt.logger.addHandler(fh)

                    def bg_worker():
                        bg_started.set()
                        rt.STOP_EVENT.wait(timeout=3.0)
                        join_events.append(("bg_done", not fh.stream.closed))

                    bg_thread = __import__("threading").Thread(target=bg_worker)
                    bg_thread.start()
                    rt.register_background_thread(bg_thread)
                    self.assertTrue(bg_started.wait(timeout=1.0))

                    plugin.stop_runtime()
                    self.assertTrue(rt.STOP_EVENT.is_set())
                    self.assertFalse(bg_thread.is_alive())
                    self.assertEqual(join_events, [("bg_done", True)])
                    self.assertIsNone(fh.stream)
                finally:
                    try:
                        plugin.stop_runtime()
                    except Exception:
                        pass
                    rt.STOP_EVENT.clear()
                    rt.DB_PATH = orig_db
                    rt.LOG_PATH = orig_log
                    rt.RUNNING = orig_running
                    rt.ORDER_CHECK_THREAD = orig_thread
        finally:
            added = set(sys.modules) - set(orig_modules)
            for name in added:
                sys.modules.pop(name, None)
            sys.modules.update(orig_modules)

    def test_bundle_import_has_no_disk_side_effects_and_prunes_stale_contexts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            plugins_dir = tmp_root / "plugins"
            plugins_dir.mkdir(parents=True, exist_ok=True)
            entry_path = plugins_dir / "AutoSMM.py"

            builder_spec = importlib.util.spec_from_file_location("build_autosmm_import_test", ROOT / "build.py")
            builder = importlib.util.module_from_spec(builder_spec)
            builder_spec.loader.exec_module(builder)
            builder.build(out_file=entry_path)

            spec = importlib.util.spec_from_file_location("_autosmm_bundle_no_side_effect_test", entry_path)
            orig_modules = dict(sys.modules)
            orig_meta = list(sys.meta_path)
            try:
                funpay_mod = types.ModuleType("FunPayAPI")
                funpay_upd = types.ModuleType("FunPayAPI.updater")
                funpay_evt = types.ModuleType("FunPayAPI.updater.events")
                funpay_enums = types.ModuleType("FunPayAPI.enums")
                funpay_evt.NewMessageEvent = type("NewMessageEvent", (), {})
                funpay_evt.NewOrderEvent = type("NewOrderEvent", (), {})
                sys.modules["FunPayAPI"] = funpay_mod
                sys.modules["FunPayAPI.updater"] = funpay_upd
                sys.modules["FunPayAPI.updater.events"] = funpay_evt
                sys.modules["FunPayAPI.enums"] = funpay_enums

                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self.assertFalse((tmp_root / "storage").exists())
                self.assertEqual(mod.CARDINAL_ROOT, tmp_root.resolve())
            finally:
                sys.meta_path[:] = orig_meta
                for name in set(sys.modules) - set(orig_modules):
                    sys.modules.pop(name, None)
                for name, m in orig_modules.items():
                    sys.modules[name] = m

        rt.UI_INPUT_STATES[999001] = {"prompt_text": "stale", "created_ts": time.time() - 7200}
        rt.UI_INPUT_STATES[999002] = {"prompt_text": "fresh", "created_ts": time.time()}
        try:
            removed = rt.prune_stale_contexts(max_ui_age_seconds=3600)
            self.assertGreaterEqual(removed, 1)
            self.assertNotIn(999001, rt.UI_INPUT_STATES)
            self.assertIn(999002, rt.UI_INPUT_STATES)
        finally:
            rt.UI_INPUT_STATES.pop(999001, None)
            rt.UI_INPUT_STATES.pop(999002, None)

    def test_remove_website_prompt_renders_domain_without_nameerror(self):
        fake_bot, calls = _make_fake_bot()
        call = types.SimpleNamespace(
            id="cb_rm_site",
            data="asmm:delete_website_sub.example.com",
            message=types.SimpleNamespace(
                chat=types.SimpleNamespace(id=555),
                message_id=88,
            ),
        )
        with (
            mock.patch.object(rt, "bot", fake_bot),
            mock.patch.object(settings, "_is_delete_confirm_grace_active", return_value=False),
        ):
            settings.remove_website_prompt(call)

        self.assertEqual(len(calls["edits"]), 1)
        chat_id, msg_id, text_html, kwargs = calls["edits"][0]
        self.assertEqual((chat_id, msg_id), (555, 88))
        self.assertIn("sub.example.com", text_html)
        self.assertEqual(kwargs.get("parse_mode"), "HTML")

    def test_rename_panel_service_migrates_lots_orders_runtime_and_rolls_back_on_db_error(self):
        fake_bot, _calls = _make_fake_bot()
        orig_cfg = dict(rt.config)
        orig_lots = dict(rt.lot_mapping)
        orig_waiting = dict(rt.waiting_for_link)
        orig_cache = dict(rt.PANEL_BALANCE_CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                cfg_path = base / "auto_lots.json"
                bak_path = base / "auto_lots.json.bak"
                db_path = base / "autosmm.db"
                initial_cfg = {
                    "services": {
                        "1": {"name": "1 · p1.example", "api_url": "https://p1.example/api/v2", "api_key": "k1"},
                        "3": {"name": "3 · p3.example", "api_url": "https://p3.example/api/v2", "api_key": "k3"},
                    },
                    "lot_mapping": {
                        "lot_1": {"name": "Primary P1", "service_id": 10, "service_number": 1, "quantity": 100},
                        "lot_2": {
                            "name": "Fallback P1",
                            "service_id": 20,
                            "service_number": 3,
                            "fallback_service_id": 11,
                            "fallback_service_number": 1,
                            "quantity": 50,
                        },
                    },
                }
                with (
                    mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                    mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                    mock.patch.object(rt, "DB_PATH", str(db_path)),
                    mock.patch.object(rt, "bot", fake_bot),
                    mock.patch.object(panels, "_open_api_service_card") as mock_open_card,
                ):
                    storage.save_config(initial_cfg)
                    storage.upsert_order_data(101, "FP-ACT", 501, "pending", 40.0, "https://t.me/a", 100, 1)
                    storage.upsert_order_data(102, "FP-OTHER", 502, "pending", 40.0, "https://t.me/b", 100, 3)
                    rt.waiting_for_link["FP-WAIT"] = {"order_id_funpay": "FP-WAIT", "service_number": 1}
                    rt.PANEL_BALANCE_CACHE["timestamp"] = 12345.0

                    msg = types.SimpleNamespace(text="2", chat=types.SimpleNamespace(id=555))
                    panels.process_apinum_change(msg, 1)

                    updated_cfg = storage.load_config()
                    self.assertNotIn("1", updated_cfg["services"])
                    self.assertIn("2", updated_cfg["services"])
                    self.assertEqual(updated_cfg["lot_mapping"]["lot_1"]["service_number"], 2)
                    self.assertEqual(updated_cfg["lot_mapping"]["lot_2"]["fallback_service_number"], 2)
                    self.assertEqual(rt.lot_mapping["lot_1"]["service_number"], 2)
                    self.assertEqual(storage.get_order_data("FP-ACT")["service_number"], 2)
                    self.assertEqual(storage.get_order_data("FP-OTHER")["service_number"], 3)
                    self.assertEqual(rt.waiting_for_link["FP-WAIT"]["service_number"], 2)
                    self.assertEqual(rt.PANEL_BALANCE_CACHE["timestamp"], 0.0)
                    mock_open_card.assert_called_once_with(555, 2)

                    with (
                        mock.patch.object(
                            db_orders,
                            "rename_orders_service_number",
                            side_effect=sqlite3.OperationalError("simulated lock"),
                        ),
                        mock.patch.object(panels, "_repeat_ui_input_flow") as mock_repeat,
                    ):
                        msg_back = types.SimpleNamespace(text="4", chat=types.SimpleNamespace(id=555))
                        panels.process_apinum_change(msg_back, 2)

                        mock_repeat.assert_called_once()
                        rolled_back = storage.load_config()
                        self.assertIn("2", rolled_back["services"])
                        self.assertNotIn("4", rolled_back["services"])
                        self.assertEqual(rolled_back["lot_mapping"]["lot_1"]["service_number"], 2)
                        self.assertEqual(rt.lot_mapping["lot_1"]["service_number"], 2)
        finally:
            rt.config.clear()
            rt.config.update(orig_cfg)
            rt.lot_mapping.clear()
            rt.lot_mapping.update(orig_lots)
            rt.waiting_for_link.clear()
            rt.waiting_for_link.update(orig_waiting)
            rt.PANEL_BALANCE_CACHE.clear()
            rt.PANEL_BALANCE_CACHE.update(orig_cache)

    def test_telemetry_guest_7d_ui_uses_current_version_and_shows_remaining_time(self):
        fake_bot, calls = _make_fake_bot()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db_path = base / "autosmm.db"
            cfg_path = base / "auto_lots.json"
            bak_path = base / "auto_lots.json.bak"
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                mock.patch.object(rt, "bot", fake_bot),
            ):
                refuse_text = tg_telemetry._build_telemetry_refuse_text()
                self.assertIn("7 дней без телеметрии", refuse_text)
                self.assertIn("48ч", refuse_text)
                self.assertIn("12ч", refuse_text)
                self.assertIn(f"AutoSMM v{rt.VERSION}", refuse_text)
                self.assertNotIn("0.0.9", refuse_text)

                tg_telemetry.telemetry_guest_7d_callback(_make_call(chat_id=7123, data="asmm:telemetry_guest_7d"))
                self.assertEqual(len(calls["edits"]), 1)
                edit_text = calls["edits"][0][2]
                self.assertIn("7 дней без телеметрии", edit_text)
                self.assertIn(f"AutoSMM v{rt.VERSION}", edit_text)
                self.assertNotIn("0.0.9", edit_text)

                menu_text, _kb = settings.build_main_menu_text_and_kb({"lot_mapping": {}, "services": {}})
                self.assertIn("Гостевой режим:", menu_text)
                self.assertIn("осталось", menu_text)


class TgUiHardeningTests(unittest.TestCase):
    def test_html_helper_renders_zero_and_skips_none(self):
        from src.tg import common as tg_common

        self.assertEqual(tg_common.h(0), "0")
        self.assertEqual(tg_common.h(None), "")

    def test_message_not_modified_error_is_suppressed_without_duplicate_send(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.tg import analytics as tg_analytics
        from src.tg import common as tg_common

        class _NotModifiedExc(Exception):
            pass

        sent = []
        fake_bot = SimpleNamespace(
            edit_message_text=mock.MagicMock(
                side_effect=_NotModifiedExc("Bad Request: message is not modified")
            ),
            send_message=lambda *a, **kw: sent.append((a, kw)),
        )
        orig_bot = rt.bot
        try:
            rt.bot = fake_bot
            res = tg_common._safe_edit_message_text("hello", 10, 20)
            self.assertIsNone(res)
            self.assertEqual(sent, [])

            call = SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                from_user=SimpleNamespace(id=42),
                data="asmm:stats_7d",
            )
            with (
                mock.patch.object(tg_analytics, "_safe_answer_callback"),
                mock.patch.object(tg_analytics.analytics, "format_analytics", return_value="<b>0</b>"),
            ):
                tg_analytics.analytics_period(call)
            self.assertEqual(sent, [])
        finally:
            rt.bot = orig_bot

    def test_guarded_step_handler_blocks_different_user_in_group_chat(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.tg import common as tg_common

        registered = []
        invoked = []

        fake_bot = SimpleNamespace(
            register_next_step_handler=lambda msg, fn: registered.append(fn)
        )
        orig_bot = rt.bot
        try:
            rt.bot = fake_bot
            prompt_msg = SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=5)
            with mock.patch.object(tg_common, "_is_authorized_tg_user", return_value=True):
                tg_common._register_guarded_step_handler(
                    prompt_msg,
                    lambda msg: invoked.append(msg.from_user.id),
                    expected_user_id=1001,
                )
                self.assertEqual(len(registered), 1)
                wrong_user_msg = SimpleNamespace(
                    chat=SimpleNamespace(id=-100),
                    from_user=SimpleNamespace(id=2002),
                    text="hijack",
                )
                right_user_msg = SimpleNamespace(
                    chat=SimpleNamespace(id=-100),
                    from_user=SimpleNamespace(id=1001),
                    text="ok",
                )
                registered[0](wrong_user_msg)
                self.assertEqual(invoked, [])
                registered[-1](right_user_msg)
                self.assertEqual(invoked, [1001])
        finally:
            rt.bot = orig_bot

    def test_non_text_media_and_invalid_numeric_inputs_do_not_crash(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.tg import lot_editor as tg_lot_editor
        from src.tg import panels as tg_panels
        from src.tg import settings as tg_settings
        from src.tg import templates as tg_templates

        sent = []
        fake_bot = SimpleNamespace(
            send_message=lambda cid, txt, **kw: sent.append(txt),
            clear_step_handler_by_chat_id=lambda cid: None,
            register_next_step_handler=lambda *a, **kw: None,
        )
        orig_bot = rt.bot
        orig_lot_states = dict(rt.LOT_INPUT_STATES)
        orig_ui_states = dict(rt.UI_INPUT_STATES)
        try:
            rt.bot = fake_bot
            rt.LOT_INPUT_STATES.clear()
            rt.UI_INPUT_STATES.clear()
            media_msg = SimpleNamespace(
                chat=SimpleNamespace(id=555),
                from_user=SimpleNamespace(id=42),
                text=None,
                message_id=9,
            )
            with mock.patch.object(tg_lot_editor, "load_config", return_value={"lot_mapping": {}, "services": {}}):
                tg_lot_editor.process_new_lot_id_step(media_msg)
                self.assertIsNone(tg_lot_editor._parse_int_or_repeat(media_msg, "err", min_value=1))
                zero_msg = SimpleNamespace(
                    chat=SimpleNamespace(id=555),
                    from_user=SimpleNamespace(id=42),
                    text="0",
                    message_id=10,
                )
                neg_msg = SimpleNamespace(
                    chat=SimpleNamespace(id=555),
                    from_user=SimpleNamespace(id=42),
                    text="-5",
                    message_id=11,
                )
                self.assertIsNone(tg_lot_editor._parse_int_or_repeat(zero_msg, "err", min_value=1))
                self.assertIsNone(tg_lot_editor._parse_int_or_repeat(neg_msg, "err", min_value=1))

            with mock.patch.object(tg_panels, "load_config", return_value={"services": {"1": {}}}):
                tg_panels.process_apiurl_change(media_msg, 1)
            tg_settings.process_add_website(media_msg)
            tg_templates.process_template_text_change(media_msg, "after_payment")
            self.assertGreaterEqual(len(sent), 1)
        finally:
            rt.bot = orig_bot
            rt.LOT_INPUT_STATES.clear()
            rt.LOT_INPUT_STATES.update(orig_lot_states)
            rt.UI_INPUT_STATES.clear()
            rt.UI_INPUT_STATES.update(orig_ui_states)

    def test_apply_lot_field_update_preserves_target_after_reindex(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.tg import lot_editor as tg_lot_editor

        cfg = {
            "lot_mapping": {
                "lot_3": {"funpay_name": "Target", "id_yslygi": 10},
                "lot_1": {"funpay_name": "First", "id_yslygi": 5},
            },
            "services": {},
        }
        opened = []

        def _reindex(target_cfg):
            lot_mapping = target_cfg["lot_mapping"]
            target = lot_mapping["lot_3"]
            first = lot_mapping["lot_1"]
            lot_mapping.clear()
            lot_mapping["lot_1"] = first
            lot_mapping["lot_2"] = target

        orig_bot = rt.bot
        try:
            rt.bot = SimpleNamespace(send_message=lambda *a, **kw: None)
            msg = SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="99")
            with (
                mock.patch.object(tg_lot_editor, "load_config", return_value=cfg),
                mock.patch.object(tg_lot_editor, "save_config"),
                mock.patch.object(tg_lot_editor, "reindex_lots", side_effect=_reindex),
                mock.patch.object(tg_lot_editor, "_show_lot_card", side_effect=lambda cid, lk, **kw: opened.append(lk)),
            ):
                tg_lot_editor._apply_lot_field_update(msg, "lot_3", "id_yslygi", 99, "ok")
            self.assertEqual(opened, ["lot_2"])
        finally:
            rt.bot = orig_bot

    def test_cycle_link_fmt_preserves_search_back_callback_and_long_website_callbacks_fit_64_bytes(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.tg import lot_editor as tg_lot_editor
        from src.tg import settings as tg_settings

        long_domain = ("very-long-subdomain-part-" * 3) + "example.com"
        cfg = {
            "lot_mapping": {"lot_1": {"funpay_name": "L", "id_yslygi": 1, "link_format": "full"}},
            "services": {},
            "allowed_websites": [long_domain],
        }
        edited_markups = []
        fake_bot = SimpleNamespace(
            edit_message_text=lambda txt, cid, mid, **kw: edited_markups.append(kw.get("reply_markup")),
            send_message=lambda cid, txt, **kw: edited_markups.append(kw.get("reply_markup")),
            clear_step_handler_by_chat_id=lambda cid: None,
        )
        orig_bot = rt.bot
        orig_tokens = dict(rt.CALLBACK_TOKENS)
        orig_card_ctx = dict(rt.LOT_CARD_CONTEXT)
        try:
            rt.bot = fake_bot
            rt.LOT_CARD_CONTEXT[10] = {
                "lot_key": "lot_1",
                "back_callback": "asmm:search_results_back",
                "back_label": "◀️ Назад",
            }
            call_fmt = SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                from_user=SimpleNamespace(id=42),
                data="asmm:cycle_link_fmt_lot_1",
            )
            rendered_back = []
            with (
                mock.patch.object(tg_lot_editor, "_safe_answer_callback"),
                mock.patch.object(tg_lot_editor, "load_config", return_value=cfg),
                mock.patch.object(tg_lot_editor, "save_config"),
                mock.patch.object(
                    tg_lot_editor,
                    "_show_lot_card",
                    side_effect=lambda c, lk, mid=None, **kw: rendered_back.append(kw.get("back_callback")),
                ),
            ):
                tg_lot_editor.cycle_link_fmt_callback(call_fmt)
            self.assertEqual(rendered_back, ["asmm:search_results_back"])

            call_sites = SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                from_user=SimpleNamespace(id=42),
                data="asmm:show_allowed_websites_list",
            )
            with (
                mock.patch.object(tg_settings, "_safe_answer_callback"),
                mock.patch.object(tg_settings, "load_valid_links", return_value=list(cfg["allowed_websites"])),
            ):
                tg_settings.show_allowed_websites_list(call_sites)

            markup = edited_markups[-1]
            delete_cb = None
            for row in getattr(markup, "keyboard", []):
                for btn in row:
                    cb = getattr(btn, "callback_data", "") or ""
                    self.assertLessEqual(len(cb.encode("utf-8")), 64)
                    if cb.startswith("asmm:delete_website_"):
                        delete_cb = cb
            self.assertIsNotNone(delete_cb)

            call_prompt = SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                from_user=SimpleNamespace(id=42),
                data=delete_cb,
            )
            with (
                mock.patch.object(tg_settings, "_safe_answer_callback"),
                mock.patch.object(tg_settings, "load_valid_links", return_value=list(cfg["allowed_websites"])),
            ):
                tg_settings.remove_website_prompt(call_prompt)
            confirm_cb = None
            for row in getattr(edited_markups[-1], "keyboard", []):
                for btn in row:
                    cb = getattr(btn, "callback_data", "") or ""
                    self.assertLessEqual(len(cb.encode("utf-8")), 64)
                    if cb.startswith("asmm:confirm_delete_website_"):
                        confirm_cb = cb
            self.assertIsNotNone(confirm_cb)

            call_confirm = SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                from_user=SimpleNamespace(id=42),
                data=confirm_cb,
            )
            saved_links = []
            with (
                mock.patch.object(tg_settings, "_safe_answer_callback"),
                mock.patch.object(tg_settings, "load_valid_links", return_value=list(cfg["allowed_websites"])),
                mock.patch.object(
                    tg_settings,
                    "save_valid_links",
                    side_effect=lambda links: saved_links.append(list(links)) or True,
                ),
            ):
                tg_settings.confirm_delete_website_callback(call_confirm)
            self.assertEqual(saved_links, [[]])
        finally:
            rt.bot = orig_bot
            rt.CALLBACK_TOKENS.clear()
            rt.CALLBACK_TOKENS.update(orig_tokens)
            rt.LOT_CARD_CONTEXT.clear()
            rt.LOT_CARD_CONTEXT.update(orig_card_ctx)

    def test_panels_ssrf_block_and_placeholder_skip_and_comma_balance(self):
        from unittest import mock
        from src.core import panel_api
        from src.tg import panels as tg_panels

        for bad_url in (
            "http://127.0.0.1/api/v2",
            "http://192.168.1.10/api/v2",
            "http://169.254.169.254/latest/meta-data",
            "https://internal.local/api/v2",
        ):
            with self.assertRaises(ValueError):
                tg_panels.normalize_api_url(bad_url)

        cfg = {
            "services": {
                "1": {"api_url": "https://example.com/api/v2", "api_key": "YOUR_API_KEY"},
                "2": {"api_url": "https://smm-real.example/api/v2", "api_key": "LIVEKEY123"},
            }
        }
        tg_panels._invalidate_balance_cache()
        with (
            mock.patch.object(
                panel_api.requests,
                "post",
                return_value=mock.MagicMock(
                    status_code=200,
                    json=lambda: {"balance": "123,45", "currency": ""},
                ),
            ) as post_mock,
        ):
            _updated, combined_text = tg_panels.core_panel_balances.refresh_panel_balances_sync(
                cfg,
                timeout=6,
                force=False,
                max_age_seconds=tg_panels.PANEL_BALANCE_CACHE_TTL_SECONDS,
                api_json_fn=tg_panels._panel_api_json,
            )
            self.assertEqual(post_mock.call_count, 1)
            self.assertIn("частично недоступно", combined_text)

            payload_real = tg_panels._get_service_balance_payload(2, cfg["services"]["2"])
            self.assertTrue(payload_real["ok"])
            self.assertEqual(payload_real["currency"], "RUB")
            self.assertAlmostEqual(payload_real["balance_value"], 123.45)

            tg_panels._invalidate_balance_cache()
            _updated, single_text = tg_panels.core_panel_balances.refresh_panel_balances_sync(
                {"services": {"2": cfg["services"]["2"]}},
                timeout=6,
                force=False,
                max_age_seconds=tg_panels.PANEL_BALANCE_CACHE_TTL_SECONDS,
                api_json_fn=tg_panels._panel_api_json,
            )
            self.assertEqual(post_mock.call_count, 3)
            self.assertIn("123.45", single_text)

    def test_invalid_website_repeats_prompt_without_clearing_state(self):
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.tg import settings as tg_settings

        repeated = []
        orig_bot = rt.bot
        try:
            rt.bot = SimpleNamespace(
                send_message=lambda *a, **kw: None,
                edit_message_text=lambda *a, **kw: None,
                clear_step_handler_by_chat_id=lambda cid: None,
                register_next_step_handler=lambda *a, **kw: None,
            )
            bad_site_msg = SimpleNamespace(
                chat=SimpleNamespace(id=10),
                from_user=SimpleNamespace(id=42),
                text="not valid!",
            )
            with (
                mock.patch.object(tg_settings, "load_valid_links", return_value=[]),
                mock.patch.object(
                    tg_settings,
                    "_repeat_ui_input_flow",
                    side_effect=lambda *a, **kw: repeated.append(a[1]),
                ),
            ):
                tg_settings.process_add_website(bad_site_msg)
            self.assertEqual(len(repeated), 1)
        finally:
            rt.bot = orig_bot

    def test_tg_handlers_masking_escaping_truncation_locks_and_null_message_guards(self):
        import threading
        from types import SimpleNamespace
        from unittest import mock
        from src import runtime as rt
        from src.storage import storage
        from src.tg import (
            analytics as tg_analytics,
            common as tg_common,
            files as tg_files,
            lot_editor as tg_lot_editor,
            lots as tg_lots,
            panels as tg_panels,
            settings as tg_settings,
            templates as tg_templates,
        )

        def _is_lock_free(lock_obj) -> bool:
            res = []

            def _probe():
                acq = lock_obj.acquire(blocking=False)
                if acq:
                    lock_obj.release()
                res.append(acq)

            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=1.0)
            return bool(res and res[0])

        orig_bot = rt.bot
        orig_cardinal = rt.cardinal_instance
        sent_messages = []
        edited_messages = []
        answered = []
        sent_docs = []
        cleared_steps = []
        registered_steps = []
        try:
            rt.bot = SimpleNamespace(
                send_message=lambda cid, text, **kw: sent_messages.append((cid, text, kw))
                or SimpleNamespace(chat=SimpleNamespace(id=cid), message_id=999),
                edit_message_text=lambda text, cid, mid, **kw: edited_messages.append((cid, mid, text, kw)),
                answer_callback_query=lambda call_id, text=None, **kw: answered.append((call_id, text)),
                send_document=lambda cid, f, **kw: sent_docs.append((cid, f, kw)),
                clear_step_handler_by_chat_id=lambda cid: cleared_steps.append(cid),
                register_next_step_handler=lambda msg, fn, *a, **kw: registered_steps.append((msg, fn)),
            )

            # 1. mask_api_key across short, medium, and normal lengths.
            self.assertEqual(tg_panels.mask_api_key(""), "не задан")
            self.assertEqual(tg_panels.mask_api_key("abcd"), "......")
            self.assertEqual(tg_panels.mask_api_key("123456"), "1......6")
            self.assertEqual(tg_panels.mask_api_key("LIVEKEY12345"), "LIV......345")

            # 2. Search results HTML truncation respects Telegram 4096-char limit without cutting entities.
            long_term = "<tag> & " + ("x" * 5000)
            rendered_search = tg_lots._format_search_results_text(
                long_term, has_results=True, current_page=1, total_pages=1
            )
            self.assertLessEqual(len(rendered_search), 4096)
            self.assertIn("&lt;tag&gt; &amp;", rendered_search)
            self.assertFalse(rendered_search.rstrip(".").endswith("&am"))

            # 3. process_new_lot_id_step & delete_one_lot (HTML escaping + reindex + error recovery).
            with tempfile.TemporaryDirectory() as tmp:
                orig_cfg_path = rt.CONFIG_PATH
                try:
                    rt.CONFIG_PATH = str(Path(tmp) / "config.json")
                    storage.save_config({"services": {"1": {"name": "S1"}}, "lot_mapping": {}})
                    fake_lot = SimpleNamespace(fields={"fields[summary][ru]": "<Promo> & Boost"})
                    rt.cardinal_instance = SimpleNamespace(
                        account=SimpleNamespace(get_lot_fields=lambda lid: fake_lot),
                        telegram=SimpleNamespace(authorized_users={42: {}}, set_state=lambda *a, **k: None, clear_state=lambda *a, **k: None),
                    )
                    bad_id_msg = SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="0")
                    with mock.patch.object(tg_lot_editor, "_repeat_ui_input_flow") as repeat_mock:
                        tg_lot_editor.process_new_lot_id_step(bad_id_msg)
                        repeat_mock.assert_called_once()

                    ok_id_msg = SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="12345")
                    tg_lot_editor.process_new_lot_id_step(ok_id_msg)
                    saved_lots = storage.load_config()["lot_mapping"]
                    self.assertIn("lot_1", saved_lots)
                    self.assertIn("&lt;Promo&gt; &amp; Boost", sent_messages[-1][1])
                    self.assertEqual(sent_messages[-1][2].get("parse_mode"), "HTML")

                    # delete_one_lot removes existing lot cleanly and reports missing lot safely.
                    del_call = SimpleNamespace(id="cb1", message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50), from_user=SimpleNamespace(id=42))
                    tg_lots.delete_one_lot(del_call, "lot_1")
                    self.assertEqual(storage.load_config()["lot_mapping"], {})
                    self.assertIn("удалён", edited_messages[-1][2])
                    tg_lots.delete_one_lot(del_call, "<missing>")
                    self.assertIn("&lt;missing&gt;", edited_messages[-1][2])

                    # 4. cycle_link_fmt_callback & process_add_service / process_delete_service lock release before TG I/O.
                    storage.save_config(
                        {
                            "services": {"1": {"name": "P1", "api_url": "https://p.example/api/v2", "api_key": "SECRET123456"}},
                            "lot_mapping": {"lot_1": {"name": "L1", "service_id": 1, "quantity": 10, "service_number": 1}},
                        }
                    )
                    fmt_lock_free = []
                    with mock.patch.object(
                        tg_lot_editor, "_show_lot_card", side_effect=lambda *a, **kw: fmt_lock_free.append(_is_lock_free(tg_lot_editor.CONFIG_LOCK))
                    ):
                        tg_lot_editor.cycle_link_fmt_callback(
                            SimpleNamespace(
                                id="cbfmt",
                                data="asmm:cycle_link_fmt_lot_1",
                                message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50),
                                from_user=SimpleNamespace(id=42),
                            )
                        )
                    self.assertEqual(fmt_lock_free, [True])
                    self.assertEqual(storage.load_config()["lot_mapping"]["lot_1"]["link_format"], "clean_username")

                    dup_lock_free = []
                    with mock.patch.object(
                        tg_panels, "_repeat_ui_input_flow", side_effect=lambda *a, **kw: dup_lock_free.append(_is_lock_free(tg_panels.CONFIG_LOCK))
                    ):
                        tg_panels.process_add_service(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="1"))
                        tg_panels.process_delete_service(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="99"))
                    self.assertEqual(dup_lock_free, [True, True])

                    tg_panels.process_add_service(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="2"))
                    self.assertIn("2", storage.load_config()["services"])
                    tg_panels.process_delete_service(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="2"))
                    self.assertIn("2", storage.load_config()["services"])
                    tg_panels.confirm_delete_service_callback(
                        SimpleNamespace(
                            id="cbdel2",
                            data="asmm:confirm_delete_service_2",
                            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50),
                            from_user=SimpleNamespace(id=42),
                        )
                    )
                    self.assertNotIn("2", storage.load_config()["services"])

                    # 5. add_website & _delete_allowed_website_and_refresh serialize config write under lock and do TG I/O outside lock.
                    site_io_lock_free = []
                    with (
                        mock.patch.object(tg_settings, "load_valid_links", return_value=["t.me"]),
                        mock.patch.object(tg_settings, "save_valid_links", return_value=True),
                        mock.patch.object(
                            rt.bot,
                            "send_message",
                            side_effect=lambda *a, **kw: site_io_lock_free.append(_is_lock_free(tg_settings.CONFIG_LOCK)),
                        ),
                    ):
                        tg_settings.add_website(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42)), "vk.com")
                    with (
                        mock.patch.object(tg_settings, "load_valid_links", return_value=["t.me", "vk.com"]),
                        mock.patch.object(tg_settings, "save_valid_links", return_value=True),
                        mock.patch.object(
                            tg_settings,
                            "show_allowed_websites_list",
                            side_effect=lambda *a, **kw: site_io_lock_free.append(_is_lock_free(tg_settings.CONFIG_LOCK)),
                        ),
                    ):
                        tg_settings._delete_allowed_website_and_refresh(
                            SimpleNamespace(id="cbs", message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50), from_user=SimpleNamespace(id=42)),
                            "vk.com",
                        )
                    self.assertEqual(site_io_lock_free, [True, True])
                finally:
                    rt.CONFIG_PATH = orig_cfg_path

            # 6. /chatid@bot step command, zero rejection, and valid chat ID persistence in process_notification_chat_id.
            self.assertEqual(getattr(tg_settings.process_notification_chat_id, "allowed_step_commands", None), ("/chatid",))
            tg_common._set_ui_input_state(
                10,
                "notif_chat_id",
                tg_settings.process_notification_chat_id,
                back_target="back_target",
                initiator_user_id=42,
            )
            fake_msg_user = SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="/chatid@my_test_bot")
            with mock.patch.object(tg_settings, "_register_guarded_step_handler", wraps=tg_common._register_guarded_step_handler) as reg_wrap:
                tg_settings.process_notification_chat_id(fake_msg_user)
            self.assertIn("ID этого чата: 10", sent_messages[-1][1])
            reg_wrap.assert_called_once()
            # Underlying step helper exposes allowed_step_commands for /chatid@bot pass-through.
            passed_msg = SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="/chatid@my_test_bot")
            tg_common._register_guarded_step_handler(passed_msg, tg_settings.process_notification_chat_id, expected_user_id=42)
            sent_count_before = len(sent_messages)
            registered_steps[-1][1](passed_msg)
            self.assertEqual(len(sent_messages), sent_count_before + 1)
            self.assertIn("ID этого чата: 10", sent_messages[-1][1])

            with mock.patch.object(tg_settings, "_repeat_ui_input_flow") as rep_chat:
                tg_settings.process_notification_chat_id(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="0"))
                rep_chat.assert_called_once()
            with mock.patch.object(tg_settings, "_update_config_value") as upd_chat:
                tg_settings.process_notification_chat_id(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42), text="-100123456"))
                upd_chat.assert_called_once_with("notification_chat_id", -100123456)

            # 7. full_backup_confirm error escaping + export_errors + call.message is None guards + cancel routing + /autosmm clear_state.
            with tempfile.TemporaryDirectory() as tmp_b:
                dummy_backup = Path(tmp_b) / "backup.json"
                dummy_backup.write_text("{}", encoding="utf-8")
                call_file = SimpleNamespace(id="cbf", message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50), from_user=SimpleNamespace(id=42))
                with (
                    mock.patch.object(tg_files, "_create_full_backup", return_value=str(dummy_backup)),
                    mock.patch.object(tg_files, "files_menu"),
                ):
                    tg_files.full_backup_confirm(call_file)
                self.assertEqual(len(sent_docs), 1)

                with (
                    mock.patch.object(tg_files, "_create_full_backup", return_value=str(dummy_backup)),
                    mock.patch.object(rt.bot, "send_document", side_effect=RuntimeError("<bad & xml>")),
                    mock.patch.object(tg_files, "files_menu"),
                ):
                    tg_files.full_backup_confirm(call_file)
                self.assertIn("&lt;bad &amp; xml&gt;", sent_messages[-1][1])
                self.assertEqual(sent_messages[-1][2].get("parse_mode"), "HTML")

                with (
                    mock.patch.object(tg_files, "_create_full_backup", side_effect=OSError("disk error")),
                    mock.patch.object(tg_files, "files_menu"),
                ):
                    tg_files.full_backup_confirm(call_file)
                self.assertIn("Не удалось создать бэкап", sent_messages[-1][1])

                with (
                    mock.patch.object(tg_files, "_send_log_file_async") as log_send_mock,
                    mock.patch.object(tg_files, "_render_files_menu") as files_render_mock,
                ):
                    tg_files.export_errors(call_file)
                    log_send_mock.assert_called_once()
                    files_render_mock.assert_called_once()

            null_call = SimpleNamespace(id="cb_null", message=None, from_user=None, data="asmm:ui_input_cancel")
            for fn in (
                tg_common.ui_input_cancel_callback,
                tg_common.ui_input_back_callback,
                tg_analytics._render_analytics,
                tg_files._render_files_menu,
                tg_files.export_errors,
            ):
                fn(null_call)

            nav_api_calls = []
            nav_tpl_calls = []
            with mock.patch.object(tg_common, "_NAVIGATION_PREFIXES", [("api_service_", lambda c, t: nav_api_calls.append(t)), ("template_view_", lambda c, t: nav_tpl_calls.append(t))]):
                tg_common._set_ui_input_state(10, "p1", lambda m: None, back_target="api_service_1", initiator_user_id=42)
                tg_common.ui_input_cancel_callback(SimpleNamespace(id="c1", message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50), from_user=SimpleNamespace(id=42)))
                tg_common._set_ui_input_state(10, "p2", lambda m: None, back_target="asmm:settings", cancel_target="template_view_payment", initiator_user_id=42)
                tg_common.ui_input_cancel_callback(SimpleNamespace(id="c2", message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=50), from_user=SimpleNamespace(id=42)))
            self.assertEqual(nav_api_calls, ["api_service_1"])
            self.assertEqual(nav_tpl_calls, ["template_view_payment"])

            cleared_tg_states = []
            rt.cardinal_instance = SimpleNamespace(
                telegram=SimpleNamespace(clear_state=lambda cid, uid, del_msg=False: cleared_tg_states.append((cid, uid)))
            )
            with mock.patch.object(tg_settings, "_open_main_settings_menu"):
                tg_settings.auto_smm_settings(SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=42)))
            self.assertIn((10, 42), cleared_tg_states)
        finally:
            tg_common._clear_autosmm_memory_contexts()
            rt.bot = orig_bot
            rt.cardinal_instance = orig_cardinal


if __name__ == "__main__":
    unittest.main()
