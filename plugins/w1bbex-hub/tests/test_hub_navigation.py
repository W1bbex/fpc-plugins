"""Regression coverage for honest status, offline navigation, and compact Hub UI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from conftest import make_fake_cardinal
from src import core, handlers, hub_api, navigation, runtime, storage, transport, ui
from src.catalog import catalog_status
from src.ui.icons import without_custom_emojis
from src.ui.kit import Btn, KeyboardBuilder


def callback() -> SimpleNamespace:
    return SimpleNamespace(id="review-callback", from_user=SimpleNamespace(id=42, username="owner"),
                           message=SimpleNamespace(chat=SimpleNamespace(id=777), id=15))


def state(pid: str = "autosmm", **changes) -> dict:
    result = dict(id=pid, name=pid.title(), description="Автоматизация заказов", status_code="active",
                  loaded_version="1.0", installed_version="1.0", latest_version="1.1",
                  installed_entry={"version": "1.0"}, has_update=False, needs_restart=False,
                  restart_action="general", is_free=True, has_key=False, commands=[("autosmm", "Управление")],
                  loaded_uuid=None, storage_dir_exists=True, catalog_known=True)
    result.update(changes)
    return result


def test_matching_prefix_saves_key_without_claiming_activation(isolated_hub: Path, monkeypatch) -> None:
    monkeypatch.setattr(hub_api, "get_plugins", lambda: [{"id": "paid", "name": "Paid", "prefix": "AO-"}])
    cardinal = make_fake_cardinal(authorized_users={42})
    message = SimpleNamespace(text="AO-THIS-IS-NOT-A-VALID-KEY", from_user=SimpleNamespace(id=42),
                              chat=SimpleNamespace(id=777))
    handlers.HubController(cardinal)._process_key_input(message, callback())
    text = cardinal.telegram.bot.reply_to.call_args.args[1]
    assert "Ключ сохранён" in text
    assert "не проверен" in text
    assert "активирован" not in text.lower()
    assert message.text not in text
    assert storage.load_data()["keys"]["paid"] == message.text
    markup = cardinal.telegram.bot.reply_to.call_args.kwargs["reply_markup"]
    assert any(b.text == "Установить Paid" for row in markup.keyboard for b in row)


def test_offline_refresh_preserves_local_plugin_and_reports_failure(isolated_hub: Path, monkeypatch) -> None:
    stub = isolated_hub / "plugins" / "AutoSMM.py"
    stub.write_text("# installed", encoding="utf-8")
    storage.save_data({"keys": {}, "installed": {"autosmm": {
        "name": "AutoSMM", "version": "1.0", "stub_path": str(stub),
    }}})
    monkeypatch.setattr(hub_api.requests, "get", MagicMock(side_effect=requests.Timeout("offline")))
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = callback()
    controller.plugins_menu(cb, force_refresh=True, view="mine")
    text = cardinal.telegram.bot.edit_message_text.call_args.args[0]
    markup = cardinal.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"]
    assert any("AutoSMM" in b.text for row in markup.keyboard for b in row)
    assert "Нет связи" in text
    assert "не удалось обновить" in cardinal.telegram.bot.answer_callback_query.call_args.args[1].lower()
    assert catalog_status()["available"] is False
    controller.plugin_info_menu(cb, "autosmm")
    assert "AutoSMM" in cardinal.telegram.bot.edit_message_text.call_args.args[0]
    controller.plugin_info_menu(cb, "autosmm", view="diagnostics")
    assert "неизвестно" in cardinal.telegram.bot.edit_message_text.call_args.args[0]


def test_failed_refresh_labels_retained_catalog_as_stale(isolated_hub: Path, monkeypatch) -> None:
    runtime.PLUGINS_CACHE["items"] = [{"id": "demo", "name": "Demo"}]
    monkeypatch.setattr(hub_api.requests, "get", MagicMock(side_effect=requests.Timeout("offline")))
    assert hub_api.get_plugins(force_refresh=True)[0]["id"] == "demo"
    assert catalog_status()["stale"] is True


def test_successful_empty_catalog_is_distinct_from_failure_and_cached(isolated_hub: Path, monkeypatch) -> None:
    response = MagicMock()
    response.json.return_value = {"success": True, "plugins": []}
    request = MagicMock(return_value=response)
    monkeypatch.setattr(hub_api.requests, "get", request)
    assert hub_api.get_plugins() == []
    assert hub_api.get_plugins() == []
    assert request.call_count == 1
    assert catalog_status()["available"] is True
    text, _ = ui.render_plugins_menu([], catalog_status=catalog_status())
    assert "Нет новых плагинов" in text
    assert "Нет связи" not in text


def test_callback_is_acknowledged_before_network_work_and_only_once(isolated_hub: Path, monkeypatch) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    events = []
    cardinal.telegram.bot.answer_callback_query.side_effect = lambda *a, **kw: events.append("ack")
    monkeypatch.setattr(navigation, "get_main_menu_content", lambda *a, **kw: (events.append("fetch") or ("Hub", None)))
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(controller, "_schedule_menu", lambda *args: None)
    controller.wrap_cb(controller.main_menu)(callback())
    assert events == ["fetch", "ack"]
    assert cardinal.telegram.bot.answer_callback_query.call_count == 1
    assert not controller._acked_callbacks

    cardinal.telegram.bot.answer_callback_query.reset_mock()
    cb2 = callback()
    controller.wrap_cb(lambda c: controller._alert(c, "Всплывашка", show_alert=False))(cb2)
    cardinal.telegram.bot.answer_callback_query.assert_called_once_with(cb2.id, "Всплывашка", show_alert=False)
    cardinal.telegram.bot.send_message.assert_not_called()
    assert not controller._acked_callbacks


def test_main_menu_prioritizes_plugins_and_retains_environment() -> None:
    text, markup = ui.render_main_menu([state()], {"os": "Linux", "python_version": "3.12"},
                                       True, runtime.VERSION, False, None, 2)
    assert "Установлено плагинов: <b>1</b>" in text
    assert "Работают:" not in text and "Изменений для применения нет" not in text
    assert "Python: 3.12" in text and "ОС: Linux" in text
    buttons = [b for row in markup.keyboard for b in row]
    assert {getattr(b, "style", None) for b in buttons} == {"primary", "success"}
    callbacks = [b.callback_data for b in buttons]
    assert len(callbacks) == len(set(callbacks))
    assert "hub:settings" in callbacks and "hub:licenses" in callbacks


def test_catalog_is_paginated_and_names_are_not_duplicated() -> None:
    states = [state(f"plugin{i:02}") for i in range(13)]
    text, markup = ui.render_plugins_menu(states, view="mine")
    assert "Страница 1 из 3" in text
    choices = [b for row in markup.keyboard for b in row if str(b.callback_data).startswith("hub:info:")]
    assert len(choices) == 5
    assert all("Plugin" in b.text for b in choices)
    assert sum("Plugin00" in b.text for b in choices) == 1
    last_text, _ = ui.render_plugins_menu(states, page=999, view="mine")
    assert "Страница 3 из 3" in last_text
    mine_text, _ = ui.render_plugins_menu(states + [state("free", status_code="not_installed")], view="mine")
    assert "Всего 13" in mine_text


def test_card_moves_file_details_to_diagnostics_and_places_status_first() -> None:
    plugin = state(loaded_uuid="abc", has_update=True)
    text, markup = ui.render_plugin_info(plugin)
    assert text.index("Доступно обновление") < text.index("Автоматизация")
    assert "Бесплатный плагин" not in text and "Команда:" not in text
    assert "На диске" not in text
    buttons = [b for row in markup.keyboard for b in row]
    assert buttons[0].callback_data == "hub:install:autosmm"
    assert any(b.text == "Включить" for b in buttons)
    diagnostic, _ = ui.render_plugin_diagnostics(plugin)
    assert "На диске" in diagnostic and "Загружена" in diagnostic


@pytest.mark.parametrize("key", ["SHORT", "AO-12345678-SECRET"])
def test_all_key_screens_mask_even_short_keys(key: str) -> None:
    for text, _ in [ui.render_licenses({"paid": key}), ui.render_key_activated("paid", "Paid", key),
                    ui.render_trial_activated("paid", "Paid", key)]:
        assert key not in text


def test_icon_payload_has_no_duplicate_prefix_and_falls_back_without_premium() -> None:
    markup = KeyboardBuilder().row(Btn("🔑 Ключи", "hub:licenses", icon="key")).build()
    payload = json.loads(markup.to_json())["inline_keyboard"][0][0]
    assert payload["text"] == "Ключи"
    assert payload["icon_custom_emoji_id"].isdigit()
    attempts = []
    text = ui.render_restart_in_progress()
    def invoke(body, keyboard):
        attempts.append((body, keyboard))
        if "tg-emoji" in body:
            raise RuntimeError("Bad Request: custom emoji not allowed: Premium required")
        return "ok"
    assert transport._call_tg_html(invoke, text, markup) == "ok"
    assert "Перезапускаю" in attempts[-1][0]
    assert "tg-emoji" not in attempts[-1][0]
    assert "icon_custom_emoji_id" not in attempts[-1][1].keyboard[0][0].to_dict()


def test_expanded_description_is_bounded_and_does_not_expose_markup() -> None:
    text, _ = ui.render_plugin_info(state(description="<b>Длинное описание</b> " * 1000), expanded=True)
    visible = re.sub("<[^>]*>", "", without_custom_emojis(text))
    assert len(visible) < 4096


def test_notification_toggle_color_and_scope_are_explicit() -> None:
    text, markup = ui.render_update_notification(dict(id="demo", name="Demo", latest="2.0", curr_ver="1.0", update_key="demo_2.0"), False)
    buttons = {b.callback_data: b for row in markup.keyboard for b in row}
    toggle = buttons["hub:toggle_notify_from_msg"]
    assert toggle.text == "Все уведомления: выкл"
    assert getattr(toggle, "style", None) == "danger"


def test_new_navigation_buttons_reach_registered_screens(isolated_hub: Path, monkeypatch) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.init_cardinal(cardinal)
    monkeypatch.setattr(controller, "_states", lambda **kw: ([state(description="Подробное описание " * 80)], {"keys": {}}))
    monkeypatch.setattr(controller, "_schedule_menu", lambda *args: None)
    cb = callback()
    routes = cardinal.telegram.cbq_routes
    cardinal.telegram.set_state(777, 1, 99, "cardinal:ban", {})
    for data, expected in [
        ("hub:catalog:mine:0", "Мои плагины"),
        ("hub:catalog:attention:0", "Требуют внимания"),
        ("hub:settings", "Настройки Hub"),
        ("hub:environment", "Окружение"),
        ("hub:description:autosmm", "Подробное описание"),
        ("hub:diagnostics:autosmm", "Диагностика"),
        ("hub:info:autosmm", "5458489123490804864"),
    ]:
        cardinal.telegram.set_state(777, 1, 42, controller.STATE_AWAIT_KEY, {"callback": cb})
        cb.data = data
        matches = [fn for fn, predicate in routes if predicate(cb)]
        assert len(matches) == 1
        matches[0](cb)
        assert expected in cardinal.telegram.bot.edit_message_text.call_args.args[0]
        assert cardinal.telegram.get_state(777, 42) is None
    assert cardinal.telegram.check_state(777, 99, "cardinal:ban")
    assert cardinal.telegram.bot.clear_step_handler_by_chat_id.call_count == 0


def test_hub_command_escapes_key_input_immediately(isolated_hub: Path, monkeypatch) -> None:
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(navigation, "get_main_menu_content", lambda *a, **kw: ("Hub", None))
    monkeypatch.setattr(controller, "_schedule_menu", lambda *args: None)
    cb = callback()
    cardinal.telegram.set_state(777, 1, 42, controller.STATE_AWAIT_KEY, {"callback": cb})
    message = SimpleNamespace(text="/hub", from_user=SimpleNamespace(id=42, username="owner"),
                              chat=SimpleNamespace(id=777))
    assert controller._is_hub_step_message(message) is True
    controller._handle_step_message(message)
    assert cardinal.telegram.get_state(777, 42) is None
    assert not cardinal.telegram.bot.clear_step_handler_by_chat_id.called
    assert cardinal.telegram.bot.send_message.call_args.args[1] == "Hub"


def test_notification_markup_retries_without_unsupported_custom_icons(isolated_hub: Path) -> None:
    storage.save_data({"notify_updates": True})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    cb = callback()
    cb.message.reply_markup = KeyboardBuilder().row(Btn("Уведомления", "hub:toggle_notify_from_msg")).build()
    attempts = []
    def edit_markup(*args, reply_markup):
        attempts.append(reply_markup)
        if any(getattr(b, "icon_custom_emoji_id", None) for row in reply_markup.keyboard for b in row):
            raise RuntimeError("Bad Request: custom emoji not allowed")
    cardinal.telegram.bot.edit_message_reply_markup.side_effect = edit_markup
    controller.toggle_notify_msg_handler(cb)
    assert len(attempts) == 2
    assert attempts[-1].keyboard[0][0].text == "Все уведомления: выкл"
    assert attempts[-1].keyboard[0][0].to_dict()["style"] == "danger"
    assert storage.load_data()["notify_updates"] is False


def _can_acquire_lock_from_other_thread(lock) -> bool:
    import threading

    can_acquire = [False]

    def probe() -> None:
        if lock.acquire(blocking=False):
            can_acquire[0] = True
            lock.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=2)
    return can_acquire[0]


def test_background_menu_edits_release_lock_before_telegram_io(isolated_hub: Path, monkeypatch) -> None:
    import threading
    from src import navigation

    monkeypatch.setattr(navigation, "fetch_release_notes", lambda: ("99.0", "notes"))
    monkeypatch.setattr(navigation, "catalog_status", lambda: {"available": True})
    cardinal = make_fake_cardinal(authorized_users={42})
    controller = handlers.HubController(cardinal)
    monkeypatch.setattr(controller, "_states", lambda **kw: ([state(name="AutoSMM", has_update=True)], {}))

    orig_edit = controller._edit_html
    release_done = threading.Event()
    check_done = threading.Event()
    release_unlocked: list[bool] = []
    check_unlocked: list[bool] = []

    def tracking_edit(text, chat_id, message_id, **kw):
        if "notes" in text:
            release_unlocked.append(_can_acquire_lock_from_other_thread(controller._menu_background.lock))
            release_done.set()
        if "Доступны обновления плагинов" in text:
            check_unlocked.append(_can_acquire_lock_from_other_thread(controller._menu_background.lock))
            check_done.set()
        return orig_edit(text, chat_id, message_id, **kw)

    monkeypatch.setattr(controller, "_edit_html", tracking_edit)
    controller.hub_release_menu(callback())
    assert release_done.wait(2)
    assert release_unlocked == [True]

    controller.check_updates_menu(callback())
    assert check_done.wait(2)
    assert check_unlocked == [True]
