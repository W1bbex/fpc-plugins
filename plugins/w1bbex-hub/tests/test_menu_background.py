"""Regression checks for nonblocking, coalesced and safely invalidated menu refreshes."""

import threading

from src.menu_background import MenuBackground
from src.runtime import CACHE_TTL_SECONDS
from src.ui.screens import render_main_menu, render_plugins_menu


def test_background_refresh_coalesces_and_does_not_redraw_after_navigation(isolated_hub) -> None:
    cache = MenuBackground()
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    calls, drawn = [], []
    def refresh():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return ("9.9.9", True)
    cache.schedule(1, 10, refresh, lambda: drawn.append("old"))
    assert started.wait(1)
    cache.schedule(1, 11, refresh, lambda: (drawn.append("visible"), done.set()))
    cache.invalidate(1, 10)
    release.set()
    assert done.wait(3)
    assert calls == [1] and drawn == ["visible"]
    assert cache.version == ("9.9.9", True)
    cache.schedule(1, 12, refresh, lambda: drawn.append("cached"))
    assert calls == [1]


def test_cache_expires_after_three_minutes(isolated_hub) -> None:
    assert CACHE_TTL_SECONDS == 180
    cache, done = MenuBackground(), threading.Event()
    cache.schedule(1, 2, lambda: ("1.0", False), done.set)
    assert done.wait(3)
    cache.checked -= 181
    done.clear()
    cache.schedule(1, 2, lambda: ("2.0", True), done.set)
    assert done.wait(3)
    assert cache.version == ("2.0", True)


def test_os_emoji_changes_with_os_and_python_is_present() -> None:
    for os_name, icon in [("Linux", "5422483755518439914"), ("Windows", "5420383490740888463")]:
        text, _ = render_main_menu([], {"os": os_name, "python_version": "3.12"}, True, "0.5.2", False, None, 0)
        assert icon in text and "5458605177802108299" in text


def test_catalog_and_installed_are_distinct_with_descriptive_buttons() -> None:
    def item(pid, status):
        return dict(id=pid, name=pid, status_code=status, needs_restart=False, has_update=False,
                    is_free=True, has_key=False)
    states = [item("Installed", "active"), item("Available", "not_installed")]
    _, catalog = render_plugins_menu(states)
    _, mine = render_plugins_menu(states, view="mine")
    catalog_buttons = {b.callback_data: b for row in catalog.keyboard for b in row}
    mine_buttons = {b.callback_data: b for row in mine.keyboard for b in row}
    assert "hub:info:Available" in catalog_buttons and "hub:info:Installed" not in catalog_buttons
    assert "hub:info:Installed" in mine_buttons and "hub:info:Available" not in mine_buttons
    assert catalog_buttons["hub:main"].text == "Назад" and catalog_buttons["hub:main"].style == "danger"
    assert not any(str(cb).startswith("hub:refresh:") for cb in catalog_buttons)


def test_controller_opens_main_without_waiting_for_network(isolated_hub, monkeypatch) -> None:
    from types import SimpleNamespace
    from conftest import make_fake_cardinal
    from src import handlers, hub_api
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow_version(*args):
        started.set()
        assert release.wait(3)
        finished.set()
        return ("9.9.9", True)
    monkeypatch.setattr(hub_api, "fetch_hub_latest_version", slow_version)
    monkeypatch.setattr(hub_api, "get_plugins", lambda: [])
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = SimpleNamespace(id="query", from_user=SimpleNamespace(id=42, username="user"),
                         message=SimpleNamespace(id=1, chat=SimpleNamespace(id=777)))
    controller.main_menu(cb)
    assert cardinal.telegram.bot.edit_message_text.call_count == 1
    assert started.wait(1) and not finished.is_set()
    controller._menu_background.invalidate(777, 1)
    release.set()
    assert finished.wait(3)
    with controller._menu_background.lock:
        assert controller._menu_background.version == ("9.9.9", True)
    assert cardinal.telegram.bot.edit_message_text.call_count == 1


def test_schedule_releases_lock_before_draw_and_skips_checked_on_unfresh(isolated_hub) -> None:
    from src.menu_background import MenuBackground

    bg = MenuBackground()
    done = threading.Event()
    other_thread_acquired: list[bool] = []
    calls = [0]

    def refresh():
        calls[0] += 1
        return ("1.0", False)

    def draw() -> None:
        acquired = [False]

        def probe() -> None:
            if bg.lock.acquire(blocking=False):
                acquired[0] = True
                bg.lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=2)
        other_thread_acquired.append(acquired[0])
        done.set()

    bg.schedule(1, 10, refresh, draw, ttl=180, is_fresh=lambda: False)
    assert done.wait(2)
    assert other_thread_acquired == [True]
    with bg.lock:
        assert bg.checked == 0.0

    done2 = threading.Event()
    bg.schedule(1, 10, refresh, lambda: done2.set(), ttl=180, is_fresh=lambda: True)
    assert done2.wait(2)
    assert calls[0] == 2
    with bg.lock:
        assert bg.checked > 0.0
