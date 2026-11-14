"""Typed domain models, enums, and pure normalization helpers for AutoSMM."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import ipaddress
import re
import socket
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse, urlsplit


class BuyerFlowState(str, Enum):
    """Lifecycle stages for a FunPay order moving through the buyer dialog."""

    WAITING_LINK = "waiting_link"
    LINK_PAUSED = "link_paused"
    WAITING_CONFIRM = "waiting_confirm"
    PANEL_CREATED = "panel_created"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class FunPayStatus(str, Enum):
    """Normalized FunPay-side order status values."""

    OPEN = "open"
    CLOSED = "closed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


FUNPAY_STATUS_ALLOWED = {item.value for item in FunPayStatus}

_FUNPAY_STATUS_ALIASES = {
    "": None,
    "paid": FunPayStatus.OPEN.value,
    "open": FunPayStatus.OPEN.value,
    "opened": FunPayStatus.OPEN.value,
    "closed": FunPayStatus.CLOSED.value,
    "close": FunPayStatus.CLOSED.value,
    "refunded": FunPayStatus.REFUNDED.value,
    "refund": FunPayStatus.REFUNDED.value,
    "partially_refunded": FunPayStatus.PARTIALLY_REFUNDED.value,
    "partial_refund": FunPayStatus.PARTIALLY_REFUNDED.value,
    "partial": FunPayStatus.PARTIALLY_REFUNDED.value,
    "partiallyrefund": FunPayStatus.PARTIALLY_REFUNDED.value,
}


def safe_int(value: Any) -> Optional[int]:
    """Parse an integer while rejecting booleans and malformed values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except (ValueError, OverflowError):
            return None


def normalize_chat_id(value: Any) -> Any:
    """Normalize numeric chat_id to int while preserving opaque FunPay string IDs (e.g. users-...)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def safe_float(value: Any, default: float = 0.0) -> float:
    """Parse a float safely with fallback default, supporting comma decimals."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return float(str(value).replace(",", ".").strip())
        except (ValueError, TypeError):
            return default


def optional_float(value: Any) -> Optional[float]:
    """Parse an optional float returning None for empty or invalid values."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def normalize_funpay_status(value: Any) -> Optional[str]:
    """Normalize raw FunPay order statuses into canonical enum values."""
    raw_value = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _FUNPAY_STATUS_ALIASES.get(
        raw_value,
        raw_value if raw_value in FUNPAY_STATUS_ALLOWED else None,
    )
    return normalized if normalized in FUNPAY_STATUS_ALLOWED else None


def is_refunded_funpay_status(funpay_status: Any) -> bool:
    """Return True if the FunPay status represents a full or partial refund."""
    return normalize_funpay_status(funpay_status) in (
        FunPayStatus.REFUNDED.value,
        FunPayStatus.PARTIALLY_REFUNDED.value,
    )


def get_effective_funpay_status(order_record: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Resolve canonical FunPay status from an order mapping or legacy is_refunded flag."""
    if not isinstance(order_record, Mapping):
        return None
    normalized = normalize_funpay_status(order_record.get("funpay_status"))
    if normalized:
        return normalized
    if bool(order_record.get("is_refunded", False)):
        return FunPayStatus.REFUNDED.value
    return None


def is_order_refundedish(order_record: Optional[Mapping[str, Any]]) -> bool:
    """Return True when an order record indicates any FunPay refund state."""
    return is_refunded_funpay_status(get_effective_funpay_status(order_record))


