"""Unit and flow tests for W1bbex Hub Telegram UI kit, screens, styles, and fallbacks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from conftest import make_fake_cardinal
from src import core, handlers, storage, ui
from src.ui.formatters import format_catalog_line
from src.ui.kit import Btn, CardBuilder, KeyboardBuilder, StyledButton


def test_styled_button_serializes_valid_bot_api_styles_into_markup_json() -> None:
    btn_primary = StyledButton("Catalog", callback_data="hub:plugins_menu", style="primary")
    btn_success = StyledButton("Install", callback_data="hub:install:autosmm", style="success")
    btn_danger = StyledButton("Delete", callback_data="hub:delete_confirm:autosmm", style="danger")
    btn_invalid = StyledButton("Plain", callback_data="hub:main", style="neon")  # type: ignore[arg-type]

    markup = (
        KeyboardBuilder()
        .row(Btn("Primary", callback_data="a", style="primary"), Btn("Plain", callback_data="b"))
        .row(Btn("Delete", callback_data="c", style="danger"))
        .build()
    )
    parsed = json.loads(markup.to_json())["inline_keyboard"]
    assert parsed[0][0]["style"] == "primary"
    assert "style" not in parsed[0][1]
    assert parsed[1][0]["style"] == "danger"

    assert btn_primary.to_dict()["style"] == "primary"
    assert btn_success.to_dict()["style"] == "success"
    assert btn_danger.to_dict()["style"] == "danger"
    assert "style" not in btn_invalid.to_dict()


def test_call_tg_html_strips_button_styles_and_retries_on_telegram_style_rejection() -> None:
    styled_kb = (
        KeyboardBuilder()
        .row(Btn("Update", callback_data="hub:install:autosmm", style="success"))
        .build()
    )
    assert ui.has_styled_buttons(styled_kb) is True

    attempts: list[InlineKeyboardMarkup] = []

    def flaky_invoke(text: str, rm: InlineKeyboardMarkup) -> str:
        attempts.append(rm)
        if ui.has_styled_buttons(rm):
            raise RuntimeError("A request to the Telegram API was unsuccessful. Bad Request: invalid button style")
        return f"ok:{text}"

    result = handlers._call_tg_html(flaky_invoke, "<b>Hello</b>", reply_markup=styled_kb)
    assert result == "ok:<b>Hello</b>"
    assert len(attempts) == 2
    assert ui.has_styled_buttons(attempts[0]) is True
    assert ui.has_styled_buttons(attempts[1]) is False


def test_card_builder_and_catalog_formatting_escape_html_and_use_tree_connectors() -> None:
    text = (
        CardBuilder("🧩", "Test <Card>", "v1.0")
        .quote("")
        .tree("<b>Section</b>", ["One", "Two", "Three"])
        .build()
    )
    assert "Test &lt;Card&gt;" in text
    assert "<blockquote" not in text
    assert " ├ One\n ├ Two\n └ Three" in text
    assert "━" not in text

    line = format_catalog_line(
        {
            "id": "demo",
            "name": "Demo<Plugin>&",
            "status_code": "active",
            "loaded_version": "1.2.3",
            "latest_version": "1.2.3",
            "commands": [("demo", "Open"), ("chatid", "Chat ID")],
            "has_update": False,
            "needs_restart": False,
        }
    )
    assert "Demo&lt;Plugin&gt;&amp;" in line
    assert "/demo" in line
    assert "/chatid" not in line


def test_plugins_menu_groups_statuses_and_styles_action_buttons() -> None:
    states = [
        {
            "id": "autosmm",
            "name": "AutoSMM",
            "status_code": "active",
            "loaded_version": "1.0.1",
            "installed_version": "1.0.1",
            "latest_version": "1.0.1",
            "commands": [("autosmm", "Manage"), ("chatid", "Chat ID")],
            "has_update": False,
            "needs_restart": False,
            "is_free": True,
            "has_key": False,
        },
        {
            "id": "autooffline",
            "name": "AutoOffline",
            "status_code": "manual",
            "loaded_version": "0.9.7.6",
            "installed_version": None,
            "latest_version": "0.9.8",
            "commands": [("autooffline", "Offline"), ("chatid", "Chat ID")],
            "has_update": False,
            "needs_restart": False,
            "is_free": False,
            "has_key": False,
        },
        {
            "id": "autochannel",
            "name": "AutoChannel",
            "status_code": "not_installed",
            "loaded_version": None,
            "installed_version": None,
            "latest_version": "2.0.0",
            "commands": [],
            "has_update": False,
            "needs_restart": False,
            "is_free": False,
            "has_key": True,
        },
    ]

    text, kb = ui.render_plugins_menu(states)
    assert "Плагинов: <b>1</b>" in text
    assert "━" not in text

    btn_by_cb = {
        btn.callback_data: btn
        for row in kb.keyboard
        for btn in row
        if getattr(btn, "callback_data", None)
    }
    assert "AutoChannel" in btn_by_cb["hub:info:autochannel"].text
    assert btn_by_cb["hub:main"].text == "Назад" and btn_by_cb["hub:main"].style == "danger"
    assert not any(str(cb).startswith("hub:refresh:") for cb in btn_by_cb)
    _, mine = ui.render_plugins_menu(states, view="mine")
    assert {b.callback_data for row in mine.keyboard for b in row if str(b.callback_data).startswith("hub:info:")} == {"hub:info:autosmm", "hub:info:autooffline"}


def test_delete_requires_two_step_confirmation_before_disk_removal(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_entry = {
        "id": "autosmm",
        "name": "AutoSMM",
        "latest_version": "1.0.1",
        "is_free": True,
        "uuid": "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8",
        "description": "Free <SMM> plugin",
    }
    monkeypatch.setattr(handlers, "get_plugins", lambda: [plugin_entry])
    from src import runtime
    runtime.PLUGINS_CACHE["items"] = [plugin_entry]

    stub = isolated_hub / "plugins" / "AutoSMM.py"
    stub.write_text("# stub\n", encoding="utf-8")
    storage.save_data(
        {
            "keys": {"autosmm": "KEY-1234567890-KEEP"},
            "installed": {
                "autosmm": {
                    "name": "AutoSMM",
                    "version": "1.0.1",
                    "stub_path": str(stub),
                    "bin_dir": str(isolated_hub / "plugins" / "w1bbex" / "AutoSMM"),
                }
            },
        }
    )

    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = SimpleNamespace(
        id="cb-info",
        from_user=SimpleNamespace(id=42, username="owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=777), id=15),
    )

    controller.plugin_info_menu(cb, "autosmm")
    info_text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    info_kb = cardinal.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"]
    info_btn_map = {
        btn.callback_data: btn
        for row in info_kb.keyboard
        for btn in row
        if getattr(btn, "callback_data", None)
    }
    assert "Free &lt;SMM&gt; plugin" in info_text
    assert "hub:delete_confirm:autosmm" in info_btn_map
    assert "hub:delete:autosmm" not in info_btn_map
    assert getattr(info_btn_map["hub:delete_confirm:autosmm"], "style", None) == "danger"

    controller.delete_confirm_handler(cb, "autosmm")
    assert stub.exists()
    confirm_text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    confirm_kb = cardinal.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"]
    confirm_btn_map = {
        btn.callback_data: btn
        for row in confirm_kb.keyboard
        for btn in row
        if getattr(btn, "callback_data", None)
    }
    assert "Удалить плагин AutoSMM?" in confirm_text
    assert "hub:delete:autosmm" in confirm_btn_map
    assert "hub:info:autosmm" in confirm_btn_map
    assert getattr(confirm_btn_map["hub:delete:autosmm"], "style", None) == "danger"


def test_toggle_notify_msg_rebuilds_styled_markup_cleanly(isolated_hub: Path) -> None:
    storage.save_data({"notify_updates": True})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)

    # Simulate deserialized markup from Telegram webhook/polling update (plain buttons)
    plain_markup = InlineKeyboardMarkup()
    plain_markup.row(InlineKeyboardButton("Update", callback_data="hub:install:autosmm"))
    plain_markup.row(
        InlineKeyboardButton("Remind", callback_data="hub:remind:autosmm_2.0"),
        InlineKeyboardButton("Disable", callback_data="hub:toggle_notify_from_msg"),
    )

    cb = SimpleNamespace(
        id="cb-notify",
        from_user=SimpleNamespace(id=42, username="owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=777), id=88, reply_markup=plain_markup),
    )
    controller.toggle_notify_msg_handler(cb)

    assert storage.load_data()["notify_updates"] is False
    updated_kb = cardinal.telegram.bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
    updated_btns = {btn.callback_data: btn for row in updated_kb.keyboard for btn in row}
    assert getattr(updated_btns["hub:install:autosmm"], "style", None) == "success"
    assert updated_btns["hub:toggle_notify_from_msg"].text == "Все уведомления: выкл"
    assert getattr(updated_btns["hub:toggle_notify_from_msg"], "style", None) == "danger"


def test_main_menu_highlights_attention_and_shows_environment() -> None:
    state = {"id": "autosmm", "name": "AutoSMM", "status_code": "active",
             "loaded_version": "1.0.3", "installed_version": "1.0.3", "latest_version": "1.0.4",
             "has_update": True, "needs_restart": False}
    text, kb = ui.render_main_menu(
        [state], {"os": "Linux", "python_version": "3.12"}, True, "0.5.0", False, None, 0,
        notice_banner="<b>Hub обновлён</b>",
    )
    assert "<b>W1bbex Hub" in text and "<tg-emoji" in text
    assert "<b>Hub обновлён</b>" in text
    assert "Доступны обновления: <b>1</b>" in text
    assert "Python: 3.12" in text and "ОС: Linux" in text
    buttons = {b.callback_data: b for row in kb.keyboard for b in row}
    assert buttons["hub:catalog:attention:0"].style == "primary"
    assert "hub:settings" in buttons and "hub:licenses" in buttons
    settings_text, settings_kb = ui.render_settings(False, {})
    assert any(b.text == "Уведомления выключены" and b.style == "danger"
               for row in settings_kb.keyboard for b in row if b.callback_data == "hub:toggle_notify")
    restart_text = ui.render_restart_in_progress()
    assert "Перезапускаю FunPay Cardinal" in restart_text
    assert "3–10 секунд" not in restart_text


def test_plugin_info_sanitizes_rich_html_descriptions_without_nested_quotes(isolated_hub: Path) -> None:
    rich_desc = (
        "<b>AutoSMM</b> — <i>автовыдача</i>\n"
        "<blockquote>Быстрая<blockquote>вложенная</blockquote> цитата</blockquote>\n"
        "<script>alert(1)</script> <SMM>"
    )
    sanitized = ui.sanitize_telegram_html(rich_desc)
    assert "<b>AutoSMM</b>" in sanitized
    assert "<i>автовыдача</i>" in sanitized
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in sanitized
    assert "&lt;SMM&gt;" in sanitized
    assert sanitized.count("<blockquote>") == 1
    assert sanitized.count("</blockquote>") == 1

    state = core.inspect_plugin_state(
        make_fake_cardinal(),
        {
            "id": "autosmm",
            "name": "AutoSMM",
            "latest_version": "1.0.3",
            "is_free": True,
            "uuid": "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8",
            "description": rich_desc,
            "credits": "@w1bbex",
        },
        {},
        {},
    )
    text, _ = ui.render_plugin_info(state)
    assert "<b>AutoSMM</b> — <i>автовыдача</i>" in text
    assert text.count("<blockquote>") == 1
