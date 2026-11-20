"""HTTP transport and response decoding for SMM panels."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

import requests

from ..models import normalize_api_url
from .panel_errors import _sanitize_secret_text

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
