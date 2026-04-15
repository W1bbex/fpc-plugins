from __future__ import annotations
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
if TYPE_CHECKING:
    from cardinal import Cardinal

from urllib.parse import quote
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

from datetime import datetime, timedelta

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI import enums

from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# =========================================================
# META / CONSTANTS / GLOBALS
# =========================================================

NAME = "AutoSMM"
VERSION = "0.0.6"
DESCRIPTION = "Плагин для автоматической накрутки через 2+ сервис"
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

waiting_for_lots_upload = set()
LOT_PAGE_CONTEXT: Dict[int, int] = {}
LOT_INPUT_STATES: Dict[int, Dict[str, Any]] = {}
LOT_CARD_CONTEXT: Dict[int, Dict[str, Any]] = {}
UI_INPUT_STATES: Dict[int, Dict[str, Any]] = {}
SEARCH_RESULT_CONTEXT: Dict[int, Dict[str, Any]] = {}

LOG_DIR = os.path.join("storage", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "auto_smm.log")
EXPECTED_API_KEY_LENGTH = 60

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

bot = None
config = {}
lot_mapping = {}
cardinal_instance = None

CONFIG_PATH = os.path.join("storage", "cache", "auto_lots.json")
VALID_LINKS_PATH = os.path.join("storage", "cache", "valid_link.json")
ORDERS_PATH = os.path.join("storage", "cache", "auto_smm_orders.json")
ORDERS_DATA_PATH = os.path.join("storage", "cache", "orders_data.json")
ORDERS_RUNTIME_PATH = os.path.join("storage", "cache", "orders_runtime.json")
os.makedirs(os.path.dirname(ORDERS_PATH), exist_ok=True)
os.makedirs(os.path.dirname(ORDERS_DATA_PATH), exist_ok=True)
os.makedirs(os.path.dirname(ORDERS_RUNTIME_PATH), exist_ok=True)

PLUGIN_FILE_PATH = Path(__file__).resolve()
PLUGIN_DIR = PLUGIN_FILE_PATH.parent
ACTIVE_PLUGIN_FILENAME = PLUGIN_FILE_PATH.name
OLD_FILES_REMIND_CFG_KEY = "old_files_cleanup_remind_until"
FULL_AUTOSMM_FILE_RE = re.compile(r"(?i)^AutoSMM(?:_(\d+(?:\.\d+)*))?\.py(?:c|o)?$")
HELPER_AUTOSMM_FILE_RE = re.compile(r"(?i)^_autosmm_.*\.(?:py|pyc|pyo)$")

REFUND_SYNC_PATTERNS = [
    re.compile(r"вернул[аи]?\s+деньги.*?по\s+заказу\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"возврат.*?по\s+заказу\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"returned\s+money.*?order\s+#?([A-Z0-9]+)", re.IGNORECASE | re.DOTALL),
]
STATUS_BATCH_SIZE = 50
STATUS_BATCH_INTERVAL_SECONDS = 300
INCORRECT_ORDER_ID_MAX_ATTEMPTS = 3

# =========================================================
# STORAGE
# =========================================================

RUNTIME_FIELDS = {
    "status_last_checked_at",
    "status_last_error",
    "incorrect_order_id_attempts",
    "status_stop_reason",
    "manual_check_alert_sent",
}

LEGACY_COMPLETED_NOTIFICATION_RUNTIME_FIELD = "completed_notification_sent"

def load_valid_links() -> List[str]:
    if os.path.exists(VALID_LINKS_PATH):
        with open(VALID_LINKS_PATH, 'r', encoding='utf-8') as f:
            links = json.load(f)
            if not links:
                default_links = [
                    "vk.com", "t.me", "instagram.com", "tiktok.com", "youtube.com",
                    "youtu.be", "twitch.tv", "vt.tiktok.com", "vm.tiktok.com",
                    "www.youtu.be", "www.youtube.com", "twitter.com"
                ]
                save_valid_links(default_links)
                return default_links
            return links
    else:
        default_links = [
            "vk.com", "t.me", "instagram.com", "tiktok.com", "youtube.com",
            "youtu.be", "twitch.tv", "vt.tiktok.com", "vm.tiktok.com",
            "www.youtu.be", "www.youtube.com", "twitter.com"
        ]
        save_valid_links(default_links)
        return default_links


def save_valid_links(links: List[str]):
    os.makedirs(os.path.dirname(VALID_LINKS_PATH), exist_ok=True)
    with open(VALID_LINKS_PATH, 'w', encoding='utf-8') as f:
        json.dump(links, f, ensure_ascii=False, indent=4)


def load_config() -> Dict:
    logger.info("Загрузка конфигурации (auto_lots.json)...")
    
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
                    
                    if "services" not in cfg:
                        cfg["services"] = {
                            "1": {
                                "api_url": "https://service1.com/api/v2",
                                "api_key": "SERVICE_1_KEY"
                            },
                            "2": {
                                "api_url": "https://service2.com/api/v2",
                                "api_key": "SERVICE_2_KEY"
                            }
                        }
                    if "auto_refunds" not in cfg:
                        cfg["auto_refunds"] = False
                    if "fallback_enabled" not in cfg:
                        cfg["fallback_enabled"] = False
                    if "confirm_link" not in cfg:
                        cfg["confirm_link"] = True
                    if "messages" not in cfg:
                        cfg["messages"] = {
                            "after_payment": "❤️ Благодарим за оплату!\n\nЧтобы начать накрутку, отправьте корректную ссылку на вашу страницу или пост в социальных сетях. Ссылка должна начинаться с \"https://\", например:\n\nПример: https://teletype.in/@w1bbex/wwTwed1ZBZE\n\nБез правильной ссылки накрутка не будет запущена. Убедитесь, что она ведет на активную страницу, доступную для общего просмотра.",
                            "after_confirmation": "🎉 Ваш заказ успешно оформлен!\n\n🔢 ID заказа в сервисе: {twiboost_id}\n🔗 Для отслеживания переходите по ссылке: {link}\n\n📋 Доступные команды:\n🔍 чек {twiboost_id} — Проверить статус заказа (выводит информацию о заказе)\n🔄 рефилл {twiboost_id} — Запросить рефилл (работает лишь с гарантией - восстанавливает отписанных подписчиков)\n\nЕсли у вас возникнут вопросы, не стесняйтесь обращаться!"
                        }
                    if "notification_chat_id" not in cfg:
                        cfg["notification_chat_id"] = None
                    if "send_auto_lots" not in cfg:
                        cfg["send_auto_lots"] = False
                    if "send_auto_lots_interval" not in cfg:
                        cfg["send_auto_lots_interval"] = 30
                    if "auto_start" not in cfg:
                        cfg["auto_start"] = True
                    if "lot_mapping" not in cfg:
                        cfg["lot_mapping"] = {}
                    if "new_order_notifications" not in cfg:
                        cfg["new_order_notifications"] = False
                    return cfg
                except Exception as e:
                    logger.error(f"Ошибка при чтении файла конфигурации: {e}. Создаем новый файл конфигурации.")
                    cfg = create_default_config()
                    save_config(cfg)
                    return cfg
            else:
                logger.info(f"Файл конфигурации {CONFIG_PATH} не найден. Создаем новый файл.")
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
        "confirm_link": True,
        "messages": {
            "after_payment": """👾 Благодарю за оплату!

Чтобы начать накрутку, отправьте корректную ссылку на вашу страницу или пост в социальных сетях. Ссылка должна начинаться с "https://", например:

Пример: https://telegra.ph/Primery-ssylok-11-11

Без правильной ссылки накрутка не будет запущена. Убедитесь, что она ведет на активную страницу, доступную для общего просмотра.""",
            "after_confirmation": """🎉 Ваш заказ успешно оформлен!

🔢 ID заказа в сервисе: {twiboost_id}
🔗 Для отслеживания переходите по ссылке: {link}

📋 Доступные команды:
🔍 чек {twiboost_id} — Проверить статус заказа (выводит информацию о заказе)
🔄 рефилл {twiboost_id} — Запросить рефилл (работает лишь с гарантией - восстанавливает отписанных подписчиков)

Если у вас возникнут вопросы, не стесняйтесь обращаться!"""
        },
        "notification_chat_id": None,
        "send_auto_lots": False,
        "send_auto_lots_interval": 30,
        "auto_start": True,
        "lot_mapping": {},
        "new_order_notifications": False
    }


