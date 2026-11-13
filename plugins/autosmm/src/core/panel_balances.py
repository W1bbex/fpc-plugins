"""Thread-safe panel URL validation and hybrid async balance cache."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import runtime as rt
from ..storage.storage import load_config
from .panel_api import (
    _normalize_api_key,
    _panel_api_json,
    _validate_api_key_for_action,
    parse_panel_balance_value,
)

PANEL_BALANCE_CACHE_TTL_SECONDS = 43200  # 12 hours
UI_BALANCE_TIMEOUT_SECONDS = 3


def format_currency_symbol(currency: str) -> str:
    """Return display currency suffix for Telegram menus."""
    currency_upper = str(currency or "").upper()
    if currency_upper == "RUB":
        return "₽"
    return currency_upper or ""


def normalize_api_url(raw_url: Any) -> str:
    """Normalize and validate an SMM panel API endpoint while blocking private/local targets."""
    raw_value = str(raw_url or "").strip()
    if not raw_value:
        raise ValueError("ссылка или домен не должны быть пустыми")

    markdown_match = re.match(r"^\[[^\]]+\]\(([^)]+)\)$", raw_value)
    if markdown_match:
        raw_value = markdown_match.group(1).strip()

    raw_value = raw_value.replace("\\", "/").strip()
    if raw_value.startswith("//"):
        raw_value = f"https:{raw_value}"

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw_value):
        raw_value = f"https://{raw_value.lstrip('/')}"

    parts = urlsplit(raw_value)
    host = str(parts.netloc or "").strip().lower()

    if not host and parts.path:
        fallback_parts = urlsplit(f"https://{str(parts.path).lstrip('/')}")
        host = str(fallback_parts.netloc or "").strip().lower()
        parts = fallback_parts

    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]

    if not host or "." not in host:
        raise ValueError("не удалось распознать домен")

    hostname = str(getattr(parts, "hostname", None) or host.split(":", 1)[0]).strip("[]").lower()
    if not hostname or ("." not in hostname and ":" not in hostname):
        raise ValueError("не удалось распознать домен")
    if hostname.endswith((".local", ".localhost", ".internal", ".home.arpa")):
        raise ValueError("локальные и внутренние домены запрещены")
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            raise ValueError("локальные и приватные IP-адреса запрещены")
    else:
        host_no_port = host.split(":", 1)[0]
        if not re.fullmatch(r"[a-z0-9.-]+", host_no_port) or host_no_port.startswith(".") or host_no_port.endswith("."):
            raise ValueError("некорректный домен")

    return f"https://{host}/api/v2"


def extract_service_domain(api_url: Any) -> str:
    """Extract normalized lowercase domain[:port] from a configured panel URL."""
    raw_url = str(api_url or "").strip()
    if not raw_url:
        return ""
    try:
        normalized = normalize_api_url(raw_url)
    except ValueError:
        normalized = raw_url if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw_url) else f"https://{raw_url.lstrip('/')}"

    try:
        host = str(urlsplit(normalized).netloc or "").strip().lower()
    except ValueError:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_placeholder_api_url(api_url: Any) -> bool:
    """Return True when the panel URL points to documentation placeholder domains."""
    domain = extract_service_domain(api_url).split(":", 1)[0].lower()
    return domain in {"example.com", "example.org", "example.net", "test.com"}


def has_valid_service_credentials(service_number: int, service_cfg: Optional[Dict[str, Any]]) -> bool:
    """Return True if a service entry has non-placeholder URL and API key ready to query."""
    cfg = service_cfg or {}
    api_url_raw = str(cfg.get("api_url") or "").strip()
    api_key = _normalize_api_key(cfg.get("api_key"))
    if (
        not api_url_raw
        or not api_key
        or is_placeholder_api_url(api_url_raw)
        or _validate_api_key_for_action(service_number, "balance", api_key)
    ):
        return False
    return True


def fetch_service_balance_payload(
    service_number: int,
    service_cfg: Optional[Dict[str, Any]],
    timeout: int = 6,
    api_json_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Fetch live balance payload for a single configured SMM panel service."""
    cfg = service_cfg or {}
    if not has_valid_service_credentials(service_number, cfg):
        return {
            "ok": False,
            "status_emoji": "❌",
            "balance_text": "недоступно",
            "balance_value": None,
            "currency": None,
            "updated_at": time.time(),
        }

    api_url_raw = str(cfg.get("api_url") or "").strip()
    api_key = _normalize_api_key(cfg.get("api_key"))
    request_json = api_json_fn or _panel_api_json

    try:
        api_url = normalize_api_url(api_url_raw)
        _response, payload, _raw_text, invalid_json = request_json(
            api_url,
            {"key": api_key, "action": "balance"},
            timeout=timeout,
        )
        if invalid_json or not isinstance(payload, dict) or "balance" not in payload:
            raise ValueError("панель не вернула balance")

        balance_value = parse_panel_balance_value(payload.get("balance"))
        currency = str(payload.get("currency") or "RUB").upper().strip() or "RUB"
        balance_text = f"{balance_value:.2f} {format_currency_symbol(currency)}".strip()
        return {
            "ok": True,
            "status_emoji": "✅",
            "balance_text": balance_text,
            "balance_value": balance_value,
            "currency": currency,
            "updated_at": time.time(),
        }
    except Exception as ex:
        rt.logger.warning(f"{rt.LOGGER_PREFIX} Не удалось получить баланс для сервиса #{service_number}: {ex}")
        return {
            "ok": False,
            "status_emoji": "❌",
            "balance_text": "недоступно",
            "balance_value": None,
            "currency": None,
            "updated_at": time.time(),
        }


