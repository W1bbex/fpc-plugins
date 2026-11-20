"""Read and merge accounting log entries backed by SQLite orders."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

from ..models import optional_float, safe_float
from . import db, db_orders

logger = logging.getLogger("FPC.autosmm")


def load_orders_log() -> List[Dict[str, Any]]:
    """Return log-style order accounting entries derived from SQLite."""
    entries: List[Dict[str, Any]] = []
    for rec in db_orders.list_orders():
        date_val = rec.get("date") or rec.get("date_str") or ""
        service_name = rec.get("service_name") or ""
        if not date_val and not service_name and safe_float(rec.get("summa"), 0.0) == 0.0:
            continue
        entries.append(
            {
                "date": date_val,
                "order_id": rec.get("order_id"),
                "summa": safe_float(rec.get("summa"), 0.0),
                "spent": safe_float(rec.get("spent"), 0.0),
                "currency": str(rec.get("currency") or "RUB"),
                "spent_rub": optional_float(rec.get("spent_rub")),
                "fx_rate_to_rub": optional_float(rec.get("fx_rate_to_rub")),
                "service_name": service_name,
                "chistota": safe_float(rec.get("chistota"), 0.0),
                "completed_notification_sent": bool(rec.get("completed_notification_sent", False)),
            }
        )
    return entries


def save_orders_log(log_entries: List[Dict[str, Any]]) -> bool:
    """Merge a list of legacy log entries into SQLite orders."""
    try:
        with db.get_db_lock():
            for entry in log_entries or []:
                if not isinstance(entry, dict):
                    continue
                oid = str(entry.get("order_id") or "").strip()
                if not oid:
                    continue
                existing = db_orders.get_order(oid)
                spent_val = optional_float(entry.get("spent"))
                spent_rub_val = optional_float(entry.get("spent_rub"))
                fx_rate_val = optional_float(entry.get("fx_rate_to_rub"))
                summa_val = optional_float(entry.get("summa"))
                chistota_val = optional_float(entry.get("chistota"))
                currency_val = str(entry.get("currency") or "").strip().upper()
                if existing is not None:
                    db_orders.update_order_fields(
                        oid,
                        date=entry.get("date") or existing.get("date"),
                        summa=summa_val if summa_val is not None else existing.get("summa"),
                        spent=spent_val if spent_val is not None else existing.get("spent"),
                        currency=currency_val or existing.get("currency") or "RUB",
                        spent_rub=spent_rub_val if spent_rub_val is not None else existing.get("spent_rub"),
                        fx_rate_to_rub=fx_rate_val if fx_rate_val is not None else existing.get("fx_rate_to_rub"),
                        service_name=entry.get("service_name") or existing.get("service_name"),
                        chistota=chistota_val if chistota_val is not None else existing.get("chistota"),
                        completed_notification_sent=bool(
                            entry.get("completed_notification_sent", existing.get("completed_notification_sent", False))
                        ),
                    )
                else:
                    db_orders.upsert_order_dict(
                        {
                            "order_id": oid,
                            "date": entry.get("date"),
                            "summa": summa_val if summa_val is not None else 0.0,
                            "spent": spent_val if spent_val is not None else 0.0,
                            "currency": currency_val or "RUB",
                            "spent_rub": spent_rub_val,
                            "fx_rate_to_rub": fx_rate_val,
                            "service_name": entry.get("service_name") or "",
                            "chistota": chistota_val if chistota_val is not None else 0.0,
                            "completed_notification_sent": bool(entry.get("completed_notification_sent", False)),
                            "status": "unknown",
                            "funpay_status": None,
                        }
                    )
        return True
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        logger.error(f"Ошибка при сохранении журнала заказов в SQLite: {exc}")
        return False