def save_config(cfg: Dict):
    logger.info("Сохранение конфигурации (auto_lots.json)...")
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
    logger.info("Конфигурация сохранена.")


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

            candidate_version = _extract_autosmm_file_version(item_name)
            if candidate_version is None:
                continue

            if not candidate_version or candidate_version < current_version:
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
    return _migrate_legacy_completed_notification_flags(raw_orders)


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
) -> Dict:
    record = {
        "chat_id": chat_id,
        "order_id": order_id,
        "id_zakaz": twiboost_id,
        "status": status,
        "chistota": float(chistota),
        "customer_url": customer_url,
        "quantity": quantity,
        "service_number": service_number,
        "is_refunded": is_refunded,
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
            order.update(
                {
                    "chat_id": chat_id,
                    "order_id": order_id,
                    "id_zakaz": twiboost_id,
                    "status": status,
                    "customer_url": customer_url,
                    "quantity": quantity,
                    "service_number": service_number,
                    "is_refunded": is_refunded,
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
            if value is None and key not in ("is_refunded",):
                order.pop(key, None)
            else:
                order[key] = value

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
        if bool(order.get("is_refunded", False)):
            return False
        order["is_refunded"] = True
        if detected_at not in (None, ""):
            order["refund_detected_at"] = detected_at
        if source not in (None, ""):
            order["refund_source"] = source
        updated = True
        break

    if updated:
        save_orders_data(orders)
        return True

    logger.warning(f"Заказ #{order_id_funpay} не найден в orders_data.json.")
    return False


def update_order_status(order_id_funpay: str, new_status: str):
    update_order_fields(order_id_funpay, status=new_status)
    logger.info(f"Статус заказа #{order_id_funpay} обновлён на '{new_status}'.")


def update_order_refunded_status(order_id_funpay: str):
    updated = mark_order_refunded(order_id_funpay)
    if updated:
        logger.info(f"Статус заказа #{order_id_funpay} обновлён на 'is_refunded': True.")
    else:
        logger.warning(f"Заказ #{order_id_funpay} не найден или уже отмечен как 'is_refunded'.")


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
        f"{key_len} символов (ожидается {EXPECTED_API_KEY_LENGTH})."
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
    order_id = _extract_refund_order_id_from_message(message_text)
    if not order_id:
        return False

    order_data = orders.get_order_data(order_id)
    if not order_data:
        logger.info(f"REFUND SYNC: заказ #{order_id} не найден в orders_data.json")
        return True

    if order_data.get("is_refunded", False):
        logger.info(f"REFUND SYNC: заказ #{order_id} уже помечен как refunded")
        return True

    updated = orders.mark_order_refunded(
        order_id_funpay=order_id,
        detected_at=_now_str(),
        source="funpay_message",
    )
    if updated:
        logger.info(f"REFUND SYNC: заказ #{order_id} помечен как refunded из сообщения FunPay")
    return True


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

    update_fields = {
        "status": mapped_status,
        "status_last_checked_at": _now_str(),
        "status_last_error": None,
        "incorrect_order_id_attempts": 0,
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
    if bool(order_record.get("is_refunded", False)):
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


def _check_pending_orders_batch(c: Cardinal):
    try:
        all_orders = orders.load_orders_data()
    except Exception as ex:
        logger.error(f"Ошибка при загрузке orders_data.json для batch status: {ex}")
        return

    pending_orders = []
    for order_record in all_orders:
        status_ = str(order_record.get("status", "unknown") or "unknown").lower()
        if status_ == "pending" and not bool(order_record.get("is_refunded", False)):
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
                logger.warning(
                    f"BATCH STATUS FALLBACK TO SINGLE: service=#{service_number}, error={result.get('error')}, detail={result.get('detail')}"
                )
                for order_record in chunk:
                    _sync_single_order_status(c, order_record, background_sync=True)
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
    if order_data and order_data.get("is_refunded", False):
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
            waiting_for_link.pop(str(order_id_funpay), None)
        except Exception as ex:
            logger.error(
                f"Не удалось вернуть средства для заказа #{order_id_funpay}: {ex}. "
                f"Технические детали: {detailed_reason}"
            )
            _send_admin_order_alert(
                order_id_funpay=order_id_funpay,
                reason=reason,
                bot_action=failure_bot_action,
                action=failure_admin_action,
                title=failure_title,
            )
    else:
        logger.warning(f"Для заказа #{order_id_funpay} требуется ручной возврат. Технические детали: {detailed_reason}")
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
    while RUNNING:
        try:
            _check_pending_orders_batch(c)
        except Exception as ex:
            logger.error(f"Ошибка в batch status мониторинге: {ex}")

        for _ in range(STATUS_BATCH_INTERVAL_SECONDS):
            if not RUNNING:
                break
            time.sleep(1)


def get_tg_id_by_description(description: str, order_amount: int) -> Tuple[int, int, int] | None:
    for lot_key, lot_data in lot_mapping.items():
        lot_name = lot_data["name"]
        if re.search(re.escape(lot_name), description, re.IGNORECASE):
            service_id = lot_data["service_id"]
            base_q = lot_data["quantity"]
            real_q = base_q * order_amount
            srv_num = lot_data.get("service_number", 1)
            return service_id, real_q, srv_num
    return None


def is_valid_link(link: str) -> Tuple[bool, str]:
    valid_links = load_valid_links()
    if not link.startswith(("http://", "https://")):
        return False, "❌ Ссылка должна начинаться с http:// или https://."
    for pf in valid_links:
        if pf in link:
            return True, f"✅ Ссылка корректна ({pf})."
    return False, "❌ Недопустимая ссылка."


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

    try:
        msg_confirmation = cfg["messages"]["after_confirmation"].format(
            twiboost_id=twiboost_id,
            link=link_,
        )
    except Exception:
        msg_confirmation = f"🎉 Заказ успешно оформлен!\n\n🔢 ID заказа в сервисе: {twiboost_id}\n🔗 {link_}"

    c.send_message(buyer_chat_id, msg_confirmation)
    waiting_for_link.pop(str(order_id_funpay), None)


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
        logger.error("FALLBACK BLOCKED")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason=f"{primary_error} на панели #{service_number}",
            detailed_reason=(
                f"Primary ошибка: {primary_error}. Автоматический fallback не запускался. Детали: {primary_detail}"
            ),
            bot_action="автоматическая обработка остановлена, fallback не запускался",
            admin_action="проверьте заказ и при необходимости выполните возврат вручную",
            failure_title="⚠️ Заказ требует ручной проверки",
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
                f"Primary ошибка: {primary_error}. Fallback заблокирован. Детали: {primary_detail}"
            ),
            bot_action="fallback заблокирован, заказ не создан автоматически",
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
                f"Primary ошибка: {primary_error}. Fallback не запускался. Детали: {primary_detail}"
            ),
            bot_action="автоматическая обработка остановлена, fallback не запускался",
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
            detailed_reason=f"Fallback отключён. Primary ошибка: {primary_error} | {primary_detail}",
            bot_action="fallback отключён, заказ не создан автоматически",
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
            detailed_reason=f"Повторный fallback запрещён. Primary ошибка: {primary_error} | {primary_detail}",
            bot_action="повторный fallback запрещён, заказ не создан автоматически",
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
            detailed_reason=f"Fallback не настроен. Primary ошибка: {primary_error} | {primary_detail}",
            bot_action="fallback не настроен, заказ не создан автоматически",
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
                f"Primary ошибка: {primary_error}. Fallback заблокирован: primary и fallback указывают на один и тот же сервис #{service_number}."
            ),
            bot_action="fallback заблокирован, потому что primary и fallback указывают на одну панель",
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
                f"Fallback заблокирован: primary и fallback указывают на один и тот же сервис #{service_number}."
            ),
            bot_action="fallback заблокирован, потому что primary и fallback указывают на одну панель",
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
            notification_info_title="ℹ️ Заказ переведён на fallback",
            notification_reason=f"{primary_error} на панели #{service_number}",
            notification_action=f"заказ отправлен через fallback на панели #{fallback_service_number}",
        )
        return

    err2 = payload2.get("error", "unknown")
    detail2 = payload2.get("detail", str(payload2))
    logger.error(f"Fallback попытка тоже провалена ({err2}) | {detail2}")
    _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason=fallback_reason)

    refund_order(
        c,
        order_id_funpay,
        buyer_chat_id,
        reason=f"{primary_error} на панели #{service_number}, fallback не смог создать заказ на панели #{fallback_service_number}",
        detailed_reason=(
            f"Primary ошибка: {primary_error} | {primary_detail}. Fallback ошибка: {err2} | {detail2}."
        ),
        bot_action="автоматическое создание через fallback не удалось",
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
                "is_refunded": bool(rec.get("is_refunded", False)),
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
        ch = sum(float(o.get("chistota", 0) or 0) for o in lst if not o.get("is_refunded", False))
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
_COMPLETED = ("completed", "done", "success", "partial")
_ACTIVE = ("pending", "processing", "inprogress", "in_progress", "in progress")
_FAILED = ("failed", "error", "canceled", "cancelled")


def get_period_analytics(period: str = "24h") -> Dict:
    """Returns analytics for selected period based on created_at.

    period: 24h | 7d | 30d | all
    """
    items = _merged_orders()
    label, lst = _filter_by_period(items, period)

    total = len(lst)
    active = 0
    completed = 0
    errors = 0

    revenue = 0.0
    spent = 0.0

    for o in lst:
        st = str(o.get("status", "unknown") or "unknown").lower()
        refunded = bool(o.get("is_refunded", False))

        if refunded:
            # в UX нет отдельной метрики refunded -> считаем как "ошибка" по смыслу
            errors += 1
            continue

        if st in _COMPLETED:
            completed += 1
        elif st in _FAILED:
            errors += 1
        else:
            active += 1

        try:
            revenue += float(o.get("summa", 0) or 0)
        except Exception:
            pass
        try:
            spent += float(o.get("spent", 0) or 0)
        except Exception:
            pass

    profit = revenue - spent

    return {
        "period_norm": _period_info(period)[0],
        "period_label": label,
        "revenue": round(revenue, 2),
        "spent": round(spent, 2),
        "profit": round(profit, 2),
        "total": int(total),
        "active": int(active),
        "completed": int(completed),
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


def format_analytics(period: str = "24h", top_n: int = 5, last_n: int = 8) -> str:
    """Main text formatter for analytics screen.

    NOTE: top_n / last_n kept for backward-compatibility (ignored).
    """
    d = get_period_analytics(period=period)
    period_title = d.get("period_label", "—")

    return (
        f"📊 AutoSMM — {period_title}\n\n"
        f"💰 Финансы\n"
        f"Выручка: {_fmt_rub(d.get('revenue', 0))} ₽\n"
        f"Затраты: {_fmt_rub(d.get('spent', 0))} ₽\n"
        f"📈 Профит: {_fmt_rub(d.get('profit', 0))} ₽\n\n"
        f"📦 Заказы\n"
        f"Всего: {int(d.get('total', 0) or 0)}\n"
        f"⚙ В работе: {int(d.get('active', 0) or 0)}\n"
        f"✅ Завершено: {int(d.get('completed', 0) or 0)}\n"
        f"❌ Ошибки: {int(d.get('errors', 0) or 0)}"
    )


def _period_to_cb(period: str) -> str:
    norm, _, _ = _period_info(period)
    if norm == "7d":
        return CB_STATS_7D
    if norm == "30d":
        return CB_STATS_30D
    if norm == "all":
        return CB_STATS_ALL
    return CB_STATS_24H


def build_analytics_keyboard(period: str = "24h"):
    """Returns InlineKeyboardMarkup:

    [24ч] [7д] [30д] [∞ всё]
    [🔄 обновить]
    [⬅ назад]
    """
    try:
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton  # type: ignore
    except Exception:
        return None

    kb = InlineKeyboardMarkup(row_width=4)
    kb.row(
        InlineKeyboardButton("24ч", callback_data=CB_STATS_24H),
        InlineKeyboardButton("7д", callback_data=CB_STATS_7D),
        InlineKeyboardButton("30д", callback_data=CB_STATS_30D),
        InlineKeyboardButton("∞ всё", callback_data=CB_STATS_ALL),
    )
    kb.row(
        InlineKeyboardButton("🔄 обновить", callback_data=_period_to_cb(period)),
    )
    kb.row(
        InlineKeyboardButton("⬅ назад", callback_data=CB_BACK),
    )
    return kb


def get_analytics_text_and_kb(period: str = "24h"):
    """Convenience: returns (text, keyboard) for the analytics screen."""
    return format_analytics(period=period), build_analytics_keyboard(period=period)


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

def _render_analytics(call: types.CallbackQuery, period: str = "day"):
    # period: day | week | month | all
    try:
        txt = analytics.format_analytics(period=period, top_n=5, last_n=8)
    except Exception as e:
        logger.error(f"Ошибка формирования аналитики: {e}")
        txt = "❌ Ошибка формирования аналитики."

    kb = InlineKeyboardMarkup(row_width=4)
    kb.row(
        InlineKeyboardButton("🕒 24ч", callback_data="analytics_period_day"),
        InlineKeyboardButton("📅 7д", callback_data="analytics_period_week"),
        InlineKeyboardButton("🗓 30д", callback_data="analytics_period_month"),
        InlineKeyboardButton("♾ Всё", callback_data="analytics_period_all"),
    )
    kb.row(
        InlineKeyboardButton("🔄 Обновить", callback_data=f"analytics_period_{period}"),
        InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"),
    )

    try:
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения аналитики: {e}")
        try:
            bot.send_message(call.message.chat.id, txt, parse_mode="HTML", reply_markup=kb)
        except Exception as e2:
            logger.error(f"Ошибка при отправке сообщения аналитики: {e2}")

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def show_orders(call: types.CallbackQuery):
    _render_analytics(call, period="day")


def analytics_period(call: types.CallbackQuery):
    try:
        period = call.data.split("analytics_period_", 1)[1].strip()
    except Exception:
        period = "day"
    if period not in ("day", "week", "month", "all"):
        period = "day"
    _render_analytics(call, period=period)


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
        parts.append(f"📦 Кол-во: {real_amount}")
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

    buyer_chat_id = order_record.get("chat_id")
    order_id_funpay = str(order_record.get("order_id"))
    twiboost_order_id = order_record.get("id_zakaz")
    order_link = f"https://funpay.com/orders/{order_id_funpay}/"
    message = (
        f"👾 Ваш заказ успешно завершён!\n"
        f"🔢 Номер заказа: {twiboost_order_id}\n"
        f"🔗 Подтвердите заказ: {order_link}"
    )

    try:
        c.send_message(buyer_chat_id, message)
        orders.update_order_fields(order_id_funpay, completed_notification_sent=True)
        order_record["completed_notification_sent"] = True
        logger.info(f"Уведомление о завершении отправлено покупателю {buyer_chat_id} (заказ #{twiboost_order_id}).")
    except Exception as ex:
        logger.error(f"Не удалось отправить уведомление о завершении для заказа #{order_id_funpay}: {ex}")


# =========================================================
# LOTS UI
# =========================================================

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
        if term and term in lot_name:
            found_lots.append((lot_key, lot_data))

    kb = InlineKeyboardMarkup(row_width=1)
    if found_lots:
        for lot_key, lot_data in found_lots:
            name_ = lot_data.get("name", "Без названия")
            sid_ = lot_data.get("service_id")
            qty_ = lot_data.get("quantity")
            snum_ = lot_data.get("service_number", 1)
            btn_text = f"{name_} [ID={sid_}, Q={qty_}, S={snum_}]"
            kb.add(InlineKeyboardButton(btn_text, callback_data=f"edit_lot_search_{lot_key}"))
        kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="search_lot"))
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="lot_settings"))
        text_ = f"🔍 Результаты поиска для '{term}':"
    else:
        kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="search_lot"))
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="lot_settings"))
        text_ = f"❌ Лоты с названием '{term}' не найдены."

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
    txt_ = f"""
<b>{lot_key}</b>
Название: <code>{html.escape(str(ld_['name']))}</code>
ID услуги: <code>{ld_['service_id']}</code>
Кол-во: <code>{ld_['quantity']}</code>
S#: <code>{ld_.get('service_number', 1)}</code>
Fallback S#: <code>{ld_.get('fallback_service_number')}</code>
Fallback ID: <code>{ld_.get('fallback_service_id')}</code>
""".strip()

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("Изменить название", callback_data=f"change_name_{lot_key}"),
        InlineKeyboardButton("Изменить ID услуги", callback_data=f"change_id_{lot_key}"),
        InlineKeyboardButton("Изменить количество", callback_data=f"change_quantity_{lot_key}"),
        InlineKeyboardButton("Изменить сервис#", callback_data=f"change_snum_{lot_key}"),
        InlineKeyboardButton("Изменить Fallback S#", callback_data=f"change_fallback_snum_{lot_key}"),
        InlineKeyboardButton("Изменить Fallback ID", callback_data=f"change_fallback_id_{lot_key}"),
    )
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
        name_ = lot_data["name"]
        sid_ = lot_data["service_id"]
        qty_ = lot_data["quantity"]
        snum_ = lot_data.get("service_number", 1)
        btn_text = f"{name_} [ID={sid_}, Q={qty_}, S={snum_}]"
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
    preserved_notification_chat_id = cfg.get("notification_chat_id")
    preserved_services = cfg.get("services", {})
    
    new_config = {
        "lot_mapping": {},
        "services": preserved_services,
        "auto_refunds": cfg.get("auto_refunds", True),
        "messages": cfg.get("messages", {}),
        "notification_chat_id": preserved_notification_chat_id
    }
    
    save_config(new_config)
    bot.edit_message_text("✅ Все лоты успешно удалены. Chat ID и сервисы сохранены.", 
                         call.message.chat.id, 
                         call.message.message_id)


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
    save_config(cfg)
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
    save_config(cfg)
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
    save_config(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Количество для {lot_key} изменено на {new_q}.")


def process_service_num_change(message: types.Message, lot_key: str):
    try:
        new_snum = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: Введите номер сервиса (число).")
        return

    cfg = load_config()
    if str(new_snum) not in cfg.get("services", {}):
        _repeat_lot_edit_flow(message.chat.id, f"❌ Ошибка: Сервис #{new_snum} не существует.")
        return

    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["service_number"] = new_snum
    cfg["lot_mapping"] = lot_map
    save_config(cfg)
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
    lot_map = cfg.get("lot_mapping", {})

    new_lot_key = f"lot_{len(lot_map) + 1}"

    lot_map[new_lot_key] = {
        "name": name,
        "service_id": 1,
        "quantity": 1,
        "service_number": 1,
        "fallback_service_number": None,
        "fallback_service_id": None
    }

    cfg["lot_mapping"] = lot_map
    save_config(cfg)
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
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: Введите номер fallback сервиса (число).")
        return

    cfg = load_config()
    if str(new_snum) not in cfg.get("services", {}):
        _repeat_lot_edit_flow(message.chat.id, f"❌ Ошибка: Сервис #{new_snum} не существует.")
        return

    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["fallback_service_number"] = new_snum
    cfg["lot_mapping"] = lot_map
    save_config(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Fallback S# для {lot_key} изменён на {new_snum}.")


def process_fallback_id_change(message: types.Message, lot_key: str):
    try:
        new_id = int(message.text.strip())
    except ValueError:
        _repeat_lot_edit_flow(message.chat.id, "❌ Ошибка: ID fallback услуги должно быть числом.")
        return

    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        _clear_lot_input_state(message.chat.id)
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["fallback_service_id"] = new_id
    cfg["lot_mapping"] = lot_map
    save_config(cfg)
    _finish_lot_edit(message, lot_key, f"✅ Fallback ID для {lot_key} изменён на {new_id}.")


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
    _start_lot_edit_flow(call, lot_key, "change_snum", f"Введите номер сервиса для {lot_key}:", process_service_num_change)


def delete_one_lot_callback(call: types.CallbackQuery):
    lot_key = call.data.split("_", 3)[3]
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
# SETTINGS UI
# =========================================================

def add_website(message: types.Message, new_site: str):
    valid_links = load_valid_links()
    if new_site not in valid_links:
        valid_links.append(new_site)
        save_valid_links(valid_links)
        bot.send_message(message.chat.id, f"✅ Сайт {new_site} успешно добавлен в список.")
    else:
        bot.send_message(message.chat.id, f"❌ Сайт {new_site} уже есть в списке.")


def remove_website(message: types.Message, site_to_remove: str):
    valid_links = load_valid_links()
    if site_to_remove in valid_links:
        valid_links.remove(site_to_remove)
        save_valid_links(valid_links)
        bot.send_message(message.chat.id, f"✅ Сайт {site_to_remove} успешно удалён из списка.")
    else:
        bot.send_message(message.chat.id, f"❌ Сайт {site_to_remove} не найден в списке.")


def _send_or_edit_text(chat_id: int, text_value: str, kb: InlineKeyboardMarkup, message_id: Optional[int] = None):
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
📁 Бэкап / файлы
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🗑 Удалить старые файлы", callback_data="cleanup_old_files_prompt"),
        InlineKeyboardButton("🕒 Напомнить позже", callback_data="cleanup_old_files_later"),
    )
    return txt_, kb


def build_old_files_confirm_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    txt_ = """
⚠️ <b>Подтверждение удаления</b>

Будут удалены только старые файлы AutoSMM.
Текущая версия плагина затронута не будет.

После удаления останется только активная версия AutoSMM.
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("✅ Подтвердить удаление", callback_data="cleanup_old_files_confirm"),
        InlineKeyboardButton("◀️ Отмена", callback_data="cleanup_old_files_cancel"),
    )
    return txt_, kb


def build_old_files_removed_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    txt_ = """
✅ <b>Старые файлы AutoSMM удалены</b>

Устаревшие файлы очищены.
Теперь используется только текущая версия плагина.
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("◀️ В настройки", callback_data="return_to_settings"))
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


