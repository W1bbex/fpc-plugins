from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any, Dict, List, Optional
import unicodedata

from ..models import (
    extract_progress_values as _extract_progress_values,
    get_effective_funpay_status as _get_effective_funpay_status,
    is_order_refundedish as _is_order_refundedish,
    normalize_funpay_status as _normalize_funpay_status,
    resolve_panel_order_id as _get_panel_order_id,
    safe_int as _safe_int,
)
from .engine import (
    BUYER_STATE_COMPLETED,
    BUYER_STATE_FAILED,
    BUYER_STATE_LINK_PAUSED,
    BUYER_STATE_PANEL_CREATED,
    BUYER_STATE_REFUNDED,
    BUYER_STATE_WAITING_CONFIRM,
    BUYER_STATE_WAITING_LINK,
    _get_buyer_flow_state,
    _has_panel_order,
)

logger = logging.getLogger("FPC.autosmm")

BUYER_CONFIRM_REMINDER_SECONDS = 600
CONFIRM_REMINDER_TEXT = """⏳ Ссылка ожидает подтверждения.

+ — запустить заказ
- — изменить ссылку

Если пока не готовы подтвердить:
!ссылка"""
BUYER_PANEL_CREATED_BLOCK_TEXT = "Заказ уже передан в работу.\nИзменить ссылку автоматически нельзя."
BUYER_AUTO_CANCEL_UNAVAILABLE_TEXT = "Автоматическая отмена недоступна.\nЕсли хотите отменить заказ, напишите продавцу."
BUYER_AUTO_CANCEL_FAILED_TEXT = "Автоматическая отмена недоступна.\nПожалуйста, напишите продавцу."
BUYER_ADD_TRANSPORT_MANUAL_TEXT = """Автоматическая обработка временно остановлена.

Ваш заказ не потерян.
Продавец уведомлён и проверит его вручную.

Пожалуйста, не отправляйте ссылку повторно, чтобы не создать дубль."""
BUYER_LOT_MATCH_MANUAL_TEXT = """Автоматическая обработка временно остановлена.

Ваш заказ не потерян.
Продавец уведомлён и проверит его вручную."""

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

def _human_funpay_status(status_value: Any) -> str:
    return {"open": "оплачен", "closed": "закрыт", "refunded": "возврат", "partially_refunded": "частичный возврат"}.get(_normalize_funpay_status(status_value), "неизвестно")

def _format_created_at(order_record: Dict) -> str:
    created_at = _safe_int(order_record.get("created_at"))
    if created_at is not None and created_at > 0:
        try:
            return datetime.fromtimestamp(created_at).strftime("%d.%m.%Y %H:%M")
        except (OSError, OverflowError, ValueError) as exc:
            logger.debug(f"Некорректный created_at '{created_at}': {exc}")
    return str(order_record.get("date") or order_record.get("date_str") or "—")

def _format_buyer_progress(order_record: Dict) -> str:
    progress = _extract_progress_values(order_record)
    total = _safe_int(progress.get("total_quantity")) or 0
    done = _safe_int(progress.get("done_quantity")) or 0
    if total > 0:
        return f"{done}/{total}"
    return "ожидается"


BUYER_ACTIVE_PRE_PANEL_STATES = {
    BUYER_STATE_WAITING_LINK,
    BUYER_STATE_LINK_PAUSED,
    BUYER_STATE_WAITING_CONFIRM,
}

ORDER_NOT_FOUND_TEXT = """Заказ не найден или недоступен.

Проверьте номер заказа или напишите:
!смм

Там будут ваши последние SMM-заказы."""

def _first_present(mapping: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def _build_template_context(order_record: Optional[Dict] = None, extra: Optional[Dict] = None) -> Dict[str, Any]:
    merged = {**dict(order_record or {}), **dict(extra or {})}
    progress = _extract_progress_values(merged)
    panel_order_id = _first_present(merged, "panel_order_id", "twiboost_id", "id_zakaz")
    lot_title = _first_present(merged, "lot_title", "service_name", "orderDesc")
    order_price = _first_present(merged, "order_price", "summa", "orderPrice")
    order_amount = _first_present(merged, "order_amount", "amount", "orderAmount")
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
        if str(order_record.get("panel_status") or order_record.get("status") or "").strip().lower() == "partial":
            return "выполнен частично (требует проверки)"
        return "требует проверки"
    if state == BUYER_STATE_REFUNDED:
        return "недоступен после возврата"
    if _has_panel_order(order_record):
        return "в работе"
    return "ожидает ссылку"

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

def _human_panel_status(order_record: Dict) -> str:
    state = _get_buyer_flow_state(order_record)
    if state in (BUYER_STATE_WAITING_LINK, BUYER_STATE_LINK_PAUSED, BUYER_STATE_WAITING_CONFIRM, BUYER_STATE_FAILED, BUYER_STATE_REFUNDED, BUYER_STATE_COMPLETED):
        return _buyer_status_label(order_record)
    status = str(order_record.get("panel_status") or order_record.get("status") or "").strip().lower()
    return {"pending": "в работе", "completed": "выполнен", "partial": "частично", "canceled": "отменён", "cancelled": "отменён", "fail": "требует проверки", "failed": "требует проверки", "unknown": "требует проверки"}.get(status, "в работе" if _has_panel_order(order_record) else "ожидает ссылку")

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


class _SafeTemplateDict(dict):
    def __missing__(self, key):
        return ""

def _send_buyer_message(c: Cardinal, chat_id: int, text: str, max_lines: int = 18):
    for chunk in split_message_by_lines(text, max_lines=max_lines):
        c.send_message(chat_id, chunk)

def _render_template(template_text: str, order_record: Optional[Dict] = None, extra: Optional[Dict] = None) -> str:
    try:
        return str(template_text or "").format_map(_SafeTemplateDict(_build_template_context(order_record, extra)))
    except Exception as ex:
        logger.error(f"Ошибка форматирования шаблона AutoSMM: {ex}")
        return str(template_text or "")
