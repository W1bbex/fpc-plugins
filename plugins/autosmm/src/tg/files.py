"""Telegram UI workflows for backups, imports/uploads, log exports, and data cleanup."""

from __future__ import annotations

from datetime import datetime
import io
import json
import os
import threading
from typing import Any, Optional, Tuple

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..storage.backups import _create_full_backup, _restore_backup_wrapper
from ..storage.storage import (
    read_log_export_bytes,
    wipe_all_autosmm_data,
)
from .common import (
    STATE_RESTORE_BACKUP,
    _clear_navigation_states,
    _clear_step_handler,
    _clear_tg_state,
    _is_authorized_tg_user,
    _safe_answer_callback,
    _safe_edit_message_text,
    _set_tg_state,
    h,
    register_navigation_target,
)

MAX_BACKUP_UPLOAD_BYTES = 20 * 1024 * 1024


def _render_files_menu(call: types.CallbackQuery) -> None:
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _clear_tg_state(call.message.chat.id, getattr(getattr(call, "from_user", None), "id", None))

    txt_ = (
        "<b>📁 Бэкап / файлы AutoSMM</b>\n\n"
        "Здесь можно сохранить данные, загрузить бэкап, выгрузить логи или очистить данные AutoSMM."
    )
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("📤 Экспорт бэкапа", callback_data="full_backup_prompt"),
        InlineKeyboardButton("📥 Загрузка бэкапа", callback_data="restore_backup_prompt"),
        InlineKeyboardButton("📝 Логи", callback_data="export_errors"),
        InlineKeyboardButton("🧹 Очистка AutoSMM", callback_data="autosmm_cleanup_menu"),
        InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"),
    )
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def files_menu(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _render_files_menu(call)


def files_menu_callback(call: types.CallbackQuery) -> None:
    files_menu(call)


def auto_smm_delete(message: types.Message) -> None:
    """Полная очистка данных AutoSMM после аварийного бэкапа."""
    prev_running = rt.RUNNING
    rt.RUNNING = False

    try:
        backup_path = _create_full_backup(reason="before_delete")
    except Exception as ex:
        rt.logger.exception(f"Не удалось создать аварийный бэкап перед очисткой: {ex}")
        backup_path = None
    if not backup_path:
        rt.RUNNING = prev_running
        rt.bot.send_message(
            message.chat.id,
            "❌ Не удалось создать аварийный бэкап. Очистка отменена.",
            reply_markup=_files_back_kb(),
            parse_mode="HTML",
        )
        return

    try:
        with open(backup_path, "rb") as backup_file:
            rt.bot.send_document(message.chat.id, backup_file, caption="Аварийный бэкап AutoSMM перед очисткой")
    except Exception as ex:
        rt.logger.warning(f"Не удалось отправить аварийный бэкап пользователю: {ex}")

    wipe_all_autosmm_data()

    filename = os.path.basename(backup_path)
    txt = f"✅ Данные AutoSMM очищены.\n\nПеред очисткой создан аварийный бэкап:\n<code>{h(filename)}</code>\n\nРекомендуется перезапустить Cardinal."
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ В меню", callback_data="return_to_settings"))
    rt.bot.send_message(message.chat.id, txt, reply_markup=kb, parse_mode="HTML")


def full_backup_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("📤 Создать бэкап", callback_data="full_backup_confirm"),
        InlineKeyboardButton("◀️ Назад", callback_data="files_menu"),
    )
    txt_ = (
        "⚠️ <b>Экспорт бэкапа AutoSMM</b>\n\n"
        "Бэкап содержит настройки AutoSMM, лоты, заказы, разрешённые сайты и техническое состояние плагина.\n\n"
        "Также внутри могут быть API-ключи панелей.\n\n"
        "Не отправляйте этот файл посторонним."
    )
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def full_backup_confirm(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    backup_path = _create_full_backup(reason="manual_export")
    if not backup_path:
        rt.bot.send_message(call.message.chat.id, "❌ Не удалось создать бэкап AutoSMM.", reply_markup=_files_back_kb())
        files_menu(call)
        return
    try:
        with open(backup_path, "rb") as backup_file:
            rt.bot.send_document(call.message.chat.id, backup_file, caption="📤 Бэкап AutoSMM")
    except Exception as ex:
        rt.bot.send_message(call.message.chat.id, f"❌ Не удалось отправить бэкап: {ex}", reply_markup=_files_back_kb())
    files_menu(call)


def restore_backup_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _set_tg_state(
        call.message.chat.id,
        call.message.message_id,
        call.from_user.id,
        STATE_RESTORE_BACKUP,
    )
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    txt_ = (
        "📥 <b>Загрузка бэкапа AutoSMM</b>\n\n"
        "Отправьте JSON-файл бэкапа AutoSMM.\n\n"
        "Перед загрузкой AutoSMM создаст аварийный бэкап текущих данных."
    )
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def autosmm_cleanup_menu(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    txt_ = (
        "🧹 <b>Очистка AutoSMM</b>\n\n"
        "Выберите, что нужно очистить.\n\n"
        "Перед очисткой данных AutoSMM будет создан аварийный бэкап."
    )
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("🧹 Очистить данные AutoSMM", callback_data="full_autosmm_delete_prompt"),
        InlineKeyboardButton("◀️ Назад", callback_data="files_menu"),
    )
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def full_autosmm_delete_prompt(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("✅ Да, очистить", callback_data="full_autosmm_delete_confirm"),
        InlineKeyboardButton("◀️ Назад", callback_data="autosmm_cleanup_menu"),
    )
    txt_ = (
        "⚠️ <b>Очистить данные AutoSMM?</b>\n\n"
        "Будут удалены:\n"
        "• настройки лотов;\n"
        "• история заказов;\n"
        "• состояние заказов;\n"
        "• разрешённые сайты;\n"
        "• состояние плагина.\n\n"
        "Перед удалением AutoSMM создаст аварийный бэкап."
    )
    _safe_edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def full_autosmm_delete_confirm(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    try:
        _safe_edit_message_text(
            "⏳ <b>Выполняю очистку данных AutoSMM...</b>",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
        )
    except Exception as exc:
        rt.logger.debug(f"Could not clear cleanup confirmation keyboard: {exc}")

    class _Msg:
        chat = call.message.chat

    auto_smm_delete(_Msg())


def _send_log_file_async(chat_id: int, caption_prefix: str, on_empty_text: str = "Логи пока не найдены.") -> None:
    def _worker() -> None:
        for handler in list(rt.logger.handlers):
            try:
                handler.flush()
            except Exception as flush_exc:
                rt.logger.debug(f"Не удалось сбросить буфер обработчика логов: {flush_exc}")

        if not os.path.exists(rt.LOG_PATH) or os.path.getsize(rt.LOG_PATH) == 0:
            try:
                rt.bot.send_message(chat_id, h(on_empty_text), reply_markup=_files_back_kb(), parse_mode="HTML")
            except Exception as exc:
                rt.logger.debug(f"Failed notifying empty logs to chat {chat_id}: {exc}")
            return

        try:
            filename, data_bytes, size_kb = read_log_export_bytes()
            full_caption = (
                f"{caption_prefix}\n"
                f"📊 Размер: {size_kb} КБ\n"
                f"⏱️ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            buffer = io.BytesIO(data_bytes)
            buffer.name = filename
            rt.bot.send_document(chat_id, buffer, caption=full_caption, parse_mode="HTML")
            rt.logger.debug(f"[LOG EXPORT] Log document sent to chat_id={chat_id}")
        except Exception as ex:
            rt.logger.error(f"[LOG EXPORT ERROR] chat_id={chat_id}: {ex}")
            try:
                rt.bot.send_message(
                    chat_id,
                    f"❌ Ошибка отправки лог-файла: {h(ex)}",
                    reply_markup=_files_back_kb(),
                    parse_mode="HTML",
                )
            except Exception as notify_exc:
                rt.logger.debug(f"Failed sending log export error to chat {chat_id}: {notify_exc}")

    threading.Thread(target=_worker, daemon=True).start()


def export_errors(call: types.CallbackQuery) -> None:
    _send_log_file_async(call.message.chat.id, "📝 <b>Логи AutoSMM</b>", on_empty_text="Логи ошибок пока не найдены.")
    _safe_answer_callback(call, "Логи отправляются...")
    _render_files_menu(call)


def _files_back_kb() -> InlineKeyboardMarkup:
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    return kb_


def _receive_tg_json_document(message: types.Message, *, context_label: str) -> Tuple[bool, Any]:
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    user_id = getattr(getattr(message, "from_user", None), "id", None)

    if not _is_authorized_tg_user(user_id):
        rt.logger.warning(f"Отклонена неавторизованная загрузка {context_label}: user_id={user_id}")
        return False, None

    document = getattr(message, "document", None)
    if document is None:
        rt.bot.send_message(chat_id, "❌ Отправьте JSON-файл документом.", reply_markup=_files_back_kb(), parse_mode="HTML")
        return False, None

    file_size = getattr(document, "file_size", None)
    if isinstance(file_size, int) and file_size > MAX_BACKUP_UPLOAD_BYTES:
        rt.bot.send_message(
            chat_id,
            "❌ Файл бэкапа слишком большой (лимит 20 МБ).",
            reply_markup=_files_back_kb(),
            parse_mode="HTML",
        )
        return False, None

    _clear_tg_state(chat_id, user_id)

    try:
        file_info = rt.bot.get_file(document.file_id)
        downloaded_file = rt.bot.download_file(file_info.file_path)
        data = json.loads(downloaded_file.decode("utf-8"))
        return True, data
    except json.JSONDecodeError as e:
        rt.bot.send_message(
            chat_id,
            f"❌ Ошибка: не удалось считать {h(context_label)}. Проверьте JSON. ({h(e)})",
            reply_markup=_files_back_kb(),
            parse_mode="HTML",
        )
        rt.logger.error(f"Ошибка декодирования JSON ({context_label}): {e}")
        return False, None
    except Exception as e:
        rt.bot.send_message(
            chat_id,
            f"❌ Произошла ошибка при загрузке {h(context_label)}: {h(e)}",
            reply_markup=_files_back_kb(),
            parse_mode="HTML",
        )
        rt.logger.error(f"Ошибка загрузки документа ({context_label}): {e}")
        return False, None


def handle_backup_restore_upload(message: types.Message) -> None:
    """Process uploaded AutoSMM backup JSON document for authorized TG state sessions."""
    ok, data = _receive_tg_json_document(message, context_label="файла бэкапа")
    if not ok:
        return

    try:
        _ok, result_text = _restore_backup_wrapper(data)
        rt.bot.send_message(message.chat.id, h(result_text), reply_markup=_files_back_kb(), parse_mode="HTML")
    except Exception as e:
        rt.bot.send_message(
            message.chat.id,
            f"❌ Произошла ошибка при восстановлении бэкапа: {h(e)}",
            reply_markup=_files_back_kb(),
            parse_mode="HTML",
        )
        rt.logger.error(f"Неизвестная ошибка при восстановлении бэкапа: {e}")


register_navigation_target("files_menu", files_menu)