def build_main_menu_text_and_kb(cfg: Dict) -> Tuple[str, InlineKeyboardMarkup]:
    """Собирает текст и клавиатуру главного меню настроек AutoSMM."""
    lmap = cfg.get("lot_mapping", {})
    auto_refunds = cfg.get("auto_refunds", True)
    confirm_link = cfg.get("confirm_link", True)
    notif_chat_id = cfg.get("notification_chat_id", "Не задан")
    auto_start = cfg.get("auto_start", False)
    status_text = "АКТИВИРОВАН ✅" if RUNNING else "ОСТАНОВЛЕН ❌"

    stats = analytics.get_statistics()
    day_profit = stats.get("day_chistota", 0) if stats else 0
    try:
        day_profit_fmt = f"{float(day_profit):.2f}"
    except Exception:
        day_profit_fmt = str(day_profit)

    txt_ = f"""🚀 <b>AUTOSMM v{VERSION}</b>

━━━━━━━━━━━━━━━━━━━━━

<b>📊 Статус:</b> {status_text}
<b>📦 Лоты:</b> {len(lmap)}

<b>Автозапуск:</b> {'✅' if auto_start else '❌'}
<b>Автовозвраты:</b> {'✅' if auto_refunds else '❌'}
<b>Подтверждение ссылки:</b> {'✅' if confirm_link else '❌'}

<b>💰 Профит за 24ч:</b> {day_profit_fmt} ₽
<b>📩 Уведомления:</b> {notif_chat_id}

━━━━━━━━━━━━━━━━━━━━━
""".strip()

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🛍️ Каталог лотов", callback_data="lot_settings"),
        InlineKeyboardButton("➕ Создать новый лот", callback_data="add_new_lot")
    )

    kb.add(
        InlineKeyboardButton("🔌 Интеграция API", callback_data="api_settings"),
    )

    kb.add(
        InlineKeyboardButton("🌐 Доверенные сайты", callback_data="manage_websites"),
        InlineKeyboardButton("💬 Шаблоны сообщений", callback_data="edit_messages")
    )
    kb.add(
        InlineKeyboardButton("📊 Аналитика", callback_data="show_orders")
    )

    kb.add(
        InlineKeyboardButton("📁 Бэкап / файлы", callback_data="files_menu"),
        InlineKeyboardButton("⚙️ Тонкая настройка", callback_data="misc_settings")
    )

    kb.add(
        InlineKeyboardButton("📚 Полезные ресурсы", callback_data="links_menu")
    )

    return txt_, kb


