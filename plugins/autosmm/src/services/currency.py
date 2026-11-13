"""Currency normalization, CBR/fallback FX fetching, caching, and RUB conversion."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
import xml.etree.ElementTree as ET

import requests

from ..models import optional_float, safe_float
from ..storage.storage import STATE_LOCK, _load_autosmm_state, load_config, update_state_fields

logger = logging.getLogger("FPC.autosmm")

FX_STATE_KEY = "fx_rates_cache_v1"
DEFAULT_FX_CACHE_TTL_SECONDS = 12 * 3600
FX_REQUEST_TIMEOUT_SECONDS = 4

CBR_JSON_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CBR_XML_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
OPEN_ER_RUB_URL = "https://open.er-api.com/v6/latest/RUB"

_CURRENCY_ALIASES = {
    "RUB": "RUB",
    "RUR": "RUB",
    "₽": "RUB",
    "РУБ": "RUB",
    "РУБ.": "RUB",
    "USD": "USD",
    "$": "USD",
    "USDT": "USD",
    "EUR": "EUR",
    "€": "EUR",
    "UAH": "UAH",
    "₴": "UAH",
    "KZT": "KZT",
    "₸": "KZT",
    "BYN": "BYN",
    "TRY": "TRY",
    "CNY": "CNY",
    "GBP": "GBP",
}

_MEMORY_LOCK = threading.RLock()
_MEMORY_CACHE: Dict[str, Any] = {
    "fetched_at": 0.0,
    "source": "",
    "rates": {"RUB": 1.0},
}


def normalize_currency_code(currency: Any) -> str:
    """Normalize currency symbols/aliases to ISO-like uppercase currency codes."""
    raw = str(currency or "").strip().upper()
    if not raw:
        return "RUB"
    return _CURRENCY_ALIASES.get(raw, raw)


def _fetch_rates_cbr_json(http_get: Callable[..., Any]) -> Optional[Dict[str, float]]:
    resp = http_get(CBR_JSON_URL, timeout=FX_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    valutes = payload.get("Valute") if isinstance(payload, dict) else None
    if not isinstance(valutes, dict):
        return None
    rates: Dict[str, float] = {"RUB": 1.0}
    for code, entry in valutes.items():
        if not isinstance(entry, dict):
            continue
        nominal = safe_float(entry.get("Nominal"), 1.0)
        value = safe_float(entry.get("Value"), 0.0)
        if nominal > 0 and value > 0:
            rates[normalize_currency_code(code)] = round(value / nominal, 6)
    return rates if len(rates) > 1 else None


def _fetch_rates_cbr_xml(http_get: Callable[..., Any]) -> Optional[Dict[str, float]]:
    resp = http_get(CBR_XML_URL, timeout=FX_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    rates: Dict[str, float] = {"RUB": 1.0}
    for node in root.findall("Valute"):
        code_el = node.find("CharCode")
        nom_el = node.find("Nominal")
        val_el = node.find("Value")
        if code_el is None or val_el is None or not code_el.text or not val_el.text:
            continue
        raw_nom = nom_el.text if nom_el is not None else None
        if raw_nom is not None and str(raw_nom).strip():
            nominal = optional_float(raw_nom)
        else:
            nominal = 1.0
        value = optional_float(val_el.text)
        if nominal is not None and nominal > 0 and value is not None and value > 0:
            rates[normalize_currency_code(code_el.text)] = round(value / nominal, 6)
    return rates if len(rates) > 1 else None


def _fetch_rates_open_er(http_get: Callable[..., Any]) -> Optional[Dict[str, float]]:
    resp = http_get(OPEN_ER_RUB_URL, timeout=FX_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    raw_rates = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(raw_rates, dict):
        return None
    rates: Dict[str, float] = {"RUB": 1.0}
    for code, per_rub in raw_rates.items():
        per_rub_f = optional_float(per_rub)
        if per_rub_f is not None and per_rub_f > 0:
            rates[normalize_currency_code(code)] = round(1.0 / per_rub_f, 6)
    return rates if len(rates) > 1 else None


_FX_PROVIDERS: Tuple[Tuple[str, Callable[[Callable[..., Any]], Optional[Dict[str, float]]]], ...] = (
    ("cbr_json", _fetch_rates_cbr_json),
    ("cbr_xml", _fetch_rates_cbr_xml),
    ("open_er_api", _fetch_rates_open_er),
)


def _load_persisted_fx_cache() -> Dict[str, Any]:
    try:
        state = _load_autosmm_state()
        entry = state.get(FX_STATE_KEY)
        if isinstance(entry, dict) and isinstance(entry.get("rates"), dict):
            return entry
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        logger.debug(f"Не удалось прочитать кэш курсов из kv_state: {exc}")
    return {}


def _save_persisted_fx_cache(fetched_at: float, source: str, rates: Mapping[str, float]) -> None:
    payload = {
        "fetched_at": float(fetched_at),
        "source": str(source),
        "rates": {str(k): float(v) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0},
    }
    try:
        with STATE_LOCK:
            update_state_fields(**{FX_STATE_KEY: payload})
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        logger.debug(f"Не удалось сохранить кэш курсов в kv_state: {exc}")


def _manual_fx_override(currency_code: str, config_loader: Callable[[], Mapping[str, Any]]) -> Optional[float]:
    try:
        cfg = config_loader() or {}
    except (OSError, ValueError, TypeError) as exc:
        logger.debug(f"Не удалось прочитать fx_rates из конфига: {exc}")
        return None
    fx_map = cfg.get("fx_rates") if isinstance(cfg, Mapping) else None
    if not isinstance(fx_map, Mapping):
        return None
    for raw_key, raw_val in fx_map.items():
        if normalize_currency_code(raw_key) == currency_code:
            parsed = optional_float(raw_val)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _get_cached_rates(*, now_ts: float, ttl_seconds: int) -> Tuple[Dict[str, float], bool]:
    with _MEMORY_LOCK:
        mem_ts = safe_float(_MEMORY_CACHE.get("fetched_at"), 0.0)
        mem_rates = dict(_MEMORY_CACHE.get("rates") or {"RUB": 1.0})
        if mem_ts > 0 and (now_ts - mem_ts) < ttl_seconds and len(mem_rates) > 1:
            return mem_rates, True

        disk_entry = _load_persisted_fx_cache()
        disk_ts = safe_float(disk_entry.get("fetched_at"), 0.0)
        raw_disk_rates = disk_entry.get("rates") if isinstance(disk_entry.get("rates"), dict) else {}
        disk_rates = {
            normalize_currency_code(k): float(v)
            for k, v in raw_disk_rates.items()
            if (parsed := optional_float(v)) is not None and parsed > 0
        }
        disk_rates["RUB"] = 1.0

        if disk_ts > mem_ts and len(disk_rates) > 1:
            _MEMORY_CACHE["fetched_at"] = disk_ts
            _MEMORY_CACHE["source"] = str(disk_entry.get("source") or "kv_state")
            _MEMORY_CACHE["rates"] = dict(disk_rates)
            mem_ts = disk_ts
            mem_rates = dict(disk_rates)

        is_fresh = mem_ts > 0 and (now_ts - mem_ts) < ttl_seconds and len(mem_rates) > 1
        return mem_rates, is_fresh


def refresh_fx_rates(
    *,
    now_ts: Optional[float] = None,
    http_get: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, float]]:
    """Fetch fresh RUB exchange rates across configured providers and cache them."""
    getter = http_get or requests.get
    ts = float(now_ts if now_ts is not None else time.time())
    for source_name, fetcher in _FX_PROVIDERS:
        try:
            rates = fetcher(getter)
        except (requests.RequestException, ET.ParseError, ValueError, TypeError, KeyError) as exc:
            logger.debug(f"FX провайдер {source_name} недоступен: {exc}")
            continue
        if rates and len(rates) > 1:
            rates["RUB"] = 1.0
            with _MEMORY_LOCK:
                _MEMORY_CACHE["fetched_at"] = ts
                _MEMORY_CACHE["source"] = source_name
                _MEMORY_CACHE["rates"] = dict(rates)
            _save_persisted_fx_cache(ts, source_name, rates)
            logger.debug(f"FX курсы обновлены из {source_name}: валют={len(rates)}")
            return rates
    return None


def reset_memory_cache() -> None:
    """Clear only the in-memory FX cache (useful for isolated tests)."""
    with _MEMORY_LOCK:
        _MEMORY_CACHE["fetched_at"] = 0.0
        _MEMORY_CACHE["source"] = ""
        _MEMORY_CACHE["rates"] = {"RUB": 1.0}


def get_rate_to_rub(
    currency: Any,
    *,
    allow_network: bool = True,
    now_ts: Optional[float] = None,
    ttl_seconds: int = DEFAULT_FX_CACHE_TTL_SECONDS,
    http_get: Optional[Callable[..., Any]] = None,
    config_loader: Callable[[], Mapping[str, Any]] = load_config,
) -> Optional[float]:
    """Return RUB cost of 1 currency unit, using manual config, fresh cache, network, or stale cache."""
    code = normalize_currency_code(currency)
    if code == "RUB":
        return 1.0

    manual = _manual_fx_override(code, config_loader)
    if manual is not None:
        return manual

    ts = float(now_ts if now_ts is not None else time.time())
    cached_rates, is_fresh = _get_cached_rates(now_ts=ts, ttl_seconds=ttl_seconds)
    if is_fresh and code in cached_rates:
        return cached_rates[code]

    if allow_network:
        fresh = refresh_fx_rates(now_ts=ts, http_get=http_get)
        if fresh and code in fresh:
            return fresh[code]

    # Graceful offline fallback: reuse cached stale rate rather than mixing USD with RUB.
    if code in cached_rates:
        return cached_rates[code]
    return None


def convert_to_rub(
    amount: Any,
    currency: Any,
    *,
    allow_network: bool = True,
    now_ts: Optional[float] = None,
    ttl_seconds: int = DEFAULT_FX_CACHE_TTL_SECONDS,
    http_get: Optional[Callable[..., Any]] = None,
    config_loader: Callable[[], Mapping[str, Any]] = load_config,
) -> Tuple[Optional[float], Optional[float]]:
    """Convert an amount in `currency` to RUB, returning `(rub_amount, fx_rate)`."""
    code = normalize_currency_code(currency)
    amount_f = safe_float(amount, 0.0)
    if code == "RUB":
        return round(amount_f, 4), 1.0
    if abs(amount_f) < 1e-12:
        rate = get_rate_to_rub(
            code,
            allow_network=False,
            now_ts=now_ts,
            ttl_seconds=ttl_seconds,
            http_get=http_get,
            config_loader=config_loader,
        )
        return 0.0, rate
    rate = get_rate_to_rub(
        code,
        allow_network=allow_network,
        now_ts=now_ts,
        ttl_seconds=ttl_seconds,
        http_get=http_get,
        config_loader=config_loader,
    )
    if rate is None or rate <= 0:
        return None, None
    return round(amount_f * rate, 4), rate


def compute_order_profit_rub(
    lot_price_rub: Any,
    charge: Any,
    currency: Any,
    *,
    allow_network: bool = True,
    now_ts: Optional[float] = None,
    ttl_seconds: int = DEFAULT_FX_CACHE_TTL_SECONDS,
    http_get: Optional[Callable[..., Any]] = None,
    config_loader: Callable[[], Mapping[str, Any]] = load_config,
) -> Dict[str, Any]:
    """Convert panel charge to RUB and calculate net RUB profit safely."""
    code = normalize_currency_code(currency)
    price_f = safe_float(lot_price_rub, 0.0)
    charge_f = safe_float(charge, 0.0)
    spent_rub, rate = convert_to_rub(
        charge_f,
        code,
        allow_network=allow_network,
        now_ts=now_ts,
        ttl_seconds=ttl_seconds,
        http_get=http_get,
        config_loader=config_loader,
    )
    net_profit = round(price_f - spent_rub, 2) if spent_rub is not None else None
    return {
        "spent": charge_f,
        "currency": code,
        "spent_rub": spent_rub,
        "fx_rate_to_rub": rate,
        "net_profit": net_profit,
    }
