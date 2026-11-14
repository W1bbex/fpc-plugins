"""FunPay incoming order/message event dispatching and buyer utility commands."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import zlib

if TYPE_CHECKING:
    from cardinal import Cardinal

try:
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent
except ImportError:
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

    OrderStatusChangedEvent = None

from .. import runtime as rt
from ..models import is_order_refundedish as _is_order_refundedish, normalize_chat_id as _normalize_chat_id
from ..runtime import now_str as _now_str
from ..services.notifications import _send_lot_match_manual_alert
from ..storage.storage import get_order_data, load_config, update_order_fields
from .buyer_flow import (
    _buyer_can_access_order,
    _handle_buyer_cancel_command,
    _handle_buyer_commands_command,
    _handle_buyer_flow_message,
    _handle_buyer_link_command,
    _handle_buyer_smm_command,
    _upsert_buyer_flow_order_record,
)
from .buyer_messages import (
    BUYER_LOT_MATCH_MANUAL_TEXT,
    ORDER_NOT_FOUND_TEXT,
    _format_buyer_lot_title,
    _format_buyer_progress,
    _human_panel_status,
    _render_template,
    _send_buyer_message,
)
from .engine import (
    BUYER_STATE_FAILED,
    BUYER_STATE_WAITING_CONFIRM,
    BUYER_STATE_WAITING_LINK,
    _get_buyer_flow_state,
    _get_panel_order_id,
    _is_lot_match_ambiguous,
    _is_lot_match_no_match,
    resolve_lot_by_description,
)
from .funpay_sync import (
    _apply_refund_gate,
    _sync_funpay_status_from_event,
    _sync_funpay_status_from_order_event,
)
from .links import normalize_incoming_text
from .orders import _apply_status_result, _find_order_by_any_order_ref
from ..models import safe_int as _safe_int
from .panel_api import (
    _extract_service_credentials,
    _fetch_single_order_status,
    _panel_api_json,
)
from .panel_errors import (
    _normalize_api_key,
    _sanitize_secret_text,
    _validate_api_key_for_action,
)

logger = logging.getLogger("FPC.autosmm")

_REFILL_UNAVAILABLE_TEXT = (
    "Рефилл недоступен для этого заказа.\n\n"
    "Возможные причины:\n"
    "услуга не поддерживает гарантию, заказ ещё выполняется или срок гарантии истёк."
)


def _resolve_buyer_command_order(
    c: Cardinal,
    raw_ref: str,
    msg_author_id: Any,
    msg_chat_id: int,
) -> Optional[Dict[str, Any]]:
    found = _find_order_by_any_order_ref(
        raw_ref,
        buyer_id=msg_author_id,
        buyer_chat_id=msg_chat_id,
        access_checker=_buyer_can_access_order,
    )
    if not found or not _buyer_can_access_order(found, msg_author_id, msg_chat_id):
        c.send_message(msg_chat_id, ORDER_NOT_FOUND_TEXT)
        return None
    return found


def _handle_buyer_check_command(
    c: Cardinal,
    msg_text: str,
    msg_author_id: Any,
    msg_chat_id: int,
) -> bool:
    match = re.match(r"^чек\s+(.+?)\s*$", msg_text, re.IGNORECASE)
    if not match:
        return False

    found = _resolve_buyer_command_order(c, match.group(1).strip(), msg_author_id, msg_chat_id)
    if not found:
        return True

    if _is_order_refundedish(found):
        progress_text = _format_buyer_progress(found)
        c.send_message(
            msg_chat_id,
            f"📦 {_format_buyer_lot_title(found)}\n\nСтатус: недоступен после возврата\nПрогресс: {progress_text}",
        )
        return True

    panel_order_id = _get_panel_order_id(found)
    if panel_order_id is None:
        state = _get_buyer_flow_state(found)
        if state == BUYER_STATE_WAITING_CONFIRM:
            c.send_message(msg_chat_id, "Ссылка ожидает подтверждения. Напишите + или -.")
        elif state == BUYER_STATE_FAILED:
            c.send_message(msg_chat_id, "Заказ передан на ручную проверку. Пожалуйста, дождитесь ответа продавца.")
        else:
            c.send_message(msg_chat_id, "Заказ ещё не запущен. Отправьте ссылку для запуска.")
        return True

    parsed_service_number = _safe_int(found.get("service_number"))
    service_number = parsed_service_number if (parsed_service_number is not None and parsed_service_number > 0) else 1
    result = _fetch_single_order_status(service_number, panel_order_id)
    order_id_funpay = str(found.get("order_id") or found.get("order_id_funpay") or "")
    if result.get("ok"):
        _apply_status_result(c, found, result)
        found = get_order_data(order_id_funpay) or found
        status_text = _human_panel_status(found)
        quantity_text = _format_buyer_progress(found)
        c.send_message(
            msg_chat_id,
            f"📦 {_format_buyer_lot_title(found)}\n\nСтатус: {status_text}\nПрогресс: {quantity_text}\n\n"
            f"Пожалуйста, не злоупотребляйте командой.\nМы уведомим вас, когда заказ будет завершён.",
        )
        return True

    if order_id_funpay:
        update_order_fields(
            order_id_funpay,
            status_last_error=result.get("error") or "status_error",
            status_last_checked_at=_now_str(),
            updated_at=_now_str(),
        )
    c.send_message(msg_chat_id, "Не удалось проверить статус. Попробуйте позже или напишите продавцу.")
    return True


def _handle_buyer_refill_command(
    c: Cardinal,
    msg_text: str,
    msg_author_id: Any,
    msg_chat_id: int,
) -> bool:
    match = re.match(r"^рефилл\s+(.+?)\s*$", msg_text, re.IGNORECASE)
    if not match:
        return False

    found = _resolve_buyer_command_order(c, match.group(1).strip(), msg_author_id, msg_chat_id)
    if not found:
        return True
    if _apply_refund_gate(c, found, notify_chat_id=msg_chat_id, force_check=True):
        return True

    panel_order_id = _get_panel_order_id(found)
    if panel_order_id is None:
        c.send_message(msg_chat_id, "Заказ ещё не запущен. Рефилл будет доступен только после запуска заказа.")
        return True

    service_number = _safe_int(found.get("service_number"))
    if service_number is None:
        c.send_message(msg_chat_id, _REFILL_UNAVAILABLE_TEXT)
        return True

    cfg = load_config()
    api_url, api_key, config_err = _extract_service_credentials(cfg, service_number)
    if config_err or not api_url or _validate_api_key_for_action(service_number, "refill", api_key):
        c.send_message(msg_chat_id, _REFILL_UNAVAILABLE_TEXT)
        return True

    try:
        _rr, rdata, _raw_text, invalid_json = _panel_api_json(
            api_url,
            {"key": api_key, "action": "refill", "order": panel_order_id},
            timeout=15,
        )
        if invalid_json or not isinstance(rdata, dict):
            raise ValueError("панель не вернула JSON рефилла")
        refill_id = rdata.get("refill")
        status_value = rdata.get("status", 0)
        has_error = bool(rdata.get("error")) or isinstance(refill_id, dict)
        refill_ok = (not has_error) and (
            (refill_id not in (None, "", 0, "0", False))
            or str(status_value).lower() in ("1", "true", "success")
        )
        if refill_ok:
            c.send_message(
                msg_chat_id,
                f"🔄 Рефилл запрошен.\n\nID заказа в сервисе: {panel_order_id}\n\n"
                f"Если услуга поддерживает гарантию, панель обработает запрос автоматически.",
            )
        else:
            c.send_message(msg_chat_id, _REFILL_UNAVAILABLE_TEXT)
    except Exception as exc:
        safe_err = _sanitize_secret_text(exc, api_key)
        logger.warning(f"Ошибка рефилла для заказа panel={panel_order_id}: {safe_err}")
        c.send_message(msg_chat_id, "Не удалось запросить рефилл. Попробуйте позже или напишите продавцу.")
    return True


def _handle_new_message_event(c: Cardinal, e: NewMessageEvent, my_id: Any) -> None:
    if e.message.author_id == my_id:
        return

    msg_text = normalize_incoming_text(e.message.text or "")
    msg_author_id = e.message.author_id
    msg_chat_id = e.message.chat_id
    logger.info(f"Новое сообщение от {e.message.author}: {msg_text}")

    if msg_author_id == 0:
        _sync_funpay_status_from_event(c, e.message)
        return

    for handler in (
        _handle_buyer_commands_command,
        _handle_buyer_smm_command,
        _handle_buyer_link_command,
        _handle_buyer_cancel_command,
        _handle_buyer_check_command,
        _handle_buyer_refill_command,
        _handle_buyer_flow_message,
    ):
        if handler(c, msg_text, msg_author_id, msg_chat_id):
            return


def _handle_unmatched_order(
    c: Cardinal,
    order_id: Any,
    order_desc: str,
    order_price: Any,
    order_amount: Any,
    buyer_chat_id: int,
    buyer_id: Any,
    buyer_username: Any,
    lot_result: dict,
) -> None:
    if _is_lot_match_no_match(lot_result):
        logger.info(
            "AUTO SMM SKIP NON-AUTOSMM ORDER: order=%s, description=%r",
            order_id,
            str(order_desc or "")[:300],
        )
        return

    error_code = str(lot_result.get("error") or "no_match")
    stop_reason = "lot_match_ambiguous" if _is_lot_match_ambiguous(lot_result) else "lot_match_failed"
    if get_order_data(str(order_id)) is not None:
        logger.info("DUPLICATE UNMATCHED NEW_ORDER SKIPPED: order=%s", order_id)
        return
    logger.warning("LOT MATCH MANUAL CHECK: order=%s, error=%s", order_id, error_code)
    created = _upsert_buyer_flow_order_record(
        chat_id=buyer_chat_id,
        buyer_id=buyer_id,
        buyer_username=buyer_username,
        order_id_funpay=str(order_id),
        lot_title=str(order_desc or "SMM-заказ"),
        order_price=order_price,
        order_amount=order_amount,
        service_id=0,
        real_amount=0,
        service_number=0,
        funpay_status="open",
        state=BUYER_STATE_FAILED,
        saved_link=None,
    )
    if created is False:
        logger.info("DUPLICATE UNMATCHED NEW_ORDER SKIPPED ON UPSERT: order=%s", order_id)
        return
    update_order_fields(
        str(order_id),
        status="unknown",
        buyer_flow_state=BUYER_STATE_FAILED,
        status_stop_reason=stop_reason,
        updated_at=_now_str(),
    )
    _send_lot_match_manual_alert(order_id, order_desc, lot_result)
    if _normalize_chat_id(buyer_chat_id) is not None:
        _send_buyer_message(c, buyer_chat_id, BUYER_LOT_MATCH_MANUAL_TEXT)
    else:
        logger.warning("UNMATCHED ORDER BUYER MESSAGE SKIPPED: missing buyer_chat_id for order=#%s", order_id)


def _persist_and_notify_matched_order(
    c: Cardinal,
    cfg: Dict[str, Any],
    order_: Any,
    buyer_chat_id: int,
    buyer_id: Any,
    buyer_username: str,
    lot_result: Dict[str, Any],
) -> None:
    order_id = order_.id
    existing_order = get_order_data(str(order_id))
    if existing_order is not None:
        logger.info(
            "DUPLICATE NEW_ORDER EVENT SKIPPED: order=#%s already exists (state=%s, funpay_status=%s)",
            order_id,
            existing_order.get("buyer_flow_state"),
            existing_order.get("funpay_status"),
        )
        return
    order_desc = order_.description
    order_amount = order_.amount
    order_price = order_.price
    service_id = lot_result.get("service_id")
    real_amount = lot_result.get("real_amount")
    srv_number = lot_result.get("service_number", 1)
    matched_lot_key = lot_result.get("lot_key")
    matched_lot_title = lot_result.get("lot_title") or order_desc

    created = _upsert_buyer_flow_order_record(
        chat_id=buyer_chat_id,
        buyer_id=buyer_id,
        buyer_username=buyer_username,
        order_id_funpay=str(order_id),
        lot_title=matched_lot_title,
        order_price=order_price,
        order_amount=order_amount,
        service_id=service_id,
        real_amount=real_amount,
        service_number=srv_number,
        lot_key=matched_lot_key,
        funpay_status="open",
        state=BUYER_STATE_WAITING_LINK,
    )
    if created is False:
        logger.info("DUPLICATE NEW_ORDER EVENT SKIPPED ON UPSERT: order=#%s", order_id)
        return

    now_ts = time.time()
    with rt.CONTEXT_LOCK:
        rt.waiting_for_link[str(order_id)] = {
            "buyer_id": buyer_id,
            "chat_id": buyer_chat_id,
            "buyer_username": buyer_username,
            "service_id": service_id,
            "real_amount": real_amount,
            "order_id_funpay": str(order_id),
            "price": order_price,
            "service_number": srv_number,
            "lot_key": matched_lot_key,
            "lot_title": matched_lot_title,
            "state": BUYER_STATE_WAITING_LINK,
            "step": "await_link",
            "created_ts": now_ts,
            "updated_ts": now_ts,
        }

    order_record = get_order_data(str(order_id)) or {}
    msg_payment = _render_template(
        cfg.get("messages", {}).get("after_payment", "❤️ Спасибо за оплату! Укажите ссылку для запуска заказа."),
        order_record,
        {
            "buyer_username": buyer_username,
            "lot_title": matched_lot_title,
            "orderDesc": matched_lot_title,
            "order_id_funpay": order_id,
            "order_price": order_price,
            "orderPrice": order_price,
            "order_amount": order_amount,
            "orderAmount": order_amount,
            "currency": "RUB",
            "total_quantity": real_amount,
            "done_quantity": 0,
            "left_quantity": real_amount,
            "progress_percent": 0,
            "funpay_status": "open",
            "panel_status": "",
        },
    )
    if _normalize_chat_id(buyer_chat_id) is None:
        logger.warning("BUYER PROMPT SKIPPED: missing buyer_chat_id for order=#%s", order_id)
        return
    try:
        _send_buyer_message(c, buyer_chat_id, msg_payment)
    except Exception as exc:
        logger.warning("Не удалось отправить приветствие покупателю по заказу #%s: %s", order_id, exc)


def _resolve_buyer_chat_target(c: Cardinal, order_: Any) -> tuple[Any, Any, str]:
    """Resolve buyer numeric chat_id via Cardinal cache first, falling back to get_order only on miss."""
    order_id = getattr(order_, "id", None)
    buyer_id = getattr(order_, "buyer_id", None)
    buyer_username = str(getattr(order_, "buyer_username", None) or "")
    raw_chat_id = getattr(order_, "chat_id", None)
    buyer_chat_id: Any = raw_chat_id if isinstance(raw_chat_id, int) else None

    account = getattr(c, "account", None)
    if buyer_chat_id is None and buyer_username and account is not None:
        get_chat_by_name = getattr(account, "get_chat_by_name", None)
        if callable(get_chat_by_name):
            try:
                chat_shortcut = get_chat_by_name(buyer_username)
                cached_chat_id = getattr(chat_shortcut, "id", None)
                if isinstance(cached_chat_id, int):
                    buyer_chat_id = cached_chat_id
            except Exception as exc:
                logger.debug(f"get_chat_by_name({buyer_username}) cache lookup failed: {exc}")

    if (buyer_chat_id is None or buyer_id is None) and account is not None and callable(getattr(account, "get_order", None)):
        try:
            od_full = account.get_order(order_id)
            full_chat_id = getattr(od_full, "chat_id", None)
            if full_chat_id is not None:
                buyer_chat_id = full_chat_id
            if buyer_id is None:
                buyer_id = getattr(od_full, "buyer_id", None)
            if not buyer_username:
                buyer_username = str(getattr(od_full, "buyer_username", None) or "")
        except Exception as exc:
            logger.warning(f"Не удалось получить детали заказа #{order_id} через get_order: {exc}")

    if buyer_chat_id is None:
        buyer_chat_id = raw_chat_id
    return buyer_chat_id, buyer_id, buyer_username


def _handle_new_order_event(c: Cardinal, e: NewOrderEvent) -> None:
    order_ = e.order
    order_id = order_.id
    order_desc = order_.description
    order_amount = order_.amount
    order_price = order_.price
    logger.info(f"Новый заказ #{order_id}: {order_desc}, x{order_amount}")

    # Match the lot before any FunPay API calls so non-AutoSMM orders trigger zero HTTP requests.
    lot_result = resolve_lot_by_description(order_desc, order_amount)
    if not lot_result.get("ok"):
        if _is_lot_match_no_match(lot_result):
            logger.info(
                "AUTO SMM SKIP NON-AUTOSMM ORDER: order=%s, description=%r",
                order_id,
                str(order_desc or "")[:300],
            )
            return
        with rt.get_order_lock(order_id):
            buyer_chat_id, buyer_id, buyer_username = _resolve_buyer_chat_target(c, order_)
            _handle_unmatched_order(
                c,
                order_id,
                order_desc,
                order_price,
                order_amount,
                buyer_chat_id,
                buyer_id,
                buyer_username,
                lot_result,
            )
        return

    cfg = load_config()
    with rt.get_order_lock(order_id):
        buyer_chat_id, buyer_id, buyer_username = _resolve_buyer_chat_target(c, order_)
        _persist_and_notify_matched_order(
            c,
            cfg,
            order_,
            buyer_chat_id,
            buyer_id,
            buyer_username,
            lot_result,
        )


_EVENT_WORKER_COUNT = 4
_EVENT_SHARD_QUEUE_MAXSIZE = 512
_STOP_SENTINEL = object()
_EVENT_WORKERS_LOCK = threading.RLock()
_SHARD_QUEUES: List[queue.Queue] = []
_EVENT_WORKERS: List[threading.Thread] = []


def _is_supported_event(e: Any) -> bool:
    if isinstance(e, (NewMessageEvent, NewOrderEvent)):
        return True
    return OrderStatusChangedEvent is not None and isinstance(e, OrderStatusChangedEvent)


def _resolve_event_shard_key(e: Any) -> str:
    if isinstance(e, NewMessageEvent):
        chat_id = getattr(getattr(e, "message", None), "chat_id", None)
        if chat_id is not None:
            return f"chat:{chat_id}"
        return "default"
    if isinstance(e, NewOrderEvent):
        order_obj = getattr(e, "order", None)
        raw_chat_id = getattr(order_obj, "chat_id", None)
        if isinstance(raw_chat_id, int):
            return f"chat:{raw_chat_id}"
        order_id = str(getattr(order_obj, "id", "") or "").strip().lstrip("#")
        return f"order:{order_id}" if order_id else "default"
    if OrderStatusChangedEvent is not None and isinstance(e, OrderStatusChangedEvent):
        order_id = str(getattr(getattr(e, "order", None), "id", "") or "").strip().lstrip("#")
        return f"order:{order_id}" if order_id else "default"
    return "default"


def _dispatch_event_sync(c: Cardinal, e: Any) -> None:
    if isinstance(e, NewMessageEvent):
        my_id = getattr(getattr(c, "account", None), "id", None)
        _handle_new_message_event(c, e, my_id)
    elif isinstance(e, NewOrderEvent):
        _handle_new_order_event(c, e)
    elif OrderStatusChangedEvent is not None and isinstance(e, OrderStatusChangedEvent):
        _sync_funpay_status_from_order_event(c, e)


def _shard_worker_loop(shard_queue: queue.Queue) -> None:
    while True:
        item = shard_queue.get()
        try:
            if item is _STOP_SENTINEL:
                return
            c, e = item
            _dispatch_event_sync(c, e)
        except Exception as exc:
            logger.exception(f"Unhandled exception in AutoSMM event worker: {exc}")
        finally:
            shard_queue.task_done()


def _ensure_event_workers_locked() -> None:
    if len(_SHARD_QUEUES) == _EVENT_WORKER_COUNT and len(_EVENT_WORKERS) == _EVENT_WORKER_COUNT:
        if all(worker.is_alive() for worker in _EVENT_WORKERS):
            return
    if not _SHARD_QUEUES or len(_SHARD_QUEUES) != _EVENT_WORKER_COUNT:
        _SHARD_QUEUES[:] = [queue.Queue(maxsize=_EVENT_SHARD_QUEUE_MAXSIZE) for _ in range(_EVENT_WORKER_COUNT)]
    rebuilt_workers: List[threading.Thread] = []
    for idx in range(_EVENT_WORKER_COUNT):
        existing = _EVENT_WORKERS[idx] if idx < len(_EVENT_WORKERS) else None
        if existing is not None and existing.is_alive():
            rebuilt_workers.append(existing)
            continue
        worker = threading.Thread(
            target=_shard_worker_loop,
            args=(_SHARD_QUEUES[idx],),
            name=f"autosmm-event-worker-{idx}",
            daemon=True,
        )
        worker.start()
        rebuilt_workers.append(worker)
    _EVENT_WORKERS[:] = rebuilt_workers


def start_event_workers() -> None:
    """Start or verify sharded background worker threads for FunPay event dispatch."""
    with _EVENT_WORKERS_LOCK:
        _ensure_event_workers_locked()


def flush_event_queue(timeout: float = 5.0) -> bool:
    """Wait up to `timeout` seconds for all currently queued FunPay events to complete."""
    with _EVENT_WORKERS_LOCK:
        queues = list(_SHARD_QUEUES)
    deadline = time.monotonic() + max(0.0, float(timeout))
    for shard_queue in queues:
        with shard_queue.all_tasks_done:
            while shard_queue.unfinished_tasks > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                shard_queue.all_tasks_done.wait(timeout=remaining)
    return True


def stop_event_workers(*, drain: bool = True, timeout: float = 5.0) -> None:
    """Stop all sharded FunPay event workers cleanly and wait up to `timeout` seconds for exit."""
    with _EVENT_WORKERS_LOCK:
        queues = list(_SHARD_QUEUES)
        workers = list(_EVENT_WORKERS)
        _SHARD_QUEUES.clear()
        _EVENT_WORKERS.clear()

    if not drain:
        for shard_queue in queues:
            while True:
                try:
                    shard_queue.get_nowait()
                    shard_queue.task_done()
                except queue.Empty:
                    break

    for shard_queue in queues:
        while True:
            try:
                shard_queue.put_nowait(_STOP_SENTINEL)
                break
            except queue.Full:
                try:
                    shard_queue.get_nowait()
                    shard_queue.task_done()
                except queue.Empty:
                    break

    deadline = time.monotonic() + max(0.0, float(timeout))
    current = threading.current_thread()
    for worker in workers:
        is_alive_fn = getattr(worker, "is_alive", None)
        if worker is current or (callable(is_alive_fn) and not is_alive_fn()):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        join_fn = getattr(worker, "join", None)
        if callable(join_fn):
            join_fn(timeout=max(0.05, remaining))


def auto_smm_handler(c: Cardinal, e: Any, *args: Any) -> None:
    """Enqueue FunPay NewMessageEvent, NewOrderEvent, and OrderStatusChangedEvent without blocking Runner."""
    del args
    if not rt.RUNNING or not _is_supported_event(e):
        return
    shard_key = _resolve_event_shard_key(e)
    with _EVENT_WORKERS_LOCK:
        if not rt.RUNNING:
            return
        _ensure_event_workers_locked()
        shard_idx = zlib.crc32(shard_key.encode("utf-8")) % len(_SHARD_QUEUES)
        target_queue = _SHARD_QUEUES[shard_idx]
        try:
            target_queue.put_nowait((c, e))
        except queue.Full:
            logger.error(
                "EVENT QUEUE FULL (shard=%s, size=%s): dropping %s",
                shard_idx,
                target_queue.qsize(),
                type(e).__name__,
            )