def resolve_panel_order_id(order_record: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Extract canonical numeric panel order ID across current and legacy key aliases."""
    if not isinstance(order_record, Mapping):
        return None
    for field_name in ("panel_order_id", "twiboost_id", "id_zakaz"):
        value = safe_int(order_record.get(field_name))
        if value is not None:
            return value
    return None


def normalize_allowed_domain(domain_value: Any) -> str:
    """Normalize a user-supplied domain or URL string into a lowercase host."""
    raw_value = str(domain_value or "").strip().lower()
    if not raw_value:
        return ""
    raw_value = raw_value.replace("\\", "/")
    if "://" in raw_value:
        try:
            parsed = urlparse(raw_value)
        except ValueError:
            return ""
        raw_value = parsed.hostname or ""
    else:
        raw_value = raw_value.split("/", 1)[0]
        raw_value = raw_value.split("?", 1)[0]
        raw_value = raw_value.split("#", 1)[0]
    raw_value = raw_value.split(":", 1)[0].strip().strip(".")
    if raw_value.startswith("www."):
        raw_value = raw_value[4:]
    return raw_value


_INT_LITERAL_LABEL_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)$")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_disallowed_ip(ip_obj: Any) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return _is_disallowed_ip(ip_obj.ipv4_mapped)
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
    ):
        return True
    return isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NETWORK


def normalize_api_url(raw_url: Any, *, check_dns: bool = False) -> str:
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
    if str(parts.scheme or "").lower() not in ("http", "https"):
        raise ValueError("поддерживаются только HTTP/HTTPS ссылки")

    if not parts.netloc and parts.path:
        parts = urlsplit(f"https://{str(parts.path).lstrip('/')}")

    raw_netloc = str(parts.netloc or "").strip()
    if "@" in raw_netloc:
        raise ValueError("некорректный домен")

    authority = raw_netloc
    if authority.startswith("["):
        if "]:" in authority:
            port_str = authority.split("]:", 1)[1]
            if not port_str.isdigit():
                raise ValueError("некорректный порт")
    elif ":" in authority:
        _, port_str = authority.split(":", 1)
        if not port_str.isdigit():
            raise ValueError("некорректный порт")

    try:
        port = parts.port
    except ValueError as ex:
        raise ValueError("некорректный порт") from ex
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("некорректный порт")

    hostname = str(getattr(parts, "hostname", None) or "").strip().strip(".").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname or ("." not in hostname and ":" not in hostname):
        raise ValueError("не удалось распознать домен")
    if hostname == "localhost" or hostname.endswith((".local", ".localhost", ".internal", ".home.arpa")):
        raise ValueError("локальные и внутренние домены запрещены")

    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if _is_disallowed_ip(ip_obj):
            raise ValueError("локальные и приватные IP-адреса запрещены")
    else:
        labels = hostname.split(".")
        if labels and all(_INT_LITERAL_LABEL_RE.fullmatch(label or "") for label in labels):
            raise ValueError("локальные и приватные IP-адреса запрещены")
        try:
            socket.inet_aton(hostname)
        except OSError:
            pass
        else:
            raise ValueError("локальные и приватные IP-адреса запрещены")

        if (
            not re.fullmatch(r"[a-z0-9.-]+", hostname)
            or ".." in hostname
            or hostname.startswith(".")
            or hostname.endswith(".")
        ):
            raise ValueError("некорректный домен")

        if check_dns:
            try:
                addr_infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
            except socket.gaierror:
                addr_infos = []
            for _family, _socktype, _proto, _canonname, sockaddr in addr_infos:
                raw_ip = str(sockaddr[0]).split("%", 1)[0] if isinstance(sockaddr, tuple) and sockaddr else ""
                if not raw_ip:
                    continue
                try:
                    resolved_ip = ipaddress.ip_address(raw_ip)
                except ValueError:
                    continue
                if _is_disallowed_ip(resolved_ip):
                    raise ValueError("локальные и приватные IP-адреса запрещены")

    canonical_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        canonical_host = f"{canonical_host}:{port}"
    return f"https://{canonical_host}/api/v2"


def is_plausible_allowed_domain(domain_value: Any) -> bool:
    """Validate that a normalized domain string is a syntactically valid hostname."""
    domain = str(domain_value or "").strip().lower()
    if not domain or " " in domain or ".." in domain:
        return False
    if len(domain) > 253 or "." not in domain:
        return False
    for label in domain.split("."):
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not re.fullmatch(r"[a-z0-9-]+", label):
            return False
    return True


def extract_progress_values(order_record: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    """Compute consistent total/done/left/progress_percent counters for an order."""
    rec = order_record or {}
    total: Optional[int] = None
    for key in ("total_quantity", "quantity", "real_amount", "order_amount"):
        parsed = safe_int(rec.get(key))
        if parsed is not None:
            total = parsed
            break
    if total is None:
        total = 0
    done = safe_int(rec.get("done_quantity"))
    left = safe_int(rec.get("left_quantity"))
    if left is None:
        left = safe_int(rec.get("remains"))
    if done is None and total > 0 and left is not None:
        done = max(0, total - max(0, left))
    if left is None and total > 0 and done is not None:
        left = max(0, total - max(0, done))
    if done is None:
        done = 0
    if left is None:
        left = max(0, total - done) if total > 0 else 0
    progress_percent = int(max(0, min(100, round((done / total) * 100)))) if total > 0 else 0
    return {
        "total_quantity": total,
        "done_quantity": done,
        "left_quantity": left,
        "progress_percent": progress_percent,
    }


@dataclass
class OrderRecord:
    """Unified persisted domain representation of an AutoSMM order."""

    order_id: str
    chat_id: Any = None
    buyer_id: Any = None
    buyer_username: str = ""
    lot_title: str = ""
    service_name: str = ""
    service_id: Optional[int] = None
    service_number: int = 1
    panel_order_id: Optional[int] = None
    status: str = "pending"
    panel_status: Optional[str] = None
    funpay_status: Optional[str] = FunPayStatus.OPEN.value
    buyer_flow_state: Optional[str] = BuyerFlowState.WAITING_LINK.value
    customer_url: str = ""
    saved_link: Optional[str] = None
    quantity: int = 0
    total_quantity: int = 0
    done_quantity: int = 0
    left_quantity: int = 0
    progress_percent: int = 0
    order_amount: Any = None
    order_price: Any = None
    summa: float = 0.0
    spent: float = 0.0
    spent_rub: Optional[float] = None
    fx_rate_to_rub: Optional[float] = None
    chistota: float = 0.0
    currency: str = "RUB"
    fallback_used: Optional[bool] = None
    fallback_reason: Optional[str] = None
    completed_notification_sent: bool = False
    status_last_checked_at: Optional[str] = None
    status_last_error: Optional[str] = None
    incorrect_order_id_attempts: int = 0
    status_stop_reason: Optional[str] = None
    manual_check_alert_sent: bool = False
    funpay_status_checked_at: Optional[str] = None
    refund_detected_at: Optional[str] = None
    refund_source: Optional[str] = None
    panel_refund_cancel_attempted_at: Optional[str] = None
    panel_refund_cancel_result: Optional[str] = None
    last_prompt_ts: Optional[str] = None
    created_at: int = 0
    updated_at: Optional[str] = None
    date: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OrderRecord":
        """Build a normalized OrderRecord from a raw dict or legacy JSON record."""
        panel_oid = resolve_panel_order_id(raw)
        funpay_status = get_effective_funpay_status(raw)
        raw_summa = raw.get("summa")
        if raw_summa in (None, ""):
            raw_summa = raw.get("order_price")
        summa = safe_float(raw_summa, 0.0)
        raw_chistota = raw.get("chistota")
        chistota = safe_float(summa if raw_chistota in (None, "") else raw_chistota, 0.0)
        fallback_raw = raw.get("fallback_used")
        fallback_used = None if fallback_raw is None else bool(fallback_raw)
        parsed_service_num = safe_int(raw.get("service_number"))
        return cls(
            order_id=str(raw.get("order_id") or ""),
            chat_id=normalize_chat_id(raw.get("chat_id")),
            buyer_id=raw.get("buyer_id"),
            buyer_username=str(raw.get("buyer_username") or ""),
            lot_title=str(raw.get("lot_title") or ""),
            service_name=str(raw.get("service_name") or ""),
            service_id=safe_int(raw.get("service_id")),
            service_number=parsed_service_num if parsed_service_num is not None else 1,
            panel_order_id=panel_oid,
            status=str(raw.get("status") or "pending"),
            panel_status=raw.get("panel_status"),
            funpay_status=funpay_status,
            buyer_flow_state=raw.get("buyer_flow_state"),
            customer_url=str(raw.get("customer_url") or ""),
            saved_link=raw.get("saved_link"),
            quantity=safe_int(raw.get("quantity")) or 0,
            total_quantity=safe_int(raw.get("total_quantity")) or 0,
            done_quantity=safe_int(raw.get("done_quantity")) or 0,
            left_quantity=safe_int(raw.get("left_quantity")) or 0,
            progress_percent=safe_int(raw.get("progress_percent")) or 0,
            order_amount=raw.get("order_amount"),
            order_price=raw.get("order_price"),
            summa=summa,
            spent=safe_float(raw.get("spent"), 0.0),
            spent_rub=optional_float(raw.get("spent_rub")),
            fx_rate_to_rub=optional_float(raw.get("fx_rate_to_rub")),
            chistota=chistota,
            currency=str(raw.get("currency") or "RUB"),
            fallback_used=fallback_used,
            fallback_reason=raw.get("fallback_reason"),
            completed_notification_sent=bool(raw.get("completed_notification_sent", False)),
            status_last_checked_at=raw.get("status_last_checked_at"),
            status_last_error=raw.get("status_last_error"),
            incorrect_order_id_attempts=safe_int(raw.get("incorrect_order_id_attempts")) or 0,
            status_stop_reason=raw.get("status_stop_reason"),
            manual_check_alert_sent=bool(raw.get("manual_check_alert_sent", False)),
            funpay_status_checked_at=raw.get("funpay_status_checked_at"),
            refund_detected_at=raw.get("refund_detected_at"),
            refund_source=raw.get("refund_source"),
            panel_refund_cancel_attempted_at=raw.get("panel_refund_cancel_attempted_at"),
            panel_refund_cancel_result=raw.get("panel_refund_cancel_result"),
            last_prompt_ts=raw.get("last_prompt_ts"),
            created_at=safe_int(raw.get("created_at")) or 0,
            updated_at=raw.get("updated_at"),
            date=raw.get("date") or raw.get("date_str"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a clean dictionary compatible with legacy callers and templates."""
        data = asdict(self)
        data["id_zakaz"] = self.panel_order_id
        if self.date:
            data["date_str"] = self.date
        if self.fallback_used is None and self.fallback_reason in (None, ""):
            data.pop("fallback_used", None)
            data.pop("fallback_reason", None)
        return {k: v for k, v in data.items() if v is not None or k in ("id_zakaz", "panel_order_id")}


@dataclass(frozen=True)
class LotMatchResult:
    """Structured result of resolving a FunPay lot description against config."""

    ok: bool
    error: Optional[str] = None
    lot_key: Optional[str] = None
    lot_title: Optional[str] = None
    service_id: Optional[int] = None
    real_amount: Optional[int] = None
    service_number: int = 1
    matches: Optional[List[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary expected by existing order-matching call sites."""
        if self.ok:
            return {
                "ok": True,
                "service_id": self.service_id,
                "real_amount": self.real_amount,
                "service_number": self.service_number,
                "lot_key": self.lot_key,
                "lot_title": self.lot_title,
            }
        return {
            "ok": False,
            "error": self.error or "no_match",
            "matches": list(self.matches or []),
        }
