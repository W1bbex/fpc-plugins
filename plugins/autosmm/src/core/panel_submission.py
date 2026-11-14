"""Panel order submission, atomic pre-add claim, fallback orchestration, and manual-check guards."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from cardinal import Cardinal

from .. import runtime as rt
from ..models import is_order_refundedish as _is_order_refundedish, normalize_chat_id as _normalize_chat_id, safe_int as _safe_int
from ..services.notifications import (
    _send_admin_order_alert,
    _send_transport_error_after_add_alert,
    send_order_started_notification,
)
from ..storage.storage import (
    ORDERS_LOCK,
    claim_order_for_panel_submission,
    get_order_data,
    load_config,
    update_order_fields,
    upsert_order_data,
)
from .buyer_messages import (
    BUYER_ADD_TRANSPORT_MANUAL_TEXT,
    BUYER_PANEL_CREATED_BLOCK_TEXT,
    _render_template,
    _send_buyer_message,
)
from .engine import (
    BUYER_STATE_FAILED,
    BUYER_STATE_PANEL_CREATED,
    BUYER_STATE_REFUNDED,
    _detect_fallback_reason,
    _find_lot_data_for_order,
    _get_lot_fallback_targets,
    _has_panel_order,
)
from .funpay_sync import _apply_refund_gate
from .links import _format_target_for_panel
from .orders import check_order_status
from .panel_api import _extract_service_credentials, _get_panel_balance_for_risk_check, send_order_to_service
from .panel_balances import schedule_single_panel_balance_refresh
from .refunds import _attempt_panel_cancel_after_funpay_refund, refund_order

AMBIGUOUS_ADD_ERRORS = ("timeout", "network_error", "invalid_json", "http_error", "unknown")
_TRANSPORT_RISK_DELAY_SECONDS = 3.0


def _store_fallback_decision(
    order_id_funpay: str,
    fallback_used: Optional[bool] = None,
    fallback_reason: Optional[str] = None,
) -> None:
    if not get_order_data(order_id_funpay):
        return
    if fallback_used is True:
        update_order_fields(order_id_funpay, fallback_used=True, fallback_reason=fallback_reason)
    elif fallback_reason:
        update_order_fields(order_id_funpay, fallback_used=False, fallback_reason=fallback_reason)


def _block_fallback_and_refund(
    c: "Cardinal",
    order_id_funpay: str,
    buyer_chat_id: int,
    service_number: int,
    primary_error: Any,
    fallback_reason: Optional[str],
    detailed_reason: str,
    bot_action: str,
    log_manual_check: bool = False,
) -> None:
    """Persist fallback block reason (if provided) and initiate safe refund/manual alert."""
    if log_manual_check:
        rt.logger.error("MANUAL CHECK REQUIRED")
    rt.logger.error("FALLBACK BLOCKED")
    if fallback_reason is not None:
        _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason=fallback_reason)
    update_order_fields(
        order_id_funpay,
        panel_status=None,
        status_stop_reason=fallback_reason or str(primary_error or "primary_rejected"),
        updated_at=rt.now_str(),
    )
    refund_order(
        c,
        order_id_funpay,
        buyer_chat_id,
        reason=f"{primary_error} на панели #{service_number}",
        detailed_reason=detailed_reason,
        bot_action=bot_action,
        admin_action="проверьте заказ и при необходимости выполните возврат вручную",
        failure_title="⚠️ Заказ требует ручной проверки",
    )


def _persist_created_order_records(
    buyer_chat_id: int,
    order_id_funpay: str,
    panel_order_id: int,
    link_: str,
    real_amount: int,
    service_number: int,
    lot_price: float,
    fallback_used: bool,
    fallback_reason: Optional[str],
) -> None:
    with ORDERS_LOCK:
        existing = get_order_data(order_id_funpay)
        already_refunded = bool(existing and _is_order_refundedish(existing))
        resolved_status = (existing.get("status") or "canceled") if already_refunded else "pending"
        resolved_panel_status = (existing.get("panel_status") or "canceled") if already_refunded else "pending"
        upsert_order_data(
            buyer_chat_id,
            order_id_funpay,
            panel_order_id,
            resolved_status,
            float(lot_price),
            link_,
            real_amount,
            service_number,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            funpay_status=None,
        )
        updated = update_order_fields(
            order_id_funpay,
            buyer_flow_state=BUYER_STATE_REFUNDED if already_refunded else BUYER_STATE_PANEL_CREATED,
            panel_order_id=panel_order_id,
            panel_status=resolved_panel_status,
            saved_link=None,
            status_stop_reason=None,
            status_last_error=None,
            total_quantity=int(real_amount or 0),
            done_quantity=0,
            left_quantity=int(real_amount or 0),
            progress_percent=0,
            updated_at=rt.now_str(),
        )
        if not updated:
            raise RuntimeError(f"update_order_fields returned False for order #{order_id_funpay}")


def _mark_persist_failure_after_add(
    c: "Cardinal",
    order_id_funpay: str,
    buyer_chat_id: int,
    panel_order_id: int,
    service_number: int,
    link_: str,
    exc: Exception,
) -> None:
    order_id_str = str(order_id_funpay)
    rt.logger.exception(
        f"PERSIST FAILED AFTER PANEL ADD: order=#{order_id_str}, panel_order_id={panel_order_id}, "
        f"service=#{service_number}, link={link_}, error={exc}"
    )
    with rt.CONTEXT_LOCK:
        rt.waiting_for_link.pop(order_id_str, None)
    try:
        update_order_fields(
            order_id_str,
            service_number=int(service_number or 1),
            status="unknown",
            buyer_flow_state=BUYER_STATE_FAILED,
            panel_status="manual_check",
            status_stop_reason="persist_failed_after_add",
            status_last_error=str(exc),
            candidate_panel_order_id=panel_order_id,
            saved_link=link_,
            customer_url=link_,
            updated_at=rt.now_str(),
        )
    except Exception as inner_ex:
        rt.logger.error(f"Не удалось обновить статус persist_failed_after_add для #{order_id_str}: {inner_ex}")

    try:
        resolved_chat_id = _normalize_chat_id(buyer_chat_id)
        if resolved_chat_id is not None:
            _send_buyer_message(c, resolved_chat_id, BUYER_ADD_TRANSPORT_MANUAL_TEXT)
    except Exception as buyer_ex:
        rt.logger.warning(f"Не удалось отправить buyer manual text по persist-failure #{order_id_str}: {buyer_ex}")

    try:
        _send_admin_order_alert(
            order_id_funpay=order_id_str,
            reason=f"ошибка сохранения заказа после успешного создания на панели #{service_number} (panel_order_id={panel_order_id}): {exc}",
            bot_action="повторное создание заказа заблокировано, заказ переведён на ручную проверку",
            action=f"проверьте заказ #{panel_order_id} на панели #{service_number} и занесите данные вручную",
            title="⚠️ Сбой сохранения после создания заказа на панели",
        )
    except Exception as alert_ex:
        rt.logger.warning(f"Не удалось отправить admin alert по persist-failure #{order_id_str}: {alert_ex}")


def _send_buyer_order_confirmation(
    c: "Cardinal",
    cfg: Dict,
    buyer_chat_id: int,
    order_id_funpay: str,
    panel_order_id: int,
    link_: str,
    real_amount: int,
) -> None:
    order_record = get_order_data(order_id_funpay) or {}
    msg_confirmation = _render_template(
        cfg.get("messages", {}).get(
            "after_confirmation",
            "🎉 Заказ успешно оформлен!\n\n🔢 ID заказа в сервисе: {panel_order_id}\n🔗 {link}",
        ),
        order_record,
        {
            "panel_order_id": panel_order_id,
            "twiboost_id": panel_order_id,
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


def _finalize_created_order(
    c: "Cardinal",
    cfg: Dict,
    buyer_chat_id: int,
    order_id_funpay: str,
    panel_order_id: int,
    link_: str,
    real_amount: int,
    service_number: int,
    lot_price: float,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    notification_info_title: Optional[str] = None,
    notification_reason: Optional[str] = None,
    notification_action: Optional[str] = None,
) -> None:
    try:
        _persist_created_order_records(
            buyer_chat_id=buyer_chat_id,
            order_id_funpay=order_id_funpay,
            panel_order_id=panel_order_id,
            link_=link_,
            real_amount=real_amount,
            service_number=service_number,
            lot_price=lot_price,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
    except Exception as exc:
        _mark_persist_failure_after_add(
            c=c,
            order_id_funpay=order_id_funpay,
            buyer_chat_id=buyer_chat_id,
            panel_order_id=panel_order_id,
            service_number=service_number,
            link_=link_,
            exc=exc,
        )
        return

    with rt.CONTEXT_LOCK:
        rt.waiting_for_link.pop(str(order_id_funpay), None)
    try:
        schedule_single_panel_balance_refresh(int(service_number or 1), cfg=cfg)
    except Exception as balance_ex:
        rt.logger.debug(f"Не удалось запустить обновление баланса панели #{service_number}: {balance_ex}")

    latest_after_save = get_order_data(order_id_funpay)
    if latest_after_save and _is_order_refundedish(latest_after_save):
        rt.logger.warning(
            f"CONCURRENT REFUND DETECTED AFTER PANEL ADD: order=#{order_id_funpay}, panel_order_id={panel_order_id}"
        )
        _attempt_panel_cancel_after_funpay_refund(latest_after_save, source="concurrent_refund_after_add")
        return

    api_url, api_key, config_err = _extract_service_credentials(cfg, service_number)
    if config_err or not api_url:
        rt.logger.warning(f"POST-CREATE NOTIFICATION SKIPPED: {config_err or f'не найден config.services[{service_number}]'}")
    else:
        try:
            send_order_started_notification(
                c,
                order_id_funpay,
                panel_order_id,
                api_url,
                api_key,
                lot_price,
                real_amount,
                service_number=service_number,
                info_title=notification_info_title,
                reason=notification_reason,
                action_text=notification_action,
            )
        except Exception as notify_ex:
            rt.logger.warning(f"Не удалось отправить уведомление о запуске заказа #{order_id_funpay}: {notify_ex}")

    try:
        check_order_status(c, panel_order_id, buyer_chat_id, link_, order_id_funpay)
    except Exception as check_ex:
        rt.logger.warning(f"Ошибка запуска проверки статуса #{order_id_funpay}: {check_ex}")

    try:
        _send_buyer_order_confirmation(c, cfg, buyer_chat_id, order_id_funpay, panel_order_id, link_, real_amount)
    except Exception as buyer_ex:
        rt.logger.warning(f"Не удалось отправить подтверждение покупателю по #{order_id_funpay}: {buyer_ex}")


def _handle_transport_error_after_add(
    c: "Cardinal",
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
    fallback_reason: Optional[str] = None,
    wait_before_balance_check: bool = False,
) -> Optional[threading.Thread]:
    """Block refunds/retries and move the order to manual verification after an ambiguous add error."""
    rt.logger.error("MANUAL CHECK REQUIRED")
    rt.logger.error("REFUND BLOCKED")
    rt.logger.error("FALLBACK BLOCKED")

    order_id_str = str(order_id_funpay)
    order_record = get_order_data(order_id_str)
    if not order_record:
        try:
            claim_order_for_panel_submission(
                order_id_str,
                saved_link=link_,
                service_number=int(service_number or 1),
                seed_fields={
                    "chat_id": _normalize_chat_id(buyer_chat_id),
                    "lot_title": lot_title or "—",
                    "service_id": int(service_id or 0),
                    "quantity": int(real_amount or 0),
                    "summa": float(lot_price or 0),
                },
            )
        except Exception as ex:
            rt.logger.warning(f"Не удалось создать минимальную запись manual-check для заказа #{order_id_str}: {ex}")

    update_kwargs: Dict[str, Any] = {
        "service_number": int(service_number or 1),
        "status": "unknown",
        "buyer_flow_state": BUYER_STATE_FAILED,
        "saved_link": link_,
        "customer_url": link_,
        "status_last_error": str(primary_error or "unknown"),
        "status_stop_reason": "transport_error_after_add",
        "panel_status": "manual_check",
        "updated_at": rt.now_str(),
    }
    if fallback_reason:
        update_kwargs["fallback_reason"] = fallback_reason
    update_order_fields(order_id_str, **update_kwargs)
    with rt.CONTEXT_LOCK:
        rt.waiting_for_link.pop(order_id_str, None)

    try:
        resolved_chat_id = _normalize_chat_id(buyer_chat_id)
        if resolved_chat_id is not None:
            _send_buyer_message(c, resolved_chat_id, BUYER_ADD_TRANSPORT_MANUAL_TEXT)
    except Exception as ex:
        rt.logger.warning(f"Не удалось отправить buyer manual text по transport-error #{order_id_str}: {ex}")

    def _emit_alert(resolved_after: Optional[Dict[str, Any]]) -> None:
        _send_transport_error_after_add_alert(
            order_id_funpay=order_id_str,
            service_number=service_number,
            service_id=service_id,
            real_amount=real_amount,
            link_=link_,
            primary_error=primary_error,
            primary_detail=primary_detail,
            balance_before=balance_before,
            balance_after=resolved_after,
        )

    if wait_before_balance_check and balance_after is None:
        def _delayed_risk_alert() -> None:
            resolved_after = None
            try:
                delay = float(_TRANSPORT_RISK_DELAY_SECONDS or 0.0)
                if delay > 0 and not rt.STOP_EVENT.is_set():
                    rt.STOP_EVENT.wait(timeout=delay)
                if not rt.STOP_EVENT.is_set():
                    resolved_after = _get_panel_balance_for_risk_check(int(service_number or 1))
            except Exception as ex:
                rt.logger.warning(f"Отложенная проверка баланса после transport-error #{order_id_str} не удалась: {ex}")
            try:
                _emit_alert(resolved_after)
            except Exception as ex:
                rt.logger.exception(f"Ошибка отправки отложенного alert #{order_id_str}: {ex}")

        worker = threading.Thread(
            target=_delayed_risk_alert,
            name=f"autosmm-transport-alert-{order_id_str}",
            daemon=True,
        )
        rt.register_background_thread(worker)
        worker.start()
        return worker

    _emit_alert(balance_after)
    return None


def _get_fallback_block_spec(
    primary_error: Any,
    primary_detail: Any,
    service_number: Any,
    fallback_enabled: bool,
    existing_order: Optional[Dict],
    fallback_service_id: Any,
    fallback_service_number: Any,
) -> Optional[Tuple[Optional[str], str, str, bool]]:
    if primary_error in ("validation_error", "unsupported_action"):
        return (
            str(primary_error),
            f"Primary ошибка: {primary_error}. Резервная панель заблокирована. Детали: {primary_detail}",
            "резервная панель заблокирована, заказ не создан автоматически",
            False,
        )
    if primary_error not in ("service_inactive", "insufficient_funds", "user_inactive"):
        return (
            None,
            f"Primary ошибка: {primary_error}. Резервная панель не запускалась. Детали: {primary_detail}",
            "автоматическая обработка остановлена, резервная панель не запускалась",
            True,
        )
    if not fallback_enabled:
        return (
            "fallback_disabled",
            f"Резервная панель отключена. Primary ошибка: {primary_error} | {primary_detail}",
            "резервная панель отключена, заказ не создан автоматически",
            False,
        )
    if existing_order and existing_order.get("fallback_used", False):
        return (
            "fallback_already_used",
            f"Повторная резервная панель запрещена. Primary ошибка: {primary_error} | {primary_detail}",
            "повторная резервная панель запрещена, заказ не создан автоматически",
            False,
        )
    fb_service_id = _safe_int(fallback_service_id)
    fb_service_number = _safe_int(fallback_service_number)
    if (fb_service_id is None or fb_service_id <= 0) or (
        fb_service_number is None or fb_service_number <= 0
    ):
        return (
            "fallback_not_configured",
            f"Резервная панель не настроена. Primary ошибка: {primary_error} | {primary_detail}",
            "резервная панель не настроена, заказ не создан автоматически",
            False,
        )
    if fb_service_number == int(service_number):
        if primary_error == "user_inactive":
            return (
                "same_panel_auth_failed",
                f"Primary ошибка: {primary_error}. Резервная панель заблокирована: основная и резервная панели указывают на один и тот же сервис #{service_number}.",
                "резервная панель заблокирована, потому что основная и резервная панели совпадают",
                False,
            )
        return (
            "same_panel_fallback",
            f"Резервная панель заблокирована: основная и резервная панели указывают на один и тот же сервис #{service_number}.",
            "резервная панель заблокирована, потому что основная и резервная панели совпадают",
            False,
        )
    return None


def _execute_fallback_order_submission(
    c: "Cardinal",
    cfg: Dict,
    buyer_chat_id: int,
    order_id_funpay: str,
    panel_link: str,
    link_: str,
    real_amount: int,
    service_number: int,
    fallback_service_id: Any,
    fallback_service_number: int,
    primary_error: Any,
    primary_detail: Any,
    lot_price: float,
    lot_title: str = "",
) -> None:
    fallback_reason = _detect_fallback_reason(primary_error)
    rt.logger.warning("FALLBACK STARTED")
    update_order_fields(
        str(order_id_funpay),
        panel_add_service_number=fallback_service_number,
        fallback_reason=fallback_reason,
        updated_at=rt.now_str(),
    )
    fallback_balance_before = _get_panel_balance_for_risk_check(fallback_service_number)
    ok2, panel_order_id2, payload2 = send_order_to_service(
        c=c,
        service_number=fallback_service_number,
        service_id=fallback_service_id,
        link=panel_link,
        quantity=real_amount,
    )

    if ok2 and panel_order_id2 is not None:
        _finalize_created_order(
            c=c,
            cfg=cfg,
            buyer_chat_id=buyer_chat_id,
            order_id_funpay=order_id_funpay,
            panel_order_id=panel_order_id2,
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
    rt.logger.error(f"Резервная панель попытка тоже провалена ({err2}) | {detail2}")
    if err2 in AMBIGUOUS_ADD_ERRORS:
        _handle_transport_error_after_add(
            c=c,
            order_id_funpay=order_id_funpay,
            buyer_chat_id=buyer_chat_id,
            service_number=fallback_service_number,
            service_id=fallback_service_id,
            real_amount=real_amount,
            link_=link_,
            primary_error=err2,
            primary_detail=detail2,
            balance_before=fallback_balance_before,
            lot_price=lot_price,
            lot_title=lot_title,
            fallback_reason=fallback_reason,
            wait_before_balance_check=True,
        )
        return

    _store_fallback_decision(order_id_funpay, fallback_used=False, fallback_reason=fallback_reason)
    update_order_fields(
        order_id_funpay,
        panel_status=None,
        status_stop_reason="fallback_rejected",
        updated_at=rt.now_str(),
    )
    refund_order(
        c,
        order_id_funpay,
        buyer_chat_id,
        reason=f"{primary_error} на панели #{service_number}, резервная панель не смогла создать заказ на панели #{fallback_service_number}",
        detailed_reason=f"Primary ошибка: {primary_error} | {primary_detail}. Ошибка резервной панели: {err2} | {detail2}.",
        bot_action="автоматическое создание через резервную панель не удалось",
        admin_action="проверьте заказ и при необходимости выполните возврат вручную",
        failure_title="⚠️ Заказ требует ручной проверки",
    )


def _handle_unclaimed_submission(
    c: "Cardinal",
    order_id_funpay: str,
    buyer_chat_id: int,
    claim_reason: str,
    current_order: Optional[Dict[str, Any]],
) -> None:
    with rt.CONTEXT_LOCK:
        rt.waiting_for_link.pop(order_id_funpay, None)
    if buyer_chat_id is None:
        return
    if claim_reason == "refunded":
        _apply_refund_gate(c, current_order or {"order_id": order_id_funpay, "funpay_status": "refunded"}, notify_chat_id=buyer_chat_id, force_check=False)
        return
    if claim_reason == "panel_created":
        _send_buyer_message(c, buyer_chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
        return
    stop_reason = str((current_order or {}).get("status_stop_reason") or "").strip().lower()
    panel_status_val = str((current_order or {}).get("panel_status") or "").strip().lower()
    rt.logger.warning(f"DUPLICATE PANEL ADD BLOCKED: order=#{order_id_funpay}, reason={claim_reason}, stop_reason={stop_reason}")
    if panel_status_val == "manual_check" or stop_reason in ("panel_add_in_flight", "transport_error_after_add", "persist_failed_after_add"):
        _send_buyer_message(c, buyer_chat_id, BUYER_ADD_TRANSPORT_MANUAL_TEXT)
    else:
        _send_buyer_message(c, buyer_chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)


def process_link_without_confirmation(c: "Cardinal", data: Dict) -> None:
    """Submit a buyer's link to the primary SMM panel with atomic claim and safe fallback."""
    link_, service_id, real_amount = data["link"], data["service_id"], data["real_amount"]
    order_id_funpay, buyer_chat_id = data["order_id_funpay"], data["chat_id"]
    service_number, lot_price = data["service_number"], data["price"]
    order_id_str = str(order_id_funpay)

    cfg = load_config()
    fallback_enabled = bool(cfg.get("fallback_enabled", False))
    with rt.get_order_lock(order_id_str):
        existing_order = get_order_data(order_id_str)

        if existing_order and _apply_refund_gate(c, existing_order, notify_chat_id=buyer_chat_id, force_check=True):
            with rt.CONTEXT_LOCK:
                rt.waiting_for_link.pop(order_id_str, None)
            return
        if existing_order and _has_panel_order(existing_order):
            with rt.CONTEXT_LOCK:
                rt.waiting_for_link.pop(order_id_str, None)
            _send_buyer_message(c, buyer_chat_id, BUYER_PANEL_CREATED_BLOCK_TEXT)
            return

        order_lot_key = data.get("lot_key") or (existing_order or {}).get("lot_key")
        order_lot_title = data.get("lot_title") or (existing_order or {}).get("lot_title") or ""
        fallback_service_id, fallback_service_number = _get_lot_fallback_targets(
            cfg,
            service_id,
            service_number,
            lot_key=order_lot_key,
            lot_title=order_lot_title,
        )

        try:
            claimed, claim_reason, current_order = claim_order_for_panel_submission(
                order_id_str,
                saved_link=link_,
                service_number=service_number,
                seed_fields={
                    "chat_id": buyer_chat_id,
                    "buyer_id": data.get("buyer_id"),
                    "buyer_username": data.get("buyer_username") or "",
                    "lot_key": order_lot_key,
                    "lot_title": order_lot_title,
                    "service_id": service_id,
                    "quantity": real_amount,
                    "summa": lot_price,
                },
            )
        except Exception as exc:
            with rt.CONTEXT_LOCK:
                rt.waiting_for_link.pop(order_id_str, None)
            rt.logger.exception(f"PRE-ADD CLAIM FAILED (ORDER ABORTED WITHOUT PANEL CALL): order=#{order_id_str}, error={exc}")
            try:
                resolved_chat_id = _normalize_chat_id(buyer_chat_id)
                if resolved_chat_id is not None:
                    _send_buyer_message(c, resolved_chat_id, BUYER_ADD_TRANSPORT_MANUAL_TEXT)
            except Exception as notify_exc:
                rt.logger.debug(
                    "Pre-add claim buyer notification failed for order #%s: %s",
                    order_id_str,
                    notify_exc,
                )
            return

        with rt.CONTEXT_LOCK:
            rt.waiting_for_link.pop(order_id_str, None)
        if not claimed:
            resolved_chat_id = _normalize_chat_id(buyer_chat_id)
            _handle_unclaimed_submission(c, order_id_str, resolved_chat_id, claim_reason, current_order)
            return

        balance_before = _get_panel_balance_for_risk_check(service_number)
        lot_data = _find_lot_data_for_order(
            cfg,
            service_id,
            service_number,
            lot_title=order_lot_title,
            lot_key=order_lot_key,
        )
        panel_link = _format_target_for_panel(link_, lot_data)

        ok, panel_order_id, payload = send_order_to_service(
            c=c,
            service_number=service_number,
            service_id=service_id,
            link=panel_link,
            quantity=real_amount,
        )
        if ok and panel_order_id is not None:
            rt.logger.info("PRIMARY ADD: SUCCESS")
            _finalize_created_order(
                c=c,
                cfg=cfg,
                buyer_chat_id=buyer_chat_id,
                order_id_funpay=order_id_str,
                panel_order_id=panel_order_id,
                link_=link_,
                real_amount=real_amount,
                service_number=service_number,
                lot_price=lot_price,
            )
            return

        primary_error = payload.get("error", "unknown")
        primary_detail = payload.get("detail", str(payload))
        rt.logger.warning(f"PRIMARY ERROR: {primary_error}")

        if primary_error in AMBIGUOUS_ADD_ERRORS:
            _handle_transport_error_after_add(
                c=c,
                order_id_funpay=order_id_str,
                buyer_chat_id=buyer_chat_id,
                service_number=service_number,
                service_id=service_id,
                real_amount=real_amount,
                link_=link_,
                primary_error=primary_error,
                primary_detail=primary_detail,
                balance_before=balance_before,
                lot_price=lot_price,
                lot_title=order_lot_title,
                wait_before_balance_check=True,
            )
            return

        block_spec = _get_fallback_block_spec(
            primary_error,
            primary_detail,
            service_number,
            fallback_enabled,
            current_order or existing_order,
            fallback_service_id,
            fallback_service_number,
        )
        if block_spec is not None:
            reason_code, detailed_reason, bot_action, log_manual_check = block_spec
            _block_fallback_and_refund(
                c,
                order_id_str,
                buyer_chat_id,
                service_number,
                primary_error,
                reason_code,
                detailed_reason=detailed_reason,
                bot_action=bot_action,
                log_manual_check=log_manual_check,
            )
            return

        _execute_fallback_order_submission(
            c=c,
            cfg=cfg,
            buyer_chat_id=buyer_chat_id,
            order_id_funpay=order_id_str,
            panel_link=panel_link,
            link_=link_,
            real_amount=real_amount,
            service_number=service_number,
            fallback_service_id=fallback_service_id,
            fallback_service_number=int(fallback_service_number),
            primary_error=primary_error,
            primary_detail=primary_detail,
            lot_price=lot_price,
            lot_title=order_lot_title,
        )