def _cache_services_dict_locked() -> Dict[str, Dict[str, Any]]:
    services = rt.PANEL_BALANCE_CACHE.get("services")
    if not isinstance(services, dict):
        services = {}
        rt.PANEL_BALANCE_CACHE["services"] = services
    return services


def _cache_inflight_dict_locked() -> Dict[str, Dict[str, Any]]:
    inflight = rt.PANEL_BALANCE_CACHE.get("inflight")
    if not isinstance(inflight, dict):
        inflight = {}
        rt.PANEL_BALANCE_CACHE["inflight"] = inflight
    return inflight


def invalidate_panel_balance_cache(service_number: Optional[int] = None) -> None:
    """Invalidate cached panel balances and advance cache generation to drop stale async writes."""
    with rt.PANEL_BALANCE_CACHE_LOCK:
        rt.PANEL_BALANCE_CACHE["generation"] = int(rt.PANEL_BALANCE_CACHE.get("generation") or 0) + 1
        rt.PANEL_BALANCE_CACHE["timestamp"] = 0.0
        _cache_inflight_dict_locked().clear()
        services_map = _cache_services_dict_locked()
        if service_number is None:
            services_map.clear()
            rt.PANEL_BALANCE_CACHE["text"] = "недоступно"
        else:
            services_map.pop(str(service_number), None)
            rt.PANEL_BALANCE_CACHE["text"] = "недоступно" if not services_map else compute_total_balance_text(
                None, services_map
            )


