"""Datenmodelle und Hilfsfunktionen für Beträge/Zeitpunkte."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def to_decimal(value: Any) -> Decimal | None:
    """Wandelt CLI-Werte (Zahl oder Dezimal-String) robust in Decimal um.

    Die Scalable CLI liefert Geldbeträge teils als Zahl, teils als String;
    nicht verfügbare Werte sind ``null`` und dürfen NICHT als 0 gelesen werden.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        # manche Felder sind {"amount": "...", "currency": "EUR"}
        for key in ("amount", "value"):
            if key in value:
                return to_decimal(value[key])
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_time(value: Any) -> datetime | None:
    """ISO-8601-Zeitstempel (auch {"time": ...}) -> timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("time") or value.get("date")
        if value is None:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_decimal(value: Decimal) -> str:
    """Decimal ohne Exponentialschreibweise und ohne überflüssige Nullen."""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_price(value: Decimal | None) -> str:
    """Kurs für die Anzeige: mindestens 2 Nachkommastellen, max. 4."""
    if value is None:
        return "–"
    text = fmt_decimal(value)
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[: max(2, min(4, len(frac)))]
    return f"{whole}.{frac}"


def round_down(value: Decimal, decimals: int) -> Decimal:
    quantum = Decimal(1).scaleb(-decimals)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def pct(value: Decimal | float | None, digits: int = 1) -> str:
    if value is None:
        return "–"
    return f"{float(value) * 100:+.{digits}f}%"


# ---------------------------------------------------------------------------
# Broker-Daten
# ---------------------------------------------------------------------------

@dataclass
class Holding:
    isin: str
    name: str
    quantity: Decimal
    blocked_quantity: Decimal = Decimal(0)
    pending_quantity: Decimal = Decimal(0)
    fifo_price: Decimal | None = None
    valuation: Decimal | None = None
    mid_price: Decimal | None = None
    currency: str | None = None
    quote_outdated: bool | None = None
    security_type: str | None = None


@dataclass
class Quote:
    isin: str
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    timestamp: datetime | None = None
    outdated: bool | None = None
    venue: str | None = None
    currency: str | None = None
    name: str | None = None

    def price(self, basis: str = "bid") -> Decimal | None:
        if basis == "mid":
            return self.mid or self.bid
        if basis == "ask":
            return self.ask or self.mid
        return self.bid or self.mid


@dataclass
class Transaction:
    """Eine Zeile aus ``sc broker transactions --json`` (vereinfacht)."""

    id: str
    summary_type: str | None
    type: str | None
    status: str | None
    time: datetime | None
    isin: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None
    security_transaction_type: str | None = None
    cash_transaction_type: str | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    is_cancellation: bool = False
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    OPEN_STATUSES = frozenset({"CREATED", "REQUESTED", "PENDING", "PARTIAL_FILLED", "CANCEL_REQUESTED"})
    DONE_STATUSES = frozenset({"FILLED", "SETTLED", "CONFIRMED"})

    @property
    def is_open_order(self) -> bool:
        return (self.status or "").upper() in self.OPEN_STATUSES and self.summary_type == "BrokerSecurityTransactionSummary"

    @property
    def is_executed(self) -> bool:
        return (self.status or "").upper() in self.DONE_STATUSES

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> "Transaction":
        return cls(
            id=str(item.get("id")),
            summary_type=item.get("summary_type"),
            type=item.get("type"),
            status=item.get("status"),
            time=parse_time(item.get("last_event_datetime")),
            isin=item.get("isin") or item.get("related_isin"),
            side=item.get("side"),
            quantity=to_decimal(item.get("quantity")),
            amount=to_decimal(item.get("amount")),
            security_transaction_type=item.get("security_transaction_type"),
            cash_transaction_type=item.get("cash_transaction_type"),
            limit_price=to_decimal(item.get("limit_price")),
            stop_price=to_decimal(item.get("stop_price")),
            is_cancellation=bool(item.get("is_cancellation")),
            description=item.get("description"),
            raw=item,
        )


ORDER_KINDS = ("stop", "stop-limit", "trailing-stop", "limit", "market", "unknown")


@dataclass
class OpenOrder:
    order_id: str
    isin: str
    side: str
    status: str
    quantity: Decimal | None
    stop_price: Decimal | None
    limit_price: Decimal | None
    kind: str
    trail_type: str | None = None  # PERCENTAGE | AMOUNT
    trail_offset: Decimal | None = None
    valid_until: datetime | None = None
    venue: str | None = None
    created: datetime | None = None

    @property
    def is_protective_sell(self) -> bool:
        return self.side == "SELL" and self.kind in ("stop", "stop-limit", "trailing-stop")

    def describe(self) -> str:
        if self.kind == "trailing-stop":
            unit = "%" if self.trail_type == "PERCENTAGE" else " EUR"
            trail = f"{fmt_decimal(self.trail_offset)}{unit}" if self.trail_offset is not None else "?"
            stop = f", akt. Stop {fmt_price(self.stop_price)}" if self.stop_price is not None else ""
            return f"Trailing {trail}{stop}"
        if self.kind == "stop-limit":
            return f"Stop-Limit {fmt_price(self.stop_price)}/{fmt_price(self.limit_price)}"
        if self.kind == "stop":
            return f"Stop {fmt_price(self.stop_price)}"
        if self.kind == "limit":
            return f"Limit {fmt_price(self.limit_price)}"
        return self.kind


@dataclass
class PriceAlert:
    alert_id: str
    isin: str
    price: Decimal
    is_active: bool
    direction: str | None = None
    triggered: datetime | None = None
