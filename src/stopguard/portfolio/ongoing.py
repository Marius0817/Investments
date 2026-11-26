"""Laufende Produktkosten (TER) der Fonds im Depot.

Die TER wird nicht gebucht, sondern steckt im Kurs – deshalb taucht sie in den Kosten aus dem
Scalable-Kassenbuch nicht auf. Die Werte kommen aus ``[instruments.<ISIN>] ter = 0.20`` (Factsheet).
Einzelaktien haben keine laufenden Produktkosten (TER 0).
"""

from __future__ import annotations

from typing import Any

from stopguard.portfolio.model import Instrument

PROJECTION_YEARS = (10, 20, 30)
ASSUMED_RETURN = 0.05


def build_ongoing_costs(positions: list[dict[str, Any]], instruments: dict[str, Instrument]) -> dict[str, Any]:
    open_pos = [p for p in positions if p["status"] == "open" and (p["value"] or 0) > 0]
    total = sum(p["value"] for p in open_pos)
    rows, missing = [], []
    known_value = 0.0
    weighted = 0.0
    for p in open_pos:
        inst = instruments.get(p["isin"])
        if inst is None:
            continue
        ter = inst.ter if inst.ter is not None else (0.0 if inst.kind == "stock" and not inst.is_fund else None)
        if ter is None:
            missing.append({"isin": p["isin"], "name": p["name"], "value": p["value"]})
            continue
        known_value += p["value"]
        weighted += p["value"] * ter / 100
        if inst.is_fund or ter > 0:
            rows.append({
                "isin": p["isin"],
                "name": p["name"],
                "value": p["value"],
                "ter": round(ter / 100, 6),
                "annual_cost": round(p["value"] * ter / 100, 2),
            })
    rows.sort(key=lambda r: -r["annual_cost"])
    w_ter = weighted / known_value if known_value else None
    projection = []
    if w_ter is not None and total:
        for years in PROJECTION_YEARS:
            gross = total * (1 + ASSUMED_RETURN) ** years
            net = total * (1 + ASSUMED_RETURN - w_ter) ** years
            projection.append({"years": years, "value_without_costs": round(gross, 2), "value_with_costs": round(net, 2),
                               "cost_effect": round(gross - net, 2)})
    return {
        "weighted_ter": round(w_ter, 6) if w_ter is not None else None,
        "annual_cost": round(weighted, 2) if known_value else None,
        "coverage": round(known_value / total, 4) if total else None,
        "by_position": rows,
        "missing": missing,
        "projection": projection,
        "assumed_return": ASSUMED_RETURN,
        "note": "TER laut Konfiguration (Factsheet). Sie steckt im Kurs und erscheint nicht als Buchung. "
                "Kosteneffekt: heutiger Depotwert, 5 % Rendite p. a. vor Kosten, ohne weitere Einzahlungen.",
    }
