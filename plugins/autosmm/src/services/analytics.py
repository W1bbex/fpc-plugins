"""Order revenue, spend, and net-profit analytics helpers for AutoSMM."""

from __future__ import annotations

from datetime import datetime
import logging
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import runtime as rt
from ..core.panel_api import _normalize_runtime_status
from ..models import (
    get_effective_funpay_status,
    is_order_refundedish,
    is_refunded_funpay_status,
    optional_float,
    safe_float,
    safe_int,
)
from .currency import convert_to_rub, normalize_currency_code
from ..storage.storage import (
    ORDERS_LOCK,
    get_order_data,
    load_config,
    load_orders_data,
    load_orders_log,
    save_orders_data,
    save_orders_log,
    update_order_fields,
)

logger = logging.getLogger("FPC.autosmm")

_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, _DATE_FMT)
    except (ValueError, TypeError):
        return None


def _coerce_int(v: Any) -> Optional[int]:
    return safe_int(v)


def _period_info(period: str) -> Tuple[str, Optional[int], str]:
    """Return `(normalized_period, seconds_window_or_None, title_label)`."""
    p = (period or "24h").strip().lower()
    if p in ("day", "24", "24ч", "24h"):
        return "24h", 86400, "24ч"
    if p in ("week", "7", "7д", "7d"):
        return "7d", 604800, "7д"
    if p in ("month", "30", "30д", "30d"):
        return "30d", 2592000, "30д"
    if p in ("all", "all_time", "всё", "все", "∞", "infinity"):
        return "all", None, "∞ всё"
    return "24h", 86400, "24ч"


def _filter_by_period(items: List[Dict], period: str) -> Tuple[str, List[Dict]]:
    """Filter items by Unix `created_at`; orders without `created_at` appear only in `all`."""
    _norm, window, label = _period_info(period)
    if window is None:
        return label, items

    now_ts = int(time.time())
    cutoff = now_ts - int(window)
    out: List[Dict] = []
    for it in items:
        ca = _coerce_int(it.get("created_at"))
        if ca is not None and ca >= cutoff:
            out.append(it)
    return label, out


def _normalize_analytics_status(status_value: Any) -> str:
    normalized = _normalize_runtime_status(status_value)
    if normalized in ("completed", "partial", "pending", "canceled", "fail", "unknown"):
        return normalized
    return "unknown"


def _is_completed_for_profit(
    _order_record: Dict,
    panel_status: str,
    funpay_status: Optional[str],
) -> bool:
    return panel_status == "completed" and funpay_status == "closed"


def _resolve_order_spent_rub(
    order_record: Mapping[str, Any],
    *,
    fx_cache: Optional[Dict[str, Optional[float]]] = None,
    config_loader: Callable[[], Mapping[str, Any]] = load_config,
) -> Tuple[Optional[float], bool]:
    """Return `(spent_in_rub, is_converted)` without mixing foreign currencies into RUB."""
    explicit_rub = optional_float(order_record.get("spent_rub"))
    if explicit_rub is not None:
        return round(explicit_rub, 4), True

    cur = normalize_currency_code(order_record.get("currency", "RUB"))
    spent = safe_float(order_record.get("spent"), 0.0)
    if cur == "RUB":
        return round(spent, 4), True
    if abs(spent) < 1e-12:
        return 0.0, True

    if fx_cache is not None and cur in fx_cache:
        cached_rate = fx_cache[cur]
        if cached_rate is not None and cached_rate > 0:
            return round(spent * cached_rate, 4), True
        return None, False

    converted, rate = convert_to_rub(spent, cur, allow_network=False, config_loader=config_loader)
    if fx_cache is not None:
        fx_cache[cur] = rate
    if converted is not None:
        return converted, True
    return None, False


def _calc_order_net_profit(order_record: Mapping[str, Any]) -> Optional[float]:
    revenue = safe_float(order_record.get("summa"), 0.0)
    spent_rub, resolved = _resolve_order_spent_rub(order_record)
    if not resolved or spent_rub is None:
        return None
    return round(revenue - spent_rub, 2)


def _fmt_rub(x: float) -> str:
    val = safe_float(x, 0.0)
    v = round(val, 2) if math.isfinite(val) else 0.0
    if abs(v - int(v)) < 1e-9:
        return str(int(v))
    return f"{v:.2f}"


