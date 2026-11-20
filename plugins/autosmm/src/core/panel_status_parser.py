"""Interpret single and batch order status responses from SMM panels."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import safe_int as _safe_int
from .panel_errors import _map_panel_status_from_text, _stringify_for_matching


def _extract_order_id_from_obj(obj: Any) -> Optional[int]:
    if isinstance(obj, dict):
        for key in ("order", "id", "order_id"):
            order_id = _safe_int(obj.get(key))
            if order_id is not None:
                return order_id
    return None


def _normalize_runtime_status(status_value: Any) -> str:
    haystack = str(status_value or "").strip().lower()
    if not haystack:
        return "unknown"
    if "partial" in haystack:
        return "partial"
    if "completed" in haystack:
        return "completed"
    if "canceled" in haystack or "cancelled" in haystack or "refunded" in haystack:
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
                "currency": entry.get("currency"),
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
                "currency": None,
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
