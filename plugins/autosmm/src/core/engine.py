"""Lot resolution and buyer flow state machine engine for AutoSMM."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import runtime as rt
from ..models import (
    BuyerFlowState,
    LotMatchResult,
    is_order_refundedish as _is_order_refundedish,
    resolve_panel_order_id as _get_panel_order_id,
    safe_int as _safe_int,
)
from ..storage.storage import CONFIG_LOCK

logger = logging.getLogger("FPC.autosmm")

BUYER_STATE_WAITING_LINK = BuyerFlowState.WAITING_LINK.value
BUYER_STATE_LINK_PAUSED = BuyerFlowState.LINK_PAUSED.value
BUYER_STATE_WAITING_CONFIRM = BuyerFlowState.WAITING_CONFIRM.value
BUYER_STATE_PANEL_CREATED = BuyerFlowState.PANEL_CREATED.value
BUYER_STATE_COMPLETED = BuyerFlowState.COMPLETED.value
BUYER_STATE_FAILED = BuyerFlowState.FAILED.value
BUYER_STATE_REFUNDED = BuyerFlowState.REFUNDED.value

_BUYER_EXPLICIT_STATES = {item.value for item in BuyerFlowState}


def _snapshot_lot_mapping(mapping: Any) -> Dict[str, Any]:
    if not isinstance(mapping, Mapping):
        return {}
    with CONFIG_LOCK:
        return {
            str(key): dict(val) if isinstance(val, Mapping) else val
            for key, val in list(mapping.items())
        }


def _engine_find_lot_matches_by_description(description: Any, *, mapping: Any) -> List[Tuple[str, Dict]]:
    description_text = str(description or "")
    matches: List[Tuple[str, Dict]] = []
    for lot_key, lot_data in _snapshot_lot_mapping(mapping).items():
        if not isinstance(lot_data, dict):
            continue
        lot_name = str(lot_data.get("name") or "").strip()
        if not lot_name:
            continue
        try:
            if re.search(re.escape(lot_name), description_text, re.IGNORECASE):
                matches.append((str(lot_key), lot_data))
        except re.error as exc:
            logger.debug(f"Ошибка regex при сопоставлении лота {lot_key}: {exc}")
    return matches


def _engine_resolve_lot_by_description(description: str, order_amount: int, *, mapping: Any) -> Dict[str, Any]:
    matches = _engine_find_lot_matches_by_description(description, mapping=mapping)
    if not matches:
        logger.warning("LOT MATCH FAILED: no matches for description=%r", str(description or "")[:300])
        return LotMatchResult(ok=False, error="no_match", matches=[]).to_dict()
    if len(matches) > 1:
        safe_matches = [
            {
                "lot_key": lot_key,
                "name": lot_data.get("name"),
                "service_id": lot_data.get("service_id"),
                "quantity": lot_data.get("quantity"),
                "service_number": lot_data.get("service_number", 1),
            }
            for lot_key, lot_data in matches
        ]
        logger.warning("LOT MATCH AMBIGUOUS: found %s matches for description=%r", len(matches), str(description or "")[:300])
        return LotMatchResult(ok=False, error="ambiguous_match", matches=safe_matches).to_dict()

    lot_key, lot_data = matches[0]
    base_q = _safe_int(lot_data.get("quantity")) or 0
    amount = _safe_int(order_amount)
    if amount is None:
        amount = 1
    real_q = base_q * amount
    return LotMatchResult(
        ok=True,
        service_id=lot_data.get("service_id"),
        real_amount=real_q,
        service_number=lot_data.get("service_number", 1),
        lot_key=lot_key,
        lot_title=lot_data.get("name") or str(description or ""),
    ).to_dict()


def _is_lot_match_no_match(result: Dict[str, Any]) -> bool:
    return not bool(result.get("ok")) and str(result.get("error") or "") == "no_match"


def _is_lot_match_ambiguous(result: Dict[str, Any]) -> bool:
    return not bool(result.get("ok")) and str(result.get("error") or "") == "ambiguous_match"


def _find_lot_data_for_order(
    cfg: Dict,
    service_id: Any,
    service_number: Any,
    lot_title: Optional[str] = None,
    lot_key: Optional[str] = None,
) -> Optional[Dict]:
    lot_map = cfg.get("lot_mapping", {})
    if not isinstance(lot_map, dict):
        return None

    def _matches_primary(lot_info: Mapping[str, Any]) -> bool:
        return (
            str(lot_info.get("service_id")) == str(service_id)
            and str(lot_info.get("service_number", 1)) == str(service_number)
        )

    def _matches_name(lot_info: Mapping[str, Any]) -> bool:
        return bool(lot_title) and str(lot_info.get("name") or "") == str(lot_title)

    valid_entries = [
        (str(key), lot_info)
        for key, lot_info in lot_map.items()
        if isinstance(lot_info, dict)
    ]

    if lot_key not in (None, ""):
        candidate = lot_map.get(str(lot_key))
        if isinstance(candidate, dict):
            if (not lot_title or _matches_name(candidate)) and (
                service_id in (None, "") or _matches_primary(candidate)
            ):
                return candidate
            if not lot_title and service_id in (None, ""):
                return candidate

    if lot_title not in (None, ""):
        name_primary_matches = [
            lot_info for _, lot_info in valid_entries
            if _matches_name(lot_info) and _matches_primary(lot_info)
        ]
        if name_primary_matches:
            return name_primary_matches[0]
        name_only_matches = [lot_info for _, lot_info in valid_entries if _matches_name(lot_info)]
        if len(name_only_matches) == 1:
            return name_only_matches[0]
        return None

    if lot_key not in (None, ""):
        return None

    primary_matches = [lot_info for _, lot_info in valid_entries if _matches_primary(lot_info)]
    if len(primary_matches) == 1:
        return primary_matches[0]
    return None


def _get_lot_fallback_targets(
    cfg: Dict,
    service_id: Any,
    service_number: Any,
    *,
    lot_key: Optional[str] = None,
    lot_title: Optional[str] = None,
) -> Tuple[Optional[int], Optional[int]]:
    lot_data = _find_lot_data_for_order(
        cfg,
        service_id,
        service_number,
        lot_title=lot_title,
        lot_key=lot_key,
    )
    if not lot_data:
        return None, None
    return lot_data.get("fallback_service_id"), lot_data.get("fallback_service_number")


def _detect_fallback_reason(primary_error: str, probe_state: Optional[str] = None) -> Optional[str]:
    primary_error = str(primary_error or "unknown")
    if primary_error in ("service_inactive", "insufficient_funds", "user_inactive"):
        return primary_error
    if primary_error == "validation_error":
        return "validation_error"
    if primary_error == "unsupported_action":
        return "unsupported_action"
    return probe_state or primary_error


def _has_panel_order(order_record: Optional[Dict]) -> bool:
    return _get_panel_order_id(order_record) is not None


def _state_from_step(step_value: Any) -> str:
    step = str(step_value or "").strip().lower()
    if step == "await_confirm":
        return BUYER_STATE_WAITING_CONFIRM
    if step in ("paused", "link_paused"):
        return BUYER_STATE_LINK_PAUSED
    return BUYER_STATE_WAITING_LINK


def _step_from_state(state_value: Any) -> str:
    state = str(state_value or "").strip().lower()
    if state == BUYER_STATE_WAITING_CONFIRM:
        return "await_confirm"
    if state == BUYER_STATE_LINK_PAUSED:
        return "paused"
    return "await_link"


def _get_buyer_flow_state(order_record: Optional[Dict]) -> str:
    if not isinstance(order_record, dict):
        return BUYER_STATE_WAITING_LINK
    if _is_order_refundedish(order_record):
        return BUYER_STATE_REFUNDED

    explicit_state = str(order_record.get("buyer_flow_state") or "").strip().lower()
    panel_status = str(order_record.get("status") or order_record.get("panel_status") or "").strip().lower()

    if _has_panel_order(order_record):
        if panel_status == "completed":
            return BUYER_STATE_COMPLETED
        if panel_status in ("fail", "failed", "canceled", "cancelled", "partial", "unknown"):
            return BUYER_STATE_FAILED
        return BUYER_STATE_PANEL_CREATED

    if explicit_state in _BUYER_EXPLICIT_STATES:
        return explicit_state
    return _state_from_step(order_record.get("step"))


def _find_lot_matches_by_description(description: Any) -> List[Tuple[str, Dict]]:
    with CONFIG_LOCK:
        mapping_copy = dict(rt.lot_mapping)
    return _engine_find_lot_matches_by_description(description, mapping=mapping_copy)


def resolve_lot_by_description(description: str, order_amount: int) -> Dict[str, Any]:
    with CONFIG_LOCK:
        mapping_copy = dict(rt.lot_mapping)
    return _engine_resolve_lot_by_description(description, order_amount, mapping=mapping_copy)
