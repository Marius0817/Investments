"""Datenmodell des Portfolio-Trackings (bewusst einfache Dataclasses, float-Arithmetik)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Teilfreistellung nach § 20 InvStG
TEILFREISTELLUNG = {
    "stock": 0.0,
    "equity_fund": 0.30,  # Aktienfonds (> 50 % Aktien), z. B. MSCI-World-ETF
    "mixed_fund": 0.15,  # Mischfonds (≥ 25 % Aktien)
    "real_estate_fund": 0.60,
    "foreign_real_estate_fund": 0.80,
    "bond_fund": 0.0,
    "other": 0.0,
}
FUND_KINDS = {"equity_fund", "mixed_fund", "real_estate_fund", "foreign_real_estate_fund", "bond_fund"}


@dataclass
class Instrument:
    isin: str
    name: str
    kind: str = "stock"
    ticker: str | None = None
    distributing: bool | None = None  # None = unbekannt → bei Fonds als thesaurierend behandelt
    tags: list[str] = field(default_factory=list)
    security_type: str | None = None
    benchmark: str | None = None  # ISIN eines abweichenden Vergleichsindex
    asset_class: str | None = None
    sector: str | None = None
    country: str | None = None
    region: str | None = None
    currency: str | None = None
    exposure: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def teilfreistellung(self) -> float:
        return TEILFREISTELLUNG.get(self.kind, 0.0)

    @property
    def is_fund(self) -> bool:
        return self.kind in FUND_KINDS

    @property
    def accumulating(self) -> bool:
        return self.is_fund and self.distributing is not True


@dataclass
class LedgerEvent:
    """Normalisierte Buchung des Verrechnungskontos.

    ``cash`` ist die vorzeichenbehaftete Wirkung auf das Konto (+ Zufluss, − Abfluss).
    """

    id: str
    when: datetime
    kind: str  # deposit | withdrawal | buy | sell | dividend | interest_income | loan_interest | fee | tax | tax_refund | transfer_in | transfer_out | other
    cash: float
    isin: str | None = None
    units: float = 0.0
    price: float | None = None  # Kurs je Stück (ohne Gebühren)
    fees: float = 0.0
    tax: float = 0.0  # tatsächlich einbehaltene Steuer (positiv = gezahlt)
    gross: float | None = None  # Brutto (Dividenden)
    description: str | None = None
    source: str = "scalable"
    raw_type: str | None = None

    @property
    def external(self) -> bool:
        """Externe Zahlungsströme für die Depot-IRR (Ein-/Auszahlungen)."""
        return self.kind in ("deposit", "withdrawal")


@dataclass
class LotExit:
    when: datetime
    units: float
    price: float
    fees: float = 0.0
    tax_actual: float | None = None
    ref: str | None = None


@dataclass
class Dividend:
    when: datetime
    net: float
    gross: float | None = None
    tax_actual: float | None = None
    approximate: bool = False


@dataclass
class Lot:
    """Ein Trade = ein Kauf (Lot) mit seinen (Teil-)Verkäufen nach FIFO."""

    id: str
    isin: str
    name: str
    open_date: datetime
    units: float
    price: float  # Kaufkurs je Stück
    fees: float = 0.0
    source: str = "scalable"
    origin: str = "single"  # single | savings_plan | transfer | import
    exits: list[LotExit] = field(default_factory=list)
    dividends: list[Dividend] = field(default_factory=list)
    # Kredit
    flagged_credit: bool = False  # Markierung aus Notion/Konfiguration
    credit_amount: float | None = None  # explizit finanzierter Betrag (Flag-Modus)
    credit_draws: list[tuple[datetime, float]] = field(default_factory=list)
    credit_repayments: list[tuple[datetime, float]] = field(default_factory=list)
    interest: list[tuple[datetime, float]] = field(default_factory=list)
    interest_estimated: bool = False
    # Steuern (gefüllt vom Steuermodell)
    vap_tax: list[tuple[datetime, float]] = field(default_factory=list)
    vap_per_unit: list[tuple[datetime, float]] = field(default_factory=list)
    # sonstiges
    manual_current_price: float | None = None
    ticker: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    open_event_id: str | None = None

    @property
    def invested(self) -> float:
        return self.units * self.price + self.fees

    @property
    def remaining_units(self) -> float:
        return max(0.0, self.units - sum(e.units for e in self.exits))

    @property
    def is_closed(self) -> bool:
        return self.remaining_units <= 1e-9

    @property
    def close_date(self) -> datetime | None:
        return max((e.when for e in self.exits), default=None) if self.is_closed else None

    def units_at(self, when: datetime) -> float:
        if when < self.open_date:
            return 0.0
        return max(0.0, self.units - sum(e.units for e in self.exits if e.when <= when))

    @property
    def credit_outstanding(self) -> float:
        return max(0.0, sum(a for _, a in self.credit_draws) - sum(a for _, a in self.credit_repayments))

    @property
    def credit_financed(self) -> float:
        return sum(a for _, a in self.credit_draws)
