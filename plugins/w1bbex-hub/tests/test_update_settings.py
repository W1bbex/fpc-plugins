"""Update scheduling is independent from notification delivery and manual checks."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import make_fake_cardinal
from src import handlers, hub_api, lifecycle, navigation, runtime, storage
from test_hub_navigation import callback, state


def test_auto_check_off_stops_timer_and_menu_checks(isolated_hub, monkeypatch):
    storage.save_data({"auto_check_updates": False, "notify_updates": True})
    cardinal = make_fake_cardinal(authorized_users={42})
    version = MagicMock()
    catalog = MagicMock()
    monkeypatch.setattr(hub_api, "fetch_hub_latest_version", version)
    monkeypatch.setattr(hub_api, "get_plugins", catalog)
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    controller = handlers.HubController(cardinal)
    controller._schedule_menu(777, 15, callback().from_user, lambda: None)
    version.assert_not_called()
    catalog.assert_not_called()


def test_notification_setting_does_not_disable_automatic_checks(isolated_hub, monkeypatch):
    storage.save_data({"auto_check_updates": True, "notify_updates": False, "owner_tg_id": 42})
    version, catalog = MagicMock(return_value=(runtime.VERSION, False)), MagicMock(return_value=[])
    monkeypatch.setattr(hub_api, "fetch_hub_latest_version", version)
    monkeypatch.setattr(hub_api, "get_plugins", catalog)
    cardinal = make_fake_cardinal(authorized_users={42})
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    version.assert_called_once()
    catalog.assert_called_once()
    cardinal.telegram.bot.send_message.assert_not_called()


@pytest.mark.parametrize("raw,enabled,interval", [("30", True, 30), ("60", True, 60), ("360", True, 360), ("1440", True, 1440), ("0", False, None)])
def test_preset_and_disable_persist_and_wake_timer(isolated_hub, raw, enabled, interval):
    runtime.UPDATE_WAKE_EVENT.clear()
    controller = handlers.HubController(make_fake_cardinal(authorized_users={42}))
    controller.set_interval_callback(callback(), raw)
    data = storage.load_data()
    assert data["auto_check_updates"] is enabled
    if interval is not None:
        assert data["update_check_interval_minutes"] == interval
    assert runtime.UPDATE_WAKE_EVENT.is_set()


def test_custom_interval_validation_and_persistence(isolated_hub):
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.init_cardinal(cardinal)
    cb = callback()
    controller.custom_interval_prompt(cb)
    assert cardinal.telegram.check_state(777, 42, controller.STATE_AWAIT_INTERVAL)
    assert not cardinal.telegram.bot.register_next_step_handler.called

    message = SimpleNamespace(chat=SimpleNamespace(id=777), from_user=cb.from_user, text="4")
    controller._handle_step_message(message)
    assert "update_check_interval_minutes" not in storage.load_data()
    assert cardinal.telegram.check_state(777, 42, controller.STATE_AWAIT_INTERVAL)
    assert not cardinal.telegram.bot.register_next_step_handler.called

    message.text = "75"
    controller._handle_step_message(message)
    assert storage.load_data()["update_check_interval_minutes"] == 75
    assert storage.load_data()["auto_check_updates"] is True
    assert cardinal.telegram.get_state(777, 42) is None


def test_scheduler_uses_saved_interval(isolated_hub, monkeypatch):
    storage.save_data({"update_check_interval_minutes": 75})
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda timeout: False)
    waits = []
    def wait(timeout):
        waits.append(timeout)
        stop.set()
    monkeypatch.setattr(lifecycle.UPDATE_WAKE_EVENT, "wait", wait)
    monkeypatch.setattr(lifecycle, "run_update_check_cycle", lambda cardinal: 0)
    lifecycle._update_checker_loop(make_fake_cardinal(), stop)
    assert waits == [75 * 60]


def test_manual_check_works_with_both_settings_off_and_displays_results(isolated_hub, monkeypatch):
    storage.save_data({"auto_check_updates": False, "notify_updates": False})
    monkeypatch.setattr(navigation, "fetch_release_notes", lambda: ("99.0", "notes"))
    monkeypatch.setattr(navigation, "catalog_status", lambda: {"available": True})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(controller, "_states", lambda **kw: ([state(name="AutoSMM", has_update=True)], {}))
    done = threading.Event()
    original = controller._edit_html
    def edit(text, *args, **kwargs):
        result = original(text, *args, **kwargs)
        if "Доступны обновления плагинов" in text:
            done.set()
        return result
    monkeypatch.setattr(controller, "_edit_html", edit)
    controller.check_updates_menu(callback())
    assert done.wait(2)
    text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    assert "99.0" in text and "AutoSMM" in text
    cardinal.telegram.bot.send_message.assert_not_called()


def test_cold_catalog_loads_when_auto_check_off_and_failed_fetch_does_not_freeze_ttl(
    isolated_hub, monkeypatch
):
    storage.save_data({"auto_check_updates": False})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(controller, "_states", lambda **kw: ([], {"keys": {}}))

    fetch_calls = []
    available = [False]
    loading = [True]

    def fake_get_plugins():
        fetch_calls.append("fetch")
        loading[0] = False
        return [] if not available[0] else [{"id": "autosmm", "name": "AutoSMM"}]

    monkeypatch.setattr(hub_api, "get_plugins", fake_get_plugins)
    monkeypatch.setattr(
        navigation,
        "catalog_status",
        lambda: {"available": available[0], "loading": loading[0], "error": not available[0]},
    )

    first_done = threading.Event()
    second_done = threading.Event()
    orig_schedule = controller._menu_background.schedule

    def wrapped_schedule(chat, message, refresh, redraw, ttl=180, is_fresh=None):
        def wrapped_refresh():
            res = refresh()
            if len(fetch_calls) == 1:
                first_done.set()
            else:
                second_done.set()
            return res
        return orig_schedule(chat, message, wrapped_refresh, redraw, ttl=ttl, is_fresh=is_fresh)

    monkeypatch.setattr(controller._menu_background, "schedule", wrapped_schedule)

    controller.plugins_menu(callback(), view="mine")
    assert first_done.wait(2)
    assert len(fetch_calls) == 1
    with controller._menu_background.lock:
        assert controller._menu_background.checked == 0.0

    available[0] = True
    controller.plugins_menu(callback(), view="all")
    assert second_done.wait(2)
    assert len(fetch_calls) == 2
    with controller._menu_background.lock:
        assert controller._menu_background.checked > 0.0
