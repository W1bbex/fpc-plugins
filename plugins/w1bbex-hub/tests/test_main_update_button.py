"""The Hub update action must not rearrange the dashboard."""

import pytest

from src import ui
from test_hub_navigation import state


def render(states, update=False, pending=None):
    return ui.render_main_menu(states, {"os": "Linux", "python_version": "3.12"},
                               True, "0.5.6", update, pending, 0)


@pytest.mark.parametrize("states", [[], [state()], [state(has_update=True)]])
def test_available_update_adds_full_width_red_bottom_button_without_moving_other_controls(states):
    _, baseline = render(states)
    text, updated = render(states, update=True)
    before = [[button.to_dict() for button in row] for row in baseline.keyboard]
    after = [[button.to_dict() for button in row] for row in updated.keyboard]
    assert after[:-1] == before
    assert len(after[-1]) == 1
    assert after[-1][0]["callback_data"] == "hub:update_hub"
    assert after[-1][0]["style"] == "danger"
    assert "0.5.6" in after[-1][0]["text"] and "Доступен Hub" in text


def test_staged_update_offers_restart_instead_of_download():
    _, markup = render([state()], update=True, pending="0.5.6")
    callbacks = [button.callback_data for row in markup.keyboard for button in row]
    assert "hub:restart:hub" in callbacks
    assert "hub:update_hub" not in callbacks
