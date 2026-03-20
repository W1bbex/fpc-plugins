from __future__ import annotations
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
if TYPE_CHECKING:
    from cardinal import Cardinal

from urllib.parse import quote
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

from datetime import datetime, timedelta

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI import enums

from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import _autosmm_orders as orders
import _autosmm_analytics as analytics

NAME = "AutoSMM"
VERSION = "0.0.5.22"
DESCRIPTION = "Плагин для автоматической накрутки через 2+ сервис"
CREDITS = "@w1bbex"
UUID = "c800e7e9-05ce-43eb-addc-4f5841f79726"
SETTINGS_PAGE = False

LOGGER_PREFIX = "[AUTO autosmm]"
logger = logging.getLogger("FPC.autosmm")

waiting_for_lots_upload = set()



VALID_LINKS_PATH = os.path.join("storage", "cache", "valid_link.json")

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

LOG_DIR = os.path.join("storage", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "auto_smm.log")
EXPECTED_API_KEY_LENGTH = 60

logger = logging.getLogger("FPC.autosmm")
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(LOG_PATH, encoding='utf-8')
file_handler.setLevel(logging.ERROR)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
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
ORDERS_PATH = os.path.join("storage", "cache", "auto_smm_orders.json")
ORDERS_DATA_PATH = os.path.join("storage", "cache", "orders_data.json")
os.makedirs(os.path.dirname(ORDERS_PATH), exist_ok=True)
os.makedirs(os.path.dirname(ORDERS_DATA_PATH), exist_ok=True)

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

Без правильной ссылки накрутка не будет запущена. Убедитесь, что она ведет на активную страницу, доступную для общего просмотра."""
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
    
    data_ = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order_id": order_id,
        "summa": order_summa,
        "service_name": service_name,
        "chistota": order_chistota,
        "completed_notification_sent": False
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

def refund_order(c: Cardinal, order_id_funpay: str, buyer_chat_id: int, reason: str, detailed_reason: str = None):
    """
    Обработка возврата средств с уведомлением клиента и получателя уведомлений.
    """
    cfg = load_config()
    auto_refunds = cfg.get("auto_refunds", True)
    notification_chat_id = cfg.get("notification_chat_id")

    if detailed_reason is None:
        detailed_reason = reason

    orders_data = orders.load_orders_data()
    order_data = next((o for o in orders_data if str(o["order_id"]) == str(order_id_funpay)), None)
    
    if order_data and order_data.get("is_refunded", False):
        logger.info(f"Заказ #{order_id_funpay} уже был возвращен. Пропуск.")
        return

    order_url = f"https://funpay.com/orders/{order_id_funpay}/"

    if auto_refunds:
        try:
            c.account.refund(order_id_funpay)
            c.send_message(buyer_chat_id, f"❌ Ваши средства возвращены по причине: {reason}")
            if notification_chat_id:
                detailed_message = f"""
⚠️ Автоматический возврат средств для заказа #{order_id_funpay}.
🔢 Номер заказа: {order_id_funpay}
📝 Причина: {detailed_reason}
🔗 Ссылка на заказ: {order_url}
                """.strip()
                bot.send_message(notification_chat_id, detailed_message)
            logger.info(f"Заказ #{order_id_funpay} был отменён (refund) для покупателя {buyer_chat_id}. Причина: {reason}")

            waiting_for_link.pop(str(order_id_funpay), None)
        except Exception as ex:
            logger.error(f"Не удалось вернуть средства для заказа #{order_id_funpay}: {ex}")
            if notification_chat_id:
                detailed_message = f"""
⚠️ Ошибка при автоматическом возврате средств для заказа #{order_id_funpay}.
🔢 Номер заказа: {order_id_funpay}
📝 Причина: {detailed_reason}
❗ Ошибка: {ex}
🔗 Ссылка на заказ: {order_url}
                """.strip()
                bot.send_message(notification_chat_id, detailed_message)
    else:
        if notification_chat_id:
            detailed_message = f"""
