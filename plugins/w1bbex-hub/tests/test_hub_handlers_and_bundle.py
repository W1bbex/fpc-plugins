"""Tests for Telegram handlers, update checker loop, HTML safety, and W1bbexHub.py bundle."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import REPO_ROOT, make_fake_cardinal
import build as hub_build
from src import core, handlers, runtime, storage


def test_main_menu_uses_semver_comparison_not_inequality(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True, "latest_version": "0.0.9"}
    monkeypatch.setattr(core.requests, "post", lambda *a, **kw: resp)
    monkeypatch.setattr(handlers, "get_plugins", lambda: [])

    text, kb = handlers.get_main_menu_content(make_fake_cardinal(), 100, "owner")
    assert "W1bbex HUB" not in text
    callbacks = [btn.callback_data for row in kb.keyboard for btn in row]
    assert "hub:update_hub" not in callbacks


def test_process_key_input_escapes_html_and_retries_invalid_keys(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        handlers,
        "get_plugins",
        lambda: [{"id": "autooffline", "name": "Auto<Offline>", "prefix": "AO-"}],
    )
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    orig_cb = SimpleNamespace(
        id="cb1",
        from_user=SimpleNamespace(id=42, username="u"),
        message=SimpleNamespace(chat=SimpleNamespace(id=99), message_id=55),
    )

    bad_msg = SimpleNamespace(chat=SimpleNamespace(id=99), from_user=SimpleNamespace(id=42), text="WRONG-<key>")
    controller._process_key_input(bad_msg, orig_cb)
    assert cardinal.telegram.bot.register_next_step_handler.called

    slash_msg = SimpleNamespace(chat=SimpleNamespace(id=99), from_user=SimpleNamespace(id=42), text="/hub")
    controller._process_key_input(slash_msg, orig_cb)
    assert cardinal.telegram.bot.clear_step_handler_by_chat_id.called

    valid_msg = SimpleNamespace(chat=SimpleNamespace(id=99), from_user=SimpleNamespace(id=42), text="AO-123<&>test")
    controller._process_key_input(valid_msg, orig_cb)
    assert storage.load_data()["keys"]["autooffline"] == "AO-123<&>test"
    _, sent_text = cardinal.telegram.bot.reply_to.call_args.args
    assert "&lt;&amp;&gt;" in sent_text
    assert "Auto&lt;Offline&gt;" in sent_text


def test_unauthorized_users_are_blocked_in_commands_and_callbacks(isolated_hub: Path) -> None:
    cardinal = make_fake_cardinal(authorized_users={100})
    controller = handlers.init_cardinal(cardinal)

    unauth_msg = SimpleNamespace(chat=SimpleNamespace(id=999), from_user=SimpleNamespace(id=999, username="intruder"), text="!hub_key a k")
    controller.hub_command(unauth_msg)
    controller.set_key_command(unauth_msg)
    assert storage.load_data() == {"keys": {}}

    called = []
    wrapped = controller.wrap_cb(lambda c: called.append(c))
    wrapped(SimpleNamespace(id="cb", data="hub:main", from_user=SimpleNamespace(id=999)))
    assert called == []


def test_wrap_cb_does_not_double_answer_when_handler_shows_alert(isolated_hub: Path) -> None:
    cardinal = make_fake_cardinal(authorized_users={1})
    controller = handlers.HubController(cardinal)

    wrapped = controller.wrap_cb(lambda c: controller.install_plugin(c, None))
    wrapped(SimpleNamespace(id="q1", data="hub:install:", from_user=SimpleNamespace(id=1)))
    assert cardinal.telegram.bot.answer_callback_query.call_count == 1


def test_update_checker_loop_sleeps_when_no_plugins_installed(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_event = threading.Event()
    wait_timeouts: list[float] = []

    def fake_wait(timeout: float) -> bool:
        wait_timeouts.append(timeout)
        if len(wait_timeouts) >= 3:
            stop_event.set()
            return True
        return False

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    handlers._update_checker_loop(make_fake_cardinal(), stop_event)
    assert wait_timeouts == [
        runtime.UPDATE_CHECK_INITIAL_DELAY_SECONDS,
        runtime.UPDATE_CHECK_INTERVAL_SECONDS,
        runtime.UPDATE_CHECK_INTERVAL_SECONDS,
    ]


def test_run_update_check_cycle_preserves_concurrent_key_updates(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.save_data(
        {
            "keys": {"autooffline": "AO-1"},
            "installed": {"autooffline": {"version": "v1.0"}},
            "owner_tg_id": 777,
            "notify_updates": True,
        }
    )
    monkeypatch.setattr(
        handlers,
        "get_plugins",
        lambda: [
            {
                "id": "autooffline",
                "name": "AutoOffline",
                "latest_version": "v2.0",
                "urgency": "high",
                "changelog": "Fix <bug>",
                "notify_users": True,
            }
        ],
    )

    cardinal = make_fake_cardinal()

    def send_and_simulate_concurrent_key(chat_id: int, text: str, **kw):
        assert "&lt;bug&gt;" in text
        storage.update_data(lambda d: d.setdefault("keys", {}).__setitem__("autochannel", "AC-NEW"))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)

    cardinal.telegram.bot.send_message.side_effect = send_and_simulate_concurrent_key
    sent = handlers.run_update_check_cycle(cardinal)
    assert sent == 1

    final_data = storage.load_data()
    assert final_data["keys"]["autochannel"] == "AC-NEW"
    assert final_data["notified_updates"]["autooffline_v2.0"] == -1


def test_built_w1bbex_hub_bundle_matches_cardinal_and_server_contracts(tmp_path: Path) -> None:
    built_path = hub_build.build(tmp_path / "W1bbexHub.py")
    raw = built_path.read_bytes()
    text = raw.decode("utf-8")

    # Matches VDS /hub/v1/init line regex
    versions = [
        m.group(1)
        for line in text.splitlines()
        if (m := re.search(r'^VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', line))
    ]
    assert versions == [runtime.VERSION]

    # Passes client self-update AST validation
    valid, err = core.validate_hub_source(raw, "text/plain")
    assert valid is True, err

    # Loads cleanly into a Cardinal plugin slot directory
    cardinal_root = tmp_path / "Cardinal"
    plugin_slot = cardinal_root / "plugins" / "W1bbexHub.py"
    plugin_slot.parent.mkdir(parents=True, exist_ok=True)
    plugin_slot.write_bytes(raw)

    spec = importlib.util.spec_from_file_location("W1bbexHub_test_slot", plugin_slot)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.NAME == runtime.NAME
    assert mod.VERSION == runtime.VERSION
    assert mod.UUID == runtime.UUID
    assert len(mod.BIND_TO_PRE_INIT) == 1
    assert len(mod.BIND_TO_POST_INIT) == 1

    fake_cardinal = make_fake_cardinal(authorized_users={1})
    controller = mod.init_cardinal(fake_cardinal)
    assert len(fake_cardinal.telegram.cbq_routes) == 18
    assert len(fake_cardinal.telegram.msg_routes) == 2
    assert controller is not None
    assert (cardinal_root / "storage" / "w1bbex" / "Hub" / "logs" / "w1bbex_hub.log").exists()

    # Keep committed root W1bbexHub.py identical to build output
    assert (REPO_ROOT / "W1bbexHub.py").read_bytes() == raw


def test_free_plugin_hides_trial_and_installs_without_license_key(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_entry = {
        "id": "autosmm",
        "name": "AutoSMM",
        "latest_version": "1.0.1",
        "description": "<b>AutoSMM</b> free plugin",
        "is_free": True,
        "uuid": "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8",
        "credits": "@w1bbex",
    }
    monkeypatch.setattr(handlers, "get_plugins", lambda: [plugin_entry])

    from src import ui

    state = core.inspect_plugin_state(make_fake_cardinal(), plugin_entry, {}, {})
    _, markup = ui.render_plugin_info(state)
    callbacks = [btn.callback_data for row in markup.keyboard for btn in row]
    assert "hub:install:autosmm" in callbacks
    assert "hub:trial:autosmm" not in callbacks

    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    captured = {}

    def fake_download(c_arg, pid, key, tg_user):
        captured["pid"] = pid
        captured["key"] = key
        storage.update_data(lambda d: d.setdefault("installed", {}).__setitem__(pid, {"name": "AutoSMM", "version": "1.0.1"}))
        return True, "Плагин AutoSMM (версия 1.0.1) успешно скачан и установлен."

    monkeypatch.setattr(handlers, "download_plugin", fake_download)
    cb = SimpleNamespace(
        id="cb-free",
        from_user=SimpleNamespace(id=42, username="owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=99), id=10),
    )
    controller.install_plugin(cb, "autosmm")
    assert captured == {"pid": "autosmm", "key": None}
    edit_texts = [call.args[0] for call in cardinal.telegram.bot.edit_message_text.call_args_list]
    assert any("AutoSMM установлен на диск" in t for t in edit_texts)
    final_markup = cardinal.telegram.bot.edit_message_text.call_args_list[-1].kwargs["reply_markup"]
    final_cbs = [btn.callback_data for row in final_markup.keyboard for btn in row]
    assert "hub:restart:install:autosmm" in final_cbs

    controller.trial_request(cb, "autosmm")
    assert cardinal.telegram.bot.answer_callback_query.called


def test_delete_restart_post_init_and_open_plugin_full_roundtrip(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_uuid = "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8"
    plugin_entry = {
        "id": "autosmm",
        "name": "AutoSMM",
        "latest_version": "1.0.1",
        "is_free": True,
        "uuid": target_uuid,
        "credits": "@w1bbex",
    }
    monkeypatch.setattr(handlers, "get_plugins", lambda: [plugin_entry])

    # Prepare installed plugin on disk + user storage data + license key
    bin_dir = isolated_hub / "plugins" / "w1bbex" / "AutoSMM"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "AutoSMM_1.0.1.py").write_text('NAME = "AutoSMM"\n', encoding="utf-8")
    stub = isolated_hub / "plugins" / "AutoSMM.py"
    stub.write_text(f"{runtime.LOADER_HEADER}\n", encoding="utf-8")
    storage_dir = isolated_hub / "storage" / "w1bbex" / "AutoSMM"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "auto_lots.json").write_text('{"kept": true}', encoding="utf-8")

    storage.save_data({
        "keys": {"autosmm": "KEY-KEEP"},
        "installed": {
            "autosmm": {
                "name": "AutoSMM",
                "version": "1.0.1",
                "stub_path": str(stub),
                "bin_dir": str(bin_dir),
            }
        },
        "owner_tg_id": 777,
    })

    cardinal = make_fake_cardinal(authorized_users={42})
    dispatched: list[str] = []
    cardinal.telegram.bot.message_handlers = [
        {"function": lambda m: dispatched.append(m.text), "filters": {"commands": ["autosmm"]}}
    ]
    cardinal.plugins = {
        target_uuid: SimpleNamespace(
            name="AutoSMM",
            version="1.0.1",
            uuid=target_uuid,
            path="plugins/AutoSMM.py",
            enabled=True,
            commands={"autosmm": "Управление AutoSMM"},
            settings_page=False,
        )
    }
    controller = handlers.HubController(cardinal)
    cb = SimpleNamespace(
        id="cb-ops",
        from_user=SimpleNamespace(id=42, username="owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=777), id=25),
    )

    # Clicking open executes /autosmm handler directly
    controller.open_plugin_handler(cb, "autosmm")
    assert dispatched == ["/autosmm"]

    # Delete removes stub + bin_dir, keeps storage + key, and offers restart callback
    controller.delete_plugin_handler(cb, "autosmm")
    assert not stub.exists()
    assert not bin_dir.exists()
    assert (storage_dir / "auto_lots.json").is_file()
    assert storage.load_data()["keys"]["autosmm"] == "KEY-KEEP"
    del_text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    assert "AutoSMM.py" in del_text
    assert "storage/w1bbex/AutoSMM/" in del_text
    del_markup = cardinal.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"]
    del_cbs = [btn.callback_data for row in del_markup.keyboard for btn in row]
    assert "hub:restart:delete:autosmm" in del_cbs

    # Clicking restart triggers restart hook cleanly
    restarted: list[bool] = []
    monkeypatch.setattr(handlers, "trigger_cardinal_restart", lambda: restarted.append(True))
    controller.restart_cardinal_handler(cb, "delete:autosmm")
    assert restarted == [True]

    # Simulated post-restart: Cardinal no longer has AutoSMM loaded
    cardinal_after_del = make_fake_cardinal(authorized_users={42})
    cardinal_after_del.plugins = {}
    assert handlers.process_post_restart_notice(cardinal_after_del) is True
    post_del_text = cardinal_after_del.telegram.bot.send_message.call_args.args[1]
    assert "полностью удалён" in post_del_text

    # Now simulate post-restart after installing/activating AutoSMM -> returns UI with Open + Cardinal settings buttons
    stub.write_text(f"{runtime.LOADER_HEADER}\n", encoding="utf-8")
    storage.update_data(lambda d: d.setdefault("installed", {}).__setitem__("autosmm", {
        "name": "AutoSMM",
        "version": "1.0.1",
        "stub_path": str(stub),
    }))
    storage.save_pending_restart({
        "action": "install",
        "chat_id": 777,
        "plugin_id": "autosmm",
        "plugin_name": "AutoSMM",
        "target_version": "1.0.1",
        "expected_uuid": target_uuid,
    })
    cardinal_after_inst = make_fake_cardinal(authorized_users={42})
    cardinal_after_inst.plugins = {
        target_uuid: SimpleNamespace(
            name="AutoSMM",
            version="1.0.1",
            uuid=target_uuid,
            path="plugins/AutoSMM.py",
            enabled=True,
            commands={"autosmm": "Управление AutoSMM"},
            settings_page=False,
        )
    }
    assert handlers.process_post_restart_notice(cardinal_after_inst) is True
    post_inst_text = cardinal_after_inst.telegram.bot.send_message.call_args.args[1]
    post_inst_kb = cardinal_after_inst.telegram.bot.send_message.call_args.kwargs["reply_markup"]
    post_cbs = [btn.callback_data for row in post_inst_kb.keyboard for btn in row]
    assert "/autosmm" in post_inst_text
    assert "hub:open:autosmm" in post_cbs
    assert f"45:{target_uuid}:0" in post_cbs
    assert "hub:main" in post_cbs


def test_resources_menu_and_hub_update_restart_flow(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = SimpleNamespace(
        id="cb-res",
        from_user=SimpleNamespace(id=42, username="owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=777), id=30),
    )

    controller.resources_menu(cb)
    res_markup = cardinal.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"]
    urls = [btn.url for row in res_markup.keyboard for btn in row if getattr(btn, "url", None)]
    assert "https://github.com/W1bbex" in urls
    assert runtime.DEFAULT_CHANNEL_URL in urls
    assert runtime.SUPPORT_URL in urls

    # Hub self-update saves pending_restart and renders restart CTA
    hub_file = isolated_hub / "plugins" / "W1bbexHub.py"
    hub_file.write_text('VERSION = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(handlers, "download_and_update_hub", lambda: (True, "OK"))
    controller.hub_update_handler(cb)
    upd_markup = cardinal.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"]
    upd_cbs = [btn.callback_data for row in upd_markup.keyboard for btn in row]
    assert "hub:restart:hub" in upd_cbs
    assert storage.load_data()["pending_restart"]["action"] == "hub"


def test_hub_init_guards_missing_telegram_stop_hook_and_offline_catalog(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert handlers.init_cardinal(None) is None
    assert handlers.init_cardinal(SimpleNamespace(telegram=None)) is None

    runtime.STOP_EVENT.clear()
    handlers.stop_hub()
    assert runtime.STOP_EVENT.is_set()

    built_path = hub_build.build(tmp_path / "W1bbexHub_check.py")
    spec = importlib.util.spec_from_file_location("W1bbexHub_stop_check", built_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert len(mod.BIND_TO_DELETE) == 1

    # When catalog is unavailable and plugin is not free/known locally, report catalog outage instead of key error
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(handlers, "get_plugins", lambda: [])
    cb = SimpleNamespace(
        id="cb-offline",
        from_user=SimpleNamespace(id=42, username="owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=99), id=10),
    )
    controller.install_plugin(cb, "autosmm")
    shown_text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    assert "временно недоступен" in shown_text