def _build_analytics_entry(rec: Dict[str, Any], log_rec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    log_data = log_rec or {}
    date_str = log_data.get("date") or rec.get("date") or rec.get("date_str")
    fp_status = get_effective_funpay_status(rec)
    return {
        "order_id": str(rec.get("order_id")),
        "created_at": _coerce_int(rec.get("created_at")),
        "date": _parse_dt(str(date_str or "")),
        "date_str": date_str,
        "service_name": log_data.get("service_name") or rec.get("service_name") or "—",
        "status": str(rec.get("status", "unknown")),
        "is_refunded": is_refunded_funpay_status(fp_status),
        "funpay_status": fp_status,
        "summa": safe_float(rec.get("summa", log_data.get("summa", 0)), 0.0),
        "spent": safe_float(rec.get("spent", log_data.get("spent", 0)), 0.0),
        "spent_rub": optional_float(rec.get("spent_rub", log_data.get("spent_rub"))),
        "fx_rate_to_rub": optional_float(rec.get("fx_rate_to_rub", log_data.get("fx_rate_to_rub"))),
        "currency": normalize_currency_code(rec.get("currency", log_data.get("currency", "RUB"))),
        "chistota": safe_float(rec.get("chistota", log_data.get("chistota", 0)), 0.0),
    }


def _analytics_merged_orders(
    *,
    load_orders: Callable[[], List[Dict]] = load_orders_data,
    load_log: Optional[Callable[[], List[Dict]]] = None,
) -> List[Dict]:
    """Normalize persisted orders (and optional legacy log entries) for analytics aggregation."""
    od = load_orders()
    od = od if isinstance(od, list) else []
    log_list = load_log() if load_log is not None else []
    log_map: Dict[str, Dict] = {}
    for rec in log_list:
        if not isinstance(rec, dict) or rec.get("order_id") in (None, ""):
            continue
        oid = str(rec.get("order_id"))
        if oid:
            log_map[oid] = rec

    merged: List[Dict] = []
    seen = set()
    for rec in od:
        if not isinstance(rec, dict) or rec.get("order_id") in (None, ""):
            continue
        oid = str(rec.get("order_id"))
        if not oid:
            continue
        merged.append(_build_analytics_entry(rec, log_map.get(oid)))
        seen.add(oid)

    for rec in log_list:
        if not isinstance(rec, dict):
            continue
        oid = str(rec.get("order_id") or "")
        if not oid or oid in seen:
            continue
        date_val = rec.get("date")
        merged.append(
            {
                "order_id": oid,
                "created_at": None,
                "date": _parse_dt(str(date_val or "")),
                "date_str": date_val,
                "service_name": rec.get("service_name") or "—",
                "status": "unknown",
                "is_refunded": False,
                "funpay_status": None,
                "summa": safe_float(rec.get("summa"), 0.0),
                "spent": safe_float(rec.get("spent"), 0.0),
                "currency": str(rec.get("currency", "RUB") or "RUB"),
                "chistota": safe_float(rec.get("chistota"), 0.0),
            }
        )
    return merged


def _analytics_get_statistics(*, load_merged: Callable[[], List[Dict]]) -> Dict:
    """Aggregate legacy summary buckets for 24h / 7d / 30d / all time."""
    items = load_merged()

    def calc(p: str) -> Dict:
        _, lst = _filter_by_period(items, p)
        total = sum(safe_float(o.get("summa"), 0.0) for o in lst)
        ch = sum(safe_float(o.get("chistota"), 0.0) for o in lst if not is_order_refundedish(o))
        return {"orders": len(lst), "total": total, "chistota": round(ch, 2)}

    day = calc("24h")
    week = calc("7d")
    month = calc("30d")
    all_ = calc("all")
    return {
        "day_orders": day["orders"],
        "day_total": day["total"],
        "day_chistota": day["chistota"],
        "week_orders": week["orders"],
        "week_total": week["total"],
        "week_chistota": week["chistota"],
        "month_orders": month["orders"],
        "month_total": month["total"],
        "month_chistota": month["chistota"],
        "all_time_orders": all_["orders"],
        "all_time_total": all_["total"],
        "all_time_chistota": all_["chistota"],
    }


def _analytics_get_period_analytics(period: str = "24h", *, load_merged: Callable[[], List[Dict]]) -> Dict:
    """Compute financial and order-status metrics for `24h | 7d | 30d | all`."""
    items = load_merged()
    label, lst = _filter_by_period(items, period)
    total = len(lst)
    active = completed = refunded = partial = errors = unconverted_fx = awaiting_confirmation = 0
    revenue = spent = profit = profit_pending = losses = 0.0
    cfg_snapshot = load_config()
    fx_cache: Dict[str, Optional[float]] = {}

    def _cfg_loader() -> Mapping[str, Any]:
        return cfg_snapshot

    for o in lst:
        panel_status = _normalize_analytics_status(o.get("status", "unknown"))
        funpay_status = get_effective_funpay_status(o)
        refunded_like = funpay_status in ("refunded", "partially_refunded")

        order_summa = safe_float(o.get("summa"), 0.0)
        order_spent_rub, fx_ok = _resolve_order_spent_rub(o, fx_cache=fx_cache, config_loader=_cfg_loader)
        if refunded_like:
            refunded += 1
            if (panel_status in ("completed", "partial") or safe_float(o.get("spent"), 0.0) > 0) and fx_ok and order_spent_rub is not None:
                losses += order_spent_rub
            continue

        revenue += order_summa
        if fx_ok and order_spent_rub is not None:
            spent += order_spent_rub
            order_net_profit = round(order_summa - order_spent_rub, 2)
        else:
            unconverted_fx += 1
            order_net_profit = None
        if panel_status == "partial":
            partial += 1
            continue
        if _is_completed_for_profit(o, panel_status, funpay_status):
            completed += 1
            if order_net_profit is not None:
                profit += order_net_profit
            continue
        if panel_status == "completed" and funpay_status in (None, "open"):
            active += 1
            awaiting_confirmation += 1
            if order_net_profit is not None:
                profit_pending += order_net_profit
            continue
        if panel_status in ("fail", "canceled", "unknown"):
            errors += 1
            continue
        active += 1

    return {
        "period_norm": _period_info(period)[0],
        "period_label": label,
        "revenue": round(revenue, 2),
        "spent": round(spent, 2),
        "profit": round(profit, 2),
        "profit_pending": round(profit_pending, 2),
        "losses": round(losses, 2),
        "total": int(total),
        "active": int(active),
        "completed": int(completed),
        "refunded": int(refunded),
        "partial": int(partial),
        "errors": int(errors),
        "unconverted_fx": int(unconverted_fx),
        "awaiting_confirmation": int(awaiting_confirmation),
    }


def _analytics_count_awaiting_confirmation(period: str = "24h", *, load_merged: Callable[[], List[Dict]]) -> int:
    _label, lst = _filter_by_period(load_merged(), period)
    count = 0
    for o in lst:
        panel_status = _normalize_analytics_status(o.get("status", "unknown"))
        funpay_status = get_effective_funpay_status(o)
        if panel_status == "completed" and funpay_status in (None, "open"):
            count += 1
    return int(count)


def _analytics_format_analytics(
    period: str = "24h",
    mode: str = "main",
    *,
    get_period: Callable[..., Dict],
    count_awaiting: Callable[..., int],
) -> str:
    """Render the Telegram-ready analytics text summary."""
    d = get_period(period=period)
    period_title = d.get("period_label", "—")
    expanded = str(mode or "").strip().lower() == "more"
    if expanded:
        awaiting_val = d.get("awaiting_confirmation")
        awaiting_confirmation = int(awaiting_val) if awaiting_val is not None else count_awaiting(period=period)
        parts = [
            f"📊 AutoSMM — {period_title} (расширенная)",
            "",
            "💰 Финансы",
            f"Выручка: {_fmt_rub(d.get('revenue', 0))} ₽",
            f"Затраты: {_fmt_rub(d.get('spent', 0))} ₽",
            f"📈 Профит: {_fmt_rub(d.get('profit', 0))} ₽",
            f"📉 Потери: {_fmt_rub(d.get('losses', 0))} ₽",
        ]
        if int(d.get("unconverted_fx", 0) or 0) > 0:
            parts.append(f"⚠️ Без курса в ₽: {int(d['unconverted_fx'])} зак.")
        parts.extend(
            [
                "",
                "📦 Заказы",
                f"Всего: {int(d.get('total', 0) or 0)}",
                f"⚙ В работе: {int(d.get('active', 0) or 0)}",
                f"✅ Завершено: {int(d.get('completed', 0) or 0)}",
                f"↩ Refunded: {int(d.get('refunded', 0) or 0)}",
                f"🧩 Partial: {int(d.get('partial', 0) or 0)}",
                f"❌ Ошибки: {int(d.get('errors', 0) or 0)}",
                "",
                "⏳ Ожидание",
                f"Профит в ожидании: {_fmt_rub(d.get('profit_pending', 0))} ₽",
                f"Ожидают подтверждения: {awaiting_confirmation}",
            ]
        )
        return "\n".join(parts)

    parts = [
        f"📊 AutoSMM — {period_title}",
        "",
        "💰 Финансы",
        f"Выручка: {_fmt_rub(d.get('revenue', 0))} ₽",
        f"Затраты: {_fmt_rub(d.get('spent', 0))} ₽",
        f"📈 Профит: {_fmt_rub(d.get('profit', 0))} ₽",
    ]
    if int(d.get("unconverted_fx", 0) or 0) > 0:
        parts.append(f"⚠️ Без курса в ₽: {int(d['unconverted_fx'])} зак.")
    parts.extend(
        [
            "",
            "📦 Заказы",
            f"Всего: {int(d.get('total', 0) or 0)}",
            f"⚙ В работе: {int(d.get('active', 0) or 0)}",
            f"✅ Завершено: {int(d.get('completed', 0) or 0)}",
            f"↩️ Refunded: {int(d.get('refunded', 0) or 0)}",
        ]
    )
    return "\n".join(parts)


def _resolve_charge_fx(
    spent_f: float,
    currency_str: str,
    spent_rub: Optional[float],
    fx_rate_to_rub: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    if spent_rub is not None:
        return round(safe_float(spent_rub, 0.0), 4), fx_rate_to_rub
    if currency_str == "RUB":
        return round(spent_f, 4), 1.0
    if abs(spent_f) < 1e-12:
        return 0.0, fx_rate_to_rub
    return convert_to_rub(spent_f, currency_str, allow_network=False)


def _apply_charge_and_net(
    record: Dict[str, Any],
    spent_f: float,
    currency: str,
    net_profit: Optional[float],
    *,
    spent_rub: Optional[float] = None,
    fx_rate_to_rub: Optional[float] = None,
) -> None:
    """Write normalized spend, RUB conversion, and profit fields onto one order dict."""
    norm_cur = normalize_currency_code(currency or "USD")
    resolved_rub, resolved_rate = _resolve_charge_fx(spent_f, norm_cur, spent_rub, fx_rate_to_rub)
    record["spent"] = spent_f
    record["currency"] = norm_cur
    if resolved_rub is not None:
        record["spent_rub"] = resolved_rub
    else:
        record.pop("spent_rub", None)
    if resolved_rate is not None:
        record["fx_rate_to_rub"] = resolved_rate
    else:
        record.pop("fx_rate_to_rub", None)

    if net_profit is not None:
        try:
            record["chistota"] = float(net_profit)
        except (ValueError, TypeError) as exc:
            logger.debug(f"Некорректный net_profit '{net_profit}': {exc}")
    elif resolved_rub is not None:
        record["chistota"] = round(safe_float(record.get("summa"), 0.0) - resolved_rub, 2)
    else:
        record["chistota"] = 0.0


def _analytics_update_order_charge_and_net(
    order_id_funpay: str,
    spent: float,
    currency: str = "USD",
    net_profit: Optional[float] = None,
    *,
    spent_rub: Optional[float] = None,
    fx_rate_to_rub: Optional[float] = None,
    order_store: Any = None,
    load_log: Any = None,
    write_log: Any = None,
) -> None:
    """Update `spent`, `spent_rub`, `currency`, and `chistota` for an order atomically."""
    if isinstance(currency, (int, float)) and not isinstance(currency, bool) and net_profit is None:
        net_profit = float(currency)
        currency = "USD"
    if not isinstance(currency, str):
        currency = "USD"
    spent_f = safe_float(spent, 0.0)
    currency_str = normalize_currency_code(currency or "USD")
    resolved_rub, resolved_rate = _resolve_charge_fx(spent_f, currency_str, spent_rub, fx_rate_to_rub)

    if order_store is None and load_log is None and write_log is None:
        with ORDERS_LOCK:
            existing = get_order_data(str(order_id_funpay))
            if existing is None:
                return
            if net_profit is not None:
                chistota_val = safe_float(net_profit, safe_float(existing.get("chistota"), 0.0))
            elif resolved_rub is not None:
                chistota_val = round(safe_float(existing.get("summa"), 0.0) - resolved_rub, 2)
            else:
                chistota_val = 0.0
            update_fields: Dict[str, Any] = {
                "spent": spent_f,
                "currency": currency_str,
                "chistota": chistota_val,
                "spent_rub": resolved_rub,
                "fx_rate_to_rub": resolved_rate,
            }
            update_order_fields(str(order_id_funpay), **update_fields)
        return

    lock = getattr(order_store, "ORDERS_LOCK", ORDERS_LOCK) if order_store is not None else ORDERS_LOCK
    load_od = order_store.load_orders_data if order_store is not None else load_orders_data
    save_od = order_store.save_orders_data if order_store is not None else save_orders_data
    log_loader = load_log if load_log is not None else load_orders_log
    orders_path = getattr(order_store, "ORDERS_PATH", rt.ORDERS_PATH) if order_store is not None else rt.ORDERS_PATH

    with lock:
        orders_data = load_od()
        found_od = False
        if isinstance(orders_data, list):
            for order in orders_data:
                if str(order.get("order_id")) == str(order_id_funpay):
                    _apply_charge_and_net(
                        order, spent_f, currency_str, net_profit,
                        spent_rub=resolved_rub, fx_rate_to_rub=resolved_rate,
                    )
                    found_od = True
                    break
        if found_od:
            save_od(orders_data)

        try:
            orders_list = log_loader()
            updated = False
            if isinstance(orders_list, list):
                for rec in orders_list:
                    if str(rec.get("order_id")) == str(order_id_funpay):
                        _apply_charge_and_net(
                            rec, spent_f, currency_str, net_profit,
                            spent_rub=resolved_rub, fx_rate_to_rub=resolved_rate,
                        )
                        updated = True
                        break
            if updated:
                if write_log is not None:
                    write_log(orders_path, orders_list)
                else:
                    save_orders_log(orders_list)
        except Exception:
            rt.logger.exception(f"Ошибка обновления spent/chistota в логе заказов #{order_id_funpay}")


def _analytics_view_key(chat_id: Any, message_id: Any) -> Tuple[int, int]:
    c_id = safe_int(chat_id)
    m_id = safe_int(message_id)
    if c_id is None or m_id is None:
        return 0, 0
    return c_id, m_id


def _set_analytics_view_mode(chat_id: Any, message_id: Any, mode: str) -> None:
    normalized = "more" if str(mode or "").strip().lower() == "more" else "main"
    key = _analytics_view_key(chat_id, message_id)
    with rt.CONTEXT_LOCK:
        rt.ANALYTICS_VIEW_MODES[key] = {
            "mode": normalized,
            "created_ts": time.time(),
        }


def _get_analytics_view_mode(chat_id: Any, message_id: Any) -> str:
    key = _analytics_view_key(chat_id, message_id)
    with rt.CONTEXT_LOCK:
        entry = rt.ANALYTICS_VIEW_MODES.get(key)
        if isinstance(entry, dict):
            entry = dict(entry)
    if isinstance(entry, dict):
        return "more" if str(entry.get("mode") or "").strip().lower() == "more" else "main"
    if isinstance(entry, str) and entry.strip():
        return "more" if entry.strip().lower() == "more" else "main"
    return "main"


def _merged_orders() -> List[Dict]:
    return _analytics_merged_orders(load_orders=load_orders_data, load_log=None)


def get_statistics() -> Dict:
    return _analytics_get_statistics(load_merged=_merged_orders)


def update_order_charge_and_net(
    order_id_funpay: Any,
    spent: Any,
    currency: str = "USD",
    net_profit: Any = None,
    *,
    spent_rub: Optional[float] = None,
    fx_rate_to_rub: Optional[float] = None,
) -> None:
    _analytics_update_order_charge_and_net(
        str(order_id_funpay),
        spent,
        currency,
        net_profit,
        spent_rub=spent_rub,
        fx_rate_to_rub=fx_rate_to_rub,
    )


def get_period_analytics(period: str = "24h") -> Dict:
    return _analytics_get_period_analytics(period, load_merged=_merged_orders)


def _count_orders_awaiting_confirmation(period: str = "24h") -> int:
    return _analytics_count_awaiting_confirmation(period, load_merged=_merged_orders)


def format_analytics(period: str = "24h", mode: str = "main") -> str:
    return _analytics_format_analytics(
        period,
        mode,
        get_period=get_period_analytics,
        count_awaiting=_count_orders_awaiting_confirmation,
    )
