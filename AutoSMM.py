from __future__ import annotations
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
if TYPE_CHECKING:
    from cardinal import Cardinal

from urllib.parse import quote, urlparse
from types import SimpleNamespace
from pathlib import Path
import re
import os
import json
import logging
import threading
import requests
import shutil
import time
import io
import sys
import subprocess
import html
import hashlib
import unicodedata

from datetime import datetime, timedelta

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI import enums

from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# =========================================================
# META / CONSTANTS / GLOBALS
# =========================================================

NAME = "AutoSMM"
VERSION = "0.0.9"
DESCRIPTION = "AutoSMM v0.0.9 Final: polished buyer templates, safer order reference commands, clean storage paths, telemetry gate, and buyer-flow safety."
CREDITS = "@w1bbex"
UUID = "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8"
SETTINGS_PAGE = False

LOGGER_PREFIX = "[AUTO autosmm]"
logger = logging.getLogger("FPC.autosmm")

ZERO_WIDTH_INVISIBLE_CHARS = {
    ord("\u200b"): None,
    ord("\u200c"): None,
    ord("\u200d"): None,
    ord("\u2060"): None,
    ord("\ufeff"): None,
    ord("\u180e"): None,
    ord("\u200e"): None,
    ord("\u200f"): None,
    ord("\u202a"): None,
    ord("\u202b"): None,
    ord("\u202c"): None,
    ord("\u202d"): None,
    ord("\u202e"): None,
}

def normalize_incoming_text(text: Any) -> str:
    value = str(text or "")
    value = value.translate(ZERO_WIDTH_INVISIBLE_CHARS)
    return value.strip()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def clean_tg_text(text: Any) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value

waiting_for_lots_upload = set()
waiting_for_backup_restore_upload = set()
LOT_PAGE_CONTEXT: Dict[int, int] = {}
LOT_INPUT_STATES: Dict[int, Dict[str, Any]] = {}
LOT_CARD_CONTEXT: Dict[int, Dict[str, Any]] = {}
UI_INPUT_STATES: Dict[int, Dict[str, Any]] = {}
SEARCH_RESULT_CONTEXT: Dict[int, Dict[str, Any]] = {}
ANALYTICS_VIEW_MODES: Dict[Tuple[int, int], str] = {}
DELETE_CONFIRM_GRACE_UNTIL: Dict[int, float] = {}

PLUGIN_FILE_PATH = Path(__file__).resolve()
PLUGIN_DIR = PLUGIN_FILE_PATH.parent
CARDINAL_ROOT = PLUGIN_DIR.parent if PLUGIN_DIR.name.lower() == "plugins" else Path.cwd()

BASE_STORAGE_DIR = CARDINAL_ROOT / "storage" / "w1bbex" / "AutoSMM"
AUTOSMM_DATA_DIR = str(BASE_STORAGE_DIR)
CONFIG_PATH = str(BASE_STORAGE_DIR / "auto_lots.json")
VALID_LINKS_PATH = str(BASE_STORAGE_DIR / "valid_link.json")
ORDERS_PATH = str(BASE_STORAGE_DIR / "auto_smm_orders.json")
ORDERS_DATA_PATH = str(BASE_STORAGE_DIR / "orders_data.json")
ORDERS_RUNTIME_PATH = str(BASE_STORAGE_DIR / "orders_runtime.json")
AUTOSMM_STATE_PATH = str(BASE_STORAGE_DIR / "auto_smm_state.json")
LOGS_DIR = str(BASE_STORAGE_DIR / "logs")
BACKUPS_DIR = str(BASE_STORAGE_DIR / "backups")
LOG_DIR = LOGS_DIR
AUTOSMM_BACKUPS_DIR = BACKUPS_DIR
LOG_PATH = str(Path(LOGS_DIR) / "auto_smm.log")

LEGACY_CACHE_DIR = CARDINAL_ROOT / "storage" / "cache"
LEGACY_LOGS_DIR = CARDINAL_ROOT / "storage" / "logs"
LEGACY_CONFIG_PATH = str(LEGACY_CACHE_DIR / "auto_lots.json")
LEGACY_ORDERS_PATH = str(LEGACY_CACHE_DIR / "auto_smm_orders.json")
LEGACY_ORDERS_DATA_PATH = str(LEGACY_CACHE_DIR / "orders_data.json")
LEGACY_ORDERS_RUNTIME_PATH = str(LEGACY_CACHE_DIR / "orders_runtime.json")
LEGACY_VALID_LINKS_PATH = str(LEGACY_CACHE_DIR / "valid_link.json")
LEGACY_AUTOSMM_STATE_PATH = str(LEGACY_CACHE_DIR / "auto_smm_state.json")
LEGACY_AUTOSMM_BACKUPS_DIR = str(LEGACY_CACHE_DIR / "autosmm_backups")
LEGACY_LOG_PATH = str(LEGACY_LOGS_DIR / "auto_smm.log")

NESTED_STORAGE_FILE_MAP = {
    CONFIG_PATH: str(BASE_STORAGE_DIR / "config" / "auto_lots.json"),
    VALID_LINKS_PATH: str(BASE_STORAGE_DIR / "config" / "valid_link.json"),
    ORDERS_PATH: str(BASE_STORAGE_DIR / "orders" / "auto_smm_orders.json"),
    ORDERS_DATA_PATH: str(BASE_STORAGE_DIR / "orders" / "orders_data.json"),
    ORDERS_RUNTIME_PATH: str(BASE_STORAGE_DIR / "runtime" / "orders_runtime.json"),
    AUTOSMM_STATE_PATH: str(BASE_STORAGE_DIR / "runtime" / "auto_smm_state.json"),
}

AUTOSMM_LEGACY_FILE_MAP = {
    CONFIG_PATH: LEGACY_CONFIG_PATH,
    VALID_LINKS_PATH: LEGACY_VALID_LINKS_PATH,
    ORDERS_PATH: LEGACY_ORDERS_PATH,
    ORDERS_DATA_PATH: LEGACY_ORDERS_DATA_PATH,
    ORDERS_RUNTIME_PATH: LEGACY_ORDERS_RUNTIME_PATH,
    AUTOSMM_STATE_PATH: LEGACY_AUTOSMM_STATE_PATH,
}

for _autosmm_dir in (AUTOSMM_DATA_DIR, AUTOSMM_BACKUPS_DIR, LOG_DIR):
    os.makedirs(_autosmm_dir, exist_ok=True)

EXPECTED_API_KEY_LENGTH = 60  # стандарт SocPanel v2; AutoSMM рассчитан на панели SocPanel v2


def _path_has_content(path_value: str) -> bool:
    try:
        return os.path.exists(path_value) and os.path.getsize(path_value) > 0
    except Exception:
        return os.path.exists(path_value)


def _copy_storage_file_if_needed(new_path: str, old_path: str, copied: List[str], source_label: str) -> None:
    try:
        if _path_has_content(new_path) or not os.path.exists(old_path):
            return
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        shutil.copy2(old_path, new_path)
        copied.append(f"{os.path.basename(old_path)} -> {os.path.basename(new_path)} ({source_label})")
    except Exception as ex:
        logger.warning(f"AUTO SMM STORAGE MIGRATION: не удалось скопировать {old_path} -> {new_path}: {ex}")


def _migrate_nested_storage_layout_once() -> None:
    """Copy files from the failed config/orders/runtime layout into the final flat layout."""
    copied: List[str] = []
    for new_path, old_path in NESTED_STORAGE_FILE_MAP.items():
        _copy_storage_file_if_needed(new_path, old_path, copied, "nested")
    if copied:
        logger.info(f"AUTO SMM STORAGE MIGRATION: nested -> flat: {', '.join(copied)}")


def _migrate_autosmm_files_from_legacy_if_needed():
    """Copy old AutoSMM files into final storage/w1bbex/AutoSMM flat layout once.

    This is intentionally copy-only: old files are not removed automatically.
    """
    try:
        os.makedirs(AUTOSMM_DATA_DIR, exist_ok=True)
        os.makedirs(AUTOSMM_BACKUPS_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        _migrate_nested_storage_layout_once()
        copied: List[str] = []
        for new_path, legacy_path in AUTOSMM_LEGACY_FILE_MAP.items():
            _copy_storage_file_if_needed(new_path, legacy_path, copied, "storage/cache")
        if copied:
            state = {}
            try:
                if os.path.exists(AUTOSMM_STATE_PATH):
                    with open(AUTOSMM_STATE_PATH, "r", encoding="utf-8") as f:
                        loaded_state = json.load(f)
                        if isinstance(loaded_state, dict):
                            state = loaded_state
            except Exception:
                state = {}
            state["legacy_files_migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["legacy_files_migrated_from"] = "storage/cache"
            try:
                with open(AUTOSMM_STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=4)
            except Exception as ex:
                logger.warning(f"AUTO SMM LEGACY MIGRATION: не удалось записать состояние миграции: {ex}")
            logger.info(f"AUTO SMM LEGACY MIGRATION: скопированы файлы: {', '.join(copied)}")
    except Exception as ex:
        logger.warning(f"AUTO SMM LEGACY MIGRATION FAILED: {ex}")

_migrate_autosmm_files_from_legacy_if_needed()

logger.setLevel(logging.INFO)
if not any(isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == os.path.abspath(LOG_PATH) for handler in logger.handlers):
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.ERROR)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

RUNNING = False
IS_STARTED = False
ORDER_CHECK_THREAD = None
AUTO_LOTS_SEND_THREAD = None

orders_info = {}
processed_users = {}
waiting_for_link: Dict[str, Dict] = {}
BUYER_ORDER_SELECTION: Dict[str, str] = {}

BUYER_STATE_WAITING_LINK = "waiting_link"
BUYER_STATE_LINK_PAUSED = "link_paused"
BUYER_STATE_WAITING_CONFIRM = "waiting_confirm"
BUYER_STATE_PANEL_CREATED = "panel_created"
BUYER_STATE_COMPLETED = "completed"
BUYER_STATE_FAILED = "failed"
BUYER_STATE_REFUNDED = "refunded"
BUYER_ACTIVE_PRE_PANEL_STATES = {
    BUYER_STATE_WAITING_LINK,
    BUYER_STATE_LINK_PAUSED,
    BUYER_STATE_WAITING_CONFIRM,
}
BUYER_REFUND_BLOCK_TEXT = "По этому заказу оформлен возврат.\nАвтоматическое выполнение недоступно."
BUYER_PANEL_CREATED_BLOCK_TEXT = "Заказ уже передан в работу.\nИзменить ссылку автоматически нельзя."

bot = None
config = {}
lot_mapping = {}
cardinal_instance = None

DEFAULT_VALID_LINKS = [
    "vk.com",
    "m.vk.com",
    "t.me",
    "telegram.me",
    "instagram.com",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "youtube.com",
    "youtu.be",
    "twitch.tv",
    "kick.com",
    "twitter.com",
    "x.com",
    "threads.net",
    "facebook.com",
    "fb.com",
    "ok.ru",
    "rutube.ru",
    "dzen.ru",
    "discord.gg",
    "discord.com",
]
LEGACY_DEFAULT_VALID_LINKS = [
    "vk.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "t.me",
    "twitter.com",
]
VALID_LINKS_MIGRATION_STATE_KEY = "valid_links_defaults_migrated"
# AutoSMM telemetry endpoint. Payload contains only plugin_name, version and anonymous_install_id.
# anonymous_install_id is SHA-256 hash derived locally from FunPay username + FunPay ID.
# Raw FunPay username/FunPay ID are never sent.
TELEMETRY_URL = "https://api.autooffline.ru/v1/autosmm/telemetry"
TELEMETRY_CONSENT_KEY = "telemetry_consent"
TELEMETRY_IDENTITY_HASH_KEY = "telemetry_identity_hash"
TELEMETRY_IDENTITY_SOURCE_KEY = "telemetry_identity_source"
TELEMETRY_IDENTITY_DEBUG_KEY = "telemetry_identity_debug"
TELEMETRY_LAST_PING_ATTEMPT_KEY = "telemetry_last_ping_attempt_at"
TELEMETRY_LAST_PING_OK_KEY = "telemetry_last_ping_ok_at"
TELEMETRY_LAST_PING_ERROR_KEY = "telemetry_last_ping_error"
TELEMETRY_PING_INTERVAL_SECONDS = 86400
TELEMETRY_TIMEOUT_SECONDS = 5
TELEMETRY_SECURITY_URL = "https://teletype.in/@w1bbex/AutoSMM-Security"
AUTOSMM_V008_NO_TELEMETRY_URL = "https://t.me/w1bbexplugins/25"
TELEMETRY_GUEST_USED_KEY = "telemetry_guest_used"
TELEMETRY_GUEST_STARTED_AT_KEY = "telemetry_guest_started_at"
TELEMETRY_GUEST_UNTIL_KEY = "telemetry_guest_until"

AUTOSMM_BACKUP_VERSION = 1
AUTOSMM_BACKUP_FILE_MAP = {
    "auto_lots.json": CONFIG_PATH,
    "auto_smm_orders.json": ORDERS_PATH,
    "orders_data.json": ORDERS_DATA_PATH,
    "orders_runtime.json": ORDERS_RUNTIME_PATH,
    "valid_link.json": VALID_LINKS_PATH,
    "auto_smm_state.json": AUTOSMM_STATE_PATH,
}
AUTOSMM_BACKUP_DEFAULTS = {
    "auto_lots.json": {},
    "auto_smm_orders.json": [],
    "orders_data.json": [],
    "orders_runtime.json": {},
    "valid_link.json": [],
    "auto_smm_state.json": {},
}

DEFAULT_AFTER_PAYMENT_TEMPLATE = """👾 Благодарю за оплату!

🧾 Заказ: #{order_id_funpay}
📦 Лот: {lot_title}

Чтобы запустить выполнение, отправьте ссылку на страницу, пост или профиль.

Ссылка должна начинаться с:
https://

Если пока не готовы отправить ссылку:
!ссылка

Ваши заказы:
!смм"""

DEFAULT_AFTER_CONFIRMATION_TEMPLATE = """🎉 Заказ запущен!

🧾 Заказ FunPay: #{order_id_funpay}
🔢 ID заказа в сервисе: {panel_order_id}
📦 Лот: {lot_title}

🔗 Ссылка:
{link}

Проверить статус:
чек {panel_order_id}

Ваши заказы:
!смм"""

DEFAULT_LINK_CONFIRM_TEMPLATE = """✅ Ссылка получена

🧾 Заказ: #{order_id_funpay}
📦 Лот: {lot_title}

🔗 Ссылка:
{link}

Подтвердите запуск:

+ — запустить
- — изменить ссылку"""

DEFAULT_AFTER_COMPLETED_TEMPLATE = """✅ Заказ выполнен!

🧾 Заказ FunPay: #{order_id_funpay}
🔢 ID заказа в сервисе: {panel_order_id}
📦 Лот: {lot_title}

Пожалуйста, подтвердите выполнение заказа:
https://funpay.com/orders/{order_id_funpay}/

Спасибо за покупку!"""

DEFAULT_BUYER_COMMANDS_TEMPLATE = """📋 Команды AutoSMM

!смм — ваши SMM-заказы
!ссылка — приостановить или продолжить ожидание ссылки
!ссылка НОМЕР — выбрать конкретный заказ

!отмена — запросить отмену заказа, если продавец разрешил автоотмену

чек НОМЕР — проверить статус заказа
рефилл НОМЕР — запросить рефилл, если услуга поддерживает гарантию

НОМЕР — это ID заказа в сервисе или номер заказа FunPay."""

LEGACY_AFTER_CONFIRMATION_DEFAULTS = {
    """🎉 Ваш заказ успешно оформлен!

🔢 ID заказа в сервисе: {twiboost_id}
🔗 Для отслеживания переходите по ссылке: {link}

📋 Доступные команды:
🔍 чек {twiboost_id} — Проверить статус заказа
🔄 рефилл {twiboost_id} — Запросить рефилл

Если у вас возникнут вопросы, не стесняйтесь обращаться!""",
    """🎉 Ваш заказ успешно оформлен!

🔢 ID заказа в сервисе: {twiboost_id}
🔗 Для отслеживания переходите по ссылке: {link}

📋 Доступные команды:
🔍 чек {twiboost_id} — Проверить статус заказа (выводит информацию о заказе)
🔄 рефилл {twiboost_id} — Запросить рефилл (работает лишь с гарантией - восстанавливает отписанных подписчиков)

Если у вас возникнут вопросы, не стесняйтесь обращаться!""",
    """🎉 Ваш заказ успешно оформлен!

Лот: {lot_title}
🔢 ID заказа в сервисе: {panel_order_id}
🔗 Ссылка: {link}

📋 Доступные команды:
🔍 чек {panel_order_id} — проверить статус заказа
🔄 рефилл {panel_order_id} — запросить рефилл, если услуга поддерживает гарантию
📦 !смм — список заказов
❓ !команды — помощь

Если у вас возникнут вопросы, не стесняйтесь обращаться!""",

}

LEGACY_AFTER_PAYMENT_DEFAULTS = {
    """❤️ Благодарим за оплату!

Чтобы начать накрутку, отправьте корректную ссылку на вашу страницу или пост в социальных сетях. Ссылка должна начинаться с "https://", например:

Пример: https://teletype.in/@w1bbex/wwTwed1ZBZE

Без правильной ссылки накрутка не будет запущена. Убедитесь, что она ведет на активную страницу, доступную для общего просмотра.""",
    """👾 Благодарю за оплату!

Чтобы начать накрутку, отправьте корректную ссылку на вашу страницу или пост в социальных сетях. Ссылка должна начинаться с "https://", например:

Пример: https://telegra.ph/Primery-ssylok-11-11

Без правильной ссылки накрутка не будет запущена. Убедитесь, что она ведет на активную страницу, доступную для общего просмотра.""",
    """👾 Благодарю за оплату!

Чтобы начать накрутку, отправьте корректную ссылку на вашу страницу или пост в социальных сетях. Ссылка должна начинаться с "https://", например:

Пример: https://telegra.ph/Primery-ssylok-11-11

Без правильной ссылки накрутка не будет запущена.

Команды:
!команды — помощь
!смм — ваши SMM-заказы""",

}

LEGACY_LINK_CONFIRM_DEFAULTS = {
    """✅ Ссылка получена:
{link}

Напишите + чтобы подтвердить, - чтобы изменить ссылку.""",
    """✅ Ссылка получена

Заказ: #{order_id_funpay}
Лот: {lot_title}
Ссылка: {link}

Подтвердите действие:
+ ━ да
- ━ нет""",
}

LEGACY_AFTER_COMPLETED_DEFAULTS = {
    """👾 Ваш заказ успешно завершён!

Лот: {lot_title}
🔢 Номер заказа: {panel_order_id}
🔗 Подтвердите заказ:
https://funpay.com/orders/{order_id_funpay}/

Спасибо за покупку!""",
    """✅ Ваш заказ завершён!

Лот: {lot_title}
🔢 ID заказа в сервисе: {panel_order_id}
📊 Выполнено: {done_quantity}/{total_quantity}

Если результат вас устраивает, пожалуйста, подтвердите выполнение заказа на FunPay.

📦 !смм — ваши SMM-заказы
❓ !команды — помощь""",
}

LEGACY_BUYER_COMMANDS_DEFAULTS = {
    """📋 Команды AutoSMM

!смм — показать ваши SMM-заказы
!ссылка — поставить ожидание ссылки на паузу или продолжить
!ссылка НОМЕР — выбрать конкретный заказ
!отмена — отменить заказ до запуска, если продавец разрешил
чек НОМЕР — проверить статус заказа
рефилл НОМЕР — запросить рефилл, если услуга поддерживает гарантию

Ссылку можно отправить обычным сообщением.
Ссылка должна начинаться с https://.""",
}

INVALID_LINK_SCHEME_TEXT = """❌ Ссылка должна начинаться с https://."""

INVALID_LINK_DOMAIN_TEXT = """❌ Ссылка не принята.

Домен этой ссылки не разрешён продавцом.

Проверьте ссылку и отправьте её ещё раз.
Если ссылка верная — напишите продавцу."""

CONFIRM_REMINDER_TEXT = """⏳ Ссылка ожидает подтверждения.

+ — запустить заказ
- — изменить ссылку

Если пока не готовы подтвердить:
!ссылка"""

ORDER_NOT_FOUND_TEXT = """Заказ не найден или недоступен.

Проверьте номер заказа или напишите:
!смм

Там будут ваши последние SMM-заказы."""

BUYER_CONFIRM_REMINDER_SECONDS = 600
BUYER_LINK_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
BUYER_CONFIRM_YES_WORDS = {"+", "да", "подтвердить", "подтверждаю", "ок", "окей", "го", "давай", "запускай", "верно", "правильно", "всё верно", "все верно"}
BUYER_CONFIRM_NO_WORDS = {"-", "нет", "изменить", "сменить", "другая ссылка"}
BUYER_AUTO_CANCEL_UNAVAILABLE_TEXT = "Автоматическая отмена недоступна.\nЕсли хотите отменить заказ, напишите продавцу."
BUYER_AUTO_CANCEL_FAILED_TEXT = "Автоматическая отмена недоступна.\nПожалуйста, напишите продавцу."
BUYER_ADD_TRANSPORT_MANUAL_TEXT = """Автоматическая обработка временно остановлена.

Ваш заказ не потерян.
Продавец уведомлён и проверит его вручную.

Пожалуйста, не отправляйте ссылку повторно, чтобы не создать дубль."""
BUYER_LOT_MATCH_MANUAL_TEXT = """Автоматическая обработка временно остановлена.

Ваш заказ не потерян.
Продавец уведомлён и проверит его вручную."""

ACTIVE_PLUGIN_FILENAME = PLUGIN_FILE_PATH.name
OLD_FILES_REMIND_CFG_KEY = "old_files_cleanup_remind_until"
FULL_AUTOSMM_FILE_RE = re.compile(r"(?i)^AutoSMM(?:_(\d+(?:\.\d+)*))?(?:\s*\(\d+\))?\.py(?:c|o)?$")
HELPER_AUTOSMM_FILE_RE = re.compile(r"(?i)^_autosmm_.*\.(?:py|pyc|pyo)$")

REFUND_SYNC_PATTERNS = [
    re.compile(r"вернул[аи]?\s+деньги.*?по\s+заказу\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"возврат.*?по\s+заказу\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"returned\s+money.*?order\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
]
FUNPAY_STATUS_ALLOWED = {"open", "closed", "refunded", "partially_refunded"}
FUNPAY_STATUS_SYNC_INTERVAL_SECONDS = 1800
FUNPAY_STATUS_RECENT_CHECK_SECONDS = 1800
FUNPAY_STATUS_RECOVERY_CLOSED_SECONDS = 43200
FUNPAY_STATUS_RECOVERY_MAX_ORDERS = 120
PANEL_BALANCE_CACHE_TTL_SECONDS = 120
FUNPAY_LIVE_STATUS_PATTERNS = [
    (
        "partially_refunded",
        [
            re.compile(r"часть\s+средств.*?по\s+заказу\s+#?([A-Z0-9]+).*?возвращен[аоы]?\s+покупателю", re.IGNORECASE | re.DOTALL),
            re.compile(r"partial\s+refund.*?order\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
        ],
    ),
    (
        "refunded",
        [
            re.compile(r"вернул[аи]?\s+деньги.*?по\s+заказу\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
            re.compile(r"возврат.*?по\s+заказу\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
            re.compile(r"returned\s+money.*?order\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
        ],
    ),
    (
        "closed",
        [
            re.compile(r"покупатель.*?подтвердил[аи]?\s+успешное\s+выполнение\s+заказа\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
            re.compile(r"администратор.*?подтвердил[аи]?\s+успешное\s+выполнение\s+заказа\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
            re.compile(r"buyer.*?confirmed.*?order\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
            re.compile(r"administrator.*?confirmed.*?order\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
        ],
    ),
]
STATUS_BATCH_SIZE = 50
STATUS_BATCH_INTERVAL_SECONDS = 300
INCORRECT_ORDER_ID_MAX_ATTEMPTS = 3
_PANEL_BALANCE_CACHE = {"timestamp": 0.0, "text": "недоступно"}

# =========================================================
# STORAGE
# =========================================================

RUNTIME_FIELDS = {
    "status_last_checked_at",
    "status_last_error",
    "incorrect_order_id_attempts",
    "status_stop_reason",
    "manual_check_alert_sent",
    "funpay_status_checked_at",
    "refund_detected_at",
    "refund_source",
    "panel_refund_cancel_attempted_at",
    "panel_refund_cancel_result",
    "buyer_flow_state",
    "saved_link",
    "panel_order_id",
    "panel_status",
    "total_quantity",
    "done_quantity",
    "left_quantity",
    "progress_percent",
    "last_prompt_ts",
    "updated_at",
}

LEGACY_COMPLETED_NOTIFICATION_RUNTIME_FIELD = "completed_notification_sent"

def _normalize_allowed_domain(domain_value: Any) -> str:
    raw_value = str(domain_value or "").strip().lower()
    if not raw_value:
        return ""
    raw_value = raw_value.replace("\\", "/")
    if "://" in raw_value:
        parsed = urlparse(raw_value)
        raw_value = parsed.hostname or ""
    else:
        raw_value = raw_value.split("/", 1)[0]
        raw_value = raw_value.split("?", 1)[0]
        raw_value = raw_value.split("#", 1)[0]
    raw_value = raw_value.split(":", 1)[0].strip().strip(".")
    if raw_value.startswith("www."):
        raw_value = raw_value[4:]
    return raw_value


def _is_plausible_allowed_domain(domain_value: Any) -> bool:
    domain = str(domain_value or "").strip().lower()
    if not domain or " " in domain or ".." in domain:
        return False
    if len(domain) > 253 or "." not in domain:
        return False
    labels = domain.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not re.fullmatch(r"[a-z0-9-]+", label):
            return False
    return True


def _normalize_valid_links_list(links: Any, include_defaults: bool = False) -> List[str]:
    normalized: List[str] = []
    seen = set()

    def add_domain(domain_value: Any):
        domain = _normalize_allowed_domain(domain_value)
        if not domain or domain in seen or not _is_plausible_allowed_domain(domain):
            return
        seen.add(domain)
        normalized.append(domain)

    if isinstance(links, list):
        for item in links:
            add_domain(item)
    elif isinstance(links, str):
        add_domain(links)

    if include_defaults:
        for item in DEFAULT_VALID_LINKS:
            add_domain(item)

    return normalized


def _load_autosmm_state() -> Dict:
    state = _safe_load_json(AUTOSMM_STATE_PATH, {})
    return state if isinstance(state, dict) else {}


def _set_valid_links_defaults_migrated() -> None:
    state = _load_autosmm_state()
    if state.get(VALID_LINKS_MIGRATION_STATE_KEY) is True:
        return
    state[VALID_LINKS_MIGRATION_STATE_KEY] = True
    _safe_write_json(AUTOSMM_STATE_PATH, state)


def _valid_links_defaults_migrated() -> bool:
    return bool(_load_autosmm_state().get(VALID_LINKS_MIGRATION_STATE_KEY, False))


def _looks_like_legacy_default_whitelist(links: List[str]) -> bool:
    if not links:
        return False
    normalized = set(_normalize_valid_links_list(links, include_defaults=False))
    legacy = set(_normalize_valid_links_list(LEGACY_DEFAULT_VALID_LINKS, include_defaults=False))
    defaults = set(_normalize_valid_links_list(DEFAULT_VALID_LINKS, include_defaults=False))
    if normalized == legacy:
        return True
    # Old installs sometimes had the narrow default list plus a couple of manual domains.
    # Do not treat an edited modern default list as legacy, otherwise removed defaults would return.
    if legacy.issubset(normalized) and not defaults.issubset(normalized) and len(normalized - legacy) <= 5:
        return True
    return False


def load_valid_links() -> List[str]:
    """Load trusted domains without forcing deleted defaults back into the user's list."""
    if not os.path.exists(VALID_LINKS_PATH):
        links = _normalize_valid_links_list(DEFAULT_VALID_LINKS, include_defaults=False)
        save_valid_links(links)
        _set_valid_links_defaults_migrated()
        return links

    raw_links = _safe_load_json(VALID_LINKS_PATH, [])
    links = _normalize_valid_links_list(raw_links, include_defaults=False)
    raw_needs_normalization = not isinstance(raw_links, list) or raw_links != links

    if not _valid_links_defaults_migrated():
        if _looks_like_legacy_default_whitelist(links):
            links = _normalize_valid_links_list(links + DEFAULT_VALID_LINKS, include_defaults=False)
            save_valid_links(links)
        else:
            # Existing custom/empty lists are treated as the user's choice.
            if raw_needs_normalization:
                save_valid_links(links)
        _set_valid_links_defaults_migrated()
        return links

    if raw_needs_normalization:
        save_valid_links(links)
    return links


def save_valid_links(links: List[str]):
    os.makedirs(os.path.dirname(VALID_LINKS_PATH), exist_ok=True)
    normalized_links = _normalize_valid_links_list(links, include_defaults=False)
    with open(VALID_LINKS_PATH, 'w', encoding='utf-8') as f:
        json.dump(normalized_links, f, ensure_ascii=False, indent=4)


def reset_valid_links_to_defaults() -> List[str]:
    links = _normalize_valid_links_list(DEFAULT_VALID_LINKS, include_defaults=False)
    save_valid_links(links)
    _set_valid_links_defaults_migrated()
    return links


def _normalize_template_for_compare(text_value: Any) -> str:
    return re.sub(r"\s+", " ", str(text_value or "").strip())


def _ensure_config_defaults(cfg: Dict) -> bool:
    changed = False
    if not isinstance(cfg, dict):
        return False

    def ensure_key(key: str, default_value: Any):
        nonlocal changed
        if key not in cfg:
            cfg[key] = default_value
            changed = True

    ensure_key("services", {
        "1": {
            "api_url": "https://smmway.ru/api/v2",
            "api_key": "YOUR_API_KEY",
        }
    })
    ensure_key("auto_refunds", False)
    ensure_key("fallback_enabled", False)
    # TODO AutoSMM v0.0.10: buyer_cancel_max_amount; buyer_cancel_lot_policy; per-lot buyer cancel override; allowlist/denylist лотов для !отмена.
    ensure_key("allow_buyer_cancel_before_panel_order", False)
    ensure_key("try_panel_cancel_after_funpay_refund", False)
    ensure_key("confirm_link", True)
    ensure_key("notification_chat_id", None)
    ensure_key("send_auto_lots", False)
    ensure_key("send_auto_lots_interval", 30)
    ensure_key("lot_mapping", {})
    ensure_key("new_order_notifications", False)

    if not isinstance(cfg.get("services"), dict):
        cfg["services"] = {}
        changed = True
    if not isinstance(cfg.get("lot_mapping"), dict):
        cfg["lot_mapping"] = {}
        changed = True

    messages = cfg.get("messages")
    if not isinstance(messages, dict):
        messages = {}
        cfg["messages"] = messages
        changed = True

    current_after_payment = messages.get("after_payment")
    legacy_norm = {_normalize_template_for_compare(item) for item in LEGACY_AFTER_PAYMENT_DEFAULTS}
    if not current_after_payment or _normalize_template_for_compare(current_after_payment) in legacy_norm:
        if messages.get("after_payment") != DEFAULT_AFTER_PAYMENT_TEMPLATE:
            messages["after_payment"] = DEFAULT_AFTER_PAYMENT_TEMPLATE
            changed = True

    current_after_confirmation = messages.get("after_confirmation")
    legacy_confirmation_norm = {_normalize_template_for_compare(item) for item in LEGACY_AFTER_CONFIRMATION_DEFAULTS}
    if not current_after_confirmation or _normalize_template_for_compare(current_after_confirmation) in legacy_confirmation_norm:
        if messages.get("after_confirmation") != DEFAULT_AFTER_CONFIRMATION_TEMPLATE:
            messages["after_confirmation"] = DEFAULT_AFTER_CONFIRMATION_TEMPLATE
            changed = True

    current_link_confirm = messages.get("buyer_link_received_confirm")
    legacy_link_norm = {_normalize_template_for_compare(item) for item in LEGACY_LINK_CONFIRM_DEFAULTS}
    if not current_link_confirm or _normalize_template_for_compare(current_link_confirm) in legacy_link_norm:
        if messages.get("buyer_link_received_confirm") != DEFAULT_LINK_CONFIRM_TEMPLATE:
            messages["buyer_link_received_confirm"] = DEFAULT_LINK_CONFIRM_TEMPLATE
            changed = True

    current_after_completed = messages.get("after_completed")
    legacy_completed_norm = {_normalize_template_for_compare(item) for item in LEGACY_AFTER_COMPLETED_DEFAULTS}
    if not current_after_completed or _normalize_template_for_compare(current_after_completed) in legacy_completed_norm:
        if messages.get("after_completed") != DEFAULT_AFTER_COMPLETED_TEMPLATE:
            messages["after_completed"] = DEFAULT_AFTER_COMPLETED_TEMPLATE
            changed = True

    current_buyer_commands = messages.get("buyer_commands")
    legacy_commands_norm = {_normalize_template_for_compare(item) for item in LEGACY_BUYER_COMMANDS_DEFAULTS}
    if not current_buyer_commands or _normalize_template_for_compare(current_buyer_commands) in legacy_commands_norm:
        if messages.get("buyer_commands") != DEFAULT_BUYER_COMMANDS_TEMPLATE:
            messages["buyer_commands"] = DEFAULT_BUYER_COMMANDS_TEMPLATE
            changed = True

    return changed


# Backward-compatible alias for older internal calls.
def _ensure_microfix_config_defaults(cfg: Dict) -> bool:
    return _ensure_config_defaults(cfg)


def load_config() -> Dict:
    logger.debug("Загрузка конфигурации (файл настроек)...")
    
    file_lock = threading.Lock()
    
    try:
        with file_lock:
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        
                    if not file_content.strip():
                        logger.error(f"Файл конфигурации {CONFIG_PATH} пуст. Создаем новый файл конфигурации.")
                        cfg = create_default_config()
                        save_config(cfg)
                        return cfg
                        
                    try:
                        cfg = json.loads(file_content)
                    except json.JSONDecodeError as e:
                        logger.error(f"Ошибка при чтении JSON: {e}. Создаем новый файл конфигурации.")
                        cfg = create_default_config()
                        save_config(cfg)
                        return cfg
                    
                    if _ensure_config_defaults(cfg):
                        save_config(cfg)
                    return cfg
                except Exception as e:
                    logger.error(f"Ошибка при чтении файла конфигурации: {e}. Создаем новый файл конфигурации.")
                    cfg = create_default_config()
                    save_config(cfg)
                    return cfg
            else:
                logger.debug(f"Файл конфигурации {CONFIG_PATH} не найден. Создаем новый файл.")
                cfg = create_default_config()
                save_config(cfg)
                return cfg
    except Exception as e:
        logger.error(f"Общая ошибка при загрузке конфигурации: {e}. Возвращаем конфигурацию по умолчанию.")
        cfg = create_default_config()
        try:
            save_config(cfg)
        except:
            pass
        return cfg


def create_default_config() -> Dict:
    """Создает конфигурацию по умолчанию"""
    return {
        "services": {
            "1": {
                "api_url": "https://smmway.ru/api/v2",
                "api_key": "YOUR_API_KEY"
            }
        },
        "auto_refunds": False,
        "fallback_enabled": False,
        "allow_buyer_cancel_before_panel_order": False,
        "try_panel_cancel_after_funpay_refund": False,
        "confirm_link": True,
        "messages": {
            "after_payment": DEFAULT_AFTER_PAYMENT_TEMPLATE,
            "buyer_link_received_confirm": DEFAULT_LINK_CONFIRM_TEMPLATE,
            "after_confirmation": DEFAULT_AFTER_CONFIRMATION_TEMPLATE,
            "after_completed": DEFAULT_AFTER_COMPLETED_TEMPLATE,
            "buyer_commands": DEFAULT_BUYER_COMMANDS_TEMPLATE
        },
        "notification_chat_id": None,
        "send_auto_lots": False,
        "send_auto_lots_interval": 30,
        "lot_mapping": {},
        "new_order_notifications": False
    }


def save_config(cfg: Dict):
    logger.debug("Сохранение конфигурации (файл настроек)...")
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
    logger.debug("Конфигурация сохранена.")


def _parse_version_tuple(version_value: str) -> Tuple[int, ...]:
    parts = re.findall(r"\d+", str(version_value or ""))
    return tuple(int(part) for part in parts)


def _extract_autosmm_file_version(file_name: str) -> Optional[Tuple[int, ...]]:
    match = FULL_AUTOSMM_FILE_RE.match(file_name or "")
    if not match:
        return None
    raw_version = match.group(1)
    if not raw_version:
        return tuple()
    return _parse_version_tuple(raw_version)


def find_old_autosmm_files() -> List[Path]:
    current_path = PLUGIN_FILE_PATH.resolve()
    current_version = _parse_version_tuple(VERSION)
    outdated_files: List[Path] = []

    try:
        for item in PLUGIN_DIR.iterdir():
            try:
                if not item.is_file():
                    continue
            except Exception:
                continue

            try:
                resolved_item = item.resolve()
            except Exception:
                resolved_item = item

            if resolved_item == current_path:
                continue

            item_name = item.name

            if HELPER_AUTOSMM_FILE_RE.match(item_name):
                outdated_files.append(item)
                continue

            lower_name = item_name.lower()
            if lower_name in {"autosmm.py", "autosmm_old.py"} or lower_name.startswith("autosmm_old"):
                outdated_files.append(item)
                continue

            candidate_version = _extract_autosmm_file_version(item_name)
            if candidate_version is None:
                continue

            if not candidate_version:
                outdated_files.append(item)
                continue

            if candidate_version < current_version:
                outdated_files.append(item)
                continue

            if candidate_version[:len(current_version)] == current_version and len(candidate_version) > len(current_version):
                outdated_files.append(item)
                continue

            if (
                candidate_version == current_version
                and re.search(r"\(\d+\)", item_name)
                and ACTIVE_PLUGIN_FILENAME.lower() == f"autosmm_{VERSION}.py".lower()
            ):
                outdated_files.append(item)
    except Exception:
        logger.exception("Ошибка при поиске старых файлов AutoSMM")

    return sorted(outdated_files, key=lambda p: p.name.lower())


def _get_old_files_cleanup_remind_until(cfg: Optional[Dict] = None) -> Optional[datetime]:
    cfg = cfg or load_config()
    raw_value = cfg.get(OLD_FILES_REMIND_CFG_KEY)
    if raw_value in (None, "", 0):
        return None

    try:
        if isinstance(raw_value, (int, float)):
            return datetime.fromtimestamp(float(raw_value))
        return datetime.fromisoformat(str(raw_value))
    except Exception:
        logger.error("Некорректное значение cooldown очистки старых файлов AutoSMM: %r", raw_value)
        return None


def is_old_files_cleanup_cooldown_active(cfg: Optional[Dict] = None) -> bool:
    remind_until = _get_old_files_cleanup_remind_until(cfg)
    return bool(remind_until and remind_until > datetime.now())


def set_old_files_cleanup_cooldown(days: int = 1):
    cfg = load_config()
    cfg[OLD_FILES_REMIND_CFG_KEY] = (datetime.now() + timedelta(days=days)).isoformat()
    save_config(cfg)


def clear_old_files_cleanup_cooldown():
    cfg = load_config()
    if cfg.get(OLD_FILES_REMIND_CFG_KEY) is not None:
        cfg[OLD_FILES_REMIND_CFG_KEY] = None
        save_config(cfg)


def reindex_lots(cfg: Dict):
    lot_map = cfg.get("lot_mapping", {})
    sorted_lots = sorted(
        lot_map.items(),
        key=lambda x: int(x[0].split('_')[1]) if x[0].startswith('lot_') and x[0].split('_')[1].isdigit() else 0
    )
    new_lot_map = {}
    for idx, (lot_key, lot_data) in enumerate(sorted_lots, start=1):
        new_key = f"lot_{idx}"
        new_lot_map[new_key] = lot_data
    cfg["lot_mapping"] = new_lot_map
    save_config(cfg)
    logger.info("Лоты были переиндексированы после удаления.")


def save_order_info(order_id: int, order_summa: float, service_name: str, order_chistota: float):
    file_lock = threading.Lock()

    order_record = get_order_data(str(order_id)) or {}
    completed_notification_sent = bool(order_record.get("completed_notification_sent", False))

    data_ = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order_id": order_id,
        "summa": order_summa,
        "service_name": service_name,
        "chistota": order_chistota,
        "completed_notification_sent": completed_notification_sent
    }
    
    try:
        with file_lock:
            if not os.path.exists(ORDERS_PATH):
                os.makedirs(os.path.dirname(ORDERS_PATH), exist_ok=True)
                with open(ORDERS_PATH, 'w', encoding='utf-8') as f:
                    json.dump([], f, indent=4, ensure_ascii=False)
            
            try:
                with open(ORDERS_PATH, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not content.strip():
                        orders = []
                    else:
                        orders = json.loads(content)
            except json.JSONDecodeError as e:
                logger.error(f"Ошибка при чтении {ORDERS_PATH}: {e}. Создаем новый файл.")
                orders = []
            
            orders.append(data_)
            
            temp_path = f"{ORDERS_PATH}.tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(orders, f, indent=4, ensure_ascii=False)
                
                if os.path.exists(temp_path):
                    if os.path.exists(ORDERS_PATH):
                        os.remove(ORDERS_PATH)
                    os.rename(temp_path, ORDERS_PATH)
                    logger.info(f"Данные заказа #{order_id} успешно сохранены")
            except Exception as e:
                logger.error(f"Ошибка при сохранении данных заказа #{order_id}: {e}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except:
                        pass
    except Exception as e:
        logger.error(f"Общая ошибка при сохранении информации о заказе #{order_id}: {e}")


def _safe_load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка при чтении {os.path.basename(path)}: {e}")
        backup_path = f"{path}.bak.{int(time.time())}"
        try:
            shutil.copy2(path, backup_path)
            logger.info(f"Создана резервная копия поврежденного файла: {backup_path}")
        except Exception as ex:
            logger.error(f"Не удалось создать резервную копию {os.path.basename(path)}: {ex}")
        return default
    except Exception as e:
        logger.error(f"Ошибка при чтении {os.path.basename(path)}: {e}")
        return default


def _safe_write_json(path: str, data) -> None:
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        if os.path.exists(path):
            os.remove(path)
        os.rename(temp_path, path)
    except Exception as e:
        logger.error(f"Ошибка при сохранении {os.path.basename(path)}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def _load_orders_data_raw() -> List[Dict]:
    data = _safe_load_json(ORDERS_DATA_PATH, [])
    raw_orders = data if isinstance(data, list) else []
    raw_orders = _migrate_legacy_completed_notification_flags(raw_orders)
    return _migrate_legacy_funpay_statuses(raw_orders)


def _normalize_funpay_status(value: Any) -> Optional[str]:
    raw_value = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "": None,
        "paid": "open",
        "open": "open",
        "opened": "open",
        "closed": "closed",
        "close": "closed",
        "refunded": "refunded",
        "refund": "refunded",
        "partially_refunded": "partially_refunded",
        "partial_refund": "partially_refunded",
        "partial": "partially_refunded",
        "partiallyrefund": "partially_refunded",
    }
    normalized = mapping.get(raw_value, raw_value if raw_value in FUNPAY_STATUS_ALLOWED else None)
    return normalized if normalized in FUNPAY_STATUS_ALLOWED else None


def _is_refunded_funpay_status(funpay_status: Any) -> bool:
    return _normalize_funpay_status(funpay_status) in ("refunded", "partially_refunded")


def _get_effective_funpay_status(order_record: Optional[Dict]) -> Optional[str]:
    if not isinstance(order_record, dict):
        return None
    normalized = _normalize_funpay_status(order_record.get("funpay_status"))
    if normalized:
        return normalized
    if bool(order_record.get("is_refunded", False)):
        return "refunded"
    return None


def _is_order_refundedish(order_record: Optional[Dict]) -> bool:
    return _is_refunded_funpay_status(_get_effective_funpay_status(order_record))


def _migrate_legacy_funpay_statuses(raw_orders: List[Dict]) -> List[Dict]:
    updated = False
    runtime_data = load_orders_runtime()
    if not isinstance(runtime_data, dict):
        runtime_data = {}
    runtime_updated = False

    for order in raw_orders:
        if not isinstance(order, dict):
            continue

        order_id = str(order.get("order_id") or "")
        runtime_entry = dict(runtime_data.get(order_id, {})) if order_id else {}

        current_status = _normalize_funpay_status(order.get("funpay_status"))
        if current_status:
            if order.get("funpay_status") != current_status:
                order["funpay_status"] = current_status
                updated = True
        elif bool(order.get("is_refunded", False)):
            order["funpay_status"] = "refunded"
            current_status = "refunded"
            updated = True

        for field_name in ("refund_detected_at", "refund_source"):
            field_value = order.pop(field_name, None)
            if field_value not in (None, "") and runtime_entry.get(field_name) != field_value:
                runtime_entry[field_name] = field_value
                runtime_updated = True
                updated = True

        if "is_refunded" in order:
            order.pop("is_refunded", None)
            updated = True

        if order_id:
            if runtime_entry:
                runtime_data[order_id] = runtime_entry
            elif order_id in runtime_data:
                runtime_data.pop(order_id, None)
                runtime_updated = True

    if updated:
        _safe_write_json(ORDERS_DATA_PATH, raw_orders)
    if runtime_updated:
        save_orders_runtime(runtime_data)
    return raw_orders


def _migrate_legacy_completed_notification_flags(raw_orders: List[Dict]) -> List[Dict]:
    runtime_data = _safe_load_json(ORDERS_RUNTIME_PATH, {})
    if not isinstance(runtime_data, dict):
        return raw_orders

    legacy_order_ids = set()
    runtime_cleanup_needed = False

    for order_id, runtime_fields in runtime_data.items():
        if not isinstance(runtime_fields, dict):
            continue
        if runtime_fields.get(LEGACY_COMPLETED_NOTIFICATION_RUNTIME_FIELD) is True:
            legacy_order_ids.add(str(order_id))
        if LEGACY_COMPLETED_NOTIFICATION_RUNTIME_FIELD in runtime_fields:
            runtime_fields.pop(LEGACY_COMPLETED_NOTIFICATION_RUNTIME_FIELD, None)
            runtime_cleanup_needed = True

    orders_updated = False
    if legacy_order_ids:
        for order in raw_orders:
            if str(order.get("order_id")) not in legacy_order_ids:
                continue
            if bool(order.get("completed_notification_sent", False)):
                continue
            order["completed_notification_sent"] = True
            orders_updated = True

    if orders_updated:
        _safe_write_json(ORDERS_DATA_PATH, raw_orders)

    if runtime_cleanup_needed:
        save_orders_runtime(runtime_data)

    return raw_orders


def load_orders_runtime() -> Dict[str, Dict]:
    data = _safe_load_json(ORDERS_RUNTIME_PATH, {})
    if not isinstance(data, dict):
        return {}

    cleaned: Dict[str, Dict] = {}
    for order_id, runtime_fields in data.items():
        if not isinstance(runtime_fields, dict):
            continue
        filtered = {k: v for k, v in runtime_fields.items() if k in RUNTIME_FIELDS and v not in (None, "")}
        if filtered:
            cleaned[str(order_id)] = filtered
    return cleaned


def save_orders_runtime(runtime_data: Dict[str, Dict]):
    cleaned: Dict[str, Dict] = {}
    for order_id, runtime_fields in (runtime_data or {}).items():
        if not isinstance(runtime_fields, dict):
            continue
        filtered = {k: v for k, v in runtime_fields.items() if k in RUNTIME_FIELDS and v not in (None, "")}
        if filtered:
            cleaned[str(order_id)] = filtered
    _safe_write_json(ORDERS_RUNTIME_PATH, cleaned)


def _strip_runtime_and_empty_fallback(order: Dict) -> Dict:
    clean = dict(order or {})
    for key in list(clean.keys()):
        if key in RUNTIME_FIELDS:
            clean.pop(key, None)

    clean.pop("is_refunded", None)
    clean.pop("refund_detected_at", None)
    clean.pop("refund_source", None)

    fallback_used = clean.get("fallback_used")
    fallback_reason = clean.get("fallback_reason")
    if fallback_used is True:
        clean["fallback_used"] = True
        if fallback_reason in (None, ""):
            clean.pop("fallback_reason", None)
    elif fallback_reason not in (None, ""):
        clean["fallback_used"] = False
        clean["fallback_reason"] = fallback_reason
    else:
        clean.pop("fallback_used", None)
        clean.pop("fallback_reason", None)

    return clean


def _merge_runtime_fields(order: Dict, runtime_map: Dict[str, Dict]) -> Dict:
    merged = dict(order or {})
    order_id = str(merged.get("order_id", ""))
    if order_id and order_id in runtime_map:
        merged.update(runtime_map[order_id])
    return merged


def load_orders_data() -> List[Dict]:
    raw_orders = _load_orders_data_raw()
    runtime_map = load_orders_runtime()
    return [_merge_runtime_fields(order, runtime_map) for order in raw_orders]


def save_orders_data(orders: List[Dict]):
    cleaned_orders = [_strip_runtime_and_empty_fallback(order) for order in (orders or [])]
    _safe_write_json(ORDERS_DATA_PATH, cleaned_orders)


def get_order_data(order_id_funpay: str) -> Dict | None:
    runtime_map = load_orders_runtime()
    for order in _load_orders_data_raw():
        if str(order.get("order_id")) == str(order_id_funpay):
            return _merge_runtime_fields(order, runtime_map)
    return None


def _build_order_record(
    chat_id: int,
    order_id: str,
    twiboost_id: int,
    status: str,
    chistota: float,
    customer_url: str,
    quantity: int,
    service_number: int,
    is_refunded: bool = False,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    created_at: int | None = None,
    funpay_status: str | None = None,
) -> Dict:
    normalized_funpay_status = _normalize_funpay_status(funpay_status)
    if not normalized_funpay_status:
        normalized_funpay_status = "refunded" if is_refunded else "open"

    record = {
        "chat_id": chat_id,
        "order_id": order_id,
        "id_zakaz": twiboost_id,
        "status": status,
        "chistota": float(chistota),
        "customer_url": customer_url,
        "quantity": quantity,
        "service_number": service_number,
        "funpay_status": normalized_funpay_status,
        "spent": 0.0,
        "summa": float(chistota),
        "currency": "RUB",
        "created_at": int(created_at or time.time()),
        "completed_notification_sent": False,
    }
    if fallback_used is True:
        record["fallback_used"] = True
        if fallback_reason not in (None, ""):
            record["fallback_reason"] = fallback_reason
    elif fallback_reason not in (None, ""):
        record["fallback_used"] = False
        record["fallback_reason"] = fallback_reason
    return record


def upsert_order_data(
    chat_id: int,
    order_id: str,
    twiboost_id: int,
    status: str,
    chistota: float,
    customer_url: str,
    quantity: int,
    service_number: int,
    is_refunded: bool = False,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    funpay_status: str | None = None,
):
    orders = _load_orders_data_raw()
    updated = False

    for order in orders:
        if str(order.get("order_id")) == str(order_id):
            created_at = order.get("created_at")
            spent = order.get("spent", 0.0)
            currency = order.get("currency", "RUB")
            summa = order.get("summa", float(chistota))
            completed_notification_sent = bool(order.get("completed_notification_sent", False))
            current_funpay_status = _normalize_funpay_status(order.get("funpay_status"))
            normalized_funpay_status = _normalize_funpay_status(funpay_status) or current_funpay_status or ("refunded" if is_refunded else "open")
            order.update(
                {
                    "chat_id": chat_id,
                    "order_id": order_id,
                    "id_zakaz": twiboost_id,
                    "status": status,
                    "customer_url": customer_url,
                    "quantity": quantity,
                    "service_number": service_number,
                    "funpay_status": normalized_funpay_status,
                    "summa": float(summa),
                    "spent": float(spent or 0.0),
                    "currency": str(currency or "RUB"),
                    "created_at": int(created_at or time.time()),
                    "completed_notification_sent": completed_notification_sent,
                }
            )
            if fallback_used is True:
                order["fallback_used"] = True
                order["fallback_reason"] = fallback_reason
            elif fallback_reason not in (None, ""):
                order["fallback_used"] = False
                order["fallback_reason"] = fallback_reason
            else:
                order.pop("fallback_used", None)
                order.pop("fallback_reason", None)
            updated = True
            break

    if not updated:
        orders.append(
            _build_order_record(
                chat_id=chat_id,
                order_id=order_id,
                twiboost_id=twiboost_id,
                status=status,
                chistota=chistota,
                customer_url=customer_url,
                quantity=quantity,
                service_number=service_number,
                is_refunded=is_refunded,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                funpay_status=funpay_status,
            )
        )

    save_orders_data(orders)
    logger.info(
        f"Данные заказа #{order_id} (twiboost ID: {twiboost_id}) сохранены/обновлены. "
        f"fallback_used={bool(fallback_used) if (fallback_used or fallback_reason not in (None, '')) else 'not_set'}, "
        f"fallback_reason={fallback_reason}"
    )


def save_order_data(
    chat_id: int,
    order_id: str,
    twiboost_id: int,
    status: str,
    chistota: float,
    customer_url: str,
    quantity: int,
    service_number: int,
    is_refunded: bool = False,
    funpay_status: str | None = None,
):
    upsert_order_data(
        chat_id=chat_id,
        order_id=order_id,
        twiboost_id=twiboost_id,
        status=status,
        chistota=chistota,
        customer_url=customer_url,
        quantity=quantity,
        service_number=service_number,
        is_refunded=is_refunded,
        fallback_used=False,
        fallback_reason=None,
        funpay_status=funpay_status,
    )


def update_order_fields(order_id_funpay: str, **fields) -> bool:
    orders = _load_orders_data_raw()
    order_found = False
    business_fields = {k: v for k, v in fields.items() if k not in RUNTIME_FIELDS}
    runtime_fields = {k: v for k, v in fields.items() if k in RUNTIME_FIELDS}

    for order in orders:
        if str(order.get("order_id")) != str(order_id_funpay):
            continue
        order_found = True
        for key, value in business_fields.items():
            if key in ("fallback_used", "fallback_reason"):
                continue
            if key == "funpay_status":
                value = _normalize_funpay_status(value)
            if key in ("is_refunded",):
                continue
            if value is None:
                order.pop(key, None)
            else:
                order[key] = value

        normalized_funpay_status = _normalize_funpay_status(order.get("funpay_status"))
        if normalized_funpay_status:
            order["funpay_status"] = normalized_funpay_status

        if "fallback_used" in business_fields or "fallback_reason" in business_fields:
            fallback_used = business_fields.get("fallback_used")
            fallback_reason = business_fields.get("fallback_reason")
            if fallback_used is True:
                order["fallback_used"] = True
                order["fallback_reason"] = fallback_reason
            elif fallback_reason not in (None, ""):
                order["fallback_used"] = False
                order["fallback_reason"] = fallback_reason
            else:
                order.pop("fallback_used", None)
                order.pop("fallback_reason", None)
        break

    if not order_found:
        logger.warning(f"Заказ #{order_id_funpay} не найден в orders_data.json.")
        return False

    if business_fields:
        save_orders_data(orders)

    if runtime_fields:
        runtime_data = load_orders_runtime()
        runtime_entry = dict(runtime_data.get(str(order_id_funpay), {}))
        for key, value in runtime_fields.items():
            if value in (None, ""):
                runtime_entry.pop(key, None)
            else:
                runtime_entry[key] = value
        if runtime_entry:
            runtime_data[str(order_id_funpay)] = runtime_entry
        else:
            runtime_data.pop(str(order_id_funpay), None)
        save_orders_runtime(runtime_data)

    return True


def mark_order_refunded(order_id_funpay: str, detected_at: str | None = None, source: str | None = None) -> bool:
    orders = _load_orders_data_raw()
    updated = False
    for order in orders:
        if str(order.get("order_id")) != str(order_id_funpay):
            continue
        if _is_refunded_funpay_status(order.get("funpay_status")):
            return False
        order["funpay_status"] = "refunded"
        updated = True
        break

    if updated:
        save_orders_data(orders)
        runtime_updates = {}
        if detected_at not in (None, ""):
            runtime_updates["refund_detected_at"] = detected_at
        if source not in (None, ""):
            runtime_updates["refund_source"] = source
        if runtime_updates:
            update_order_fields(order_id_funpay, **runtime_updates)
        update_order_fields(
            order_id_funpay,
            buyer_flow_state=BUYER_STATE_REFUNDED,
            saved_link=None,
            refund_detected_at=detected_at or _now_str(),
            refund_source=source or "refund_mark",
            updated_at=_now_str(),
        )
        waiting_for_link.pop(str(order_id_funpay), None)
        _attempt_panel_cancel_after_funpay_refund(get_order_data(order_id_funpay), source=source or "refund_mark")
        return True

    logger.warning(f"Заказ #{order_id_funpay} не найден в orders_data.json.")
    return False


def update_order_status(order_id_funpay: str, new_status: str):
    update_order_fields(order_id_funpay, status=new_status)
    logger.info(f"Статус заказа #{order_id_funpay} обновлён на '{new_status}'.")


def update_order_refunded_status(order_id_funpay: str):
    updated = mark_order_refunded(order_id_funpay)
    if updated:
        logger.info(f"Статус заказа #{order_id_funpay} обновлён на 'funpay_status': refunded.")
    else:
        logger.warning(f"Заказ #{order_id_funpay} не найден или уже отмечен как refunded.")


orders = SimpleNamespace(
    ORDERS_PATH=ORDERS_PATH,
    ORDERS_DATA_PATH=ORDERS_DATA_PATH,
    ORDERS_RUNTIME_PATH=ORDERS_RUNTIME_PATH,
    RUNTIME_FIELDS=RUNTIME_FIELDS,
    load_orders_runtime=load_orders_runtime,
    save_orders_runtime=save_orders_runtime,
    load_orders_data=load_orders_data,
    save_orders_data=save_orders_data,
    get_order_data=get_order_data,
    upsert_order_data=upsert_order_data,
    save_order_data=save_order_data,
    update_order_fields=update_order_fields,
    mark_order_refunded=mark_order_refunded,
    update_order_status=update_order_status,
    update_order_refunded_status=update_order_refunded_status,
)

# =========================================================
# PANEL API
# =========================================================

def _normalize_api_key(api_key: Any) -> str:
    return str(api_key or "").strip()


def _build_invalid_api_key_payload(service_number: int, action: str, api_key: Any) -> Dict:
    key_len = len(_normalize_api_key(api_key))
    detail = (
        f"Некорректная длина API-ключа для service_number={service_number} перед action={action}: "
        f"{key_len} символов (ожидается {EXPECTED_API_KEY_LENGTH}, стандарт SocPanel v2)."
    )
    logger.warning(f"{LOGGER_PREFIX} {detail}")
    return {
        "error": "invalid_api_key",
        "detail": detail,
        "expected_length": EXPECTED_API_KEY_LENGTH,
        "actual_length": key_len,
    }


def _validate_api_key_for_action(service_number: int, action: str, api_key: Any) -> Optional[Dict]:
    if len(_normalize_api_key(api_key)) == EXPECTED_API_KEY_LENGTH:
        return None
    return _build_invalid_api_key_payload(service_number, action, api_key)


def _stringify_for_matching(value: Any) -> str:
    try:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
    except Exception:
        pass
    return str(value or "")


def _contains_any(haystack: str, needles: List[str]) -> bool:
    return any(needle in haystack for needle in needles)


def _map_panel_status_from_text(text: str) -> Optional[str]:
    haystack_raw = str(text or "").lower()
    if not haystack_raw:
        return None

    haystack = haystack_raw.replace("-", "_")
    haystack_spaces = haystack.replace("_", " ")

    def has_any(tokens: List[str]) -> bool:
        for token in tokens:
            token_norm = str(token or "").lower().replace("-", "_")
            if token_norm in haystack or token_norm.replace("_", " ") in haystack_spaces:
                return True
        return False

    if has_any([
        "unsupported_action",
        "unsupported action",
        "action not supported",
        "method not found",
        "invalid action",
        "unknown action",
        "endpoint not supported",
        "not supported",
    ]):
        return "unsupported_action"

    if has_any([
        "service_inactive",
        "service inactive",
        "inactive service",
        "service is inactive",
        "service disabled",
        "disabled service",
        "service not active",
        "service not found",
    ]):
        return "service_inactive"

    if has_any([
        "insufficient_funds",
        "insufficient funds",
        "not enough funds",
        "not enough balance",
        "insufficient balance",
        "balance too low",
        "low balance",
        "no balance",
    ]):
        return "insufficient_funds"

    if has_any([
        "user_inactive",
        "user inactive",
        "inactive user",
        "invalid api key",
        "api key invalid",
        "incorrect api key",
        "wrong api key",
        "api key not found",
        "bad api key",
        "account inactive",
        "inactive account",
        "user not found",
    ]):
        return "user_inactive"

    if has_any([
        "validation_error",
        "validation error",
        "invalid link",
        "invalid url",
        "invalid quantity",
        "invalid format",
        "required parameter",
        "parameter is required",
        "missing parameter",
    ]):
        return "validation_error"

    return None


def _classify_panel_api_result(
    action: str,
    response: Optional[requests.Response] = None,
    payload: Any = None,
    raw_text: str = "",
    exception: Optional[Exception] = None,
    invalid_json: bool = False,
    local_status: Optional[str] = None,
    local_detail: Optional[str] = None,
) -> Dict:
    http_status = response.status_code if response is not None else None

    if local_status:
        return {
            "error": local_status,
            "error_class": "PANEL_ERROR" if local_status in (
                "service_inactive",
                "insufficient_funds",
                "user_inactive",
                "unsupported_action",
                "validation_error",
            ) else "TRANSPORT_ERROR",
            "detail": local_detail or "Локальная ошибка конфигурации.",
            "response": payload,
            "http_status": http_status,
        }

    if exception is not None:
        error_code = "timeout" if isinstance(exception, requests.exceptions.Timeout) else "network_error"
        return {
            "error": error_code,
            "error_class": "TRANSPORT_ERROR",
            "detail": str(exception),
            "response": payload,
            "http_status": http_status,
        }

    text_for_match = " ".join(part for part in [
        _stringify_for_matching(payload),
        _stringify_for_matching(raw_text),
    ] if part).strip()

    if action == "add" and isinstance(payload, dict) and "order" in payload and (http_status is None or http_status < 400):
        return {
            "error": "success",
            "error_class": None,
            "detail": "OK",
            "response": payload,
            "http_status": http_status,
        }

    mapped_status = _map_panel_status_from_text(text_for_match)
    if mapped_status:
        return {
            "error": mapped_status,
            "error_class": "PANEL_ERROR",
            "detail": text_for_match or mapped_status,
            "response": payload if payload is not None else raw_text,
            "http_status": http_status,
        }

    if invalid_json:
        return {
            "error": "invalid_json",
            "error_class": "TRANSPORT_ERROR",
            "detail": raw_text or "Не удалось разобрать JSON-ответ панели.",
            "response": raw_text,
            "http_status": http_status,
        }

    if http_status is not None and http_status >= 400:
        return {
            "error": "http_error",
            "error_class": "TRANSPORT_ERROR",
            "detail": f"HTTP {http_status}",
            "response": payload if payload is not None else raw_text,
            "http_status": http_status,
        }

    if action in ("status", "refill") and payload is not None:
        return {
            "error": "success",
            "error_class": None,
            "detail": "OK",
            "response": payload,
            "http_status": http_status,
        }

    return {
        "error": "unknown",
        "error_class": "UNKNOWN_ERROR",
        "detail": text_for_match or "Неизвестный ответ панели.",
        "response": payload if payload is not None else raw_text,
        "http_status": http_status,
    }


def send_order_to_service(
    c: Cardinal,
    service_number: int,
    service_id: int,
    link: str,
    quantity: int,
    api_timeout: int = 20
) -> Tuple[bool, int | None, Dict]:
    """
    Отправляет заказ в выбранный SMM-сервис.

    Возвращает:
    - ok: bool
    - twiboost_order_id: int | None
    - payload: dict с нормализованным результатом
    """
    cfg = load_config()
    service_cfg = cfg["services"].get(str(service_number))
    if not service_cfg:
        return False, None, {
            "error": "unknown",
            "error_class": "UNKNOWN_ERROR",
            "detail": f"Нет настроек для service_number={service_number}",
            "response": None,
            "http_status": None,
        }

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    invalid_key_payload = _validate_api_key_for_action(service_number, "add", api_key)
    if invalid_key_payload:
        classified = _classify_panel_api_result(
            action="add",
            payload=invalid_key_payload,
            local_status="user_inactive",
            local_detail=invalid_key_payload.get("detail"),
        )
        return False, None, classified

    encoded_link = quote(link, safe="")
    url_req = f"{api_url}?action=add&service={service_id}&link={encoded_link}&quantity={quantity}&key={api_key}"

    try:
        resp_ = requests.get(url_req, timeout=api_timeout)
        raw_text = resp_.text
        try:
            j_ = resp_.json()
            invalid_json = False
        except Exception:
            j_ = None
            invalid_json = True

        classified = _classify_panel_api_result(
            action="add",
            response=resp_,
            payload=j_,
            raw_text=raw_text,
            invalid_json=invalid_json,
        )

        if classified["error"] == "success":
            try:
                return True, int(j_["order"]), {
                    "error": "success",
                    "error_class": None,
                    "detail": "OK",
                    "response": j_,
                    "http_status": resp_.status_code,
                }
            except Exception:
                return False, None, {
                    "error": "unknown",
                    "error_class": "UNKNOWN_ERROR",
                    "detail": f"Не удалось распарсить order из ответа панели: {j_}",
                    "response": j_,
                    "http_status": resp_.status_code,
                }

        return False, None, classified

    except requests.exceptions.Timeout as ex:
        return False, None, _classify_panel_api_result(action="add", exception=ex)
    except requests.exceptions.ConnectionError as ex:
        return False, None, _classify_panel_api_result(action="add", exception=ex)
    except requests.exceptions.RequestException as ex:
        return False, None, _classify_panel_api_result(action="add", exception=ex)
    except Exception as ex:
        return False, None, {
            "error": "unknown",
            "error_class": "UNKNOWN_ERROR",
            "detail": str(ex),
            "response": None,
            "http_status": None,
        }


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        return int(float(value))
    except Exception:
        return None


def _normalize_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _extract_order_id_from_obj(obj: Any) -> Optional[int]:
    if isinstance(obj, dict):
        for key in ("order", "id", "order_id"):
            order_id = _safe_int(obj.get(key))
            if order_id is not None:
                return order_id
    return None


def _iter_dicts(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_dicts(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_dicts(item)


def _normalize_runtime_status(status_value: Any) -> str:
    haystack = str(status_value or "").strip().lower()
    if not haystack:
        return "unknown"
    if "completed" in haystack:
        return "completed"
    if "partial" in haystack:
        return "partial"
    if "canceled" in haystack or "cancelled" in haystack:
        return "canceled"
    if "failed" in haystack or haystack == "fail" or " error" in f" {haystack}" or haystack.startswith("error"):
        return "fail"
    if haystack in ("pending", "awaiting") or "in progress" in haystack or "processing" in haystack:
        return "pending"
    return "unknown"


def _is_incorrect_order_id_text(value: Any) -> bool:
    haystack = str(value or "").strip().lower()
    if not haystack:
        return False
    return any(token in haystack for token in [
        "incorrect order id",
        "incorrect order",
        "order id is incorrect",
        "order not found",
        "заказ не найден",
        "неверный id заказа",
        "не найден заказ",
    ])


def _build_status_result_from_entry(entry: Any) -> Dict:
    text_ = _stringify_for_matching(entry).strip()

    if _is_incorrect_order_id_text(text_):
        return {
            "ok": False,
            "error": "incorrect_order_id",
            "detail": text_ or "Incorrect order ID",
            "raw": entry,
        }

    mapped_error = _map_panel_status_from_text(text_)
    if mapped_error in ("unsupported_action", "service_inactive", "insufficient_funds", "user_inactive"):
        return {
            "ok": False,
            "error": mapped_error,
            "detail": text_ or mapped_error,
            "raw": entry,
        }

    if isinstance(entry, dict):
        status_value = entry.get("status")
        if status_value is not None:
            return {
                "ok": True,
                "status": _normalize_runtime_status(status_value),
                "charge": entry.get("charge", "0"),
                "currency": entry.get("currency", "USD"),
                "remains": entry.get("remains"),
                "start_count": entry.get("start_count"),
                "raw": entry,
            }

    if isinstance(entry, str):
        norm_status = _normalize_runtime_status(entry)
        if norm_status != "unknown":
            return {
                "ok": True,
                "status": norm_status,
                "charge": "0",
                "currency": "USD",
                "raw": entry,
            }

    return {
        "ok": False,
        "error": "unknown",
        "detail": text_ or "Не удалось подтвердить состояние заказа.",
        "raw": entry,
    }


def _fetch_single_order_status(service_number: int, twiboost_order_id: int, timeout: int = 15) -> Dict:
    cfg = load_config()
    service_cfg = cfg.get("services", {}).get(str(service_number))
    if not service_cfg:
        return {"ok": False, "error": "unknown", "detail": f"Нет настроек для service_number={service_number}"}

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
    if invalid_key_payload:
        return {
            "ok": False,
            "error": "user_inactive",
            "detail": invalid_key_payload.get("detail", "Некорректный API-ключ."),
        }

    url_ = f"{api_url}?action=status&order={twiboost_order_id}&key={api_key}"

    try:
        response = requests.get(url_, timeout=timeout)
        raw_text = response.text
        try:
            payload = response.json()
            invalid_json = False
        except Exception:
            payload = None
            invalid_json = True

        text_for_check = " ".join(part for part in [
            _stringify_for_matching(payload),
            _stringify_for_matching(raw_text),
        ] if part).strip()

        if _is_incorrect_order_id_text(text_for_check):
            return {
                "ok": False,
                "error": "incorrect_order_id",
                "detail": text_for_check or "Incorrect order ID",
                "raw": payload if payload is not None else raw_text,
            }

        classified = _classify_panel_api_result(
            action="status",
            response=response,
            payload=payload,
            raw_text=raw_text,
            invalid_json=invalid_json,
        )

        if classified.get("error") != "success":
            logger.error(
                f"STATUS CHECK ERROR: service=#{service_number}, order={twiboost_order_id}, "
                f"error={classified.get('error')}, detail={classified.get('detail')}"
            )
            return {
                "ok": False,
                "error": classified.get("error", "unknown"),
                "detail": classified.get("detail", "Не удалось подтвердить состояние заказа."),
                "raw": classified.get("response"),
            }

        return _build_status_result_from_entry(payload)
    except requests.exceptions.Timeout as ex:
        logger.error(f"STATUS CHECK TIMEOUT: service=#{service_number}, order={twiboost_order_id}, error={ex}")
        return {"ok": False, "error": "timeout", "detail": str(ex)}
    except requests.exceptions.RequestException as ex:
        logger.error(f"STATUS CHECK NETWORK ERROR: service=#{service_number}, order={twiboost_order_id}, error={ex}")
        return {"ok": False, "error": "network_error", "detail": str(ex)}
    except Exception as ex:
        logger.error(f"STATUS CHECK UNKNOWN ERROR: service=#{service_number}, order={twiboost_order_id}, error={ex}")
        return {"ok": False, "error": "unknown", "detail": str(ex)}


def _extract_batch_status_entries(payload: Any, order_ids: List[int]) -> Dict[str, Any]:
    expected_ids = {str(order_id) for order_id in order_ids}

    if isinstance(payload, dict):
        nested = payload.get("orders")
        if isinstance(nested, dict):
            payload = nested

        direct = {str(k): v for k, v in payload.items() if str(k) in expected_ids}
        if direct:
            return direct

        mapped: Dict[str, Any] = {}
        for value in payload.values():
            if isinstance(value, dict):
                order_id = _extract_order_id_from_obj(value)
                if order_id is not None and str(order_id) in expected_ids:
                    mapped[str(order_id)] = value
        return mapped

    if isinstance(payload, list):
        mapped = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            order_id = _extract_order_id_from_obj(item)
            if order_id is not None and str(order_id) in expected_ids:
                mapped[str(order_id)] = item
        return mapped

    return {}


def _fetch_batch_order_status(service_number: int, order_ids: List[int], timeout: int = 20) -> Dict:
    cfg = load_config()
    service_cfg = cfg.get("services", {}).get(str(service_number))
    if not service_cfg:
        return {"ok": False, "error": "unknown", "detail": f"Нет настроек для service_number={service_number}"}

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
    if invalid_key_payload:
        return {
            "ok": False,
            "error": "user_inactive",
            "detail": invalid_key_payload.get("detail", "Некорректный API-ключ."),
        }

    orders_param = ",".join(str(order_id) for order_id in order_ids)
    url_ = f"{api_url}?action=status&orders={orders_param}&key={api_key}"

    try:
        response = requests.get(url_, timeout=timeout)
        raw_text = response.text
        try:
            payload = response.json()
            invalid_json = False
        except Exception:
            payload = None
            invalid_json = True

        text_for_check = " ".join(part for part in [
            _stringify_for_matching(payload),
            _stringify_for_matching(raw_text),
        ] if part).strip()

        if _is_incorrect_order_id_text(text_for_check):
            return {
                "ok": False,
                "error": "incorrect_order_id",
                "detail": text_for_check or "Incorrect order ID",
                "raw": payload if payload is not None else raw_text,
            }

        classified = _classify_panel_api_result(
            action="status",
            response=response,
            payload=payload,
            raw_text=raw_text,
            invalid_json=invalid_json,
        )

        if classified.get("error") != "success":
            logger.error(
                f"BATCH STATUS ERROR: service=#{service_number}, orders={orders_param}, "
                f"error={classified.get('error')}, detail={classified.get('detail')}"
            )
            return {
                "ok": False,
                "error": classified.get("error", "unknown"),
                "detail": classified.get("detail", "Не удалось обработать batch status ответ."),
                "raw": classified.get("response"),
            }

        entries = _extract_batch_status_entries(payload, order_ids)
        if entries:
            return {"ok": True, "entries": entries}

        fallback_result = _build_status_result_from_entry(payload)
        return {
            "ok": False,
            "error": fallback_result.get("error", "unknown"),
            "detail": fallback_result.get("detail", "Не удалось обработать batch status ответ."),
            "raw": payload,
        }
    except requests.exceptions.Timeout as ex:
        logger.error(f"BATCH STATUS TIMEOUT: service=#{service_number}, orders={orders_param}, error={ex}")
        return {"ok": False, "error": "timeout", "detail": str(ex)}
    except requests.exceptions.RequestException as ex:
        logger.error(f"BATCH STATUS NETWORK ERROR: service=#{service_number}, orders={orders_param}, error={ex}")
        return {"ok": False, "error": "network_error", "detail": str(ex)}
    except Exception as ex:
        logger.error(f"BATCH STATUS UNKNOWN ERROR: service=#{service_number}, orders={orders_param}, error={ex}")
        return {"ok": False, "error": "unknown", "detail": str(ex)}


# =========================================================
# ORDER PROCESSING
# =========================================================

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt_or_none(value: Any) -> Optional[datetime]:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw_value, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw_value)
    except Exception:
        return None


def _seconds_since_checked_at(value: Any) -> Optional[float]:
    dt_value = _parse_dt_or_none(value)
    if not dt_value:
        return None
    return max(0.0, (datetime.now() - dt_value).total_seconds())


def _extract_funpay_status_from_message(message_text: str) -> Tuple[Optional[str], Optional[str]]:
    text_ = normalize_incoming_text(message_text)
    if not text_:
        return None, None

    for funpay_status, patterns in FUNPAY_LIVE_STATUS_PATTERNS:
        for pattern in patterns:
            match = pattern.search(text_)
            if match:
                return funpay_status, match.group(1)
    return None, None


def _sync_funpay_status_from_message(message_text: str) -> bool:
    funpay_status, order_id = _extract_funpay_status_from_message(message_text)
    if not funpay_status or not order_id:
        return False

    order_data = orders.get_order_data(order_id)
    if not order_data:
        logger.info(f"FUNPAY STATUS LIVE: заказ #{order_id} не найден в orders_data.json (status={funpay_status})")
        return True

    previous_funpay_status = _get_effective_funpay_status(order_data)
    update_fields: Dict[str, Any] = {
        "funpay_status": funpay_status,
        "funpay_status_checked_at": _now_str(),
    }
    if funpay_status in ("refunded", "partially_refunded"):
        update_fields["refund_detected_at"] = _now_str()
        update_fields["refund_source"] = "funpay_message"

    if funpay_status in ("refunded", "partially_refunded"):
        update_fields["buyer_flow_state"] = BUYER_STATE_REFUNDED
        update_fields["saved_link"] = None
        update_fields["updated_at"] = _now_str()
        waiting_for_link.pop(str(order_id), None)
    orders.update_order_fields(order_id, **update_fields)
    updated_order = orders.get_order_data(order_id) or order_data
    if funpay_status == "refunded" and previous_funpay_status != "refunded":
        _attempt_panel_cancel_after_funpay_refund(updated_order, source="funpay_live")
    logger.info(f"FUNPAY STATUS LIVE: заказ #{order_id} обновлён до '{funpay_status}'")
    return True


def _map_cardinal_order_status_to_funpay_status(order_status: Any) -> Optional[str]:
    order_statuses = getattr(enums, "OrderStatuses", None)
    if order_statuses is not None:
        status_mapping = {
            getattr(order_statuses, "PAID", None): "open",
            getattr(order_statuses, "CLOSED", None): "closed",
            getattr(order_statuses, "REFUNDED", None): "refunded",
            getattr(order_statuses, "PARTIALLY_REFUNDED", None): "partially_refunded",
        }
        for candidate, normalized in status_mapping.items():
            if candidate is not None and order_status == candidate:
                return normalized

    raw_status = str(order_status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw_status.endswith(".paid") or raw_status == "paid":
        return "open"
    if raw_status.endswith(".closed") or raw_status == "closed":
        return "closed"
    if raw_status.endswith(".refunded") or raw_status == "refunded":
        return "refunded"
    if "partially_refunded" in raw_status:
        return "partially_refunded"
    return None


def _fetch_funpay_status_via_cardinal(c: Cardinal, order_id_funpay: str) -> Dict[str, Any]:
    try:
        order_data = c.account.get_order(order_id_funpay)
        normalized_status = _map_cardinal_order_status_to_funpay_status(getattr(order_data, "status", None))
        if not normalized_status:
            return {
                "ok": False,
                "error": "unknown_funpay_status",
                "detail": f"Не удалось сопоставить order.status для заказа #{order_id_funpay}",
            }
        return {"ok": True, "funpay_status": normalized_status}
    except Exception as ex:
        return {"ok": False, "error": "cardinal_error", "detail": str(ex)}




def _refresh_funpay_status_for_order(
    c: Optional[Cardinal],
    order_record: Optional[Dict],
    force_check: bool = False,
) -> Dict:
    """Refreshes FunPay status for one stored order without breaking buyer-flow.

    This helper is intentionally conservative: if Cardinal/FunPay status check is
    unavailable, it returns the latest local record and does not invent a status.
    """
    if not isinstance(order_record, dict):
        return {}

    order_id_funpay = str(
        order_record.get("order_id")
        or order_record.get("order_id_funpay")
        or ""
    ).strip().lstrip("#")
    if not order_id_funpay:
        return dict(order_record)

    latest_order = orders.get_order_data(order_id_funpay) or dict(order_record)

    if not force_check and not _funpay_status_needs_recheck(latest_order, FUNPAY_STATUS_RECENT_CHECK_SECONDS):
        return latest_order

    if c is None:
        return latest_order

    previous_funpay_status = _get_effective_funpay_status(latest_order)
    result = _fetch_funpay_status_via_cardinal(c, order_id_funpay)
    checked_at = _now_str()

    if not result.get("ok"):
        try:
            orders.update_order_fields(order_id_funpay, funpay_status_checked_at=checked_at)
            latest_order = orders.get_order_data(order_id_funpay) or latest_order
        except Exception:
            logger.warning(
                f"FUNPAY STATUS REFRESH FAILED: order=#{order_id_funpay}, "
                f"error={result.get('error')}, detail={result.get('detail')}"
            )
        return latest_order

    new_funpay_status = _normalize_funpay_status(result.get("funpay_status"))
    if not new_funpay_status:
        return latest_order

    update_fields: Dict[str, Any] = {
        "funpay_status": new_funpay_status,
        "funpay_status_checked_at": checked_at,
    }

    if new_funpay_status in ("refunded", "partially_refunded"):
        update_fields.update(
            {
                "buyer_flow_state": BUYER_STATE_REFUNDED,
                "saved_link": None,
                "refund_detected_at": checked_at,
                "refund_source": "refund_gate_refresh",
                "updated_at": checked_at,
            }
        )
        waiting_for_link.pop(order_id_funpay, None)

    try:
        orders.update_order_fields(order_id_funpay, **update_fields)
    except Exception as ex:
        logger.warning(f"FUNPAY STATUS REFRESH SAVE FAILED: order=#{order_id_funpay}, error={ex}")
        latest_order.update(update_fields)
        return latest_order

    refreshed_order = orders.get_order_data(order_id_funpay) or latest_order
    refreshed_order.update(update_fields)

    if new_funpay_status == "refunded" and previous_funpay_status != "refunded":
        try:
            _attempt_panel_cancel_after_funpay_refund(refreshed_order, source="refund_gate_refresh")
        except Exception as ex:
            logger.warning(f"PANEL CANCEL AFTER REFUND GATE FAILED: order=#{order_id_funpay}, error={ex}")

    return refreshed_order


def _apply_refund_gate(
    c: Optional[Cardinal],
    order_record: Optional[Dict],
    notify_chat_id: Optional[int] = None,
    force_check: bool = False,
) -> bool:
    """Blocks buyer actions when the related FunPay order is refunded.

    Returns True when the caller must stop further buyer-flow processing.
    """
    if not isinstance(order_record, dict):
        return False

    latest_order = _refresh_funpay_status_for_order(c, order_record, force_check=force_check)
    if not isinstance(latest_order, dict) or not latest_order:
        latest_order = order_record

    if not _is_order_refundedish(latest_order):
        return False

    order_id_funpay = str(
        latest_order.get("order_id")
        or latest_order.get("order_id_funpay")
        or order_record.get("order_id")
        or order_record.get("order_id_funpay")
        or ""
    ).strip().lstrip("#")

    if order_id_funpay:
        try:
            orders.update_order_fields(
                order_id_funpay,
                buyer_flow_state=BUYER_STATE_REFUNDED,
                saved_link=None,
                updated_at=_now_str(),
            )
        except Exception as ex:
            logger.warning(f"REFUND GATE SAVE FAILED: order=#{order_id_funpay}, error={ex}")
        waiting_for_link.pop(order_id_funpay, None)

    if notify_chat_id is not None and c is not None:
        try:
            _send_buyer_message(c, notify_chat_id, BUYER_REFUND_BLOCK_TEXT)
        except Exception as ex:
            logger.warning(f"REFUND GATE NOTIFY FAILED: order=#{order_id_funpay}, error={ex}")

    return True


def _attempt_panel_cancel_after_funpay_refund(order_record: Optional[Dict], source: str = "funpay_refund") -> bool:
    if not isinstance(order_record, dict):
        return False

    order_id_funpay = str(order_record.get("order_id") or "")
    if not order_id_funpay:
        return False
    if _get_effective_funpay_status(order_record) != "refunded":
        return False

    cfg = load_config()
    if not bool(cfg.get("try_panel_cancel_after_funpay_refund", False)):
        logger.info(f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=disabled_by_config")
        return False

    if order_record.get("panel_refund_cancel_attempted_at"):
        return False

    twiboost_order_id = order_record.get("id_zakaz")
    service_number = order_record.get("service_number")
    if not twiboost_order_id or service_number in (None, ""):
        logger.info(
            f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=missing_order_or_service"
        )
        return False

    service_cfg = (cfg.get("services") or {}).get(str(service_number))
    if not isinstance(service_cfg, dict):
        logger.info(
            f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=service_not_found, service=#{service_number}"
        )
        return False

    api_url = str(service_cfg.get("api_url") or "").strip()
    api_key = str(service_cfg.get("api_key") or "").strip()
    if not api_url or not api_key:
        logger.info(
            f"PANEL CANCEL SKIPPED: order=#{order_id_funpay}, source={source}, reason=service_credentials_missing, service=#{service_number}"
        )
        return False

    attempted_at = _now_str()
    orders.update_order_fields(order_id_funpay, panel_refund_cancel_attempted_at=attempted_at)

    try:
        response = requests.get(
            api_url,
            params={"action": "cancel", "order": twiboost_order_id, "key": api_key},
            timeout=10,
        )
        response_text = (response.text or "").strip()
        if len(response_text) > 300:
            response_text = response_text[:300] + "…"
        try:
            payload = response.json()
        except Exception:
            payload = response_text or f"HTTP {response.status_code}"
        orders.update_order_fields(
            order_id_funpay,
            panel_refund_cancel_result=f"HTTP {response.status_code}: {payload}",
        )
        logger.info(
            f"PANEL CANCEL ATTEMPT: order=#{order_id_funpay}, source={source}, service=#{service_number}, "
            f"panel_order={twiboost_order_id}, http_status={response.status_code}, result={payload}"
        )
        return True
    except Exception as ex:
        orders.update_order_fields(order_id_funpay, panel_refund_cancel_result=f"error: {ex}")
        logger.warning(
            f"PANEL CANCEL FAILED: order=#{order_id_funpay}, source={source}, service=#{service_number}, "
            f"panel_order={twiboost_order_id}, error={ex}"
        )
        return False


def _record_plugin_start_state():
    state = _safe_load_json(AUTOSMM_STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    state["last_plugin_start_at"] = _now_str()
    _safe_write_json(AUTOSMM_STATE_PATH, state)


def _funpay_status_needs_recheck(order_record: Dict, min_age_seconds: int) -> bool:
    checked_age = _seconds_since_checked_at(order_record.get("funpay_status_checked_at"))
    if checked_age is None:
        return True
    return checked_age >= int(min_age_seconds)


def _collect_funpay_status_candidates(all_orders: List[Dict], recovery: bool = False) -> List[Dict]:
    open_candidates: List[Dict] = []
    closed_candidates: List[Dict] = []

    for order_record in all_orders:
        order_id = str(order_record.get("order_id") or "")
        if not order_id:
            continue

        effective_funpay_status = _get_effective_funpay_status(order_record)
        if effective_funpay_status in ("refunded", "partially_refunded"):
            continue

        if effective_funpay_status in (None, "open"):
            if _funpay_status_needs_recheck(order_record, FUNPAY_STATUS_RECENT_CHECK_SECONDS):
                open_candidates.append(order_record)
            continue

        if recovery and effective_funpay_status == "closed":
            if _funpay_status_needs_recheck(order_record, FUNPAY_STATUS_RECOVERY_CLOSED_SECONDS):
                closed_candidates.append(order_record)

    def _sort_key(order_record: Dict):
        checked_dt = _parse_dt_or_none(order_record.get("funpay_status_checked_at"))
        created_at = int(order_record.get("created_at") or 0)
        return (checked_dt is not None, checked_dt or datetime.min, created_at)

    open_candidates = sorted(open_candidates, key=_sort_key)
    if not recovery:
        return open_candidates

    closed_candidates = sorted(closed_candidates, key=_sort_key)
    combined = open_candidates + closed_candidates
    return combined[:FUNPAY_STATUS_RECOVERY_MAX_ORDERS]


def _run_funpay_status_sync(c: Cardinal, recovery: bool = False):
    try:
        all_orders = orders.load_orders_data()
    except Exception as ex:
        logger.error(f"Ошибка при загрузке orders_data.json для funpay status sync: {ex}")
        return

    candidates = _collect_funpay_status_candidates(all_orders, recovery=recovery)
    if not candidates:
        return

    sync_type = "recovery-check" if recovery else "background funpay sync"
    logger.info(f"FUNPAY STATUS SYNC: {sync_type}, orders={len(candidates)}")

    for order_record in candidates:
        if not RUNNING:
            break

        order_id_funpay = str(order_record.get("order_id"))
        result = _fetch_funpay_status_via_cardinal(c, order_id_funpay)
        checked_at = _now_str()

        if result.get("ok"):
            new_funpay_status = result.get("funpay_status")
            previous_funpay_status = _get_effective_funpay_status(order_record)
            update_fields: Dict[str, Any] = {
                "funpay_status": new_funpay_status,
                "funpay_status_checked_at": checked_at,
            }
            if new_funpay_status in ("refunded", "partially_refunded"):
                update_fields["buyer_flow_state"] = BUYER_STATE_REFUNDED
                update_fields["saved_link"] = None
                update_fields["updated_at"] = _now_str()
                waiting_for_link.pop(str(order_id_funpay), None)
            orders.update_order_fields(order_id_funpay, **update_fields)
            updated_order = orders.get_order_data(order_id_funpay) or order_record
            if new_funpay_status == "refunded" and previous_funpay_status != "refunded":
                _attempt_panel_cancel_after_funpay_refund(updated_order, source=sync_type)
            logger.info(f"FUNPAY STATUS SYNC: order=#{order_id_funpay}, status={new_funpay_status}, source={sync_type}")
            continue

        orders.update_order_fields(order_id_funpay, funpay_status_checked_at=checked_at)
        logger.warning(
            f"FUNPAY STATUS SYNC FAILED: order=#{order_id_funpay}, source={sync_type}, "
            f"error={result.get('error')}, detail={result.get('detail')}"
        )


def _extract_refund_order_id_from_message(message_text: str) -> Optional[str]:
    text_ = normalize_incoming_text(message_text)
    if not text_:
        return None

    for pattern in REFUND_SYNC_PATTERNS:
        match = pattern.search(text_)
        if match:
            return match.group(1)
    return None


def _sync_refund_from_funpay_message(message_text: str) -> bool:
    return _sync_funpay_status_from_message(message_text)


def _mark_order_unknown(order_record: Dict, stop_reason: str, log_detail: str, alert_reason: str = "не удалось подтвердить состояние заказа"):
    order_id_funpay = str(order_record.get("order_id"))
    logger.error(f"ORDER STATUS UNKNOWN: order=#{order_id_funpay}, reason={stop_reason}, detail={log_detail}")
    fields = {
        "status": "unknown",
        "status_stop_reason": stop_reason,
        "status_last_error": stop_reason,
        "status_last_checked_at": _now_str(),
    }
    orders.update_order_fields(order_id_funpay, **fields)
    order_record.update(fields)
    _send_manual_check_alert_once(order_record, alert_reason)


def _apply_status_result(c: Cardinal, order_record: Dict, result: Dict, background_sync: bool = False):
    order_id_funpay = str(order_record.get("order_id"))
    mapped_status = str(result.get("status", "unknown") or "unknown").lower()

    progress_fields: Dict[str, Any] = {"panel_status": mapped_status}
    remains_value = _safe_int(result.get("remains"))
    total_quantity = _safe_int(order_record.get("total_quantity") or order_record.get("quantity")) or 0
    if remains_value is not None:
        progress_fields["left_quantity"] = max(0, remains_value)
        if total_quantity:
            done_quantity = max(0, total_quantity - progress_fields["left_quantity"])
            progress_fields["done_quantity"] = done_quantity
            progress_fields["progress_percent"] = int(max(0, min(100, round((done_quantity / total_quantity) * 100))))
    elif mapped_status == "completed" and total_quantity:
        progress_fields["done_quantity"] = total_quantity
        progress_fields["left_quantity"] = 0
        progress_fields["progress_percent"] = 100

    if mapped_status == "completed":
        progress_fields["buyer_flow_state"] = BUYER_STATE_COMPLETED
    elif mapped_status in ("fail", "canceled", "unknown"):
        progress_fields["buyer_flow_state"] = BUYER_STATE_FAILED
    else:
        progress_fields["buyer_flow_state"] = BUYER_STATE_PANEL_CREATED

    update_fields = {
        "status": mapped_status,
        "status_last_checked_at": _now_str(),
        "status_last_error": None,
        "incorrect_order_id_attempts": 0,
        "updated_at": _now_str(),
        **progress_fields,
    }

    if mapped_status == "unknown":
        update_fields["status_stop_reason"] = "unknown_status"
    else:
        update_fields["status_stop_reason"] = None

    orders.update_order_fields(order_id_funpay, **update_fields)
    order_record.update(update_fields)

    charge_raw = result.get("charge", "0")
    currency = str(result.get("currency", "USD") or "USD")
    try:
        charge = float(charge_raw)
    except Exception:
        charge = 0.0

    try:
        lot_summa = float(order_record.get("summa", 0) or 0)
    except Exception:
        lot_summa = 0.0

    try:
        analytics.update_order_charge_and_net(
            order_id_funpay,
            charge,
            currency=currency,
            net_profit=round(lot_summa - charge, 2),
        )
    except Exception as ex:
        logger.error(f"Не удалось обновить затраты/профит для заказа #{order_id_funpay}: {ex}")

    if mapped_status == "completed":
        _send_completion_notification_if_needed(c, order_record)
    elif mapped_status == "unknown":
        _send_manual_check_alert_once(order_record, "не удалось подтвердить состояние заказа")


def _try_rebind_order_panel(c: Cardinal, order_record: Dict, background_sync: bool = False) -> bool:
    order_id_funpay = str(order_record.get("order_id"))
    current_service_number = int(order_record.get("service_number") or 1)
    twiboost_order_id = order_record.get("id_zakaz")
    cfg = load_config()

    service_numbers = []
    for srv_num in cfg.get("services", {}).keys():
        try:
            service_numbers.append(int(srv_num))
        except Exception:
            continue

    for service_number in sorted(service_numbers):
        if service_number == current_service_number:
            continue

        result = _fetch_single_order_status(service_number, twiboost_order_id)
        if result.get("ok"):
            logger.warning(
                f"ORDER PANEL REBOUND: FunPay order #{order_id_funpay}, panel {current_service_number} -> {service_number}"
            )
            orders.update_order_fields(
                order_id_funpay,
                service_number=service_number,
                incorrect_order_id_attempts=0,
                status_last_error=None,
                status_last_checked_at=_now_str(),
                status_stop_reason=None,
            )
            order_record["service_number"] = service_number
            order_record["incorrect_order_id_attempts"] = 0
            _apply_status_result(c, order_record, result, background_sync=background_sync)
            return True

    attempts = int(order_record.get("incorrect_order_id_attempts", 0) or 0) + 1
    fields = {
        "incorrect_order_id_attempts": attempts,
        "status_last_error": "incorrect_order_id",
        "status_last_checked_at": _now_str(),
    }

    if attempts >= INCORRECT_ORDER_ID_MAX_ATTEMPTS:
        fields.update({
            "status": "unknown",
            "status_stop_reason": "incorrect_order_id",
        })
        orders.update_order_fields(order_id_funpay, **fields)
        order_record.update(fields)
        logger.warning(
            f"ORDER STATUS STOPPED: order #{order_id_funpay} переведён в unknown после {attempts} попыток lookup по incorrect order id"
        )
        _send_manual_check_alert_once(order_record, "не удалось подтвердить состояние заказа")
    else:
        orders.update_order_fields(order_id_funpay, **fields)
        order_record.update(fields)
        logger.warning(
            f"ORDER REBIND RETRY: order #{order_id_funpay}, попытка {attempts}/{INCORRECT_ORDER_ID_MAX_ATTEMPTS}"
        )

    return False


def _sync_single_order_status(c: Cardinal, order_record: Dict, background_sync: bool = False):
    if not order_record:
        return
    if _is_order_refundedish(order_record):
        return
    if str(order_record.get("status", "unknown") or "unknown").lower() != "pending":
        return

    twiboost_order_id = order_record.get("id_zakaz")
    service_number = int(order_record.get("service_number") or 1)
    order_id_funpay = str(order_record.get("order_id"))

    result = _fetch_single_order_status(service_number, twiboost_order_id)
    if result.get("ok"):
        _apply_status_result(c, order_record, result, background_sync=background_sync)
        return

    error_code = result.get("error", "unknown")
    detail = result.get("detail", "")
    logger.warning(f"STATUS CHECK RESULT: order=#{order_id_funpay}, service=#{service_number}, error={error_code}, detail={detail}")

    if error_code == "incorrect_order_id":
        _try_rebind_order_panel(c, order_record, background_sync=background_sync)
        return

    if error_code in ("network_error", "http_error", "invalid_json", "user_inactive", "service_inactive", "insufficient_funds", "unsupported_action"):
        orders.update_order_fields(
            order_id_funpay,
            status_last_error=error_code,
            status_last_checked_at=_now_str(),
        )
        return

    _mark_order_unknown(order_record, "status_unknown", detail)


def _is_batch_transport_error(error_code: Any) -> bool:
    return str(error_code or "").strip().lower() in ("timeout", "network_error", "invalid_json", "http_error", "unknown")


def _save_batch_failure_for_chunk(chunk: List[Dict], error_code: Any, detail: Any):
    batch_error = f"batch_{str(error_code or 'unknown').strip().lower() or 'unknown'}"
    checked_at = _now_str()
    for order_record in chunk:
        order_id_funpay = str(order_record.get("order_id") or "")
        if not order_id_funpay:
            continue
        orders.update_order_fields(
            order_id_funpay,
            status_last_error=batch_error,
            status_last_checked_at=checked_at,
        )
        order_record["status_last_error"] = batch_error
        order_record["status_last_checked_at"] = checked_at
    logger.warning(f"BATCH STATUS SOFT FAIL: error={batch_error}, detail={detail}")


def _check_pending_orders_batch(c: Cardinal):
    try:
        all_orders = orders.load_orders_data()
    except Exception as ex:
        logger.error(f"Ошибка при загрузке orders_data.json для batch status: {ex}")
        return

    pending_orders = []
    for order_record in all_orders:
        status_ = str(order_record.get("status", "unknown") or "unknown").lower()
        if status_ == "pending" and not _is_order_refundedish(order_record):
            pending_orders.append(order_record)

    if not pending_orders:
        return

    grouped: Dict[int, List[Dict]] = {}
    for order_record in pending_orders:
        service_number = int(order_record.get("service_number") or 1)
        grouped.setdefault(service_number, []).append(order_record)

    for service_number, orders_group in grouped.items():
        for start_idx in range(0, len(orders_group), STATUS_BATCH_SIZE):
            chunk = orders_group[start_idx:start_idx + STATUS_BATCH_SIZE]
            order_ids = [int(order_record.get("id_zakaz")) for order_record in chunk if _safe_int(order_record.get("id_zakaz")) is not None]
            if not order_ids:
                continue

            result = _fetch_batch_order_status(service_number, order_ids)
            if not result.get("ok"):
                error_code = result.get("error", "unknown")
                detail = result.get("detail", "")

                if _is_batch_transport_error(error_code):
                    logger.warning(
                        f"BATCH STATUS RETRY: service=#{service_number}, orders={order_ids}, error={error_code}, detail={detail}"
                    )
                    retry_result = _fetch_batch_order_status(service_number, order_ids)
                    if not retry_result.get("ok"):
                        _save_batch_failure_for_chunk(chunk, retry_result.get("error", error_code), retry_result.get("detail", detail))
                        continue
                    result = retry_result
                else:
                    _save_batch_failure_for_chunk(chunk, error_code, detail)
                    continue

            entries = result.get("entries", {}) or {}
            for order_record in chunk:
                order_key = str(order_record.get("id_zakaz"))
                entry = entries.get(order_key)
                if entry is None:
                    logger.warning(
                        f"BATCH STATUS MISSING ENTRY: service=#{service_number}, order={order_key}. Перехожу на single status."
                    )
                    _sync_single_order_status(c, order_record, background_sync=True)
                    continue

                entry_result = _build_status_result_from_entry(entry)
                if entry_result.get("ok"):
                    _apply_status_result(c, order_record, entry_result, background_sync=True)
                    continue

                entry_error = entry_result.get("error", "unknown")
                logger.warning(
                    f"BATCH STATUS ENTRY ERROR: order={order_record.get('order_id')}, service=#{service_number}, error={entry_error}, detail={entry_result.get('detail')}"
                )

                if entry_error == "incorrect_order_id":
                    _try_rebind_order_panel(c, order_record, background_sync=True)
                    continue

                if entry_error in ("network_error", "http_error", "invalid_json", "user_inactive", "service_inactive", "insufficient_funds", "unsupported_action"):
                    orders.update_order_fields(
                        str(order_record.get("order_id")),
                        status_last_error=entry_error,
                        status_last_checked_at=_now_str(),
                    )
                    continue

                _mark_order_unknown(order_record, "status_unknown", entry_result.get("detail", ""))


def refund_order(
    c: Cardinal,
    order_id_funpay: str,
    buyer_chat_id: int,
    reason: str,
    detailed_reason: str = None,
    bot_action: Optional[str] = None,
    admin_action: Optional[str] = None,
    success_title: str = "↩️ Выполнен возврат средств",
    failure_title: str = "⚠️ Заказ требует ручной проверки",
):
    """
    Обработка возврата средств с короткими admin alerts и полными техническими деталями только в логах.
    """
    cfg = load_config()
    auto_refunds = cfg.get("auto_refunds", True)

    if detailed_reason is None:
        detailed_reason = reason

    order_data = orders.get_order_data(order_id_funpay)
    if order_data and _is_order_refundedish(order_data):
        logger.info(f"Заказ #{order_id_funpay} уже был возвращен. Пропуск.")
        return

    success_bot_action = (
        f"{bot_action}; затем возврат выполнен автоматически" if bot_action else "возврат выполнен автоматически"
    )
    success_admin_action = "дополнительных действий не требуется"
    failure_bot_action = bot_action or "автоматический возврат не выполнен"
    failure_admin_action = admin_action or "выполните возврат вручную"

    if auto_refunds:
        try:
            c.account.refund(order_id_funpay)
            c.send_message(buyer_chat_id, f"❌ Ваши средства возвращены по причине: {reason}")
            _send_admin_order_alert(
                order_id_funpay=order_id_funpay,
                reason=reason,
                bot_action=success_bot_action,
                action=success_admin_action,
                title=success_title,
            )
            logger.info(
                f"Заказ #{order_id_funpay} был отменён (refund) для покупателя {buyer_chat_id}. "
                f"Причина: {reason}. Технические детали: {detailed_reason}"
            )
            orders.update_order_fields(order_id_funpay, funpay_status="refunded", buyer_flow_state=BUYER_STATE_REFUNDED, saved_link=None, refund_detected_at=_now_str(), refund_source="auto_refund", updated_at=_now_str())
            waiting_for_link.pop(str(order_id_funpay), None)
        except Exception as ex:
            logger.error(
                f"Не удалось вернуть средства для заказа #{order_id_funpay}: {ex}. "
                f"Технические детали: {detailed_reason}"
            )
            orders.update_order_fields(order_id_funpay, buyer_flow_state=BUYER_STATE_FAILED, updated_at=_now_str())
            _send_admin_order_alert(
                order_id_funpay=order_id_funpay,
                reason=reason,
                bot_action=failure_bot_action,
                action=failure_admin_action,
                title=failure_title,
            )
    else:
        logger.warning(f"Для заказа #{order_id_funpay} требуется ручной возврат. Технические детали: {detailed_reason}")
        orders.update_order_fields(order_id_funpay, buyer_flow_state=BUYER_STATE_FAILED, updated_at=_now_str())
        _send_admin_order_alert(
            order_id_funpay=order_id_funpay,
            reason=reason,
            bot_action=bot_action or "автоматический возврат отключён",
            action=admin_action or "выполните возврат вручную",
            title=failure_title,
        )


def check_order_status(
    c: Cardinal,
    twiboost_order_id: int,
    buyer_chat_id: int,
    link: str,
    order_id_funpay: str,
    attempt: int = 1
):
    """
    Одноразовая синхронизация статуса заказа. Дальнейший мониторинг выполняется batch-потоком.
    """
    order_record = orders.get_order_data(order_id_funpay)
    if not order_record:
        logger.warning(f"Заказ {order_id_funpay} не найден при check_order_status.")
        return

    _sync_single_order_status(c, order_record)


def start_order_checking(c: Cardinal):
    logger.info("Запуск batch status мониторинга pending-заказов")
    _record_plugin_start_state()

    try:
        _run_funpay_status_sync(c, recovery=True)
    except Exception as ex:
        logger.error(f"Ошибка в recovery-check funpay status: {ex}")

    last_funpay_status_sync_ts = time.time()

    while RUNNING:
        try:
            _check_pending_orders_batch(c)
        except Exception as ex:
            logger.error(f"Ошибка в batch status мониторинге: {ex}")

        try:
            if time.time() - last_funpay_status_sync_ts >= FUNPAY_STATUS_SYNC_INTERVAL_SECONDS:
                _run_funpay_status_sync(c, recovery=False)
                last_funpay_status_sync_ts = time.time()
        except Exception as ex:
            logger.error(f"Ошибка в background funpay status sync: {ex}")

        for _ in range(STATUS_BATCH_INTERVAL_SECONDS):
            if not RUNNING:
                break
            time.sleep(1)


def _find_lot_matches_by_description(description: Any) -> List[Tuple[str, Dict]]:
    description_text = str(description or "")
    matches: List[Tuple[str, Dict]] = []
    for lot_key, lot_data in (lot_mapping or {}).items():
        if not isinstance(lot_data, dict):
            continue
        lot_name = str(lot_data.get("name") or "").strip()
        if not lot_name:
            continue
        try:
            if re.search(re.escape(lot_name), description_text, re.IGNORECASE):
                matches.append((str(lot_key), lot_data))
        except Exception:
            continue
    return matches


def resolve_lot_by_description(description: str, order_amount: int) -> Dict[str, Any]:
    matches = _find_lot_matches_by_description(description)
    if not matches:
        logger.warning("LOT MATCH FAILED: no matches for description=%r", str(description or "")[:300])
        return {"ok": False, "error": "no_match", "matches": []}

    if len(matches) > 1:
        safe_matches = []
        for lot_key, lot_data in matches:
            safe_matches.append({
                "lot_key": lot_key,
                "name": lot_data.get("name"),
                "service_id": lot_data.get("service_id"),
                "quantity": lot_data.get("quantity"),
                "service_number": lot_data.get("service_number", 1),
            })
        logger.warning(
            "LOT MATCH AMBIGUOUS: found %s matches for description=%r",
            len(matches),
            str(description or "")[:300],
        )
        return {"ok": False, "error": "ambiguous_match", "matches": safe_matches}

    lot_key, lot_data = matches[0]
    try:
        base_q = int(lot_data.get("quantity") or 0)
    except Exception:
        base_q = 0
    try:
        amount = int(order_amount or 1)
    except Exception:
        amount = 1
    real_q = base_q * amount
    return {
        "ok": True,
        "service_id": lot_data.get("service_id"),
        "real_amount": real_q,
        "service_number": lot_data.get("service_number", 1),
        "lot_key": lot_key,
        "lot_title": lot_data.get("name") or str(description or ""),
    }


def _is_lot_match_no_match(result: Dict[str, Any]) -> bool:
    return not bool(result.get("ok")) and str(result.get("error") or "") == "no_match"


def _is_lot_match_ambiguous(result: Dict[str, Any]) -> bool:
    return not bool(result.get("ok")) and str(result.get("error") or "") == "ambiguous_match"


def get_tg_id_by_description(description: str, order_amount: int) -> Tuple[int, int, int] | None:
    result = resolve_lot_by_description(description, order_amount)
    if not result.get("ok"):
        return None
    return result.get("service_id"), result.get("real_amount"), result.get("service_number", 1)


def _send_lot_match_manual_alert(order_id_funpay: Any, description: Any, resolve_result: Dict[str, Any]):
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id or bot is None:
        return

    error_code = str(resolve_result.get("error") or "unknown")
    if error_code == "ambiguous_match":
        lines = [
            "⚠️ AutoSMM: найдено несколько подходящих лотов",
            "",
            f"Заказ FunPay: #{order_id_funpay}",
            "Описание заказа:",
            str(description or "—"),
            "",
            "AutoSMM не может безопасно выбрать услугу панели.",
            "",
            "Совпадения:",
        ]
        for idx, item in enumerate(resolve_result.get("matches") or [], start=1):
            lines.append(
                f"{idx}. {item.get('lot_key')} — {item.get('name') or '—'} | "
                f"ID услуги: {item.get('service_id') or '—'} | "
                f"Кол-во: {item.get('quantity') or '—'} | "
                f"Панель: {item.get('service_number') or '—'}"
            )
        lines.extend(["", "Действие:", "проверьте привязку лота вручную."])
    else:
        lines = [
            "⚠️ AutoSMM: не удалось определить лот",
            "",
            f"Заказ FunPay: #{order_id_funpay}",
            "Описание заказа:",
            str(description or "—"),
            "",
            "AutoSMM не нашёл подходящий лот в настройках.",
            "",
            "Действие:",
            "проверьте привязки лотов вручную.",
        ]

    try:
        bot.send_message(notification_chat_id, "\n".join(lines))
    except Exception as ex:
        logger.error(f"Не удалось отправить alert по lot matching для заказа #{order_id_funpay}: {ex}")


def _extract_http_link(text_value: Any) -> Optional[str]:
    match = BUYER_LINK_RE.search(str(text_value or ""))
    return match.group(1).strip() if match else None


def _looks_like_link_without_https(text_value: Any) -> bool:
    text = str(text_value or "").strip()
    if not text or _extract_http_link(text):
        return False
    if re.search(r"(?i)\bwww\.[a-z0-9.-]+\.[a-z]{2,}\S*", text):
        return True
    if re.search(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*", text):
        return True
    if re.fullmatch(r"@[A-Za-z0-9_.-]{3,}", text):
        return True
    return False


def is_valid_link(link: str) -> Tuple[bool, str]:
    raw_link = str(link or "").strip()
    if not raw_link.startswith("https://"):
        return False, INVALID_LINK_SCHEME_TEXT

    try:
        parsed = urlparse(raw_link)
    except Exception:
        return False, "❌ Не удалось прочитать ссылку."

    hostname = (parsed.hostname or "").strip().lower().strip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname:
        return False, "❌ Не удалось определить домен ссылки."

    for allowed_domain in load_valid_links():
        domain = _normalize_allowed_domain(allowed_domain)
        if not domain:
            continue
        if hostname == domain or hostname.endswith("." + domain):
            return True, f"✅ Ссылка корректна ({domain})."

    return False, INVALID_LINK_DOMAIN_TEXT



# =========================================================
# BUYER FLOW / RECOVERY
# =========================================================

class _SafeTemplateDict(dict):
    def __missing__(self, key):
        return ""


def split_message_by_lines(text, max_lines: int = 18) -> List[str]:
    max_lines = max(1, int(max_lines or 18))
    lines = str(text or "").splitlines()
    if not lines:
        return [""]
    chunks: List[str] = []
    current: List[str] = []
    for line in lines:
        if len(current) >= max_lines:
            chunks.append("\n".join(current).rstrip())
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current).rstrip())
    return [chunk for chunk in chunks if chunk != ""] or [""]


def _send_buyer_message(c: Cardinal, chat_id: int, text: str, max_lines: int = 18):
    for chunk in split_message_by_lines(text, max_lines=max_lines):
        c.send_message(chat_id, chunk)


def _order_ref_candidates(raw_ref: Any) -> List[str]:
    value = str(raw_ref or "").strip().lstrip("#")
    if not value:
        return []
    candidates = [value]
    if value.lower().startswith("fp") and len(value) > 2:
        candidates.append(value[2:])
    return [candidate.upper() for candidate in candidates if candidate]


def _matches_order_ref(order_id: Any, raw_ref: Any) -> bool:
    order_value = str(order_id or "").strip().lstrip("#").upper()
    return bool(order_value and order_value in _order_ref_candidates(raw_ref))


def _buyer_selection_key(buyer_id: Any, chat_id: Any) -> str:
    return f"buyer:{buyer_id}" if buyer_id not in (None, "") else f"chat:{chat_id}"


def _get_panel_order_id(order_record: Optional[Dict]) -> Optional[int]:
    if not isinstance(order_record, dict):
        return None
    for field_name in ("panel_order_id", "twiboost_id", "id_zakaz"):
        value = _safe_int(order_record.get(field_name))
        if value is not None:
            return value
    return None


def _has_panel_order(order_record: Optional[Dict]) -> bool:
    return _get_panel_order_id(order_record) is not None


def _state_from_step(step_value: Any) -> str:
    step = str(step_value or "").strip().lower()
    if step == "await_confirm":
        return BUYER_STATE_WAITING_CONFIRM
    if step in ("paused", "link_paused"):
        return BUYER_STATE_LINK_PAUSED
    return BUYER_STATE_WAITING_LINK


def _step_from_state(state_value: Any) -> str:
    state = str(state_value or "").strip().lower()
    if state == BUYER_STATE_WAITING_CONFIRM:
        return "await_confirm"
    if state == BUYER_STATE_LINK_PAUSED:
        return "paused"
    return "await_link"


def _get_buyer_flow_state(order_record: Optional[Dict]) -> str:
    if not isinstance(order_record, dict):
        return BUYER_STATE_WAITING_LINK
    if _is_order_refundedish(order_record):
        return BUYER_STATE_REFUNDED

    explicit_state = str(order_record.get("buyer_flow_state") or "").strip().lower()
    panel_status = str(order_record.get("status") or order_record.get("panel_status") or "").strip().lower()

    if _has_panel_order(order_record):
        if panel_status == "completed":
            return BUYER_STATE_COMPLETED
        if panel_status in ("fail", "failed", "canceled", "cancelled", "unknown"):
            return BUYER_STATE_FAILED
        return BUYER_STATE_PANEL_CREATED

    if explicit_state in {
        BUYER_STATE_WAITING_LINK,
        BUYER_STATE_LINK_PAUSED,
        BUYER_STATE_WAITING_CONFIRM,
        BUYER_STATE_PANEL_CREATED,
        BUYER_STATE_COMPLETED,
        BUYER_STATE_FAILED,
        BUYER_STATE_REFUNDED,
    }:
        return explicit_state
    return _state_from_step(order_record.get("step"))


def _extract_progress_values(order_record: Optional[Dict]) -> Dict[str, Any]:
    order_record = order_record or {}
    total = _safe_int(order_record.get("total_quantity") or order_record.get("quantity") or order_record.get("real_amount") or order_record.get("order_amount")) or 0
    done = _safe_int(order_record.get("done_quantity"))
    left = _safe_int(order_record.get("left_quantity"))
    if left is None:
        left = _safe_int(order_record.get("remains"))
    if done is None and total and left is not None:
        done = max(0, total - max(0, left))
    if left is None and total and done is not None:
        left = max(0, total - max(0, done))
    if done is None:
        done = 0
    if left is None:
        left = max(0, total - done) if total else 0
    progress_percent = int(max(0, min(100, round((done / total) * 100)))) if total > 0 else 0
    return {"total_quantity": total, "done_quantity": done, "left_quantity": left, "progress_percent": progress_percent}


def _build_template_context(order_record: Optional[Dict] = None, extra: Optional[Dict] = None) -> Dict[str, Any]:
    merged = {**dict(order_record or {}), **dict(extra or {})}
    progress = _extract_progress_values(merged)
    panel_order_id = merged.get("panel_order_id") or merged.get("twiboost_id") or merged.get("id_zakaz") or ""
    lot_title = merged.get("lot_title") or merged.get("service_name") or merged.get("orderDesc") or ""
    order_price = merged.get("order_price") or merged.get("summa") or merged.get("orderPrice") or ""
    order_amount = merged.get("order_amount") or merged.get("amount") or merged.get("orderAmount") or ""
    ctx = {
        "buyer_username": merged.get("buyer_username") or "",
        "lot_title": lot_title,
        "order_id_funpay": merged.get("order_id_funpay") or merged.get("order_id") or "",
        "order_price": order_price,
        "order_amount": order_amount,
        "currency": merged.get("currency") or "RUB",
        "link": merged.get("link") or merged.get("saved_link") or merged.get("customer_url") or "",
        "panel_order_id": panel_order_id,
        "panel_status": merged.get("panel_status") or merged.get("status") or "",
        "funpay_status": _get_effective_funpay_status(merged) or merged.get("funpay_status") or "",
        **progress,
    }
    ctx["orderDesc"] = ctx["lot_title"]
    ctx["orderPrice"] = ctx["order_price"]
    ctx["orderAmount"] = ctx["order_amount"]
    ctx["twiboost_id"] = ctx["panel_order_id"]
    return {key: (0 if value is None and key in ("total_quantity", "done_quantity", "left_quantity", "progress_percent") else ("" if value is None else value)) for key, value in ctx.items()}


def _render_template(template_text: str, order_record: Optional[Dict] = None, extra: Optional[Dict] = None) -> str:
    try:
        return str(template_text or "").format_map(_SafeTemplateDict(_build_template_context(order_record, extra)))
    except Exception as ex:
        logger.error(f"Ошибка форматирования шаблона AutoSMM: {ex}")
        return str(template_text or "")


def _upsert_buyer_flow_order_record(chat_id: int, buyer_id: Any, buyer_username: Any, order_id_funpay: str, lot_title: str, order_price: Any, order_amount: Any, service_id: int, real_amount: int, service_number: int, funpay_status: str = "open", state: str = BUYER_STATE_WAITING_LINK, saved_link: Optional[str] = None):
    orders_list = _load_orders_data_raw()
    order_id_str = str(order_id_funpay)
    existing = next((order for order in orders_list if str(order.get("order_id")) == order_id_str), None)
    base = existing if isinstance(existing, dict) else {}
    created_at = int(base.get("created_at") or time.time())
    record_update = {
        "chat_id": chat_id,
        "buyer_id": buyer_id,
        "buyer_username": buyer_username or "",
        "order_id": order_id_str,
        "lot_title": lot_title or base.get("lot_title") or base.get("service_name") or "",
        "service_name": lot_title or base.get("service_name") or "—",
        "service_id": service_id,
        "quantity": int(real_amount or 0),
        "total_quantity": int(real_amount or 0),
        "order_amount": order_amount,
        "order_price": order_price,
        "service_number": service_number,
        "funpay_status": _normalize_funpay_status(funpay_status) or "open",
        "spent": float(base.get("spent", 0.0) or 0.0),
        "summa": float(order_price or base.get("summa", 0) or 0),
        "currency": str(base.get("currency", "RUB") or "RUB"),
        "created_at": created_at,
        "completed_notification_sent": bool(base.get("completed_notification_sent", False)),
    }
    if not _has_panel_order(base):
        record_update["id_zakaz"] = base.get("id_zakaz")
        record_update["status"] = base.get("status") or "pending"
        record_update["customer_url"] = saved_link or base.get("customer_url") or ""
    else:
        record_update["id_zakaz"] = base.get("id_zakaz")
        record_update["status"] = base.get("status", "pending")
        record_update["customer_url"] = base.get("customer_url", saved_link or "")
    if existing is None:
        orders_list.append(record_update)
    else:
        existing.update(record_update)
    save_orders_data(orders_list)
    orders.update_order_fields(order_id_str, buyer_flow_state=state, saved_link=saved_link, total_quantity=int(real_amount or 0), done_quantity=0, left_quantity=int(real_amount or 0), progress_percent=0, last_prompt_ts=_now_str(), updated_at=_now_str())


def _save_waiting_context(data: Dict, state: Optional[str] = None, saved_link_marker: Any = "__keep__"):
    if not isinstance(data, dict):
        return
    order_id = str(data.get("order_id_funpay") or data.get("order_id") or "")
    if not order_id:
        return
    if state:
        data["state"] = state
        data["step"] = _step_from_state(state)
    else:
        state = data.get("state") or _state_from_step(data.get("step"))
    fields: Dict[str, Any] = {"buyer_flow_state": state, "updated_at": _now_str()}
    if saved_link_marker != "__keep__":
        fields["saved_link"] = saved_link_marker
        if saved_link_marker in (None, ""):
            data.pop("link", None)
        else:
            data["link"] = saved_link_marker
    elif data.get("link"):
        fields["saved_link"] = data.get("link")
    orders.update_order_fields(order_id, **fields)


def _build_waiting_context_from_order(order_record: Dict) -> Optional[Dict[str, Any]]:
    if not isinstance(order_record, dict):
        return None
    order_id = str(order_record.get("order_id") or "")
    if not order_id:
        return None
    state = _get_buyer_flow_state(order_record)
    return {
        "buyer_id": order_record.get("buyer_id"),
        "chat_id": order_record.get("chat_id"),
        "buyer_username": order_record.get("buyer_username") or "",
        "service_id": order_record.get("service_id"),
        "real_amount": int(order_record.get("quantity") or order_record.get("total_quantity") or 0),
        "order_id_funpay": order_id,
        "price": order_record.get("summa") or order_record.get("order_price") or 0,
        "service_number": order_record.get("service_number", 1),
        "lot_title": order_record.get("lot_title") or order_record.get("service_name") or "",
        "state": state,
        "step": _step_from_state(state),
        "link": order_record.get("saved_link") or order_record.get("customer_url") or "",
    }


def _set_selected_order_for_buyer(buyer_id: Any, chat_id: Any, order_id: Any):
    if order_id not in (None, ""):
        BUYER_ORDER_SELECTION[_buyer_selection_key(buyer_id, chat_id)] = str(order_id)


def _clear_selected_order_for_buyer(buyer_id: Any, chat_id: Any):
    BUYER_ORDER_SELECTION.pop(_buyer_selection_key(buyer_id, chat_id), None)


def _get_buyer_orders(buyer_id: Any, chat_id: Any) -> List[Dict]:
    result: List[Dict] = []
    for order_record in orders.load_orders_data():
        if not isinstance(order_record, dict):
            continue
        buyer_match = buyer_id not in (None, "") and str(order_record.get("buyer_id")) == str(buyer_id)
        chat_match = chat_id not in (None, "") and str(order_record.get("chat_id")) == str(chat_id)
        if buyer_match or chat_match:
            result.append(order_record)
    result.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    return result


def _format_buyer_lot_title(order_record: Dict, max_len: int = 70) -> str:
    """Return a short, buyer-facing lot title without long FunPay sales noise."""
    if not isinstance(order_record, dict):
        return "SMM-заказ"
    raw_title = (
        order_record.get("lot_title")
        or order_record.get("service_name")
        or order_record.get("orderDesc")
        or "SMM-заказ"
    )
    raw_title = str(raw_title or "SMM-заказ").strip()
    normalized = unicodedata.normalize("NFKC", raw_title)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    low = normalized.lower()

    platform_patterns = [
        ("TikTok", ("tiktok", "tik tok", "t i k t o k", "тик ток")),
        ("Instagram", ("instagram", "инст")),
        ("Telegram", ("telegram", "телеграм", "тг")),
        ("YouTube", ("youtube", "ютуб")),
        ("VK", ("vk", "вк")),
        ("X", ("twitter", "x.com")),
        ("Facebook", ("facebook", "fb")),
        ("Twitch", ("twitch",)),
    ]
    platform = None
    for name, needles in platform_patterns:
        if any(needle in low for needle in needles):
            platform = name
            break

    tail = ""
    if "," in normalized:
        parts = [part.strip(" ,;:-") for part in normalized.split(",") if part.strip(" ,;:-")]
        if len(parts) >= 2:
            tail = ", ".join(parts[1:]).strip()

    def _clean(value: str) -> str:
        value = unicodedata.normalize("NFKC", str(value or ""))
        value = re.sub(r"[♻✅🤖💙❤️🔥⭐✨]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" -—,;:")
        return value

    tail = _clean(tail)
    if platform and tail:
        result = f"{platform} — {tail}"
    elif platform:
        cleaned = _clean(normalized)
        # Remove a noisy platform token from the beginning when possible.
        cleaned = re.sub(r"(?i)^(t\s*i\s*k\s*t\s*o\s*k|tiktok|tik tok|instagram|telegram|youtube|twitter|facebook|twitch|vk|вк|ютуб|телеграм|тик ток)\s*[-—:,]*\s*", "", cleaned).strip()
        result = f"{platform} — {cleaned}" if cleaned else f"{platform} SMM-заказ"
    else:
        result = _clean(normalized) or "SMM-заказ"

    if len(result) > max_len:
        result = result[: max(1, max_len - 1)].rstrip() + "…"
    return result


def _buyer_status_label(order_record: Dict) -> str:
    state = _get_buyer_flow_state(order_record)
    if state == BUYER_STATE_WAITING_LINK:
        return "ожидает ссылку"
    if state == BUYER_STATE_LINK_PAUSED:
        return "ожидание ссылки приостановлено"
    if state == BUYER_STATE_WAITING_CONFIRM:
        return "ожидает подтверждение ссылки"
    if state == BUYER_STATE_COMPLETED:
        return "выполнен"
    if state == BUYER_STATE_FAILED:
        return "требует проверки"
    if state == BUYER_STATE_REFUNDED:
        return "недоступен после возврата"
    if _has_panel_order(order_record):
        return "в работе"
    return "ожидает ссылку"


def _buyer_action_label(order_record: Dict) -> str:
    state = _get_buyer_flow_state(order_record)
    order_id = str(order_record.get("order_id") or "").lstrip("#")
    panel_order_id = _get_panel_order_id(order_record)
    if state == BUYER_STATE_WAITING_LINK:
        return "отправьте ссылку"
    if state == BUYER_STATE_LINK_PAUSED:
        return f"напишите !ссылка {order_id}, когда будете готовы" if order_id else "напишите !ссылка, когда будете готовы"
    if state == BUYER_STATE_WAITING_CONFIRM:
        return "подтвердите ссылку: + или -"
    if state == BUYER_STATE_REFUNDED:
        return "недоступны после возврата"
    if state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED) or _has_panel_order(order_record):
        return f"чек {panel_order_id}" if panel_order_id else "ожидайте выполнение"
    if state == BUYER_STATE_FAILED:
        return "продавец проверит заказ вручную"
    return "отправьте ссылку"


def _format_short_order_list(records: List[Dict], command_name: str = "!ссылка") -> str:
    command_name = str(command_name or "!ссылка").strip() or "!ссылка"
    lines = ["📦 У вас несколько SMM-заказов", "", "Выберите нужный заказ командой:", ""]
    shown = 0
    for order_record in records[:5]:
        order_id = str(order_record.get("order_id") or "").strip().lstrip("#")
        if not order_id:
            continue
        shown += 1
        lines.append(f"• #{order_id} — {_format_buyer_lot_title(order_record)}")
        lines.append(f"  Команда: {command_name} {order_id}")
        lines.append(f"  Статус: {_buyer_status_label(order_record)}")
        lines.append("")
    if not shown:
        return "Заказ не найден."
    lines.extend(["Чтобы посмотреть все заказы:", "!смм"])
    return "\n".join(lines).strip()


def _format_order_not_found_for_buyer(raw_ref: Any = None, records: Optional[List[Dict]] = None, command_name: str = "!ссылка") -> str:
    ref = str(raw_ref or "").strip().lstrip("#")
    active = [
        r for r in (records or [])
        if _get_buyer_flow_state(r) in BUYER_ACTIVE_PRE_PANEL_STATES
        and not _has_panel_order(r)
        and not _is_order_refundedish(r)
    ]
    if ref and len(active) == 1 and command_name in ("!ссылка", "!отмена"):
        real_order_id = str(active[0].get("order_id") or "").strip().lstrip("#")
        if real_order_id:
            return (
                f"Заказ #{ref} не найден.\n\n"
                f"У вас есть активный заказ:\n"
                f"#{real_order_id} — {_format_buyer_lot_title(active[0])}\n\n"
                f"Чтобы продолжить:\n{command_name} {real_order_id}"
            )
    return ORDER_NOT_FOUND_TEXT

def _resolve_buyer_order_for_action(c: Cardinal, buyer_id: Any, chat_id: Any, raw_ref: Optional[str], command_name: str) -> Optional[Dict]:
    records = _get_buyer_orders(buyer_id, chat_id)
    if raw_ref:
        for order_record in records:
            if _matches_order_ref(order_record.get("order_id"), raw_ref):
                return order_record
        _send_buyer_message(c, chat_id, _format_order_not_found_for_buyer(raw_ref, records, command_name=command_name))
        return None

    active = [
        r for r in records
        if _get_buyer_flow_state(r) in BUYER_ACTIVE_PRE_PANEL_STATES
        and not _has_panel_order(r)
        and not _is_order_refundedish(r)
    ]
    if command_name in ("!ссылка", "!отмена"):
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            _send_buyer_message(c, chat_id, _format_short_order_list(active, command_name=command_name))
            return None
        _send_buyer_message(c, chat_id, ORDER_NOT_FOUND_TEXT)
        return None

    if len(records) == 1:
        return records[0]
    if records:
        _send_buyer_message(c, chat_id, _format_short_order_list(records, command_name=command_name))
        return None
    _send_buyer_message(c, chat_id, "У вас пока нет SMM-заказов.")
    return None



def _human_funpay_status(status_value: Any) -> str:
    return {"open": "оплачен", "closed": "закрыт", "refunded": "возврат", "partially_refunded": "частичный возврат"}.get(_normalize_funpay_status(status_value), "неизвестно")


def _human_panel_status(order_record: Dict) -> str:
    state = _get_buyer_flow_state(order_record)
    if state in (BUYER_STATE_WAITING_LINK, BUYER_STATE_LINK_PAUSED, BUYER_STATE_WAITING_CONFIRM, BUYER_STATE_FAILED, BUYER_STATE_REFUNDED, BUYER_STATE_COMPLETED):
        return _buyer_status_label(order_record)
    status = str(order_record.get("panel_status") or order_record.get("status") or "").strip().lower()
    return {"pending": "в работе", "completed": "выполнен", "partial": "частично", "canceled": "отменён", "cancelled": "отменён", "fail": "требует проверки", "failed": "требует проверки", "unknown": "требует проверки"}.get(status, "в работе" if _has_panel_order(order_record) else "ожидает ссылку")


def _format_created_at(order_record: Dict) -> str:
    created_at = _safe_int(order_record.get("created_at"))
    if created_at is not None:
        try:
            return datetime.fromtimestamp(created_at).strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass
    return str(order_record.get("date") or order_record.get("date_str") or "—")


def _format_buyer_progress(order_record: Dict) -> str:
    progress = _extract_progress_values(order_record)
    total = _safe_int(progress.get("total_quantity")) or 0
    done = _safe_int(progress.get("done_quantity")) or 0
    if total > 0:
        return f"{done}/{total}"
    return "ожидается"


def _format_buyer_smm_orders(records: List[Dict]) -> str:
    if not records:
        return "У вас пока нет SMM-заказов."
    lines = ["📂 Ваши последние SMM-заказы", "━━━━━━━━━━━━━━━━━━━━━"]
    for idx, order_record in enumerate(records[:5]):
        if idx > 0:
            lines.extend(["", "────────────────────", ""])
        order_id = str(order_record.get("order_id") or "—").lstrip("#")
        panel_order_id = _get_panel_order_id(order_record)
        state = _get_buyer_flow_state(order_record)
        lines.append(f"🧾 Заказ: #{order_id}")
        lines.append(f"├ 📦 Лот: {_format_buyer_lot_title(order_record)}")
        lines.append(f"├ 📅 Дата: {_format_created_at(order_record)}")
        lines.append(f"├ ✅ FunPay: {_human_funpay_status(_get_effective_funpay_status(order_record))}")
        lines.append(f"├ 🏁 Статус: {_buyer_status_label(order_record)}")
        lines.append(f"├ 📊 Прогресс: {_format_buyer_progress(order_record)}")
        if state == BUYER_STATE_WAITING_LINK:
            lines.append("└ ✏️ Действие: отправьте ссылку")
        elif state == BUYER_STATE_LINK_PAUSED:
            lines.append(f"└ ✏️ Действие: напишите !ссылка {order_id}, когда будете готовы")
        elif state == BUYER_STATE_WAITING_CONFIRM:
            lines.append("└ ✏️ Действие: подтвердите ссылку: + или -")
        elif state == BUYER_STATE_REFUNDED:
            lines.append("└ 🔒 Действия: недоступны после возврата")
        elif state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED) or _has_panel_order(order_record):
            if panel_order_id:
                lines.append(f"├ 🔍 Проверить: чек {panel_order_id}")
                lines.append(f"└ 🔄 Рефилл: рефилл {panel_order_id}")
            else:
                lines.append("└ ✏️ Действие: ожидайте выполнение")
        else:
            lines.append("└ ⚠️ Действие: продавец проверит заказ вручную")
    return "\n".join(lines).strip()


def _handle_buyer_smm_command(c: Cardinal, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    if not re.match(r"^!(?:смм|smm)\s*$", msg_text.strip(), re.IGNORECASE):
        return False
    _send_buyer_message(c, chat_id, _format_buyer_smm_orders(_get_buyer_orders(buyer_id, chat_id)[:5]))
    return True


def _handle_buyer_commands_command(c: Cardinal, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    if not re.match(r"^!(?:команды|help|помощь)\s*$", msg_text.strip(), re.IGNORECASE):
        return False
    cfg = load_config()
    template_text = (cfg.get("messages") or {}).get("buyer_commands") or DEFAULT_BUYER_COMMANDS_TEMPLATE
    _send_buyer_message(c, chat_id, _render_template(template_text, {}, {}))
    return True

def _handle_buyer_link_command(c: Cardinal, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    match = re.match(r"^!ссылка(?:\s+(.+))?$", msg_text.strip(), re.IGNORECASE)
    if not match:
        return False
    raw_ref = (match.group(1) or "").strip() or None
    order_record = _resolve_buyer_order_for_action(c, buyer_id, chat_id, raw_ref, command_name="!ссылка")
    if not order_record:
        return True
    order_id = str(order_record.get("order_id"))
    if _apply_refund_gate(c, order_record, notify_chat_id=chat_id, force_check=True):
        return True
    state = _get_buyer_flow_state(order_record)
    if _has_panel_order(order_record) or state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED):
        _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
        return True
    ctx = waiting_for_link.get(order_id) or _build_waiting_context_from_order(order_record) or {}
    ctx.update({"order_id_funpay": order_id, "buyer_id": buyer_id, "chat_id": chat_id})
    if state == BUYER_STATE_WAITING_LINK:
        waiting_for_link[order_id] = ctx
        _save_waiting_context(ctx, state=BUYER_STATE_LINK_PAUSED, saved_link_marker=None)
        _clear_selected_order_for_buyer(buyer_id, chat_id)
        _send_buyer_message(c, chat_id, f"⏸ Ожидание ссылки приостановлено.\n\n🧾 Заказ: #{order_id}\n\nКогда будете готовы отправить ссылку:\n!ссылка {order_id}")
        return True
    if state == BUYER_STATE_LINK_PAUSED:
        waiting_for_link[order_id] = ctx
        _save_waiting_context(ctx, state=BUYER_STATE_WAITING_LINK, saved_link_marker=None)
        _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
        _send_buyer_message(c, chat_id, f"▶️ Ожидание ссылки возобновлено.\n\n🧾 Заказ: #{order_id}\n\nОтправьте ссылку для запуска заказа.\nСсылка должна начинаться с:\nhttps://")
        return True
    if state == BUYER_STATE_WAITING_CONFIRM:
        waiting_for_link[order_id] = ctx
        _save_waiting_context(ctx, state=BUYER_STATE_WAITING_LINK, saved_link_marker=None)
        _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
        _send_buyer_message(c, chat_id, f"Текущая ссылка сброшена.\n\n🧾 Заказ: #{order_id}\n\nОтправьте новую ссылку.\nСсылка должна начинаться с:\nhttps://")
        return True
    _send_buyer_message(c, chat_id, "Отправьте ссылку для запуска заказа.")
    return True

def _buyer_owns_order(order_record: Dict, buyer_id: Any, chat_id: Any) -> bool:
    if not isinstance(order_record, dict):
        return False
    buyer_match = buyer_id not in (None, "") and str(order_record.get("buyer_id")) == str(buyer_id)
    chat_match = chat_id not in (None, "") and str(order_record.get("chat_id")) == str(chat_id)
    return bool(buyer_match or chat_match)


def _can_buyer_refund_pre_panel(c: Cardinal, order_record: Dict, buyer_id: Any, chat_id: Any) -> Tuple[bool, Dict]:
    if not isinstance(order_record, dict):
        return False, {}

    order_id = str(order_record.get("order_id") or order_record.get("order_id_funpay") or "")
    if not order_id:
        return False, order_record

    latest = orders.get_order_data(order_id) or order_record
    if not _buyer_owns_order(latest, buyer_id, chat_id):
        return False, latest

    state = _get_buyer_flow_state(latest)
    if state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED, BUYER_STATE_REFUNDED):
        return False, latest
    if _has_panel_order(latest):
        return False, latest

    # Force-check FunPay before any refund attempt. If the check is unavailable, do not refund.
    result = _fetch_funpay_status_via_cardinal(c, order_id) if c else {"ok": False, "error": "no_cardinal"}
    if not result.get("ok"):
        logger.warning(f"BUYER CANCEL SAFETY BLOCKED: order=#{order_id}, reason=funpay_force_check_failed, detail={result.get('detail') or result.get('error')}")
        return False, latest

    funpay_status = _normalize_funpay_status(result.get("funpay_status"))
    if funpay_status:
        update_fields: Dict[str, Any] = {"funpay_status": funpay_status, "funpay_status_checked_at": _now_str()}
        if funpay_status in ("refunded", "partially_refunded"):
            update_fields.update({"buyer_flow_state": BUYER_STATE_REFUNDED, "saved_link": None, "updated_at": _now_str()})
            waiting_for_link.pop(order_id, None)
        orders.update_order_fields(order_id, **update_fields)
        latest = orders.get_order_data(order_id) or latest

    if funpay_status != "open":
        logger.warning(f"BUYER CANCEL SAFETY BLOCKED: order=#{order_id}, reason=funpay_status_{funpay_status}")
        return False, latest

    state = _get_buyer_flow_state(latest)
    if state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED, BUYER_STATE_REFUNDED):
        return False, latest
    if _has_panel_order(latest):
        return False, latest

    return True, latest


def _handle_buyer_cancel_command(c: Cardinal, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    match = re.match(r"^!отмена(?:\s+(.+))?$", msg_text.strip(), re.IGNORECASE)
    if not match:
        return False
    raw_ref = (match.group(1) or "").strip() or None
    order_record = _resolve_buyer_order_for_action(c, buyer_id, chat_id, raw_ref, command_name="!отмена")
    if not order_record:
        return True
    order_id = str(order_record.get("order_id"))
    if _apply_refund_gate(c, order_record, notify_chat_id=chat_id, force_check=True):
        return True
    state = _get_buyer_flow_state(order_record)
    if _has_panel_order(order_record) or state in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED):
        _send_buyer_message(c, chat_id, "Заказ уже передан в работу.\nАвтоматическая отмена недоступна.")
        return True
    ctx = waiting_for_link.get(order_id) or _build_waiting_context_from_order(order_record) or {}
    ctx.update({"order_id_funpay": order_id, "buyer_id": buyer_id, "chat_id": chat_id})

    if bool(load_config().get("allow_buyer_cancel_before_panel_order", False)):
        can_refund, fresh_order = _can_buyer_refund_pre_panel(c, order_record, buyer_id, chat_id)
        if not can_refund:
            _send_buyer_message(c, chat_id, BUYER_AUTO_CANCEL_UNAVAILABLE_TEXT)
            return True
        try:
            c.account.refund(order_id)
            orders.update_order_fields(order_id, funpay_status="refunded", buyer_flow_state=BUYER_STATE_REFUNDED, saved_link=None, refund_detected_at=_now_str(), refund_source="buyer_cancel_command", updated_at=_now_str())
            waiting_for_link.pop(order_id, None)
            _clear_selected_order_for_buyer(buyer_id, chat_id)
            _send_buyer_message(c, chat_id, "Ввод ссылки отменён.\n\nЗаказ FunPay отменён.")
            return True
        except Exception as ex:
            logger.error(f"BUYER CANCEL REFUND FAILED: order=#{order_id}, error={ex}")
            waiting_for_link[order_id] = ctx
            _save_waiting_context(ctx, state=BUYER_STATE_LINK_PAUSED, saved_link_marker=None)
            _clear_selected_order_for_buyer(buyer_id, chat_id)
            _send_admin_order_alert(
                order_id_funpay=order_id,
                reason=f"покупатель запросил !отмена, но автоматический refund не сработал: {ex}",
                bot_action="refund не выполнен, ожидание ссылки приостановлено",
                action="проверьте заказ и при необходимости отмените вручную",
                title="⚠️ AutoSMM: не удалось выполнить buyer cancel",
            )
            _send_buyer_message(c, chat_id, BUYER_AUTO_CANCEL_FAILED_TEXT)
            return True

    waiting_for_link[order_id] = ctx
    _save_waiting_context(ctx, state=BUYER_STATE_LINK_PAUSED, saved_link_marker=None)
    _clear_selected_order_for_buyer(buyer_id, chat_id)
    _send_buyer_message(c, chat_id, "Ввод ссылки отменён.\n\nЗаказ FunPay не отменён.\nЕсли хотите отменить заказ, напишите продавцу.\nЧтобы снова отправить ссылку, напишите: !ссылка")
    return True


def _get_waiting_candidates_for_message(buyer_id: Any, chat_id: int) -> List[Dict]:
    return [r for r in _get_buyer_orders(buyer_id, chat_id) if _get_buyer_flow_state(r) in (BUYER_STATE_WAITING_LINK, BUYER_STATE_WAITING_CONFIRM) and not _has_panel_order(r)]


def _choose_waiting_candidate(buyer_id: Any, chat_id: int, candidates: List[Dict]) -> Optional[Dict]:
    selected = BUYER_ORDER_SELECTION.get(_buyer_selection_key(buyer_id, chat_id))
    if selected:
        for order_record in candidates:
            if str(order_record.get("order_id")) == str(selected):
                return order_record
    return candidates[0] if len(candidates) == 1 else None


def _send_link_confirmation_prompt(c: Cardinal, chat_id: int, order_id: str, link_value: str):
    orders.update_order_fields(order_id, last_prompt_ts=_now_str(), updated_at=_now_str())
    order_record = orders.get_order_data(order_id) or {}
    cfg = load_config()
    template_text = (cfg.get("messages") or {}).get("buyer_link_received_confirm") or DEFAULT_LINK_CONFIRM_TEMPLATE
    message_text = _render_template(
        template_text,
        order_record,
        {
            "order_id_funpay": order_id,
            "link": link_value,
            "lot_title": order_record.get("lot_title") or order_record.get("service_name") or "—",
        },
    )
    _send_buyer_message(c, chat_id, message_text)


def _maybe_send_confirm_reminder(c: Cardinal, chat_id: int, order_record: Dict) -> bool:
    order_id = str(order_record.get("order_id") or "")
    age = _seconds_since_checked_at(order_record.get("last_prompt_ts"))
    if age is not None and age < BUYER_CONFIRM_REMINDER_SECONDS:
        return False
    if order_id:
        orders.update_order_fields(order_id, last_prompt_ts=_now_str(), updated_at=_now_str())
    _send_buyer_message(c, chat_id, CONFIRM_REMINDER_TEXT)
    return True

def _normalize_buyer_confirm_action(text_value: Any) -> str:
    return re.sub(r"\s+", " ", str(text_value or "").strip().lower()).strip(".!?,;: ")


def _handle_waiting_link_message(c: Cardinal, order_record: Dict, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    order_id = str(order_record.get("order_id"))
    link_ = _extract_http_link(msg_text)
    if not link_:
        if _looks_like_link_without_https(msg_text):
            _send_buyer_message(c, chat_id, INVALID_LINK_SCHEME_TEXT)
            return True
        return False
    if _apply_refund_gate(c, order_record, notify_chat_id=chat_id, force_check=True):
        return True
    if _has_panel_order(order_record):
        _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
        return True
    ok, reason = is_valid_link(link_)
    if not ok:
        _send_buyer_message(c, chat_id, reason)
        return True
    ctx = waiting_for_link.get(order_id) or _build_waiting_context_from_order(order_record) or {}
    ctx.update({"buyer_id": buyer_id, "chat_id": chat_id, "order_id_funpay": order_id, "link": link_})
    if load_config().get("confirm_link", True):
        waiting_for_link[order_id] = ctx
        _save_waiting_context(ctx, state=BUYER_STATE_WAITING_CONFIRM, saved_link_marker=link_)
        _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
        _send_link_confirmation_prompt(c, chat_id, order_id, link_)
        return True
    waiting_for_link[order_id] = ctx
    _save_waiting_context(ctx, state=BUYER_STATE_WAITING_LINK, saved_link_marker=link_)
    process_link_without_confirmation(c, ctx)
    _clear_selected_order_for_buyer(buyer_id, chat_id)
    return True


def _handle_waiting_confirm_message(c: Cardinal, order_record: Dict, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    order_id = str(order_record.get("order_id"))
    lowered = _normalize_buyer_confirm_action(msg_text)
    link_ = _extract_http_link(msg_text)

    if not link_ and lowered not in BUYER_CONFIRM_YES_WORDS and lowered not in BUYER_CONFIRM_NO_WORDS:
        if _looks_like_link_without_https(msg_text):
            _send_buyer_message(c, chat_id, INVALID_LINK_SCHEME_TEXT)
            return True
        return _maybe_send_confirm_reminder(c, chat_id, order_record)

    if _apply_refund_gate(c, order_record, notify_chat_id=chat_id, force_check=True):
        return True
    if _has_panel_order(order_record):
        _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
        return True
    ctx = waiting_for_link.get(order_id) or _build_waiting_context_from_order(order_record) or {}
    ctx.update({"buyer_id": buyer_id, "chat_id": chat_id, "order_id_funpay": order_id, "link": order_record.get("saved_link") or ctx.get("link") or order_record.get("customer_url") or ""})

    if lowered in BUYER_CONFIRM_YES_WORDS:
        if not ctx.get("link"):
            waiting_for_link[order_id] = ctx
            _save_waiting_context(ctx, state=BUYER_STATE_WAITING_LINK, saved_link_marker=None)
            _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
            _send_buyer_message(c, chat_id, f"Ссылка не найдена.\n\n🧾 Заказ: #{order_id}\n\nОтправьте ссылку для запуска заказа.\nСсылка должна начинаться с:\nhttps://")
            return True
        process_link_without_confirmation(c, ctx)
        _clear_selected_order_for_buyer(buyer_id, chat_id)
        return True

    if lowered in BUYER_CONFIRM_NO_WORDS:
        waiting_for_link[order_id] = ctx
        _save_waiting_context(ctx, state=BUYER_STATE_WAITING_LINK, saved_link_marker=None)
        _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
        _send_buyer_message(c, chat_id, f"Текущая ссылка сброшена.\n\n🧾 Заказ: #{order_id}\n\nОтправьте новую ссылку.\nСсылка должна начинаться с:\nhttps://")
        return True

    if link_:
        ok, reason = is_valid_link(link_)
        if not ok:
            _send_buyer_message(c, chat_id, reason)
            return True
        ctx["link"] = link_
        waiting_for_link[order_id] = ctx
        _save_waiting_context(ctx, state=BUYER_STATE_WAITING_CONFIRM, saved_link_marker=link_)
        _set_selected_order_for_buyer(buyer_id, chat_id, order_id)
        _send_link_confirmation_prompt(c, chat_id, order_id, link_)
        return True

    return _maybe_send_confirm_reminder(c, chat_id, order_record)

def _handle_buyer_flow_message(c: Cardinal, msg_text: str, buyer_id: Any, chat_id: int) -> bool:
    candidates = _get_waiting_candidates_for_message(buyer_id, chat_id)
    if not candidates:
        if _extract_http_link(msg_text):
            recent_orders = _get_buyer_orders(buyer_id, chat_id)
            if recent_orders:
                latest_order = recent_orders[0]
                if _apply_refund_gate(c, latest_order, notify_chat_id=chat_id, force_check=True):
                    return True
                if _has_panel_order(latest_order) or _get_buyer_flow_state(latest_order) in (BUYER_STATE_PANEL_CREATED, BUYER_STATE_COMPLETED, BUYER_STATE_FAILED):
                    _send_buyer_message(c, chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
                    return True
        return False

    order_record = _choose_waiting_candidate(buyer_id, chat_id, candidates)
    if not order_record:
        lowered = _normalize_buyer_confirm_action(msg_text)
        if not _extract_http_link(msg_text) and lowered not in BUYER_CONFIRM_YES_WORDS and lowered not in BUYER_CONFIRM_NO_WORDS:
            return False
        _send_buyer_message(c, chat_id, _format_short_order_list(candidates, command_name="!ссылка"))
        return True

    state = _get_buyer_flow_state(order_record)
    if state == BUYER_STATE_WAITING_LINK:
        return _handle_waiting_link_message(c, order_record, msg_text, buyer_id, chat_id)
    if state == BUYER_STATE_WAITING_CONFIRM:
        return _handle_waiting_confirm_message(c, order_record, msg_text, buyer_id, chat_id)
    return False


def _recover_buyer_flow_runtime(c: Optional[Cardinal] = None):
    waiting_for_link.clear()
    recovered = 0
    try:
        all_orders = orders.load_orders_data()
    except Exception as ex:
        logger.error(f"BUYER FLOW RECOVERY FAILED: cannot load orders_data: {ex}")
        return
    for order_record in all_orders:
        if not isinstance(order_record, dict):
            continue
        order_id = str(order_record.get("order_id") or "")
        if not order_id:
            continue
        state = _get_buyer_flow_state(order_record)
        if state not in BUYER_ACTIVE_PRE_PANEL_STATES:
            continue
        if _apply_refund_gate(c, order_record, notify_chat_id=None, force_check=True):
            continue
        if _has_panel_order(order_record):
            orders.update_order_fields(order_id, buyer_flow_state=BUYER_STATE_PANEL_CREATED, panel_order_id=_get_panel_order_id(order_record), updated_at=_now_str())
            continue
        ctx = _build_waiting_context_from_order(order_record)
        if ctx:
            waiting_for_link[order_id] = ctx
            recovered += 1
    if recovered:
        logger.info(f"BUYER FLOW RECOVERY: restored {recovered} active waiting orders")

def _get_lot_fallback_targets(cfg: Dict, service_id: int, service_number: int) -> Tuple[Optional[int], Optional[int]]:
    lot_mapping_local = cfg.get("lot_mapping", {})
    for _, lot_data in lot_mapping_local.items():
        if lot_data.get("service_id") == service_id and lot_data.get("service_number") == service_number:
            return lot_data.get("fallback_service_id"), lot_data.get("fallback_service_number")
    return None, None


def _detect_fallback_reason(primary_error: str, probe_state: Optional[str] = None) -> Optional[str]:
    primary_error = str(primary_error or "unknown")
    if primary_error in ("service_inactive", "insufficient_funds", "user_inactive"):
        return primary_error
    if primary_error == "validation_error":
        return "validation_error"
    if primary_error == "unsupported_action":
        return "unsupported_action"
    return probe_state or primary_error


def _store_fallback_decision(order_id_funpay: str, fallback_used: Optional[bool] = None, fallback_reason: Optional[str] = None):
    order_record = orders.get_order_data(order_id_funpay)
    if not order_record:
        return

    if fallback_used is True:
        orders.update_order_fields(order_id_funpay, fallback_used=True, fallback_reason=fallback_reason)
        order_record["fallback_used"] = True
        order_record["fallback_reason"] = fallback_reason
        return

    if fallback_reason:
        orders.update_order_fields(order_id_funpay, fallback_used=False, fallback_reason=fallback_reason)
        order_record["fallback_used"] = False
        order_record["fallback_reason"] = fallback_reason


def _finalize_created_order(
    c: Cardinal,
    cfg: Dict,
    buyer_chat_id: int,
    order_id_funpay: str,
    twiboost_id: int,
    link_: str,
    real_amount: int,
    service_number: int,
    lot_price: float,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    notification_info_title: Optional[str] = None,
    notification_reason: Optional[str] = None,
    notification_action: Optional[str] = None,
):
    chistota = float(lot_price)

    orders.upsert_order_data(
        buyer_chat_id,
        order_id_funpay,
        twiboost_id,
        "pending",
        chistota,
        link_,
        real_amount,
        service_number,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        funpay_status="open",
    )
    orders.update_order_fields(
        order_id_funpay,
        buyer_flow_state=BUYER_STATE_PANEL_CREATED,
        panel_order_id=twiboost_id,
        panel_status="pending",
        saved_link=None,
        total_quantity=int(real_amount or 0),
        done_quantity=0,
        left_quantity=int(real_amount or 0),
        progress_percent=0,
        updated_at=_now_str(),
    )

    service_cfg = cfg["services"].get(str(service_number))
    if not service_cfg:
        raise ValueError(f"Не найден config.services[{service_number}]")

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    send_order_started_notification(
        c,
        order_id_funpay,
        twiboost_id,
        link_,
        api_url,
        api_key,
        lot_price,
        real_amount,
        service_number=service_number,
        info_title=notification_info_title,
        reason=notification_reason,
        action_text=notification_action,
    )

    check_order_status(c, twiboost_id, buyer_chat_id, link_, order_id_funpay)

    order_record = orders.get_order_data(order_id_funpay) or {}
    msg_confirmation = _render_template(
        cfg.get("messages", {}).get("after_confirmation", "🎉 Заказ успешно оформлен!\n\n🔢 ID заказа в сервисе: {panel_order_id}\n🔗 {link}"),
        order_record,
        {
            "panel_order_id": twiboost_id,
            "twiboost_id": twiboost_id,
            "link": link_,
            "order_id_funpay": order_id_funpay,
            "total_quantity": real_amount,
            "done_quantity": 0,
            "left_quantity": real_amount,
            "progress_percent": 0,
            "panel_status": "pending",
            "funpay_status": "open",
        },
    )

    _send_buyer_message(c, buyer_chat_id, msg_confirmation)
    waiting_for_link.pop(str(order_id_funpay), None)


def _get_panel_balance_for_risk_check(service_number: Any) -> Dict[str, Any]:
    cfg = load_config()
    service_cfg = (cfg.get("services") or {}).get(str(service_number))
    if not isinstance(service_cfg, dict):
        return {"ok": False, "error": "service_not_found", "text": "недоступно"}

    api_url = str(service_cfg.get("api_url") or "").strip()
    api_key = _normalize_api_key(service_cfg.get("api_key"))
    if not api_url or not api_key:
        return {"ok": False, "error": "credentials_missing", "text": "недоступно"}

    try:
        response = requests.get(
            api_url,
            params={"action": "balance", "key": api_key},
            timeout=6,
        )
        response.raise_for_status()
        payload = response.json()
        balance_value = float(str(payload.get("balance", 0) or 0).replace(",", "."))
        currency = str(payload.get("currency") or "RUB").upper().strip() or "RUB"
        return {
            "ok": True,
            "balance": balance_value,
            "currency": currency,
            "text": f"{balance_value:.2f} {currency}",
        }
    except Exception as ex:
        logger.warning(f"BALANCE RISK CHECK FAILED: service=#{service_number}, error={ex}")
        return {"ok": False, "error": str(ex)[:120], "text": "недоступно"}


def _format_balance_risk_value(balance_payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(balance_payload, dict) or not balance_payload.get("ok"):
        return "недоступно"
    return str(balance_payload.get("text") or "недоступно")


def _build_transport_error_balance_note(balance_before: Optional[Dict[str, Any]], balance_after: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    before_ok = isinstance(balance_before, dict) and balance_before.get("ok")
    after_ok = isinstance(balance_after, dict) and balance_after.get("ok")
    before_text = _format_balance_risk_value(balance_before)
    after_text = _format_balance_risk_value(balance_after)

    if before_ok and after_ok and str(balance_before.get("currency")) == str(balance_after.get("currency")):
        try:
            delta = float(balance_after.get("balance") or 0) - float(balance_before.get("balance") or 0)
        except Exception:
            delta = 0.0
        delta_text = f"{delta:.2f} {str(balance_after.get('currency') or '').upper()}".strip()
        if float(balance_after.get("balance") or 0) < float(balance_before.get("balance") or 0):
            note = (
                f"Баланс до запроса: {before_text}\n"
                f"Баланс после ошибки: {after_text}\n"
                f"Разница: {delta_text}\n\n"
                "Возможно, заказ был создан на панели, но AutoSMM не получил ответ.\n"
                "Баланс — не доказательство создания заказа, а только признак риска."
            )
            return note, "high_risk_balance_decreased"
        note = (
            f"Баланс до запроса: {before_text}\n"
            f"Баланс после ошибки: {after_text}\n"
            "Разница: 0\n\n"
            "Баланс не изменился, но transport-error не позволяет безопасно продолжить автоматически.\n"
            "Баланс — не доказательство отсутствия заказа, а только признак риска."
        )
        return note, "manual_check_balance_unchanged"

    note = (
        "Баланс до/после проверить не удалось.\n"
        "Transport-error после add-запроса требует ручной проверки.\n"
        "Баланс — не источник правды, а только возможный признак риска."
    )
    return note, "manual_check_balance_unavailable"


def _send_transport_error_after_add_alert(
    order_id_funpay: Any,
    service_number: Any,
    service_id: Any,
    real_amount: Any,
    link_: str,
    primary_error: Any,
    primary_detail: Any,
    balance_before: Optional[Dict[str, Any]] = None,
    balance_after: Optional[Dict[str, Any]] = None,
):
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id or bot is None:
        return

    balance_note, _risk_code = _build_transport_error_balance_note(balance_before, balance_after)
    text_ = (
        "⚠️ Заказ требует ручной проверки\n\n"
        f"Заказ FunPay: #{order_id_funpay}\n"
        f"Панель: #{service_number}\n"
        f"Услуга: {service_id}\n"
        f"Итоговое количество: {real_amount}\n"
        f"Ссылка: {link_}\n\n"
        "Причина:\n"
        "панель не вернула ID заказа после add-запроса.\n"
        f"Ошибка: {primary_error}\n"
        f"Детали: {str(primary_detail or '')[:300]}\n\n"
        f"{balance_note}\n\n"
        "Refund и резервная панель заблокированы.\n"
        "Проверьте панель вручную."
    )
    try:
        bot.send_message(notification_chat_id, text_)
    except Exception as ex:
        logger.error(f"Не удалось отправить transport-error alert по заказу #{order_id_funpay}: {ex}")


def _handle_transport_error_after_add(
    c: Cardinal,
    order_id_funpay: Any,
    buyer_chat_id: Any,
    service_number: Any,
    service_id: Any,
    real_amount: Any,
    link_: str,
    primary_error: Any,
    primary_detail: Any,
    balance_before: Optional[Dict[str, Any]] = None,
    balance_after: Optional[Dict[str, Any]] = None,
    lot_price: Any = 0,
    lot_title: str = "",
):
    order_id_str = str(order_id_funpay)
    order_record = orders.get_order_data(order_id_str)
    if not order_record:
        try:
            _upsert_buyer_flow_order_record(
                chat_id=int(buyer_chat_id),
                buyer_id=None,
                buyer_username="",
                order_id_funpay=order_id_str,
                lot_title=lot_title or "—",
                order_price=lot_price or 0,
                order_amount=1,
                service_id=int(service_id or 0),
                real_amount=int(real_amount or 0),
                service_number=int(service_number or 1),
                funpay_status="open",
                state=BUYER_STATE_FAILED,
                saved_link=link_,
            )
        except Exception as ex:
            logger.warning(f"Не удалось создать минимальную запись manual-check для заказа #{order_id_str}: {ex}")

    orders.update_order_fields(
        order_id_str,
        status="unknown",
        buyer_flow_state=BUYER_STATE_FAILED,
        saved_link=link_,
        customer_url=link_,
        status_last_error=str(primary_error or "unknown"),
        status_stop_reason="transport_error_after_add",
        panel_status="manual_check",
        updated_at=_now_str(),
    )
    waiting_for_link.pop(order_id_str, None)

    try:
        _send_buyer_message(c, int(buyer_chat_id), BUYER_ADD_TRANSPORT_MANUAL_TEXT)
    except Exception as ex:
        logger.warning(f"Не удалось отправить buyer manual text по transport-error #{order_id_str}: {ex}")

    _send_transport_error_after_add_alert(
        order_id_funpay=order_id_str,
        service_number=service_number,
        service_id=service_id,
        real_amount=real_amount,
        link_=link_,
        primary_error=primary_error,
        primary_detail=primary_detail,
        balance_before=balance_before,
        balance_after=balance_after,
    )

def process_link_without_confirmation(c: Cardinal, data: Dict):
    """
    Обрабатывает ссылку без подтверждения или после получения подтверждения.
    Только primary add + консервативный fallback.
    """
    link_ = data["link"]
    service_id = data["service_id"]
    real_amount = data["real_amount"]
    order_id_funpay = data["order_id_funpay"]
    buyer_chat_id = data["chat_id"]
    service_number = data["service_number"]
    lot_price = data["price"]

    cfg = load_config()
    fallback_enabled = bool(cfg.get("fallback_enabled", False))
    fallback_service_id, fallback_service_number = _get_lot_fallback_targets(cfg, service_id, service_number)
    existing_order = orders.get_order_data(order_id_funpay)

    if existing_order and _apply_refund_gate(c, existing_order, notify_chat_id=buyer_chat_id, force_check=True):
        return
    if existing_order and _has_panel_order(existing_order):
        _send_buyer_message(c, buyer_chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
        return

    balance_before = _get_panel_balance_for_risk_check(service_number)

    ok, twiboost_id, payload = send_order_to_service(
        c=c,
        service_number=service_number,
        service_id=service_id,
        link=link_,
        quantity=real_amount,
    )

    if ok and twiboost_id is not None:
        logger.info("PRIMARY ADD: SUCCESS")
        _finalize_created_order(
            c=c,
            cfg=cfg,
            buyer_chat_id=buyer_chat_id,
            order_id_funpay=order_id_funpay,
            twiboost_id=twiboost_id,
            link_=link_,
            real_amount=real_amount,
            service_number=service_number,
            lot_price=lot_price,
        )
        return

    primary_error = payload.get("error", "unknown")
    primary_detail = payload.get("detail", str(payload))
    logger.warning(f"PRIMARY ERROR: {primary_error}")

    if primary_error in ("timeout", "network_error", "invalid_json", "http_error", "unknown"):
        logger.error("MANUAL CHECK REQUIRED")
        logger.error("REFUND BLOCKED")
        logger.error("FALLBACK BLOCKED")
        try:
            time.sleep(3)
        except Exception:
            pass
        balance_after = _get_panel_balance_for_risk_check(service_number)
        _handle_transport_error_after_add(
            c=c,
            order_id_funpay=order_id_funpay,
            buyer_chat_id=buyer_chat_id,
            service_number=service_number,
            service_id=service_id,
            real_amount=real_amount,
            link_=link_,
            primary_error=primary_error,
            primary_detail=primary_detail,
            balance_before=balance_before,
            balance_after=balance_after,
            lot_price=lot_price,
            lot_title=(data.get("lot_title") or ""),
        )
        return

    if primary_error in ("validation_error", "unsupported_action"):
        logger.error("FALLBACK BLOCKED")
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason=primary_error)
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=(
                f"Primary ошибка: {primary_error}. Резервная панель заблокирована. Детали: {primary_detail}"
            ),
            bot_action="резервная панель заблокирована, заказ не создан автоматически",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    if primary_error not in ("service_inactive", "insufficient_funds", "user_inactive"):
        logger.error("MANUAL CHECK REQUIRED")
        logger.error("FALLBACK BLOCKED")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=(
                f"Primary ошибка: {primary_error}. Резервная панель не запускалась. Детали: {primary_detail}"
            ),
            bot_action="автоматическая обработка остановлена, резервная панель не запускалась",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    if not fallback_enabled:
        logger.error("FALLBACK BLOCKED")
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason="fallback_disabled")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=f"Резервная панель отключена. Primary ошибка: {primary_error} | {primary_detail}",
            bot_action="резервная панель отключена, заказ не создан автоматически",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    if existing_order and existing_order.get("fallback_used", False):
        logger.error("FALLBACK BLOCKED")
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason="fallback_already_used")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=f"Повторная резервная панель запрещена. Primary ошибка: {primary_error} | {primary_detail}",
            bot_action="повторная резервная панель запрещена, заказ не создан автоматически",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    if not fallback_service_id or not fallback_service_number:
        logger.error("FALLBACK BLOCKED")
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason="fallback_not_configured")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=f"Резервная панель не настроена. Primary ошибка: {primary_error} | {primary_detail}",
            bot_action="резервная панель не настроена, заказ не создан автоматически",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    fallback_service_number = int(fallback_service_number)
    fallback_reason = _detect_fallback_reason(primary_error)

    if primary_error == "user_inactive" and fallback_service_number == int(service_number):
        logger.error("FALLBACK BLOCKED")
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason="same_panel_auth_failed")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=(
                f"Primary ошибка: {primary_error}. Резервная панель заблокирована: основная и резервная панели указывают на один и тот же сервис #{service_number}."
            ),
            bot_action="резервная панель заблокирована, потому что основная и резервная панели совпадают",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    if fallback_service_number == int(service_number):
        logger.error("FALLBACK BLOCKED")
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason="same_panel_fallback")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=(
                f"Резервная панель заблокирована: основная и резервная панели указывают на один и тот же сервис #{service_number}."
            ),
            bot_action="резервная панель заблокирована, потому что основная и резервная панели совпадают",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
        )
        return

    logger.warning("FALLBACK STARTED")
    ok2, twiboost_id2, payload2 = send_order_to_service(
        c=c,
        service_number=fallback_service_number,
        service_id=fallback_service_id,
        link=link_,
        quantity=real_amount,
    )

    if ok2 and twiboost_id2 is not None:
        _finalize_created_order(
            c=c,
            cfg=cfg,
            buyer_chat_id=buyer_chat_id,
            order_id_funpay=order_id_funpay,
            twiboost_id=twiboost_id2,
            link_=link_,
            real_amount=real_amount,
            service_number=fallback_service_number,
            lot_price=lot_price,
            fallback_used=True,
            fallback_reason=fallback_reason,
            notification_info_title="ℹ️ Заказ переведён на резервную панель",
            notification_reason=f"{primary_error} на панели #{service_number}",
            notification_action=f"заказ отправлен через резервную панель #{fallback_service_number}",
        )
        return

    err2 = payload2.get("error", "unknown")
    detail2 = payload2.get("detail", str(payload2))
    logger.error(f"Резервная панель попытка тоже провалена ({err2}) | {detail2}")
    _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason=fallback_reason)

    refund_order(
        c,
        order_id_funpay,
        buyer_chat_id,
        reason=f"{primary_error} на панели #{service_number}, резервная панель не смогла создать заказ на панели #{fallback_service_number}",
        detailed_reason=(
            f"Primary ошибка: {primary_error} | {primary_detail}. Ошибка резервной панели: {err2} | {detail2}."
        ),
        bot_action="автоматическое создание через резервную панель не удалось",
        admin_action="проверьте заказ и при необходимости выполните возврат вручную",
        failure_title="⚠️ Заказ требует ручной проверки",
    )


# =========================================================
# ANALYTICS
# =========================================================

_DATE_FMT = "%Y-%m-%d %H:%M:%S"

CB_STATS_24H = "autosmm_stats_24h"
CB_STATS_7D = "autosmm_stats_7d"
CB_STATS_30D = "autosmm_stats_30d"
CB_STATS_ALL = "autosmm_stats_all"
CB_BACK = "return_to_settings"

def _analytics_safe_load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                return default
            return json.loads(content)
    except Exception:
        return default


def _analytics_safe_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    os.rename(tmp, path)


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, _DATE_FMT)
    except Exception:
        return None


def _load_orders_log() -> List[Dict]:
    # auto_smm_orders.json — лог заказов (date/summa/service_name/chistota/...) создаётся core'ом
    data = _analytics_safe_load_json(orders.ORDERS_PATH, [])
    return data if isinstance(data, list) else []


def _coerce_int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        # allow float-like timestamps
        return int(float(v))
    except Exception:
        return None


def _period_info(period: str) -> Tuple[str, Optional[int], str]:
    """Returns (normalized_period, seconds_window_or_None, title_label).

    Supports both new and legacy period names.
    """
    p = (period or "24h").strip().lower()

    # legacy aliases
    if p in ("day", "24", "24ч", "24h"):
        return "24h", 86400, "24ч"
    if p in ("week", "7", "7д", "7d"):
        return "7d", 604800, "7д"
    if p in ("month", "30", "30д", "30d"):
        return "30d", 2592000, "30д"
    if p in ("all", "all_time", "всё", "все", "∞", "infinity"):
        return "all", None, "∞ всё"

    # fallback
    return "24h", 86400, "24ч"


def _filter_by_period(items: List[Dict], period: str) -> Tuple[str, List[Dict]]:
    """Filters by created_at (unix timestamp) from orders_data.json.

    If created_at is missing for an order -> it is considered "old" and appears ONLY in period "all".
    """
    norm, window, label = _period_info(period)
    if window is None:
        return label, items

    now_ts = int(time.time())
    cutoff = now_ts - int(window)

    out: List[Dict] = []
    for it in items:
        ca = _coerce_int(it.get("created_at"))
        if ca is None:
            # show only in "all"
            continue
        if ca >= cutoff:
            out.append(it)
    return label, out


def _merged_orders() -> List[Dict]:
    """Сшивает orders_data.json (статусы/траты/created_at) и auto_smm_orders.json (дата/описание) по order_id."""
    od = orders.load_orders_data()
    od = od if isinstance(od, list) else []

    log_list = _load_orders_log()
    log_map = {}
    for rec in log_list:
        try:
            oid = str(rec.get("order_id"))
        except Exception:
            continue
        if oid:
            log_map[oid] = rec

    merged: List[Dict] = []
    seen = set()

    # сначала — из orders_data (главный источник created_at)
    for rec in od:
        try:
            oid = str(rec.get("order_id"))
        except Exception:
            continue
        if not oid:
            continue

        log_rec = log_map.get(oid, {})
        dt = _parse_dt(str(log_rec.get("date", "")))

        merged.append(
            {
                "order_id": oid,
                "created_at": _coerce_int(rec.get("created_at")),
                "date": dt,
                "date_str": log_rec.get("date"),
                "service_name": log_rec.get("service_name") or rec.get("service_name") or "—",
                "status": str(rec.get("status", "unknown")),
                "is_refunded": _is_refunded_funpay_status(_get_effective_funpay_status(rec)),
                "funpay_status": _get_effective_funpay_status(rec),
                "summa": float(rec.get("summa", log_rec.get("summa", 0)) or 0),
                "spent": float(rec.get("spent", log_rec.get("spent", 0)) or 0),
                "currency": str(rec.get("currency", log_rec.get("currency", "RUB")) or "RUB"),
                "chistota": float(rec.get("chistota", log_rec.get("chistota", 0)) or 0),
            }
        )
        seen.add(oid)

    # затем — те, что есть только в логе (created_at неизвестен -> показываем только в "all")
    for rec in log_list:
        oid = str(rec.get("order_id", ""))
        if not oid or oid in seen:
            continue
        dt = _parse_dt(str(rec.get("date", "")))
        merged.append(
            {
                "order_id": oid,
                "created_at": None,
                "date": dt,
                "date_str": rec.get("date"),
                "service_name": rec.get("service_name") or "—",
                "status": "unknown",
                "is_refunded": False,
                "funpay_status": None,
                "summa": float(rec.get("summa", 0) or 0),
                "spent": float(rec.get("spent", 0) or 0),
                "currency": str(rec.get("currency", "RUB") or "RUB"),
                "chistota": float(rec.get("chistota", 0) or 0),
            }
        )

    return merged


def get_statistics():
    """Совместимость: агрегаты 24ч/7д/30д/всё время.

    ВАЖНО: структура результата сохранена.
    """
    items = _merged_orders()

    def calc(p: str) -> Dict:
        _, lst = _filter_by_period(items, p)
        total = sum(float(o.get("summa", 0) or 0) for o in lst)
        ch = sum(float(o.get("chistota", 0) or 0) for o in lst if not _is_order_refundedish(o))
        return {"orders": len(lst), "total": total, "chistota": round(ch, 2)}

    day = calc("24h")
    week = calc("7d")
    month = calc("30d")
    all_ = calc("all")

    return {
        "day_orders": day["orders"],
        "day_total": day["total"],
        "day_chistota": day["chistota"],
        "week_orders": week["orders"],
        "week_total": week["total"],
        "week_chistota": week["chistota"],
        "month_orders": month["orders"],
        "month_total": month["total"],
        "month_chistota": month["chistota"],
        "all_time_orders": all_["orders"],
        "all_time_total": all_["total"],
        "all_time_chistota": all_["chistota"],
    }


def update_order_charge_and_net(order_id_funpay: str, spent: float, currency: str = "USD", net_profit: float = None):
    """Обновляет spent/currency/chistota в orders_data.json и auto_smm_orders.json.

    Поддержка старого вызова (из неудачных рефакторингов):
      update_order_charge_and_net(order_id, spent, net_profit)
    """
    # совместимость: если третьим параметром прилетела цифра (net_profit), а не валюта
    if isinstance(currency, (int, float)) and net_profit is None:
        net_profit = float(currency)
        currency = "USD"

    try:
        spent_f = float(spent)
    except Exception:
        spent_f = 0.0

    # 1) orders_data.json
    orders_data = orders.load_orders_data()
    found_od = False
    if isinstance(orders_data, list):
        for o in orders_data:
            if str(o.get("order_id")) == str(order_id_funpay):
                o["spent"] = spent_f
                o["currency"] = str(currency or "USD")
                if net_profit is not None:
                    try:
                        o["chistota"] = float(net_profit)
                    except Exception:
                        pass
                else:
                    try:
                        o["chistota"] = round(float(o.get("summa", 0) or 0) - spent_f, 2)
                    except Exception:
                        o["chistota"] = round(0 - spent_f, 2)
                found_od = True
                break

    if found_od:
        orders.save_orders_data(orders_data)

    # 2) auto_smm_orders.json (лог)
    try:
        orders_list = _load_orders_log()
        updated = False
        for rec in orders_list:
            if str(rec.get("order_id")) == str(order_id_funpay):
                rec["spent"] = spent_f
                rec["currency"] = str(currency or "USD")
                if net_profit is not None:
                    try:
                        rec["chistota"] = float(net_profit)
                    except Exception:
                        pass
                else:
                    try:
                        rec["chistota"] = round(float(rec.get("summa", 0) or 0) - spent_f, 2)
                    except Exception:
                        rec["chistota"] = round(0 - spent_f, 2)
                updated = True
                break

        if updated:
            _analytics_safe_write_json(orders.ORDERS_PATH, orders_list)
    except Exception:
        # статистика — не критичный путь
        pass


# --- Analytics v2 (required UX format) ---
_COMPLETED = ("completed", "done", "success")
_ACTIVE = ("pending", "processing", "inprogress", "in_progress", "in progress")
_FAILED = ("failed", "error", "canceled", "cancelled", "unknown", "fail")
_PARTIAL = ("partial",)


def _analytics_view_key(chat_id: Any, message_id: Any) -> Tuple[int, int]:
    try:
        return int(chat_id), int(message_id)
    except Exception:
        return 0, 0


def _set_analytics_view_mode(chat_id: Any, message_id: Any, mode: str):
    ANALYTICS_VIEW_MODES[_analytics_view_key(chat_id, message_id)] = "more" if str(mode or "").strip().lower() == "more" else "main"


def _get_analytics_view_mode(chat_id: Any, message_id: Any) -> str:
    return ANALYTICS_VIEW_MODES.get(_analytics_view_key(chat_id, message_id), "main")


def _normalize_analytics_status(status_value: Any) -> str:
    normalized = _normalize_runtime_status(status_value)
    if normalized in ("completed", "partial", "pending", "canceled", "fail", "unknown"):
        return normalized
    return "unknown"


def _is_completed_for_profit(order_record: Dict, panel_status: str, funpay_status: Optional[str]) -> bool:
    return panel_status == "completed" and funpay_status == "closed"


def _calc_order_net_profit(order_record: Dict) -> float:
    try:
        revenue = float(order_record.get("summa", 0) or 0)
    except Exception:
        revenue = 0.0
    try:
        spent = float(order_record.get("spent", 0) or 0)
    except Exception:
        spent = 0.0
    return round(revenue - spent, 2)


def get_period_analytics(period: str = "24h") -> Dict:
    """Returns analytics for selected period based on created_at.

    period: 24h | 7d | 30d | all
    """
    items = _merged_orders()
    label, lst = _filter_by_period(items, period)

    total = len(lst)
    active = 0
    completed = 0
    refunded = 0
    partial = 0
    errors = 0

    revenue = 0.0
    spent = 0.0
    profit = 0.0
    profit_pending = 0.0
    losses = 0.0

    for o in lst:
        panel_status = _normalize_analytics_status(o.get("status", "unknown"))
        funpay_status = _get_effective_funpay_status(o)
        refunded_like = funpay_status in ("refunded", "partially_refunded")

        try:
            revenue += float(o.get("summa", 0) or 0)
        except Exception:
            pass
        try:
            order_spent = float(o.get("spent", 0) or 0)
        except Exception:
            order_spent = 0.0
        spent += order_spent

        order_net_profit = _calc_order_net_profit(o)

        if refunded_like:
            refunded += 1
            if panel_status in ("completed", "partial") or order_spent > 0:
                losses += order_spent
            continue

        if panel_status == "partial":
            partial += 1
            continue

        if _is_completed_for_profit(o, panel_status, funpay_status):
            completed += 1
            profit += order_net_profit
            continue

        if panel_status == "completed" and funpay_status in (None, "open"):
            active += 1
            profit_pending += order_net_profit
            continue

        if panel_status in ("fail", "canceled", "unknown"):
            errors += 1
            continue

        active += 1

    return {
        "period_norm": _period_info(period)[0],
        "period_label": label,
        "revenue": round(revenue, 2),
        "spent": round(spent, 2),
        "profit": round(profit, 2),
        "profit_pending": round(profit_pending, 2),
        "losses": round(losses, 2),
        "total": int(total),
        "active": int(active),
        "completed": int(completed),
        "refunded": int(refunded),
        "partial": int(partial),
        "errors": int(errors),
    }


def _fmt_rub(x: float) -> str:
    try:
        v = float(x)
    except Exception:
        v = 0.0
    v = round(v, 2)
    if abs(v - int(v)) < 1e-9:
        return str(int(v))
    return f"{v:.2f}"


def _count_orders_awaiting_confirmation(period: str = "24h") -> int:
    label, lst = _filter_by_period(_merged_orders(), period)
    count = 0

    for o in lst:
        panel_status = _normalize_analytics_status(o.get("status", "unknown"))
        funpay_status = _get_effective_funpay_status(o)
        if panel_status == "completed" and funpay_status in (None, "open"):
            count += 1

    return int(count)


def format_analytics(period: str = "24h", top_n: int = 5, last_n: int = 8, mode: str = "main") -> str:
    """Main text formatter for analytics screen.

    NOTE: top_n / last_n kept for backward-compatibility (ignored).
    """
    d = get_period_analytics(period=period)
    period_title = d.get("period_label", "—")
    expanded = str(mode or "").strip().lower() == "more"

    if expanded:
        awaiting_confirmation = _count_orders_awaiting_confirmation(period=period)
        parts = [
            f"📊 AutoSMM — {period_title} (расширенная)",
            "",
            "💰 Финансы",
            f"Выручка: {_fmt_rub(d.get('revenue', 0))} ₽",
            f"Затраты: {_fmt_rub(d.get('spent', 0))} ₽",
            f"📈 Профит: {_fmt_rub(d.get('profit', 0))} ₽",
            f"📉 Потери: {_fmt_rub(d.get('losses', 0))} ₽",
            "",
            "📦 Заказы",
            f"Всего: {int(d.get('total', 0) or 0)}",
            f"⚙ В работе: {int(d.get('active', 0) or 0)}",
            f"✅ Завершено: {int(d.get('completed', 0) or 0)}",
            f"↩ Refunded: {int(d.get('refunded', 0) or 0)}",
            f"🧩 Partial: {int(d.get('partial', 0) or 0)}",
            f"❌ Ошибки: {int(d.get('errors', 0) or 0)}",
            "",
            "⏳ Ожидание",
            f"Профит в ожидании: {_fmt_rub(d.get('profit_pending', 0))} ₽",
            f"Ожидают подтверждения: {awaiting_confirmation}",
        ]
        return "\n".join(parts)

    parts = [
        f"📊 AutoSMM — {period_title}",
        "",
        "💰 Финансы",
        f"Выручка: {_fmt_rub(d.get('revenue', 0))} ₽",
        f"Затраты: {_fmt_rub(d.get('spent', 0))} ₽",
        f"📈 Профит: {_fmt_rub(d.get('profit', 0))} ₽",
        "",
        "📦 Заказы",
        f"Всего: {int(d.get('total', 0) or 0)}",
        f"⚙ В работе: {int(d.get('active', 0) or 0)}",
        f"✅ Завершено: {int(d.get('completed', 0) or 0)}",
        f"↩️ Refunded: {int(d.get('refunded', 0) or 0)}",
    ]

    return "\n".join(parts)


def _period_to_cb(period: str) -> str:
    norm, _, _ = _period_info(period)
    if norm == "7d":
        return CB_STATS_7D
    if norm == "30d":
        return CB_STATS_30D
    if norm == "all":
        return CB_STATS_ALL
    return CB_STATS_24H


def build_analytics_keyboard(period: str = "24h", mode: str = "main"):
    """Returns InlineKeyboardMarkup for the analytics screen."""
    try:
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton  # type: ignore
    except Exception:
        return None

    period_norm = _period_info(period)[0]
    if period_norm == "7d":
        period_callback_suffix = "week"
    elif period_norm == "30d":
        period_callback_suffix = "month"
    elif period_norm == "all":
        period_callback_suffix = "all"
    else:
        period_callback_suffix = "day"

    toggle_callback = f"analytics_less_{period_callback_suffix}" if str(mode or "").strip().lower() == "more" else f"analytics_more_{period_callback_suffix}"
    toggle_text = "◀️ Основное" if str(mode or "").strip().lower() == "more" else "Еще"

    kb = InlineKeyboardMarkup(row_width=4)
    kb.row(
        InlineKeyboardButton("🕒 24ч", callback_data="analytics_period_day"),
        InlineKeyboardButton("📅 7д", callback_data="analytics_period_week"),
        InlineKeyboardButton("🗓 30д", callback_data="analytics_period_month"),
        InlineKeyboardButton("♾ Всё", callback_data="analytics_period_all"),
    )
    kb.row(
        InlineKeyboardButton(toggle_text, callback_data=toggle_callback),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"analytics_period_{period_callback_suffix}"),
    )
    kb.row(
        InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"),
    )
    return kb


def get_analytics_text_and_kb(period: str = "24h", mode: str = "main"):
    """Convenience: returns (text, keyboard) for the analytics screen."""
    return format_analytics(period=period, mode=mode), build_analytics_keyboard(period=period, mode=mode)


def parse_period_from_callback(callback_data: str) -> str:
    """Maps callback_data to period code."""
    cd = str(callback_data or "").strip()
    if cd == CB_STATS_7D:
        return "7d"
    if cd == CB_STATS_30D:
        return "30d"
    if cd == CB_STATS_ALL:
        return "all"
    return "24h"


analytics = SimpleNamespace(
    CB_STATS_24H=CB_STATS_24H,
    CB_STATS_7D=CB_STATS_7D,
    CB_STATS_30D=CB_STATS_30D,
    CB_STATS_ALL=CB_STATS_ALL,
    CB_BACK=CB_BACK,
    get_statistics=get_statistics,
    update_order_charge_and_net=update_order_charge_and_net,
    get_period_analytics=get_period_analytics,
    format_analytics=format_analytics,
    build_analytics_keyboard=build_analytics_keyboard,
    get_analytics_text_and_kb=get_analytics_text_and_kb,
    parse_period_from_callback=parse_period_from_callback,
)

def _render_analytics(call: types.CallbackQuery, period: str = "day", mode: Optional[str] = None):
    period_map = {
        "day": "24h",
        "week": "7d",
        "month": "30d",
        "all": "all",
        "24h": "24h",
        "7d": "7d",
        "30d": "30d",
    }
    period_norm = period_map.get(str(period or "day").strip().lower(), "24h")
    if mode is None:
        mode = _get_analytics_view_mode(call.message.chat.id, call.message.message_id)
    else:
        _set_analytics_view_mode(call.message.chat.id, call.message.message_id, mode)

    try:
        txt = analytics.format_analytics(period=period_norm, top_n=5, last_n=8, mode=mode)
    except Exception as e:
        logger.error(f"Ошибка формирования аналитики: {e}")
        txt = "❌ Ошибка формирования аналитики."

    kb = analytics.build_analytics_keyboard(period=period_norm, mode=mode)

    try:
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения аналитики: {e}")
        try:
            sent = bot.send_message(call.message.chat.id, txt, parse_mode="HTML", reply_markup=kb)
            if sent is not None:
                _set_analytics_view_mode(call.message.chat.id, getattr(sent, "message_id", 0), mode)
        except Exception as e2:
            logger.error(f"Ошибка при отправке сообщения аналитики: {e2}")

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def show_orders(call: types.CallbackQuery):
    _render_analytics(call, period="day", mode="main")


def analytics_period(call: types.CallbackQuery):
    try:
        period = call.data.split("analytics_period_", 1)[1].strip()
    except Exception:
        period = "day"
    if period not in ("day", "week", "month", "all"):
        period = "day"
    _render_analytics(call, period=period)


def analytics_more(call: types.CallbackQuery):
    try:
        period = call.data.split("analytics_more_", 1)[1].strip()
    except Exception:
        period = "day"
    if period not in ("day", "week", "month", "all"):
        period = "day"
    _render_analytics(call, period=period, mode="more")


def analytics_less(call: types.CallbackQuery):
    try:
        period = call.data.split("analytics_less_", 1)[1].strip()
    except Exception:
        period = "day"
    if period not in ("day", "week", "month", "all"):
        period = "day"
    _render_analytics(call, period=period, mode="main")


# =========================================================
# ALERTS / NOTIFICATIONS
# =========================================================

def _send_admin_order_alert(
    order_id_funpay: str,
    reason: str,
    action: str,
    title: str = "⚠️ Заказ требует ручной проверки",
    bot_action: Optional[str] = None,
):
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id or bot is None:
        return

    if bot_action is None:
        bot_action = "автоматическая обработка завершена без дополнительных действий"

    alert_text = (
        f"{title}\n"
        f"Заказ: #{order_id_funpay}\n"
        f"Причина: {reason}\n"
        f"Что сделал бот: {bot_action}\n"
        f"Действие: {action}\n"
        f"Ссылка: https://funpay.com/orders/{order_id_funpay}/"
    )

    try:
        bot.send_message(notification_chat_id, alert_text)
    except Exception as ex:
        logger.error(f"Не удалось отправить admin alert по заказу #{order_id_funpay}: {ex}")


def _send_manual_check_alert_once(order_record: Dict, reason: str = "не удалось подтвердить состояние заказа"):
    if not order_record:
        return
    if order_record.get("manual_check_alert_sent", False):
        return

    order_id_funpay = str(order_record.get("order_id"))
    _send_admin_order_alert(
        order_id_funpay=order_id_funpay,
        reason=reason,
        bot_action="автоматическая обработка остановлена",
        action="проверьте заказ вручную",
        title="⚠️ Заказ требует ручной проверки",
    )
    orders.update_order_fields(order_id_funpay, manual_check_alert_sent=True)
    order_record["manual_check_alert_sent"] = True


def _format_alert_money(value: Any) -> str:
    try:
        num = float(value)
    except Exception:
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{num:.2f}".rstrip("0").rstrip(".")


def send_order_started_notification(
    c: Cardinal,
    order_id_funpay: str,
    twiboost_id: int,
    link: str,
    api_url: str,
    api_key: str,
    lot_price: float,
    real_amount: int,
    service_number: Optional[int] = None,
    title: str = "🚀 AUTOSMM | Старт заказа",
    info_title: Optional[str] = None,
    reason: Optional[str] = None,
    action_text: Optional[str] = None,
):
    """
    Отправляет уведомление о начале заказа с информацией о сумме, затратах и прибыли.
    """
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id:
        logger.warning("Notification chat_id не задан в конфигурации.")
        return

    charge: Optional[float] = None
    currency: Optional[str] = None

    try:
        if service_number is not None:
            invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
            if invalid_key_payload:
                logger.warning(
                    f"{LOGGER_PREFIX} Пропускаю action=status для уведомления о старте заказа #{order_id_funpay}: {invalid_key_payload['detail']}"
                )
            else:
                status_url = f"{api_url}?action=status&order={twiboost_id}&key={api_key}"
                status_resp = requests.get(status_url, timeout=10)
                status_resp.raise_for_status()
                status_data = status_resp.json()

                charge = float(status_data.get("charge", "0") or 0)
                currency = str(status_data.get("currency", "USD") or "USD").upper()

                net_profit = round(float(lot_price) - charge, 2)

                try:
                    analytics.update_order_charge_and_net(order_id_funpay, charge, currency=currency, net_profit=net_profit)
                except Exception:
                    pass
    except Exception as ex:
        logger.error(f"Ошибка при получении данных action=status для уведомления о начале заказа #{order_id_funpay}: {ex}")

    try:
        stored_order = orders.get_order_data(order_id_funpay) or {}
        if charge is None:
            try:
                charge = float(stored_order.get("spent", 0) or 0)
            except Exception:
                charge = 0.0
        if not currency:
            currency = str(stored_order.get("currency", "RUB") or "RUB").upper()

        net_profit = round(float(lot_price) - float(charge or 0), 2)

        buyer_username = "—"
        try:
            order_data = c.account.get_order(order_id_funpay)
            buyer_username = getattr(order_data, "buyer_username", None) or "—"
        except Exception as ex:
            logger.warning(f"Не удалось получить buyer_username для заказа #{order_id_funpay}: {ex}")

        kb_ = InlineKeyboardMarkup()
        order_url = f"https://funpay.com/orders/{order_id_funpay}/"
        kb_.add(InlineKeyboardButton("Перейти к заказу", url=order_url))

        parts = [title, ""]
        if info_title:
            parts.extend([info_title, ""])

        parts.append(f"Заказ: #{order_id_funpay}")
        parts.append(f"Покупатель: {buyer_username}")
        parts.append("")

        if reason:
            parts.append(f"Причина: {reason}")
        if action_text:
            parts.append(f"Действие: {action_text}")
        if reason or action_text:
            parts.append("")

        parts.append(f"💰 FunPay: {_format_alert_money(lot_price)} ₽")
        parts.append(f"💸 Сайт: {_format_alert_money(charge)} {currency}")
        if str(currency or "").upper() == "RUB":
            parts.append(f"📈 Профит: {_format_alert_money(net_profit)} ₽")
        else:
            parts.append(f"📈 Профит: недоступен (валюта панели: {currency})")
        parts.append("")
        parts.append(f"📦 Итоговое количество: {real_amount}")
        parts.append(f"🔗 {order_url}")

        notification_text = "\n".join(parts)
        c.telegram.bot.send_message(
            notification_chat_id,
            notification_text,
            reply_markup=kb_
        )

    except Exception as ex:
        logger.error(f"Ошибка при отправке уведомления о начале заказа: {ex}")


def _send_completion_notification_if_needed(c: Cardinal, order_record: Dict):
    if not order_record:
        return
    if str(order_record.get("status", "") or "").lower() != "completed":
        return
    if bool(order_record.get("completed_notification_sent", False)):
        return
    if _get_effective_funpay_status(order_record) != "open":
        logger.info(
            f"Completion-уведомление пропущено для заказа #{order_record.get('order_id')}: "
            f"funpay_status={_get_effective_funpay_status(order_record)}"
        )
        return

    buyer_chat_id = order_record.get("chat_id")
    order_id_funpay = str(order_record.get("order_id"))
    twiboost_order_id = order_record.get("id_zakaz")
    cfg = load_config()
    template_text = (cfg.get("messages") or {}).get("after_completed") or DEFAULT_AFTER_COMPLETED_TEMPLATE
    message = _render_template(
        template_text,
        order_record,
        {
            "order_id_funpay": order_id_funpay,
            "panel_order_id": twiboost_order_id,
            "twiboost_id": twiboost_order_id,
            "lot_title": order_record.get("lot_title") or order_record.get("service_name") or "—",
        },
    )

    try:
        c.send_message(buyer_chat_id, message)
        orders.update_order_fields(order_id_funpay, completed_notification_sent=True, buyer_flow_state=BUYER_STATE_COMPLETED, panel_status="completed", updated_at=_now_str())
        order_record["completed_notification_sent"] = True
        logger.info(f"Уведомление о завершении отправлено покупателю {buyer_chat_id} (заказ #{twiboost_order_id}).")
    except Exception as ex:
        logger.error(f"Не удалось отправить уведомление о завершении для заказа #{order_id_funpay}: {ex}")


# =========================================================
# LOTS UI
# =========================================================

def _is_delete_confirm_grace_active(chat_id: Any) -> bool:
    try:
        return time.time() < float(DELETE_CONFIRM_GRACE_UNTIL.get(int(chat_id), 0) or 0)
    except Exception:
        return False


def _set_delete_confirm_grace(chat_id: Any, seconds: int = 30):
    try:
        DELETE_CONFIRM_GRACE_UNTIL[int(chat_id)] = time.time() + int(seconds or 30)
    except Exception:
        pass


def _truncate_button_text(value: Any, limit: int = 48) -> str:
    text_value = re.sub(r"\s+", " ", str(value or "Без названия")).strip() or "Без названия"
    if len(text_value) <= limit:
        return text_value
    return text_value[: max(1, limit - 1)].rstrip() + "…"


def _not_configured(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value or raw_value.lower() in ("none", "null"):
        return "не настроена"
    return raw_value

def _set_lot_page_context(chat_id: int, page: int) -> int:
    page = max(0, int(page or 0))
    LOT_PAGE_CONTEXT[int(chat_id)] = page
    return page


def _get_lot_page_context(chat_id: int) -> int:
    return max(0, int(LOT_PAGE_CONTEXT.get(int(chat_id), 0) or 0))


def _clear_step_handler(chat_id: int):
    try:
        bot.clear_step_handler_by_chat_id(chat_id)
    except Exception:
        pass


def _clear_navigation_states(chat_id: int):
    _clear_lot_input_state(chat_id)
    _clear_ui_input_state(chat_id)
    LOT_CARD_CONTEXT.pop(int(chat_id), None)


def _set_lot_card_context(chat_id: int, lot_key: str, back_callback: str, back_label: str):
    LOT_CARD_CONTEXT[int(chat_id)] = {
        "lot_key": lot_key,
        "back_callback": back_callback,
        "back_label": back_label,
    }


def _get_lot_card_context(chat_id: int) -> Optional[Dict[str, Any]]:
    return LOT_CARD_CONTEXT.get(int(chat_id))


def _set_ui_input_state(
    chat_id: int,
    prompt_text: str,
    next_handler,
    *handler_args,
    back_target: str = "return_to_settings",
    cancel_target: Optional[str] = None,
):
    UI_INPUT_STATES[int(chat_id)] = {
        "prompt_text": prompt_text,
        "next_handler": next_handler,
        "handler_args": tuple(handler_args),
        "back_target": back_target,
        "cancel_target": cancel_target,
    }


def _get_ui_input_state(chat_id: int) -> Optional[Dict[str, Any]]:
    return UI_INPUT_STATES.get(int(chat_id))


def _clear_ui_input_state(chat_id: int):
    UI_INPUT_STATES.pop(int(chat_id), None)


def _build_ui_input_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    state = _get_ui_input_state(chat_id) or {}
    back_target = state.get("back_target", "return_to_settings")
    cancel_target = state.get("cancel_target")

    kb_ = InlineKeyboardMarkup(row_width=2)
    if cancel_target and cancel_target != back_target:
        kb_.row(
            InlineKeyboardButton("◀️ Назад", callback_data="ui_input_back"),
            InlineKeyboardButton("✖️ Отмена", callback_data="ui_input_cancel"),
        )
    else:
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="ui_input_back"))
    return kb_


def _open_lots_page(chat_id: int, message_id: int, page: int):
    page = max(0, int(page or 0))
    _set_lot_page_context(chat_id, page)
    _clear_step_handler(chat_id)
    _clear_navigation_states(chat_id)
    bot.edit_message_text(
        "Выберите лот:",
        chat_id,
        message_id,
        reply_markup=generate_lots_keyboard(page),
    )


def _dispatch_navigation_callback(call: types.CallbackQuery, target: str):
    target = str(target or "return_to_settings")
    if target.startswith("return_to_lots"):
        try:
            page = int(target.rsplit("_", 1)[1])
        except Exception:
            page = _get_lot_page_context(call.message.chat.id)
        _open_lots_page(call.message.chat.id, call.message.message_id, page)
        return

    if target == "return_to_settings":
        return_to_settings(call)
    elif target == "lot_settings":
        lot_settings(call)
    elif target == "api_settings":
        api_settings_menu(call)
    elif target.startswith("api_service_"):
        try:
            service_idx = int(target.split("_")[-1])
        except Exception:
            api_settings_menu(call)
            return
        _open_api_service_card(call.message.chat.id, service_idx, message_id=call.message.message_id)
    elif target == "edit_messages":
        edit_messages_menu(call)
    elif target == "manage_websites":
        manage_websites(call)
    elif target == "files_menu":
        files_menu(call)
    elif target == "misc_settings":
        misc_settings(call)
    elif target == "links_menu":
        links_menu(call)
    elif target == "seller_notifications_settings":
        seller_notifications_settings(call)
    elif target == "add_new_lot":
        add_new_lot(call)
    elif target == "search_lot":
        search_lot_prompt(call)
    elif target == "search_results_back":
        search_results_back(call)
    else:
        return_to_settings(call)


def ui_input_back_callback(call: types.CallbackQuery):
    state = _get_ui_input_state(call.message.chat.id)
    _clear_step_handler(call.message.chat.id)
    _clear_ui_input_state(call.message.chat.id)
    _dispatch_navigation_callback(call, state.get("back_target") if state else "return_to_settings")


def ui_input_cancel_callback(call: types.CallbackQuery):
    state = _get_ui_input_state(call.message.chat.id)
    _clear_step_handler(call.message.chat.id)
    _clear_ui_input_state(call.message.chat.id)
    _dispatch_navigation_callback(
        call,
        state.get("cancel_target") if state and state.get("cancel_target") else (state.get("back_target") if state else "return_to_settings"),
    )


def _start_ui_input_flow(
    call: types.CallbackQuery,
    prompt_text: str,
    next_handler,
    *handler_args,
    back_target: str = "return_to_settings",
    cancel_target: Optional[str] = None,
):
    _clear_step_handler(call.message.chat.id)
    _clear_lot_input_state(call.message.chat.id)
    _set_ui_input_state(
        call.message.chat.id,
        prompt_text,
        next_handler,
        *handler_args,
        back_target=back_target,
        cancel_target=cancel_target,
    )
    msg_ = bot.edit_message_text(
        prompt_text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=_build_ui_input_keyboard(call.message.chat.id),
    )
    bot.register_next_step_handler(msg_, next_handler, *handler_args)


def _repeat_ui_input_flow(chat_id: int, error_text: str = ""):
    state = _get_ui_input_state(chat_id)
    if not state:
        return
    text_ = state["prompt_text"]
    if error_text:
        text_ = f"{error_text}\n\n{state['prompt_text']}"
    msg_ = bot.send_message(chat_id, text_, reply_markup=_build_ui_input_keyboard(chat_id))
    bot.register_next_step_handler(msg_, state["next_handler"], *state.get("handler_args", ()))


def _render_search_results(chat_id: int, search_term: str, message_id: Optional[int] = None):
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    term = str(search_term or "").strip().lower()
    SEARCH_RESULT_CONTEXT[int(chat_id)] = {"term": term}

    found_lots = []
    for lot_key, lot_data in lot_map.items():
        lot_name = str(lot_data.get("name", "")).lower()
        service_id = str(lot_data.get("service_id", "")).lower()
        lot_key_l = str(lot_key or "").lower()
        if term and (term in lot_name or term in service_id or term in lot_key_l):
            found_lots.append((lot_key, lot_data))

    kb = InlineKeyboardMarkup(row_width=1)
    if found_lots:
        for lot_key, lot_data in found_lots:
            name_ = lot_data.get("name", "Без названия")
            sid_ = lot_data.get("service_id")
            qty_ = lot_data.get("quantity")
            snum_ = lot_data.get("service_number", 1)
            btn_text = f"{name_} [ID={sid_}, Q={qty_}, панель={snum_}]"
            kb.add(InlineKeyboardButton(btn_text, callback_data=f"edit_lot_search_{lot_key}"))
        kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="search_lot"))
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="lot_settings"))
        text_ = f"🔍 Результаты поиска для '{term}':"
    else:
        kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="search_lot"))
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="lot_settings"))
        text_ = f"❌ Лоты по запросу '{term}' не найдены."

    if message_id is not None:
        bot.edit_message_text(text_, chat_id, message_id, reply_markup=kb)
    else:
        bot.send_message(chat_id, text_, reply_markup=kb)


def search_results_back(call: types.CallbackQuery):
    ctx = SEARCH_RESULT_CONTEXT.get(int(call.message.chat.id))
    if not ctx or not ctx.get("term"):
        lot_settings(call)
        return
    _clear_step_handler(call.message.chat.id)
    _clear_lot_input_state(call.message.chat.id)
    _clear_ui_input_state(call.message.chat.id)
    _render_search_results(call.message.chat.id, ctx["term"], call.message.message_id)


def edit_lot_from_search(call: types.CallbackQuery):
    lot_key = call.data.split("edit_lot_search_", 1)[1]
    _clear_step_handler(call.message.chat.id)
    _clear_lot_input_state(call.message.chat.id)
    _clear_ui_input_state(call.message.chat.id)
    _show_lot_card(
        call.message.chat.id,
        lot_key,
        call.message.message_id,
        back_callback="search_results_back",
        back_label="◀️ Назад",
    )


def header_no_action_callback(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def _set_lot_input_state(chat_id: int, lot_key: str, mode: str, prompt_text: str, next_handler):
    card_ctx = _get_lot_card_context(chat_id) or {}
    LOT_INPUT_STATES[int(chat_id)] = {
        "mode": mode,
        "lot_key": lot_key,
        "from": "lot_card",
        "back_target": card_ctx.get("back_callback") or "lot_settings",
        "back_label": card_ctx.get("back_label") or "◀️ Назад",
        "page": _get_lot_page_context(chat_id),
        "prompt_text": prompt_text,
        "next_handler": next_handler,
    }


def _get_lot_input_state(chat_id: int) -> Optional[Dict[str, Any]]:
    return LOT_INPUT_STATES.get(int(chat_id))


def _clear_lot_input_state(chat_id: int):
    LOT_INPUT_STATES.pop(int(chat_id), None)


def _build_return_to_lots_callback(chat_id: int) -> str:
    return f"return_to_lots_{_get_lot_page_context(chat_id)}"


def _build_lot_input_keyboard() -> InlineKeyboardMarkup:
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="lot_input_back"))
    return kb_


def _show_lot_card(
    chat_id: int,
    lot_key: str,
    message_id: Optional[int] = None,
    back_callback: Optional[str] = None,
    back_label: Optional[str] = None,
):
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        text_ = f"❌ Лот {lot_key} не найден."
        if message_id is not None:
            bot.edit_message_text(text_, chat_id, message_id)
        else:
            bot.send_message(chat_id, text_)
        return

    if not back_callback:
        back_callback = _build_return_to_lots_callback(chat_id)
    if not back_label:
        back_label = "◀️ К списку" if str(back_callback).startswith("return_to_lots") else "◀️ Назад"

    _set_lot_card_context(chat_id, lot_key, back_callback, back_label)

    ld_ = lot_map[lot_key]
    fallback_number = _not_configured(ld_.get("fallback_service_number"))
    fallback_id = _not_configured(ld_.get("fallback_service_id"))
    txt_ = "\n".join([
        f"<b>{html.escape(str(lot_key))}</b>",
        "",
        "<b>Название:</b>",
        html.escape(str(ld_.get("name") or "Без названия")),
        "",
        f"Номер панели: <code>{html.escape(str(ld_.get('service_number', 1)))}</code>",
        f"ID услуги: <code>{html.escape(str(ld_.get('service_id', '—')))}</code>",
        f"Кол-во: <code>{html.escape(str(ld_.get('quantity', '—')))}</code>",
        "",
        "━━━━━━━━━━━━━━",
        "<b>Резервная панель</b>",
        "",
        f"Номер панели: <code>{html.escape(fallback_number)}</code>",
        f"ID услуги: <code>{html.escape(fallback_id)}</code>",
    ]).strip()

    kb_ = InlineKeyboardMarkup(row_width=1)
    card_buttons = [
        InlineKeyboardButton("Изменить название", callback_data=f"change_name_{lot_key}"),
        InlineKeyboardButton("Изменить ID услуги", callback_data=f"change_id_{lot_key}"),
        InlineKeyboardButton("Изменить количество", callback_data=f"change_quantity_{lot_key}"),
        InlineKeyboardButton("Изменить номер панели", callback_data=f"change_snum_{lot_key}"),
        InlineKeyboardButton("Изменить номер резервной панели", callback_data=f"change_fallback_snum_{lot_key}"),
        InlineKeyboardButton("Изменить ID резервной услуги", callback_data=f"change_fallback_id_{lot_key}"),
    ]
    kb_.add(*card_buttons)
    kb_.add(InlineKeyboardButton("❌ Удалить лот", callback_data=f"delete_one_lot_{lot_key}"))
    kb_.add(InlineKeyboardButton(back_label, callback_data=back_callback))

    if message_id is not None:
        bot.edit_message_text(txt_, chat_id, message_id, parse_mode="HTML", reply_markup=kb_)
    else:
        bot.send_message(chat_id, txt_, parse_mode="HTML", reply_markup=kb_)


def _start_lot_edit_flow(call: types.CallbackQuery, lot_key: str, mode: str, prompt_text: str, next_handler):
    _clear_step_handler(call.message.chat.id)
    _clear_ui_input_state(call.message.chat.id)
    _set_lot_input_state(call.message.chat.id, lot_key, mode, prompt_text, next_handler)
    msg_ = bot.edit_message_text(
        prompt_text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=_build_lot_input_keyboard(),
    )
    bot.register_next_step_handler(msg_, next_handler, lot_key)


def lot_input_back_callback(call: types.CallbackQuery):
    state = _get_lot_input_state(call.message.chat.id)
    _clear_step_handler(call.message.chat.id)
    _clear_lot_input_state(call.message.chat.id)
    if state and state.get("lot_key"):
        _show_lot_card(
            call.message.chat.id,
            state["lot_key"],
            call.message.message_id,
            back_callback=state.get("back_target"),
            back_label=state.get("back_label"),
        )
    else:
        lot_settings(call)


def lot_input_cancel_callback(call: types.CallbackQuery):
    lot_input_back_callback(call)


def generate_lots_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    items = list(lot_map.items())

    per_page = 10
    total_pages = max(1, ((len(items) - 1) // per_page) + 1) if items else 1
    page = max(0, min(int(page or 0), total_pages - 1))

    start_ = page * per_page
    end_ = start_ + per_page
    chunk = items[start_:end_]

    kb_ = InlineKeyboardMarkup(row_width=1)
    for lot_key, lot_data in chunk:
        name_ = lot_data.get("name", "Без названия")
        btn_text = _truncate_button_text(name_)
        cd_ = f"edit_lot_{lot_key}"
        kb_.add(InlineKeyboardButton(btn_text, callback_data=cd_))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"prev_page_{page-1}"))
    if end_ < len(items):
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"next_page_{page+1}"))
    if nav_buttons:
        kb_.row(*nav_buttons)

    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))
    return kb_


def edit_lot(call: types.CallbackQuery, lot_key: str):
    _clear_lot_input_state(call.message.chat.id)
    _show_lot_card(call.message.chat.id, lot_key, call.message.message_id)


def delete_one_lot(call: types.CallbackQuery, lot_key: str):
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key in lot_map:
        del lot_map[lot_key]
        cfg["lot_mapping"] = lot_map
        reindex_lots(cfg)
        total_lots = len(cfg.get("lot_mapping", {}))
        max_page = max(0, ((total_lots - 1) // 10)) if total_lots else 0
        page_ = min(_get_lot_page_context(call.message.chat.id), max_page)
        _set_lot_page_context(call.message.chat.id, page_)
        bot.edit_message_text(
            f"✅ Лот {lot_key} удалён и лоты переиндексированы.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=generate_lots_keyboard(page_),
        )
    else:
        bot.edit_message_text(f"❌ Лот {lot_key} не найден.", call.message.chat.id, call.message.message_id)


def delete_all_lots_func(call: types.CallbackQuery):
    cfg = load_config()
    cfg["lot_mapping"] = {}
    save_config(cfg)
    _set_lot_page_context(call.message.chat.id, 0)
    bot.edit_message_text(
        "✅ Все лоты успешно удалены. Настройки сохранены.",
        call.message.chat.id,
        call.message.message_id,
    )


def _finish_lot_edit(message: types.Message, lot_key: str, success_text: str):
    state = _get_lot_input_state(message.chat.id) or {}
    _clear_lot_input_state(message.chat.id)
    bot.send_message(message.chat.id, success_text)
    _show_lot_card(
        message.chat.id,
        lot_key,
        back_callback=state.get("back_target"),
        back_label=state.get("back_label"),
    )


def _repeat_lot_edit_flow(chat_id: int, error_text: str = ""):
    state = _get_lot_input_state(chat_id)
    if not state:
        return
    text_ = state["prompt_text"]
    if error_text:
        text_ = f"{error_text}\n\n{state['prompt_text']}"
    msg_ = bot.send_message(chat_id, text_, reply_markup=_build_lot_input_keyboard())
    bot.register_next_step_handler(msg_, state["next_handler"], state["lot_key"])


def process_name_change(message: types.Message, lot_key: str):
    new_name = message.text.strip()
    if not new_name:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: Название не может быть пустым.")
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return
    lot_map[lot_key]["name"] = new_name
    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Название лота {lot_key} изменено на {new_name}.")


def process_id_change(message: types.Message, lot_key: str):
    try:
        new_id = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: ID услуги должно быть числом.")
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return
    lot_map[lot_key]["service_id"] = new_id
    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _finish_lot_edit(message, lot_key, f"✅ ID услуги для {lot_key} изменён на {new_id}.")


def process_quantity_change(message: types.Message, lot_key: str):
    try:
        new_q = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: Количество должно быть числом.")
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return
    lot_map[lot_key]["quantity"] = new_q
    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Количество для {lot_key} изменено на {new_q}.")


def process_service_num_change(message: types.Message, lot_key: str):
    try:
        new_snum = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: Введите номер сервиса (число).")
        return

    cfg = load_config()
    if str(new_snum) not in cfg.get("services", {}):
        _repeat_lot_edit_flow(message.chat.id, f"❌ Ошибка: панель #{new_snum} не существует.")
        return

    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["service_number"] = new_snum
    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Номер сервиса для {lot_key} изменён на {new_snum}.")


def process_new_lot_id_step(message: types.Message):
    try:
        lot_id = int(message.text.strip())
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: ID лота должно быть числом.")
        return

    try:
        lot_fields = cardinal_instance.account.get_lot_fields(lot_id)
        fields = lot_fields.fields
        name = fields.get("fields[summary][ru]", "Без названия")
    except Exception as e:
        _repeat_ui_input_flow(message.chat.id, f"❌ Не удалось получить данные лота: {e}")
        return

    cfg = load_config()
    reindex_lots(cfg)
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})

    lot_indices = []
    for lot_key in lot_map.keys():
        if str(lot_key).startswith("lot_"):
            try:
                lot_indices.append(int(str(lot_key).split("_", 1)[1]))
            except Exception:
                continue
    new_lot_key = f"lot_{max(lot_indices, default=0) + 1}"

    lot_map[new_lot_key] = {
        "name": name,
        "service_id": 1,
        "quantity": 1,
        "service_number": 1,
        "fallback_service_number": None,
        "fallback_service_id": None
    }

    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _clear_ui_input_state(message.chat.id)

    page_ = max(0, ((len(lot_map) - 1) // 10))
    _set_lot_page_context(message.chat.id, page_)
    bot.send_message(message.chat.id, f"✅ Добавлен новый лот {new_lot_key} с названием: {name}")
    _show_lot_card(
        message.chat.id,
        new_lot_key,
        back_callback=f"return_to_lots_{page_}",
        back_label="◀️ К списку",
    )


def process_fallback_snum_change(message: types.Message, lot_key: str):
    try:
        new_snum = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: введите номер резервной панели числом.")
        return

    cfg = load_config()
    if str(new_snum) not in cfg.get("services", {}):
        _repeat_lot_edit_flow(message.chat.id, f"❌ Ошибка: панель #{new_snum} не существует.")
        return

    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["fallback_service_number"] = new_snum
    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Номер резервной панели для {lot_key} изменён на {new_snum}.")


def process_fallback_id_change(message: types.Message, lot_key: str):
    try:
        new_id = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: ID резервной услуги должен быть числом.")
        return

    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["fallback_service_id"] = new_id
    cfg["lot_mapping"] = lot_map
    reindex_lots(cfg)
    _finish_lot_edit(message, lot_key, f"✅ ID резервной услуги для {lot_key} изменён на {new_id}.")


def lot_settings(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔍 Поиск лота", callback_data="search_lot"))
    kb.add(InlineKeyboardButton("📋 Список лотов", callback_data="show_lots_list"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))
    bot.edit_message_text("Управление лотами:", call.message.chat.id, call.message.message_id, reply_markup=kb)


def show_lots_list(call: types.CallbackQuery):
    SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)
    _open_lots_page(call.message.chat.id, call.message.message_id, 0)


def search_lot_prompt(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Введите название или часть названия лота для поиска:",
        process_lot_search,
        back_target="lot_settings",
    )


def process_lot_search(message: types.Message):
    search_term = message.text.strip()
    if not search_term:
        _repeat_ui_input_flow(message.chat.id, "❌ Поисковый запрос не может быть пустым.")
        return

    _clear_ui_input_state(message.chat.id)
    _render_search_results(message.chat.id, search_term)


def edit_lot_callback(call: types.CallbackQuery):
    lot_key = call.data.split("_", 2)[2]
    edit_lot(call, lot_key)


def page_navigation(call: types.CallbackQuery):
    try:
        page_ = int(call.data.split("_")[-1])
    except ValueError:
        page_ = 0
    _open_lots_page(call.message.chat.id, call.message.message_id, page_)


def return_to_lots(call: types.CallbackQuery):
    try:
        page_ = int(call.data.rsplit("_", 1)[1]) if call.data.startswith("return_to_lots_") else _get_lot_page_context(call.message.chat.id)
    except ValueError:
        page_ = _get_lot_page_context(call.message.chat.id)
    _open_lots_page(call.message.chat.id, call.message.message_id, page_)


def change_name(call: types.CallbackQuery):
    lot_key = call.data.split("_", 2)[2]
    _start_lot_edit_flow(call, lot_key, "change_name", f"Введите новое название для {lot_key}:", process_name_change)


def change_id(call: types.CallbackQuery):
    lot_key = call.data.split("_", 2)[2]
    _start_lot_edit_flow(call, lot_key, "change_id", f"Введите новый ID услуги для {lot_key}:", process_id_change)


def change_quantity(call: types.CallbackQuery):
    lot_key = call.data.split("_", 2)[2]
    _start_lot_edit_flow(call, lot_key, "change_quantity", f"Введите новое количество для {lot_key}:", process_quantity_change)


def change_snum(call: types.CallbackQuery):
    lot_key = call.data.split("_", 2)[2]
    _start_lot_edit_flow(call, lot_key, "change_snum", f"Введите номер панели для {lot_key}:", process_service_num_change)


def delete_one_lot_callback(call: types.CallbackQuery):
    lot_key = call.data.split("_", 3)[3]
    if _is_delete_confirm_grace_active(call.message.chat.id):
        delete_one_lot(call, lot_key)
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {}) or {}
    lot_data = lot_map.get(lot_key) or {}
    lot_title = str(lot_data.get("name") or lot_key)
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("✅ Удалить", callback_data=f"confirm_delete_one_lot_{lot_key}"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data=f"edit_lot_{lot_key}"))
    txt_ = f"""⚠️ <b>Удалить лот?</b>

Лот:
{html.escape(lot_title)}

Будет удалена только связка AutoSMM.
FunPay-лот на сайте не удаляется.

После подтверждения следующие удаления в течение 30 секунд будут выполняться без повторного подтверждения."""
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def confirm_delete_one_lot_callback(call: types.CallbackQuery):
    lot_key = call.data.split("confirm_delete_one_lot_", 1)[1]
    _set_delete_confirm_grace(call.message.chat.id, 30)
    delete_one_lot(call, lot_key)


def add_new_lot(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _start_ui_input_flow(
        call,
        "Введите ID лота для добавления:",
        process_new_lot_id_step,
        back_target="return_to_settings",
    )


def update_lot_ids(call: types.CallbackQuery):
    cfg = load_config()
    reindex_lots(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)



# =========================================================
# TELEMETRY / BACKUP HELPERS
# =========================================================

def _parse_autosmm_dt(value: Any) -> Optional[datetime]:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw_value, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw_value)
    except Exception:
        return None


def _save_autosmm_state(state: Dict) -> None:
    _safe_write_json(AUTOSMM_STATE_PATH, state if isinstance(state, dict) else {})


def _get_telemetry_consent_state() -> Optional[bool]:
    state = _load_autosmm_state()
    if TELEMETRY_CONSENT_KEY not in state:
        return None
    return bool(state.get(TELEMETRY_CONSENT_KEY))


def _set_telemetry_consent(value: bool) -> None:
    state = _load_autosmm_state()
    state[TELEMETRY_CONSENT_KEY] = bool(value)
    _save_autosmm_state(state)


def _telemetry_consent_accepted() -> bool:
    return _get_telemetry_consent_state() is True


def _telemetry_guest_used() -> bool:
    state = _load_autosmm_state()
    return state.get(TELEMETRY_GUEST_USED_KEY) is True


def _telemetry_guest_active() -> bool:
    state = _load_autosmm_state()
    guest_until = _parse_autosmm_dt(state.get(TELEMETRY_GUEST_UNTIL_KEY))
    return bool(guest_until and guest_until > datetime.now())


def _telemetry_allows_ping() -> bool:
    return _telemetry_consent_accepted()


def _activate_telemetry_guest_24h() -> bool:
    state = _load_autosmm_state()
    if state.get(TELEMETRY_CONSENT_KEY) is True:
        return True
    if state.get(TELEMETRY_GUEST_USED_KEY) is True:
        return False
    now_dt = datetime.now()
    state[TELEMETRY_GUEST_USED_KEY] = True
    state[TELEMETRY_GUEST_STARTED_AT_KEY] = now_dt.isoformat(timespec="seconds")
    state[TELEMETRY_GUEST_UNTIL_KEY] = (now_dt + timedelta(hours=24)).isoformat(timespec="seconds")
    _save_autosmm_state(state)
    return True


def _normalize_funpay_username(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_funpay_id(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_funpay_user_id(value: Any) -> bool:
    raw = _normalize_funpay_id(value)
    return raw.isdigit() and 3 <= len(raw) <= 20


def _looks_like_funpay_username(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if len(raw) > 64:
        return False
    if raw.startswith("<") or raw.startswith("{"):
        return False
    return True


def _safe_get_identity_field(obj: Any, field_name: str) -> Any:
    if obj is None:
        return None
    try:
        if isinstance(obj, dict):
            return obj.get(field_name)
        value = getattr(obj, field_name, None)
        if callable(value):
            return None
        return value
    except Exception:
        return None


def _extract_identity_pair_from_obj(obj: Any, source_name: str) -> Optional[Dict[str, str]]:
    """Extract a seller FunPay identity only when username and user_id come from the same object."""
    if obj is None:
        return None

    username_value = None
    for field_name in ("username", "user_name", "login", "name"):
        candidate = _safe_get_identity_field(obj, field_name)
        if _looks_like_funpay_username(candidate):
            username_value = candidate
            break

    user_id_value = None
    for field_name in ("user_id", "account_id", "id"):
        candidate = _safe_get_identity_field(obj, field_name)
        if _looks_like_funpay_user_id(candidate):
            user_id_value = candidate
            break

    if not _looks_like_funpay_username(username_value) or not _looks_like_funpay_user_id(user_id_value):
        return None

    return {
        "source": str(source_name or "unknown"),
        "username": _normalize_funpay_username(username_value),
        "user_id": _normalize_funpay_id(user_id_value),
    }


def _safe_call_account_identity_method(account: Any, method_name: str) -> Any:
    try:
        method = getattr(account, method_name, None)
        if not callable(method):
            return None
        return method()
    except Exception:
        return None


def _get_funpay_identity(c: Optional[Cardinal] = None) -> Tuple[str, str, str]:
    """Return stable seller FunPay identity as (username, user_id, source).

    The function intentionally accepts only complete username+user_id pairs from a single
    Cardinal/FunPayAPI source object. It never mixes username from one source with user_id
    from another source, because that can make anonymous_install_id unstable.
    """
    account = getattr(c or cardinal_instance, "account", None)
    if account is None:
        return "", "", "unavailable"

    candidate_sources: List[Tuple[str, Any]] = []
    for attr_name in ("profile", "me", "user"):
        candidate_sources.append((f"account.{attr_name}", _safe_get_identity_field(account, attr_name)))
    candidate_sources.append(("account", account))

    for method_name in ("get_me", "get_profile", "get_user"):
        candidate_sources.append((f"account.{method_name}()", _safe_call_account_identity_method(account, method_name)))

    for source_name, source_obj in candidate_sources:
        pair = _extract_identity_pair_from_obj(source_obj, source_name)
        if not pair:
            continue
        return pair["username"], pair["user_id"], pair["source"]

    return "", "", "unavailable"


def build_anonymous_install_id(funpay_username: str, funpay_id: str) -> str:
    username = _normalize_funpay_username(funpay_username)
    user_id = _normalize_funpay_id(funpay_id)

    if not username or not user_id:
        raise ValueError("FunPay username and FunPay ID are required for AutoSMM telemetry id")

    raw = f"autosmm:v1:{username}:{user_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _looks_like_anonymous_install_id(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{16}", str(value or "").strip().lower()))


def _get_anonymous_install_id(c: Optional[Cardinal] = None) -> str:
    state = _load_autosmm_state()
    cached_hash = str(state.get(TELEMETRY_IDENTITY_HASH_KEY) or "").strip().lower()
    if cached_hash and not _looks_like_anonymous_install_id(cached_hash):
        cached_hash = ""

    username, user_id, source = _get_funpay_identity(c)
    new_hash = build_anonymous_install_id(username, user_id)

    if cached_hash:
        if cached_hash == new_hash:
            return cached_hash

        state[TELEMETRY_IDENTITY_DEBUG_KEY] = {
            "last_mismatch_at": _now_str(),
            "cached_hash_prefix": cached_hash[:8],
            "new_hash_prefix": new_hash[:8],
            "source": source,
        }
        _save_autosmm_state(state)
        logger.warning(
            "TELEMETRY IDENTITY MISMATCH: cached hash differs from current FunPay identity hash; using cached hash"
        )
        return cached_hash

    state[TELEMETRY_IDENTITY_HASH_KEY] = new_hash
    state[TELEMETRY_IDENTITY_SOURCE_KEY] = source
    _save_autosmm_state(state)
    return new_hash


def _mask_identity_value(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    return f"{raw[:2]}***"


def _build_telemetry_identity_debug_text(c: Optional[Cardinal] = None) -> str:
    state = _load_autosmm_state()
    cached_hash = str(state.get(TELEMETRY_IDENTITY_HASH_KEY) or "").strip().lower()
    try:
        username, user_id, source = _get_funpay_identity(c)
        current_hash = build_anonymous_install_id(username, user_id)
        return "\n".join([
            "Telemetry identity debug:",
            f"source: {h(source or 'unavailable')}",
            f"username: {h(_mask_identity_value(username))}",
            f"user_id: {h(_mask_identity_value(user_id))}",
            f"hash: {h(current_hash[:8])}***",
            f"cached_hash: {h(cached_hash[:8] + '***' if cached_hash else '—')}",
        ])
    except Exception:
        return "\n".join([
            "Telemetry identity debug:",
            "source: unavailable",
            "username: —",
            "user_id: —",
            "hash: —",
            f"cached_hash: {h(cached_hash[:8] + '***' if cached_hash else '—')}",
        ])


def _build_telemetry_payload(c: Optional[Cardinal] = None) -> Dict[str, str]:
    return {
        "plugin_name": NAME,
        "version": VERSION,
        "anonymous_install_id": _get_anonymous_install_id(c),
    }


def _send_telemetry_ping_background(c: Optional[Cardinal] = None) -> None:
    try:
        payload = _build_telemetry_payload(c or cardinal_instance)
    except ValueError:
        state = _load_autosmm_state()
        state[TELEMETRY_LAST_PING_ERROR_KEY] = "cannot resolve FunPay username/id"
        _save_autosmm_state(state)
        logger.warning("TELEMETRY ID FAILED: cannot resolve FunPay username/id")
        return

    try:
        requests.post(TELEMETRY_URL, json=payload, timeout=TELEMETRY_TIMEOUT_SECONDS)
        state = _load_autosmm_state()
        state[TELEMETRY_LAST_PING_OK_KEY] = _now_str()
        state[TELEMETRY_LAST_PING_ERROR_KEY] = None
        _save_autosmm_state(state)
    except Exception as ex:
        state = _load_autosmm_state()
        state[TELEMETRY_LAST_PING_ERROR_KEY] = str(ex)[:300]
        _save_autosmm_state(state)
        logger.warning(f"TELEMETRY PING FAILED: {ex}")


def _maybe_send_telemetry_ping(c: Optional[Cardinal] = None, force: bool = False) -> None:
    state = _load_autosmm_state()
    if not _telemetry_allows_ping():
        return
    if not force:
        last_attempt = _parse_autosmm_dt(state.get(TELEMETRY_LAST_PING_ATTEMPT_KEY))
        if last_attempt and (datetime.now() - last_attempt).total_seconds() < TELEMETRY_PING_INTERVAL_SECONDS:
            return
    state[TELEMETRY_LAST_PING_ATTEMPT_KEY] = _now_str()
    _save_autosmm_state(state)
    try:
        threading.Thread(target=_send_telemetry_ping_background, args=(c or cardinal_instance,), daemon=True).start()
    except Exception as ex:
        state = _load_autosmm_state()
        state[TELEMETRY_LAST_PING_ERROR_KEY] = str(ex)[:300]
        _save_autosmm_state(state)
        logger.warning(f"TELEMETRY THREAD FAILED: {ex}")


def _build_telemetry_consent_text(c: Optional[Cardinal] = None) -> str:
    try:
        anonymous_install_id = _get_anonymous_install_id(c or cardinal_instance)
    except ValueError:
        anonymous_install_id = "будет сформирован после получения FunPay username и FunPay ID"

    return f"""📊 <b>Телеметрия AutoSMM</b>

<b>AutoSMM</b> — бесплатный плагин.

Для работы <b>AutoSMM v{h(VERSION)}</b> требуется согласие на минимальную техническую статистику.

<b>Как формируется ID:</b>
<blockquote>• <i>AutoSMM локально берёт FunPay username;</i>
• <i>AutoSMM локально берёт FunPay ID;</i>
• <i>из них формируется короткий SHA-256 хеш;</i>
• <i>сырые FunPay username и FunPay ID на сервер не отправляются.</i>
</blockquote>

<b>Отправляется:</b>
<blockquote>• <i>название плагина;</i>
• <i>версия плагина;</i>
• <i>обезличенный технический ID;</i>
• <i>факт запуска.</i>
</blockquote>

<b>Не отправляются:</b>
<blockquote>• <i>FunPay username в открытом виде;</i>
• <i>FunPay ID в открытом виде;</i>
• <i>переписки;</i>
• <i>ссылки покупателей;</i>
• <i>заказы;</i>
• <i>API-ключи;</i>
• <i>cookies;</i>
• <i>настройки лотов.</i>
</blockquote>

На сервер отправляется только короткий хеш.

Пример обезличенного ID: <tg-spoiler>{h(anonymous_install_id)}</tg-spoiler>

<b>Продолжить использование AutoSMM?</b>

Если не хотите использовать телеметрию, можно остаться на <a href=\"{h(AUTOSMM_V008_NO_TELEMETRY_URL)}\">AutoSMM v0.0.8</a> без неё."""

def _build_telemetry_consent_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("✅ Согласиться и продолжить", callback_data="autosmm_telemetry_accept"))
    kb.add(InlineKeyboardButton("🛡 Насколько это безопасно?", url=TELEMETRY_SECURITY_URL))
    kb.add(InlineKeyboardButton("⛔ Не использовать v0.0.9", callback_data="autosmm_telemetry_refuse"))
    return kb


def _build_telemetry_refuse_text() -> str:
    if _telemetry_guest_used() and not _telemetry_guest_active():
        return f"""⏳ <b>Гостевой режим AutoSMM завершён</b>

Вы уже использовали 24 часа без телеметрии.

Чтобы продолжить использовать <b>AutoSMM v{h(VERSION)}</b>, примите минимальную техническую статистику.

Если не хотите использовать телеметрию, можно остаться на <a href=\"{h(AUTOSMM_V008_NO_TELEMETRY_URL)}\">AutoSMM v0.0.8</a> без неё."""
    return f"""⛔ <b>Телеметрия не принята</b>

AutoSMM v{h(VERSION)} использует минимальную техническую статистику для бесплатной версии.

Вы можете один раз попробовать плагин <b>24 часа без телеметрии</b>, чтобы проверить работу перед решением.

<blockquote>• телеметрия в гостевом режиме не отправляется;
• через 24 часа AutoSMM снова попросит выбрать;
• повторно включить гостевой режим нельзя.
</blockquote>

Выберите действие:"""


def _build_telemetry_refuse_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    if not _telemetry_guest_used():
        kb.add(InlineKeyboardButton("🧪 Попробовать 24 часа без телеметрии", callback_data="autosmm_telemetry_guest_24h"))
    kb.add(InlineKeyboardButton("✅ Согласиться и продолжить", callback_data="autosmm_telemetry_accept"))
    kb.add(InlineKeyboardButton("🛡 Насколько это безопасно?", url=TELEMETRY_SECURITY_URL))
    kb.add(InlineKeyboardButton("📦 AutoSMM v0.0.8 без телеметрии", url=AUTOSMM_V008_NO_TELEMETRY_URL))
    return kb


def _show_telemetry_gate(chat_id: int, message_id: Optional[int] = None):
    consent = _get_telemetry_consent_state()
    if consent is False:
        text_value = _build_telemetry_refuse_text()
        kb = _build_telemetry_refuse_keyboard()
    else:
        text_value = _build_telemetry_consent_text(cardinal_instance)
        kb = _build_telemetry_consent_keyboard()
    if message_id is None:
        bot.send_message(chat_id, clean_tg_text(text_value), parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        bot.edit_message_text(clean_tg_text(text_value), chat_id, message_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


def _telemetry_gate_allows_menu() -> bool:
    return _telemetry_consent_accepted() or _telemetry_guest_active()


def telemetry_accept_callback(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _set_telemetry_consent(True)
    _open_main_settings_menu(call.message.chat.id, message_id=call.message.message_id)
    _maybe_send_telemetry_ping(cardinal_instance, force=True)


def telemetry_refuse_callback(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _set_telemetry_consent(False)
    bot.edit_message_text(
        clean_tg_text(_build_telemetry_refuse_text()),
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=_build_telemetry_refuse_keyboard(),
        disable_web_page_preview=True,
    )


def telemetry_guest_24h_callback(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if _telemetry_consent_accepted():
        _open_main_settings_menu(call.message.chat.id, message_id=call.message.message_id)
        return
    if not _activate_telemetry_guest_24h():
        bot.edit_message_text(
            clean_tg_text(_build_telemetry_refuse_text()),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=_build_telemetry_refuse_keyboard(),
            disable_web_page_preview=True,
        )
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🚀 Открыть AutoSMM", callback_data="return_to_settings"))
    text_value = """🧪 <b>Гостевой режим включён</b>

AutoSMM v0.0.9 доступен на <b>24 часа без телеметрии</b>.

Через 24 часа плагин снова попросит выбрать: принять телеметрию или остаться на AutoSMM v0.0.8."""
    bot.edit_message_text(
        clean_tg_text(text_value),
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


def _backup_default_for_file(file_name: str):
    default_value = AUTOSMM_BACKUP_DEFAULTS.get(file_name, {})
    if isinstance(default_value, (dict, list)):
        return json.loads(json.dumps(default_value, ensure_ascii=False))
    return default_value


def _read_backup_file_payload(file_name: str, path: str):
    if not os.path.exists(path):
        return _backup_default_for_file(file_name)
    return _safe_load_json(path, _backup_default_for_file(file_name))


def _build_full_backup_payload(reason: str = "manual_export") -> Dict[str, Any]:
    return {
        "autosmm_backup_version": AUTOSMM_BACKUP_VERSION,
        "plugin": {
            "name": NAME,
            "version": VERSION,
            "created_at": _now_str(),
            "reason": reason,
        },
        "files": {
            file_name: _read_backup_file_payload(file_name, file_path)
            for file_name, file_path in AUTOSMM_BACKUP_FILE_MAP.items()
        },
    }


def _backup_filename(prefix: str = "autosmm_backup") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{prefix}_{VERSION}_{stamp}.json"


def _create_full_backup(reason: str = "manual_export") -> Optional[str]:
    try:
        os.makedirs(AUTOSMM_BACKUPS_DIR, exist_ok=True)
        prefix = "autosmm_emergency_backup" if str(reason or "").startswith("before_") else "autosmm_backup"
        backup_path = os.path.join(AUTOSMM_BACKUPS_DIR, _backup_filename(prefix))
        payload = _build_full_backup_payload(reason=reason)
        _safe_write_json(backup_path, payload)
        return backup_path
    except Exception as ex:
        logger.error(f"Не удалось создать полный бэкап AutoSMM: {ex}")
        return None


def _clear_autosmm_memory_contexts():
    try:
        waiting_for_link.clear()
        BUYER_ORDER_SELECTION.clear()
        LOT_INPUT_STATES.clear()
        LOT_CARD_CONTEXT.clear()
        UI_INPUT_STATES.clear()
        SEARCH_RESULT_CONTEXT.clear()
        ANALYTICS_VIEW_MODES.clear()
        waiting_for_lots_upload.clear()
        waiting_for_backup_restore_upload.clear()
    except Exception:
        logger.exception("Не удалось полностью очистить in-memory состояния AutoSMM")


def _validate_backup_wrapper(payload: Any) -> Tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "Файл не похож на полный бэкап AutoSMM."
    if payload.get("autosmm_backup_version") != AUTOSMM_BACKUP_VERSION:
        return False, "Неподдерживаемая версия бэкапа AutoSMM."
    files = payload.get("files")
    if not isinstance(files, dict):
        return False, "В бэкапе нет раздела files."
    unknown_files = sorted(set(files.keys()) - set(AUTOSMM_BACKUP_FILE_MAP.keys()))
    if unknown_files:
        return False, "Бэкап содержит неизвестные файлы. Восстановление отменено."
    return True, "OK"


def _restore_backup_wrapper(payload: Dict[str, Any]) -> Tuple[bool, str]:
    ok, error = _validate_backup_wrapper(payload)
    if not ok:
        return False, error
    emergency_backup = _create_full_backup(reason="before_restore")
    if not emergency_backup:
        return False, "Не удалось создать аварийный бэкап. Восстановление отменено."
    files = payload.get("files") or {}
    try:
        for file_name, file_path in AUTOSMM_BACKUP_FILE_MAP.items():
            value = files.get(file_name, _backup_default_for_file(file_name))
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            _safe_write_json(file_path, value)
        _clear_autosmm_memory_contexts()
        try:
            cfg = load_config()
            config.clear()
            config.update(cfg)
            lot_mapping.clear()
            lot_mapping.update(cfg.get("lot_mapping", {}))
        except Exception:
            pass
        return True, f"✅ Бэкап AutoSMM восстановлен.\n\nРекомендуется перезапустить Cardinal, чтобы все состояния точно перечитались."
    except Exception as ex:
        logger.error(f"Ошибка восстановления бэкапа AutoSMM: {ex}")
        return False, f"❌ Не удалось восстановить бэкап: {ex}"

# =========================================================
# SETTINGS UI
# =========================================================

def add_website(message: types.Message, new_site: str):
    normalized_site = _normalize_allowed_domain(new_site)
    if not normalized_site or not _is_plausible_allowed_domain(normalized_site):
        bot.send_message(message.chat.id, "❌ Не удалось определить корректный домен сайта.")
        return
    valid_links = load_valid_links()
    if normalized_site not in valid_links:
        valid_links.append(normalized_site)
        save_valid_links(valid_links)
        bot.send_message(message.chat.id, f"✅ Сайт {normalized_site} успешно добавлен в список.")
    else:
        bot.send_message(message.chat.id, f"❌ Сайт {normalized_site} уже есть в списке.")


def remove_website(message: types.Message, site_to_remove: str):
    normalized_site = _normalize_allowed_domain(site_to_remove)
    valid_links = load_valid_links()
    if normalized_site in valid_links:
        valid_links.remove(normalized_site)
        save_valid_links(valid_links)
        bot.send_message(message.chat.id, f"✅ Сайт {normalized_site} успешно удалён из списка.")
    else:
        bot.send_message(message.chat.id, f"❌ Сайт {normalized_site or site_to_remove} не найден в списке.")


def _send_or_edit_text(chat_id: int, text_value: str, kb: InlineKeyboardMarkup, message_id: Optional[int] = None):
    text_value = clean_tg_text(text_value)
    if message_id is None:
        bot.send_message(chat_id, text_value, parse_mode="HTML", reply_markup=kb)
        return
    bot.edit_message_text(text_value, chat_id, message_id, parse_mode="HTML", reply_markup=kb)


def _open_main_settings_menu(chat_id: int, message_id: Optional[int] = None):
    cfg = load_config()
    txt_, kb = build_main_menu_text_and_kb(cfg)
    _send_or_edit_text(chat_id, txt_, kb, message_id=message_id)


def _should_show_old_files_warning(ignore_cooldown: bool = False) -> bool:
    old_files = find_old_autosmm_files()
    if not old_files:
        return False
    if ignore_cooldown:
        return True
    cfg = load_config()
    return not is_old_files_cleanup_cooldown_active(cfg)


def build_old_files_warning_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    txt_ = """
⚠️ <b>Обнаружены старые файлы AutoSMM</b>

В папке плагина найдены устаревшие файлы AutoSMM.
Они могут вызывать конфликты, дубли обработчиков и загрузку старой версии плагина.

Рекомендуется удалить их перед использованием текущей версии.

Если хотите вернуться к этому позже, очистка будет доступна в разделе:
📁 Файлы и бэкап
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🗑 Удалить старые файлы", callback_data="cleanup_old_files_prompt"),
        InlineKeyboardButton("🕒 Напомнить позже", callback_data="cleanup_old_files_later"),
    )
    return txt_, kb


def build_old_files_confirm_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    txt_ = """
⚠️ <b>Очистка старых файлов AutoSMM</b>

Будут удалены только старые файлы плагина AutoSMM из папки plugins.

Текущая сборка затронута не будет.
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("✅ Удалить старые файлы", callback_data="cleanup_old_files_confirm"),
        InlineKeyboardButton("◀️ Назад", callback_data="autosmm_cleanup_menu"),
    )
    return txt_, kb

def build_old_files_removed_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    txt_ = """
✅ <b>Старые файлы AutoSMM удалены</b>

Устаревшие файлы очищены.
Теперь используется только текущая версия плагина.
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="autosmm_cleanup_menu"))
    return txt_, kb


def build_old_files_not_found_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    txt_ = """
✅ <b>Старые файлы AutoSMM не найдены</b>

В папке текущего плагина не обнаружено устаревших файлов AutoSMM.
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    return txt_, kb


def open_old_files_warning_screen(chat_id: int, message_id: Optional[int] = None, ignore_cooldown: bool = False):
    if not _should_show_old_files_warning(ignore_cooldown=ignore_cooldown):
        if ignore_cooldown and not find_old_autosmm_files():
            txt_, kb = build_old_files_not_found_text_and_kb()
            _send_or_edit_text(chat_id, txt_, kb, message_id=message_id)
            return
        _open_main_settings_menu(chat_id, message_id=message_id)
        return

    txt_, kb = build_old_files_warning_text_and_kb()
    _send_or_edit_text(chat_id, txt_, kb, message_id=message_id)


def _format_currency_for_main_menu(currency: str) -> str:
    currency_upper = str(currency or "").upper()
    if currency_upper == "RUB":
        return "₽"
    return currency_upper or ""


def _get_total_panel_balance_text(cfg: Dict) -> str:
    global _PANEL_BALANCE_CACHE

    now_ts = time.time()
    if now_ts - float(_PANEL_BALANCE_CACHE.get("timestamp", 0.0) or 0.0) < PANEL_BALANCE_CACHE_TTL_SECONDS:
        return str(_PANEL_BALANCE_CACHE.get("text", "недоступно"))

    services_cfg = cfg.get("services", {}) or {}
    if not services_cfg:
        _PANEL_BALANCE_CACHE = {"timestamp": now_ts, "text": "недоступно"}
        return "недоступно"

    totals_by_currency: Dict[str, float] = {}
    successful_services = 0
    failed_services = 0

    for srv_num, srv_data in services_cfg.items():
        api_url = str((srv_data or {}).get("api_url", "") or "").strip()
        api_key = str((srv_data or {}).get("api_key", "") or "").strip()
        try:
            service_number = int(srv_num)
        except Exception:
            service_number = 0

        invalid_key_payload = _validate_api_key_for_action(service_number, "balance", api_key)
        if invalid_key_payload or not api_url:
            failed_services += 1
            continue

        try:
            rr = requests.get(f"{api_url}?action=balance&key={api_key}", timeout=6)
            rr.raise_for_status()
            balance_data = rr.json()
            balance_value = float(balance_data.get("balance", 0) or 0)
            currency_value = str(balance_data.get("currency", "") or "").upper().strip() or "UNKNOWN"
            totals_by_currency[currency_value] = totals_by_currency.get(currency_value, 0.0) + balance_value
            successful_services += 1
        except Exception as ex:
            failed_services += 1
            logger.warning(f"MAIN MENU BALANCE: не удалось получить баланс для сервиса #{srv_num}: {ex}")

    if not totals_by_currency:
        balance_text = "недоступно"
    elif len(totals_by_currency) > 1:
        balance_text = "mixed currencies"
    elif failed_services > 0:
        balance_text = "частично недоступно"
    else:
        currency_value, total_value = next(iter(totals_by_currency.items()))
        currency_label = _format_currency_for_main_menu(currency_value)
        balance_text = f"{float(total_value):.2f} {currency_label}".strip()

    _PANEL_BALANCE_CACHE = {"timestamp": now_ts, "text": balance_text}
    return balance_text


def build_main_menu_text_and_kb(cfg: Dict) -> Tuple[str, InlineKeyboardMarkup]:
    """Собирает главное меню AutoSMM."""
    lmap = cfg.get("lot_mapping", {}) or {}
    auto_refunds_icon = "✅" if cfg.get("auto_refunds", False) else "❌"
    confirm_link_icon = "✅" if cfg.get("confirm_link", True) else "❌"
    fallback_icon = "✅" if cfg.get("fallback_enabled", False) else "❌"

    stats = analytics.get_statistics()
    day_profit = stats.get("day_chistota", 0) if stats else 0
    try:
        day_profit_fmt = f"{float(day_profit):.2f}"
    except Exception:
        day_profit_fmt = str(day_profit)

    total_balance_text = _get_total_panel_balance_text(cfg)

    lines = [
        f"🚀 <b>AutoSMM v{h(VERSION)}</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📦 <b>Лоты:</b> <code>{h(len(lmap))}</code>",
        "",
        f"<b>Автовозвраты:</b> {auto_refunds_icon}",
        f"<b>Подтверждение ссылки:</b> {confirm_link_icon}",
        f"<b>Резервная панель:</b> {fallback_icon}",
        "",
        f"💰 <b>Профит за 24ч:</b> <code>{h(day_profit_fmt)} ₽</code>",
        f"💳 <b>Баланс панелей:</b> <code>{h(total_balance_text)}</code>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    txt_ = clean_tg_text("\n".join(lines))

    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🛍️ Каталог лотов", callback_data="lot_settings"),
        InlineKeyboardButton("➕ Создать новый лот", callback_data="add_new_lot"),
    )
    kb.add(InlineKeyboardButton("🔌 Интеграция API", callback_data="api_settings"))
    kb.add(InlineKeyboardButton("📊 Аналитика", callback_data="show_orders"))
    kb.row(
        InlineKeyboardButton("📁 Бэкап / файлы", callback_data="files_menu"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="misc_settings"),
    )
    kb.add(InlineKeyboardButton("📚 Полезные ресурсы", callback_data="links_menu"))
    return txt_, kb

def auto_smm_settings(message: types.Message):
    if not _telemetry_gate_allows_menu():
        _show_telemetry_gate(message.chat.id)
        return
    if _should_show_old_files_warning(ignore_cooldown=False):
        open_old_files_warning_screen(message.chat.id, ignore_cooldown=False)
        return
    _open_main_settings_menu(message.chat.id)
    _maybe_send_telemetry_ping(cardinal_instance, force=False)


def auto_smm_delete(message: types.Message):
    """Полная очистка данных AutoSMM после аварийного бэкапа."""
    global RUNNING
    try:
        RUNNING = False
    except Exception:
        pass

    backup_path = _create_full_backup(reason="before_delete")
    if not backup_path:
        bot.send_message(message.chat.id, "❌ Не удалось создать аварийный бэкап. Очистка отменена.")
        return

    try:
        with open(backup_path, "rb") as backup_file:
            bot.send_document(message.chat.id, backup_file, caption="Аварийный бэкап AutoSMM перед очисткой")
    except Exception as ex:
        logger.warning(f"Не удалось отправить аварийный бэкап пользователю: {ex}")

    deleted = []
    for p in [CONFIG_PATH, ORDERS_PATH, ORDERS_DATA_PATH, ORDERS_RUNTIME_PATH, VALID_LINKS_PATH, AUTOSMM_STATE_PATH]:
        try:
            if os.path.exists(p):
                os.remove(p)
                deleted.append(os.path.basename(p))
        except Exception as e:
            logger.error(f"Не удалось удалить файл {p}: {e}")

    _clear_autosmm_memory_contexts()
    filename = os.path.basename(backup_path)
    txt = f"✅ Данные AutoSMM очищены.\n\nПеред очисткой создан аварийный бэкап:\n{filename}\n\nРекомендуется перезапустить Cardinal."
    bot.send_message(message.chat.id, txt)


def files_menu(call: types.CallbackQuery):
    waiting_for_lots_upload.discard(call.from_user.id)
    waiting_for_backup_restore_upload.discard(call.from_user.id)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    txt_ = """
<b>📁 Бэкап / файлы AutoSMM</b>

Здесь можно сохранить данные, загрузить бэкап, выгрузить логи или очистить данные AutoSMM.
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("📤 Экспорт бэкапа", callback_data="full_backup_prompt"))
    kb_.add(InlineKeyboardButton("📥 Загрузка бэкапа", callback_data="restore_backup_prompt"))
    kb_.add(InlineKeyboardButton("📝 Логи", callback_data="export_errors"))
    kb_.add(InlineKeyboardButton("🧹 Очистка AutoSMM", callback_data="autosmm_cleanup_menu"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def misc_settings(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    cfg = load_config()
    fallback_enabled = bool(cfg.get("fallback_enabled", False))

    txt_ = """
⚙️ <b>Настройки AutoSMM</b>

Выберите раздел настроек:
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("👤 Покупательский сценарий", callback_data="buyer_flow_settings"))
    kb_.add(InlineKeyboardButton("💬 Тексты сообщений", callback_data="edit_messages"))
    kb_.add(InlineKeyboardButton("🔔 Уведомления продавцу", callback_data="seller_notifications_settings"))
    kb_.add(InlineKeyboardButton("🌐 Разрешённые сайты", callback_data="manage_websites"))
    kb_.add(InlineKeyboardButton(f"🔁 Резервная панель: {'✅' if fallback_enabled else '❌'}", callback_data="toggle_fallback"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def buyer_flow_settings(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    cfg = load_config()
    confirm_link = bool(cfg.get("confirm_link", True))
    auto_refunds = bool(cfg.get("auto_refunds", False))
    allow_cancel = bool(cfg.get("allow_buyer_cancel_before_panel_order", False))
    txt_ = f"""
👤 <b>Покупательский сценарий</b>

Подтверждение ссылки: {'✅' if confirm_link else '❌'}
Автовозвраты: {'✅' if auto_refunds else '❌'}
Отмена до запуска: {'✅' if allow_cancel else '❌'}

Здесь настраивается поведение покупателя после оплаты.
    """.strip()
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton(f"✅ Подтверждение ссылки: {'✅' if confirm_link else '❌'}", callback_data="toggle_confirm_link"))
    kb_.add(InlineKeyboardButton(f"↩️ Автовозвраты: {'✅' if auto_refunds else '❌'}", callback_data="toggle_auto_refunds"))
    kb_.add(InlineKeyboardButton(f"⚠️ Отмена до запуска: {'✅' if allow_cancel else '❌'}", callback_data="toggle_buyer_cancel_before_panel"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def seller_notifications_settings(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    cfg = load_config()
    notif_chat_id = cfg.get("notification_chat_id")
    txt_ = f"""
<b>🔔 Уведомления продавцу</b>

Чат уведомлений: {'задан' if notif_chat_id else 'не задан'}

AutoSMM может отправлять продавцу уведомления о новых заказах, ошибках панели и ручной проверке.
    """.strip()
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("📌 Использовать этот чат", callback_data="use_current_chat_for_notifications"))
    kb_.add(InlineKeyboardButton("📩 Указать другой чат", callback_data="set_notification_chat_id"))
    kb_.add(InlineKeyboardButton("🧪 Отправить тестовое уведомление", callback_data="send_test_notification"))
    kb_.add(InlineKeyboardButton("🔕 Отключить уведомления", callback_data="disable_notifications"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def use_current_chat_for_notifications(call: types.CallbackQuery):
    cfg = load_config()
    cfg["notification_chat_id"] = call.message.chat.id
    save_config(cfg)
    try:
        bot.answer_callback_query(call.id, "Чат уведомлений установлен")
    except Exception:
        pass
    seller_notifications_settings(call)


def send_test_notification(call: types.CallbackQuery):
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id:
        bot.answer_callback_query(call.id, "Чат уведомлений не задан")
        seller_notifications_settings(call)
        return
    try:
        bot.send_message(notification_chat_id, "🧪 Тестовое уведомление AutoSMM")
        bot.answer_callback_query(call.id, "Отправлено")
    except Exception as ex:
        bot.answer_callback_query(call.id, "Не удалось отправить")
        logger.warning(f"Не удалось отправить тестовое уведомление AutoSMM: {ex}")
    seller_notifications_settings(call)


def disable_notifications(call: types.CallbackQuery):
    cfg = load_config()
    cfg["notification_chat_id"] = None
    save_config(cfg)
    bot.answer_callback_query(call.id, "Отключено")
    seller_notifications_settings(call)


def toggle_fallback(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)

    cfg = load_config()
    current = bool(cfg.get("fallback_enabled", False))
    cfg["fallback_enabled"] = not current
    save_config(cfg)

    try:
        config["fallback_enabled"] = cfg["fallback_enabled"]
    except Exception:
        pass

    misc_settings(call)

def toggle_buyer_cancel_before_panel(call: types.CallbackQuery):
    cfg = load_config()
    current = bool(cfg.get("allow_buyer_cancel_before_panel_order", False))
    if current:
        cfg["allow_buyer_cancel_before_panel_order"] = False
        save_config(cfg)
        try:
            config["allow_buyer_cancel_before_panel_order"] = False
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Отключено")
        buyer_flow_settings(call)
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("✅ Включить", callback_data="enable_buyer_cancel_before_panel"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="buyer_flow_settings"))
    txt_ = """⚠️ <b>Опасная настройка</b>

Если включить эту опцию, покупатель сможет отменить заказ командой !отмена, пока выполнение ещё не запущено.

После запуска выполнения автоматическая отмена будет недоступна.

Включайте только если готовы к автоматическим отменам."""
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def enable_buyer_cancel_before_panel(call: types.CallbackQuery):
    cfg = load_config()
    cfg["allow_buyer_cancel_before_panel_order"] = True
    save_config(cfg)
    try:
        config["allow_buyer_cancel_before_panel_order"] = True
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Включено")
    buyer_flow_settings(call)


def links_menu(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    txt_ = """
📚 <b>Полезные ресурсы</b>

Полезные ссылки по AutoSMM и SMM-панелям.
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=2)
    kb_.row(
        InlineKeyboardButton("🛠 Поддержка", url="https://t.me/w1bbex"),
        InlineKeyboardButton("📢 Канал плагинов", url="https://t.me/w1bbexplugins"),
    )
    kb_.row(
        InlineKeyboardButton("🌐 SmmWay", url="https://smmway.ru/ref3967025"),
        InlineKeyboardButton("🌐 KuzyaBoost", url="https://kuzya-boost.ru/ref4251277"),
    )
    kb_.add(InlineKeyboardButton("❓ FAQ", url="https://teletype.in/@w1bbex/FAQ-AutoSMM"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def _sort_service_items(services: Dict) -> List[Tuple[str, Dict]]:
    def _service_sort_key(item: Tuple[str, Dict]) -> Tuple[int, str]:
        key = str(item[0] or "")
        try:
            return (0, f"{int(key):09d}")
        except Exception:
            return (1, key)
    return sorted((services or {}).items(), key=_service_sort_key)


def normalize_api_url(raw_url: Any) -> str:
    from urllib.parse import urlsplit

    raw_value = str(raw_url or "").strip()
    if not raw_value:
        raise ValueError("ссылка или домен не должны быть пустыми")

    markdown_match = re.match(r"^\[[^\]]+\]\(([^)]+)\)$", raw_value)
    if markdown_match:
        raw_value = markdown_match.group(1).strip()

    raw_value = raw_value.replace("\\", "/").strip()
    if raw_value.startswith("//"):
        raw_value = f"https:{raw_value}"

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw_value):
        raw_value = f"https://{raw_value.lstrip('/')}"

    parts = urlsplit(raw_value)
    host = str(parts.netloc or "").strip().lower()

    if not host and parts.path:
        fallback_parts = urlsplit(f"https://{str(parts.path).lstrip('/')}")
        host = str(fallback_parts.netloc or "").strip().lower()
        parts = fallback_parts

    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]

    if not host or "." not in host:
        raise ValueError("не удалось распознать домен")

    host_no_port = host.split(":", 1)[0]
    if not re.fullmatch(r"[a-z0-9.-]+", host_no_port) or host_no_port.startswith(".") or host_no_port.endswith("."):
        raise ValueError("некорректный домен")

    path_value = re.sub(r"/+", "/", str(parts.path or "").replace("\\", "/"))
    lower_path = path_value.lower()

    api_idx = lower_path.find("/api/v2")
    normalized_path = "/api/v2" if api_idx == -1 else path_value[:api_idx + len("/api/v2")]

    normalized_path = normalized_path.rstrip("/")
    if normalized_path != "/api/v2":
        normalized_path = "/api/v2"

    return f"https://{host}{normalized_path}"


def _extract_service_domain(api_url: Any) -> str:
    from urllib.parse import urlsplit

    raw_url = str(api_url or "").strip()
    if not raw_url:
        return ""

    try:
        normalized = normalize_api_url(raw_url)
    except Exception:
        normalized = raw_url if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw_url) else f"https://{raw_url.lstrip('/')}"

    try:
        host = str(urlsplit(normalized).netloc or "").strip().lower()
    except Exception:
        return ""

    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]
    return host


def _domain_to_service_name(domain: str) -> str:
    host = str(domain or "").strip().lower()
    if not host:
        return "Сервис"

    first_label = host.split(":", 1)[0].split(".", 1)[0]
    first_label = first_label.strip()
    if not first_label:
        return "Сервис"

    if first_label.startswith("smm") and len(first_label) > 3:
        return "SMM" + first_label[3:].capitalize()
    if first_label.endswith("smm") and len(first_label) > 3:
        return first_label[:-3].capitalize() + "SMM"
    return first_label[:1].upper() + first_label[1:]


def build_service_display_name(service_id, api_url, existing_name=None) -> str:
    service_num = str(service_id or "").strip()
    existing = re.sub(r"\s+", " ", str(existing_name or "").strip())

    if existing:
        if re.search(r"\s+#\d+\s*$", existing):
            return re.sub(r"\s+#\d+\s*$", f" #{service_num}", existing)
        return f"{existing} #{service_num}"

    generated_base = _domain_to_service_name(_extract_service_domain(api_url))
    return f"{generated_base} #{service_num}"


def _service_name_is_default(existing_name: Any, service_id: int, api_url: Any) -> bool:
    name_value = re.sub(r"\s+", " ", str(existing_name or "").strip())
    if not name_value:
        return True

    generated_name = build_service_display_name(service_id, api_url, None)
    if name_value.lower() == generated_name.lower():
        return True

    if re.fullmatch(r"(?i)(service|сервис)\s*#?\d+", name_value):
        return True

    return False


def mask_api_key(api_key: Any) -> str:
    key_value = _normalize_api_key(api_key)
    if not key_value:
        return "не задан"
    if len(key_value) <= 2:
        return f"{key_value[0]}......{key_value[-1]}"
    return f"{key_value[:3]}......{key_value[-3:]}"


def _is_api_key_length_suspicious(api_key: Any) -> bool:
    return len(_normalize_api_key(api_key)) != EXPECTED_API_KEY_LENGTH


def _get_service_balance_payload(service_number: int, service_cfg: Optional[Dict], timeout: int = 6) -> Dict[str, Any]:
    service_cfg = service_cfg or {}
    api_url_raw = str(service_cfg.get("api_url") or "").strip()
    api_key = _normalize_api_key(service_cfg.get("api_key"))

    if not api_url_raw or not api_key:
        return {
            "ok": False,
            "status_emoji": "❌",
            "balance_text": "недоступно",
            "balance_value": None,
            "currency": None,
        }

    try:
        api_url = normalize_api_url(api_url_raw)
        response = requests.get(api_url, params={"action": "balance", "key": api_key}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()

        balance_raw = payload.get("balance", 0)
        try:
            balance_value = float(str(balance_raw).replace(",", "."))
        except Exception:
            balance_value = float(balance_raw or 0)

        currency = str(payload.get("currency") or "RUB").upper().strip() or "RUB"
        balance_text = f"{balance_value:.2f} {_format_currency_for_main_menu(currency)}".strip()

        return {
            "ok": True,
            "status_emoji": "✅",
            "balance_text": balance_text,
            "balance_value": balance_value,
            "currency": currency,
        }
    except Exception as ex:
        logger.warning(f"{LOGGER_PREFIX} Не удалось получить баланс для сервиса #{service_number}: {ex}")
        return {
            "ok": False,
            "status_emoji": "❌",
            "balance_text": "недоступно",
            "balance_value": None,
            "currency": None,
        }


def get_service_api_status(service_number: int, service_cfg: Optional[Dict]) -> str:
    return _get_service_balance_payload(service_number, service_cfg).get("status_emoji", "❌")


def _build_total_api_balance_text(service_payloads: List[Dict[str, Any]]) -> str:
    successful = [item for item in (service_payloads or []) if item.get("ok")]
    if not successful:
        return "недоступно"

    currencies = {str(item.get("currency") or "").upper().strip() for item in successful if item.get("currency")}
    if len(currencies) != 1:
        return "недоступно"

    total_value = sum(float(item.get("balance_value") or 0.0) for item in successful)
    currency_value = next(iter(currencies))
    return f"{total_value:.2f} {_format_currency_for_main_menu(currency_value)}".strip()


def render_api_menu(cfg: Dict) -> Tuple[str, InlineKeyboardMarkup]:
    services = cfg.get("services", {}) or {}
    service_rows: List[Tuple[str, Dict, Dict[str, Any], str]] = []
    payloads: List[Dict[str, Any]] = []

    for srv_num, srv_data in _sort_service_items(services):
        try:
            srv_int = int(srv_num)
        except Exception:
            continue

        display_name = build_service_display_name(srv_int, (srv_data or {}).get("api_url"), (srv_data or {}).get("name"))
        payload = _get_service_balance_payload(srv_int, srv_data)
        payloads.append(payload)
        service_rows.append((srv_num, srv_data or {}, payload, display_name))

    total_balance_text = _build_total_api_balance_text(payloads)

    lines = [
        "🔌 Интеграция API",
        "",
        f"Общий баланс: {total_balance_text}",
        "",
        "Подключённые сервисы:",
        "",
    ]

    if service_rows:
        for idx, (_, _, payload, display_name) in enumerate(service_rows):
            lines.append(f"📡 {display_name}")
            lines.append(f"Состояние подключения: {payload.get('status_emoji', '❌')}")
            if idx != len(service_rows) - 1:
                lines.append("")
    else:
        lines.append("Сервисов пока нет.")

    kb = InlineKeyboardMarkup(row_width=1)
    for srv_num, _, _, display_name in service_rows:
        kb.add(InlineKeyboardButton(f"📡 {display_name}", callback_data=f"api_service_{srv_num}"))

    kb.row(
        InlineKeyboardButton("➕ Добавить сервис", callback_data="add_service"),
        InlineKeyboardButton("➖ Удалить сервис", callback_data="delete_service"),
    )
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

    return "\n".join(lines).strip(), kb


def _build_api_service_card_not_found() -> Tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
    return "❌ Сервис не найден.", kb


def render_api_service_card(cfg: Dict, service_idx: int) -> Tuple[str, InlineKeyboardMarkup]:
    services = cfg.get("services", {}) or {}
    service_cfg = services.get(str(service_idx))
    if not service_cfg:
        return _build_api_service_card_not_found()

    display_name = build_service_display_name(service_idx, service_cfg.get("api_url"), service_cfg.get("name"))
    balance_payload = _get_service_balance_payload(service_idx, service_cfg)

    lines = [
        f"📡 {display_name}",
        "",
        f"Ключ: {mask_api_key(service_cfg.get('api_key'))}",
        f"Состояние подключения: {balance_payload.get('status_emoji', '❌')}",
        f"Баланс: {balance_payload.get('balance_text', 'недоступно')}",
    ]

    if _is_api_key_length_suspicious(service_cfg.get("api_key")):
        lines.extend(
            [
                "",
                "⚠️ Ключ выглядит некорректно",
                "Обычно API key берётся в настройках API панели.",
            ]
        )

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("Изменить ссылку", callback_data=f"edit_apiurl_{service_idx}"))
    kb.add(InlineKeyboardButton("Изменить ключ", callback_data=f"edit_apikey_{service_idx}"))
    kb.add(InlineKeyboardButton("Изменить название сервиса", callback_data=f"edit_apiname_{service_idx}"))
    kb.add(InlineKeyboardButton("Изменить номер сервиса", callback_data=f"edit_apinum_{service_idx}"))
    kb.add(InlineKeyboardButton("Обновить баланс", callback_data=f"check_balance_{service_idx}"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))

    return "\n".join(lines).strip(), kb


def _open_api_menu(chat_id: int, message_id: Optional[int] = None):
    cfg = load_config()
    text_, kb = render_api_menu(cfg)
    if message_id is None:
        bot.send_message(chat_id, text_, reply_markup=kb)
        return

    try:
        bot.edit_message_text(text_, chat_id, message_id, reply_markup=kb)
    except Exception as ex:
        if "message is not modified" not in str(ex).lower():
            raise


def _open_api_service_card(chat_id: int, service_idx: int, message_id: Optional[int] = None):
    cfg = load_config()
    text_, kb = render_api_service_card(cfg, service_idx)
    if message_id is None:
        bot.send_message(chat_id, text_, reply_markup=kb)
        return

    try:
        bot.edit_message_text(text_, chat_id, message_id, reply_markup=kb)
    except Exception as ex:
        if "message is not modified" not in str(ex).lower():
            raise


def api_settings_menu(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_menu(call.message.chat.id, message_id=call.message.message_id)


def process_apiurl_change(message: types.Message, service_idx: int):
    cfg = load_config()
    service_cfg = (cfg.get("services") or {}).get(str(service_idx))
    if not service_cfg:
        _clear_ui_input_state(message.chat.id)
        bot.send_message(message.chat.id, "❌ Сервис не найден.")
        return

    raw_url = str(message.text or "").strip()
    try:
        normalized_url = normalize_api_url(raw_url)
    except ValueError as ex:
        _repeat_ui_input_flow(message.chat.id, f"❌ Ошибка: {ex}.")
        return
    except Exception:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: не удалось обработать ссылку.")
        return

    old_url = str(service_cfg.get("api_url") or "").strip()
    old_name = service_cfg.get("name")

    service_cfg["api_url"] = normalized_url
    if _service_name_is_default(old_name, service_idx, old_url):
        service_cfg["name"] = build_service_display_name(service_idx, normalized_url, None)

    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, service_idx)


def process_apikey_change(message: types.Message, service_idx: int):
    new_key = _normalize_api_key(message.text)
    if not new_key:
        _repeat_ui_input_flow(message.chat.id, "❌ API ключ не может быть пустым.")
        return

    cfg = load_config()
    service_cfg = (cfg.get("services") or {}).get(str(service_idx))
    if not service_cfg:
        _clear_ui_input_state(message.chat.id)
        bot.send_message(message.chat.id, "❌ Сервис не найден.")
        return

    service_cfg["api_key"] = new_key
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, service_idx)


def process_apiname_change(message: types.Message, service_idx: int):
    new_name = re.sub(r"\s+", " ", str(message.text or "").strip())
    if not new_name:
        _repeat_ui_input_flow(message.chat.id, "❌ Название сервиса не может быть пустым.")
        return

    cfg = load_config()
    service_cfg = (cfg.get("services") or {}).get(str(service_idx))
    if not service_cfg:
        _clear_ui_input_state(message.chat.id)
        bot.send_message(message.chat.id, "❌ Сервис не найден.")
        return

    service_cfg["name"] = new_name
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, service_idx)


def process_apinum_change(message: types.Message, service_idx: int):
    try:
        new_service_idx = int(str(message.text or "").strip())
        if new_service_idx < 1:
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: номер сервиса должен быть положительным числом.")
        return

    cfg = load_config()
    services = cfg.get("services", {}) or {}
    old_key = str(service_idx)
    new_key = str(new_service_idx)

    if old_key not in services:
        _clear_ui_input_state(message.chat.id)
        bot.send_message(message.chat.id, "❌ Сервис не найден.")
        return

    if new_key != old_key and new_key in services:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{new_service_idx} уже существует.")
        return

    service_cfg = dict(services.pop(old_key))
    service_cfg["name"] = build_service_display_name(new_service_idx, service_cfg.get("api_url"), service_cfg.get("name"))
    services[new_key] = service_cfg
    cfg["services"] = {k: v for k, v in _sort_service_items(services)}

    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, new_service_idx)


def check_balance_func(call: types.CallbackQuery, service_idx: int):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_service_card(call.message.chat.id, service_idx, message_id=call.message.message_id)


def change_fallback_snum_prompt(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    lot_key = call.data.split("change_fallback_snum_", 1)[1]
    _start_lot_edit_flow(
        call,
        lot_key,
        "change_fallback_snum",
        f"Введите номер резервной панели для {lot_key}:",
        process_fallback_snum_change,
    )


def change_fallback_id_prompt(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    lot_key = call.data.split("change_fallback_id_", 1)[1]
    _start_lot_edit_flow(
        call,
        lot_key,
        "change_fallback_id",
        f"Введите ID резервной услуги для {lot_key}:",
        process_fallback_id_change,
    )


def manage_websites(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    txt_ = """
🌐 <b>Разрешённые сайты</b>

Здесь настраиваются сайты, ссылки с которых покупатели могут отправлять после оплаты.
    """.strip()
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("📋 Показать список", callback_data="show_allowed_websites_list"))
    kb_.add(InlineKeyboardButton("➕ Добавить сайт", callback_data="add_website"))
    kb_.add(InlineKeyboardButton("🗑 Удалить сайт", callback_data="show_allowed_websites_list"))
    kb_.add(InlineKeyboardButton("🔄 Сбросить список", callback_data="reset_valid_links_prompt"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def reset_valid_links_prompt(call: types.CallbackQuery):
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("✅ Сбросить", callback_data="reset_valid_links_confirm"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="manage_websites"))
    txt_ = """Список разрешённых сайтов будет заменён на стандартный набор AutoSMM.
Ваши добавленные вручную домены будут удалены.

Продолжить?"""
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, reply_markup=kb_)


def reset_valid_links_confirm(call: types.CallbackQuery):
    reset_valid_links_to_defaults()
    try:
        bot.answer_callback_query(call.id, "Список сброшен")
    except Exception:
        pass
    manage_websites(call)


def show_allowed_websites_list(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    valid_links = load_valid_links()
    kb_ = InlineKeyboardMarkup(row_width=2)
    if valid_links:
        buttons = [InlineKeyboardButton(f"🗑 {site}", callback_data=f"delete_website_{site}") for site in valid_links]
        for idx in range(0, len(buttons), 2):
            kb_.row(*buttons[idx:idx + 2])
        text_ = "🌐 <b>Разрешённые сайты</b>\n\nНажмите на сайт, чтобы удалить его из списка."
    else:
        text_ = "🌐 <b>Разрешённые сайты</b>\n\nСписок разрешённых сайтов пуст."

    kb_.add(InlineKeyboardButton("➕ Добавить сайт", callback_data="add_website"))
    kb_.add(InlineKeyboardButton("🔄 Сбросить список", callback_data="reset_valid_links_prompt"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="manage_websites"))
    bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def add_website_prompt(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Введите ссылку для добавления (например, example.com):",
        process_add_website,
        back_target="manage_websites",
    )


def process_add_website(message: types.Message):
    new_site = message.text.strip()
    if not new_site:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: ссылка не может быть пустой.")
        return
    add_website(message, new_site)
    _clear_ui_input_state(message.chat.id)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="manage_websites"))
    bot.send_message(message.chat.id, "Возврат к списку сайтов.", reply_markup=kb_)


def remove_website_prompt(call: types.CallbackQuery):
    site_to_remove = _normalize_allowed_domain(call.data.split("_", 2)[2])
    if not site_to_remove:
        show_allowed_websites_list(call)
        return
    if _is_delete_confirm_grace_active(call.message.chat.id):
        _delete_allowed_website_and_refresh(call, site_to_remove)
        return
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("✅ Удалить", callback_data=f"confirm_delete_website_{site_to_remove}"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="show_allowed_websites_list"))
    txt_ = f"""⚠️ <b>Удалить сайт из разрешённых?</b>

Сайт:
{html.escape(site_to_remove)}

Покупатели больше не смогут отправлять ссылки с этого домена.

После подтверждения следующие удаления в течение 30 секунд будут выполняться без повторного подтверждения."""
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def _delete_allowed_website_and_refresh(call: types.CallbackQuery, domain: str):
    domain = _normalize_allowed_domain(domain)
    valid_links = load_valid_links()
    if domain in valid_links:
        valid_links.remove(domain)
        save_valid_links(valid_links)
        try:
            bot.answer_callback_query(call.id, "Удалено")
        except Exception:
            pass
    else:
        try:
            bot.answer_callback_query(call.id, "Сайт уже отсутствует")
        except Exception:
            pass
    show_allowed_websites_list(call)


def confirm_delete_website_callback(call: types.CallbackQuery):
    domain = call.data.split("confirm_delete_website_", 1)[1]
    _set_delete_confirm_grace(call.message.chat.id, 30)
    _delete_allowed_website_and_refresh(call, domain)


def delete_all_lots_prompt(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("✅ Да, удалить", callback_data="confirm_delete_all_lots"),
        InlineKeyboardButton("◀️ Назад", callback_data="files_menu")
    )
    bot.edit_message_text(
        "⚠️ Вы уверены, что хотите удалить все лоты?\n\nЭто действие необратимо.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb,
    )


def confirm_delete_all_lots(call: types.CallbackQuery):
    cfg = load_config()
    cfg["lot_mapping"] = {}
    save_config(cfg)
    _set_lot_page_context(call.message.chat.id, 0)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    bot.edit_message_text("✅ Все лоты успешно удалены. Настройки сохранены.", call.message.chat.id, call.message.message_id, reply_markup=kb)


def upload_lots_json(call: types.CallbackQuery):
    user_id = call.from_user.id
    waiting_for_lots_upload.add(user_id)
    logger.info(f"Добавлен пользователь {user_id} в waiting_for_lots_upload: {waiting_for_lots_upload}")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    bot.edit_message_text("Пришлите файл JSON (можно любым названием).", call.message.chat.id, call.message.message_id, reply_markup=kb)


def full_backup_prompt(call: types.CallbackQuery):
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("📤 Создать бэкап", callback_data="full_backup_confirm"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    txt_ = """
⚠️ <b>Экспорт бэкапа AutoSMM</b>

Бэкап содержит настройки AutoSMM, лоты, заказы, разрешённые сайты и техническое состояние плагина.

Также внутри могут быть API-ключи панелей.

Не отправляйте этот файл посторонним.
    """.strip()
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def full_backup_confirm(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    backup_path = _create_full_backup(reason="manual_export")
    if not backup_path:
        bot.send_message(call.message.chat.id, "❌ Не удалось создать бэкап AutoSMM.")
        files_menu(call)
        return
    try:
        with open(backup_path, "rb") as backup_file:
            bot.send_document(call.message.chat.id, backup_file, caption="📤 Бэкап AutoSMM")
    except Exception as ex:
        bot.send_message(call.message.chat.id, f"❌ Не удалось отправить бэкап: {ex}")
    files_menu(call)


# Backward-compatible callback name. It now opens full backup warning instead of partial export.
def export_files(call: types.CallbackQuery):
    full_backup_prompt(call)


def restore_backup_prompt(call: types.CallbackQuery):
    waiting_for_backup_restore_upload.add(call.from_user.id)
    waiting_for_lots_upload.discard(call.from_user.id)
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    txt_ = """
📥 <b>Загрузка бэкапа AutoSMM</b>

Отправьте JSON-файл бэкапа AutoSMM.

Перед загрузкой AutoSMM создаст аварийный бэкап текущих данных.
    """.strip()
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def autosmm_cleanup_menu(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    txt_ = """
🧹 <b>Очистка AutoSMM</b>

Выберите, что нужно очистить.

Перед очисткой данных AutoSMM будет создан аварийный бэкап.
    """.strip()
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("🧹 Очистить данные AutoSMM", callback_data="full_autosmm_delete_prompt"))
    kb_.add(InlineKeyboardButton("🗑 Очистить старые файлы", callback_data="cleanup_old_files_manual"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def full_autosmm_delete_prompt(call: types.CallbackQuery):
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(InlineKeyboardButton("✅ Да, очистить", callback_data="full_autosmm_delete_confirm"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="autosmm_cleanup_menu"))
    txt_ = """
⚠️ <b>Очистить данные AutoSMM?</b>

Будут удалены:
• настройки лотов;
• история заказов;
• состояние заказов;
• разрешённые сайты;
• состояние плагина.

Перед удалением AutoSMM создаст аварийный бэкап.

Старые файлы старых версий не будут удалены.
    """.strip()
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def full_autosmm_delete_confirm(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    class _Msg:
        chat = call.message.chat
    auto_smm_delete(_Msg())


def export_errors(call: types.CallbackQuery):
    chat_id_ = call.message.chat.id
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, 'rb') as f:
                bot.send_document(chat_id_, f, caption="📝 Логи AutoSMM")
            try:
                bot.answer_callback_query(call.id, "Логи выгружены.")
            except Exception:
                pass
        except Exception as e:
            bot.send_message(chat_id_, f"❌ Ошибка отправки лог-файла: {e}")
    else:
        bot.send_message(chat_id_, "Логи ошибок пока не найдены.")
    files_menu(call)


def confirm_delete_orders(call: types.CallbackQuery):
    """Просит подтверждение перед удалением истории заказов."""
    kb2 = InlineKeyboardMarkup(row_width=2)
    kb2.row(
        InlineKeyboardButton("✅ Да, удалить", callback_data="delete_orders_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="files_menu")
    )
    bot.edit_message_text(
        "⚠️ <b>Вы уверены, что хотите удалить всю историю заказов?</b>\n\n"
        "Это действие необратимо.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=kb2
    )


def delete_orders_yes(call: types.CallbackQuery):
    """Удаляет файлы истории заказов после подтверждения."""
    try:
        delete_orders_file()
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id)
        try:
            send_debug(f"delete_orders_yes error: {e}")
        except Exception:
            pass

    # Возвращаемся в меню файлов
    try:
        # переиспользуем существующий обработчик меню файлов
        files_menu_callback(call)
    except Exception:
        # фолбэк: просто скажем, что ок
        try:
            bot.edit_message_text(
                "✅ Готово.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            pass


def toggle_auto_refunds(call: types.CallbackQuery):
    cfg = load_config()
    current = bool(cfg.get("auto_refunds", False))
    if current:
        cfg["auto_refunds"] = False
        save_config(cfg)
        try:
            bot.answer_callback_query(call.id, "Автовозвраты выключены")
        except Exception:
            pass
        buyer_flow_settings(call)
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("✅ Включить автовозвраты", callback_data="enable_auto_refunds"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="buyer_flow_settings"))
    txt = """
⚠️ <b>Автовозвраты</b>

Если включить эту настройку, AutoSMM сможет автоматически вернуть деньги покупателю, если заказ не удалось безопасно запустить.

Не включайте автовозвраты, пока не проверили свои панели и лоты.
    """.strip()
    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)

def enable_auto_refunds(call: types.CallbackQuery):
    cfg = load_config()
    cfg["auto_refunds"] = True
    save_config(cfg)
    try:
        bot.answer_callback_query(call.id, "Автовозвраты включены")
    except Exception:
        pass
    buyer_flow_settings(call)

def toggle_confirm_link(call: types.CallbackQuery):
    cfg = load_config()
    confirm_link = cfg.get("confirm_link", True)
    cfg["confirm_link"] = not confirm_link
    save_config(cfg)
    bot.answer_callback_query(call.id)
    buyer_flow_settings(call)


def toggle_send_auto_lots(call: types.CallbackQuery):
    cfg = load_config()
    current_value = cfg.get("send_auto_lots", True)
    cfg["send_auto_lots"] = not current_value
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)



def change_send_interval(call: types.CallbackQuery):
    cfg = load_config()
    current_interval = cfg.get("send_auto_lots_interval", 30)
    _start_ui_input_flow(
        call,
        f"Текущий интервал отправки: {current_interval} минут\n\nВведите новый интервал отправки файл настроек в минутах (от 5 до 1440):",
        process_send_interval_change,
        back_target="misc_settings",
    )


def cancel_interval_change(call: types.CallbackQuery):
    ui_input_back_callback(call)


def process_send_interval_change(message: types.Message):
    try:
        new_interval = int(message.text.strip())
        if new_interval < 5:
            _repeat_ui_input_flow(message.chat.id, "❌ Интервал не может быть меньше 5 минут.")
            return
        if new_interval > 1440:
            _repeat_ui_input_flow(message.chat.id, "❌ Интервал не может быть больше 1440 минут (24 часа).")
            return

        cfg = load_config()
        cfg["send_auto_lots_interval"] = new_interval
        save_config(cfg)
        _clear_ui_input_state(message.chat.id)

        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
        bot.send_message(message.chat.id, f"✅ Интервал отправки файл настроек установлен: {new_interval} минут", reply_markup=kb_)
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Введите корректное число минут.")


def return_to_settings(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    waiting_for_lots_upload.discard(call.from_user.id)
    waiting_for_backup_restore_upload.discard(call.from_user.id)
    SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    _open_main_settings_menu(call.message.chat.id, message_id=call.message.message_id)


def api_settings_callback(call: types.CallbackQuery):
    api_settings_menu(call)


def api_service_callback(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_service_card(call.message.chat.id, idx_, message_id=call.message.message_id)


def edit_apiurl(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    cfg = load_config()
    service_cfg = (cfg.get("services") or {}).get(str(idx_)) or {}
    current_url = str(service_cfg.get("api_url") or "").strip() or "не задан"
    _start_ui_input_flow(
        call,
        f"Введите ссылку или домен для сервиса #{idx_}:\n\nТекущий URL: {current_url}",
        process_apiurl_change,
        idx_,
        back_target=f"api_service_{idx_}",
    )


def edit_apikey(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    _start_ui_input_flow(
        call,
        f"Введите новый ключ для сервиса #{idx_}:",
        process_apikey_change,
        idx_,
        back_target=f"api_service_{idx_}",
    )


def edit_apiname(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    cfg = load_config()
    service_cfg = (cfg.get("services") or {}).get(str(idx_)) or {}
    current_name = build_service_display_name(idx_, service_cfg.get("api_url"), service_cfg.get("name"))
    _start_ui_input_flow(
        call,
        f"Введите новое название сервиса #{idx_}:\n\nТекущее название: {current_name}",
        process_apiname_change,
        idx_,
        back_target=f"api_service_{idx_}",
    )


def edit_apinum(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    _start_ui_input_flow(
        call,
        f"Введите новый номер для сервиса #{idx_}:",
        process_apinum_change,
        idx_,
        back_target=f"api_service_{idx_}",
    )


def check_balance(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    check_balance_func(call, idx_)


TEMPLATE_EDITOR_DEFS = {
    "after_payment": {
        "title": "💸 Текст после оплаты",
        "button": "💸 После оплаты",
        "default": DEFAULT_AFTER_PAYMENT_TEMPLATE,
        "variables": [
            "• {buyer_username} — покупатель",
            "• {lot_title} — название лота",
            "• {order_id_funpay} — ID заказа FunPay",
            "• {order_price} — цена",
            "• {order_amount} — количество FunPay",
            "• {total_quantity} — итоговое количество услуги",
            "• {done_quantity} — выполнено",
            "• {left_quantity} — осталось",
            "• {progress_percent} — прогресс в процентах",
            "• {currency} — валюта",
        ],
    },
    "buyer_link_received_confirm": {
        "title": "✅ Подтверждение ссылки",
        "button": "✅ Подтверждение ссылки",
        "default": DEFAULT_LINK_CONFIRM_TEMPLATE,
        "variables": [
            "• {order_id_funpay} — ID заказа FunPay",
            "• {lot_title} — название лота",
            "• {link} — ссылка покупателя",
            "• {buyer_username} — покупатель",
            "• {order_amount} — количество FunPay",
            "• {order_price} — цена",
            "• {total_quantity} — итоговое количество услуги",
            "• {done_quantity} — выполнено",
            "• {left_quantity} — осталось",
            "• {progress_percent} — прогресс в процентах",
            "• {currency} — валюта",
        ],
    },
    "after_confirmation": {
        "title": "🚀 Текст после запуска заказа",
        "button": "🚀 После запуска заказа",
        "default": DEFAULT_AFTER_CONFIRMATION_TEMPLATE,
        "variables": [
            "• {lot_title} — название лота",
            "• {panel_order_id} — ID заказа в панели",
            "• {twiboost_id} — ID заказа в панели",
            "• {link} — ссылка покупателя",
            "• {order_id_funpay} — ID заказа FunPay",
            "• {total_quantity} — итоговое количество услуги",
            "• {done_quantity} — выполнено",
            "• {left_quantity} — осталось",
            "• {progress_percent} — прогресс в процентах",
        ],
    },
    "after_completed": {
        "title": "🎉 Текст после завершения",
        "button": "🎉 После завершения",
        "default": DEFAULT_AFTER_COMPLETED_TEMPLATE,
        "variables": [
            "• {lot_title} — название лота",
            "• {panel_order_id} — ID заказа в панели",
            "• {twiboost_id} — ID заказа в панели",
            "• {order_id_funpay} — ID заказа FunPay",
            "• {total_quantity} — итоговое количество услуги",
            "• {done_quantity} — выполнено",
            "• {left_quantity} — осталось",
            "• {progress_percent} — прогресс в процентах",
        ],
    },
}


def edit_messages_menu(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    text_ = """
💬 <b>Тексты сообщений</b>

Здесь настраиваются сообщения, которые видит покупатель.

Полный список переменных:
https://teletype.in/@w1bbex/AutoSMM
    """.strip()
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💸 После оплаты", callback_data="edit_msg_payment"))
    kb.add(InlineKeyboardButton("✅ Подтверждение ссылки", callback_data="edit_msg_link_confirm"))
    kb.add(InlineKeyboardButton("🚀 После запуска заказа", callback_data="edit_msg_confirmation"))
    kb.add(InlineKeyboardButton("🎉 После завершения", callback_data="edit_msg_completion"))
    kb.add(InlineKeyboardButton("🔄 Сбросить тексты", callback_data="reset_message_texts_prompt"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
    bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)


def _get_template_editor_def(template_key: str) -> Optional[Dict[str, Any]]:
    return TEMPLATE_EDITOR_DEFS.get(str(template_key or ""))


def _get_current_template_text(template_key: str) -> str:
    cfg = load_config()
    messages = cfg.get("messages") or {}
    meta = _get_template_editor_def(template_key) or {}
    return str(messages.get(template_key) or meta.get("default") or "")


def _render_template_view_text(template_key: str) -> str:
    meta = _get_template_editor_def(template_key) or {}
    title = meta.get("title", "Текст сообщения")
    template_text = _get_current_template_text(template_key)
    template_for_view = template_text
    warning = ""
    if len(template_for_view) > 3500:
        template_for_view = template_for_view[:3500].rstrip()
        warning = "\n\n⚠️ Текст очень длинный, показаны первые 3500 символов."
    variables = "\n".join(meta.get("variables") or [])
    return f"""{html.escape(title)}

Текущий текст:

<pre>{html.escape(template_for_view)}</pre>{warning}

Доступные переменные:
{html.escape(variables)}

Полный список переменных:
https://teletype.in/@w1bbex/AutoSMM""".strip()


def _show_template_editor_screen(chat_id: int, message_id: Optional[int], template_key: str):
    if template_key not in TEMPLATE_EDITOR_DEFS:
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("✏️ Изменить текст", callback_data=f"template_edit_{template_key}"))
    kb.add(InlineKeyboardButton("🔄 Сбросить этот текст", callback_data=f"template_reset_prompt_{template_key}"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="edit_messages"))
    text_ = _render_template_view_text(template_key)
    if message_id is None:
        bot.send_message(chat_id, text_, parse_mode="HTML", reply_markup=kb)
    else:
        bot.edit_message_text(text_, chat_id, message_id, parse_mode="HTML", reply_markup=kb)


def template_view_callback(call: types.CallbackQuery):
    template_key = call.data.split("template_view_", 1)[1]
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, template_key)


def template_edit_callback(call: types.CallbackQuery):
    template_key = call.data.split("template_edit_", 1)[1]
    meta = _get_template_editor_def(template_key)
    if not meta:
        edit_messages_menu(call)
        return
    _start_ui_input_flow(
        call,
        f"Отправьте новый текст для:\n{meta.get('title')}\n\nТекущий текст можно скопировать с прошлого экрана.",
        process_template_text_change,
        template_key,
        back_target="edit_messages",
    )


def template_reset_prompt_callback(call: types.CallbackQuery):
    template_key = call.data.split("template_reset_prompt_", 1)[1]
    meta = _get_template_editor_def(template_key)
    if not meta:
        edit_messages_menu(call)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("✅ Сбросить этот текст", callback_data=f"template_reset_confirm_{template_key}"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data=f"template_view_{template_key}"))
    bot.edit_message_text(f"Сбросить стандартный текст?\n\n{meta.get('title')}", call.message.chat.id, call.message.message_id, reply_markup=kb)


def template_reset_confirm_callback(call: types.CallbackQuery):
    template_key = call.data.split("template_reset_confirm_", 1)[1]
    meta = _get_template_editor_def(template_key)
    if not meta:
        edit_messages_menu(call)
        return
    cfg = load_config()
    messages = cfg.setdefault("messages", {})
    messages[template_key] = meta.get("default") or ""
    save_config(cfg)
    try:
        bot.answer_callback_query(call.id, "Текст сброшен")
    except Exception:
        pass
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, template_key)


def process_template_text_change(message: types.Message, template_key: str):
    new_text = str(message.text or "").strip()
    if not new_text:
        _repeat_ui_input_flow(message.chat.id, "❌ Текст не может быть пустым.")
        return
    if template_key not in TEMPLATE_EDITOR_DEFS:
        _clear_ui_input_state(message.chat.id)
        bot.send_message(message.chat.id, "❌ Неизвестный текст сообщения.")
        return
    cfg = load_config()
    cfg.setdefault("messages", {})[template_key] = new_text
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    bot.send_message(message.chat.id, "✅ Текст обновлён.")
    _show_template_editor_screen(message.chat.id, None, template_key)


def edit_msg_payment(call: types.CallbackQuery):
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "after_payment")


def edit_msg_confirmation(call: types.CallbackQuery):
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "after_confirmation")


def edit_msg_link_confirm(call: types.CallbackQuery):
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "buyer_link_received_confirm")


def edit_msg_completion(call: types.CallbackQuery):
    _show_template_editor_screen(call.message.chat.id, call.message.message_id, "after_completed")


def reset_message_texts_prompt(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("✅ Сбросить тексты", callback_data="reset_message_texts_confirm"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="edit_messages"))
    bot.edit_message_text("Сбросить стандартные тексты AutoSMM?", call.message.chat.id, call.message.message_id, reply_markup=kb)


def reset_message_texts_confirm(call: types.CallbackQuery):
    cfg = load_config()
    cfg.setdefault("messages", {})
    for key, meta in TEMPLATE_EDITOR_DEFS.items():
        cfg["messages"][key] = meta.get("default") or ""
    _ensure_config_defaults(cfg)
    save_config(cfg)
    try:
        bot.answer_callback_query(call.id, "Тексты сброшены")
    except Exception:
        pass
    edit_messages_menu(call)


def _build_template_preview(text_value: str, limit: int = 450) -> str:
    raw = str(text_value or "").strip()
    if len(raw) > limit:
        raw = raw[:limit].rstrip() + "…"
    return html.escape(raw) if raw else "—"


def add_service(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Введите номер нового сервиса (число):",
        process_add_service,
        back_target="api_settings",
    )


def delete_service(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Введите номер панели для удаления:",
        process_delete_service,
        back_target="api_settings",
    )


def process_add_service(message: types.Message):
    try:
        srv_num = int(str(message.text or "").strip())
        if srv_num < 1:
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Номер сервиса должен быть положительным числом.")
        return

    cfg = load_config()
    services = cfg.get("services", {}) or {}
    service_key = str(srv_num)
    if service_key in services:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{srv_num} уже существует.")
        return

    services[service_key] = {
        "name": build_service_display_name(srv_num, "https://example.com/api/v2", None),
        "api_url": "https://example.com/api/v2",
        "api_key": "YOUR_API_KEY",
    }
    cfg["services"] = {k: v for k, v in _sort_service_items(services)}
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, srv_num)


def process_delete_service(message: types.Message):
    try:
        srv_num = int(str(message.text or "").strip())
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Номер сервиса должен быть числом.")
        return

    cfg = load_config()
    services = cfg.get("services", {}) or {}
    service_key = str(srv_num)
    if service_key not in services:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{srv_num} не существует.")
        return

    del services[service_key]
    cfg["services"] = {k: v for k, v in _sort_service_items(services)}
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    _open_api_menu(message.chat.id)


def set_notification_chat_id(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Отправьте ID Telegram-чата для уведомлений.\n\nМожно нажать «Использовать этот чат» в предыдущем меню.",
        process_notification_chat_id,
        back_target="seller_notifications_settings",
    )


def process_notification_chat_id(message: types.Message):
    raw_text = str(message.text or "").strip()
    if raw_text.lower() == "/chatid":
        bot.send_message(
            message.chat.id,
            f"ID этого чата: {message.chat.id}\n\nОтправьте ID чата числом или нажмите «Использовать этот чат» в меню уведомлений.",
            reply_markup=_build_ui_input_keyboard(message.chat.id),
        )
        state = _get_ui_input_state(message.chat.id)
        if state:
            try:
                bot.register_next_step_handler(message, state["next_handler"], *state.get("handler_args", ()))
            except Exception:
                pass
        return
    try:
        new_chat_id = int(raw_text)
        cfg = load_config()
        cfg["notification_chat_id"] = new_chat_id
        save_config(cfg)
        _clear_ui_input_state(message.chat.id)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="seller_notifications_settings"))
        bot.send_message(message.chat.id, f"✅ Чат уведомлений установлен: {new_chat_id}", reply_markup=kb_)
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: отправьте ID чата числом.")


def cleanup_old_files_manual(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    waiting_for_lots_upload.discard(call.from_user.id)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    txt_, kb = build_old_files_confirm_text_and_kb()
    _send_or_edit_text(call.message.chat.id, txt_, kb, message_id=call.message.message_id)

def cleanup_old_files_later(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    set_old_files_cleanup_cooldown(days=1)
    _open_main_settings_menu(call.message.chat.id, message_id=call.message.message_id)


def cleanup_old_files_prompt(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    txt_, kb = build_old_files_confirm_text_and_kb()
    _send_or_edit_text(call.message.chat.id, txt_, kb, message_id=call.message.message_id)


def cleanup_old_files_cancel(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    autosmm_cleanup_menu(call)

def cleanup_old_files_confirm(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    old_files = find_old_autosmm_files()
    if not old_files:
        txt_, kb = build_old_files_not_found_text_and_kb()
        _send_or_edit_text(call.message.chat.id, txt_, kb, message_id=call.message.message_id)
        return

    failed_files: List[str] = []
    for old_file in old_files:
        try:
            old_file.unlink()
        except Exception:
            failed_files.append(old_file.name)
            logger.exception("Не удалось удалить старый файл AutoSMM: %s", old_file)

    if failed_files:
        txt_ = "❌ <b>Не удалось удалить часть старых файлов AutoSMM</b>\n\n" + "\n".join(f"• {html.escape(name)}" for name in failed_files)
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="cleanup_old_files_cancel"))
        _send_or_edit_text(call.message.chat.id, txt_, kb, message_id=call.message.message_id)
        return

    clear_old_files_cleanup_cooldown()
    txt_, kb = build_old_files_removed_text_and_kb()
    _send_or_edit_text(call.message.chat.id, txt_, kb, message_id=call.message.message_id)


def files_menu_callback(call: types.CallbackQuery):
    files_menu(call)


def misc_settings_callback(call: types.CallbackQuery):
    misc_settings(call)


def links_menu_callback(call: types.CallbackQuery):
    links_menu(call)


def _find_order_by_panel_order_id(panel_order_id: Any) -> Optional[Dict]:
    target = str(panel_order_id or "").strip()
    if not target:
        return None
    matches = []
    for order_record in orders.load_orders_data():
        if str(order_record.get("panel_order_id") or order_record.get("twiboost_id") or order_record.get("id_zakaz") or "").strip() == target:
            matches.append(order_record)
    if len(matches) > 1:
        logger.warning("ORDER REF AMBIGUOUS: panel_order_id=%s matched %s local records", target, len(matches))
        return None
    return matches[0] if matches else None


def _normalize_order_ref_for_lookup(raw_ref: Any) -> str:
    return str(raw_ref or "").strip().lstrip("#").upper()


def _find_order_by_any_order_ref(raw_ref: Any) -> Optional[Dict]:
    ref = _normalize_order_ref_for_lookup(raw_ref)
    if not ref:
        return None
    all_orders = [order for order in orders.load_orders_data() if isinstance(order, dict)]

    def _funpay_matches() -> List[Dict]:
        result = []
        for order_record in all_orders:
            for field_name in ("order_id", "order_id_funpay"):
                value = _normalize_order_ref_for_lookup(order_record.get(field_name))
                if value and value == ref:
                    result.append(order_record)
                    break
        return result

    def _panel_matches() -> List[Dict]:
        result = []
        for order_record in all_orders:
            for field_name in ("panel_order_id", "twiboost_id", "id_zakaz"):
                value = str(order_record.get(field_name) or "").strip()
                if value and value == ref:
                    result.append(order_record)
                    break
        return result

    ordered_matchers = (_panel_matches, _funpay_matches) if ref.isdigit() else (_funpay_matches, _panel_matches)
    for matcher in ordered_matchers:
        matches = matcher()
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("ORDER REF AMBIGUOUS: ref=%s matched %s local records", ref, len(matches))
            return None
    return None

def _buyer_can_access_order(order_record: Dict, buyer_id: Any, chat_id: Any) -> bool:
    if not isinstance(order_record, dict):
        return False
    if buyer_id not in (None, "") and str(order_record.get("buyer_id")) == str(buyer_id):
        return True
    if chat_id not in (None, "") and str(order_record.get("chat_id")) == str(chat_id):
        return True
    return False


# =========================================================
# EVENT HANDLERS / INIT
# =========================================================

def auto_smm_handler(c: Cardinal, e, *args):
    global RUNNING, orders_info, waiting_for_link

    if not RUNNING:
        return

    my_id = c.account.id
    bot_ = c.telegram.bot

    if isinstance(e, NewMessageEvent):
        if e.message.author_id == my_id:
            return

        msg_text_raw = e.message.text or ""
        msg_text = normalize_incoming_text(msg_text_raw)
        msg_author_id = e.message.author_id
        msg_chat_id = e.message.chat_id

        logger.info(f"Новое сообщение от {e.message.author}: {msg_text}")

        if _sync_funpay_status_from_message(msg_text):
            return

        if _handle_buyer_commands_command(c, msg_text, msg_author_id, msg_chat_id):
            return

        if _handle_buyer_smm_command(c, msg_text, msg_author_id, msg_chat_id):
            return

        if _handle_buyer_link_command(c, msg_text, msg_author_id, msg_chat_id):
            return

        if _handle_buyer_cancel_command(c, msg_text, msg_author_id, msg_chat_id):
            return

        m_check = re.match(r'^чек\s+(.+?)\s*$', msg_text, re.IGNORECASE)
        if m_check:
            raw_ref = m_check.group(1).strip()
            found = _find_order_by_any_order_ref(raw_ref)
            if not found:
                c.send_message(msg_chat_id, ORDER_NOT_FOUND_TEXT)
                return
            if not _buyer_can_access_order(found, msg_author_id, msg_chat_id):
                c.send_message(msg_chat_id, ORDER_NOT_FOUND_TEXT)
                return

            if _is_order_refundedish(found):
                progress_text = _format_buyer_progress(found)
                c.send_message(msg_chat_id, f"📦 {_format_buyer_lot_title(found)}\n\nСтатус: недоступен после возврата\nПрогресс: {progress_text}")
                return

            panel_order_id = _get_panel_order_id(found)
            if panel_order_id is None:
                state = _get_buyer_flow_state(found)
                if state == BUYER_STATE_WAITING_CONFIRM:
                    c.send_message(msg_chat_id, "Ссылка ожидает подтверждения. Напишите + или -.")
                else:
                    c.send_message(msg_chat_id, "Заказ ещё не запущен. Отправьте ссылку для запуска.")
                return

            service_number = _safe_int(found.get("service_number")) or 1
            result = _fetch_single_order_status(service_number, panel_order_id)
            order_id_funpay = str(found.get("order_id") or found.get("order_id_funpay") or "")
            if result.get("ok"):
                _apply_status_result(c, found, result)
                found = orders.get_order_data(order_id_funpay) or found
                status_text = _human_panel_status(found)
                quantity_text = _format_buyer_progress(found)
                c.send_message(
                    msg_chat_id,
                    f"📦 {_format_buyer_lot_title(found)}\n\nСтатус: {status_text}\nПрогресс: {quantity_text}\n\nПожалуйста, не злоупотребляйте командой.\nМы уведомим вас, когда заказ будет завершён.",
                )
            else:
                if order_id_funpay:
                    orders.update_order_fields(order_id_funpay, status_last_error=result.get("error") or "status_error", status_last_checked_at=_now_str(), updated_at=_now_str())
                c.send_message(msg_chat_id, "Не удалось проверить статус. Попробуйте позже или напишите продавцу.")
            return

        m_refill = re.match(r'^рефилл\s+(.+?)\s*$', msg_text, re.IGNORECASE)
        if m_refill:
            raw_ref = m_refill.group(1).strip()
            found = _find_order_by_any_order_ref(raw_ref)
            if not found:
                c.send_message(msg_chat_id, ORDER_NOT_FOUND_TEXT)
                return
            if not _buyer_can_access_order(found, msg_author_id, msg_chat_id):
                c.send_message(msg_chat_id, ORDER_NOT_FOUND_TEXT)
                return
            if _apply_refund_gate(c, found, notify_chat_id=msg_chat_id, force_check=True):
                return

            panel_order_id = _get_panel_order_id(found)
            if panel_order_id is None:
                c.send_message(msg_chat_id, "Заказ ещё не запущен. Рефилл будет доступен только после запуска заказа.")
                return

            cfg = load_config()
            service_cfg = cfg.get("services", {}).get(str(found.get("service_number")))
            if not service_cfg:
                c.send_message(msg_chat_id, "Рефилл недоступен для этого заказа.\n\nВозможные причины:\nуслуга не поддерживает гарантию, заказ ещё выполняется или срок гарантии истёк.")
                return
            api_url = service_cfg.get("api_url")
            api_key = service_cfg.get("api_key")
            invalid_key_payload = _validate_api_key_for_action(found.get("service_number"), "refill", api_key)
            if invalid_key_payload:
                c.send_message(msg_chat_id, "Рефилл недоступен для этого заказа.\n\nВозможные причины:\nуслуга не поддерживает гарантию, заказ ещё выполняется или срок гарантии истёк.")
                return
            url_ = f"{api_url}?action=refill&order={panel_order_id}&key={api_key}"
            try:
                rr = requests.get(url_, timeout=15)
                rr.raise_for_status()
                rdata = rr.json()
                st_ = rdata.get("status", 0)
                if str(st_).lower() in ("1", "true", "success"):
                    c.send_message(
                        msg_chat_id,
                        f"🔄 Рефилл запрошен.\n\nID заказа в сервисе: {panel_order_id}\n\nЕсли услуга поддерживает гарантию, панель обработает запрос автоматически.",
                    )
                else:
                    c.send_message(msg_chat_id, "Рефилл недоступен для этого заказа.\n\nВозможные причины:\nуслуга не поддерживает гарантию, заказ ещё выполняется или срок гарантии истёк.")
            except Exception:
                c.send_message(msg_chat_id, "Не удалось запросить рефилл. Попробуйте позже или напишите продавцу.")
            return

        if _handle_buyer_flow_message(c, msg_text, msg_author_id, msg_chat_id):
            return

    elif isinstance(e, NewOrderEvent):
        order_ = e.order
        orderID = order_.id
        orderDesc = order_.description
        orderAmount = order_.amount
        orderPrice = order_.price

        logger.info(f"Новый заказ #{orderID}: {orderDesc}, x{orderAmount}")

        cfg = load_config()

        od_full = c.account.get_order(orderID)
        buyer_chat_id = od_full.chat_id
        buyer_id = od_full.buyer_id
        buyer_username = od_full.buyer_username

        lot_result = resolve_lot_by_description(orderDesc, orderAmount)
        if not lot_result.get("ok"):
            error_code = str(lot_result.get("error") or "no_match")

            if _is_lot_match_no_match(lot_result):
                logger.info(
                    "AUTO SMM SKIP NON-AUTOSMM ORDER: order=%s, description=%r",
                    orderID,
                    str(orderDesc or "")[:300],
                )
                return

            stop_reason = "lot_match_ambiguous" if _is_lot_match_ambiguous(lot_result) else "lot_match_failed"
            logger.warning(
                "LOT MATCH MANUAL CHECK: order=%s, error=%s",
                orderID,
                error_code,
            )
            _upsert_buyer_flow_order_record(
                chat_id=buyer_chat_id,
                buyer_id=buyer_id,
                buyer_username=buyer_username,
                order_id_funpay=str(orderID),
                lot_title=str(orderDesc or "SMM-заказ"),
                order_price=orderPrice,
                order_amount=orderAmount,
                service_id=0,
                real_amount=0,
                service_number=0,
                funpay_status="open",
                state=BUYER_STATE_FAILED,
                saved_link=None,
            )
            orders.update_order_fields(
                str(orderID),
                status="unknown",
                buyer_flow_state=BUYER_STATE_FAILED,
                status_stop_reason=stop_reason,
                updated_at=_now_str(),
            )
            _send_lot_match_manual_alert(orderID, orderDesc, lot_result)
            _send_buyer_message(c, buyer_chat_id, BUYER_LOT_MATCH_MANUAL_TEXT)
            return

        service_id = lot_result.get("service_id")
        real_amount = lot_result.get("real_amount")
        srv_number = lot_result.get("service_number", 1)
        matched_lot_title = lot_result.get("lot_title") or orderDesc
        orders_info[orderID] = {
            "buyer_id": buyer_id,
            "chat_id": buyer_chat_id,
            "summa": orderPrice
        }

        save_order_info(orderID, orderPrice, orderDesc, 0)
        _upsert_buyer_flow_order_record(
            chat_id=buyer_chat_id,
            buyer_id=buyer_id,
            buyer_username=buyer_username,
            order_id_funpay=str(orderID),
            lot_title=matched_lot_title,
            order_price=orderPrice,
            order_amount=orderAmount,
            service_id=service_id,
            real_amount=real_amount,
            service_number=srv_number,
            funpay_status="open",
            state=BUYER_STATE_WAITING_LINK,
        )

        order_record = orders.get_order_data(str(orderID)) or {}
        msg_payment = _render_template(
            cfg.get("messages", {}).get("after_payment", "❤️ Спасибо за оплату! Укажите ссылку для запуска заказа."),
            order_record,
            {
                "buyer_username": buyer_username,
                "lot_title": matched_lot_title,
                "orderDesc": matched_lot_title,
                "order_id_funpay": orderID,
                "order_price": orderPrice,
                "orderPrice": orderPrice,
                "order_amount": orderAmount,
                "orderAmount": orderAmount,
                "currency": "RUB",
                "total_quantity": real_amount,
                "done_quantity": 0,
                "left_quantity": real_amount,
                "progress_percent": 0,
                "funpay_status": "open",
                "panel_status": "",
            },
        )
        _send_buyer_message(c, buyer_chat_id, msg_payment)

        waiting_for_link[str(orderID)] = {
            "buyer_id": buyer_id,
            "chat_id": buyer_chat_id,
            "buyer_username": buyer_username,
            "service_id": service_id,
            "real_amount": real_amount,
            "order_id_funpay": str(orderID),
            "price": orderPrice,
            "service_number": srv_number,
            "lot_title": matched_lot_title,
            "state": BUYER_STATE_WAITING_LINK,
            "step": "await_link"
        }





def send_chat_id(message: types.Message):
    try:
        bot.send_message(
            message.chat.id,
            f"🆔 <b>Chat ID</b>\n\n"
            f"Текущий chat_id:\n"
            f"<code>{h(message.chat.id)}</code>\n\n"
            f"<i>Используйте его в настройках уведомлений AutoSMM.</i>",
            parse_mode="HTML"
        )
    except Exception as ex:
        logger.warning(f"CHATID COMMAND FAILED: {ex}")


def init_commands(c_: Cardinal):
    global bot, config, lot_mapping, cardinal_instance
    logger.info("=== init_commands() from auto_smm (2 services) ===")
    cardinal_instance = c_
    bot = c_.telegram.bot
    
    def handle_document_upload(message: types.Message):
        user_id = message.from_user.id
        logger.info(f"Получен документ от {user_id}. Проверка ожидания...")
        file_id = message.document.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        if user_id in waiting_for_backup_restore_upload:
            waiting_for_backup_restore_upload.discard(user_id)
            try:
                data = json.loads(downloaded_file.decode('utf-8'))
                ok, result_text = _restore_backup_wrapper(data)
                kb_ = InlineKeyboardMarkup()
                kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
                bot.send_message(message.chat.id, result_text, reply_markup=kb_)
            except json.JSONDecodeError as e:
                bot.send_message(message.chat.id, f"❌ Не удалось считать файл бэкапа. Проверьте JSON. ({e})")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Произошла ошибка при восстановлении бэкапа: {e}")
                logger.error(f"Неизвестная ошибка при восстановлении бэкапа: {e}")
            return

        if user_id not in waiting_for_lots_upload:
            logger.info(f"Пользователь {user_id} не ожидает загрузки файла")
            bot.send_message(message.chat.id, "❌ Вы не активировали загрузку файла. Используйте меню настроек.")
            return
        waiting_for_lots_upload.remove(user_id)
        logger.info(f"Пользователь {user_id} удалён из ожидания. Обрабатываю файл настроек...")
        try:
            data = json.loads(downloaded_file.decode('utf-8'))
            if "lot_mapping" not in data:
                bot.send_message(message.chat.id, "❌ Ошибка: в файле нет раздела с лотами.")
                logger.error("JSON не содержит 'lot_mapping'")
                return
            _ensure_config_defaults(data)
            save_config(data)
            kb_ = InlineKeyboardMarkup()
            kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
            bot.send_message(message.chat.id, "✅ Файл настроек успешно загружен и сохранён!", reply_markup=kb_)
            logger.info("JSON успешно загружен и сохранён")
        except json.JSONDecodeError as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: Не удалось считать JSON. Проверьте синтаксис. ({e})")
            logger.error(f"Ошибка декодирования JSON: {e}")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Произошла ошибка при загрузке файла: {e}")
            logger.error(f"Неизвестная ошибка при загрузке: {e}")

    cfg = load_config()
    config.update(cfg)
    lot_mapping.clear()
    lot_mapping.update(cfg.get("lot_mapping", {}))
    global RUNNING, IS_STARTED, ORDER_CHECK_THREAD, AUTO_LOTS_SEND_THREAD
    RUNNING = True
    IS_STARTED = True
    _recover_buyer_flow_runtime(c_)
    if not ORDER_CHECK_THREAD or not ORDER_CHECK_THREAD.is_alive():
        ORDER_CHECK_THREAD = threading.Thread(target=start_order_checking, args=(c_,), daemon=True)
        ORDER_CHECK_THREAD.start()
    try:
        if bool(cfg.get("send_auto_lots", False)) and (not AUTO_LOTS_SEND_THREAD or not AUTO_LOTS_SEND_THREAD.is_alive()):
            AUTO_LOTS_SEND_THREAD = threading.Thread(target=start_auto_lots_sender, args=(c_,), daemon=True)
            AUTO_LOTS_SEND_THREAD.start()
    except Exception as ex:
        logger.warning(f"AUTO SMM AUTO LOTS THREAD START FAILED: {ex}")
    c_.add_telegram_commands(UUID, [
        ("autosmm", "Открыть AutoSMM", True),
        ("chatid", "Показать chat_id", True),
            ])



    # --- Telegram handlers registration (NO decorators; Cardinal-safe) ---
    def _add_cb(_fn, **_kw):
        def _guarded_callback(call):
            data = str(getattr(call, "data", "") or "")
            telemetry_allowed_callbacks = {
                "autosmm_telemetry_accept",
                "autosmm_telemetry_refuse",
                "autosmm_telemetry_guest_24h",
            }
            if data not in telemetry_allowed_callbacks and not _telemetry_gate_allows_menu():
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _show_telemetry_gate(call.message.chat.id, message_id=call.message.message_id)
                return
            return _fn(call)
        try:
            bot.add_callback_query_handler(_guarded_callback, **_kw)
        except Exception:
            try:
                bot.register_callback_query_handler(_guarded_callback, **_kw)
            except Exception:
                pass

    def _add_msg(_fn, **_kw):
        try:
            bot.add_message_handler(_fn, **_kw)
        except Exception:
            try:
                bot.register_message_handler(_fn, **_kw)
            except Exception:
                pass

    _add_msg(handle_document_upload, content_types=['document'])

    _add_cb(change_fallback_snum_prompt, func=lambda call: call.data.startswith("change_fallback_snum_"))
    _add_cb(change_fallback_id_prompt, func=lambda call: call.data.startswith("change_fallback_id_"))
    _add_cb(manage_websites, func=lambda call: call.data == "manage_websites")
    _add_cb(show_allowed_websites_list, func=lambda call: call.data == "show_allowed_websites_list")
    _add_cb(add_website_prompt, func=lambda call: call.data == "add_website")
    _add_cb(reset_valid_links_prompt, func=lambda call: call.data == "reset_valid_links_prompt")
    _add_cb(reset_valid_links_confirm, func=lambda call: call.data == "reset_valid_links_confirm")
    _add_cb(remove_website_prompt, func=lambda call: call.data.startswith("delete_website_"))
    _add_cb(confirm_delete_website_callback, func=lambda call: call.data.startswith("confirm_delete_website_"))
    _add_cb(delete_all_lots_prompt, func=lambda call: call.data == "delete_all_lots")
    _add_cb(confirm_delete_all_lots, func=lambda call: call.data == "confirm_delete_all_lots")
    _add_cb(lot_settings, func=lambda call: call.data == "lot_settings")
    _add_cb(show_lots_list, func=lambda call: call.data == "show_lots_list")
    _add_cb(search_lot_prompt, func=lambda call: call.data == "search_lot")
    _add_cb(edit_lot_from_search, func=lambda call: call.data.startswith("edit_lot_search_"))
    _add_cb(edit_lot_callback, func=lambda call: call.data.startswith("edit_lot_") and not call.data.startswith("edit_lot_search_"))
    _add_cb(search_results_back, func=lambda call: call.data == "search_results_back")
    _add_cb(lot_input_back_callback, func=lambda call: call.data == "lot_input_back")
    _add_cb(lot_input_cancel_callback, func=lambda call: call.data == "lot_input_cancel")
    _add_cb(ui_input_back_callback, func=lambda call: call.data == "ui_input_back")
    _add_cb(ui_input_cancel_callback, func=lambda call: call.data == "ui_input_cancel")
    _add_cb(page_navigation, func=lambda call: call.data.startswith("prev_page_") or call.data.startswith("next_page_"))
    _add_cb(show_orders, func=lambda call: call.data == "show_orders")
    _add_cb(analytics_period, func=lambda call: call.data.startswith("analytics_period_"))
    _add_cb(analytics_more, func=lambda call: call.data.startswith("analytics_more_"))
    _add_cb(analytics_less, func=lambda call: call.data.startswith("analytics_less_"))
    _add_cb(upload_lots_json, func=lambda call: call.data == "upload_lots_json")
    _add_cb(export_files, func=lambda call: call.data == "export_files")
    _add_cb(export_errors, func=lambda call: call.data == "export_errors")
    _add_cb(cleanup_old_files_manual, func=lambda call: call.data == "cleanup_old_files_manual")
    _add_cb(cleanup_old_files_later, func=lambda call: call.data == "cleanup_old_files_later")
    _add_cb(cleanup_old_files_prompt, func=lambda call: call.data == "cleanup_old_files_prompt")
    _add_cb(cleanup_old_files_cancel, func=lambda call: call.data == "cleanup_old_files_cancel")
    _add_cb(cleanup_old_files_confirm, func=lambda call: call.data == "cleanup_old_files_confirm")
    _add_cb(confirm_delete_orders, func=lambda call: call.data == "delete_orders")
    _add_cb(delete_orders_yes, func=lambda call: call.data == "delete_orders_yes")
    _add_cb(toggle_auto_refunds, func=lambda call: call.data == "toggle_auto_refunds")
    _add_cb(enable_auto_refunds, func=lambda call: call.data == "enable_auto_refunds")
    _add_cb(toggle_confirm_link, func=lambda call: call.data == "toggle_confirm_link")
    _add_cb(toggle_send_auto_lots, func=lambda call: call.data == "toggle_send_auto_lots")
    _add_cb(toggle_buyer_cancel_before_panel, func=lambda call: call.data == "toggle_buyer_cancel_before_panel")
    _add_cb(enable_buyer_cancel_before_panel, func=lambda call: call.data == "enable_buyer_cancel_before_panel")
    _add_cb(change_send_interval, func=lambda call: call.data == "change_send_interval")
    _add_cb(cancel_interval_change, func=lambda call: call.data == "cancel_interval_change")
    _add_cb(return_to_settings, func=lambda call: call.data == "return_to_settings")
    _add_cb(return_to_lots, func=lambda call: call.data.startswith("return_to_lots"))
    _add_cb(change_name, func=lambda call: call.data.startswith("change_name_"))
    _add_cb(change_id, func=lambda call: call.data.startswith("change_id_"))
    _add_cb(change_quantity, func=lambda call: call.data.startswith("change_quantity_"))
    _add_cb(change_snum, func=lambda call: call.data.startswith("change_snum_"))
    _add_cb(delete_one_lot_callback, func=lambda call: call.data.startswith("delete_one_lot_"))
    _add_cb(confirm_delete_one_lot_callback, func=lambda call: call.data.startswith("confirm_delete_one_lot_"))
    _add_cb(api_settings_callback, func=lambda call: call.data == "api_settings")
    _add_cb(api_service_callback, func=lambda call: call.data.startswith("api_service_"))
    _add_cb(header_no_action_callback, func=lambda call: call.data == "header_no_action")
    _add_cb(edit_apiurl, func=lambda call: call.data.startswith("edit_apiurl_"))
    _add_cb(edit_apikey, func=lambda call: call.data.startswith("edit_apikey_"))
    _add_cb(edit_apiname, func=lambda call: call.data.startswith("edit_apiname_"))
    _add_cb(edit_apinum, func=lambda call: call.data.startswith("edit_apinum_"))
    _add_cb(check_balance, func=lambda call: call.data.startswith("check_balance_"))
    _add_cb(add_new_lot, func=lambda call: call.data == "add_new_lot")
    _add_cb(edit_messages_menu, func=lambda call: call.data == "edit_messages")
    _add_cb(edit_msg_payment, func=lambda call: call.data == "edit_msg_payment")
    _add_cb(edit_msg_confirmation, func=lambda call: call.data == "edit_msg_confirmation")
    _add_cb(add_service, func=lambda call: call.data == "add_service")
    _add_cb(delete_service, func=lambda call: call.data == "delete_service")
    _add_cb(set_notification_chat_id, func=lambda call: call.data == "set_notification_chat_id")
    _add_cb(files_menu_callback, func=lambda call: call.data == "files_menu")
    _add_cb(misc_settings_callback, func=lambda call: call.data == "misc_settings")
    _add_cb(toggle_fallback, func=lambda call: call.data == "toggle_fallback")
    _add_cb(links_menu_callback, func=lambda call: call.data == "links_menu")
    _add_cb(telemetry_accept_callback, func=lambda call: call.data == "autosmm_telemetry_accept")
    _add_cb(telemetry_refuse_callback, func=lambda call: call.data == "autosmm_telemetry_refuse")
    _add_cb(telemetry_guest_24h_callback, func=lambda call: call.data == "autosmm_telemetry_guest_24h")
    _add_cb(buyer_flow_settings, func=lambda call: call.data == "buyer_flow_settings")
    _add_cb(seller_notifications_settings, func=lambda call: call.data == "seller_notifications_settings")
    _add_cb(use_current_chat_for_notifications, func=lambda call: call.data == "use_current_chat_for_notifications")
    _add_cb(send_test_notification, func=lambda call: call.data == "send_test_notification")
    _add_cb(disable_notifications, func=lambda call: call.data == "disable_notifications")
    _add_cb(full_backup_prompt, func=lambda call: call.data == "full_backup_prompt")
    _add_cb(full_backup_confirm, func=lambda call: call.data == "full_backup_confirm")
    _add_cb(restore_backup_prompt, func=lambda call: call.data == "restore_backup_prompt")
    _add_cb(autosmm_cleanup_menu, func=lambda call: call.data == "autosmm_cleanup_menu")
    _add_cb(full_autosmm_delete_prompt, func=lambda call: call.data == "full_autosmm_delete_prompt")
    _add_cb(full_autosmm_delete_confirm, func=lambda call: call.data == "full_autosmm_delete_confirm")
    _add_cb(edit_msg_link_confirm, func=lambda call: call.data == "edit_msg_link_confirm")
    _add_cb(edit_msg_completion, func=lambda call: call.data == "edit_msg_completion")
    _add_cb(template_view_callback, func=lambda call: call.data.startswith("template_view_"))
    _add_cb(template_edit_callback, func=lambda call: call.data.startswith("template_edit_"))
    _add_cb(template_reset_prompt_callback, func=lambda call: call.data.startswith("template_reset_prompt_"))
    _add_cb(template_reset_confirm_callback, func=lambda call: call.data.startswith("template_reset_confirm_"))
    _add_cb(reset_message_texts_prompt, func=lambda call: call.data == "reset_message_texts_prompt")
    _add_cb(reset_message_texts_confirm, func=lambda call: call.data == "reset_message_texts_confirm")

    # Telegram commands (Cardinal)
    c_.telegram.msg_handler(auto_smm_settings, commands=["autosmm"])
    c_.telegram.msg_handler(send_chat_id, commands=["chatid"])


def start_order_checking_if_needed(c: Cardinal):
    if RUNNING:  
        start_order_checking(c)


def start_auto_lots_sender(c: Cardinal):
    """
    Отправляет файл файл настроек на заданный chat_id с заданной периодичностью
    """
    global RUNNING
    
    logger.info("Запуск потока отправки файл настроек")
    
    while RUNNING:
        try:
            cfg = load_config()
            chat_id = cfg.get("notification_chat_id")
            send_auto_lots = cfg.get("send_auto_lots", False)
            interval_minutes = cfg.get("send_auto_lots_interval", 30)
            
            if chat_id and send_auto_lots and os.path.exists(CONFIG_PATH):
                if c.telegram and c.telegram.bot:
                    try:
                        with open(CONFIG_PATH, 'rb') as file:
                            c.telegram.bot.send_document(
                                chat_id, 
                                file, 
                                caption=f"📄 Автоматическая отправка файла файл настроек\n\n⏱️ Следующая отправка через {interval_minutes} минут"
                            )
                            logger.info(f"Файл файл настроек отправлен на chat_id {chat_id}")
                    except Exception as e:
                        logger.error(f"Ошибка при отправке файл настроек: {e}")
            
            wait_time = interval_minutes * 60
            sleep_interval = 1
            
            for _ in range(wait_time):
                if not RUNNING:
                    break
                time.sleep(sleep_interval)
                
        except Exception as e:
            logger.error(f"Ошибка в потоке отправки файл настроек: {e}")
            time.sleep(60)



BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [auto_smm_handler]
BIND_TO_NEW_ORDER = [auto_smm_handler]
BIND_TO_DELETE = None