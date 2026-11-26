"""Dividendenprognose und -kalender für die nächsten 12 Monate.

Grundlage sind ausschließlich die eigenen Ausschüttungen aus dem Scalable-Kassenbuch:
Jede Zahlung der letzten 12 Monate wird je Stück auf den heutigen Bestand
hochgerechnet und ein Jahr später erneut erwartet. Bei Positionen, die noch kein
ganzes Jahr gehalten werden, ergänzt der erkannte Zahlungsrhythmus (monatlich,
quartalsweise, halbjährlich) die fehlenden Termine. Alle Werte sind Schätzungen
(netto nach einbehaltener Steuer, sofern Scalable sie so bucht).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any

from stopguard.portfolio.metrics import TradeResult

FREQ_LABELS = {12: "monatlich", 4: "quartalsweise", 2: "halbjährlich", 1: "jährlich"}
YEAR = timedelta(days=365)


@dataclass
class Payment:
    when: datetime
    net: float
    gross: float | None
    units: float

    @property
    def per_share(self) -> float:
        return self.net / self.units if self.units else 0.0

    @property
    def gross_per_share(self) -> float | None:
        return self.gross / self.units if self.gross is not None and self.units else None


def _payments(results: list[TradeResult]) -> dict[str, list[Payment]]:
    """Ausschüttungen je ISIN, gleiche Tage zusammengefasst, mit dem Bestand am Zahltag."""
    by_isin: dict[str, list[TradeResult]] = defaultdict(list)
    for r in results:
        by_isin[r.lot.isin].append(r)
    out: dict[str, list[Payment]] = {}
    for isin, rs in by_isin.items():
        days: dict[str, list] = defaultdict(lambda: [None, 0.0, 0.0, True])
        for r in rs:
            for d in r.lot.dividends:
                key = d.when.date().isoformat()
                slot = days[key]
                slot[0] = slot[0] or d.when
                slot[1] += d.net
                if d.gross is None:
                    slot[3] = False
                else:
                    slot[2] += d.gross
        pays = []
        for slot in days.values():
            when, net, gross, gross_known = slot
            units = sum(r.lot.units_at(when) for r in rs) or sum(r.lot.units for r in rs if r.lot.open_date <= when)
            if net > 0 and units > 0:
                pays.append(Payment(when, net, gross if gross_known else None, units))
        if pays:
            out[isin] = sorted(pays, key=lambda p: p.when)
    return out


def _frequency(pays: list[Payment]) -> int:
    if len(pays) < 2:
        return 1
    gap = median((b.when - a.when).days for a, b in pairwise(pays))
    return 12 if gap < 45 else 4 if gap < 135 else 2 if gap < 270 else 1


def build_dividend_forecast(results: list[TradeResult], positions: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    open_pos = {p["isin"]: p for p in positions if p["status"] == "open" and (p["units"] or 0) > 0}
    first_buy = {}
    for r in results:
        first_buy[r.lot.isin] = min(first_buy.get(r.lot.isin, r.lot.open_date), r.lot.open_date)
    horizon = now + YEAR
    payments: list[dict[str, Any]] = []
    by_position: list[dict[str, Any]] = []

    for isin, pays in _payments(results).items():
        pos = open_pos.get(isin)
        if not pos:
            continue
        units = pos["units"]
        recent = [p for p in pays if p.when > now - timedelta(days=380)]
        if not recent:
            continue
        freq = _frequency(recent)
        held_days = (now - first_buy[isin]).days
        # sicher: ein volles Jahr gehalten; mittel: Rhythmus aus ≥ 2 Zahlungen erkannt; gering: eine einzelne Zahlung
        confidence = "high" if held_days >= 330 else "medium" if len(recent) >= 2 else "low"
        planned: list[tuple[datetime, Payment, bool]] = []
        for p in recent:
            due = p.when + YEAR
            pending = due <= now  # letztes Jahr etwas später gezahlt – jetzt fällig
            planned.append((max(due, now + timedelta(days=1)), p, pending))
        # Rhythmus ergänzen, wenn die Position noch kein ganzes Jahr gehalten wird
        if held_days < 330 and len(recent) >= 2 and freq > 1:
            step = timedelta(days=365.25 / freq)
            latest = recent[-1]
            t = latest.when + step
            while t <= horizon:
                if t > now and all(abs((t - d).days) > step.days / 2 for d, _, _ in planned):
                    planned.append((t, latest, False))
                t += step
        total = 0.0
        total_gross = 0.0
        gross_known = True
        for due, p, pending in sorted(planned, key=lambda x: x[0]):
            if due > horizon:
                continue
            amount = p.per_share * units
            gross = p.gross_per_share * units if p.gross_per_share is not None else None
            total += amount
            if gross is None:
                gross_known = False
            else:
                total_gross += gross
            payments.append({
                "date": due.date().isoformat(),
                "isin": isin,
                "name": pos["name"],
                "amount": round(amount, 2),
                "gross": round(gross, 2) if gross is not None else None,
                "per_share": round(p.per_share, 4),
                "units": round(units, 6),
                "based_on": p.when.date().isoformat(),
                "pending": pending,
                "estimated": True,
            })
        trailing = sum(p.net for p in pays if p.when > now - YEAR)
        by_position.append({
            "isin": isin,
            "name": pos["name"],
            "amount": round(total, 2),
            "gross": round(total_gross, 2) if gross_known else None,
            "trailing_12m": round(trailing, 2),
            "frequency": freq,
            "frequency_label": FREQ_LABELS.get(freq, f"{freq}× im Jahr"),
            "payments": sum(1 for x in payments if x["isin"] == isin),
            "yield_on_cost": round(total / pos["cost_basis"], 5) if pos.get("cost_basis") else None,
            "yield_current": round(total / pos["value"], 5) if pos.get("value") else None,
            "confidence": confidence,
        })

    payments.sort(key=lambda x: (x["date"], x["name"]))
    by_position.sort(key=lambda x: -x["amount"])
    months: list[dict[str, Any]] = []
    y, m = now.year, now.month
    for _ in range(13):
        key = f"{y:04d}-{m:02d}"
        items = [x for x in payments if x["date"].startswith(key)]
        if key <= horizon.strftime("%Y-%m"):
            months.append({
                "month": key,
                "amount": round(sum(x["amount"] for x in items), 2),
                "items": [{"isin": x["isin"], "name": x["name"], "amount": x["amount"], "date": x["date"]} for x in items],
            })
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    total = sum(x["amount"] for x in payments)
    value = sum(p["value"] or 0 for p in open_pos.values())
    cost = sum(p["cost_basis"] or 0 for p in open_pos.values())
    return {
        "basis": "Ausschüttungen der letzten 12 Monate je Stück × heutiger Bestand; bei kürzerer Haltedauer ergänzt um den erkannten Zahlungsrhythmus",
        "estimated": True,
        "next_12m": round(total, 2),
        "yield_current": round(total / value, 5) if value else None,
        "yield_on_cost": round(total / cost, 5) if cost else None,
        "monthly": months,
        "payments": payments,
        "by_position": by_position,
    }


def add_growth(by_year: list[dict[str, Any]], first_year: int | None = None) -> list[dict[str, Any]]:
    """Wachstum der Ausschüttungen gegenüber dem Vorjahr.

    Das erste Depotjahr ist meist angebrochen – ein Vergleich damit wäre irreführend, daher erst ab dem
    Folgejahr eines vollen Jahres.
    """
    prev = None
    prev_year = None
    for row in by_year:
        full_prev = prev_year is not None and prev_year == row["year"] - 1 and (first_year is None or prev_year > first_year)
        row["growth"] = round(row["dividends"] / prev - 1, 4) if prev and full_prev else None
        prev = row["dividends"] or None
        prev_year = row["year"]
    return by_year
