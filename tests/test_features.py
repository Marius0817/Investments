"""Neue Auswertungen: Diversifikation/Durchschau, Dividendenprognose, Hinweise, Rebalancing, Prognose, Watchlist."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stopguard.config import ConfigError, parse_config
from stopguard.portfolio.analysis import analyse_portfolio
from stopguard.portfolio.dividends import add_growth, build_dividend_forecast
from stopguard.portfolio.exposure import breakdown, build_exposure
from stopguard.portfolio.metrics import TradeResult
from stopguard.portfolio.model import Dividend, Instrument, Lot
from stopguard.portfolio.planning import TargetsStore, project, rebalance
from stopguard.portfolio.store import PortfolioStore
from stopguard.portfolio.sync import sync_portfolio
from stopguard.portfolio.watchlist import WatchlistError, WatchlistStore, enrich, normalise_isin

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


# ------------------------------------------------------------------ Diversifikation

def test_exposure_config_validation():
    cfg = parse_config({"instruments": {"IE00B4L5Y983": {"exposure": {"regions": {"Nordamerika": 70, "Europa": 20}}}}})
    assert cfg.instruments["IE00B4L5Y983"].exposure["regions"]["Europa"] == 20
    with pytest.raises(ConfigError, match="über 100"):
        parse_config({"instruments": {"IE00B4L5Y983": {"exposure": {"regions": {"A": 80, "B": 40}}}}})
    with pytest.raises(ConfigError, match="Unbekannte"):
        parse_config({"instruments": {"IE00B4L5Y983": {"exposure": {"planets": {"Mars": 1}}}}})


def test_breakdown_look_through_and_isin_fallback():
    etf = Instrument("IE00B4L5Y983", "Welt-ETF", "equity_fund", exposure={"countries": {"US": 70, "DE": 10}})
    weights, derived = breakdown(etf, "region")
    assert weights["Nordamerika"] == pytest.approx(0.7) and weights["Europa"] == pytest.approx(0.1)
    assert weights["Sonstige"] == pytest.approx(0.2) and not derived
    assert breakdown(etf, "sector") == (None, False)  # nichts geraten
    apple = Instrument("US0378331005", "Apple", "stock")
    assert breakdown(apple, "region") == ({"Nordamerika": 1.0}, True)  # aus ISIN abgeleitet, gekennzeichnet
    assert breakdown(apple, "currency") == ({"USD": 1.0}, True)


def test_build_exposure_overlap_and_coverage():
    insts = {
        "US0378331005": Instrument("US0378331005", "Apple", "stock", sector="IT"),
        "IE00B4L5Y983": Instrument("IE00B4L5Y983", "Welt-ETF", "equity_fund", exposure={"holdings": {"US0378331005": 5, "Amazon": 3}}),
    }
    positions = [
        {"isin": "US0378331005", "name": "Apple", "kind": "stock", "status": "open", "value": 1000.0},
        {"isin": "IE00B4L5Y983", "name": "Welt-ETF", "kind": "equity_fund", "status": "open", "value": 9000.0},
    ]
    exp = build_exposure(positions, insts)
    apple = next(i for i in exp["look_through"]["items"] if i["isin"] == "US0378331005")
    assert apple["direct"] == 1000 and apple["via_funds"] == pytest.approx(450) and apple["weight"] == pytest.approx(0.145)
    assert [i["name"] for i in exp["look_through"]["overlap"]] == ["Apple"]
    assert exp["coverage"]["sector"] == pytest.approx(0.1)  # nur Apple hat eine Branche
    assert exp["by_sector"][-1]["key"] == "Nicht zugeordnet"


# ------------------------------------------------------------------ Dividenden

def _result(isin, units, open_days, dividends):
    lot = Lot(f"L-{isin}", isin, isin, NOW - timedelta(days=open_days), units, 100.0)
    lot.dividends = [Dividend(NOW - timedelta(days=d), net) for d, net in dividends]
    return TradeResult(lot=lot, inst=Instrument(isin, isin), data={}, flows_pre=[], flows_post=[], flows_equity=[], current_price=100.0)


def _pos(isin, units, value=1000.0, cost=900.0):
    return {"isin": isin, "name": isin, "status": "open", "units": units, "value": value, "cost_basis": cost}


def test_forecast_shifts_last_year_and_scales_to_units():
    r = _result("DE0008404005", 10, 500, [(200, 50.0)])  # jährlich 5 € je Stück
    f = build_dividend_forecast([r], [_pos("DE0008404005", 12)], NOW)  # heute 12 Stück
    pay = f["payments"][0]
    assert pay["amount"] == pytest.approx(60.0) and pay["date"] == (NOW - timedelta(days=200) + timedelta(days=365)).date().isoformat()
    assert f["by_position"][0]["frequency_label"] == "jährlich" and f["by_position"][0]["confidence"] == "high"
    assert f["next_12m"] == pytest.approx(60.0)
    assert sum(m["amount"] for m in f["monthly"]) == pytest.approx(60.0)


def test_forecast_fills_quarterly_rhythm_for_young_positions():
    r = _result("IE00B8GKDB10", 20, 200, [(180, 10.0), (90, 10.0)])  # 2 Quartalszahlungen in 200 Tagen
    f = build_dividend_forecast([r], [_pos("IE00B8GKDB10", 20)], NOW)
    assert len(f["payments"]) == 4 and f["next_12m"] == pytest.approx(40.0)
    assert f["by_position"][0]["frequency"] == 4 and f["by_position"][0]["confidence"] == "medium"


def test_dividend_growth_skips_partial_first_year():
    rows = add_growth([{"year": 2024, "dividends": 10.0}, {"year": 2025, "dividends": 100.0}, {"year": 2026, "dividends": 120.0}], 2024)
    assert [r["growth"] for r in rows] == [None, None, pytest.approx(0.2)]


# ------------------------------------------------------------------ Rebalancing

POSITIONS = [
    {"isin": "AAAAAAAAAAA1", "name": "A", "status": "open", "value": 6000.0, "latent_tax": 300.0},
    {"isin": "BBBBBBBBBBB2", "name": "B", "status": "open", "value": 3000.0, "latent_tax": 0.0},
    {"isin": "CCCCCCCCCCC3", "name": "C", "status": "open", "value": 1000.0, "latent_tax": 0.0},
]
TARGETS = {"AAAAAAAAAAA1": 50, "BBBBBBBBBBB2": 30, "CCCCCCCCCCC3": 20}


def test_buy_only_rebalance_fills_most_underweight_first():
    res = rebalance(POSITIONS, TARGETS, 1000)
    buys = {r["isin"]: r["buy"] for r in res["rows"]}
    assert sum(buys.values()) == pytest.approx(1000) and res["total_sell"] == 0
    assert buys["CCCCCCCCCCC3"] == pytest.approx(1000)  # C liegt am weitesten unter Ziel
    assert res["drift_after"] < res["drift_now"]
    res = rebalance(POSITIONS, TARGETS, 5000)
    after = {r["isin"]: r["weight_after"] for r in res["rows"]}
    assert after["CCCCCCCCCCC3"] == pytest.approx(after["BBBBBBBBBBB2"] * 20 / 30, rel=1e-3)  # gleiches Verhältnis zum Ziel


def test_min_order_drops_tiny_buys_and_keeps_amount():
    res = rebalance(POSITIONS, TARGETS, 1200, min_order=250)
    buys = [r["buy"] for r in res["rows"] if r["buy"]]
    assert all(b >= 250 for b in buys) and sum(buys) == pytest.approx(1200)


def test_full_rebalance_reaches_targets_and_estimates_tax():
    res = rebalance(POSITIONS, TARGETS, 0, mode="full")
    after = {r["isin"]: r["weight_after"] for r in res["rows"]}
    assert after == {k: pytest.approx(v / 100) for k, v in TARGETS.items()}
    sell_a = next(r for r in res["rows"] if r["isin"] == "AAAAAAAAAAA1")
    assert sell_a["sell"] == pytest.approx(1000) and sell_a["tax_estimate"] == pytest.approx(50)  # 1/6 der latenten Steuer


def test_targets_store_validates_sum(tmp_path):
    store = TargetsStore(tmp_path)
    assert store.load()["targets"] == {}
    with pytest.raises(ValueError, match="100"):
        store.save({"IE00B4L5Y983": 60, "US0378331005": 30})
    store.save({"IE00B4L5Y983": 70, "US0378331005": 30, "DE0007164600": 0})
    assert store.load()["targets"] == {"IE00B4L5Y983": 70.0, "US0378331005": 30.0}


# ------------------------------------------------------------------ Prognose

def test_projection_is_reproducible_and_ordered():
    a = project(start=10000, monthly=500, years=10, expected_return=0.05, volatility=0.15, goal=100000)
    b = project(start=10000, monthly=500, years=10, expected_return=0.05, volatility=0.15, goal=100000)
    assert a["final"] == b["final"]
    f = a["final"]
    assert f["p10"] < f["p25"] < f["p50"] < f["p75"] < f["p90"]
    assert f["invested"] == pytest.approx(70000)
    assert 0 < a["goal"]["probability"] < 1
    flat = project(start=1000, monthly=0, years=1, expected_return=0.0, volatility=0.0)
    assert flat["final"]["p50"] == pytest.approx(1000)
    real = project(start=1000, monthly=0, years=10, expected_return=0.0, volatility=0.0, inflation=0.02)
    assert real["final"]["p50"] == pytest.approx(1000 / 1.02**10, rel=1e-3)


# ------------------------------------------------------------------ Watchlist

def test_isin_validation():
    assert normalise_isin(" us0378331005 ") == "US0378331005"
    for bad in ("US0378331006", "XYZ", "123456789012"):
        with pytest.raises(WatchlistError):
            normalise_isin(bad)


def test_watchlist_store_and_enrich(demo, tmp_path):
    store = WatchlistStore(tmp_path)
    store.upsert("NL0010273215", note="Zielkurs", target_price=600)
    store.upsert("US5949181045")
    store.upsert("NL0010273215", target_price=650)  # Update statt Duplikat
    assert [i["isin"] for i in store.items()] == ["NL0010273215", "US5949181045"]
    demo.client.add_price_alert("NL0010273215", Decimal("600"))
    res = enrich(demo.client, store.items(), workers=1)  # In-Process-Runner ist nicht threadsicher
    asml = res["items"][0]
    assert asml["price"] and asml["history"] and asml["target_price"] == 650
    assert asml["to_target"] == pytest.approx(650 / asml["price"] - 1, abs=1e-4)
    assert asml["alerts"] and asml["alerts"][0]["price"] == 600
    assert store.remove("US5949181045") and not store.remove("US5949181045")


# ------------------------------------------------------------------ Gesamtdokument (Demo)

def test_demo_document_contains_new_sections(demo):
    store = PortfolioStore(demo.cfg.state_dir / "portfolio.db")
    try:
        sync_portfolio(demo.client, store, demo.cfg, sleep=lambda s: None)
        doc = analyse_portfolio(store, demo.cfg)
    finally:
        store.close()
    alloc = doc["allocation"]
    assert alloc["coverage"]["region"] == 1.0 and alloc["by_region"][0]["key"] == "Nordamerika"
    assert {i["name"] for i in alloc["look_through"]["overlap"]} >= {"Apple Inc. (Demo)"}
    fc = doc["income"]["forecast"]
    vhyl = next(p for p in fc["by_position"] if p["isin"] == "IE00B8GKDB10")
    assert vhyl["frequency_label"] == "quartalsweise" and vhyl["payments"] == 4
    assert next(p for p in fc["by_position"] if p["isin"] == "US0378331005")["confidence"] == "low"
    kinds = {a["kind"] for a in doc["activities"]}
    assert {"deposit", "buy", "sell", "dividend", "loan_interest"} <= kinds
    assert all(set(p["performance"]) == {"1w", "1m", "3m", "1y", "ytd"} for p in doc["positions"] if p["status"] == "open")
    ids = {i["id"] for i in doc["insights"]}
    assert "overlap:US0378331005" in ids
    assert all(i["level"] in ("warn", "info", "good") and i["message"] for i in doc["insights"])
