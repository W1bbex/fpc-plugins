"""HTTP transport client and order/status operations for PerfectPanel/Peakerr SMM APIs."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..models import PanelServiceConfig, normalize_api_url
from ..storage.storage import load_config
from .panel_errors import (
    _build_status_result_from_entry,
    _classify_panel_api_result,
    _extract_batch_status_entries,
    _extract_order_id_from_obj,
    _is_incorrect_order_id_text,
    _normalize_api_key,
    _sanitize_secret_text,
    _stringify_for_matching,
    _validate_api_key_for_action,
    parse_panel_balance_value,
)

LOGGER_PREFIX = "[AUTO autosmm]"
logger = logging.getLogger("FPC.autosmm")
PANEL_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


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
    raw_url = str(api_url or "").strip()
    if not raw_url:
        raise ValueError("не указан API URL панели")
    url = normalize_api_url(raw_url, check_dns=True)
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


def _panel_api_json(
    api_url: str,
    params: Dict[str, Any],
    timeout: int = 15,
) -> Tuple[requests.Response, Any, str, bool]:
    action = (params or {}).get("action")
    raw_key = str((params or {}).get("key") or "")
    masked_params = dict(params or {})
    if "key" in masked_params:
        masked_params["key"] = f"{raw_key[:4]}***" if len(raw_key) > 4 else "***"
    safe_url = _sanitize_secret_text(api_url, raw_key)
    logger.debug(f"[PANEL REQ] url={safe_url}, action={action}, params={masked_params}")
    response = _panel_api_request(api_url, params, timeout=timeout)
    payload, raw_text, invalid_json = _decode_panel_response_payload(response)
    http_code = getattr(response, "status_code", None)
    safe_body = _sanitize_secret_text(str(raw_text or "")[:600], raw_key)[:300]
    logger.debug(f"[PANEL RESP] http={http_code}, action={action}, invalid_json={invalid_json}, body={safe_body}")
    return response, payload, raw_text, invalid_json


def _extract_service_credentials(config: Any, service_number: Any) -> Tuple[Optional[str], str, Optional[str]]:
    cfg = config
    services = cfg.get("services") if isinstance(cfg, dict) else None
    service_raw = services.get(str(service_number)) if isinstance(services, dict) else None
    if not isinstance(service_raw, (dict, PanelServiceConfig)):
        return None, "", f"Нет настроек для service_number={service_number}"
    service_cfg = PanelServiceConfig.from_dict(service_raw) if isinstance(service_raw, dict) else service_raw
    if not service_cfg.api_url:
        return None, "", f"Не указан api_url для service_number={service_number}"
    try:
        api_url = normalize_api_url(service_cfg.api_url)
    except ValueError as ex:
        return None, "", f"Некорректный api_url для service_number={service_number}: {ex}"
    api_key = _normalize_api_key(service_cfg.api_key)
    return api_url, api_key, None


def _panel_send_order(
    _c: Any,
    service_number: int,
    service_id: int,
    link: str,
    quantity: int,
    api_timeout: int = 20,
    *,
    config,
) -> Tuple[bool, int | None, Dict]:
    """Send an order to a configured SMM panel service."""
    api_url, api_key, config_err = _extract_service_credentials(config, service_number)
    if config_err or not api_url:
        return (
            False,
            None,
            {
                "error": "service_inactive",
                "error_class": "PANEL_ERROR",
                "detail": config_err or f"Нет настроек для service_number={service_number}",
                "response": None,
                "http_status": None,
            },
        )
    invalid_key_payload = _validate_api_key_for_action(service_number, "add", api_key)
    if invalid_key_payload:
        classified = _classify_panel_api_result(
            action="add",
            payload=invalid_key_payload,
            local_status="user_inactive",
            local_detail=invalid_key_payload.get("detail"),
            secret_value=api_key,
        )
        return (False, None, classified)
    try:
        resp_, j_, raw_text, invalid_json = _panel_api_json(
            api_url,
            {"key": api_key, "action": "add", "service": service_id, "link": link, "quantity": quantity},
            timeout=api_timeout,
        )
        classified = _classify_panel_api_result(
            action="add",
            response=resp_,
            payload=j_,
            raw_text=raw_text,
            invalid_json=invalid_json,
            secret_value=api_key,
        )
        if classified["error"] == "success":
            order_id = _extract_order_id_from_obj(j_)
            if order_id is None:
                try:
                    order_id = int(j_["order"])
                except (KeyError, ValueError, TypeError):
                    order_id = None
            http_status = getattr(resp_, "status_code", None)
            if order_id is not None:
                return (
                    True,
                    order_id,
                    {"error": "success", "error_class": None, "detail": "OK", "response": j_, "http_status": http_status},
                )
            return (
                False,
                None,
                {
                    "error": "unknown",
                    "error_class": "UNKNOWN_ERROR",
                    "detail": _sanitize_secret_text(f"Не удалось распарсить order из ответа панели: {j_}", api_key),
                    "response": j_,
                    "http_status": http_status,
                },
            )
        return (False, None, classified)
    except requests.exceptions.RequestException as ex:
        return (False, None, _classify_panel_api_result(action="add", exception=ex, secret_value=api_key))
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        return (
            False,
            None,
            {
                "error": "unknown",
                "error_class": "UNKNOWN_ERROR",
                "detail": _sanitize_secret_text(ex, api_key),
                "response": None,
                "http_status": None,
            },
        )


def _panel_single_status(service_number: int, panel_order_id: int, timeout: int = 15, *, config) -> Dict:
    api_url, api_key, config_err = _extract_service_credentials(config, service_number)
    if config_err or not api_url:
        return {"ok": False, "error": "service_inactive", "detail": config_err or f"Нет настроек для service_number={service_number}"}
    invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
    if invalid_key_payload:
        return {"ok": False, "error": "user_inactive", "detail": invalid_key_payload.get("detail", "Некорректный API-ключ.")}
    try:
        response, payload, raw_text, invalid_json = _panel_api_json(
            api_url,
            {"key": api_key, "action": "status", "order": panel_order_id},
            timeout=timeout,
        )
        http_status = getattr(response, "status_code", None)
        text_for_check = " ".join((part for part in [_stringify_for_matching(payload), _stringify_for_matching(raw_text)] if part)).strip()
        if not invalid_json and (http_status is None or http_status < 500) and _is_incorrect_order_id_text(text_for_check):
            return {
                "ok": False,
                "error": "incorrect_order_id",
                "detail": _sanitize_secret_text(text_for_check or "Incorrect order ID", api_key),
                "raw": payload if payload is not None else raw_text,
            }
        classified = _classify_panel_api_result(
            action="status",
            response=response,
            payload=payload,
            raw_text=raw_text,
            invalid_json=invalid_json,
            secret_value=api_key,
        )
        if classified.get("error") != "success":
            logger.error(
                f"STATUS CHECK ERROR: service=#{service_number}, order={panel_order_id}, "
                f"error={classified.get('error')}, detail={_sanitize_secret_text(classified.get('detail'), api_key)}"
            )
            return {
                "ok": False,
                "error": classified.get("error", "unknown"),
                "detail": _sanitize_secret_text(classified.get("detail", "Не удалось подтвердить состояние заказа."), api_key),
                "raw": classified.get("response"),
            }
        return _build_status_result_from_entry(payload)
    except requests.exceptions.Timeout as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f"STATUS CHECK TIMEOUT: service=#{service_number}, order={panel_order_id}, error={safe_ex}")
        return {"ok": False, "error": "timeout", "detail": safe_ex}
    except requests.exceptions.RequestException as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f"STATUS CHECK NETWORK ERROR: service=#{service_number}, order={panel_order_id}, error={safe_ex}")
        return {"ok": False, "error": "network_error", "detail": safe_ex}
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f"STATUS CHECK UNKNOWN ERROR: service=#{service_number}, order={panel_order_id}, error={safe_ex}")
        return {"ok": False, "error": "unknown", "detail": safe_ex}


def _panel_batch_status(service_number: int, order_ids: List[int], timeout: int = 20, *, config) -> Dict:
    api_url, api_key, config_err = _extract_service_credentials(config, service_number)
    if config_err or not api_url:
        return {"ok": False, "error": "service_inactive", "detail": config_err or f"Нет настроек для service_number={service_number}"}
    invalid_key_payload = _validate_api_key_for_action(service_number, "status", api_key)
    if invalid_key_payload:
        return {"ok": False, "error": "user_inactive", "detail": invalid_key_payload.get("detail", "Некорректный API-ключ.")}
    orders_param = ",".join((str(order_id) for order_id in order_ids))
    try:
        response, payload, raw_text, invalid_json = _panel_api_json(
            api_url,
            {"key": api_key, "action": "status", "orders": orders_param},
            timeout=timeout,
        )
        http_status = getattr(response, "status_code", None)
        entries = _extract_batch_status_entries(payload, order_ids)
        if entries:
            return {"ok": True, "entries": entries}
        text_for_check = " ".join((part for part in [_stringify_for_matching(payload), _stringify_for_matching(raw_text)] if part)).strip()
        if not invalid_json and (http_status is None or http_status < 500) and _is_incorrect_order_id_text(text_for_check):
            return {
                "ok": False,
                "error": "incorrect_order_id",
                "detail": _sanitize_secret_text(text_for_check or "Incorrect order ID", api_key),
                "raw": payload if payload is not None else raw_text,
            }
        classified = _classify_panel_api_result(
            action="status",
            response=response,
            payload=payload,
            raw_text=raw_text,
            invalid_json=invalid_json,
            secret_value=api_key,
        )
        if classified.get("error") != "success":
            logger.error(
                f"BATCH STATUS ERROR: service=#{service_number}, orders={orders_param}, "
                f"error={classified.get('error')}, detail={_sanitize_secret_text(classified.get('detail'), api_key)}"
            )
            return {
                "ok": False,
                "error": classified.get("error", "unknown"),
                "detail": _sanitize_secret_text(classified.get("detail", "Не удалось обработать batch status ответ."), api_key),
                "raw": classified.get("response"),
            }
        fallback_result = _build_status_result_from_entry(payload)
        return {
            "ok": False,
            "error": fallback_result.get("error", "unknown"),
            "detail": _sanitize_secret_text(fallback_result.get("detail", "Не удалось обработать batch status ответ."), api_key),
            "raw": payload,
        }
    except requests.exceptions.Timeout as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f"BATCH STATUS TIMEOUT: service=#{service_number}, orders={orders_param}, error={safe_ex}")
        return {"ok": False, "error": "timeout", "detail": safe_ex}
    except requests.exceptions.RequestException as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f"BATCH STATUS NETWORK ERROR: service=#{service_number}, orders={orders_param}, error={safe_ex}")
        return {"ok": False, "error": "network_error", "detail": safe_ex}
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as ex:
        safe_ex = _sanitize_secret_text(ex, api_key)
        logger.error(f"BATCH STATUS UNKNOWN ERROR: service=#{service_number}, orders={orders_param}, error={safe_ex}")
        return {"ok": False, "error": "unknown", "detail": safe_ex}


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

    raw_url = str(service_cfg.get("api_url") or "").strip()
    api_key = _normalize_api_key(service_cfg.get("api_key"))
    if not raw_url or not api_key:
        return {"ok": False, "error": "credentials_missing", "text": "недоступно"}
    try:
        api_url = normalize_api_url(raw_url)
    except ValueError as ex:
        logger.warning(f"BALANCE RISK CHECK INVALID URL: service=#{service_number}, error={ex}")
        return {"ok": False, "error": "invalid_api_url", "text": "недоступно"}

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
