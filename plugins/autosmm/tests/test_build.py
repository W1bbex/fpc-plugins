import ast
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parent.parent


def _install_fake_funpay(sys_modules: dict) -> None:
    """Register minimal FunPayAPI stubs when the host environment lacks FunPayAPI."""
    funpay = types.ModuleType("FunPayAPI")
    common = types.ModuleType("FunPayAPI.common")
    utils = types.ModuleType("FunPayAPI.common.utils")
    updater = types.ModuleType("FunPayAPI.updater")
    events = types.ModuleType("FunPayAPI.updater.events")
    events.NewMessageEvent = type("NewMessageEvent", (), {})
    events.NewOrderEvent = type("NewOrderEvent", (), {})
    events.OrderStatusChangedEvent = type("OrderStatusChangedEvent", (), {})
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
    import re as _re

    class _FakeRegularExpressions:
        ORDER_ID = _re.compile(r"#[A-Z0-9]{8}")

    utils.RegularExpressions = _FakeRegularExpressions
    common.utils = utils
    funpay.common = common
    funpay.updater = updater
    funpay.enums = enums
    updater.events = events
    sys_modules["FunPayAPI"] = funpay
    sys_modules["FunPayAPI.common"] = common
    sys_modules["FunPayAPI.common.utils"] = utils
    sys_modules["FunPayAPI.updater"] = updater
    sys_modules["FunPayAPI.updater.events"] = events
    sys_modules["FunPayAPI.enums"] = enums


def _load_bundle_in_isolated_dir(bundle_path: Path, plugins_dir: Path, module_name: str):
    """Copy AutoSMM.py into plugins_dir and execute it without src/ beside it."""
    target = plugins_dir / "AutoSMM.py"
    target.write_text(bundle_path.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _IsolatedBundleEnv:
    """Context manager isolating sys.modules, sys.meta_path, and logger handlers."""

    def __enter__(self):
        self._orig_modules = dict(sys.modules)
        self._orig_meta_path = list(sys.meta_path)
        self._logger = logging.getLogger("FPC.autosmm")
        self._orig_handlers = list(self._logger.handlers)
        for name in (
            "analytics",
            "buyer_messages",
            "core",
            "defaults",
            "engine",
            "links",
            "panel_api",
            "plugin",
            "services",
            "storage",
            "telemetry",
            "tg",
        ):
            sys.modules.pop(name, None)
        _install_fake_funpay(sys.modules)
        return self

    def __exit__(self, exc_type, exc, tb):
        for handler in list(self._logger.handlers):
            if handler not in self._orig_handlers:
                self._logger.removeHandler(handler)
                handler.close()
        sys.meta_path[:] = self._orig_meta_path
        added = set(sys.modules) - set(self._orig_modules)
        for name in added:
            sys.modules.pop(name, None)
        for name, mod in self._orig_modules.items():
            sys.modules[name] = mod


class _FakeTelegram:
    def __init__(self) -> None:
        self.bot = types.SimpleNamespace(
            get_me=lambda: types.SimpleNamespace(id=1, username="test_bot")
        )
        self.cbqs = []
        self.msgs = []
        self.file_handlers = {}
        self.states = {}
        self.authorized_users = {42: {}}

    def cbq_handler(self, handler, *args, **kwargs) -> None:
        self.cbqs.append((handler, args, kwargs))

    def msg_handler(self, handler, *args, **kwargs) -> None:
        self.msgs.append((handler, args, kwargs))

    def file_handler(self, state, handler) -> None:
        self.file_handlers[state] = handler

    def set_state(self, chat_id, message_id, user_id, state, data=None) -> None:
        self.states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data}

    def clear_state(self, chat_id, user_id, del_msg=False):
        entry = self.states.pop((chat_id, user_id), None)
        return entry["mid"] if entry else None


class _FakeCardinal:
    def __init__(self) -> None:
        self.telegram = _FakeTelegram()
        self.commands = []
        self.account = types.SimpleNamespace(id=123, username="seller")
        self.MAIN_CFG = {"FunPay": {"locale": "ru"}}

    def add_telegram_commands(self, plugin_uuid: str, commands) -> None:
        self.commands.append((plugin_uuid, list(commands)))


