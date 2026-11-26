"""Diversifikation: Aufteilung nach Anlageklasse, Region, Land, Branche und Währung.

Fonds und ETFs werden über die Gewichte aus der Konfiguration „durchschaut“
(``[instruments.<ISIN>.exposure]``). Einzelaktien ordnet stopguard ohne Angaben
anhand des ISIN-Länderpräfixes zu (Sitz des Emittenten – eine Näherung, die als
solche gekennzeichnet wird). Was sich nicht zuordnen lässt, erscheint offen als
„Nicht zugeordnet“ – es wird nichts geraten.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from stopguard.portfolio.model import Instrument

UNASSIGNED = "Nicht zugeordnet"
OTHER = "Sonstige"

ASSET_CLASS_BY_KIND = {
    "stock": "Aktien",
    "equity_fund": "Aktien",
    "mixed_fund": "Mischfonds",
    "bond_fund": "Anleihen",
    "real_estate_fund": "Immobilien",
    "foreign_real_estate_fund": "Immobilien",
    "other": OTHER,
}

COUNTRY_NAMES = {
    "US": "USA", "CA": "Kanada", "DE": "Deutschland", "FR": "Frankreich", "NL": "Niederlande", "GB": "Großbritannien",
    "CH": "Schweiz", "IT": "Italien", "ES": "Spanien", "IE": "Irland", "LU": "Luxemburg", "BE": "Belgien",
    "AT": "Österreich", "FI": "Finnland", "SE": "Schweden", "DK": "Dänemark", "NO": "Norwegen", "PT": "Portugal",
    "JP": "Japan", "AU": "Australien", "HK": "Hongkong", "SG": "Singapur", "NZ": "Neuseeland",
    "CN": "China", "TW": "Taiwan", "KR": "Südkorea", "IN": "Indien", "BR": "Brasilien", "MX": "Mexiko",
    "ZA": "Südafrika", "SA": "Saudi-Arabien", "ID": "Indonesien", "TH": "Thailand", "PL": "Polen",
    "IL": "Israel", "KY": "Kaimaninseln", "BM": "Bermuda", "JE": "Jersey", "GG": "Guernsey",
}

REGIONS = ("Nordamerika", "Europa", "Asien-Pazifik", "Schwellenländer")
_REGION_BY_COUNTRY = {
    "USA": "Nordamerika", "Kanada": "Nordamerika", "Bermuda": "Nordamerika",
    "Deutschland": "Europa", "Frankreich": "Europa", "Niederlande": "Europa", "Großbritannien": "Europa",
    "Schweiz": "Europa", "Italien": "Europa", "Spanien": "Europa", "Irland": "Europa", "Luxemburg": "Europa",
    "Belgien": "Europa", "Österreich": "Europa", "Finnland": "Europa", "Schweden": "Europa", "Dänemark": "Europa",
    "Norwegen": "Europa", "Portugal": "Europa", "Jersey": "Europa", "Guernsey": "Europa", "Israel": "Europa",
    "Japan": "Asien-Pazifik", "Australien": "Asien-Pazifik", "Hongkong": "Asien-Pazifik", "Singapur": "Asien-Pazifik",
    "Neuseeland": "Asien-Pazifik",
    "China": "Schwellenländer", "Taiwan": "Schwellenländer", "Südkorea": "Schwellenländer", "Indien": "Schwellenländer",
    "Brasilien": "Schwellenländer", "Mexiko": "Schwellenländer", "Südafrika": "Schwellenländer",
    "Saudi-Arabien": "Schwellenländer", "Indonesien": "Schwellenländer", "Thailand": "Schwellenländer",
    "Polen": "Schwellenländer", "Kaimaninseln": "Schwellenländer",
}
_CURRENCY_BY_COUNTRY = {
    "USA": "USD", "Kanada": "CAD", "Großbritannien": "GBP", "Schweiz": "CHF", "Japan": "JPY", "Australien": "AUD",
    "Hongkong": "HKD", "Schweden": "SEK", "Dänemark": "DKK", "Norwegen": "NOK", "China": "CNY", "Taiwan": "TWD",
    "Südkorea": "KRW", "Indien": "INR", "Brasilien": "BRL",
    **{c: "EUR" for c in ("Deutschland", "Frankreich", "Niederlande", "Italien", "Spanien", "Irland", "Luxemburg",
                          "Belgien", "Österreich", "Finnland", "Portugal")},
}

DIMENSIONS = {
    "asset_class": "Anlageklasse",
    "region": "Region",
    "country": "Land",
    "sector": "Branche",
    "currency": "Währung",
}


def country_name(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    return COUNTRY_NAMES.get(v.upper(), v) if len(v) == 2 else v


def region_of(country: str | None) -> str | None:
    return _REGION_BY_COUNTRY.get(country or "")


def _normalise(weights: dict[str, float], mapper=None) -> dict[str, float]:
    """Prozent → Anteile; fehlender Rest wird „Sonstige“, Summen über 100 % werden skaliert."""
    acc: dict[str, float] = defaultdict(float)
    for k, v in weights.items():
        acc[mapper(k) if mapper else k] += v / 100
    total = sum(acc.values())
    if total > 1.0001:
        return {k: v / total for k, v in acc.items()}
    if total < 0.9999:
        acc[OTHER] += 1 - total
    return dict(acc)


def breakdown(inst: Instrument, dim: str) -> tuple[dict[str, float] | None, bool]:
    """Anteile einer Position je Ausprägung (Summe 1) und ob die Zuordnung nur abgeleitet ist."""
    exp = inst.exposure or {}
    if dim == "asset_class":
        return {inst.asset_class or ASSET_CLASS_BY_KIND.get(inst.kind, OTHER): 1.0}, False
    if dim == "region":
        if exp.get("regions"):
            return _normalise(exp["regions"]), False
        if exp.get("countries"):
            return _normalise(exp["countries"], lambda c: region_of(country_name(c)) or OTHER), False
        if inst.region:
            return {inst.region: 1.0}, False
    if dim == "country":
        if exp.get("countries"):
            return _normalise(exp["countries"], country_name), False
        if inst.country:
            return {country_name(inst.country) or inst.country: 1.0}, False
    if dim == "sector":
        if exp.get("sectors"):
            return _normalise(exp["sectors"]), False
        return ({inst.sector: 1.0}, False) if inst.sector else (None, False)
    if dim == "currency":
        if exp.get("currencies"):
            return _normalise(exp["currencies"], str.upper), False
        if inst.currency:
            return {inst.currency.upper(): 1.0}, False
    # Ableitung für Einzelaktien über Land (konfiguriert oder ISIN-Präfix)
    derived = False
    country = country_name(inst.country)
    if not country and inst.kind == "stock" and not inst.is_fund:
        country = COUNTRY_NAMES.get(inst.isin[:2])
        derived = country is not None
    if not country:
        return None, False
    if dim == "region":
        reg = inst.region or region_of(country)
        return ({reg: 1.0}, derived) if reg else (None, False)
    if dim == "country":
        return {country: 1.0}, derived
    if dim == "currency":
        cur = _CURRENCY_BY_COUNTRY.get(country)
        return ({cur: 1.0}, derived) if cur else (None, False)
    return None, False


def build_exposure(positions: list[dict[str, Any]], instruments: dict[str, Instrument]) -> dict[str, Any]:
    open_pos = [p for p in positions if p["status"] == "open" and (p["value"] or 0) > 0]
    total = sum(p["value"] for p in open_pos) or 1.0
    out: dict[str, Any] = {}
    coverage: dict[str, float] = {}
    derived_value: dict[str, float] = defaultdict(float)
    unassigned: dict[str, list[dict[str, Any]]] = {}

    for dim in DIMENSIONS:
        acc: dict[str, float] = defaultdict(float)
        contributors: dict[str, list[tuple[str, float]]] = defaultdict(list)
        missing: list[dict[str, Any]] = []
        covered = 0.0
        for p in open_pos:
            inst = instruments.get(p["isin"]) or Instrument(p["isin"], p["name"], p["kind"])
            weights, derived = breakdown(inst, dim)
            if not weights:
                acc[UNASSIGNED] += p["value"]
                missing.append({"isin": p["isin"], "name": p["name"], "value": p["value"]})
                continue
            covered += p["value"]
            if derived:
                derived_value[dim] += p["value"]
            for k, w in weights.items():
                acc[k] += p["value"] * w
                contributors[k].append((p["name"], p["value"] * w))
        out[f"by_{dim}"] = sorted(
            (
                {
                    "key": k,
                    "value": round(v, 2),
                    "weight": round(v / total, 5),
                    "positions": [
                        {"name": n, "value": round(x, 2)} for n, x in sorted(contributors.get(k, []), key=lambda t: -t[1])[:8]
                    ],
                }
                for k, v in acc.items()
                if v > 0.005
            ),
            key=lambda x: (x["key"] == UNASSIGNED, -x["value"]),
        )
        coverage[dim] = round(covered / total, 4)
        if missing:
            unassigned[dim] = sorted(missing, key=lambda x: -x["value"])

    out["coverage"] = coverage
    out["derived_share"] = {k: round(v / total, 4) for k, v in derived_value.items() if v}
    out["unassigned"] = unassigned
    out["look_through"] = _look_through(open_pos, instruments, total)
    return out


def _look_through(open_pos: list[dict[str, Any]], instruments: dict[str, Instrument], total: float) -> dict[str, Any]:
    """Einzelwerte direkt und über Fonds (nur die in der Konfiguration genannten größten Positionen der Fonds)."""
    names = {isin: inst.name for isin, inst in instruments.items()}
    rows: dict[str, dict[str, Any]] = {}
    funds_with_holdings = 0
    fund_value = 0.0

    def row(key: str) -> dict[str, Any]:
        isin = key.upper() if len(key) == 12 and key[:2].isalpha() and key[2:].isalnum() else None
        k = isin or key
        if k not in rows:
            rows[k] = {"key": k, "isin": isin, "name": names.get(isin, key) if isin else key, "direct": 0.0, "via_funds": 0.0, "sources": []}
        return rows[k]

    for p in open_pos:
        inst = instruments.get(p["isin"])
        if inst is None:
            continue
        holdings = (inst.exposure or {}).get("holdings")
        if inst.kind == "stock" and not inst.is_fund:
            row(p["isin"])["direct"] += p["value"]
        elif holdings:
            funds_with_holdings += 1
            fund_value += p["value"]
            for key, pct in holdings.items():
                r = row(key)
                v = p["value"] * pct / 100
                r["via_funds"] += v
                r["sources"].append({"isin": p["isin"], "name": p["name"], "value": round(v, 2)})
    items = []
    for r in rows.values():
        value = r["direct"] + r["via_funds"]
        if value <= 0.005:
            continue
        items.append({
            **r,
            "direct": round(r["direct"], 2),
            "via_funds": round(r["via_funds"], 2),
            "value": round(value, 2),
            "weight": round(value / total, 5),
            "sources": sorted(r["sources"], key=lambda s: -s["value"]),
        })
    items.sort(key=lambda x: -x["value"])
    return {
        "items": items[:25],
        "funds_with_holdings": funds_with_holdings,
        "fund_value_covered": round(fund_value, 2),
        "overlap": [i for i in items if i["direct"] > 0 and i["via_funds"] > 0][:10],
    }
