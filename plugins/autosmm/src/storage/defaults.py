from __future__ import annotations

import re
from typing import Any, Dict

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

DEFAULT_AFTER_PAYMENT_TEMPLATE = """👾 Благодарю за оплату!

🧾 Заказ: #{order_id_funpay}
📦 Лот: {lot_title}

Чтобы запустить выполнение, отправьте ссылку или юзернейм (@username).

Ссылка должна начинаться с https:// или быть юзернеймом (например, @channel).

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

DEFAULT_LINK_CONFIRM_TEMPLATE = """✅ Ссылка / юзернейм получены

🧾 Заказ: #{order_id_funpay}
📦 Лот: {lot_title}

🔗 Ссылка / юзернейм:
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
    ensure_key("allow_buyer_cancel_before_panel_order", False)
    ensure_key("try_panel_cancel_after_funpay_refund", False)
    ensure_key("confirm_link", True)
    ensure_key("notification_chat_id", None)
    ensure_key("lot_mapping", {})

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
        "lot_mapping": {},
    }