def auto_smm_settings(message: types.Message):
    if _should_show_old_files_warning(ignore_cooldown=False):
        open_old_files_warning_screen(message.chat.id, ignore_cooldown=False)
        return
    _open_main_settings_menu(message.chat.id)


def auto_smm_delete(message: types.Message):
    """Удаляет файлы кеша AutoSMM (конфиг/заказы/валидные ссылки) и останавливает работу."""
    global RUNNING
    try:
        RUNNING = False
    except Exception:
        pass

    deleted = []
    missing = []

    for p in [CONFIG_PATH, ORDERS_PATH, ORDERS_DATA_PATH, VALID_LINKS_PATH]:
        try:
            if os.path.exists(p):
                os.remove(p)
                deleted.append(p)
            else:
                missing.append(p)
        except Exception as e:
            logger.error(f"Не удалось удалить файл {p}: {e}")

    txt = "🧹 AutoSMM: очистка выполнена.\n\n"
    if deleted:
        txt += "Удалено:\n" + "\n".join(f"• {x}" for x in deleted) + "\n\n"
    if missing:
        txt += "Не найдено:\n" + "\n".join(f"• {x}" for x in missing)

    bot.send_message(message.chat.id, txt)


def files_menu(call: types.CallbackQuery):
    waiting_for_lots_upload.discard(call.from_user.id)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    txt_ = """
<b>📁 Работа с файлами</b>

Здесь вы можете управлять файлами плагина, экспортировать данные и очищать историю заказов.
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=2)
    kb_.row(
        InlineKeyboardButton("📤 Экспорт файлов", callback_data="export_files"),
        InlineKeyboardButton("📥 Загрузить JSON", callback_data="upload_lots_json")
    )

    kb_.row(
        InlineKeyboardButton("📝 Логи ошибок", callback_data="export_errors"),
        InlineKeyboardButton("🗑 Удалить заказы", callback_data="delete_orders")
    )

    kb_.add(InlineKeyboardButton("🧹 Очистка старых файлов AutoSMM", callback_data="cleanup_old_files_manual"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


def misc_settings(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    cfg = load_config()
    auto_refunds = cfg.get("auto_refunds", True)
    confirm_link = cfg.get("confirm_link", True)
    send_auto_lots = cfg.get("send_auto_lots", False)
    send_auto_lots_interval = cfg.get("send_auto_lots_interval", 30)
    auto_start = cfg.get("auto_start", False)
    fallback_enabled = cfg.get("fallback_enabled", False)

    txt_ = f"""
<b>⚙️ Дополнительные настройки</b>

Здесь вы можете настроить дополнительные параметры работы плагина.

