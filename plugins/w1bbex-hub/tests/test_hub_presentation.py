"""Regression tests for independent icon/color support and the main menu contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_fake_cardinal
from src import handlers, runtime, storage, transport, ui
from src.ui.icons import tg_icon
from src.ui.kit import Btn, KeyboardBuilder, has_custom_button_icons, has_styled_buttons, strip_button_styles
from test_hub_navigation import callback


@pytest.mark.parametrize("first_failure", ["emoji", "style"])
def test_telegram_icon_and_color_fallbacks_are_independent(first_failure: str) -> None:
    markup = KeyboardBuilder().row(
        Btn("Установить", "hub:install:demo", style="success", icon="download"),
        Btn("Удалить", "hub:delete_confirm:demo", style="danger", icon="trash"),
    ).row(Btn("Справка", url="https://example.com", style="primary", icon="info")).build()
    attempts = []
    text = f"{tg_icon('hub')} <b>Hub</b>"

    def invoke(body, keyboard):
        payload = json.loads(keyboard.to_json())["inline_keyboard"]
        attempts.append((body, payload))
        if len(attempts) == 1:
            if first_failure == "emoji":
                raise RuntimeError("Bad Request: PREMIUM_ACCOUNT_REQUIRED")
            raise RuntimeError("Bad Request: invalid button style")
        if first_failure == "style" and has_custom_button_icons(keyboard):
            assert not has_styled_buttons(keyboard)
            raise RuntimeError("Bad Request: custom emoji not allowed")
        return "ok"

    assert transport._call_tg_html(invoke, text, markup) == "ok"
    final_body, final_keyboard = attempts[-1]
    assert "tg-emoji" not in final_body
    assert "<b>Hub</b>" in final_body
    assert all("icon_custom_emoji_id" not in b for row in final_keyboard for b in row)
    if first_failure == "emoji":
        assert [b["style"] for row in final_keyboard for b in row] == ["success", "danger", "primary"]
    else:
        assert all("style" not in b for row in final_keyboard for b in row)
    assert final_keyboard[0][0]["callback_data"] == "hub:install:demo"
    assert final_keyboard[1][0]["url"] == "https://example.com"
    assert has_custom_button_icons(markup) and has_styled_buttons(markup)


def test_style_rejection_keeps_icons_and_original_buttons() -> None:
    original = KeyboardBuilder().row(Btn("Назад", "hub:main", style="primary", icon="back")).build()
    fallback = strip_button_styles(original)
    assert not has_styled_buttons(fallback)
    assert has_custom_button_icons(fallback)
    assert has_styled_buttons(original)
    assert fallback.keyboard[0][0].to_dict()["icon_custom_emoji_id"] == original.keyboard[0][0].to_dict()["icon_custom_emoji_id"]


@pytest.mark.parametrize("notifications, expected_style", [(True, "success"), (False, "danger")])
def test_main_menu_shows_environment_and_colored_notification_state(notifications, expected_style) -> None:
    text, markup = ui.render_main_menu([], {"os": "Linux <x86_64>", "python_version": "3.12.3"},
                                       notifications, runtime.VERSION, False, None, 0)
    assert "ОС: Linux &lt;x86_64&gt;" in text and "Python: 3.12.3" in text
    assert '5422483755518439914' in text and '5458605177802108299' in text
    buttons = {b.callback_data: b for row in markup.keyboard for b in row}
    assert buttons["hub:plugins_menu"].style == "success"
    assert buttons["hub:settings"].style == "primary"
    assert buttons["hub:toggle_notify_main"].style == expected_style


def test_notification_toggle_on_main_menu_keeps_the_main_menu(isolated_hub: Path, monkeypatch) -> None:
    storage.save_data({"notify_updates": True, "keys": {}})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.init_cardinal(cardinal)
    visits = []
    monkeypatch.setattr(controller, "main_menu", lambda c: visits.append("main"))
    monkeypatch.setattr(controller, "settings_menu", lambda c: visits.append("settings"))
    cb = callback()
    cb.data = "hub:toggle_notify_main"
    matches = [fn for fn, predicate in cardinal.telegram.cbq_routes if predicate(cb)]
    assert len(matches) == 1
    matches[0](cb)
    assert storage.load_data()["notify_updates"] is False
    assert visits == ["main"]
