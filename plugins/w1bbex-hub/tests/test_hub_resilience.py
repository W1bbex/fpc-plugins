"""Regression tests for Hub UI limits, async worker guards, path hardening, and version normalization."""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import make_fake_cardinal
from src import core, handlers, hub_api, installer, navigation, runtime, storage, ui
from src.ui.kit import Btn, compact_callback_data, resolve_callback_data


def _make_callback(data: str = "hub:main", chat_id: int = 777, user_id: int = 42, msg_id: int = 15) -> SimpleNamespace:
    return SimpleNamespace(
        id="cb-resilience",
        data=data,
        from_user=SimpleNamespace(id=user_id, username="owner"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="private"),
            id=msg_id,
            message_id=msg_id,
            reply_markup=None,
        ),
    )


def test_long_callback_data_is_compacted_under_64_bytes_and_dispatched(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_plugin_id = "very_long_plugin_identifier_" * 3
    long_data = f"hub:info:{long_plugin_id}"
    btn = Btn("Details", long_data).to_button()
    assert len(btn.callback_data.encode("utf-8")) <= 64
    assert btn.callback_data.startswith("hub:cb:")
    assert resolve_callback_data(btn.callback_data) == long_data

    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.init_cardinal(cardinal)
    assert controller is not None
    called: list[str] = []
    monkeypatch.setattr(controller, "plugin_info_menu", lambda cb, pid: called.append(pid) or handlers.CB_ANSWERED)
    controller._prefix_routes = tuple(
        (prefix, controller.plugin_info_menu if prefix == "hub:info:" else fn)
        for prefix, fn in controller._prefix_routes
    )

    cb = _make_callback(data=btn.callback_data)
    controller.dispatch_compact_callback(cb)
    assert called == [long_plugin_id]


def test_render_plugin_info_and_update_notification_stay_within_4096_html_limit() -> None:
    huge_desc = ("<script>&\"alert\" " * 600).strip()
    state = {
        "id": "autosmm",
        "name": "AutoSMM <Pro>",
        "status_code": "active",
        "loaded_version": "1.0",
        "installed_version": "1.0",
        "latest_version": "2.0",
        "installed_entry": {"version": "1.0"},
        "has_update": True,
        "needs_restart": False,
        "restart_action": "general",
        "is_free": False,
        "has_key": True,
        "masked_key": "ABCD…WXYZ",
        "commands": [("autosmm", "Menu")],
        "description": huge_desc,
        "changelog": huge_desc,
        "changelog_pending": False,
        "changelog_error": False,
    }
    info_text, _ = ui.render_plugin_info(state, expanded=True, catalog_view="all")
    assert len(info_text) <= 4096

    notif_text, _ = ui.render_update_notification(
        {
            "id": "autosmm",
            "name": "AutoSMM <Pro>",
            "curr_ver": "1.0",
            "latest": "2.0",
            "urgency": "high",
            "changelog": huge_desc,
            "update_key": "autosmm_2.0",
        }
    )
    assert len(notif_text) <= 4096


def test_install_route_runs_in_background_and_deduplicates_concurrent_clicks(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.init_cardinal(cardinal)
    assert controller is not None

    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_download(_c, pid, _key, _user):
        calls.append(pid)
        started.set()
        release.wait(timeout=3.0)
        return False, "stopped"

    monkeypatch.setattr(hub_api, "get_plugins", lambda: [{"id": "autosmm", "name": "AutoSMM", "is_free": True, "latest_version": "1.0"}])
    monkeypatch.setattr(installer, "download_plugin", slow_download)

    cb = _make_callback(data="hub:install:autosmm")
    install_route = next(fn for fn, pred in cardinal.telegram.cbq_routes if pred(cb))

    t0 = time.monotonic()
    install_route(cb)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5
    assert started.wait(timeout=2.0)

    controller.install_plugin(cb, "autosmm", background=False)
    release.set()
    deadline = time.monotonic() + 2.0
    while controller._try_begin_action("install:autosmm") is False and time.monotonic() < deadline:
        time.sleep(0.02)
    controller._end_action("install:autosmm")

    assert calls == ["autosmm"]


def test_hub_command_ignores_group_chat_for_owner_id_and_set_key_accepts_mixed_case(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.save_data({"owner_tg_id": 100, "keys": {}, "installed": {}})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(navigation, "get_main_menu_content", lambda *a, **kw: ("Hub", None))
    monkeypatch.setattr(hub_api, "get_plugins", lambda: [{"id": "AutoSMM", "name": "AutoSMM", "prefix": "AS-"}])

    group_msg = SimpleNamespace(
        text="/hub",
        from_user=SimpleNamespace(id=42, username="user42"),
        chat=SimpleNamespace(id=-100999, type="supergroup"),
    )
    controller.hub_command(group_msg)
    assert storage.load_data()["owner_tg_id"] == 100

    key_msg = SimpleNamespace(
        text="!hub_key autosmm AS-VALIDKEY123",
        from_user=SimpleNamespace(id=42, username="user42"),
        chat=SimpleNamespace(id=42, type="private"),
    )
    controller.set_key_command(key_msg)
    assert storage.load_data()["keys"]["AutoSMM"] == "AS-VALIDKEY123"


def test_sanitize_segment_sanitizes_unsafe_fallback() -> None:
    assert core.sanitize_segment("///", fallback="../bad") == "bad"
    assert core.sanitize_segment("...", fallback="///") == "item"
    assert core.sanitize_segment("valid_name", "item") == "valid_name"


def test_is_newer_version_normalizes_trailing_zeros_without_packaging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "packaging", None)
    monkeypatch.setitem(sys.modules, "packaging.version", None)
    assert core.is_newer_version("1.0.0", "1.0") is False
    assert core.is_newer_version("1.0.1", "1.0") is True
    assert core.is_newer_version("v2.0", "2.0.0") is False


def test_download_plugin_rolls_back_written_files_on_failure(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.PLUGINS_CACHE["items"] = [
        {
            "id": "demo",
            "name": "Demo",
            "filename": "DemoPlugin.py",
            "version": "1.0.0",
            "latest_version": "1.0.0",
        }
    ]
    fake_resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/plain"},
        content=b"NAME = 'Demo'\nVERSION = '1.0.0'\nUUID = '11111111-1111-1111-1111-111111111111'\nBIND_TO_PRE_INIT = []\n",
    )
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(installer, "generate_loader", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    cardinal = make_fake_cardinal()
    ok, msg = installer.download_plugin(cardinal, "demo", None, SimpleNamespace(id=42, username="u"))
    assert ok is False
    assert "disk full" in msg

    plugin_dir = Path(runtime.PATHS.plugins_dir) / "w1bbex" / "demo"
    assert list(plugin_dir.glob("*")) == []


def test_download_plugin_does_not_swallow_os_remove_error(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_dir = Path(runtime.PATHS.plugins_dir) / "w1bbex" / "demo_1.0.0"
    stale_dir.mkdir(parents=True, exist_ok=True)
    old_file = stale_dir / "DemoPlugin_old.pyd"
    old_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        installer,
        "get_plugins",
        lambda: [{"id": "demo", "name": "Demo", "filename": "DemoPlugin_new.pyd", "version": "1.0.0", "latest_version": "1.0.0"}],
    )
    fake_resp = SimpleNamespace(
        status_code=200,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Filename": "DemoPlugin_new.pyd",
        },
        content=b"\x00binary-build",
    )
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(installer.os, "remove", lambda _p: (_ for _ in ()).throw(PermissionError("locked")))

    cardinal = make_fake_cardinal()
    ok, msg = installer.download_plugin(cardinal, "demo", None, SimpleNamespace(id=42, username="u"))
    assert ok is False
    assert "занят процессом" in msg


def test_validate_hub_source_and_sha256_protect_hub_self_update(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_uuid_src = (
        "VERSION = '9.9.9'\n"
        "UUID = '00000000-0000-0000-0000-000000000000'\n"
        "BIND_TO_PRE_INIT = []\n"
    ).encode("utf-8")
    ok, reason = installer.validate_hub_source(bad_uuid_src)
    assert ok is False
    assert "UUID" in reason

    good_src = (
        f"VERSION = '9.9.9'\nUUID = {installer.HUB_UUID!r}\nBIND_TO_PRE_INIT = []\n"
    ).encode("utf-8")
    fake_resp = SimpleNamespace(
        status_code=200,
        content=good_src,
        headers={"Content-Type": "text/plain", "X-File-SHA256": "0" * 64},
    )
    monkeypatch.setattr(installer.requests, "get", lambda *a, **kw: fake_resp)

    target_hub = Path(runtime.PATHS.hub_file)
    original = target_hub.read_bytes()
    ok_upd, msg_upd = installer.download_and_update_hub()
    assert ok_upd is False
    assert "SHA-256" in msg_upd
    assert target_hub.read_bytes() == original


def test_interval_prompt_has_cancel_button_and_fallback_send_when_edit_fails(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = _make_callback(data="hub:custom_interval")

    controller.custom_interval_prompt(cb)
    prompt_call = cardinal.telegram.bot.send_message.call_args
    kb = prompt_call.kwargs["reply_markup"]
    btn_callbacks = [btn.callback_data for row in kb.keyboard for btn in row]
    assert "hub:check_interval" in btn_callbacks

    cardinal.telegram.bot.edit_message_text.side_effect = RuntimeError("message to edit not found")
    interval_msg = SimpleNamespace(
        text="15",
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=777),
        message_id=901,
    )
    controller._process_interval_input(interval_msg, cb, prompt_msg_id=101)
    assert cardinal.telegram.bot.delete_message.called
    assert cardinal.telegram.bot.send_message.call_count >= 2


def test_valid_interval_guards_bad_stored_values() -> None:
    assert navigation._valid_interval({"update_check_interval_minutes": "bad"}) == 30
    assert navigation._valid_interval({"update_check_interval_minutes": True}) == 30
    assert navigation._valid_interval({"update_check_interval_minutes": 1}) == 30
    assert navigation._valid_interval({"update_check_interval_minutes": 60}) == 60


def test_step_message_cancel_key_refreshes_menu_with_correct_kwargs_and_no_typeerror(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = _make_callback(data="hub:activate_key")

    cardinal.telegram.bot.edit_message_text.side_effect = RuntimeError("message to edit not found")
    cancel_msg = SimpleNamespace(
        text="отмена",
        from_user=SimpleNamespace(id=42, username="owner"),
        chat=SimpleNamespace(id=777),
        id=902,
        message_id=902,
    )
    controller._process_key_input(cancel_msg, cb, prompt_msg_id=102)
    assert cardinal.telegram.bot.send_message.call_count >= 2


def test_trial_confirm_error_in_thread_survives_acked_query_and_sends_html_alert(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = _make_callback(data="hub:trial_confirm:demo")

    monkeypatch.setattr(hub_api, "get_plugins", lambda: [{"id": "demo", "name": "Demo", "is_free": False}])
    monkeypatch.setattr(hub_api, "request_trial_key", lambda *a, **kw: (False, "Лимит пробных периодов исчерпан"))

    done = threading.Event()
    orig_alert = controller._alert

    def tracked_alert(*args, **kwargs):
        res = orig_alert(*args, **kwargs)
        done.set()
        return res

    monkeypatch.setattr(controller, "_alert", tracked_alert)

    wrapped = controller.wrap_cb(lambda c: controller.trial_confirm(c, "demo", background=True))
    wrapped(cb)

    assert done.wait(timeout=3)
    assert cardinal.telegram.bot.send_message.call_count >= 1
    sent_text = cardinal.telegram.bot.send_message.call_args[0][1]
    assert "Лимит пробных периодов исчерпан" in sent_text


def test_menu_background_registers_watcher_during_inflight_worker(isolated_hub: Path) -> None:
    from src.menu_background import MenuBackground

    bg = MenuBackground()
    worker_started = threading.Event()
    worker_release = threading.Event()
    draw_called = threading.Event()

    def slow_refresh() -> tuple[str, bool]:
        worker_started.set()
        assert worker_release.wait(timeout=3)
        return ("1.0", True)

    bg.schedule(1, 1, slow_refresh, lambda: None, ttl=180)
    assert worker_started.wait(timeout=1)

    bg.checked = time.monotonic()  # simulate checked is recent
    bg.schedule(1, 2, slow_refresh, draw_called.set, ttl=180)

    worker_release.set()
    assert draw_called.wait(timeout=3)


def test_download_plugin_handles_uppercase_extension_case_insensitively(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hub_api,
        "get_plugins",
        lambda: [{"id": "upper", "name": "Upper", "filename": "Upper.PYD", "version": "1.0", "latest_version": "1.0"}],
    )
    fake_resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/octet-stream", "X-Filename": "Upper_Build.PYD"},
        content=b"\x00upper-binary",
    )
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: fake_resp)

    cardinal = make_fake_cardinal()
    ok, msg = installer.download_plugin(cardinal, "upper", None, SimpleNamespace(id=42, username="u"))
    assert ok is True
    entry = storage.load_data()["installed"]["upper"]
    saved = list(Path(entry["bin_dir"]).glob("*.pyd"))
    assert len(saved) == 1
    assert saved[0].name.endswith(".pyd")


def test_download_plugin_rolls_back_archived_legacy_files_on_failure(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_file = Path(runtime.PATHS.plugins_dir) / "LegacyMod.py"
    legacy_file.write_text(
        "UUID = '22222222-2222-2222-2222-222222222222'\nCREDITS = '@w1bbex'\nNAME = 'Legacy'\nVERSION = '1.0'\nBIND_TO_PRE_INIT = []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hub_api,
        "get_plugins",
        lambda: [{"id": "legacy", "name": "Legacy", "uuid": "22222222-2222-2222-2222-222222222222", "credits": "@w1bbex", "version": "2.0"}],
    )
    fake_resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/plain"},
        content=b"NAME = 'Legacy'\nVERSION = '2.0'\nUUID = '22222222-2222-2222-2222-222222222222'\nBIND_TO_PRE_INIT = []\n",
    )
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(installer, "generate_loader", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    cardinal = make_fake_cardinal()
    ok, msg = installer.download_plugin(cardinal, "legacy", None, SimpleNamespace(id=42, username="u"))
    assert ok is False
    assert legacy_file.exists()


def test_download_plugin_preserves_catalog_uuid_when_offline(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.save_data({
        "installed": {
            "offline_mod": {
                "name": "OfflineMod",
                "version": "1.0",
                "catalog": {"uuid": "persistent-uuid-42", "credits": "@w1bbex", "is_free": True},
            }
        },
        "keys": {},
    })
    monkeypatch.setattr(hub_api, "get_plugins", lambda: [])
    fake_resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/plain"},
        content=b"NAME = 'OfflineMod'\nVERSION = '1.1'\nUUID = 'persistent-uuid-42'\nBIND_TO_PRE_INIT = []\n",
    )
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: fake_resp)

    cardinal = make_fake_cardinal()
    ok, _ = installer.download_plugin(cardinal, "offline_mod", None, SimpleNamespace(id=42, username="u"))
    assert ok is True
    data = storage.load_data()
    assert data["installed"]["offline_mod"]["catalog"]["uuid"] == "persistent-uuid-42"
    assert data["installed"]["offline_mod"]["catalog"]["is_free"] is True


def test_download_and_update_hub_rejects_older_version_downgrade(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_version_src = (
        f"VERSION = '0.1.0'\nUUID = {installer.HUB_UUID!r}\nBIND_TO_PRE_INIT = []\n"
    ).encode("utf-8")
    fake_resp = SimpleNamespace(
        status_code=200,
        content=old_version_src,
        headers={"Content-Type": "text/plain"},
    )
    monkeypatch.setattr(installer.requests, "get", lambda *a, **kw: fake_resp)

    ok, reason = installer.download_and_update_hub()
    assert ok is False
    assert "старее текущей" in reason


def test_interval_input_slash_command_cancels_and_deletes_prompt(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = _make_callback(data="hub:custom_interval")

    slash_msg = SimpleNamespace(
        text="/help",
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=777),
        message_id=902,
    )
    controller._process_interval_input(slash_msg, cb, prompt_msg_id=102)
    assert cardinal.telegram.bot.delete_message.called
    deleted_id = cardinal.telegram.bot.delete_message.call_args[0][1]
    assert deleted_id == 102


def test_outcomes_error_messages_truncated_under_3500_chars() -> None:
    long_err = "Fatal error: " + "X" * 6000
    html_text, _ = ui.render_install_result("mod", "Mod", False, long_err, is_update=False)
    assert len(html_text) <= 4096

    upd_html, _ = ui.render_hub_update_error(long_err)
    assert len(upd_html) <= 4096


def test_matches_creator_and_plugin_label_from_info_none_safe() -> None:
    assert core.plugin_label_from_info("mod", {"name": None}) == "Mod"
    assert core.plugin_label_from_info("mod", None) == "Mod"
    assert core._matches_creator("W1bbex", "@w1bbex") is True
    assert core._matches_creator("@w1bbex", "W1bbex") is True
    assert core._matches_creator(None, "@w1bbex") is False