<b>Текущие настройки:</b>
• Автовозвраты: <code>{'Включены ✅' if auto_refunds else 'Выключены ❌'}</code>
• Подтверждение ссылки: <code>{'Включено ✅' if confirm_link else 'Выключено ❌'}</code>
• Отправка файла auto_lots.json: <code>{'Включена ✅' if send_auto_lots else 'Выключена ❌'}</code>
• Интервал отправки (минуты): <code>{send_auto_lots_interval}</code>
• Автозапуск плагина: <code>{'Включен ✅' if auto_start else 'Выключен ❌'}</code>
• Fallback: <code>{'Включен ✅' if fallback_enabled else 'Выключен ❌'}</code>
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton(f"🔄 {'Выключить' if auto_refunds else 'Включить'} автовозвраты", callback_data="toggle_auto_refunds"),
        InlineKeyboardButton(f"✅ {'Выключить' if confirm_link else 'Включить'} подтверждение ссылки", callback_data="toggle_confirm_link")
    )

    kb_.add(
        InlineKeyboardButton(f"📤 {'Выключить' if send_auto_lots else 'Включить'} отправку auto_lots.json", callback_data="toggle_send_auto_lots"),
        InlineKeyboardButton("⏱️ Изменить интервал отправки", callback_data="change_send_interval")
    )

    kb_.add(
        InlineKeyboardButton(f"🚀 {'Выключить' if auto_start else 'Включить'} автозапуск плагина", callback_data="toggle_auto_start"),
        InlineKeyboardButton("🔄 Обновить номера лотов", callback_data="update_lot_ids")
    )

    kb_.add(
        InlineKeyboardButton(f"🔁 {'Выключить' if fallback_enabled else 'Включить'} fallback", callback_data="toggle_fallback")
    )

    kb_.add(
        InlineKeyboardButton("🗑 Удалить все лоты", callback_data="delete_all_lots"),
        InlineKeyboardButton("📩 Указать Chat ID для уведомлений", callback_data="set_notification_chat_id")
    )

    kb_.add(
        InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings")
    )

    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


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


