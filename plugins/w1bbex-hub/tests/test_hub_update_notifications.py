"""Background self-update notification flows, retries, reminders, and opt-out."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from conftest import make_fake_cardinal
from src import handlers, hub_api, hub_updates, lifecycle, runtime, storage, ui


@pytest.fixture()
def releases(isolated_hub: Path, monkeypatch):
    storage.save_data({"keys": {}, "owner_tg_id": 777, "notify_updates": True})
    check = MagicMock(return_value=("9.9.9", True))
    monkeypatch.setattr(hub_updates, "fetch_hub_latest_version", check)
    monkeypatch.setattr(hub_updates, "get_disk_hub_pending_version", lambda: None)
    monkeypatch.setattr(hub_api, "get_plugins", lambda: [])
    return check


def test_notifies_without_installed_plugins_or_opening_hub_and_deduplicates(releases) -> None:
    cardinal = make_fake_cardinal()
    assert lifecycle.run_update_check_cycle(cardinal) == 1
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    cardinal.telegram.bot.send_message.assert_called_once()
    chat, text = cardinal.telegram.bot.send_message.call_args.args
    assert chat == 777 and "W1bbex Hub" in text and "9.9.9" in text
    markup = cardinal.telegram.bot.send_message.call_args.kwargs["reply_markup"]
    buttons = {b.callback_data: b for row in markup.keyboard for b in row}
    assert buttons["hub:update_hub"].style == "success"
    assert "hub:remind:hub:9.9.9" in buttons
    assert not any(str(cb).startswith("hub:install:") for cb in buttons)
    assert storage.load_data()["notified_updates"]["hub:9.9.9"] == -1


def test_hub_notification_works_when_plugin_catalog_is_unavailable(releases) -> None:
    storage.update_data(lambda d: d.update(installed={"demo": {"version": "1.0"}}))
    cardinal = make_fake_cardinal()
    assert lifecycle.run_update_check_cycle(cardinal) == 1


@pytest.mark.parametrize("settings", [{"notify_updates": False}, {"owner_tg_id": None}])
def test_disabled_notifications_and_unknown_owner_do_not_send(releases, settings, monkeypatch) -> None:
    monkeypatch.setattr(hub_api, "fetch_hub_latest_version", lambda *args: (runtime.VERSION, False))
    storage.update_data(lambda d: d.update(settings))
    cardinal = make_fake_cardinal()
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    releases.assert_not_called()
    cardinal.telegram.bot.send_message.assert_not_called()


def test_missing_telegram_does_not_attempt_delivery(releases) -> None:
    assert lifecycle.run_update_check_cycle(SimpleNamespace(telegram=None)) == 0
    releases.assert_not_called()


@pytest.mark.parametrize("latest", [runtime.VERSION, "0.1.0"])
def test_current_or_older_hub_does_not_notify(releases, latest) -> None:
    releases.return_value = (latest, False)
    cardinal = make_fake_cardinal()
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    cardinal.telegram.bot.send_message.assert_not_called()


@pytest.mark.parametrize("staged", ["9.9.9", "10.0.0"])
def test_downloaded_release_awaiting_restart_does_not_notify_again(releases, monkeypatch, staged) -> None:
    monkeypatch.setattr(hub_updates, "get_disk_hub_pending_version", lambda: staged)
    cardinal = make_fake_cardinal()
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    cardinal.telegram.bot.send_message.assert_not_called()


def test_newer_release_is_announced_even_if_an_older_one_is_staged(releases, monkeypatch) -> None:
    monkeypatch.setattr(hub_updates, "get_disk_hub_pending_version", lambda: "8.0.0")
    assert lifecycle.run_update_check_cycle(make_fake_cardinal()) == 1


def test_failed_delivery_retries_without_recording_a_false_success(releases) -> None:
    cardinal = make_fake_cardinal()
    cardinal.telegram.bot.send_message.side_effect = RuntimeError("Telegram temporarily unavailable")
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    assert "hub:9.9.9" not in storage.load_data().get("notified_updates", {})
    cardinal.telegram.bot.send_message.side_effect = None
    assert lifecycle.run_update_check_cycle(cardinal) == 1
    assert lifecycle.run_update_check_cycle(cardinal) == 0


def test_remind_tomorrow_defers_hub_release_then_notifies_once(releases, monkeypatch) -> None:
    cardinal = make_fake_cardinal()
    controller = handlers.HubController(cardinal)
    cb = SimpleNamespace(id="reminder", message=SimpleNamespace(chat=SimpleNamespace(id=777), id=42))
    monkeypatch.setattr(lifecycle.time, "time", lambda: 1000)
    controller.remind_handler(cb, "hub:9.9.9")
    assert storage.load_data()["notified_updates"]["hub:9.9.9"] == 87400
    assert lifecycle.run_update_check_cycle(cardinal) == 0
    monkeypatch.setattr(lifecycle.time, "time", lambda: 87401)
    assert lifecycle.run_update_check_cycle(cardinal) == 1
    assert lifecycle.run_update_check_cycle(cardinal) == 0


def test_deduplication_is_per_hub_release_and_preserves_concurrent_changes(releases) -> None:
    storage.update_data(lambda d: d.update(notified_updates={"hub:8.0.0": -1, "demo_2.0": -1}))
    cardinal = make_fake_cardinal()
    def delivered(*args, **kw):
        storage.update_data(lambda d: d.setdefault("keys", {}).update(other="kept-key"))
    cardinal.telegram.bot.send_message.side_effect = delivered
    assert lifecycle.run_update_check_cycle(cardinal) == 1
    data = storage.load_data()
    assert data["keys"]["other"] == "kept-key"
    assert data["notified_updates"] == {"hub:8.0.0": -1, "demo_2.0": -1, "hub:9.9.9": -1}


def test_hub_api_outage_does_not_block_plugin_notifications(releases, monkeypatch) -> None:
    storage.update_data(lambda d: d.update(installed={"demo": {"version": "1.0"}}))
    monkeypatch.setattr(hub_updates, "fetch_hub_latest_version", hub_api.fetch_hub_latest_version)
    monkeypatch.setattr(hub_api.requests, "post", MagicMock(side_effect=requests.Timeout("offline")))
    monkeypatch.setattr(hub_api, "get_plugins", lambda: [{"id": "demo", "name": "Demo", "latest_version": "2.0"}])
    cardinal = make_fake_cardinal()
    assert lifecycle.run_update_check_cycle(cardinal) == 1
    assert "Demo" in cardinal.telegram.bot.send_message.call_args.args[1]
    assert storage.load_data()["notified_updates"]["demo_2.0"] == -1


def test_notification_toggle_preserves_hub_update_action_and_color() -> None:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Обновить Hub", callback_data="hub:update_hub"))
    markup.row(InlineKeyboardButton("Уведомления", callback_data="hub:toggle_notify_from_msg"))
    rebuilt = ui.rebuild_notification_toggle_markup(markup, notify_enabled=False)
    assert rebuilt.keyboard[0][0].callback_data == "hub:update_hub"
    assert rebuilt.keyboard[0][0].style == "success"
    assert rebuilt.keyboard[1][0].style == "danger"
