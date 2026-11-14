"""Response classification, error mapping, balance parsing, and secret redaction for SMM panel APIs."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..models import safe_int as _safe_int
from .links import ZERO_WIDTH_INVISIBLE_CHARS

LOGGER_PREFIX = "[AUTO autosmm]"
logger = logging.getLogger("FPC.autosmm")

_SECRET_QUERY_PARAM_RE = re.compile(r"([?&](?:key|api_key|token)=)([^&\s'\"\\)]+)", re.IGNORECASE)

EXPECTED_API_KEY_LENGTH = 60
MIN_API_KEY_LENGTH = 8
MAX_API_KEY_LENGTH = 256
PLACEHOLDER_API_KEYS = {
    "your_api_key",
    "your-api-key",
    "api_key",
    "apikey",
    "xxx",
    "changeme",
    "insert_api_key_here",
}


def _normalize_api_key(api_key: Any) -> str:
    value = str(api_key or "")
    value = value.translate(ZERO_WIDTH_INVISIBLE_CHARS)
    value = value.strip().strip("\"'").strip()
    value = re.sub(r"^(?:api[\s_]*key|ключ)\s*[:=]\s*", "", value, flags=re.IGNORECASE).strip()
    return value


def _sanitize_secret_text(text: Any, secret_value: Optional[str] = None) -> str:
    cleaned = _SECRET_QUERY_PARAM_RE.sub(r"\1***", str(text or ""))
    secret = _normalize_api_key(secret_value)
    if secret and secret in cleaned:
        masked = f"{secret[:4]}***" if len(secret) > 8 else "***"
        cleaned = cleaned.replace(secret, masked)
    return cleaned


def _stringify_for_matching(value: Any) -> str:
    try:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.debug(f"Не удалось сериализовать ответ панели для матчинга: {exc}")
    return str(value or "")


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
        "incorrect service id",
        "wrong service id",
    ]):
        return "service_inactive"

    if has_any([
        "insufficient_funds",
        "insufficient funds",
        "not enough funds",
        "not_enough_funds",
        "not enough balance",
        "not enough money",
        "insufficient balance",
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
    ]):
        return "user_inactive"

    if has_any([
        "rate_limited",
        "rate limit",
        "too many requests",
        "too_many_requests",
    ]):
        return "rate_limited"

    if has_any([
        "validation_error",
        "validation error",
        "invalid link",
        "invalid url",
        "invalid quantity",
        "incorrect quantity",
        "invalid format",
        "required parameter",
        "parameter is required",
        "missing parameter",
        "less than minimum",
        "more than maximum",
        "min quantity",
        "max quantity",
    ]):
        return "validation_error"

    return None


def _classify_local_or_exception_result(
    payload: Any,
    http_status: Optional[int],
    exception: Optional[Exception],
    local_status: Optional[str],
    local_detail: Optional[str],
    secret_value: Optional[str] = None,
) -> Optional[Dict]:
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
            "detail": _sanitize_secret_text(local_detail or "Локальная ошибка конфигурации.", secret_value),
            "response": payload,
            "http_status": http_status,
        }

    if exception is not None:
        error_code = "timeout" if isinstance(exception, requests.exceptions.Timeout) else "network_error"
        return {
            "error": error_code,
            "error_class": "TRANSPORT_ERROR",
            "detail": _sanitize_secret_text(exception, secret_value),
            "response": payload,
            "http_status": http_status,
        }
    return None


def _classify_panel_payload_result(
    action: str,
    payload: Any,
    raw_text: str,
    text_for_match: str,
    http_status: Optional[int],
    invalid_json: bool,
    secret_value: Optional[str] = None,
) -> Dict:
    if (
        action == "add"
        and isinstance(payload, dict)
        and payload.get("order") not in (None, "", False)
        and not payload.get("error")
        and (http_status is None or http_status < 400)
    ):
        return {
            "error": "success",
            "error_class": None,
            "detail": "OK",
            "response": payload,
            "http_status": http_status,
        }

    if invalid_json:
        if http_status == 429:
            return {
                "error": "rate_limited",
                "error_class": "TRANSPORT_ERROR",
                "detail": _sanitize_secret_text(raw_text or "HTTP 429", secret_value),
                "response": raw_text,
                "http_status": http_status,
            }
        return {
            "error": "invalid_json",
            "error_class": "TRANSPORT_ERROR",
            "detail": _sanitize_secret_text(raw_text or "Не удалось разобрать JSON-ответ панели.", secret_value),
            "response": raw_text,
            "http_status": http_status,
        }

    if http_status == 429:
        return {
            "error": "rate_limited",
            "error_class": "TRANSPORT_ERROR",
            "detail": _sanitize_secret_text(text_for_match or "HTTP 429", secret_value),
            "response": payload if payload is not None else raw_text,
            "http_status": http_status,
        }

    if http_status is not None and http_status >= 400:
        if http_status < 500 and isinstance(payload, dict) and payload.get("error"):
            mapped_4xx = _map_panel_status_from_text(_stringify_for_matching(payload))
            if mapped_4xx:
                return {
                    "error": mapped_4xx,
                    "error_class": "TRANSPORT_ERROR" if mapped_4xx == "rate_limited" else "PANEL_ERROR",
                    "detail": _sanitize_secret_text(text_for_match or mapped_4xx, secret_value),
                    "response": payload,
                    "http_status": http_status,
                }
        return {
            "error": "http_error",
            "error_class": "TRANSPORT_ERROR",
            "detail": f"HTTP {http_status}",
            "response": payload if payload is not None else raw_text,
            "http_status": http_status,
        }

    mapped_status = _map_panel_status_from_text(text_for_match)
    if mapped_status:
        return {
            "error": mapped_status,
            "error_class": "TRANSPORT_ERROR" if mapped_status == "rate_limited" else "PANEL_ERROR",
            "detail": _sanitize_secret_text(text_for_match or mapped_status, secret_value),
            "response": payload if payload is not None else raw_text,
            "http_status": http_status,
        }

    if action in ("status", "refill") and payload is not None and not (isinstance(payload, dict) and payload.get("error")):
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
        "detail": _sanitize_secret_text(text_for_match or "Неизвестный ответ панели.", secret_value),
        "response": payload if payload is not None else raw_text,
        "http_status": http_status,
    }


def _classify_panel_api_result(
    action: str,
    response: Optional[requests.Response] = None,
    payload: Any = None,
    raw_text: str = "",
    exception: Optional[Exception] = None,
    invalid_json: bool = False,
    local_status: Optional[str] = None,
    local_detail: Optional[str] = None,
    secret_value: Optional[str] = None,
) -> Dict:
    http_status = response.status_code if response is not None else None
    pre_classified = _classify_local_or_exception_result(
        payload=payload,
        http_status=http_status,
        exception=exception,
        local_status=local_status,
        local_detail=local_detail,
        secret_value=secret_value,
    )
    if pre_classified is not None:
        return pre_classified

    text_for_match = " ".join(part for part in [
        _stringify_for_matching(payload),
        _stringify_for_matching(raw_text),
    ] if part).strip()

    return _classify_panel_payload_result(
        action=action,
        payload=payload,
        raw_text=raw_text,
        text_for_match=text_for_match,
        http_status=http_status,
        invalid_json=invalid_json,
        secret_value=secret_value,
    )


def _is_placeholder_api_key(api_key: Any) -> bool:
    key = _normalize_api_key(api_key)
    return (not key) or (key.lower() in PLACEHOLDER_API_KEYS)


def _build_invalid_api_key_payload(service_number: int, action: str, api_key: Any) -> Dict:
    key = _normalize_api_key(api_key)
    key_len = len(key)
    if _is_placeholder_api_key(key):
        detail = (
            f"API-ключ не задан для service_number={service_number} перед action={action}."
        )
    elif key_len < MIN_API_KEY_LENGTH:
        detail = (
            f"API-ключ слишком короткий для service_number={service_number} перед action={action}: "
            f"{key_len} символов."
        )
    elif key_len > MAX_API_KEY_LENGTH:
        detail = (
            f"API-ключ слишком длинный для service_number={service_number} перед action={action}: "
            f"{key_len} символов."
        )
    else:
        detail = (
            f"Некорректный API-ключ для service_number={service_number} перед action={action}."
        )
    logger.warning(f"{LOGGER_PREFIX} {detail}")
    return {
        "error": "invalid_api_key",
        "detail": detail,
        "expected_length": EXPECTED_API_KEY_LENGTH,
        "actual_length": key_len,
    }


def _validate_api_key_for_action(service_number: int, action: str, api_key: Any) -> Optional[Dict]:
    key = _normalize_api_key(api_key)
    if _is_placeholder_api_key(key):
        return _build_invalid_api_key_payload(service_number, action, api_key)
    if len(key) < MIN_API_KEY_LENGTH or len(key) > MAX_API_KEY_LENGTH:
        return _build_invalid_api_key_payload(service_number, action, api_key)
    return None


def _extract_order_id_from_obj(obj: Any) -> Optional[int]:
    if isinstance(obj, dict):
        for key in ("order", "id", "order_id"):
            order_id = _safe_int(obj.get(key))
            if order_id is not None:
                return order_id
    return None


def parse_panel_balance_value(raw_value: Any) -> float:
    """Parse panel balance strictly, rejecting None, blanks, booleans, and non-finite numbers."""
    if raw_value is None or isinstance(raw_value, bool):
        raise ValueError("панель не вернула balance")
    cleaned = str(raw_value).strip().replace(",", ".")
    if not cleaned:
        raise ValueError("панель вернула пустой balance")
    value = float(cleaned)
    if not math.isfinite(value):
        raise ValueError("панель вернула некорректный balance")
    return value


def _normalize_runtime_status(status_value: Any) -> str:
    haystack = str(status_value or "").strip().lower()
    if not haystack:
        return "unknown"
    if "partial" in haystack:
        return "partial"
    if "completed" in haystack:
        return "completed"
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


def _format_balance_risk_value(balance_payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(balance_payload, dict) or not balance_payload.get("ok"):
        return "недоступно"
    return str(balance_payload.get("text") or "недоступно")


def _build_transport_error_balance_note(
    balance_before: Optional[Dict[str, Any]],
    balance_after: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    before_ok = isinstance(balance_before, dict) and balance_before.get("ok")
    after_ok = isinstance(balance_after, dict) and balance_after.get("ok")
    before_text = _format_balance_risk_value(balance_before)
    after_text = _format_balance_risk_value(balance_after)

    if before_ok and after_ok and str(balance_before.get("currency")) == str(balance_after.get("currency")):
        try:
            before_val = float(balance_before.get("balance"))
            after_val = float(balance_after.get("balance"))
        except (TypeError, ValueError):
            before_val = None
            after_val = None
        if before_val is not None and after_val is not None and math.isfinite(before_val) and math.isfinite(after_val):
            delta = after_val - before_val
            delta_text = f"{delta:.2f} {str(balance_after.get('currency') or '').upper()}".strip()
            if after_val < before_val:
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