class BuildTests(unittest.TestCase):
    def _run_build(self) -> Path:
        spec = importlib.util.spec_from_file_location("build_autosmm", ROOT / "build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        return builder.build()

    def test_build_outputs_compilable_standalone_bundle(self):
        built_path = self._run_build()
        self.assertEqual(built_path, ROOT / "AutoSMM.py")
        self.assertTrue(built_path.is_file())
        source = built_path.read_text(encoding="utf-8")
        compile(source, str(built_path), "exec")

        expected_keys = {
            ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
            for path in (ROOT / "src").rglob("*.py")
            if "__pycache__" not in path.parts
        }
        with tempfile.TemporaryDirectory() as tmp:
            plugins_dir = Path(tmp) / "plugins"
            plugins_dir.mkdir()
            with _IsolatedBundleEnv():
                mod = _load_bundle_in_isolated_dir(
                    built_path, plugins_dir, "autosmm_bundle_smoke"
                )
                self.assertEqual(set(mod._BUNDLE_SOURCES), expected_keys)

    def test_bundle_loads_with_isolated_namespaces_and_cardinal_hooks(self):
        built_path = self._run_build()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            plugins_dir = tmp_root / "plugins"
            plugins_dir.mkdir()
            self.assertFalse((tmp_root / "src").exists())

            with _IsolatedBundleEnv():
                mod = _load_bundle_in_isolated_dir(
                    built_path, plugins_dir, "autosmm_bundle_isolated"
                )

                self.assertEqual(mod.NAME, "AutoSMM")
                self.assertEqual(mod.VERSION, "1.0")
                self.assertEqual(str(uuid.UUID(mod.UUID)), mod.UUID)
                self.assertIs(mod.SETTINGS_PAGE, False)
                self.assertIs(mod.BIND_TO_DELETE, mod.stop_runtime)
                self.assertGreaterEqual(len(mod.BIND_TO_PRE_INIT), 1)
                self.assertGreaterEqual(len(mod.BIND_TO_POST_INIT), 1)
                self.assertGreaterEqual(len(mod.BIND_TO_NEW_MESSAGE), 1)
                self.assertGreaterEqual(len(mod.BIND_TO_NEW_ORDER), 1)
                self.assertGreaterEqual(len(mod.BIND_TO_ORDER_STATUS_CHANGED), 1)
                self.assertEqual(mod.CARDINAL_ROOT, tmp_root.resolve())

                for top_name in ("core", "services", "storage", "tg", "analytics", "engine", "links"):
                    self.assertNotIn(top_name, sys.modules)

                for bundled_name in (
                    "storage",
                    "storage.defaults",
                    "storage.storage",
                    "core",
                    "core.panel_api",
                    "core.links",
                    "core.engine",
                    "core.buyer_messages",
                    "services",
                    "services.analytics",
                    "services.telemetry",
                ):
                    self.assertIn(f"_autosmm_src.{bundled_name}", sys.modules)

                pkg = sys.modules["_autosmm_src"]
                plugin_mod = sys.modules["_autosmm_src.plugin"]
                storage_mod = sys.modules["_autosmm_src.storage.storage"]
                refunds_mod = sys.modules["_autosmm_src.core.refunds"]
                self.assertEqual(pkg.__path__, [])
                self.assertIsNot(plugin_mod, storage_mod)
                self.assertIs(plugin_mod.load_config, storage_mod.load_config)
                self.assertIs(refunds_mod.ORDERS_LOCK, storage_mod.ORDERS_LOCK)
                self.assertIs(plugin_mod.CONFIG_LOCK, storage_mod.CONFIG_LOCK)

                mod_reloaded = _load_bundle_in_isolated_dir(
                    built_path, plugins_dir, "autosmm_bundle_reloaded"
                )
                finders = [
                    f for f in sys.meta_path
                    if getattr(f, "_autosmm_bundle_id", None) == "_autosmm_src"
                ]
                self.assertEqual(len(finders), 1)

                plugin_reloaded = sys.modules["_autosmm_src.plugin"]
                fake_cardinal = _FakeCardinal()
                spawned = []

                def _fake_thread(*args, **kwargs):
                    spawned.append((args, kwargs))
                    return types.SimpleNamespace(start=lambda: None)

                with mock.patch.object(plugin_reloaded.threading, "Thread", side_effect=_fake_thread):
                    mod_reloaded.init_commands(fake_cardinal)
                    self.assertEqual(spawned, [])
                    mod_reloaded.start_runtime(fake_cardinal)

                self.assertEqual(len(fake_cardinal.commands), 1)
                self.assertEqual(fake_cardinal.commands[0][0], mod_reloaded.UUID)
                registered_cmds = {
                    cmd for _, _, kw in fake_cardinal.telegram.msgs for cmd in kw.get("commands", [])
                }
                self.assertTrue({"autosmm", "chatid", "autosmm_debug"}.issubset(registered_cmds))
                self.assertGreaterEqual(len(fake_cardinal.telegram.cbqs), 10)
                self.assertNotIn("autosmm:upload_lots_json", fake_cardinal.telegram.file_handlers)
                self.assertIn("autosmm:restore_backup", fake_cardinal.telegram.file_handlers)
                self.assertGreaterEqual(len(spawned), 1)
                rt_reloaded = sys.modules["_autosmm_src.runtime"]
                self.assertTrue(rt_reloaded.RUNNING)
                mod_reloaded.BIND_TO_DELETE(fake_cardinal)
                self.assertFalse(rt_reloaded.RUNNING)

    def test_bundle_resolves_cardinal_root_under_plugins_w1bbex_dir(self):
        built_path = self._run_build()
        prev_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            plugin_slot_dir = tmp_root / "plugins" / "w1bbex" / "AutoSMM"
            plugin_slot_dir.mkdir(parents=True)
            other_cwd = tmp_root / "elsewhere"
            other_cwd.mkdir()
            try:
                os.chdir(other_cwd)
                with _IsolatedBundleEnv():
                    mod = _load_bundle_in_isolated_dir(
                        built_path, plugin_slot_dir, "autosmm_bundle_hub_dir"
                    )
                    rt_mod = sys.modules["_autosmm_src.runtime"]
                    self.assertEqual(mod.CARDINAL_ROOT, tmp_root.resolve())
                    self.assertEqual(
                        rt_mod.BASE_STORAGE_DIR,
                        (tmp_root / "storage" / "w1bbex" / "AutoSMM").resolve(),
                    )
            finally:
                os.chdir(prev_cwd)

    def test_bundle_exposes_top_level_metadata_in_ast(self):
        built_path = self._run_build()
        tree = ast.parse(built_path.read_text(encoding="utf-8"), filename=str(built_path))
        top_level = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                key = node.targets[0].id
                if key in {"NAME", "VERSION", "DESCRIPTION", "CREDITS", "UUID", "SETTINGS_PAGE"}:
                    top_level[key] = ast.literal_eval(node.value)

        self.assertEqual(top_level.get("NAME"), "AutoSMM")
        self.assertEqual(top_level.get("VERSION"), "1.0")
        self.assertEqual(top_level.get("CREDITS"), "@w1bbex")
        self.assertEqual(top_level.get("UUID"), "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8")
        self.assertIs(top_level.get("SETTINGS_PAGE"), False)

    def test_links_menu_includes_github_button(self):
        from src import runtime as rt
        from src.tg import settings as tg_settings

        fake_bot = mock.MagicMock()
        fake_call = types.SimpleNamespace(
            id="cb-links",
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=777), message_id=888),
        )
        with mock.patch.object(rt, "bot", fake_bot):
            tg_settings.links_menu(fake_call)

        self.assertTrue(fake_bot.edit_message_text.called)
        kb = fake_bot.edit_message_text.call_args.kwargs["reply_markup"]
        urls = [btn.url for row in kb.keyboard for btn in row if getattr(btn, "url", None)]
        self.assertIn("https://github.com/W1bbex", urls)


if __name__ == "__main__":
    unittest.main()
