"""Risikokennzahlen und Monatsrenditen aus dem zeitgewichteten Renditeindex (TWR).

Grundlage ist ``timeseries.twr`` (Depot und Benchmark, Start = 100). Weil die TWR Ein- und
Auszahlungen herausrechnet, sind die Kennzahlen mit denen von Fonds und Indizes vergleichbar.
Kurze Historien liefern bewusst ``null`` statt scheinbar präziser Zahlen.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

PER_YEAR = {"daily": 365, "weekly": 52, "monthly": 12}
MIN_RETURNS = 12  # weniger Renditepunkte → keine Kennzahlen
MIN_DAYS_ANNUALISED = 180  # Rendite p. a. erst ab einem halben Jahr Historie


def _std(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def period_returns(points: list[dict[str, Any]], key: str) -> list[float]:
    out = []
    for a, b in pairwise(points):
        if a.get(key) and b.get(key):
            out.append(b[key] / a[key] - 1)
    return out


def annualised_volatility(points: list[dict[str, Any]], resolution: str, key: str = "portfolio") -> float | None:
    """Annualisierte Schwankung (Standardabweichung der Log-Renditen)."""
    ppy = PER_YEAR.get(resolution, 52)
    logs = [math.log(1 + r) for r in period_returns(points, key)]
    if len(logs) < max(MIN_RETURNS, ppy // 2):
        return None
    sd = _std(logs)
    return sd * math.sqrt(ppy) if sd is not None else None


def monthly_returns(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rendite je Kalendermonat (Depot und Benchmark); erster und laufender Monat sind angebrochen."""
    if len(points) < 2:
        return []
    by_month: dict[str, dict[str, Any]] = {}
    for p in points:
        by_month[p["date"][:7]] = p  # letzter Punkt des Monats
    months = sorted(by_month)
    out = []
    prev = points[0]
    for i, m in enumerate(months):
        end = by_month[m]
        if end is points[0]:
            continue  # Monat besteht nur aus dem Startpunkt – keine Rendite
        row: dict[str, Any] = {"month": m, "partial": i == 0 or i == len(months) - 1}
        for key, name in (("portfolio", "twr"), ("benchmark", "benchmark")):
            row[name] = round(end[key] / prev[key] - 1, 5) if prev.get(key) and end.get(key) else None
        out.append(row)
        prev = end
    if out and out[-1]["month"] == points[-1]["date"][:7]:
        # Der laufende Monat ist nur angebrochen, wenn der letzte Punkt nicht am Monatsende liegt
        last = date.fromisoformat(points[-1]["date"])
        out[-1]["partial"] = (last + timedelta(days=1)).month == last.month
    return out


def _window(points: list[dict[str, Any]], start: str | None) -> list[dict[str, Any]]:
    if start is None:
        return points
    i = next((k for k, p in enumerate(points) if p["date"] >= start), len(points))
    return points[max(0, i - 1):]


def _drawdown(points: list[dict[str, Any]], key: str) -> float | None:
    peak = None
    worst = 0.0
    for p in points:
        v = p.get(key)
        if not v:
            continue
        peak = v if peak is None else max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst if peak is not None else None


def _metrics(points: list[dict[str, Any]], resolution: str, rf: float) -> dict[str, Any] | None:
    ppy = PER_YEAR.get(resolution, 52)
    rp = period_returns(points, "portfolio")
    if len(rp) < MIN_RETURNS:
        return None
    first, last = points[0], points[-1]
    days = (date.fromisoformat(last["date"]) - date.fromisoformat(first["date"])).days
    cumulative = last["portfolio"] / first["portfolio"] - 1
    ann = (1 + cumulative) ** (365 / days) - 1 if days >= MIN_DAYS_ANNUALISED else None
    logs = [math.log(1 + r) for r in rp]
    sd = _std(logs)
    vol = sd * math.sqrt(ppy) if sd is not None else None
    rf_p = (1 + rf) ** (1 / ppy) - 1
    downside = [min(0.0, r - rf_p) for r in rp]
    dd = math.sqrt(sum(x * x for x in downside) / len(downside)) * math.sqrt(ppy) if downside else None
    out: dict[str, Any] = {
        "from": first["date"],
        "to": last["date"],
        "days": days,
        "return_cumulative": round(cumulative, 5),
        "return_annualised": round(ann, 5) if ann is not None else None,
        "volatility": round(vol, 5) if vol is not None else None,
        "sharpe": round((ann - rf) / vol, 3) if ann is not None and vol else None,
        "sortino": round((ann - rf) / dd, 3) if ann is not None and dd else None,
        "max_drawdown": _round(_drawdown(points, "portfolio")),
        "positive_periods": round(sum(1 for r in rp if r > 0) / len(rp), 4),
    }
    # Benchmark-Vergleich: nur über Perioden, in denen beide Werte vorliegen
    pairs = [
        (b["portfolio"] / a["portfolio"] - 1, b["benchmark"] / a["benchmark"] - 1)
        for a, b in pairwise(points)
        if a.get("portfolio") and b.get("portfolio") and a.get("benchmark") and b.get("benchmark")
    ]
    if len(pairs) >= MIN_RETURNS:
        xs = [p for p, _ in pairs]
        ys = [q for _, q in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs) / (len(pairs) - 1)
        vx = sum((x - mx) ** 2 for x in xs) / (len(xs) - 1)
        vy = sum((y - my) ** 2 for y in ys) / (len(ys) - 1)
        te = _std([x - y for x, y in pairs])
        bench_cum = last["benchmark"] / first["benchmark"] - 1 if first.get("benchmark") and last.get("benchmark") else None
        out.update({
            "beta": round(cov / vy, 3) if vy else None,
            "correlation": round(cov / math.sqrt(vx * vy), 3) if vx and vy else None,
            "tracking_error": round(te * math.sqrt(ppy), 5) if te is not None else None,
            "benchmark_return_cumulative": round(bench_cum, 5) if bench_cum is not None else None,
            "benchmark_volatility": _round(annualised_volatility(points, resolution, "benchmark")),
            "benchmark_max_drawdown": _round(_drawdown(points, "benchmark")),
        })
    return out


def _round(v: float | None, n: int = 5) -> float | None:
    return None if v is None else round(v, n)


def risk_metrics(points: list[dict[str, Any]], resolution: str, risk_free: float, months: list[dict[str, Any]]) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    if points:
        windows["since_start"] = _metrics(points, resolution, risk_free)
        last = date.fromisoformat(points[-1]["date"])
        first = date.fromisoformat(points[0]["date"])
        if (last - first).days >= 360:
            windows["1y"] = _metrics(_window(points, (last - timedelta(days=365)).isoformat()), resolution, risk_free)
    full = [m for m in months if not m["partial"] and m.get("twr") is not None]
    best = max(full, key=lambda m: m["twr"], default=None)
    worst = min(full, key=lambda m: m["twr"], default=None)
    return {
        "risk_free": risk_free,
        "resolution": resolution,
        "windows": windows,
        "best_month": {"month": best["month"], "twr": best["twr"]} if best else None,
        "worst_month": {"month": worst["month"], "twr": worst["twr"]} if worst else None,
        "note": "Aus der zeitgewichteten Rendite (TWR). Sharpe/Sortino mit risikofreiem Zins "
                f"{risk_free * 100:.1f} % p. a.; erst ab {MIN_RETURNS} Renditepunkten bzw. einem halben Jahr Historie.",
    }
