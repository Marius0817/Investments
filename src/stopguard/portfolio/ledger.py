"""Kassenbuch: Scalable-Transaktionen → normalisierte Buchungen (LedgerEvent).

Vorzeichen-Konvention der Scalable CLI ist nicht dokumentiert (Beträge werden meist
positiv geliefert). Deshalb bestimmt der *Typ* die Richtung; nur wo der Typ mehrdeutig
ist (Zinsen), entscheiden Vorzeichen und Beschreibung. Unbekannte Typen werden als
``other`` mit dem gelieferten Vorzeichen verbucht und im Datenqualitäts-Bericht gemeldet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stopguard.config import CreditSettings
from stopguard.models import parse_time, to_decimal
from stopguard.portfolio.model import LedgerEvent

EXECUTED = {"FILLED", "SETTLED", "CONFIRMED", "PARTIAL_FILLED"}

INFLOW_TYPES = {
    "DEPOSIT": "deposit",
    "CASH_TRANSFER_IN": "deposit",
    "TRANSFER_IN": "deposit",
    "POCKET_MONEY": "deposit",
    "DISTRIBUTION": "dividend",
    "DIVIDEND": "dividend",
    "REINVESTMENT_DISTRIBUTION": "dividend",
    "TAX_RETURN": "tax_refund",
    "TAX_REFUND": "tax_refund",
}
OUTFLOW_TYPES = {
    "WITHDRAWAL": "withdrawal",
    "CASH_TRANSFER_OUT": "withdrawal",
    "TRANSFER_OUT": "withdrawal",
    "FEE": "fee",
    "TAX": "tax",
}


def _f(v: Any) -> float | None:
    d = to_decimal(v)
    return float(d) if d is not None else None


@dataclass
class LedgerResult:
    events: list[LedgerEvent]
    unknown_types: dict[str, int] = field(default_factory=dict)
    without_details: int = 0
    skipped: int = 0


def _trade_amounts(details: dict[str, Any] | None) -> tuple[float | None, float | None, float, float | None]:
    """(Stück, Durchschnittskurs, Gebühren, Steuern) aus den Transaktionsdetails."""
    if not details:
        return None, None, 0.0, None
    st = details.get("security_trade") or {}
    shares = st.get("number_of_shares") or {}
    units = _f(shares.get("filled")) if isinstance(shares, dict) else _f(shares)
    if not units:
        units = _f(shares.get("total")) if isinstance(shares, dict) else None
    price = _f(st.get("average_price"))
    amounts = st.get("trade_transaction_amounts") or {}
    fee_parts = [_f(amounts.get("transaction_fee")), _f(amounts.get("venue_fee")), _f(amounts.get("crypto_spread_fee"))]
    fees = sum(abs(x) for x in fee_parts if x)
    if not fees:
        fees = sum(abs(x) for x in (_f(st.get("fee")), _f(st.get("transactional_fee"))) if x)
    tax = _f(amounts.get("tax_amount"))
    if tax is None:
        tax = _f(st.get("taxes"))
    return units, price, fees, tax


def build_ledger(transactions: list[dict[str, Any]], credit: CreditSettings) -> LedgerResult:
    """Erzeugt das chronologische Kassenbuch aus den gespeicherten Scalable-Transaktionen."""
    res = LedgerResult(events=[])
    keywords = [k.lower() for k in credit.loan_interest_keywords]
    for t in transactions:
        when = parse_time(t.get("last_event_datetime"))
        status = str(t.get("status") or "").upper()
        if when is None or status not in EXECUTED or t.get("is_cancellation"):
            res.skipped += 1
            continue
        details = t.get("_details")
        stype = t.get("summary_type")
        amount = _f(t.get("amount"))
        desc = t.get("description")
        tid = str(t.get("id"))

        if stype == "BrokerSecurityTransactionSummary" and t.get("side") in ("BUY", "SELL"):
            units, price, fees, tax = _trade_amounts(details)
            if details is None:
                res.without_details += 1
            units = units or abs(_f(t.get("quantity")) or 0)
            if not units:
                res.skipped += 1
                continue
            gross = abs(amount or 0)
            if price is None:
                price = gross / units if units else 0.0
            market = units * price
            if t["side"] == "BUY":
                cash = -(market + fees) if details else -gross
                kind = "buy"
            else:
                cash = (market - fees - (tax or 0)) if details else gross
                kind = "sell"
            res.events.append(
                LedgerEvent(tid, when, kind, cash, t.get("isin"), units, price, fees, tax or 0.0, None, desc, raw_type=t.get("security_transaction_type"))
            )
            continue

        if stype == "BrokerNonTradeSecurityTransactionSummary":
            nt = str(t.get("non_trade_security_transaction_type") or "").upper()
            units = abs(_f(t.get("quantity")) or 0)
            if not units:
                res.skipped += 1
                continue
            kind = "transfer_in" if nt.endswith("IN") else "transfer_out" if nt.endswith("OUT") else "other"
            if kind == "other":
                res.unknown_types[f"security:{nt}"] = res.unknown_types.get(f"security:{nt}", 0) + 1
            price = abs(amount) / units if amount else None
            res.events.append(LedgerEvent(tid, when, kind, 0.0, t.get("isin"), units, price, description=desc, raw_type=nt))
            continue

        if stype == "BrokerCashTransactionSummary":
            ctype = str(t.get("cash_transaction_type") or "").upper()
            value = abs(amount or 0)
            isin = t.get("related_isin")
            gross = None
            tax_actual = 0.0
            cd = (details or {}).get("cash") or {}
            tax_details = cd.get("tax_details") or {}
            if ctype in INFLOW_TYPES:
                kind = INFLOW_TYPES[ctype]
                cash = value
                if kind == "dividend":
                    gross = _f(tax_details.get("gross_amount"))
                    tax_actual = abs(_f(tax_details.get("tax_amount")) or 0.0)
                    if gross is None and tax_actual:
                        gross = value + tax_actual
            elif ctype in OUTFLOW_TYPES:
                kind = OUTFLOW_TYPES[ctype]
                cash = -value
                if kind == "tax" and amount is not None and amount < 0 and "erstatt" in (desc or "").lower():
                    kind, cash = "tax_refund", value
            elif ctype == "INTEREST":
                text = (desc or "").lower()
                is_loan = (amount is not None and amount < 0) or any(k in text for k in keywords)
                kind = "loan_interest" if is_loan else "interest_income"
                cash = -value if is_loan else value
            else:
                kind = "other"
                cash = amount or 0.0
                res.unknown_types[ctype or "?"] = res.unknown_types.get(ctype or "?", 0) + 1
            res.events.append(
                LedgerEvent(tid, when, kind, cash, isin, gross=gross, tax=tax_actual, description=desc, raw_type=ctype)
            )
            continue

        if stype == "BrokerEltifTransactionSummary":
            res.unknown_types["eltif"] = res.unknown_types.get("eltif", 0) + 1
        res.skipped += 1

    # gleiche Zeitpunkte: erst Zuflüsse, dann Abflüsse (vermeidet scheinbare Kredit-Spitzen)
    res.events.sort(key=lambda e: (e.when, 0 if e.cash >= 0 else 1, e.id))
    return res


def cash_balance(events: list[LedgerEvent]) -> float:
    return sum(e.cash for e in events)
