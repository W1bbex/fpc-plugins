"""Regression tests for the real AutoSMM legacy metadata and editable descriptions."""

from pathlib import Path
from types import SimpleNamespace

from conftest import make_fake_cardinal
from src import core, handlers, storage, ui
from src.ui.kit import sanitize_telegram_html


UUID = "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8"


def test_description_preserves_telegram_entities_and_safe_custom_emoji() -> None:
    source = ('<tg-emoji emoji-id="5188481279963715781">🚀</tg-emoji> <b>AutoSMM</b> <code>v1.0.1</code>\n\n\n'
              '<b>Автоматизация</b>\n\n<i>Строка 1\n    Строка 2</i>\n\n'
              '<blockquote expandable>Первая\nВторая</blockquote>\n'
              '<a href="https://example.com/">Ссылка</a>')
    safe = sanitize_telegram_html(source)
    assert '<tg-emoji emoji-id="5188481279963715781">🚀</tg-emoji>' in safe
    assert '<i>Строка 1\n    Строка 2</i>' in safe
    assert '<blockquote expandable>Первая\nВторая</blockquote>' in safe
    assert '<a href="https://example.com/">Ссылка</a>' in safe
    invalid = sanitize_telegram_html('<tg-emoji emoji-id="1\" onload=\"bad">🚀</tg-emoji>')
    assert '<tg-emoji emoji-id=' not in invalid


def test_autosmm_card_uses_dynamic_header_without_forced_details(isolated_hub: Path) -> None:
    info = {"id": "autosmm", "name": "AutoSMM", "latest_version": "1.0.1", "is_free": True,
            "description": '<tg-emoji emoji-id="5188481279963715781">🚀</tg-emoji> <b>AutoSMM</b> <code>v1.0.1</code>\n\n<b>Мой текст</b>\n\n<i>Оформление</i>'}
    state = core.inspect_plugin_state(make_fake_cardinal(), info, {}, {})
    body, _ = ui.render_plugin_info(state)
    assert '5458489123490804864' in body
    assert body.count('<b>AutoSMM</b>') == 1
    assert '<b>Мой текст</b>\n\n<i>Оформление</i>' in body
    assert 'Бесплатный плагин' not in body and 'Команда:' not in body
    state.update(status_code="active", loaded_enabled=True)
    active, _ = ui.render_plugin_info(state)
    assert 'Отключён в Cardinal' not in active


def test_legacy_autosmm_009_is_detected_and_notified_before_hub_command(isolated_hub: Path, monkeypatch) -> None:
    plugin_file = isolated_hub / "plugins" / "AutoSMM.py"
    plugin_file.write_text(f'NAME = "AutoSMM"\nVERSION = "0.0.9"\nCREDITS = "@w1bbex"\nUUID = "{UUID}"\n', encoding="utf-8")
    plugin = {"id": "autosmm", "name": "AutoSMM", "uuid": UUID, "credits": "@w1bbex",
              "latest_version": "1.0.1", "is_free": True, "notify_users": True}
    cardinal = make_fake_cardinal(authorized_users={42})
    cardinal.plugins = {UUID: SimpleNamespace(name="AutoSMM", uuid=UUID, version="0.0.9",
                        path=str(plugin_file), enabled=True, commands={"autosmm": "Меню"}, settings_page=False)}
    storage.save_data({"keys": {}})
    monkeypatch.setattr(handlers, "get_plugins", lambda: [plugin])
    monkeypatch.setattr(handlers, "notify_hub_update", lambda *args: 0)
    state = core.inspect_plugin_state(cardinal, plugin, {}, {})
    assert state["status_code"] == "manual" and state["manual_version"] == "0.0.9" and state["has_update"]
    _, markup = ui.render_plugin_info(state)
    assert any(b.callback_data == "hub:install:autosmm" and "Обновить" in b.text for row in markup.keyboard for b in row)
    assert handlers.run_update_check_cycle(cardinal) == 1
    assert handlers.run_update_check_cycle(cardinal) == 0
    assert cardinal.telegram.bot.send_message.call_args.args[0] == 42
    notification = cardinal.telegram.bot.send_message.call_args.args[1]
    assert "0.0.9" in notification and "1.0.1" in notification
    assert "installed" not in storage.load_data()


def test_manual_updates_respect_opt_out_and_ambiguous_owner(isolated_hub: Path, monkeypatch) -> None:
    monkeypatch.setattr(handlers, "fetch_hub_latest_version", lambda *args: ("0.5.8", False))
    (isolated_hub / "plugins" / "AutoSMM.py").write_text(f'VERSION = "0.0.9"\nCREDITS = "@w1bbex"\nUUID = "{UUID}"\n', encoding="utf-8")
    monkeypatch.setattr(handlers, "get_plugins", lambda: [{"id": "autosmm", "name": "AutoSMM", "uuid": UUID,
                                                          "credits": "@w1bbex", "latest_version": "1.0.1"}])
    monkeypatch.setattr(handlers, "notify_hub_update", lambda *args: 0)
    cardinal = make_fake_cardinal(authorized_users={42, 43})
    storage.save_data({"keys": {}})
    assert handlers.run_update_check_cycle(cardinal) == 0
    cardinal.telegram.bot.send_message.assert_not_called()
    cardinal.telegram.authorized_users = {42}
    storage.save_data({"keys": {}, "notify_updates": False})
    assert handlers.run_update_check_cycle(cardinal) == 0
    cardinal.telegram.bot.send_message.assert_not_called()


def test_matching_manual_file_without_loaded_plugin_is_checked(isolated_hub: Path, monkeypatch) -> None:
    (isolated_hub / "plugins" / "AutoSMM.py").write_text(f'VERSION = "0.0.9"\nCREDITS = "@w1bbex"\nUUID = "{UUID}"\n', encoding="utf-8")
    plugin = {"id": "autosmm", "name": "AutoSMM", "uuid": UUID, "credits": "@w1bbex", "latest_version": "1.0.1"}
    monkeypatch.setattr(handlers, "get_plugins", lambda: [plugin])
    monkeypatch.setattr(handlers, "notify_hub_update", lambda *args: 0)
    cardinal = make_fake_cardinal(authorized_users={42})
    cardinal.plugins = {}
    storage.save_data({"keys": {}})
    assert handlers.run_update_check_cycle(cardinal) == 1
    assert "0.0.9" in cardinal.telegram.bot.send_message.call_args.args[1]


def test_foreign_or_unversioned_manual_files_do_not_trigger_update(isolated_hub: Path, monkeypatch) -> None:
    plugin = {"id": "autosmm", "name": "AutoSMM", "uuid": UUID, "credits": "@w1bbex", "latest_version": "1.0.1"}
    monkeypatch.setattr(handlers, "get_plugins", lambda: [plugin])
    monkeypatch.setattr(handlers, "notify_hub_update", lambda *args: 0)
    cardinal = make_fake_cardinal(authorized_users={42})
    cardinal.plugins = {}
    storage.save_data({"keys": {}})
    path = isolated_hub / "plugins" / "AutoSMM.py"
    path.write_text(f'VERSION = "0.0.9"\nCREDITS = "@someone_else"\nUUID = "{UUID}"\n', encoding="utf-8")
    assert handlers.run_update_check_cycle(cardinal) == 0
    path.write_text(f'CREDITS = "@w1bbex"\nUUID = "{UUID}"\n', encoding="utf-8")
    assert handlers.run_update_check_cycle(cardinal) == 0
    cardinal.telegram.bot.send_message.assert_not_called()
