"""Regression tests for Cardinal plugin state controls."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import make_fake_cardinal
from src import handlers, ui
from test_hub_navigation import callback, state


@pytest.mark.parametrize("enabled,style", [(True, "success"), (False, "danger")])
def test_my_plugin_button_uses_cardinal_state_and_rocket(enabled, style):
    plugin = state(name="AutoSMM", status_code="manual", loaded_uuid="abc", loaded_enabled=enabled)
    _, markup = ui.render_plugins_menu([plugin], view="mine")
    button = markup.keyboard[0][0].to_dict()
    assert button["text"] == "AutoSMM"
    assert button["style"] == style
    assert button["icon_custom_emoji_id"] == "5458489123490804864"
    text, card = ui.render_plugin_info(plugin)
    assert ("Отключён в Cardinal" in text) is (not enabled)
    buttons = {b.callback_data: b for row in card.keyboard for b in row}
    assert f"hub:plugin_{'disable' if enabled else 'enable'}:autosmm" in buttons


def test_unloaded_plugin_has_no_false_disabled_color():
    _, markup = ui.render_plugins_menu([state(loaded_uuid=None)], view="mine")
    assert markup.keyboard[0][0].to_dict().get("style") not in {"success", "danger"}


def test_toggle_routes_use_cardinal_and_repaint_without_double_toggle(isolated_hub, monkeypatch):
    cardinal = make_fake_cardinal(authorized_users={42})
    plugin = SimpleNamespace(enabled=True)
    cardinal.plugins = {"abc": plugin}
    cardinal.toggle_plugin = MagicMock(side_effect=lambda uid: setattr(plugin, "enabled", not plugin.enabled))
    controller = handlers.init_cardinal(cardinal)
    monkeypatch.setattr(controller, "_states", lambda **kw: ([state(loaded_uuid="abc", loaded_enabled=plugin.enabled)], {}))
    cb = callback()
    for data, enabled in [("hub:plugin_disable:autosmm", False),
                          ("hub:plugin_disable:autosmm", False),
                          ("hub:plugin_enable:autosmm", True)]:
        cb.data = data
        routes = [fn for fn, predicate in cardinal.telegram.cbq_routes if predicate(cb)]
        assert len(routes) == 1
        routes[0](cb)
        assert plugin.enabled is enabled
        body = cardinal.telegram.bot.edit_message_text.call_args.args[0]
        assert ("Отключён в Cardinal" in body) is (not enabled)
    assert cardinal.toggle_plugin.call_count == 2
