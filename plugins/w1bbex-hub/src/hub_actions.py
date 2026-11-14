"""Hub user action handlers: license activation, plugin install/delete/open, restart, and Hub updates."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from telebot.types import CallbackQuery, Message

from . import hub_api, installer
from .catalog import merge_local_plugins
from .core import (
    extract_py_plugin_metadata,
    find_loaded_plugin,
    get_disk_hub_pending_version,
    inspect_plugin_state,
    invoke_plugin_command,
)
from .lifecycle import _save_restart_marker
from .runtime import CACHE_LOCK, CB_ANSWERED, PATHS, PLUGINS_CACHE, STOP_EVENT, logger
from .storage import load_data, save_pending_restart, update_data
from .transport import _is_authorized
from .ui import (
    esc,
    render_activate_key_prompt,
    render_catalog_unavailable,
    render_delete_confirm,
    render_delete_result,
    render_hub_update_error,
    render_hub_update_result,
    render_install_result,
    render_key_activated,
    render_missing_key_notice,
    render_restart_in_progress,
    render_trial_activated,
    render_trial_prompt,
)


class HubActions:
    """Action handlers layered into HubController alongside navigation and Telegram transport."""

    def activate_key_handler(self, c: CallbackQuery) -> None:
        text, kb = render_activate_key_prompt()
        msg = self._send_html(c.message.chat.id, text, reply_markup=kb)
        self._set_hub_state(
            c.message.chat.id,
            getattr(msg, "id", getattr(c.message, "id", 0)),
            self._step_user_id(c),
            self.STATE_AWAIT_KEY,
            {"callback": c, "prompt_msg_id": getattr(msg, "id", None)},
        )

    def _is_hub_step_message(self, message: Message) -> bool:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return False
        chat = getattr(message, "chat", None)
        if chat is None:
            return False
        return self._get_hub_state(chat.id, self._step_user_id(message)) is not None

    def _handle_step_message(self, message: Message) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        chat_id = message.chat.id
        user_id = self._step_user_id(message)
        entry = self._get_hub_state(chat_id, user_id)
        if not entry:
            return
        state = entry.get("state")
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        orig_callback = data.get("callback")
        if orig_callback is None:
            self._clear_hub_state(chat_id, user_id)
            return
        prompt_msg_id = data.get("prompt_msg_id")
        if state == self.STATE_AWAIT_KEY:
            self._process_key_input(message, orig_callback, prompt_msg_id=prompt_msg_id)
        elif state == self.STATE_AWAIT_INTERVAL:
            self._process_interval_input(message, orig_callback, prompt_msg_id=prompt_msg_id)

    def _delete_prompt_message(self, chat_id: int, prompt_msg_id: Any) -> None:
        if prompt_msg_id:
            try:
                self.bot.delete_message(chat_id, prompt_msg_id)
            except Exception:
                logger.debug("Could not delete prompt message %s", prompt_msg_id, exc_info=True)

    def _process_key_input(
        self,
        message: Message,
        orig_callback: CallbackQuery,
        *,
        prompt_msg_id: Any = None,
    ) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        chat_id = message.chat.id
        user_id = self._step_user_id(message)
        key = message.text.strip() if message.text else ""
        if key.startswith("/"):
            self._clear_hub_state(chat_id, user_id)
            self._delete_prompt_message(chat_id, prompt_msg_id)
            if key.split()[0].split("@")[0].lower() == "/hub":
                self.hub_command(message)
            else:
                self._reply_html(message, "Ввод ключа отменён · отправьте команду ещё раз")
            return
        if key.lower() == "отмена":
            self._clear_hub_state(chat_id, user_id)
            self._delete_prompt_message(chat_id, prompt_msg_id)
            self.bot.send_message(chat_id, "Действие отменено.")
            def _send_fresh() -> None:
                text, kb = self._main_content(message.from_user)
                self._send_html(chat_id, text, reply_markup=kb)

            self._safe_refresh_callback_screen(
                orig_callback,
                self.main_menu,
                _send_fresh,
            )
            return
        if not key:
            self._delete_prompt_message(chat_id, prompt_msg_id)
            retry_msg = self.bot.reply_to(message, "Пустой ключ. Попробуйте еще раз или отправьте 'отмена'.")
            self._set_hub_state(
                chat_id,
                getattr(retry_msg, "id", getattr(message, "id", 0)),
                user_id,
                self.STATE_AWAIT_KEY,
                {"callback": orig_callback, "prompt_msg_id": getattr(retry_msg, "id", None)},
            )
            return

        local = load_data().get("installed") or {}
        plugins = merge_local_plugins(hub_api.get_plugins(), local)
        candidates = sorted(
            (p for p in plugins if p.get("prefix")),
            key=lambda p: len(str(p.get("prefix", ""))),
            reverse=True,
        )
        matched = next((p for p in candidates if key.upper().startswith(str(p["prefix"]).upper())), None)
        if not matched:
            self._delete_prompt_message(chat_id, prompt_msg_id)
            prefixes = "".join(
                f"• <code>{esc(p['prefix'])}...</code> — {esc(p.get('name', ''))}\n"
                for p in plugins
                if p.get("prefix")
            )
            retry_msg = self._reply_html(
                message,
                (f"<b>Не удалось определить плагин</b>\nПроверьте префикс ключа:\n{prefixes}"
                 if prefixes else "Каталог недоступен · повторите ввод позже или отправьте «отмена»"),
            )
            self._set_hub_state(
                chat_id,
                getattr(retry_msg, "id", getattr(message, "id", 0)),
                user_id,
                self.STATE_AWAIT_KEY,
                {"callback": orig_callback, "prompt_msg_id": getattr(retry_msg, "id", None)},
            )
            return

        self._clear_hub_state(chat_id, user_id)
        self._delete_prompt_message(chat_id, prompt_msg_id)
        plugin_name = matched["id"]
        plugin_label = str(matched.get("name", plugin_name))
        try:
            update_data(lambda d: d.setdefault("keys", {}).__setitem__(plugin_name, key))
        except OSError as exc:
            logger.exception("Failed saving key for %s", plugin_name)
            self._reply_html(message, f"<b>Ошибка сохранения ключа:</b> {esc(exc)}")
            return

        text, kb = render_key_activated(plugin_name, plugin_label, key)
        self._reply_html(message, text, reply_markup=kb)

    def _run_install_plugin(
        self,
        c: CallbackQuery,
        plugin_name: str,
        msg_obj: Any,
        action_key: str,
        *,
        background: bool = False,
        catalog: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        def _can_edit() -> bool:
            return not background or not STOP_EVENT.is_set()

        try:
            catalog = catalog if catalog is not None else hub_api.get_plugins()
            plugin_info = next((x for x in catalog if x.get("id") == plugin_name), None)
            plugin_label = str(plugin_info.get("name", plugin_name)) if plugin_info else plugin_name
            is_free = bool(plugin_info.get("is_free")) if plugin_info else False

            data = load_data()
            installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
            was_installed = plugin_name in installed
            key = (data.get("keys") or {}).get(plugin_name)
            if not plugin_info and not catalog and not key:
                text, kb = render_catalog_unavailable(plugin_name)
                if _can_edit():
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                return
            if not key and not is_free:
                text, kb = render_missing_key_notice(plugin_name, plugin_label)
                if _can_edit():
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                return

            if _can_edit():
                self._edit_html(
                    f"<i>Скачиваю и устанавливаю {esc(plugin_label)}...</i>",
                    msg_obj.chat.id,
                    msg_obj.id,
                )
            success, msg = installer.download_plugin(self.cardinal, plugin_name, key, c.from_user)
            if success:
                after_installed = load_data().get("installed")
                after_entry = after_installed.get(plugin_name) if isinstance(after_installed, dict) else None
                if not isinstance(after_entry, dict):
                    after_entry = {}
                _save_restart_marker(
                    action="update" if was_installed else "install",
                    chat_id=msg_obj.chat.id,
                    plugin_id=plugin_name,
                    plugin_name=plugin_label,
                    target_version=str(after_entry.get("version") or (plugin_info or {}).get("latest_version") or ""),
                    expected_uuid=str((plugin_info or {}).get("uuid") or ""),
                )

            text, kb = render_install_result(plugin_name, plugin_label, success, msg, is_update=was_installed)
            if _can_edit():
                self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
        except Exception:
            if not background:
                raise
            logger.exception("Async install failed for %s", plugin_name)
            if _can_edit():
                try:
                    self._edit_html(
                        "<b>Ошибка установки плагина</b>\nПовторите попытку позже или откройте /hub.",
                        msg_obj.chat.id,
                        msg_obj.id,
                    )
                except Exception:
                    logger.debug("Could not display install error", exc_info=True)
        finally:
            self._end_action(action_key)

    def install_plugin(self, c: CallbackQuery, plugin_name: Optional[str], *, background: bool = False) -> Any:
        if not plugin_name:
            return self._alert(c, "Некорректный идентификатор плагина")
        msg_obj = getattr(c, "message", None)
        if msg_obj is None:
            return self._alert(c, "Сообщение устарело. Откройте /hub заново.")

        action_key = f"install:{plugin_name}"
        if not self._try_begin_action(action_key):
            return self._alert(c, "Установка уже выполняется · подождите")

        if background:
            with CACHE_LOCK:
                cached = PLUGINS_CACHE.get("items")
                cached_list = list(cached) if isinstance(cached, list) else []
            cached_info = next((x for x in cached_list if isinstance(x, dict) and x.get("id") == plugin_name), None)
            plugin_label = str(cached_info.get("name", plugin_name)) if cached_info else plugin_name
            try:
                self._ack(c, f"Устанавливаю {plugin_label}")
            except Exception:
                logger.debug("Early callback ack failed for install:%s", plugin_name, exc_info=True)
            threading.Thread(
                target=self._run_install_plugin,
                args=(c, plugin_name, msg_obj, action_key),
                kwargs={"background": True},
                name=f"hub-install-{plugin_name}",
                daemon=True,
            ).start()
            return CB_ANSWERED

        try:
            catalog = hub_api.get_plugins()
            plugin_info = next((x for x in catalog if x.get("id") == plugin_name), None)
            plugin_label = str(plugin_info.get("name", plugin_name)) if plugin_info else plugin_name
            data = load_data()
            key = (data.get("keys") or {}).get(plugin_name)
            is_free = bool(plugin_info.get("is_free")) if plugin_info else False
            if (plugin_info or catalog or key) and (key or is_free):
                try:
                    self._ack(c, f"Устанавливаю {plugin_label}")
                except Exception:
                    logger.debug("Early callback ack failed for install:%s", plugin_name, exc_info=True)
        except Exception:
            self._end_action(action_key)
            raise

        self._run_install_plugin(c, plugin_name, msg_obj, action_key, background=False, catalog=catalog)
        return CB_ANSWERED

    def trial_request(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        p = next((x for x in hub_api.get_plugins() if x.get("id") == plugin_name), None)
        if not p or not plugin_name:
            return self._alert(c, "Плагин не найден")
        if p.get("is_free"):
            return self._alert(c, "Этот плагин бесплатный · пробный ключ не требуется")
        text, kb = render_trial_prompt(p, plugin_name)
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb, disable_web_page_preview=True)
        return None

    def _run_trial_confirm(self, c: CallbackQuery, plugin_name: str, msg_obj: Any, action_key: str) -> None:
        try:
            ok, result = hub_api.request_trial_key(self.cardinal, plugin_name, c.from_user)
            if not ok:
                if not STOP_EVENT.is_set():
                    self._alert(c, f"{result}")
                return

            p = next((x for x in hub_api.get_plugins() if x.get("id") == plugin_name), None)
            plugin_label = str(p.get("name", plugin_name)) if p else plugin_name
            try:
                update_data(lambda d: d.setdefault("keys", {}).__setitem__(plugin_name, result))
            except OSError as exc:
                logger.exception("Failed saving trial key for %s", plugin_name)
                if not STOP_EVENT.is_set():
                    self._alert(c, f"Ошибка сохранения ключа: {exc}")
                return

            text, kb = render_trial_activated(plugin_name, plugin_label, result)
            if not STOP_EVENT.is_set():
                self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
        except Exception:
            logger.exception("Trial request failed for %s", plugin_name)
        finally:
            self._end_action(action_key)

    def trial_confirm(self, c: CallbackQuery, plugin_name: Optional[str], *, background: bool = False) -> Any:
        if not plugin_name:
            return self._alert(c, "Некорректный идентификатор плагина")
        msg_obj = getattr(c, "message", None)
        if msg_obj is None:
            return self._alert(c, "Сообщение устарело. Откройте /hub заново.")

        action_key = f"trial:{plugin_name}"
        if not self._try_begin_action(action_key):
            return self._alert(c, "Запрос пробного ключа уже выполняется · подождите")

        if background:
            try:
                self._ack(c, "Запрашиваю пробный ключ…")
            except Exception:
                logger.debug("Early callback ack failed for trial:%s", plugin_name, exc_info=True)
            threading.Thread(
                target=self._run_trial_confirm,
                args=(c, plugin_name, msg_obj, action_key),
                name=f"hub-trial-{plugin_name}",
                daemon=True,
            ).start()
            return CB_ANSWERED

        self._run_trial_confirm(c, plugin_name, msg_obj, action_key)
        return None

    def delete_confirm_handler(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        if not plugin_name:
            return self._alert(c, "Плагин не установлен или уже удален")

        data = load_data()
        installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
        if plugin_name not in installed:
            return self._alert(c, "Плагин не установлен или уже удален")

        p_info = next((x for x in hub_api.get_plugins() if x.get("id") == plugin_name), None) or {
            "id": plugin_name,
            "name": installed[plugin_name].get("name") or plugin_name,
        }
        state = inspect_plugin_state(self.cardinal, p_info, installed, data.get("keys"))
        text, kb = render_delete_confirm(state)
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb)
        return None

    def delete_plugin_handler(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        if not plugin_name:
            return self._alert(c, "Плагин не установлен или уже удален")

        data = load_data()
        installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
        if plugin_name not in installed:
            return self._alert(c, "Плагин не установлен или уже удален")

        p_info = next((x for x in hub_api.get_plugins() if x.get("id") == plugin_name), None) or {
            "id": plugin_name,
            "name": installed[plugin_name].get("name") or plugin_name,
        }
        pre_state = inspect_plugin_state(self.cardinal, p_info, installed, data.get("keys"))

        try:
            result = installer.delete_plugin_files(plugin_name)
        except OSError as exc:
            logger.exception("Failed deleting plugin %s", plugin_name)
            return self._alert(c, f"Ошибка удаления: {exc}")

        if not result.get("ok"):
            return self._alert(c, f"Отказ: {result.get('error')}")

        _save_restart_marker(
            action="delete",
            chat_id=c.message.chat.id,
            plugin_id=plugin_name,
            plugin_name=str(result.get("plugin_name") or plugin_name),
            expected_uuid=str(p_info.get("uuid") or pre_state.get("loaded_uuid") or ""),
            stub_path=str(result.get("stub_path") or ""),
            bin_dir=str(result.get("bin_dir") or ""),
        )

        text, kb = render_delete_result(
            result=result,
            was_loaded_in_ram=bool(pre_state.get("loaded_uuid")),
            had_storage_dir=bool(pre_state.get("storage_dir_exists")),
        )
        self._edit_html(text, c.message.chat.id, c.message.id, reply_markup=kb)
        return None

    def open_plugin_handler(self, c: CallbackQuery, plugin_name: Optional[str]) -> Any:
        if not plugin_name:
            return self._alert(c, "Плагин не указан")

        p_info = next((x for x in hub_api.get_plugins() if x.get("id") == plugin_name), None) or {
            "id": plugin_name,
            "name": plugin_name,
        }
        installed = load_data().get("installed") or {}
        _uid, loaded_plugin = find_loaded_plugin(self.cardinal, p_info, installed.get(plugin_name))
        if loaded_plugin is None:
            return self._alert(c, "Плагин ещё не загружен в память Cardinal. Сначала перезапустите бота.")

        ok, detail = invoke_plugin_command(self.cardinal, loaded_plugin, c.message, c.from_user)
        if not ok:
            return self._alert(c, detail)

        self._ack(c, f"Открываю /{detail}")
        return CB_ANSWERED

    def restart_cardinal_handler(self, c: CallbackQuery, arg: Optional[str]) -> Any:
        parts = (arg or "general").split(":", 1)
        action = parts[0] or "general"
        pid = parts[1] if len(parts) > 1 else ""
        msg_id = getattr(getattr(c, "message", None), "id", None)

        existing = load_data().get("pending_restart")
        if (
            isinstance(existing, dict)
            and existing.get("action") == action
            and str(existing.get("plugin_id") or "") == pid
        ):
            payload = dict(existing)
            payload["chat_id"] = c.message.chat.id
            payload["message_id"] = msg_id
            payload["created_at"] = time.time()
            try:
                save_pending_restart(payload)
            except OSError:
                logger.exception("Failed updating pending_restart before reboot")
        else:
            plugin_label = ""
            expected_uuid = ""
            target_version = ""
            if action == "hub":
                target_version = get_disk_hub_pending_version() or ""
            elif pid:
                p_info = next((x for x in hub_api.get_plugins() if x.get("id") == pid), None)
                installed_map = load_data().get("installed")
                installed_entry = installed_map.get(pid) if isinstance(installed_map, dict) else None
                if not isinstance(installed_entry, dict):
                    installed_entry = {}
                plugin_label = str((p_info or {}).get("name") or installed_entry.get("name") or pid)
                expected_uuid = str((p_info or {}).get("uuid") or "")
                target_version = str(installed_entry.get("version") or (p_info or {}).get("latest_version") or "")
            _save_restart_marker(
                action=action,
                chat_id=c.message.chat.id,
                message_id=msg_id,
                plugin_id=pid,
                plugin_name=plugin_label,
                target_version=target_version,
                expected_uuid=expected_uuid,
            )

        logger.info(
            "Restarting Cardinal via Hub (action=%s pid=%s chat_id=%s msg_id=%s)",
            action,
            pid or "-",
            c.message.chat.id,
            msg_id,
        )
        self._edit_html(
            render_restart_in_progress(),
            c.message.chat.id,
            c.message.id,
        )
        self._ack(c, "Перезапуск запущен")
        installer.trigger_cardinal_restart()
        return CB_ANSWERED

    def _run_hub_update(self, msg_obj: Any, action_key: str) -> None:
        try:
            self._edit_html("<i>Скачивание обновления Hub...</i>", msg_obj.chat.id, msg_obj.id)
            ok, message = installer.download_and_update_hub()
            if ok:
                disk_ver = None
                if PATHS.hub_file and os.path.isfile(PATHS.hub_file):
                    disk_ver = extract_py_plugin_metadata(PATHS.hub_file).get("version")
                _save_restart_marker(
                    action="hub",
                    chat_id=msg_obj.chat.id,
                    message_id=msg_obj.id,
                    target_version=str(disk_ver or ""),
                )
                logger.info("Downloaded Hub update v%s; saved pending_restart marker", disk_ver or "?")
                text, kb = render_hub_update_result(str(disk_ver) if disk_ver else None)
                if not STOP_EVENT.is_set():
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                return
            text, kb = render_hub_update_error(message)
            if not STOP_EVENT.is_set():
                self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
        except Exception:
            logger.exception("Async Hub update failed")
            if not STOP_EVENT.is_set():
                try:
                    text, kb = render_hub_update_error("Неожиданная ошибка при загрузке обновления Hub.")
                    self._edit_html(text, msg_obj.chat.id, msg_obj.id, reply_markup=kb)
                except Exception:
                    logger.debug("Could not display Hub update error", exc_info=True)
        finally:
            self._end_action(action_key)

    def hub_update_handler(self, c: CallbackQuery, *, background: bool = False) -> Any:
        msg_obj = getattr(c, "message", None)
        if msg_obj is None:
            return self._alert(c, "Сообщение устарело. Откройте /hub заново.")
        action_key = "update_hub"
        if not self._try_begin_action(action_key):
            return self._alert(c, "Обновление Hub уже выполняется · подождите")
        try:
            self._ack(c, "Загружаю обновление Hub")
        except Exception:
            logger.debug("Early callback ack failed for hub_update_handler", exc_info=True)

        if background:
            threading.Thread(
                target=self._run_hub_update,
                args=(msg_obj, action_key),
                name="hub-self-update",
                daemon=True,
            ).start()
            return CB_ANSWERED

        self._run_hub_update(msg_obj, action_key)
        return CB_ANSWERED

    def hub_command(self, message: Message) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        self._clear_hub_state(message.chat.id, self._step_user_id(message))
        chat_type = str(getattr(message.chat, "type", "") or "").lower()
        is_private_chat = (chat_type == "private") or (not chat_type and isinstance(message.chat.id, int) and message.chat.id > 0)
        if is_private_chat:
            try:
                update_data(lambda d: d.__setitem__("owner_tg_id", message.chat.id))
            except OSError:
                logger.exception("Failed saving owner_tg_id")
        text, kb = self._main_content(message.from_user)
        sent = self._send_html(message.chat.id, text, reply_markup=kb)
        msg_id = getattr(sent, "message_id", None) or getattr(sent, "id", None)
        if msg_id:
            def redraw() -> None:
                new_text, new_kb = self._main_content(message.from_user)
                if new_text != text or new_kb.to_json() != kb.to_json():
                    self._edit_html(new_text, message.chat.id, msg_id, reply_markup=new_kb)
            self._schedule_menu(message.chat.id, msg_id, message.from_user, redraw)

    def set_key_command(self, message: Message) -> None:
        if not _is_authorized(self.tg, getattr(message, "from_user", None)):
            return
        self._clear_hub_state(message.chat.id, self._step_user_id(message))
        parts = (message.text or "").strip().split(maxsplit=2)
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            self._reply_html(
                message,
                "Использование: <code>!hub_key [plugin] [key]</code>\nПример: <code>!hub_key autooffline TRIAL-123</code>",
            )
            return

        raw_plugin, key = parts[1].strip(), parts[2].strip()
        catalog = hub_api.get_plugins()
        p = next((x for x in catalog if str(x.get("id") or "").lower() == raw_plugin.lower()), None)
        if p is not None:
            plugin = str(p["id"])
        else:
            installed_map = load_data().get("installed")
            matched_local = (
                next((k for k in installed_map if str(k).lower() == raw_plugin.lower()), None)
                if isinstance(installed_map, dict)
                else None
            )
            plugin = str(matched_local or raw_plugin)
        try:
            update_data(lambda d: d.setdefault("keys", {}).__setitem__(plugin, key))
        except OSError as exc:
            logger.exception("Failed saving manual key for %s", plugin)
            self._reply_html(message, f"<b>Ошибка сохранения ключа:</b> {esc(exc)}")
            return

        plugin_label = str(p.get("name", plugin)) if p else plugin
        text, kb = render_key_activated(plugin, plugin_label, key)
        self._reply_html(message, text, reply_markup=kb)
