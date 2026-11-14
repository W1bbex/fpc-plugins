"""Release details are read safely before the user confirms an update."""

import threading
from unittest.mock import MagicMock

import requests

from conftest import make_fake_cardinal
from src import handlers, navigation, release_notes, runtime
from src.ui.outcomes import render_hub_release
from test_hub_navigation import callback
import pytest


@pytest.fixture(autouse=True)
def reset_release_cache():
    release_notes.clear_release_notes_cache()
    yield
    release_notes.clear_release_notes_cache()


def test_release_metadata_comes_from_downloaded_artifact(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = iter([b"VERSION = '0.6.0'\nRELEASE_NOTES = 'New <feature>'\nDESCRIPTION = 'x'\n"])
    monkeypatch.setattr(release_notes.requests, "get", lambda *a, **kw: response)
    assert release_notes.fetch_release_notes() == ("0.6.0", "New <feature>")
    text, keyboard = render_hub_release("0.5.7", "0.6.0", "New <feature>")
    assert "0.5.7" in text and "0.6.0" in text
    assert "<blockquote>New &lt;feature&gt;</blockquote>" in text
    assert keyboard.keyboard[0][0].callback_data == "hub:download_hub"


def test_release_metadata_network_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(release_notes.requests, "get", MagicMock(side_effect=requests.Timeout()))
    assert release_notes.fetch_release_notes() == (None, None)
    text, keyboard = render_hub_release("0.5.7", None, None)
    assert "неизвестна" in text and "Описание изменений пока недоступно" in text
    assert keyboard.keyboard[0][0].callback_data == "hub:update_hub"


def test_release_notes_caching_avoids_repeated_network_calls(monkeypatch):
    call_count = 0
    def fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.iter_content.return_value = iter([b"VERSION = '0.7.0'\nRELEASE_NOTES = 'Cached changelog'\n"])
        return resp

    monkeypatch.setattr(release_notes.requests, "get", fake_get)
    ver1, notes1 = release_notes.fetch_release_notes()
    ver2, notes2 = release_notes.fetch_release_notes()
    assert ver1 == ver2 == "0.7.0"
    assert notes1 == notes2 == "Cached changelog"
    assert call_count == 1  # Second call served from in-memory cache


def test_release_screen_loads_in_background_and_does_not_download(isolated_hub, monkeypatch):
    started, finish, edited = threading.Event(), threading.Event(), threading.Event()
    def fetch():
        started.set()
        assert finish.wait(3)
        return "0.6.0", "Новое описание"
    monkeypatch.setattr(navigation, "fetch_release_notes", fetch)
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.init_cardinal(cardinal)
    cb = callback()
    cb.data = "hub:update_hub"
    original = controller._edit_html
    def edit(text, *args, **kwargs):
        result = original(text, *args, **kwargs)
        if "Новое описание" in text:
            edited.set()
        return result
    monkeypatch.setattr(controller, "_edit_html", edit)
    route = next(fn for fn, predicate in cardinal.telegram.cbq_routes if predicate(cb))
    route(cb)
    assert started.wait(1)
    assert "Загружаем описание" in cardinal.telegram.bot.edit_message_text.call_args.args[0]
    finish.set()
    assert edited.wait(2)
    text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    assert runtime.VERSION in text and "0.6.0" in text and "Новое описание" in text


def test_older_background_result_does_not_overwrite_navigation(isolated_hub, monkeypatch):
    started, finish, fetched = threading.Event(), threading.Event(), threading.Event()
    def fetch():
        started.set()
        finish.wait(3)
        fetched.set()
        return "0.6.0", "Устаревший экран"
    monkeypatch.setattr(navigation, "fetch_release_notes", fetch)
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = callback()
    controller.hub_release_menu(cb)
    assert started.wait(1)
    controller._menu_background.invalidate(cb.message.chat.id, cb.message.id)
    finish.set()
    assert fetched.wait(2)
    assert "Устаревший экран" not in cardinal.telegram.bot.edit_message_text.call_args.args[0]