⚠️ Требуется ручной возврат средств для заказа #{order_id_funpay}.
🔢 Номер заказа: {order_id_funpay}
📝 Причина: {detailed_reason}
🔗 Перейдите по ссылке, чтобы отменить заказ: {order_url}
            """.strip()
            bot.send_message(notification_chat_id, detailed_message)
        else:
            logger.warning("Notification chat_id не задан для уведомления о возврате.")


def check_order_status(
    c: Cardinal,
    twiboost_order_id: int,
    buyer_chat_id: int,
    link: str,
    order_id_funpay: str,
    attempt: int = 1
):
    """
    Потоковая проверка статуса заказа на соответствующем SMM-сервисе.
    """
    file_lock = threading.Lock()
    
    try:
        with file_lock:
            if os.path.exists(ORDERS_PATH):
                try:
                    with open(ORDERS_PATH, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        if not file_content.strip():
                            orders_info = []
                        else:
                            orders_info = json.loads(file_content)
                except (json.JSONDecodeError, Exception) as e:
                    logger.error(f"Ошибка при чтении файла {ORDERS_PATH}: {e}")
                    orders_info = []
            else:
                orders_info = []
    except Exception as e:
        logger.error(f"Ошибка при доступе к файлу {ORDERS_PATH}: {e}")
        orders_info = []
    
    order_info = next((o for o in orders_info if str(o["order_id"]) == str(order_id_funpay)), None)
    if order_info and order_info.get("completed_notification_sent", False):
        logger.info(f"{LOGGER_PREFIX} Уведомление о завершении для заказа #{order_id_funpay} уже было отправлено. Пропускаем проверку.")
        return
    
    try:
        orders_data = orders.load_orders_data()
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных о заказах: {e}")
        orders_data = []
    
    order_data = next((o for o in orders_data if str(o["order_id"]) == str(order_id_funpay)), None)
    if not order_data:
        logger.warning(f"Заказ {order_id_funpay} не найден при check_order_status.")
        order_data = {"service_number": 1}

    service_number = order_data["service_number"]
    
    try:
        cfg = load_config()
        service_cfg = cfg["services"].get(str(service_number))
    except Exception as e:
        logger.error(f"Ошибка при загрузке конфигурации: {e}")
        return
    
    if not service_cfg:
        logger.warning(f"Не найден config.services[{service_number}] — прерываем проверку.")
        return

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
    if invalid_key_payload:
        delay = min(300 * (2 ** (attempt - 1)), 3600)
        logger.warning(
            f"{LOGGER_PREFIX} Пропускаю action=status для заказа #{twiboost_order_id}: {invalid_key_payload['detail']}"
        )
        logger.info(f"Повторная проверка заказа #{twiboost_order_id} через {delay} сек.")
        threading.Timer(
            delay,
            check_order_status,
            args=(c, twiboost_order_id, buyer_chat_id, link, order_id_funpay, attempt + 1)
        ).start()
        return

    url_ = f"{api_url}?action=status&order={twiboost_order_id}&key={api_key}"
    logger.info(f"{LOGGER_PREFIX} Проверка статуса заказа #{twiboost_order_id}, попытка {attempt}...")

    completed_statuses = ["completed", "done", "success", "partial"]
    failed_statuses = ["failed", "error", "canceled"]

    try:
        response = requests.get(url_, timeout=10)
        logger.debug(f"Запрос к {url_} вернул статус {response.status_code}")
        if response.status_code == 200:
            data_ = response.json()
            status_ = data_.get("status", "Unknown")
            remains_ = data_.get("remains", "Unknown")
            charge_ = data_.get("charge", "0")
            currency_ = data_.get("currency", "USD")

            logger.info(f"Ответ сервиса #{twiboost_order_id}: {data_}")
            try:
                remains_ = int(remains_)
            except ValueError:
                remains_ = None
            try:
                spent_ = float(charge_)
            except ValueError:
                spent_ = 0.0

            status_lower = status_.lower()

            if status_lower in completed_statuses or (remains_ is not None and remains_ == 0):
                try:
                    order_data["failed_status_checks"] = 0
                    orders.save_orders_data(orders_data)
                except Exception:
                    pass
                if order_info:
                    try:
                        with file_lock:
                            order_info["completed_notification_sent"] = True
                            with open(ORDERS_PATH, 'w', encoding='utf-8') as f:
                                json.dump(orders_info, f, indent=4, ensure_ascii=False)
                            logger.info(f"Успешно обновлен статус уведомления для заказа #{order_id_funpay}")
                    except Exception as e:
                        logger.error(f"Ошибка при сохранении статуса уведомления для заказа #{order_id_funpay}: {e}")

                order_link = f"https://funpay.com/orders/{order_id_funpay}/"
                message = (
                    f"👾 Ваш заказ успешно завершён!\n"
                    f"🔢 Номер заказа: {twiboost_order_id}\n"
                    f"🔗 Подтвердите заказ: {order_link}"
                )
                c.send_message(buyer_chat_id, message)
                logger.info(f"Уведомление о завершении отправлено покупателю {buyer_chat_id} (заказ #{twiboost_order_id}).")

                return

            elif status_lower in failed_statuses:
                # --- fail-safe: 3 attempts with 5s cooldown before refund ---
                try:
                    fail_cnt = int(order_data.get("failed_status_checks", 0)) + 1
                except Exception:
                    fail_cnt = 1

                order_data["failed_status_checks"] = fail_cnt
                try:
                    orders.save_orders_data(orders_data)
                except Exception as _e:
                    logger.error(f"Не удалось сохранить счётчик failed_status_checks для заказа #{order_id_funpay}: {_e}")

                if fail_cnt < 3:
                    logger.warning(
                        f"Заказ #{twiboost_order_id} имеет статус '{status_}' (попытка {fail_cnt}/3). Повторная проверка через 5 сек..."
                    )
                    threading.Timer(
                        5,
                        check_order_status,
                        args=(c, twiboost_order_id, buyer_chat_id, link, order_id_funpay, attempt + 1)
                    ).start()
                    return

                refund_order(
                    c,
                    order_id_funpay,
                    buyer_chat_id,
                    reason="Заказ не выполнен.",
                    detailed_reason=f"Заказ в сервисе имеет статус '{status_}' (3/3)."
                )
                return

            else:
                try:
                    order_data["failed_status_checks"] = 0
                    orders.save_orders_data(orders_data)
                except Exception:
                    pass
                logger.info(f"Заказ #{twiboost_order_id} в статусе '{status_}' (осталось: {remains_}).")
                delay = 300

        elif response.status_code == 429:
            logger.warning(f"Получен статус 429 (Too Many Requests) для заказа #{twiboost_order_id}.")
            delay = 3600


        else:
            logger.error(f"Ошибка при проверке заказа #{twiboost_order_id}: {response.status_code}, {response.text}")
            delay = 300

    except requests.exceptions.RequestException as req_ex:
        logger.error(f"RequestException при проверке #{twiboost_order_id}: {req_ex}")
        delay = min(300 * (2 ** (attempt - 1)), 3600)
    except Exception as ex:
        logger.error(f"Неизвестная ошибка при проверке заказа #{twiboost_order_id}: {ex}")
        delay = 300

    logger.info(f"Повторная проверка заказа #{twiboost_order_id} через {delay} сек.")
    threading.Timer(
        delay,
        check_order_status,
        args=(c, twiboost_order_id, buyer_chat_id, link, order_id_funpay, attempt + 1)
    ).start()

def start_order_checking(c: Cardinal):
    if RUNNING:  
        try:
            all_data = orders.load_orders_data()
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных о заказах в start_order_checking: {e}")
            all_data = []
        
        orders_info_data = []
        file_lock = threading.Lock()
        
        try:
            with file_lock:
                if os.path.exists(ORDERS_PATH):
                    try:
                        with open(ORDERS_PATH, 'r', encoding='utf-8') as f:
                            file_content = f.read()
                            if not file_content.strip():
                                orders_info_data = []
                            else:
                                orders_info_data = json.loads(file_content)
                    except (json.JSONDecodeError, Exception) as e:
                        logger.error(f"Ошибка при чтении файла {ORDERS_PATH} в start_order_checking: {e}")
                        orders_info_data = []
        except Exception as e:
            logger.error(f"Ошибка при доступе к файлу {ORDERS_PATH} в start_order_checking: {e}")
            orders_info_data = []
        
        for od_ in all_data:
            try:
                if od_["status"].lower() != "completed" and not od_.get("is_refunded", False):
                    order_info = next((o for o in orders_info_data if str(o["order_id"]) == str(od_["order_id"])), None)
                    if order_info and order_info.get("completed_notification_sent", False):
                        logger.info(f"{LOGGER_PREFIX} Пропуск проверки заказа #{od_['order_id']}, уведомление уже отправлено")
                        continue
                    
                    threading.Thread(
                        target=check_order_status,
                        args=(c, od_["id_zakaz"], od_["chat_id"], od_["customer_url"], od_["order_id"])
                    ).start()
                    time.sleep(0.5)
            except Exception as e:
                logger.error(f"Ошибка при обработке заказа в start_order_checking: {e}")
                continue

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

def auto_smm_handler(c: Cardinal, e, *args):
    global RUNNING, orders_info, waiting_for_link

    if False:  # activation disabled
        return

    if not RUNNING:
        return

    my_id = c.account.id
    bot_ = c.telegram.bot

    if isinstance(e, NewMessageEvent):
        if e.message.author_id == my_id:
            return

        msg_text = e.message.text.strip()
        msg_author_id = e.message.author_id
        msg_chat_id = e.message.chat_id

        logger.info(f"Новое сообщение от {e.message.author}: {msg_text}")

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

    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return

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
    
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return

    if not RUNNING:
        bot.send_message(message.chat.id, "❌ Плагин не запущен.")
        return

    RUNNING = False
    bot.send_message(message.chat.id, "✅ Плагин остановлен.")



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
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return

    cfg = load_config()
    txt_, kb = build_main_menu_text_and_kb(cfg)
    bot.send_message(message.chat.id, txt_, parse_mode="HTML", reply_markup=kb)




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
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
        
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
    
    kb_.add(InlineKeyboardButton("🔙 Вернуться в настройки", callback_data="return_to_settings"))
    
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode=None, reply_markup=kb_)

def misc_settings(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
        
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
        InlineKeyboardButton("🔙 Вернуться в настройки", callback_data="return_to_settings")
    )
    
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def toggle_fallback(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.answer_callback_query(call.id)
        return

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

    txt_ = """
