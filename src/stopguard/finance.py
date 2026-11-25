"""Renditeberechnung: XIRR (IRR mit Datumsangaben) und IRR-basierte Stop-Kurse.

Konvention: Zahlungen aus Sicht des Anlegers –
Käufe sind negativ (Geld fließt ab), Verkäufe/Ausschüttungen positiv.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Sequence

from stopguard.models import Holding, Transaction

DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class CashFlow:
    when: datetime
    amount: float  # < 0 Kauf, > 0 Verkauf/Ausschüttung
    shares_delta: float = 0.0
    kind: str = "trade"
    ref: str = ""


def _years(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400.0 / DAYS_PER_YEAR


def _growth(rate: float, years: float) -> float:
    """(1 + rate) ** years, overflow-sicher."""
    if rate <= -1.0:
        return 0.0 if years > 0 else math.inf
    try:
        return math.exp(years * math.log1p(rate))
    except OverflowError:
        return math.inf


def xnpv(rate: float, flows: Sequence[tuple[datetime, float]]) -> float:
    t0 = min(d for d, _ in flows)
    total = 0.0
    for when, amount in flows:
        g = _growth(rate, _years(t0, when))
        if g == 0.0:
            return math.inf if amount > 0 else -math.inf
        total += amount / g
    return total


def xirr(flows: Sequence[tuple[datetime, float]], *, max_rate: float = 1e6) -> float | None:
    """Jährlicher interner Zinsfuß (wie Excel XINTZINSFUSS) per robuster Bisektion.

    Gibt ``None`` zurück, wenn keine eindeutige Lösung existiert
    (z. B. nur Käufe, keine Bewertung).
    """
    flows = [(d, float(a)) for d, a in flows if a != 0]
    if len(flows) < 2:
        return None
    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    span_days = (max(d for d, _ in flows) - min(d for d, _ in flows)).total_seconds() / 86400
    if span_days < 1e-6:
        return None

    lo, hi = -0.999999, 1.0
    f_lo = xnpv(lo, flows)
    f_hi = xnpv(hi, flows)
    while f_lo * f_hi > 0 and hi < max_rate:
        hi *= 4
        f_hi = xnpv(hi, flows)
    if f_lo * f_hi > 0 or math.isnan(f_lo) or math.isnan(f_hi):
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, flows)
        if f_mid == 0 or (hi - lo) < 1e-12:
            return mid
        if (f_lo < 0) == (f_mid < 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return (lo + hi) / 2


def floor_price_for_irr(
    flows: Iterable[CashFlow],
    shares: float,
    target_rate: float,
    at: datetime,
    exit_costs: float = 0.0,
) -> float | None:
    """Kurs, bei dem ein Verkauf aller ``shares`` zum Zeitpunkt ``at`` genau
    die Zielrendite ``target_rate`` p.a. (XIRR) ergibt.

    Geschlossene Lösung: Alle Zahlungen werden mit der Zielrendite auf ``at``
    aufgezinst; der Verkaufserlös muss sie exakt ausgleichen::

        shares * P - Kosten + Σ CF_i · (1+r)^((at - t_i)/365) = 0
    """
    if shares <= 0:
        return None
    future_value = 0.0
    for cf in flows:
        future_value += cf.amount * _growth(target_rate, _years(cf.when, at))
    price = (-future_value + exit_costs) / shares
    return price if price > 0 else None


@dataclass
class PositionReturns:
    """Rendite-Kennzahlen einer Position zum Bewertungszeitpunkt."""

    flows: list[CashFlow]
    first_buy: datetime | None
    shares: float
    invested: float  # Summe Käufe (positiv)
    received: float  # Summe Verkäufe + Ausschüttungen
    market_value: float
    irr: float | None  # p.a., 0.05 = 5 %
    total_return: float | None  # (Wert + Rückflüsse - Einsatz) / Einsatz
    holding_days: float
    quality: str  # "exact" | "approx" | "override"
    notes: list[str] = field(default_factory=list)


def build_cash_flows(
    isin: str,
    transactions: Iterable[Transaction],
    holding: Holding,
    *,
    entry_date: datetime | None = None,
    entry_price: Decimal | None = None,
    tolerance: float = 1e-4,
) -> tuple[list[CashFlow], str, list[str]]:
    """Erzeugt die Zahlungsreihe der *aktuellen* Haltephase einer Position.

    - Käufe/Verkäufe (inkl. Sparplanausführungen) aus der Transaktionshistorie
    - Ausschüttungen (Cash-Transaktion DISTRIBUTION mit related_isin)
    - Wird die Position zwischendurch komplett verkauft, beginnt die Reihe neu.
    - Passt die rekonstruierte Stückzahl nicht zum Depotbestand (z. B. Depot-
      übertrag ohne Kaufkurs), wird auf eine Näherung mit Einstandskurs
      (FIFO) zurückgefallen – oder auf ``entry_date``/``entry_price`` aus der
      Konfiguration.
    """
    notes: list[str] = []
    qty_now = float(holding.quantity)

    if entry_date is not None and (entry_price is not None or holding.fifo_price is not None):
        price = float(entry_price if entry_price is not None else holding.fifo_price)  # type: ignore[arg-type]
        notes.append("Einstieg aus Konfiguration (entry_date/entry_price)")
        return [CashFlow(entry_date, -price * qty_now, qty_now, "override")], "override", notes

    relevant = [
        t
        for t in transactions
        if t.time is not None and not t.is_cancellation and t.is_executed and (t.isin == isin)
    ]
    relevant.sort(key=lambda t: t.time)  # type: ignore[arg-type, return-value]

    flows: list[CashFlow] = []
    balance = 0.0
    for t in relevant:
        if t.summary_type == "BrokerSecurityTransactionSummary" and t.side in ("BUY", "SELL"):
            qty = float(t.quantity or 0)
            amt = abs(float(t.amount or 0))
            if qty <= 0 or amt <= 0:
                continue
            if t.side == "BUY":
                if balance <= tolerance:
                    flows = []  # neue Haltephase
                    balance = 0.0
                flows.append(CashFlow(t.time, -amt, qty, "buy", t.id))  # type: ignore[arg-type]
                balance += qty
            else:
                flows.append(CashFlow(t.time, amt, -qty, "sell", t.id))  # type: ignore[arg-type]
                balance -= qty
        elif t.summary_type == "BrokerCashTransactionSummary" and (t.cash_transaction_type or "").upper() in (
            "DISTRIBUTION",
            "DIVIDEND",
        ):
            amt = abs(float(t.amount or 0))
            if amt > 0 and balance > tolerance:
                flows.append(CashFlow(t.time, amt, 0.0, "distribution", t.id))  # type: ignore[arg-type]

    if flows and abs(balance - qty_now) <= max(tolerance, qty_now * 1e-6):
        return flows, "exact", notes

    # Fallback: Näherung über Einstandskurs
    if holding.fifo_price is None:
        notes.append("Keine verwertbare Historie und kein Einstandskurs – Rendite nicht berechenbar")
        return [], "approx", notes
    first_buy = next((f.when for f in flows if f.kind == "buy"), None)
    if first_buy is None:
        first_buy = next((t.time for t in relevant), None)
    if first_buy is None:
        notes.append("Kein Kaufdatum gefunden – bitte entry_date in der Konfiguration setzen")
        return [], "approx", notes
    notes.append(
        f"Historie unvollständig (rekonstruiert {balance:g} Stk., Depot {qty_now:g} Stk.) – "
        "Näherung mit Einstandskurs ab erstem Kauf"
    )
    return [CashFlow(first_buy, -float(holding.fifo_price) * qty_now, qty_now, "approx")], "approx", notes


def position_returns(
    flows: list[CashFlow],
    shares: float,
    price: float | None,
    at: datetime,
    *,
    quality: str = "exact",
    notes: list[str] | None = None,
    exit_costs: float = 0.0,
) -> PositionReturns:
    invested = -sum(f.amount for f in flows if f.amount < 0)
    received = sum(f.amount for f in flows if f.amount > 0)
    market_value = shares * price if price is not None else 0.0
    first_buy = min((f.when for f in flows), default=None)
    holding_days = (at - first_buy).total_seconds() / 86400 if first_buy else 0.0

    irr: float | None = None
    total_return: float | None = None
    if flows and price is not None and invested > 0:
        terminal = market_value - exit_costs
        irr = xirr([(f.when, f.amount) for f in flows] + [(at, terminal)])
        total_return = (terminal + received - invested) / invested
    return PositionReturns(
        flows=flows,
        first_buy=first_buy,
        shares=shares,
        invested=invested,
        received=received,
        market_value=market_value,
        irr=irr,
        total_return=total_return,
        holding_days=holding_days,
        quality=quality,
        notes=list(notes or []),
    )
