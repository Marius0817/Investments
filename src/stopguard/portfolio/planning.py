"""Planung: Zielallokation mit Rebalancing-Rechner und Vermögensprognose.

Beides sind reine Rechenhilfen. stopguard kauft nichts – die Ergebnisse zeigen,
wie sich ein Betrag verteilen ließe oder wie sich das Vermögen unter Annahmen
entwickeln könnte.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from stopguard.models import utcnow
from stopguard.portfolio.risk import annualised_volatility

# ---------------------------------------------------------------------------
# Zielallokation
# ---------------------------------------------------------------------------


class TargetsStore:
    """Zielgewichte je Position (ISIN → Prozent) in ``<state_dir>/targets.json``."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "targets.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"targets": {}, "updated_at": None}
        return {"targets": {str(k).upper(): float(v) for k, v in (data.get("targets") or {}).items()}, "updated_at": data.get("updated_at")}

    def save(self, targets: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, float] = {}
        for k, v in targets.items():
            isin = str(k).strip().upper()
            if len(isin) != 12:
                raise ValueError(f"'{k}' ist keine ISIN")
            try:
                pct = float(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{isin}: Prozentwert erwartet") from exc
            if not 0 <= pct <= 100:
                raise ValueError(f"{isin}: Zielgewicht muss zwischen 0 und 100 % liegen")
            if pct > 0:
                clean[isin] = round(pct, 3)
        if clean and abs(sum(clean.values()) - 100) > 0.5:
            raise ValueError(f"Die Zielgewichte ergeben {sum(clean.values()):.1f} % statt 100 %")
        doc = {"targets": clean, "updated_at": utcnow().isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return doc


def rebalance(
    positions: list[dict[str, Any]],
    targets: dict[str, float],
    amount: float = 0.0,
    *,
    mode: str = "buy",
    min_order: float = 0.0,
    tax_rate: float | None = None,
) -> dict[str, Any]:
    """Verteilt ``amount`` auf die Positionen mit Ziel.

    ``mode="buy"``: nur Käufe – die Positionen, die am weitesten unter ihrem Ziel liegen,
    werden zuerst aufgefüllt (Wasserstand-Verfahren). Keine Verkäufe, also keine Steuern.
    ``mode="full"``: exakt auf die Zielgewichte, auch mit Verkäufen; die Steuer auf
    Verkäufe wird anteilig aus der latenten Steuer der Position geschätzt.
    """
    if mode not in ("buy", "full"):
        raise ValueError("mode muss 'buy' oder 'full' sein")
    if amount < 0:
        raise ValueError("Betrag darf nicht negativ sein")
    by_isin = {p["isin"]: p for p in positions}
    rows = []
    for isin, pct in targets.items():
        p = by_isin.get(isin)
        rows.append({
            "isin": isin,
            "name": p["name"] if p else isin,
            "value": float((p or {}).get("value") or 0.0) if p and p["status"] == "open" else 0.0,
            "target": pct / 100,
            "latent_tax": float((p or {}).get("latent_tax") or 0.0),
            "held": bool(p and p["status"] == "open"),
        })
    total_t = sum(r["target"] for r in rows)
    if not rows or total_t <= 0:
        return {"rows": [], "amount": amount, "mode": mode, "error": "Noch keine Zielgewichte festgelegt."}
    for r in rows:
        r["target"] /= total_t
    untargeted = [p for p in positions if p["status"] == "open" and p["isin"] not in targets and (p["value"] or 0) > 0]
    value_now = sum(r["value"] for r in rows)
    value_after = value_now + amount
    trades: dict[str, float] = {}
    if mode == "buy":
        trades = _water_fill(rows, amount)
        if min_order > 0:
            trades = _apply_min_order(rows, trades, amount, min_order)
    else:
        for r in rows:
            trades[r["isin"]] = r["target"] * value_after - r["value"]
            if min_order > 0 and abs(trades[r["isin"]]) < min_order:
                trades[r["isin"]] = 0.0
    out_rows = []
    tax_total = 0.0
    for r in rows:
        t = trades.get(r["isin"], 0.0)
        after = r["value"] + t
        tax = 0.0
        if t < 0 and r["value"] > 0 and r["latent_tax"] > 0:
            tax = r["latent_tax"] * min(1.0, -t / r["value"])
        tax_total += tax
        out_rows.append({
            "isin": r["isin"],
            "name": r["name"],
            "held": r["held"],
            "value": round(r["value"], 2),
            "weight_now": round(r["value"] / value_now, 5) if value_now else 0.0,
            "target": round(r["target"], 5),
            "deviation_now": round((r["value"] / value_now if value_now else 0.0) - r["target"], 5),
            "buy": round(max(0.0, t), 2),
            "sell": round(max(0.0, -t), 2),
            "tax_estimate": round(tax, 2) if tax else 0.0,
            "value_after": round(after, 2),
            "weight_after": round(after / value_after, 5) if value_after else 0.0,
            "deviation_after": round((after / value_after if value_after else 0.0) - r["target"], 5),
        })
    out_rows.sort(key=lambda x: (-x["buy"], x["sell"], -x["target"]))
    return {
        "mode": mode,
        "amount": round(amount, 2),
        "min_order": min_order,
        "rows": out_rows,
        "orders": sum(1 for r in out_rows if r["buy"] > 0 or r["sell"] > 0),
        "total_buy": round(sum(r["buy"] for r in out_rows), 2),
        "total_sell": round(sum(r["sell"] for r in out_rows), 2),
        "tax_estimate": round(tax_total, 2),
        "drift_now": round(sum(abs(r["deviation_now"]) for r in out_rows) / 2, 5),
        "drift_after": round(sum(abs(r["deviation_after"]) for r in out_rows) / 2, 5),
        "untargeted": [{"isin": p["isin"], "name": p["name"], "value": p["value"]} for p in untargeted],
        "note": "Nur Rechenhilfe – Käufe führst du selbst in der Scalable-App aus (Einzelorder oder Sparplan).",
        "tax_rate": tax_rate,
    }


def _water_fill(rows: list[dict[str, Any]], amount: float) -> dict[str, float]:
    """Findet λ mit Σ max(0, λ·t_i − v_i) = Betrag; Kauf_i = max(0, λ·t_i − v_i)."""
    if amount <= 0:
        return {}
    cands = sorted((r for r in rows if r["target"] > 0), key=lambda r: r["value"] / r["target"])
    active: list[dict[str, Any]] = []
    for i, r in enumerate(cands):
        active.append(r)
        sum_t = sum(a["target"] for a in active)
        sum_v = sum(a["value"] for a in active)
        lam = (amount + sum_v) / sum_t
        nxt = cands[i + 1]["value"] / cands[i + 1]["target"] if i + 1 < len(cands) else math.inf
        if lam <= nxt:
            return {a["isin"]: max(0.0, lam * a["target"] - a["value"]) for a in active}
    return {}


def _apply_min_order(rows: list[dict[str, Any]], trades: dict[str, float], amount: float, min_order: float) -> dict[str, float]:
    """Streicht Kleinstkäufe unter ``min_order`` und verteilt deren Betrag auf die übrigen Positionen."""
    excluded: set[str] = set()
    current = dict(trades)
    while True:
        small = [k for k, v in current.items() if 0 < v < min_order]
        if not small:
            return {k: v for k, v in current.items() if v > 0}
        excluded.add(min(small, key=lambda k: current[k]))
        pool = [r for r in rows if r["isin"] not in excluded and r["target"] > 0]
        if not pool:
            # Betrag kleiner als die Mindestorder: alles in die am stärksten untergewichtete Position
            best = min((r for r in rows if r["target"] > 0), key=lambda r: r["value"] / r["target"])
            return {best["isin"]: amount}
        current = _water_fill(pool, amount)


# ---------------------------------------------------------------------------
# Vermögensprognose
# ---------------------------------------------------------------------------


def historic_defaults(doc: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Vorschläge für die Prognose aus der eigenen Historie."""
    since = (now - timedelta(days=365)).isoformat()
    deposits = sum(a["amount"] for a in doc.get("activities", []) if a["kind"] in ("deposit", "withdrawal") and a["date"] >= since)
    ts = (doc.get("timeseries") or {}).get("twr") or []
    vol = annualised_volatility(ts, (doc.get("timeseries") or {}).get("resolution") or "weekly")
    twr = (doc.get("summary") or {}).get("twr") or {}
    return {
        "start": round((doc.get("summary") or {}).get("net_worth") or 0.0, 2),
        "monthly": round(max(0.0, deposits / 12), 2),
        "expected_return": 0.05,
        "historic_return": twr.get("annualized"),
        "volatility": round(vol, 4) if vol is not None else 0.15,
        "historic_volatility": round(vol, 4) if vol is not None else None,
        "inflation": 0.02,
    }


def project(
    *,
    start: float,
    monthly: float,
    years: int,
    expected_return: float,
    volatility: float,
    inflation: float = 0.0,
    goal: float | None = None,
    paths: int = 2000,
    seed: int = 7,
) -> dict[str, Any]:
    """Monte-Carlo-Prognose (monatlich, lognormal) mit Perzentilen – reproduzierbar dank festem Seed.

    ``monthly`` < 0 ist ein Entnahmeplan: Die Entnahme wächst mit der Inflation (gleiche Kaufkraft),
    ein Verlauf endet bei 0 € („Kapital aufgebraucht“).
    """
    if not 1 <= years <= 50:
        raise ValueError("Zeitraum zwischen 1 und 50 Jahren")
    if not -0.5 < expected_return < 0.5 or not 0 <= volatility < 1.5:
        raise ValueError("Rendite oder Schwankung außerhalb des sinnvollen Bereichs")
    months = years * 12
    # Monatsfaktor exp(mu − σ²/2 + σ·Z): Erwartungswert (1 + Rendite)^(1/12), Median etwas darunter
    mu = math.log(1 + expected_return) / 12
    sigma = volatility / math.sqrt(12)
    rng = random.Random(seed)
    values = [start] * paths
    infl_m = (1 + inflation) ** (1 / 12)
    marks = {12 * y for y in range(1, years + 1)}
    series: list[dict[str, Any]] = [{"month": 0, "p10": start, "p25": start, "p50": start, "p75": start, "p90": start, "invested": start}]
    reached_month: list[int | None] = [None] * paths
    depleted_month: list[int | None] = [None] * paths
    taken = [0.0] * paths  # tatsächlich entnommen je Verlauf (endet, wenn das Kapital aufgebraucht ist)
    withdraw = monthly < 0
    flows = 0.0  # Summe der Ein-/Auszahlungen ohne Rendite
    for m in range(1, months + 1):
        flow = monthly * infl_m ** (m - 1) if withdraw else monthly
        flows += flow
        for i in range(paths):
            if depleted_month[i] is not None:
                continue
            grown = values[i] * math.exp(mu - sigma**2 / 2 + sigma * rng.gauss(0, 1))
            v = grown + flow
            if withdraw:
                taken[i] += min(-flow, grown) / infl_m**m  # in heutiger Kaufkraft, wenn Inflation > 0
            if v <= 0:
                v = 0.0
                depleted_month[i] = m
            values[i] = v
            if goal is not None and reached_month[i] is None and v / infl_m**m >= goal:
                reached_month[i] = m
        if m in marks or m == months or (years <= 5 and m % 3 == 0):
            deflate = infl_m**m
            srt = sorted(values)
            series.append({
                "month": m,
                **{f"p{q}": round(srt[min(paths - 1, int(paths * q / 100))] / deflate, 2) for q in (10, 25, 50, 75, 90)},
                "invested": round(max(0.0, start + flows) / deflate, 2),
            })
    final = series[-1]
    out: dict[str, Any] = {
        "params": {"start": start, "monthly": monthly, "years": years, "expected_return": expected_return,
                   "volatility": volatility, "inflation": inflation, "goal": goal, "paths": paths},
        "series": series,
        "final": {k: final[k] for k in ("p10", "p25", "p50", "p75", "p90", "invested")},
        "real_terms": inflation > 0,
        "method": "Monte-Carlo-Simulation mit monatlichen, lognormal verteilten Renditen; Werte in heutiger Kaufkraft, wenn Inflation > 0.",
    }
    if withdraw:
        gone = sorted(m for m in depleted_month if m is not None)
        out["withdrawal"] = {
            "monthly": -monthly,
            "inflation_adjusted": inflation > 0,
            "depleted_probability": round(len(gone) / paths, 4),
            "median_months": gone[len(gone) // 2] if len(gone) >= paths / 2 else None,
            "early_months": gone[int(paths * 0.1)] if len(gone) > paths * 0.1 else None,
            "withdrawn_median": round(sorted(taken)[paths // 2], 2),
            "planned_total": round(-flows, 2),
        }
    if goal is not None:
        hits = sorted(m for m in reached_month if m is not None)
        out["goal"] = {
            "amount": goal,
            "probability": round(len(hits) / paths, 4),
            "median_months": hits[len(hits) // 2] if len(hits) >= paths / 2 else None,
        }
    return out