<b>🔗 Полезные ссылки</b>

Нужна помощь или есть идеи по улучшению? Пиши в поддержку.
    """.strip()

    kb_ = InlineKeyboardMarkup(row_width=2)
    kb_.row(
        InlineKeyboardButton("🛠 Поддержка", url="https://t.me/w1bbex"),
        InlineKeyboardButton("🌐 Smmway", url="https://smmway.ru/ref3967025")
    )
    kb_.add(InlineKeyboardButton("🔙 Вернуться в настройки", callback_data="return_to_settings"))

    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode=None, reply_markup=kb_)



def generate_lots_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    items = list(lot_map.items())

    per_page = 10
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

    kb_.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))
    return kb_

def edit_lot(call: types.CallbackQuery, lot_key: str):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        bot.edit_message_text(f"❌ Лот {lot_key} не найден.", call.message.chat.id, call.message.message_id)
        return

    ld_ = lot_map[lot_key]
    txt_ = f"""
<b>{lot_key}</b>
Название: <code>{ld_['name']}</code>
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
    kb_.add(InlineKeyboardButton("◀️ К списку", callback_data="return_to_lots"))
    bot.edit_message_text(txt_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)

def delete_one_lot(call: types.CallbackQuery, lot_key: str):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key in lot_map:
        del lot_map[lot_key]
        cfg["lot_mapping"] = lot_map
        reindex_lots(cfg)
        bot.edit_message_text(f"✅ Лот {lot_key} удалён и лоты переиндексированы.", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(0))
    else:
        bot.edit_message_text(f"❌ Лот {lot_key} не найден.", call.message.chat.id, call.message.message_id)

def delete_all_lots_func(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
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

def process_name_change(message: types.Message, lot_key: str):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    new_name = message.text.strip()
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return
    lot_map[lot_key]["name"] = new_name
    cfg["lot_mapping"] = lot_map
    save_config(cfg)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_to_lots"))
    bot.send_message(message.chat.id, f"✅ Название лота {lot_key} изменено на {new_name}.", reply_markup=kb_)

def process_id_change(message: types.Message, lot_key: str):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_id = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: ID услуги должно быть числом.")
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return
    lot_map[lot_key]["service_id"] = new_id
    cfg["lot_mapping"] = lot_map
    save_config(cfg)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_to_lots"))
    bot.send_message(message.chat.id, f"✅ ID услуги для {lot_key} изменён на {new_id}.", reply_markup=kb_)

def process_quantity_change(message: types.Message, lot_key: str):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_q = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Количество должно быть числом.")
        return
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return
    lot_map[lot_key]["quantity"] = new_q
    cfg["lot_mapping"] = lot_map
    save_config(cfg)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_to_lots"))
    bot.send_message(message.chat.id, f"✅ Количество для {lot_key} изменено на {new_q}.", reply_markup=kb_)

def process_service_num_change(message: types.Message, lot_key: str):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_snum = int(message.text.strip())
        cfg = load_config()
        if str(new_snum) not in cfg["services"]:
            bot.send_message(message.chat.id, f"❌ Ошибка: Сервис #{new_snum} не существует.")
            return
        lot_map = cfg.get("lot_mapping", {})
        if lot_key not in lot_map:
            bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
            return
        lot_map[lot_key]["service_number"] = new_snum
        cfg["lot_mapping"] = lot_map
        save_config(cfg)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_to_lots"))
        bot.send_message(message.chat.id, f"✅ Номер сервиса для {lot_key} изменён на {new_snum}.", reply_markup=kb_)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Введите номер сервиса (число).")

def process_new_lot_id_step(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        lot_id = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: ID лота должно быть числом.")
        return

    try:
        lot_fields = cardinal_instance.account.get_lot_fields(lot_id)
        fields = lot_fields.fields
        name = fields.get("fields[summary][ru]", "Без названия")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Не удалось получить данные лота: {e}")
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

    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("🔙 К настройкам", callback_data="return_to_settings"))
    bot.send_message(message.chat.id, f"✅ Добавлен новый лот {new_lot_key} с названием: {name}", reply_markup=kb_)



def process_fallback_snum_change(message: types.Message, lot_key: str):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_snum = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Введите номер fallback сервиса (число).")
        return

    cfg = load_config()
    if str(new_snum) not in cfg.get("services", {}):
        bot.send_message(message.chat.id, f"❌ Ошибка: Сервис #{new_snum} не существует.")
        return

    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["fallback_service_number"] = new_snum
    cfg["lot_mapping"] = lot_map
    save_config(cfg)

    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_to_lots"))
    bot.send_message(message.chat.id, f"✅ Fallback S# для {lot_key} изменён на {new_snum}.", reply_markup=kb_)


def process_fallback_id_change(message: types.Message, lot_key: str):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_id = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: ID fallback услуги должно быть числом.")
        return

    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    if lot_key not in lot_map:
        bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
        return

    lot_map[lot_key]["fallback_service_id"] = new_id
    cfg["lot_mapping"] = lot_map
    save_config(cfg)

    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_to_lots"))
    bot.send_message(message.chat.id, f"✅ Fallback ID для {lot_key} изменён на {new_id}.", reply_markup=kb_)

def api_settings_menu(call):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
        
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
• <b>URL:</b> <code>{srv_data['api_url']}</code>
• <b>API KEY:</b> <code>{srv_data['api_key']}</code>
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
    
    kb.add(InlineKeyboardButton("🔙 Вернуться в настройки", callback_data="return_to_settings"))

    bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)

def process_apiurl_change(message: types.Message, service_idx: int):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
        
    new_url = message.text.strip()
    
    if not new_url.startswith(("http://", "https://")):
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        bot.send_message(message.chat.id, "❌ URL должен начинаться с http:// или https://", reply_markup=kb_)
        return
        
    cfg = load_config()
    if str(service_idx) not in cfg["services"]:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        bot.send_message(message.chat.id, f"❌ Сервис #{service_idx} не найден в конфигурации.", reply_markup=kb_)
        return
        
    old_url = cfg["services"][str(service_idx)]["api_url"]
    cfg["services"][str(service_idx)]["api_url"] = new_url
    save_config(cfg)
    
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("✅ Проверить баланс", callback_data=f"check_balance_{service_idx}"),
        InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings")
    )
    
    text_ = f"""
✅ <b>URL сервиса #{service_idx} успешно обновлен!</b>

• <b>Было:</b> <code>{old_url}</code>
• <b>Стало:</b> <code>{new_url}</code>

Вы можете сразу проверить баланс сервиса для проверки работоспособности.
    """.strip()
    
    bot.send_message(message.chat.id, text_, parse_mode="HTML", reply_markup=kb_)