def links_menu(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    txt_ = """
<b>🔗 Полезные ссылки</b>

Нужна помощь или есть идеи по улучшению? Пиши в поддержку.
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=2)
    kb_.row(
        InlineKeyboardButton("🛠 Поддержка", url="https://t.me/w1bbex"),
        InlineKeyboardButton("🌐 Smmway", url="https://smmway.ru/ref3967025")
    )
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode=None, reply_markup=kb_)


def api_settings_menu(call):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    cfg = load_config()
    services = cfg["services"]

    text_ = """
<b>⚙️ Настройки API сервисов SMM</b>

Здесь вы можете настроить подключение к различным SMM-сервисам, 
проверить баланс или добавить новые сервисы.
"""

    for srv_num, srv_data in services.items():
        text_ += f"""
<b>📡 Сервис #{srv_num}</b>
• <b>URL:</b> <code>{html.escape(str(srv_data['api_url']))}</code>
• <b>API KEY:</b> <code>{html.escape(str(srv_data['api_key']))}</code>
"""

    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("📡 API URLs", callback_data="header_no_action"))

    api_buttons = []
    for srv_num in services:
        api_buttons.append(InlineKeyboardButton(f"Сервис #{srv_num}", callback_data=f"edit_apiurl_{srv_num}"))
    kb.add(*api_buttons)

    kb.row(InlineKeyboardButton("🔑 API Keys", callback_data="header_no_action"))

    key_buttons = []
    for srv_num in services:
        key_buttons.append(InlineKeyboardButton(f"Ключ #{srv_num}", callback_data=f"edit_apikey_{srv_num}"))
    kb.add(*key_buttons)

    kb.row(InlineKeyboardButton("💰 Балансы сервисов", callback_data="header_no_action"))

    balance_buttons = []
    for srv_num in services:
        balance_buttons.append(InlineKeyboardButton(f"Баланс #{srv_num}", callback_data=f"check_balance_{srv_num}"))
    kb.add(*balance_buttons)

    kb.row(
        InlineKeyboardButton("➕ Добавить сервис", callback_data="add_service"),
        InlineKeyboardButton("🗑 Удалить сервис", callback_data="delete_service")
    )

    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

    bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)


def process_apiurl_change(message: types.Message, service_idx: int):
    new_url = message.text.strip()

    if not new_url.startswith(("http://", "https://")):
        _repeat_ui_input_flow(message.chat.id, "❌ URL должен начинаться с http:// или https://")
        return

    cfg = load_config()
    if str(service_idx) not in cfg["services"]:
        _clear_ui_input_state(message.chat.id)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
        bot.send_message(message.chat.id, f"❌ Сервис #{service_idx} не найден в конфигурации.", reply_markup=kb_)
        return

    old_url = cfg["services"][str(service_idx)]["api_url"]
    cfg["services"][str(service_idx)]["api_url"] = new_url
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("✅ Проверить баланс", callback_data=f"check_balance_{service_idx}"),
        InlineKeyboardButton("◀️ Назад", callback_data="api_settings")
    )

    text_ = f"""
✅ <b>URL сервиса #{service_idx} успешно обновлен!</b>

• <b>Было:</b> <code>{html.escape(str(old_url))}</code>
• <b>Стало:</b> <code>{html.escape(str(new_url))}</code>

Вы можете сразу проверить баланс сервиса для проверки работоспособности.
    """.strip()

    bot.send_message(message.chat.id, text_, parse_mode="HTML", reply_markup=kb_)


def process_apikey_change(message: types.Message, service_idx: int):
    new_key = message.text.strip()

    if not new_key:
        _repeat_ui_input_flow(message.chat.id, "❌ API ключ не может быть пустым")
        return

    cfg = load_config()
    if str(service_idx) not in cfg["services"]:
        _clear_ui_input_state(message.chat.id)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
        bot.send_message(message.chat.id, f"❌ Сервис #{service_idx} не найден в конфигурации.", reply_markup=kb_)
        return

    old_key = cfg["services"][str(service_idx)]["api_key"]
    old_key_masked = f"{old_key[:4]}...{old_key[-4:]}" if len(old_key) > 8 else old_key
    new_key_masked = f"{new_key[:4]}...{new_key[-4:]}" if len(new_key) > 8 else new_key

    cfg["services"][str(service_idx)]["api_key"] = new_key
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)

    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("✅ Проверить баланс", callback_data=f"check_balance_{service_idx}"),
        InlineKeyboardButton("◀️ Назад", callback_data="api_settings")
    )

    warning_text = ""
    if len(new_key) != EXPECTED_API_KEY_LENGTH:
        warning_text = (
            "\n\n⚠️ Длина API-ключа отличается от ожидаемой (60 символов). Проверьте корректность ключа."
        )

    text_ = f"""
✅ <b>API-ключ сервиса #{service_idx} успешно обновлен!</b>

• <b>Было:</b> <code>{html.escape(str(old_key_masked))}</code>
• <b>Стало:</b> <code>{html.escape(str(new_key_masked))}</code>{warning_text}

Вы можете сразу проверить баланс сервиса для проверки работоспособности.
    """.strip()

    bot.send_message(message.chat.id, text_, parse_mode="HTML", reply_markup=kb_)


def check_balance_func(call: types.CallbackQuery, service_idx: int):
    cfg = load_config()
    s_ = cfg["services"].get(str(service_idx))
    if not s_:
        bot.edit_message_text(f"❌ Сервис {service_idx} не найден.", call.message.chat.id, call.message.message_id)
        return
        
    bot.edit_message_text(f"⏳ Проверка баланса сервиса #{service_idx}...", 
                         call.message.chat.id, call.message.message_id)
                         
    url_ = f"{s_['api_url']}?action=balance&key={s_['api_key']}"
    
    try:
        rr = requests.get(url_, timeout=10)
        rr.raise_for_status()
        d_ = rr.json()
        bal_ = d_.get("balance", "0")
        

        text_ = f"""
<b>💰 Баланс сервиса #{service_idx}</b>

• <b>Текущий баланс:</b> <code>{bal_}</code>
• <b>Сервис:</b> <code>{s_['api_url'].split('/')[2]}</code>
• <b>Время запроса:</b> <code>{datetime.now().strftime('%H:%M:%S')}</code>
        """.strip()
        
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
        
        bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, 
                             parse_mode="HTML", reply_markup=kb_)
                             
    except requests.exceptions.Timeout:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔄 Повторить", callback_data=f"check_balance_{service_idx}"))
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
        
        bot.edit_message_text(f"⚠️ Время ожидания ответа от сервиса #{service_idx} истекло.",
                             call.message.chat.id, call.message.message_id, reply_markup=kb_)
                             
    except Exception as e:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔄 Повторить", callback_data=f"check_balance_{service_idx}"))
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
        
        bot.edit_message_text(f"❌ Ошибка при запросе баланса сервиса #{service_idx}:\n<code>{str(e)[:100]}</code>", 
                             call.message.chat.id, call.message.message_id, 
                             parse_mode="HTML", reply_markup=kb_)


def change_fallback_snum_prompt(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    lot_key = call.data.split("change_fallback_snum_", 1)[1]
    _start_lot_edit_flow(
        call,
        lot_key,
        "change_fallback_snum",
        f"Введите номер fallback сервиса (S#) для {lot_key}:",
        process_fallback_snum_change,
    )


def change_fallback_id_prompt(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    lot_key = call.data.split("change_fallback_id_", 1)[1]
    _start_lot_edit_flow(
        call,
        lot_key,
        "change_fallback_id",
        f"Введите fallback ID услуги для {lot_key}:",
        process_fallback_id_change,
    )


def manage_websites(call: types.CallbackQuery):
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    valid_links = load_valid_links()
    kb_ = InlineKeyboardMarkup(row_width=1)
    for site in valid_links:
        kb_.add(InlineKeyboardButton(f"🗑 {site}", callback_data=f"delete_website_{site}"))

    kb_.add(InlineKeyboardButton("➕ Добавить сайт", callback_data="add_website"))
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

    bot.edit_message_text("Список разрешённых сайтов:", call.message.chat.id, call.message.message_id, reply_markup=kb_)


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
    site_to_remove = call.data.split("_", 2)[2]
    valid_links = load_valid_links()
    if site_to_remove in valid_links:
        valid_links.remove(site_to_remove)
        save_valid_links(valid_links)
    manage_websites(call)


def delete_all_lots_prompt(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("✅ Да, удалить", callback_data="confirm_delete_all_lots"),
        InlineKeyboardButton("◀️ Назад", callback_data="misc_settings")
    )
    bot.edit_message_text("Вы уверены, что хотите удалить все лоты?", call.message.chat.id, call.message.message_id, reply_markup=kb)


def confirm_delete_all_lots(call: types.CallbackQuery):
    cfg = load_config()
    preserved_notification_chat_id = cfg.get("notification_chat_id")
    preserved_services = cfg.get("services", {})

    new_config = {
        "lot_mapping": {},
        "services": preserved_services,
        "auto_refunds": cfg.get("auto_refunds", True),
        "messages": cfg.get("messages", {}),
        "notification_chat_id": preserved_notification_chat_id
    }

    save_config(new_config)
    _set_lot_page_context(call.message.chat.id, 0)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
    bot.edit_message_text("✅ Все лоты успешно удалены. Chat ID и сервисы сохранены.", call.message.chat.id, call.message.message_id, reply_markup=kb)


def upload_lots_json(call: types.CallbackQuery):
    user_id = call.from_user.id
    waiting_for_lots_upload.add(user_id)
    logger.info(f"Добавлен пользователь {user_id} в waiting_for_lots_upload: {waiting_for_lots_upload}")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
    bot.edit_message_text("Пришлите файл JSON (можно любым названием).", call.message.chat.id, call.message.message_id, reply_markup=kb)


def export_files(call: types.CallbackQuery):
    chat_id_ = call.message.chat.id
    files_to_send = [CONFIG_PATH, ORDERS_PATH, ORDERS_DATA_PATH]
    errors = []
    for f_ in files_to_send:
        if os.path.exists(f_):
            try:
                with open(f_, 'rb') as ff:
                    bot.send_document(chat_id_, ff, caption=f"Файл: {os.path.basename(f_)}")
            except Exception as e:
                errors.append(f"Ошибка отправки {os.path.basename(f_)}: {e}")
        else:
            errors.append(f"Файл не найден: {os.path.basename(f_)}")
    try:
        bot.answer_callback_query(call.id, "Файлы отправлены." if not errors else "Часть файлов не отправлена.")
    except Exception:
        pass
    if errors:
        bot.send_message(chat_id_, "\n".join(errors))
    files_menu(call)


def export_errors(call: types.CallbackQuery):
    chat_id_ = call.message.chat.id
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, 'rb') as f:
                bot.send_document(chat_id_, f, caption="Лог ошибок")
            try:
                bot.answer_callback_query(call.id, "Логи выгружены.")
            except Exception:
                pass
        except Exception as e:
            bot.send_message(chat_id_, f"❌ Ошибка отправки лог-файла: {e}")
    else:
        bot.send_message(chat_id_, "Лог-файл не найден.")
    files_menu(call)


def confirm_delete_orders(call: types.CallbackQuery):
    """Просит подтверждение перед удалением истории заказов."""
    kb2 = InlineKeyboardMarkup(row_width=2)
    kb2.row(
        InlineKeyboardButton("✅ Да, удалить", callback_data="delete_orders_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="files_menu")
    )
    bot.edit_message_text(
        "⚠️ <b>Вы действительно хотите удалить ВСЮ историю заказов?</b>\n\n"
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
    ar_ = cfg.get("auto_refunds", True)
    cfg["auto_refunds"] = not ar_
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)


def toggle_confirm_link(call: types.CallbackQuery):
    cfg = load_config()
    confirm_link = cfg.get("confirm_link", True)
    cfg["confirm_link"] = not confirm_link
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)


def toggle_send_auto_lots(call: types.CallbackQuery):
    cfg = load_config()
    current_value = cfg.get("send_auto_lots", True)
    cfg["send_auto_lots"] = not current_value
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)


def toggle_auto_start(call: types.CallbackQuery):
    cfg = load_config()
    current_value = cfg.get("auto_start", False)
    cfg["auto_start"] = not current_value
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)


def change_send_interval(call: types.CallbackQuery):
    cfg = load_config()
    current_interval = cfg.get("send_auto_lots_interval", 30)
    _start_ui_input_flow(
        call,
        f"Текущий интервал отправки: {current_interval} минут\n\nВведите новый интервал отправки auto_lots.json в минутах (от 5 до 1440):",
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
        bot.send_message(message.chat.id, f"✅ Интервал отправки auto_lots.json установлен: {new_interval} минут", reply_markup=kb_)
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Введите корректное число минут.")


def return_to_settings(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    waiting_for_lots_upload.discard(call.from_user.id)
    SEARCH_RESULT_CONTEXT.pop(int(call.message.chat.id), None)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)

    _open_main_settings_menu(call.message.chat.id, message_id=call.message.message_id)


def api_settings_callback(call: types.CallbackQuery):
    api_settings_menu(call)


def edit_apiurl(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    _start_ui_input_flow(
        call,
        f"Введите новый URL для сервиса #{idx_}:",
        process_apiurl_change,
        idx_,
        back_target="api_settings",
    )


def edit_apikey(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    _start_ui_input_flow(
        call,
        f"Введите новый ключ для сервиса #{idx_}:",
        process_apikey_change,
        idx_,
        back_target="api_settings",
    )


def check_balance(call: types.CallbackQuery):
    idx_ = int(call.data.split("_")[-1])
    check_balance_func(call, idx_)


def edit_messages_menu(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    try:
        _clear_step_handler(call.message.chat.id)
        _clear_navigation_states(call.message.chat.id)

        cfg = load_config()
        default_messages = {
            "after_payment": """❤️ Благодарим за оплату!

Чтобы начать накрутку, отправьте корректную ссылку на вашу страницу или пост в социальных сетях. Ссылка должна начинаться с "https://", например:

Пример: https://teletype.in/@w1bbex/wwTwed1ZBZE

Без правильной ссылки накрутка не будет запущена. Убедитесь, что она ведет на активную страницу, доступную для общего просмотра.""",
            "after_confirmation": """🎉 Ваш заказ успешно оформлен!

🔢 ID заказа в сервисе: {twiboost_id}
🔗 Для отслеживания переходите по ссылке: {link}

📋 Доступные команды:
🔍 чек {twiboost_id} — Проверить статус заказа (выводит информацию о заказе)
🔄 рефилл {twiboost_id} — Запросить рефилл (работает лишь с гарантией - восстанавливает отписанных подписчиков)

Если у вас возникнут вопросы, не стесняйтесь обращаться!"""
        }
        messages_cfg = cfg.get("messages", {})
        if not isinstance(messages_cfg, dict):
            messages_cfg = {}

        msg_payment = messages_cfg.get("after_payment") or default_messages["after_payment"]
        msg_confirmation = messages_cfg.get("after_confirmation") or default_messages["after_confirmation"]

        payment_preview = _build_template_preview(msg_payment)
        confirmation_preview = _build_template_preview(msg_confirmation)

        text_ = f"""
⚙ <b>Редактирование текстов сообщений</b>

<b>После оплаты:</b>

Переменные: https://teletype.in/@w1bbex/kT9IpmDNovR

<code>{payment_preview}</code>

<b>После подтверждения ссылки:</b>

Переменные: https://teletype.in/@w1bbex/kT9IpmDNovR

<code>{confirmation_preview}</code>
        """.strip()

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("Изменить текст после оплаты", callback_data="edit_msg_payment"),
            InlineKeyboardButton("Изменить текст после подтверждения", callback_data="edit_msg_confirmation")
        )
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))

        try:
            bot.edit_message_text(
                text_,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as edit_error:
            err_text = str(edit_error).lower()
            if "message is not modified" in err_text:
                try:
                    bot.edit_message_reply_markup(
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=kb,
                    )
                except Exception:
                    pass
                return
            raise
    except Exception:
        logger.exception("Ошибка при открытии экрана шаблонов сообщений")
        error_text = "❌ Не удалось открыть раздел шаблонов сообщений."
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("◀️ Назад", callback_data="return_to_settings"))
        try:
            bot.edit_message_text(
                error_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            try:
                bot.send_message(call.message.chat.id, error_text, reply_markup=kb)
            except Exception:
                logger.exception("Не удалось показать fallback-ошибку для экрана шаблонов сообщений")


def _build_template_preview(text_value: str, limit: int = 450) -> str:
    raw = str(text_value or "").strip()
    if len(raw) > limit:
        raw = raw[:limit].rstrip() + "…"
    return html.escape(raw) if raw else "—"


def edit_msg_payment(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Введите новый текст после оплаты:",
        process_message_payment_change,
        back_target="edit_messages",
    )


def edit_msg_confirmation(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        "Введите новый текст после подтверждения:",
        process_message_confirmation_change,
        back_target="edit_messages",
    )


def process_message_payment_change(message: types.Message):
    new_text = message.text.strip()
    if not new_text:
        _repeat_ui_input_flow(message.chat.id, "❌ Текст не может быть пустым.")
        return
    cfg = load_config()
    cfg["messages"]["after_payment"] = new_text
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="edit_messages"))
    bot.send_message(message.chat.id, "Текст после оплаты обновлен.", reply_markup=kb)


def process_message_confirmation_change(message: types.Message):
    new_text = message.text.strip()
    if not new_text:
        _repeat_ui_input_flow(message.chat.id, "❌ Текст не может быть пустым.")
        return
    cfg = load_config()
    cfg["messages"]["after_confirmation"] = new_text
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="edit_messages"))
    bot.send_message(message.chat.id, "Текст после подтверждения обновлен.", reply_markup=kb)


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
        "Введите номер сервиса для удаления:",
        process_delete_service,
        back_target="api_settings",
    )


def process_add_service(message: types.Message):
    try:
        srv_num = int(message.text.strip())
        if srv_num < 1:
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Номер сервиса должен быть положительным числом.")
        return

    cfg = load_config()
    if str(srv_num) in cfg["services"]:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{srv_num} уже существует.")
        return

    cfg["services"][str(srv_num)] = {
        "api_url": "https://example.com/api/v2",
        "api_key": "YOUR_API_KEY"
    }
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
    bot.send_message(message.chat.id, f"✅ Сервис #{srv_num} добавлен. Настройте его URL и ключ.", reply_markup=kb_)


def process_delete_service(message: types.Message):
    try:
        srv_num = int(message.text.strip())
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Номер сервиса должен быть числом.")
        return

    cfg = load_config()
    if str(srv_num) not in cfg["services"]:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{srv_num} не существует.")
        return

    del cfg["services"][str(srv_num)]
    save_config(cfg)
    _clear_ui_input_state(message.chat.id)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="api_settings"))
    bot.send_message(message.chat.id, f"✅ Сервис #{srv_num} удален.", reply_markup=kb_)


def set_notification_chat_id(call: types.CallbackQuery):
    _start_ui_input_flow(
        call,
        f"Введите Chat ID для уведомлений (например, -1001234567890 для группы или ваш ID, ваш id: {call.message.chat.id}):",
        process_notification_chat_id,
        back_target="misc_settings",
    )


def process_notification_chat_id(message: types.Message):
    try:
        new_chat_id = int(message.text.strip())
        cfg = load_config()
        cfg["notification_chat_id"] = new_chat_id
        save_config(cfg)
        _clear_ui_input_state(message.chat.id)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="misc_settings"))
        bot.send_message(message.chat.id, f"✅ Chat ID для уведомлений установлен: {new_chat_id}", reply_markup=kb_)
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Введите корректный Chat ID (целое число).")


def cleanup_old_files_manual(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    waiting_for_lots_upload.discard(call.from_user.id)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    open_old_files_warning_screen(call.message.chat.id, call.message.message_id, ignore_cooldown=True)


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

    open_old_files_warning_screen(call.message.chat.id, call.message.message_id, ignore_cooldown=True)


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

        if _sync_refund_from_funpay_message(msg_text):
            return

        m_check = re.match(r'^чек\s+(\d+)$', msg_text.lower())
        if m_check:
            order_num = m_check.group(1)
            od_ = orders.load_orders_data()
            found = next((o for o in od_ if str(o["id_zakaz"]) == order_num), None)
            if not found:
                c.send_message(msg_chat_id, "❌ Заказ не найден в базе.")
                return
            cfg = load_config()
            service_cfg = cfg["services"].get(str(found["service_number"]))
            if not service_cfg:
                c.send_message(msg_chat_id, f"❌ Не найден конфиг для service_number={found['service_number']}")
                return
            api_url = service_cfg["api_url"]
            api_key = service_cfg["api_key"]
            invalid_key_payload = _validate_api_key_for_action(found["service_number"], "status", api_key)
            if invalid_key_payload:
                c.send_message(msg_chat_id, "❌ Невозможно выполнить проверку: API-ключ сервиса имеет некорректную длину.")
                return
            url_ = f"{api_url}?action=status&order={order_num}&key={api_key}"
            try:
                rr = requests.get(url_)
                rr.raise_for_status()
                rdata = rr.json()
                st_ = rdata.get("status", "неизв.")
                rm_ = rdata.get("remains", "неизв.")
                ch_ = rdata.get("charge", "неизв.")
                cur_ = rdata.get("currency", "неизв.")
                c.send_message(msg_chat_id, f"Статус: {st_}")
            except Exception as ex:
                c.send_message(msg_chat_id, f"Ошибка при проверке")
            return

        m_refill = re.match(r'^рефилл\s+(\d+)$', msg_text.lower())
        if m_refill:
            order_num = m_refill.group(1)
            c.send_message(msg_chat_id, "Запрашиваю рефилл...")
            od_ = orders.load_orders_data()
            found = next((o for o in od_ if str(o["id_zakaz"]) == order_num), None)
            if not found:
                c.send_message(msg_chat_id, "❌ Заказ не найден в базе.")
                return
            cfg = load_config()
            service_cfg = cfg["services"].get(str(found["service_number"]))
            if not service_cfg:
                c.send_message(msg_chat_id, f"❌ Не найден конфиг для service_number={found['service_number']}")
                return
            api_url = service_cfg["api_url"]
            api_key = service_cfg["api_key"]
            url_ = f"{api_url}?action=refill&order={order_num}&key={api_key}"
            try:
                rr = requests.get(url_)
                rr.raise_for_status()
                rdata = rr.json()
                st_ = rdata.get("status", 0)
                if str(st_) in ("1", "true"):
                    c.send_message(msg_chat_id, "✅ Рефилл успешно запущен.")
                else:
                    c.send_message(msg_chat_id, f"❌ Рефилл отклонён (status={st_}).")
            except Exception as ex:
                c.send_message(msg_chat_id, f"Ошибка при запросе рефилла")
            return

        for order_id, data in waiting_for_link.items():
            if data["buyer_id"] == msg_author_id:
                if data["step"] == "await_link":
                    link_m = re.search(r'(https?://\S+)', msg_text)
                    if not link_m:
                        c.send_message(msg_chat_id, "❌ Неверная ссылка, повторите...")
                        return
                    link_ = link_m.group(0)
                    ok, reason = is_valid_link(link_)
                    if not ok:
                        c.send_message(msg_chat_id, reason)
                        return
                    data["link"] = link_
                    
                    cfg = load_config()
                    confirm_link = cfg.get("confirm_link", True)
                    
                    if confirm_link:
                        data["step"] = "await_confirm"
                        c.send_message(msg_chat_id, f"""✅ Ссылка получена: {link_}

Подтвердите действие:
➕ — да
➖ — нет""")
                        return
                    else:
                        process_link_without_confirmation(c, data)
                    return

                elif data["step"] == "await_confirm":
                    if msg_text.lower() == "+":
                        process_link_without_confirmation(c, data)
                        return
                    elif msg_text.lower() == "-":
                        data["step"] = "await_link"
                        c.send_message(msg_chat_id, "❌ Подтверждение отклонено. Введите другую ссылку.")
                        return
                    else:
                        c.send_message(msg_chat_id, "❌ Используйте + или -. Повторите.")
                        return

    elif isinstance(e, NewOrderEvent):
        order_ = e.order
        orderID = order_.id
        orderDesc = order_.description
        orderAmount = order_.amount
        orderPrice = order_.price

        logger.info(f"Новый заказ #{orderID}: {orderDesc}, x{orderAmount}")

        cfg = load_config()
        found_lot = get_tg_id_by_description(orderDesc, orderAmount)
        if found_lot is None:
            logger.info("Лот не найден по описанию. Пропуск обработки.")
            return

        service_id, real_amount, srv_number = found_lot

        od_full = c.account.get_order(orderID)
        buyer_chat_id = od_full.chat_id
        buyer_id = od_full.buyer_id
        buyer_username = od_full.buyer_username
        orders_info[orderID] = {
            "buyer_id": buyer_id,
            "chat_id": buyer_chat_id,
            "summa": orderPrice
        }

        save_order_info(orderID, orderPrice, orderDesc, 0)

        try:
            msg_payment = cfg["messages"]["after_payment"].format(
                buyer_username=buyer_username,
                orderDesc=orderDesc,
                orderPrice=orderPrice,
                orderAmount=orderAmount
            )
        except KeyError as e:
            logger.error(f"Ошибка в шаблоне сообщения: отсутствует переменная {e}")
            msg_payment = "❤️ Спасибо за оплату! Укажите ссылку для запуска заказа."
        
        c.send_message(buyer_chat_id, msg_payment)

        waiting_for_link[str(orderID)] = {
            "buyer_id": buyer_id,
            "chat_id": buyer_chat_id,
            "service_id": service_id,
            "real_amount": real_amount,
            "order_id_funpay": orderID,
            "price": orderPrice,
            "service_number": srv_number,
            "step": "await_link"
        }


def start_smm(message: types.Message):
    global RUNNING, IS_STARTED, ORDER_CHECK_THREAD, AUTO_LOTS_SEND_THREAD, cardinal_instance

    if RUNNING:
        bot.send_message(message.chat.id, "✅ Плагин уже запущен.")
        return

    RUNNING = True
    IS_STARTED = True
    
    c_ = cardinal_instance
    
    if not ORDER_CHECK_THREAD or not ORDER_CHECK_THREAD.is_alive():
        ORDER_CHECK_THREAD = threading.Thread(target=start_order_checking, args=(c_,))
        ORDER_CHECK_THREAD.daemon = True
        ORDER_CHECK_THREAD.start()
    
    try:
        cfg = load_config()
        send_auto_lots = cfg.get("send_auto_lots", False)
    except Exception:
        send_auto_lots = False

    if send_auto_lots:
        if not AUTO_LOTS_SEND_THREAD or not AUTO_LOTS_SEND_THREAD.is_alive():
            AUTO_LOTS_SEND_THREAD = threading.Thread(target=start_auto_lots_sender, args=(c_,))
            AUTO_LOTS_SEND_THREAD.daemon = True
            AUTO_LOTS_SEND_THREAD.start()
    
    bot.send_message(message.chat.id, "✅ Плагин успешно запущен.")


def stop_smm(message: types.Message):
    global RUNNING
    
    if not RUNNING:
        bot.send_message(message.chat.id, "❌ Плагин не запущен.")
        return

    RUNNING = False
    bot.send_message(message.chat.id, "✅ Плагин остановлен.")


def init_commands(c_: Cardinal):
    global bot, config, lot_mapping, cardinal_instance
    logger.info("=== init_commands() from auto_smm (2 services) ===")
    cardinal_instance = c_
    bot = c_.telegram.bot
    
    auto_started = auto_start_plugin(c_)
    if auto_started:
        logger.info("Плагин был автоматически запущен при инициализации")

    def handle_document_upload(message: types.Message):
        user_id = message.from_user.id
        logger.info(f"Получен документ от {user_id}. Проверка ожидания...")
        if user_id not in waiting_for_lots_upload:
            logger.info(f"Пользователь {user_id} не ожидает загрузки JSON")
            bot.send_message(message.chat.id, "❌ Вы не активировали загрузку JSON. Используйте меню настроек.")
            return
        waiting_for_lots_upload.remove(user_id)
        logger.info(f"Пользователь {user_id} удалён из ожидания. Обрабатываю файл...")
        file_id = message.document.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        try:
            data = json.loads(downloaded_file.decode('utf-8'))
            if "lot_mapping" not in data:
                bot.send_message(message.chat.id, "❌ Ошибка: в файле нет ключа 'lot_mapping'.")
                logger.error("JSON не содержит 'lot_mapping'")
                return
            save_config(data)
            kb_ = InlineKeyboardMarkup()
            kb_.add(InlineKeyboardButton("◀️ Назад", callback_data="files_menu"))
            bot.send_message(message.chat.id, "✅ Новый auto_lots.json успешно загружен и сохранён!", reply_markup=kb_)
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
    c_.add_telegram_commands(UUID, [
        ("start_smm", "Включить автопродажу", True),
        ("stop_smm", "Выключить автопродажу", True),
        ("auto_smm_settings", "Настройки автопродажи", True),
            ])



    # --- Telegram handlers registration (NO decorators; Cardinal-safe) ---
    def _add_cb(_fn, **_kw):
        try:
            bot.add_callback_query_handler(_fn, **_kw)
        except Exception:
            try:
                bot.register_callback_query_handler(_fn, **_kw)
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
    _add_cb(add_website_prompt, func=lambda call: call.data == "add_website")
    _add_cb(remove_website_prompt, func=lambda call: call.data.startswith("delete_website_"))
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
    _add_cb(toggle_confirm_link, func=lambda call: call.data == "toggle_confirm_link")
    _add_cb(toggle_send_auto_lots, func=lambda call: call.data == "toggle_send_auto_lots")
    _add_cb(toggle_auto_start, func=lambda call: call.data == "toggle_auto_start")
    _add_cb(change_send_interval, func=lambda call: call.data == "change_send_interval")
    _add_cb(cancel_interval_change, func=lambda call: call.data == "cancel_interval_change")
    _add_cb(return_to_settings, func=lambda call: call.data == "return_to_settings")
    _add_cb(return_to_lots, func=lambda call: call.data.startswith("return_to_lots"))
    _add_cb(change_name, func=lambda call: call.data.startswith("change_name_"))
    _add_cb(change_id, func=lambda call: call.data.startswith("change_id_"))
    _add_cb(change_quantity, func=lambda call: call.data.startswith("change_quantity_"))
    _add_cb(change_snum, func=lambda call: call.data.startswith("change_snum_"))
    _add_cb(delete_one_lot_callback, func=lambda call: call.data.startswith("delete_one_lot_"))
    _add_cb(api_settings_callback, func=lambda call: call.data == "api_settings")
    _add_cb(header_no_action_callback, func=lambda call: call.data == "header_no_action")
    _add_cb(edit_apiurl, func=lambda call: call.data.startswith("edit_apiurl_"))
    _add_cb(edit_apikey, func=lambda call: call.data.startswith("edit_apikey_"))
    _add_cb(check_balance, func=lambda call: call.data.startswith("check_balance_"))
    _add_cb(add_new_lot, func=lambda call: call.data == "add_new_lot")
    _add_cb(update_lot_ids, func=lambda call: call.data == "update_lot_ids")
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

    # Telegram commands (Cardinal)
    c_.telegram.msg_handler(start_smm, commands=["start_smm"])
    c_.telegram.msg_handler(stop_smm, commands=["stop_smm"])
    c_.telegram.msg_handler(auto_smm_settings, commands=["auto_smm_settings"])
    c_.telegram.msg_handler(auto_smm_delete, commands=["auto_smm_delete"])


def start_order_checking_if_needed(c: Cardinal):
    if RUNNING:  
        start_order_checking(c)


def start_auto_lots_sender(c: Cardinal):
    """
    Отправляет файл auto_lots.json на заданный chat_id с заданной периодичностью
    """
    global RUNNING
    
    logger.info("Запуск потока отправки auto_lots.json")
    
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
                                caption=f"📄 Автоматическая отправка файла auto_lots.json\n\n⏱️ Следующая отправка через {interval_minutes} минут"
                            )
                            logger.info(f"Файл auto_lots.json отправлен на chat_id {chat_id}")
                    except Exception as e:
                        logger.error(f"Ошибка при отправке auto_lots.json: {e}")
            
            wait_time = interval_minutes * 60
            sleep_interval = 1
            
            for _ in range(wait_time):
                if not RUNNING:
                    break
                time.sleep(sleep_interval)
                
        except Exception as e:
            logger.error(f"Ошибка в потоке отправки auto_lots.json: {e}")
            time.sleep(60)


def auto_start_plugin(c: Cardinal):
    """
    Автоматический запуск плагина на основе настройки auto_start
    """
    cfg = load_config()
    auto_start = cfg.get("auto_start", False)
    
    if auto_start:
        logger.info("Автоматический запуск плагина SMM")
        global RUNNING, IS_STARTED, ORDER_CHECK_THREAD, AUTO_LOTS_SEND_THREAD
        RUNNING = True
        IS_STARTED = True
    
        if not ORDER_CHECK_THREAD or not ORDER_CHECK_THREAD.is_alive():
            ORDER_CHECK_THREAD = threading.Thread(target=start_order_checking, args=(c,))
            ORDER_CHECK_THREAD.daemon = True
            ORDER_CHECK_THREAD.start()
        
        try:
            cfg2 = load_config()
            send_auto_lots2 = cfg2.get("send_auto_lots", False)
        except Exception:
            send_auto_lots2 = False

        if send_auto_lots2:
            if not AUTO_LOTS_SEND_THREAD or not AUTO_LOTS_SEND_THREAD.is_alive():
                AUTO_LOTS_SEND_THREAD = threading.Thread(target=start_auto_lots_sender, args=(c,))
                AUTO_LOTS_SEND_THREAD.daemon = True
                AUTO_LOTS_SEND_THREAD.start()
            
        logger.info("Плагин SMM успешно запущен автоматически")
        return True
    
    return False


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [auto_smm_handler]
BIND_TO_NEW_ORDER = [auto_smm_handler]
BIND_TO_DELETE = None
