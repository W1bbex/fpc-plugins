"""HTTP client and response classification helpers for PerfectPanel/Peakerr SMM APIs."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..models import safe_int as _safe_int
from ..storage.storage import load_config
from .links import ZERO_WIDTH_INVISIBLE_CHARS

LOGGER_PREFIX = "[AUTO autosmm]"
logger = logging.getLogger("FPC.autosmm")
PANEL_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

_SECRET_QUERY_PARAM_RE = re.compile(r"([?&](?:key|api_key|token)=)([^&\s'\"\\)]+)", re.IGNORECASE)


def _normalize_api_key(api_key: Any) -> str:
    value = str(api_key or "")
    value = value.translate(ZERO_WIDTH_INVISIBLE_CHARS)
    value = value.strip().strip("\"'").strip()
    value = re.sub(r"^(?:api\s*key|ключ)\s*[:=]\s*", "", value, flags=re.IGNORECASE).strip()
    return value


def _sanitize_secret_text(text: Any, secret_value: Optional[str] = None) -> str:
    cleaned = _SECRET_QUERY_PARAM_RE.sub(r"\1***", str(text or ""))
    secret = _normalize_api_key(secret_value)
    if secret and secret in cleaned:
        masked = f"{secret[:4]}***" if len(secret) > 8 else "***"
        cleaned = cleaned.replace(secret, masked)
    return cleaned


def _stringify_panel_params(params: Optional[Dict[str, Any]]) -> Dict[str, str]:
    encoded: Dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        encoded[str(key)] = str(value)
    return encoded


def _decode_panel_response_payload(response: Optional[requests.Response]) -> Tuple[Any, str, bool]:
    raw_text = response.text if response is not None else ""
    if response is None:
        return None, raw_text, True
    try:
        return response.json(), raw_text, False
    except ValueError:
        text = (raw_text or "").strip()
        if not text:
            return None, raw_text, True
        try:
            return json.loads(text), raw_text, False
        except ValueError:
            return None, raw_text, True


def _panel_response_looks_like_html(raw_text: str) -> bool:
    snippet = str(raw_text or "").lstrip()[:240].lower()
    return (
        snippet.startswith("<!doctype")
        or snippet.startswith("<html")
        or snippet.startswith("<head")
        or snippet.startswith("<body")
    )


def _should_retry_panel_via_get(
    action: str,
    response: Optional[requests.Response],
    payload: Any,
    invalid_json: bool,
    post_exception: Optional[Exception],
) -> bool:
    # Never retry action=add except when the panel clearly rejected POST as a method.
    # Timeout / ConnectionError / HTML after POST can mean the order already exists.
    if action == "add":
        if post_exception is not None or response is None:
            return False
        return response.status_code in (405, 501)
    if post_exception is not None:
        return True
    if response is None:
        return True
    if response.status_code in (404, 405, 501):
        return True
    if _panel_response_looks_like_html(response.text or ""):
        return True
    if invalid_json and payload is None:
        return True
    return False


def _panel_api_request(
    api_url: str,
    params: Dict[str, Any],
    timeout: int = 15,
) -> requests.Response:
    """Call a PerfectPanel / SocPanel v2 endpoint.

    Official method is POST application/x-www-form-urlencoded (Peakerr, PerfectPanel,
    SocPanel PHP examples). GET is a compatibility fallback for older panels.
    """
    url = str(api_url or "").strip()
    if not url:
        raise ValueError("не указан API URL панели")
    data = _stringify_panel_params(params)
    action = str(data.get("action") or "")
    post_error: Optional[Exception] = None
    response: Optional[requests.Response] = None
    payload: Any = None
    invalid_json = True

    try:
        response = requests.post(url, data=data, headers=PANEL_API_HEADERS, timeout=timeout)
        payload, _raw_text, invalid_json = _decode_panel_response_payload(response)
        if not _should_retry_panel_via_get(action, response, payload, invalid_json, None):
            return response
        logger.info(
            f"{LOGGER_PREFIX} PANEL API POST fallback to GET: action={action}, "
            f"http={getattr(response, 'status_code', None)}"
        )
    except requests.exceptions.RequestException as ex:
        post_error = ex
        if not _should_retry_panel_via_get(action, None, None, True, ex):
            raise
        logger.info(
            f"{LOGGER_PREFIX} PANEL API POST failed, trying GET: action={action}, "
            f"error={_sanitize_secret_text(ex, data.get('key'))}"
        )

    try:
        return requests.get(url, params=data, headers=PANEL_API_HEADERS, timeout=timeout)
    except requests.exceptions.RequestException:
        if post_error is not None:
            raise post_error
        raise


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


def _panel_api_json(
    api_url: str,
    params: Dict[str, Any],
    timeout: int = 15,
) -> Tuple[requests.Response, Any, str, bool]:
    action = (params or {}).get("action")
    masked_params = dict(params or {})
    if "key" in masked_params:
        raw_key = str(masked_params["key"] or "")
        masked_params["key"] = f"{raw_key[:4]}***" if len(raw_key) > 4 else "***"
    logger.debug(f"[PANEL REQ] url={api_url}, action={action}, params={masked_params}")
    response = _panel_api_request(api_url, params, timeout=timeout)
    payload, raw_text, invalid_json = _decode_panel_response_payload(response)
    http_code = getattr(response, "status_code", None)
    logger.debug(f"[PANEL RESP] http={http_code}, action={action}, invalid_json={invalid_json}, body={str(raw_text or '')[:300]}")
    return response, payload, raw_text, invalid_json


def _stringify_for_matching(value: Any) -> str:
    try:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.debug(f"Не удалось сериализовать ответ панели для матчинга: {exc}")
    return str(value or "")


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
        return {
            "error": "invalid_json",
            "error_class": "TRANSPORT_ERROR",
            "detail": raw_text or "Не удалось разобрать JSON-ответ панели.",
            "response": raw_text,
            "http_status": http_status,
        }

    if http_status is not None and http_status >= 400:
        if http_status < 500 and isinstance(payload, dict) and payload.get("error"):
            mapped_4xx = _map_panel_status_from_text(_stringify_for_matching(payload))
            if mapped_4xx:
                return {
                    "error": mapped_4xx,
                    "error_class": "PANEL_ERROR",
                    "detail": text_for_match or mapped_4xx,
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
            "error_class": "PANEL_ERROR",
            "detail": text_for_match or mapped_status,
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
    )


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


def _extract_service_credentials(config: Any, service_number: Any) -> Tuple[Optional[str], str, Optional[str]]:
    cfg = _provided_config(config)
    services = cfg.get("services") if isinstance(cfg, dict) else None
    service_cfg = services.get(str(service_number)) if isinstance(services, dict) else None
    if not isinstance(service_cfg, dict):
        return None, "", f"Нет настроек для service_number={service_number}"
    api_url = str(service_cfg.get("api_url") or "").strip()
    if not api_url:
        return None, "", f"Не указан api_url для service_number={service_number}"
    api_key = _normalize_api_key(service_cfg.get("api_key"))
    return api_url, api_key, None


def _panel_send_order(c: Cardinal, service_number: int, service_id: int, link: str, quantity: int, api_timeout: int=20, *, config) -> Tuple[bool, int | None, Dict]:
    """
    Отправляет заказ в выбранный SMM-сервис.

    Возвращает:
    - ok: bool
    - panel_order_id: int | None
    - payload: dict с нормализованным результатом
    """
    api_url, api_key, config_err = _extract_service_credentials(config, service_number)
    if config_err or not api_url:
        return (False, None, {"error": "service_inactive", "error_class": "PANEL_ERROR", "detail": config_err or f"Нет настроек для service_number={service_number}", "response": None, "http_status": None})
    invalid_key_payload = _validate_api_key_for_action(service_number, 'add', api_key)
    if invalid_key_payload:
        classified = _classify_panel_api_result(action='add', payload=invalid_key_payload, local_status='user_inactive', local_detail=invalid_key_payload.get('detail'), secret_value=api_key)
        return (False, None, classified)
    try:
        resp_, j_, raw_text, invalid_json = _panel_api_json(api_url, {'key': api_key, 'action': 'add', 'service': service_id, 'link': link, 'quantity': quantity}, timeout=api_timeout)
        classified = _classify_panel_api_result(action='add', response=resp_, payload=j_, raw_text=raw_text, invalid_json=invalid_json, secret_value=api_key)
        if classified['error'] == 'success':
            order_id = _extract_order_id_from_obj(j_)
            if order_id is None:
                try:
                    order_id = int(j_["order"])
                except (KeyError, ValueError, TypeError):
                    order_id = None
            http_status = getattr(resp_, 'status_code', None)
            if order_id is not None:
                return (True, order_id, {'error': 'success', 'error_class': None, 'detail': 'OK', 'response': j_, 'http_status': http_status})
            return (False, None, {'error': 'unknown', 'error_class': 'UNKNOWN_ERROR', 'detail': _sanitize_secret_text(f'Не удалось распарсить order из ответа панели: {j_}', api_key), 'response': j_, 'http_status': http_status})
        return (False, None, classified)
    except requests.exceptions.RequestException as ex:
        return (False, None, _classify_panel_api_result(action='add', exception=ex, secret_value=api_key))
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        return (False, None, {'error': 'unknown', 'error_class': 'UNKNOWN_ERROR', 'detail': _sanitize_secret_text(ex, api_key), 'response': None, 'http_status': None})


def _panel_single_status(service_number: int, panel_order_id: int, timeout: int=15, *, config) -> Dict:
    api_url, api_key, config_err = _extract_service_credentials(config, service_number)
    if config_err or not api_url:
        return {'ok': False, 'error': 'service_inactive', 'detail': config_err or f'Нет настроек для service_number={service_number}'}
    invalid_key_payload = _validate_api_key_for_action(service_number, 'status', api_key)
    if invalid_key_payload:
        return {'ok': False, 'error': 'user_inactive', 'detail': invalid_key_payload.get('detail', 'Некорректный API-ключ.')}
    try:
        response, payload, raw_text, invalid_json = _panel_api_json(api_url, {'key': api_key, 'action': 'status', 'order': panel_order_id}, timeout=timeout)
        http_status = getattr(response, 'status_code', None)
        text_for_check = ' '.join((part for part in [_stringify_for_matching(payload), _stringify_for_matching(raw_text)] if part)).strip()
        if not invalid_json and (http_status is None or http_status < 500) and _is_incorrect_order_id_text(text_for_check):
            return {'ok': False, 'error': 'incorrect_order_id', 'detail': text_for_check or 'Incorrect order ID', 'raw': payload if payload is not None else raw_text}
        classified = _classify_panel_api_result(action='status', response=response, payload=payload, raw_text=raw_text, invalid_json=invalid_json, secret_value=api_key)
        if classified.get('error') != 'success':
            logger.error(f"STATUS CHECK ERROR: service=#{service_number}, order={panel_order_id}, error={classified.get('error')}, detail={_sanitize_secret_text(classified.get('detail'), api_key)}")
            return {'ok': False, 'error': classified.get('error', 'unknown'), 'detail': _sanitize_secret_text(classified.get('detail', 'Не удалось подтвердить состояние заказа.'), api_key), 'raw': classified.get('response')}
        return _build_status_result_from_entry(payload)
    except requests.exceptions.Timeout as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f'STATUS CHECK TIMEOUT: service=#{service_number}, order={panel_order_id}, error={safe_ex}')
        return {'ok': False, 'error': 'timeout', 'detail': safe_ex}
    except requests.exceptions.RequestException as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f'STATUS CHECK NETWORK ERROR: service=#{service_number}, order={panel_order_id}, error={safe_ex}')
        return {'ok': False, 'error': 'network_error', 'detail': safe_ex}
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f'STATUS CHECK UNKNOWN ERROR: service=#{service_number}, order={panel_order_id}, error={safe_ex}')
        return {'ok': False, 'error': 'unknown', 'detail': safe_ex}


def _panel_batch_status(service_number: int, order_ids: List[int], timeout: int=20, *, config) -> Dict:
    api_url, api_key, config_err = _extract_service_credentials(config, service_number)
    if config_err or not api_url:
        return {'ok': False, 'error': 'service_inactive', 'detail': config_err or f'Нет настроек для service_number={service_number}'}
    invalid_key_payload = _validate_api_key_for_action(service_number, 'status', api_key)
    if invalid_key_payload:
        return {'ok': False, 'error': 'user_inactive', 'detail': invalid_key_payload.get('detail', 'Некорректный API-ключ.')}
    orders_param = ','.join((str(order_id) for order_id in order_ids))
    try:
        response, payload, raw_text, invalid_json = _panel_api_json(api_url, {'key': api_key, 'action': 'status', 'orders': orders_param}, timeout=timeout)
        http_status = getattr(response, 'status_code', None)
        entries = _extract_batch_status_entries(payload, order_ids)
        if entries:
            return {'ok': True, 'entries': entries}
        text_for_check = ' '.join((part for part in [_stringify_for_matching(payload), _stringify_for_matching(raw_text)] if part)).strip()
        if not invalid_json and (http_status is None or http_status < 500) and _is_incorrect_order_id_text(text_for_check):
            return {'ok': False, 'error': 'incorrect_order_id', 'detail': text_for_check or 'Incorrect order ID', 'raw': payload if payload is not None else raw_text}
        classified = _classify_panel_api_result(action='status', response=response, payload=payload, raw_text=raw_text, invalid_json=invalid_json, secret_value=api_key)
        if classified.get('error') != 'success':
            logger.error(f"BATCH STATUS ERROR: service=#{service_number}, orders={orders_param}, error={classified.get('error')}, detail={_sanitize_secret_text(classified.get('detail'), api_key)}")
            return {'ok': False, 'error': classified.get('error', 'unknown'), 'detail': _sanitize_secret_text(classified.get('detail', 'Не удалось обработать batch status ответ.'), api_key), 'raw': classified.get('response')}
        fallback_result = _build_status_result_from_entry(payload)
        return {'ok': False, 'error': fallback_result.get('error', 'unknown'), 'detail': _sanitize_secret_text(fallback_result.get('detail', 'Не удалось обработать batch status ответ.'), api_key), 'raw': payload}
    except requests.exceptions.Timeout as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f'BATCH STATUS TIMEOUT: service=#{service_number}, orders={orders_param}, error={safe_ex}')
        return {'ok': False, 'error': 'timeout', 'detail': safe_ex}
    except requests.exceptions.RequestException as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f'BATCH STATUS NETWORK ERROR: service=#{service_number}, orders={orders_param}, error={safe_ex}')
        return {'ok': False, 'error': 'network_error', 'detail': safe_ex}
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f'BATCH STATUS UNKNOWN ERROR: service=#{service_number}, orders={orders_param}, error={safe_ex}')
        return {'ok': False, 'error': 'unknown', 'detail': safe_ex}


def _provided_config(config):
    return config


def send_order_to_service(c, service_number: int, service_id: int, link: str, quantity: int, api_timeout: int = 20):
    return _panel_send_order(c, service_number, service_id, link, quantity, api_timeout, config=load_config())


def _fetch_single_order_status(service_number: int, panel_order_id: int, timeout: int = 15) -> Dict:
    return _panel_single_status(service_number, panel_order_id, timeout, config=load_config())


def _fetch_batch_order_status(service_number: int, order_ids: List[int], timeout: int = 20) -> Dict:
    return _panel_batch_status(service_number, order_ids, timeout, config=load_config())


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
        _response, payload, _raw_text, invalid_json = _panel_api_json(
            api_url,
            {"key": api_key, "action": "balance"},
            timeout=6,
        )
        if invalid_json or not isinstance(payload, dict) or "balance" not in payload:
            raise ValueError("панель не вернула balance")
        balance_value = parse_panel_balance_value(payload.get("balance"))
        currency = str(payload.get("currency") or "RUB").upper().strip() or "RUB"
        return {
            "ok": True,
            "balance": balance_value,
            "currency": currency,
            "text": f"{balance_value:.2f} {currency}",
        }
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.warning(f"BALANCE RISK CHECK FAILED: service=#{service_number}, error={safe_ex}")
        return {"ok": False, "error": safe_ex[:120], "text": "недоступно"}


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
            delta = float(balance_after.get("balance") or 0) - float(balance_before.get("balance") or 0)
        except (TypeError, ValueError):
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