def process_apikey_change(message: types.Message, service_idx: int):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
        
    new_key = message.text.strip()
    
    if not new_key:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        bot.send_message(message.chat.id, "❌ API ключ не может быть пустым", reply_markup=kb_)
        return
        
    cfg = load_config()
    if str(service_idx) not in cfg["services"]:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        bot.send_message(message.chat.id, f"❌ Сервис #{service_idx} не найден в конфигурации.", reply_markup=kb_)
        return
    
    old_key = cfg["services"][str(service_idx)]["api_key"]
    old_key_masked = f"{old_key[:4]}...{old_key[-4:]}" if len(old_key) > 8 else old_key
    new_key_masked = f"{new_key[:4]}...{new_key[-4:]}" if len(new_key) > 8 else new_key
    
    cfg["services"][str(service_idx)]["api_key"] = new_key
    save_config(cfg)
    
    kb_ = InlineKeyboardMarkup(row_width=1)
    kb_.add(
        InlineKeyboardButton("✅ Проверить баланс", callback_data=f"check_balance_{service_idx}"),
        InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings")
    )
    
    warning_text = ""
    if len(new_key) != EXPECTED_API_KEY_LENGTH:
        warning_text = (
            "\n\n⚠️ Длина API-ключа отличается от ожидаемой (60 символов). Проверьте корректность ключа."
        )

    text_ = f"""
✅ <b>API-ключ сервиса #{service_idx} успешно обновлен!</b>

• <b>Было:</b> <code>{old_key_masked}</code>
• <b>Стало:</b> <code>{new_key_masked}</code>{warning_text}

Вы можете сразу проверить баланс сервиса для проверки работоспособности.
    """.strip()
    
    bot.send_message(message.chat.id, text_, parse_mode="HTML", reply_markup=kb_)

def check_balance_func(call: types.CallbackQuery, service_idx: int):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
        
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
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        
        bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, 
                             parse_mode="HTML", reply_markup=kb_)
                             
    except requests.exceptions.Timeout:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔄 Повторить", callback_data=f"check_balance_{service_idx}"))
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        
        bot.edit_message_text(f"⚠️ Время ожидания ответа от сервиса #{service_idx} истекло.",
                             call.message.chat.id, call.message.message_id, reply_markup=kb_)
                             
    except Exception as e:
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔄 Повторить", callback_data=f"check_balance_{service_idx}"))
        kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
        
        bot.edit_message_text(f"❌ Ошибка при запросе баланса сервиса #{service_idx}:\n<code>{str(e)[:100]}</code>", 
                             call.message.chat.id, call.message.message_id, 
                             parse_mode="HTML", reply_markup=kb_)

def init_commands(c_: Cardinal):
    global bot, config, lot_mapping, cardinal_instance
    logger.info("=== init_commands() from auto_smm (2 services) ===")
    cardinal_instance = c_
    bot = c_.telegram.bot
    
    auto_started = auto_start_plugin(c_)
    if auto_started:
        logger.info("Плагин был автоматически запущен при инициализации")

    def handle_document_upload(message: types.Message):
        if False:  # activation disabled
            bot.send_message(message.chat.id, f"❌  ")
            return
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
            kb_.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))
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
    _add_cb(edit_lot_callback, func=lambda call: call.data.startswith("edit_lot_"))
    _add_cb(page_navigation, func=lambda call: call.data.startswith("prev_page_") or call.data.startswith("next_page_"))
    _add_cb(show_orders, func=lambda call: call.data == "show_orders")
    _add_cb(analytics_period, func=lambda call: call.data.startswith("analytics_period_"))
    _add_cb(upload_lots_json, func=lambda call: call.data == "upload_lots_json")
    _add_cb(export_files, func=lambda call: call.data == "export_files")
    _add_cb(export_errors, func=lambda call: call.data == "export_errors")
    _add_cb(confirm_delete_orders, func=lambda call: call.data == "delete_orders")
    _add_cb(delete_orders_yes, func=lambda call: call.data == "delete_orders_yes")
    _add_cb(toggle_auto_refunds, func=lambda call: call.data == "toggle_auto_refunds")
    _add_cb(toggle_confirm_link, func=lambda call: call.data == "toggle_confirm_link")
    _add_cb(toggle_send_auto_lots, func=lambda call: call.data == "toggle_send_auto_lots")
    _add_cb(toggle_auto_start, func=lambda call: call.data == "toggle_auto_start")
    _add_cb(change_send_interval, func=lambda call: call.data == "change_send_interval")
    _add_cb(cancel_interval_change, func=lambda call: call.data == "cancel_interval_change")
    _add_cb(return_to_settings, func=lambda call: call.data == "return_to_settings")
    _add_cb(return_to_lots, func=lambda call: call.data == "return_to_lots")
    _add_cb(change_name, func=lambda call: call.data.startswith("change_name_"))
    _add_cb(change_id, func=lambda call: call.data.startswith("change_id_"))
    _add_cb(change_quantity, func=lambda call: call.data.startswith("change_quantity_"))
    _add_cb(change_snum, func=lambda call: call.data.startswith("change_snum_"))
    _add_cb(delete_one_lot_callback, func=lambda call: call.data.startswith("delete_one_lot_"))
    _add_cb(api_settings_callback, func=lambda call: call.data == "api_settings")
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