def compute_total_balance_text(
    services_cfg: Optional[Dict[str, Any]],
    cached_services: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Compute human-readable combined balance summary across services from cached payloads."""
    source_services = cached_services if cached_services is not None else {}
    target_keys = list((services_cfg or {}).keys()) if services_cfg is not None else list(source_services.keys())
    if not target_keys:
        return "недоступно"

    totals_by_currency: Dict[str, float] = {}
    failed_or_missing = 0

    for srv_key in target_keys:
        payload = source_services.get(str(srv_key))
        if not isinstance(payload, dict) or not payload.get("ok"):
            failed_or_missing += 1
            continue
        currency_value = str(payload.get("currency") or "RUB").upper().strip() or "RUB"
        totals_by_currency[currency_value] = (
            totals_by_currency.get(currency_value, 0.0) + float(payload.get("balance_value") or 0.0)
        )

    if not totals_by_currency:
        return "недоступно"
    if len(totals_by_currency) > 1:
        return "mixed currencies"
    if failed_or_missing > 0:
        return "частично недоступно"
    currency_value, total_value = next(iter(totals_by_currency.items()))
    return f"{float(total_value):.2f} {format_currency_symbol(currency_value)}".strip()


def get_cached_service_balance(
    service_number: int,
    service_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return cached balance payload for one panel without performing network I/O."""
    key = str(service_number)
    with rt.PANEL_BALANCE_CACHE_LOCK:
        cached = _cache_services_dict_locked().get(key)
        if isinstance(cached, dict):
            return dict(cached)
        inflight = _cache_inflight_dict_locked()
        is_refreshing = "all" in inflight or f"srv:{service_number}" in inflight

    if service_cfg is not None and not has_valid_service_credentials(service_number, service_cfg):
        return {
            "ok": False,
            "status_emoji": "❌",
            "balance_text": "недоступно",
            "balance_value": None,
            "currency": None,
        }

    return {
        "ok": False,
        "status_emoji": "⏳",
        "balance_text": "обновляется..." if is_refreshing else "не проверялся",
        "balance_value": None,
        "currency": None,
        "pending": True,
    }


def get_cached_total_balance_text(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Return the cached aggregate panel balance string in 0 ms without network I/O."""
    services_cfg = (cfg or {}).get("services") if isinstance(cfg, dict) else None
    with rt.PANEL_BALANCE_CACHE_LOCK:
        if services_cfg is not None and not services_cfg:
            return "недоступно"
        services_map = _cache_services_dict_locked()
        if services_cfg is not None and services_map:
            return compute_total_balance_text(services_cfg, services_map)
        return str(rt.PANEL_BALANCE_CACHE.get("text") or "недоступно")


def is_panel_balance_cache_stale(
    *,
    service_number: Optional[int] = None,
    max_age_seconds: int = PANEL_BALANCE_CACHE_TTL_SECONDS,
    now_ts: Optional[float] = None,
) -> bool:
    """Return True when target panel balance cache entry is missing or older than TTL."""
    now_val = float(now_ts if now_ts is not None else time.time())
    ttl = max(0, int(max_age_seconds))
    with rt.PANEL_BALANCE_CACHE_LOCK:
        if service_number is None:
            ts = float(rt.PANEL_BALANCE_CACHE.get("timestamp") or 0.0)
            return ts <= 0.0 or (now_val - ts) >= ttl
        entry = _cache_services_dict_locked().get(str(service_number))
        if not isinstance(entry, dict):
            return True
        ts = float(entry.get("updated_at") or 0.0)
        return ts <= 0.0 or (now_val - ts) >= ttl


def _collect_service_targets(
    services_cfg: Dict[str, Any],
    service_number: Optional[int] = None,
) -> List[Tuple[str, int, Dict[str, Any]]]:
    targets: List[Tuple[str, int, Dict[str, Any]]] = []
    if service_number is not None:
        key = str(int(service_number))
        srv_cfg = services_cfg.get(key)
        if isinstance(srv_cfg, dict):
            targets.append((key, int(service_number), dict(srv_cfg)))
        return targets

    for srv_key, srv_cfg in services_cfg.items():
        if not isinstance(srv_cfg, dict):
            continue
        try:
            srv_int = int(srv_key)
        except (TypeError, ValueError):
            srv_int = 0
        targets.append((str(srv_key), srv_int, dict(srv_cfg)))
    return targets


def refresh_panel_balances_sync(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    timeout: int = 6,
    service_number: Optional[int] = None,
    force: bool = True,
    max_age_seconds: int = PANEL_BALANCE_CACHE_TTL_SECONDS,
    api_json_fn: Optional[Callable[..., Any]] = None,
) -> Tuple[bool, str]:
    """Poll one or all panel balances in parallel and update cache if generation did not advance."""
    effective_cfg = cfg if isinstance(cfg, dict) else load_config()
    services_cfg = (effective_cfg.get("services") or {}) if isinstance(effective_cfg, dict) else {}

    with rt.PANEL_BALANCE_CACHE_LOCK:
        if not force and not is_panel_balance_cache_stale(
            service_number=service_number,
            max_age_seconds=max_age_seconds,
        ):
            return False, get_cached_total_balance_text(effective_cfg)
        start_generation = int(rt.PANEL_BALANCE_CACHE.get("generation") or 0)

    if not services_cfg:
        with rt.PANEL_BALANCE_CACHE_LOCK:
            if int(rt.PANEL_BALANCE_CACHE.get("generation") or 0) != start_generation:
                return False, str(rt.PANEL_BALANCE_CACHE.get("text") or "недоступно")
            _cache_services_dict_locked().clear()
            rt.PANEL_BALANCE_CACHE["timestamp"] = time.time()
            rt.PANEL_BALANCE_CACHE["text"] = "недоступно"
            return True, "недоступно"

    targets = _collect_service_targets(services_cfg, service_number=service_number)
    if not targets:
        return False, get_cached_total_balance_text(effective_cfg)

    results: Dict[str, Dict[str, Any]] = {}
    if len(targets) == 1:
        srv_key, srv_int, srv_cfg = targets[0]
        results[srv_key] = fetch_service_balance_payload(
            srv_int, srv_cfg, timeout=timeout, api_json_fn=api_json_fn
        )
    else:
        max_workers = min(8, len(targets))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                srv_key: pool.submit(
                    fetch_service_balance_payload,
                    srv_int,
                    srv_cfg,
                    timeout,
                    api_json_fn,
                )
                for srv_key, srv_int, srv_cfg in targets
            }
            for srv_key, future in futures.items():
                try:
                    results[srv_key] = future.result()
                except Exception as ex:
                    rt.logger.warning(f"{rt.LOGGER_PREFIX} Ошибка опроса баланса сервиса #{srv_key}: {ex}")
                    results[srv_key] = {
                        "ok": False,
                        "status_emoji": "❌",
                        "balance_text": "недоступно",
                        "balance_value": None,
                        "currency": None,
                        "updated_at": time.time(),
                    }

    now_ts = time.time()
    with rt.PANEL_BALANCE_CACHE_LOCK:
        if int(rt.PANEL_BALANCE_CACHE.get("generation") or 0) != start_generation:
            return False, str(rt.PANEL_BALANCE_CACHE.get("text") or "недоступно")
        services_map = _cache_services_dict_locked()
        if service_number is None:
            for stale_key in [k for k in list(services_map.keys()) if k not in services_cfg]:
                services_map.pop(stale_key, None)
            rt.PANEL_BALANCE_CACHE["timestamp"] = now_ts
        services_map.update(results)
        total_text = compute_total_balance_text(services_cfg, services_map)
        rt.PANEL_BALANCE_CACHE["text"] = total_text
        return True, total_text


def schedule_panel_balances_refresh(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    force: bool = False,
    max_age_seconds: int = PANEL_BALANCE_CACHE_TTL_SECONDS,
    timeout: int = UI_BALANCE_TIMEOUT_SECONDS,
    service_number: Optional[int] = None,
    on_complete: Optional[Callable[[bool, str], None]] = None,
    api_json_fn: Optional[Callable[..., Any]] = None,
) -> bool:
    """Start a deduplicated background balance poll when stale/forced and invoke callbacks on completion."""
    effective_cfg = cfg if isinstance(cfg, dict) else load_config()
    services_cfg = (effective_cfg.get("services") or {}) if isinstance(effective_cfg, dict) else {}
    if not services_cfg:
        return False

    inflight_key = "all" if service_number is None else f"srv:{int(service_number)}"
    with rt.PANEL_BALANCE_CACHE_LOCK:
        if not force and not is_panel_balance_cache_stale(
            service_number=service_number,
            max_age_seconds=max_age_seconds,
        ):
            return False

        inflight = _cache_inflight_dict_locked()
        existing = inflight.get(inflight_key)
        if existing is None and service_number is not None:
            existing = inflight.get("all")
        if existing is not None:
            if on_complete is not None:
                existing.setdefault("callbacks", []).append(on_complete)
            return False

        callbacks: List[Callable[[bool, str], None]] = []
        if on_complete is not None:
            callbacks.append(on_complete)
        inflight_entry = {"started_at": time.time(), "callbacks": callbacks}
        inflight[inflight_key] = inflight_entry

    def _worker() -> None:
        updated = False
        total_text = "недоступно"
        try:
            updated, total_text = refresh_panel_balances_sync(
                effective_cfg,
                timeout=timeout,
                service_number=service_number,
                force=True,
                max_age_seconds=max_age_seconds,
                api_json_fn=api_json_fn,
            )
        except Exception as ex:
            rt.logger.warning(f"{rt.LOGGER_PREFIX} Фоновое обновление балансов завершилось ошибкой: {ex}")
        finally:
            with rt.PANEL_BALANCE_CACHE_LOCK:
                inflight_map = _cache_inflight_dict_locked()
                if inflight_map.get(inflight_key) is inflight_entry:
                    entry = inflight_map.pop(inflight_key, None)
                else:
                    entry = inflight_entry
                pending_callbacks = list((entry or {}).get("callbacks") or [])

        for callback in pending_callbacks:
            try:
                callback(updated, total_text)
            except Exception as ex:
                rt.logger.debug(f"{rt.LOGGER_PREFIX} Ошибка callback после обновления баланса: {ex}")

    thread = threading.Thread(target=_worker, name=f"autosmm-balance-{inflight_key}", daemon=True)
    thread.start()
    return True


def schedule_single_panel_balance_refresh(
    service_number: int,
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Trigger a non-blocking balance refresh for one service after order placement."""
    return schedule_panel_balances_refresh(
        cfg=cfg,
        force=True,
        timeout=6,
        service_number=int(service_number),
    )