def change_fallback_snum_prompt(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    lot_key = call.data.split("change_fallback_snum_", 1)[1]
    msg_ = bot.edit_message_text(
        f"Введите номер fallback сервиса (S#) для {lot_key}:",
        call.message.chat.id,
        call.message.message_id
    )
    bot.register_next_step_handler(msg_, process_fallback_snum_change, lot_key)



def change_fallback_id_prompt(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    lot_key = call.data.split("change_fallback_id_", 1)[1]
    msg_ = bot.edit_message_text(
        f"Введите fallback ID услуги для {lot_key}:",
        call.message.chat.id,
        call.message.message_id
    )
    bot.register_next_step_handler(msg_, process_fallback_id_change, lot_key)


def manage_websites(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    valid_links = load_valid_links()
    if valid_links:
        kb_ = InlineKeyboardMarkup(row_width=2)
        for site in valid_links:
            kb_.add(
                InlineKeyboardButton(site, callback_data=f"delete_website_{site}"),
                InlineKeyboardButton("Удалить", callback_data=f"delete_website_{site}")
            )
    else:
        kb_ = InlineKeyboardMarkup(row_width=1)
    
    kb_.add(InlineKeyboardButton("➕ Добавить сайт", callback_data="add_website"))
    kb_.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))

    bot.edit_message_text("Список разрешённых сайтов:", call.message.chat.id, call.message.message_id, reply_markup=kb_)

def add_website_prompt(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    msg_ = bot.edit_message_text("Введите ссылку для добавления (например, example.com):", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_add_website)

def process_add_website(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    new_site = message.text.strip()
    add_website(message, new_site)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("🔙 Вернуться в настройки", callback_data="return_to_settings"))
    bot.send_message(message.chat.id, "Вернитесь в настройки для продолжения.", reply_markup=kb_)

def remove_website_prompt(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    site_to_remove = call.data.split("_", 2)[2]
    valid_links = load_valid_links()
    if site_to_remove in valid_links:
        valid_links.remove(site_to_remove)
        save_valid_links(valid_links)
        bot.edit_message_text(f"✅ Сайт {site_to_remove} удалён из списка.", call.message.chat.id, call.message.message_id)
    else:
        bot.edit_message_text(f"❌ Сайт {site_to_remove} не найден в списке.", call.message.chat.id, call.message.message_id)
    manage_websites(call)

def delete_all_lots_prompt(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("Да, удалить", callback_data="confirm_delete_all_lots"),
        InlineKeyboardButton("Нет, отменить", callback_data="return_to_settings")
    )
    bot.edit_message_text("Вы уверены, что хотите удалить все лоты?", call.message.chat.id, call.message.message_id, reply_markup=kb)

def confirm_delete_all_lots(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    delete_all_lots_func(call)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))
    bot.edit_message_text("Все лоты удалены.", call.message.chat.id, call.message.message_id, reply_markup=kb)

def lot_settings(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔍 Поиск лота", callback_data="search_lot"))
    kb.add(InlineKeyboardButton("📋 Список лотов", callback_data="show_lots_list"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))
    bot.edit_message_text("Управление лотами:", call.message.chat.id, call.message.message_id, reply_markup=kb)

def show_lots_list(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("Выберите лот:", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(0))

def search_lot_prompt(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="lot_settings"))
    msg = bot.edit_message_text("Введите название или часть названия лота для поиска:", 
                                call.message.chat.id, call.message.message_id, reply_markup=kb)
    bot.register_next_step_handler(msg, process_lot_search)

def process_lot_search(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    search_term = message.text.strip().lower()
    if not search_term:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 К настройкам лотов", callback_data="lot_settings"))
        bot.send_message(message.chat.id, "❌ Поисковый запрос не может быть пустым.", reply_markup=kb)
        return
        
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    
    found_lots = {}
    for lot_key, lot_data in lot_map.items():
        lot_name = lot_data["name"].lower()
        if search_term in lot_name:
            found_lots[lot_key] = lot_data
            
    if not found_lots:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="search_lot"))
        kb.add(InlineKeyboardButton("🔙 К настройкам лотов", callback_data="lot_settings"))
        bot.send_message(message.chat.id, f"❌ Лоты с названием '{search_term}' не найдены.", reply_markup=kb)
        return
        
    kb = InlineKeyboardMarkup(row_width=1)
    for lot_key, lot_data in found_lots.items():
        name_ = lot_data["name"]
        sid_ = lot_data["service_id"]
        qty_ = lot_data["quantity"]
        snum_ = lot_data.get("service_number", 1)
        btn_text = f"{name_} [ID={sid_}, Q={qty_}, S={snum_}]"
        cd_ = f"edit_lot_{lot_key}"
        kb.add(InlineKeyboardButton(btn_text, callback_data=cd_))
        
    kb.add(InlineKeyboardButton("🔍 Новый поиск", callback_data="search_lot"))
    kb.add(InlineKeyboardButton("🔙 К настройкам лотов", callback_data="lot_settings"))
    bot.send_message(message.chat.id, f"🔍 Результаты поиска для '{search_term}':", reply_markup=kb)

def edit_lot_callback(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    lot_key = call.data.split("_", 2)[2]
    edit_lot(call, lot_key)

def page_navigation(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    try:
        page_ = int(call.data.split("_")[-1])
    except ValueError:
        page_ = 0
    bot.edit_message_text("Выберите лот:", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(page_))

def _render_analytics(call: types.CallbackQuery, period: str = "day"):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return

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
        InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"),
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


def upload_lots_json(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    user_id = call.from_user.id
    waiting_for_lots_upload.add(user_id)
    logger.info(f"Добавлен пользователь {user_id} в waiting_for_lots_upload: {waiting_for_lots_upload}")
    bot.edit_message_text("Пришлите файл JSON (можно любым названием).", call.message.chat.id, call.message.message_id)

def export_files(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    chat_id_ = call.message.chat.id
    files_to_send = [CONFIG_PATH, ORDERS_PATH, ORDERS_DATA_PATH]
    for f_ in files_to_send:
        if os.path.exists(f_):
            try:
                with open(f_, 'rb') as ff:
                    bot.send_document(chat_id_, ff, caption=f"Файл: {os.path.basename(f_)}")
            except Exception as e:
                bot.edit_message_text(f"Ошибка отправки {f_}: {e}", chat_id_, call.message.message_id)
        else:
            bot.edit_message_text(f"Файл не найден: {f_}", chat_id_, call.message.message_id)

def export_errors(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    chat_id_ = call.message.chat.id
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, 'rb') as f:
                bot.send_document(chat_id_, f, caption="Лог ошибок")
            bot.edit_message_text("Логи выгружены.", chat_id_, call.message.message_id)
        except Exception as e:
            bot.edit_message_text(f"Ошибка отправки лог-файла: {e}", chat_id_, call.message.message_id)
    else:
        bot.edit_message_text("Лог-файл не найден.", chat_id_, call.message.message_id)

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
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    ar_ = cfg.get("auto_refunds", True)
    cfg["auto_refunds"] = not ar_
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)

def toggle_confirm_link(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    confirm_link = cfg.get("confirm_link", True)
    cfg["confirm_link"] = not confirm_link
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)

def toggle_send_auto_lots(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                          call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    current_value = cfg.get("send_auto_lots", True)
    cfg["send_auto_lots"] = not current_value
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)
    
def toggle_auto_start(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                          call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    current_value = cfg.get("auto_start", False)
    cfg["auto_start"] = not current_value
    save_config(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)

def change_send_interval(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                            call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    current_interval = cfg.get("send_auto_lots_interval", 30)
    
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("🔙 Вернуться назад", callback_data="cancel_interval_change"))
    
    msg_ = bot.edit_message_text(f"Текущий интервал отправки: {current_interval} минут\n\nВведите новый интервал отправки auto_lots.json в минутах (от 5 до 1440):", 
                            call.message.chat.id, call.message.message_id, reply_markup=kb_)
    bot.register_next_step_handler(msg_, process_send_interval_change)

def cancel_interval_change(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                            call.message.chat.id, call.message.message_id)
        return
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    misc_settings(call)

def process_send_interval_change(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_interval = int(message.text.strip())
        if new_interval < 5:
            bot.send_message(message.chat.id, "❌ Интервал не может быть меньше 5 минут")
            return
        if new_interval > 1440:
            bot.send_message(message.chat.id, "❌ Интервал не может быть больше 1440 минут (24 часа)")
            return
            
        cfg = load_config()
        cfg["send_auto_lots_interval"] = new_interval
        save_config(cfg)
        
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔙 К настройкам", callback_data="misc_settings"))
        bot.send_message(message.chat.id, f"✅ Интервал отправки auto_lots.json установлен: {new_interval} минут", reply_markup=kb_)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Введите корректное число минут.")

        
        
def return_to_settings(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    if False:  # activation disabled
        bot.edit_message_text(
            f"❌  ",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    cfg = load_config()
    txt_, kb = build_main_menu_text_and_kb(cfg)
    bot.edit_message_text(
        txt_,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=kb
    )



def return_to_lots(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("Выберите лот:", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(0))

def change_name(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    lot_key = call.data.split("_", 2)[2]
    msg_ = bot.edit_message_text(f"Введите новое название для {lot_key}:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_name_change, lot_key)

def change_id(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    lot_key = call.data.split("_", 2)[2]
    msg_ = bot.edit_message_text(f"Введите новый ID услуги для {lot_key}:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_id_change, lot_key)

def change_quantity(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    lot_key = call.data.split("_", 2)[2]
    msg_ = bot.edit_message_text(f"Введите новое количество для {lot_key}:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_quantity_change, lot_key)

def change_snum(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    lot_key = call.data.split("_", 2)[2]
    msg_ = bot.edit_message_text(f"Введите номер сервиса для {lot_key}:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_service_num_change, lot_key)

def delete_one_lot_callback(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    lot_key = call.data.split("_", 3)[3]
    delete_one_lot(call, lot_key)

def api_settings_callback(call: types.CallbackQuery):
    api_settings_menu(call)

def edit_apiurl(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    idx_ = int(call.data.split("_")[-1])
    msg_ = bot.edit_message_text(f"Введите новый URL для сервиса #{idx_}:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_apiurl_change, idx_)

def edit_apikey(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    idx_ = int(call.data.split("_")[-1])
    msg_ = bot.edit_message_text(f"Введите новый ключ для сервиса #{idx_}:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_apikey_change, idx_)

def check_balance(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    idx_ = int(call.data.split("_")[-1])
    check_balance_func(call, idx_)

def add_new_lot(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    bot.delete_message(call.message.chat.id, call.message.message_id)
    msg_ = bot.send_message(call.message.chat.id, "Введите ID лота для добавления:")
    bot.register_next_step_handler(msg_, process_new_lot_id_step)

def update_lot_ids(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    reindex_lots(cfg)
    bot.answer_callback_query(call.id)
    misc_settings(call)

def edit_messages_menu(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    cfg = load_config()
    msg_payment = cfg["messages"]["after_payment"]
    msg_confirmation = cfg["messages"]["after_confirmation"]

    text_ = f"""
⚙ <b>Редактирование текстов сообщений</b>

<b>После оплаты:</b>

Переменные: https://teletype.in/@w1bbex/kT9IpmDNovR

<code>{msg_payment}</code>

<b>После подтверждения ссылки:</b>

Переменные: https://teletype.in/@w1bbex/kT9IpmDNovR

<code>{msg_confirmation}</code>
    """.strip()

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Изменить текст после оплаты", callback_data="edit_msg_payment"),
        InlineKeyboardButton("Изменить текст после подтверждения", callback_data="edit_msg_confirmation")
    )
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))

    bot.edit_message_text(text_, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)

def edit_msg_payment(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    msg_ = bot.edit_message_text("Введите новый текст после оплаты:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_message_payment_change)

def edit_msg_confirmation(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    msg_ = bot.edit_message_text("Введите новый текст после подтверждения:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_message_confirmation_change)

def process_message_payment_change(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    new_text = message.text.strip()
    cfg = load_config()
    cfg["messages"]["after_payment"] = new_text
    save_config(cfg)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))
    bot.send_message(message.chat.id, "Текст после оплаты обновлен.", reply_markup=kb)

def process_message_confirmation_change(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    new_text = message.text.strip()
    cfg = load_config()
    cfg["messages"]["after_confirmation"] = new_text
    save_config(cfg)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="return_to_settings"))
    bot.send_message(message.chat.id, "Текст после подтверждения обновлен.", reply_markup=kb)

def add_service(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    msg_ = bot.edit_message_text("Введите номер нового сервиса (число):", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_add_service)

def delete_service(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    msg_ = bot.edit_message_text("Введите номер сервиса для удаления:", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_delete_service)

def process_add_service(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        srv_num = int(message.text.strip())
        if srv_num < 1:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Номер сервиса должен быть положительным числом.")
        return

    cfg = load_config()
    if str(srv_num) in cfg["services"]:
        bot.send_message(message.chat.id, f"❌ Сервис #{srv_num} уже существует.")
        return

    cfg["services"][str(srv_num)] = {
        "api_url": "https://example.com/api/v2",
        "api_key": "YOUR_API_KEY"
    }
    save_config(cfg)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
    bot.send_message(message.chat.id, f"✅ Сервис #{srv_num} добавлен. Настройте его URL и ключ.", reply_markup=kb_)

def process_delete_service(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        srv_num = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Номер сервиса должен быть числом.")
        return

    cfg = load_config()
    if str(srv_num) not in cfg["services"]:
        bot.send_message(message.chat.id, f"❌ Сервис #{srv_num} не существует.")
        return

    del cfg["services"][str(srv_num)]
    save_config(cfg)
    kb_ = InlineKeyboardMarkup()
    kb_.add(InlineKeyboardButton("🔙 К настройкам API", callback_data="api_settings"))
    bot.send_message(message.chat.id, f"✅ Сервис #{srv_num} удален.", reply_markup=kb_)

def set_notification_chat_id(call: types.CallbackQuery):
    if False:  # activation disabled
        bot.edit_message_text(f"❌  ", 
                              call.message.chat.id, call.message.message_id)
        return
    msg_ = bot.edit_message_text(f"Введите Chat ID для уведомлений (например, -1001234567890 для группы или ваш ID, ваш id: {call.message.chat.id}):", 
                                call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(msg_, process_notification_chat_id)

def process_notification_chat_id(message: types.Message):
    if False:  # activation disabled
        bot.send_message(message.chat.id, f"❌  ")
        return
    try:
        new_chat_id = int(message.text.strip())
        cfg = load_config()
        cfg["notification_chat_id"] = new_chat_id
        save_config(cfg)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("🔙 К настройкам", callback_data="return_to_settings"))
        bot.send_message(message.chat.id, f"✅ Chat ID для уведомлений установлен: {new_chat_id}", reply_markup=kb_)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Введите корректный Chat ID (целое число).")

def files_menu_callback(call: types.CallbackQuery):
    files_menu(call)

def misc_settings_callback(call: types.CallbackQuery):
    misc_settings(call)

def links_menu_callback(call: types.CallbackQuery):
    links_menu(call)


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [auto_smm_handler]
BIND_TO_NEW_ORDER = [auto_smm_handler]
BIND_TO_DELETE = None

def start_order_checking_if_needed(c: Cardinal):
    if RUNNING:  
        start_order_checking(c)

def send_order_started_notification(c: Cardinal, order_id_funpay: str, twiboost_id: int, link: str, api_url: str, api_key: str, lot_price: float, real_amount: int, service_number: Optional[int] = None):
    """
    Отправляет уведомление о начале заказа с информацией о чистой прибыли
    """
    cfg = load_config()
    notification_chat_id = cfg.get("notification_chat_id")
    if not notification_chat_id:
        logger.warning("Notification chat_id не задан в конфигурации.")
        return
        
    try:
        if service_number is not None:
            invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
            if invalid_key_payload:
                logger.warning(
                    f"{LOGGER_PREFIX} Пропускаю action=status для уведомления о старте заказа #{order_id_funpay}: {invalid_key_payload['detail']}"
                )
                return

        status_url = f"{api_url}?action=status&order={twiboost_id}&key={api_key}"
        status_resp = requests.get(status_url, timeout=10)
        status_resp.raise_for_status()
        status_data = status_resp.json()
        
        charge = float(status_data.get("charge", "0"))
        currency = status_data.get("currency", "USD")
        
        net_profit = round(lot_price - charge, 2)

        # Сохраняем фактические траты/прибыль в файлы статистики
        try:
            analytics.update_order_charge_and_net(order_id_funpay, charge, currency=currency, net_profit=net_profit)
        except Exception:
            pass

        
        order_data = c.account.get_order(order_id_funpay)
        buyer_username = order_data.buyer_username
        
        kb_ = InlineKeyboardMarkup()
        order_url = f"https://funpay.com/orders/{order_id_funpay}/"
        kb_.add(InlineKeyboardButton("Перейти к заказу", url=order_url))
        
        notification_text = (
            f"🚀 AUTOSMM | Старт заказа\n\n"
            f"Заказ: #{order_id_funpay}\n"
            f"Покупатель: {buyer_username}\n\n"
            f"💰 FunPay: {lot_price} ₽\n"
            f"💸 Сайт: {charge} {currency}\n"
            f"📈 Профит: {net_profit} ₽\n\n"
            f"📦 Кол-во: {real_amount}\n"
            f"🔗 {link}"
        )
        c.telegram.bot.send_message(
            notification_chat_id,
            notification_text,
            reply_markup=kb_
        )
        
    except Exception as ex:
        logger.error(f"Ошибка при отправке уведомления о начале заказа: {ex}")



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
    - payload: dict (сырой ответ API или данные об ошибке)
    """
    cfg = load_config()
    service_cfg = cfg["services"].get(str(service_number))
    if not service_cfg:
        return False, None, {"error": "config", "detail": f"Нет настроек для service_number={service_number}"}

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    invalid_key_payload = _validate_api_key_for_action(service_number, "add", api_key)
    if invalid_key_payload:
        return False, None, invalid_key_payload

    encoded_link = quote(link, safe="")
    url_req = f"{api_url}?action=add&service={service_id}&link={encoded_link}&quantity={quantity}&key={api_key}"

    try:
        resp_ = requests.get(url_req, timeout=api_timeout)
        try:
            j_ = resp_.json()
        except Exception:
            j_ = {"raw_text": resp_.text}

        if resp_.status_code == 429:
            return False, None, {"error": "429", "detail": "Too Many Requests", "response": j_}

        resp_.raise_for_status()

        if isinstance(j_, dict) and "order" in j_:
            try:
                return True, int(j_["order"]), j_
            except Exception:
                return False, None, {"error": "bad_order_id", "detail": f"Не удалось распарсить order из ответа: {j_}", "response": j_}

        return False, None, {"error": "no_order", "detail": f"Нет 'order' в ответе API: {j_}", "response": j_}

    except requests.exceptions.Timeout as ex:
        return False, None, {"error": "timeout", "detail": str(ex)}
    except requests.exceptions.ConnectionError as ex:
        return False, None, {"error": "network_error", "detail": str(ex)}
    except requests.exceptions.RequestException as ex:
        return False, None, {"error": "network_error", "detail": str(ex)}
    except Exception as ex:
        return False, None, {"error": "unknown", "detail": str(ex)}


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


def _get_lot_fallback_targets(cfg: Dict, service_id: int, service_number: int) -> Tuple[Optional[int], Optional[int]]:
    lot_mapping_local = cfg.get("lot_mapping", {})
    for _, lot_data in lot_mapping_local.items():
        if lot_data.get("service_id") == service_id and lot_data.get("service_number") == service_number:
            return lot_data.get("fallback_service_id"), lot_data.get("fallback_service_number")
    return None, None


def _detect_fallback_reason(err: str, payload: Dict) -> str:
    err_l = str(err or "").lower()
    detail_l = str(payload.get("detail", "") or "").lower()
    response_l = str(payload.get("response", "") or "").lower()
    haystack = f"{err_l} {detail_l} {response_l}"

    if err_l == "timeout":
        return "timeout"
    if err_l == "network_error":
        return "network_error"
    if err_l == "invalid_api_key":
        return "invalid_api_key"
    if "balance" in haystack or "insufficient funds" in haystack or "not enough funds" in haystack or "no balance" in haystack:
        return "no_balance"
    return "api_error"


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


def _probe_order_after_uncertain_error(
    service_number: int,
    service_id: int,
    link: str,
    quantity: int,
    delay_seconds: int = 3,
) -> Tuple[str, Optional[int], Dict]:
    time.sleep(delay_seconds)

    cfg = load_config()
    service_cfg = cfg["services"].get(str(service_number))
    if not service_cfg:
        return "unknown", None, {"error": "config", "detail": f"Нет настроек для service_number={service_number}"}

    api_url = service_cfg["api_url"]
    api_key = service_cfg["api_key"]

    invalid_key_payload = _validate_api_key_for_action(service_number, "orders", api_key)
    if invalid_key_payload:
        return "invalid_api_key", None, invalid_key_payload

    normalized_link = _normalize_url(link)
    lookup_urls = [
        f"{api_url}?action=orders&key={api_key}",
        f"{api_url}?action=orders&limit=100&key={api_key}",
    ]

    had_successful_lookup = False
    lookup_errors: List[str] = []

    for lookup_url in lookup_urls:
        try:
            response = requests.get(lookup_url, timeout=15)
            if response.status_code != 200:
                lookup_errors.append(f"{lookup_url} -> HTTP {response.status_code}")
                continue

            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text}

            had_successful_lookup = True
            candidates: List[Tuple[int, int, Dict]] = []
            for item in _iter_dicts(payload):
                order_id = _extract_order_id_from_obj(item)
                if order_id is None:
                    continue

                service_match = False
                for key in ("service", "service_id", "serviceid"):
                    item_service = _safe_int(item.get(key))
                    if item_service is not None and item_service == int(service_id):
                        service_match = True
                        break

                link_match = False
                for key in ("link", "url", "target", "customer_url", "username"):
                    if key in item and _normalize_url(item.get(key)) == normalized_link:
                        link_match = True
                        break

                qty_match = False
                for key in ("quantity", "qty", "count", "amount"):
                    item_qty = _safe_int(item.get(key))
                    if item_qty is not None and item_qty == int(quantity):
                        qty_match = True
                        break

                score = int(service_match) + int(link_match) + int(qty_match)
                if score >= 2:
                    candidates.append((score, order_id, item))

            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                _, found_order_id, found_item = candidates[0]
                return "found", found_order_id, {"source": "orders_lookup", "entry": found_item}
        except requests.exceptions.RequestException as ex:
            lookup_errors.append(str(ex))
        except Exception as ex:
            lookup_errors.append(str(ex))

    if had_successful_lookup:
        return "not_found", None, {"source": "orders_lookup", "detail": "Заказ не найден в recent orders."}

    return "unknown", None, {"error": "lookup_unavailable", "detail": "; ".join(lookup_errors) or "Не удалось выполнить проверку recent orders."}


def _probe_order_after_uncertain_error_with_retries(
    service_number: int,
    service_id: int,
    link: str,
    quantity: int,
    initial_delay_seconds: int = 3,
    retry_delay_seconds: int = 4,
    retry_attempts_for_unknown: int = 2,
) -> Tuple[str, Optional[int], Dict]:
    """
    Сначала делает базовый lookup после timeout/network_error.
    Если состояние unknown, повторяет lookup ещё retry_attempts_for_unknown раз.

    Итого по умолчанию:
    - 1 первичная проверка после ~3 сек
    - 2 повторных lookup при unknown с паузой ~4 сек
    """
    last_payload: Dict = {}
    state, order_id, payload = _probe_order_after_uncertain_error(
        service_number=service_number,
        service_id=service_id,
        link=link,
        quantity=quantity,
        delay_seconds=initial_delay_seconds,
    )
    last_payload = payload

    if state in ("found", "not_found", "invalid_api_key"):
        return state, order_id, payload

    for retry_no in range(1, retry_attempts_for_unknown + 1):
        logger.warning(
            f"Lookup recent orders для service #{service_number} вернул unknown. Повторная попытка {retry_no}/{retry_attempts_for_unknown} через {retry_delay_seconds} сек."
        )
        state, order_id, payload = _probe_order_after_uncertain_error(
            service_number=service_number,
            service_id=service_id,
            link=link,
            quantity=quantity,
            delay_seconds=retry_delay_seconds,
        )
        last_payload = payload
        if state != "unknown":
            return state, order_id, payload

    return "unknown", None, last_payload


def process_link_without_confirmation(c: Cardinal, data: Dict):
    """
    Обрабатывает ссылку без подтверждения или после получения подтверждения.
    С поддержкой fallback-логики и защитой от двойных заказов после timeout/network_error.
    """
    link_ = data["link"]
    service_id = data["service_id"]
    real_amount = data["real_amount"]
    order_id_funpay = data["order_id_funpay"]
    buyer_chat_id = data["chat_id"]
    service_number = data["service_number"]
    lot_price = data["price"]

    cfg = load_config()
    existing_order = orders.get_order_data(order_id_funpay)

    ok, twiboost_id, payload = send_order_to_service(
        c=c,
        service_number=service_number,
        service_id=service_id,
        link=link_,
        quantity=real_amount,
    )

    if ok and twiboost_id is not None:
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
            fallback_used=bool(existing_order.get("fallback_used", False)) if existing_order else False,
            fallback_reason=existing_order.get("fallback_reason") if existing_order else None,
        )
        return

    err = payload.get("error", "unknown")
    detail = payload.get("detail", str(payload))
    fallback_reason = _detect_fallback_reason(err, payload)

    if err == "invalid_api_key":
        logger.warning(
            f"Primary add для заказа #{order_id_funpay} заблокирован из-за invalid_api_key. Перехожу к fallback без retry lookup."
        )

    if err in ("timeout", "network_error"):
        logger.warning(
            f"Неопределённый результат primary add для заказа #{order_id_funpay}: {err}. Жду 3 сек и проверяю recent orders..."
        )
        probe_state, probed_order_id, probe_payload = _probe_order_after_uncertain_error_with_retries(
            service_number=service_number,
            service_id=service_id,
            link=link_,
            quantity=real_amount,
            initial_delay_seconds=3,
            retry_delay_seconds=4,
            retry_attempts_for_unknown=2,
        )

        if probe_state == "found" and probed_order_id is not None:
            logger.warning(
                f"Заказ #{order_id_funpay} найден на primary после {err}. Fallback отменён, продолжаю мониторинг #{probed_order_id}."
            )
            _finalize_created_order(
                c=c,
                cfg=cfg,
                buyer_chat_id=buyer_chat_id,
                order_id_funpay=order_id_funpay,
                twiboost_id=probed_order_id,
                link_=link_,
                real_amount=real_amount,
                service_number=service_number,
                lot_price=lot_price,
                fallback_used=False,
                fallback_reason=None,
            )
            return

        if probe_state == "invalid_api_key":
            logger.warning(
                f"Lookup recent orders для заказа #{order_id_funpay} остановлен из-за invalid_api_key. Перехожу к fallback без дополнительных retry."
            )
        elif probe_state == "unknown":
            logger.error(
                f"Не удалось подтвердить существование заказа #{order_id_funpay} после {err} даже после retry lookup. Fallback заблокирован: {probe_payload}"
            )
            refund_order(
                c,
                order_id_funpay,
                buyer_chat_id,
                reason="Ошибка при создании заказа.",
                detailed_reason=(
                    f"Primary вернул {err}. Проверка существования заказа осталась в состоянии unknown даже после повторных lookup, "
                    f"поэтому fallback не запущен во избежание двойного заказа. "
                    f"Детали: {probe_payload.get('detail', probe_payload)}"
                ),
            )
            return

        logger.warning(
            f"Заказ #{order_id_funpay} не найден на primary после {err}. Разрешаю fallback по ТЗ."
        )

    fallback_enabled = cfg.get("fallback_enabled", False)
    if not fallback_enabled:
        logger.error(f"Fallback отключён. Выполняю refund для заказа #{order_id_funpay}. Причина: {err} | {detail}")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason="Ошибка при создании заказа.",
            detailed_reason=f"Fallback отключён. Primary ошибка: {err} | {detail}",
        )
        return

    existing_order = orders.get_order_data(order_id_funpay)
    if existing_order and existing_order.get("fallback_used", False):
        logger.error(f"Повторный fallback для заказа #{order_id_funpay} запрещён (fallback_used=True).")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason="Ошибка при создании заказа.",
            detailed_reason=f"Повторный fallback запрещён. Primary ошибка: {err} | {detail}",
        )
        return

    fallback_service_id, fallback_service_number = _get_lot_fallback_targets(cfg, service_id, service_number)
    if not fallback_service_id or not fallback_service_number:
        logger.error(f"Fallback не настроен для service_id={service_id}, service_number={service_number}.")
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason="Ошибка при создании заказа.",
            detailed_reason=f"Fallback не настроен. Primary ошибка: {err} | {detail}",
        )
        return

    if int(fallback_service_number) == int(service_number):
        logger.error(
            f"Fallback для заказа #{order_id_funpay} запрещён: primary_service_number == fallback_service_number == {service_number}."
        )
        refund_order(
            c,
            order_id_funpay,
            buyer_chat_id,
            reason="Ошибка при создании заказа.",
            detailed_reason=(
                f"Fallback заблокирован: primary и fallback указывают на один и тот же сервис #{service_number}."
            ),
        )
        return

    logger.warning(
        f"Primary попытка для заказа #{order_id_funpay} провалена ({err}). Запускаю fallback: service #{fallback_service_number}, id {fallback_service_id}."
    )
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
        )
        return

    err2 = payload2.get("error", "unknown")
    detail2 = payload2.get("detail", str(payload2))
    logger.error(f"Fallback попытка тоже провалена ({err2}) | {detail2}")

    refund_order(
        c,
        order_id_funpay,
        buyer_chat_id,
        reason="Ошибка при создании заказа.",
        detailed_reason=(
            f"Primary ошибка: {err} | {detail}. Fallback ошибка: {err2} | {detail2}."
        ),
    )

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
    if False:  # activation disabled
        logger.warning(f", автозапуск невозможен. ")
        return False
        
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
