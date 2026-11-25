import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from stopguard.config import parse_config
from stopguard.portfolio.analysis import analyse_portfolio
from stopguard.portfolio.api import PortfolioCache, serve
from stopguard.portfolio.credit import CreditReport, allocate_auto
from stopguard.portfolio.importers import parse_date, parse_number, read_trades_csv
from stopguard.portfolio.ledger import build_ledger
from stopguard.portfolio.lots import build_lots, dedupe_manual, manual_to_lots
from stopguard.portfolio.metrics import trade_metrics
from stopguard.portfolio.model import Instrument, LedgerEvent, Lot, LotExit
from stopguard.portfolio.prices import PriceBook
from stopguard.portfolio.store import PortfolioStore
from stopguard.portfolio.sync import sync_portfolio
from stopguard.portfolio.tax import TaxModel, effective_rate

T0 = datetime(2025, 1, 2, 10, tzinfo=timezone.utc)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


# ---------------------------------------------------------------- Steuern

def test_effective_tax_rates():
    assert effective_rate(0) == pytest.approx(0.26375)
    assert effective_rate(9) == pytest.approx(0.279951, abs=1e-6)
    assert effective_rate(8) == pytest.approx(0.278186, abs=1e-6)


def test_vorabpauschale_and_credit_at_sale():
    cfg = parse_config({})
    prices = PriceBook({"F": [("2024-12-31", 110.0), ("2025-01-01", 110.0), ("2025-12-31", 120.0)]})
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    tax = TaxModel(cfg.tax, prices, now)
    inst = Instrument("F", "Fonds Acc", kind="equity_fund", distributing=False)
    lot = Lot("L1", "F", "Fonds", datetime(2024, 1, 15, tzinfo=timezone.utc), 10, 100.0)
    tax.apply_vorabpauschale(lot, inst)
    # 2024: Kaufkurs als Startwert (keine Historie), Kauf im Januar → Faktor 12/12
    # 2025: 110 × 2,53 % × 0,7 = 1,9481 je Stück (Deckel Wertzuwachs 10 € greift nicht)
    per_unit = [round(v, 4) for _, v in lot.vap_per_unit]
    assert per_unit == [round(100 * 0.0229 * 0.7, 4), round(110 * 0.0253 * 0.7, 4)]
    assert lot.vap_tax[1][1] == pytest.approx(1.9481 * 10 * 0.7 * 0.26375, rel=1e-3)
    # Beim Verkauf wird die bereits versteuerte Vorabpauschale abgezogen
    gain = tax.sale_gain(lot, inst, 10, 130.0, 0.0, now)
    assert gain == pytest.approx(300 - 10 * (1.603 + 1.9481), rel=1e-3)


def test_trade_equity_irr_with_credit_tax_and_interest():
    """Kauf 1000 € (500 € Kredit), nach einem Jahr Verkauf zu 1100 €, 20 € Zinsen."""
    cfg = parse_config({"portfolio": {"benchmark_isin": ""}})
    now = day(400)
    lot = Lot("L", "S", "Aktie", T0, 10, 100.0)
    lot.exits.append(LotExit(day(365), 10, 110.0))
    lot.credit_draws.append((T0, 500.0))
    lot.credit_repayments.append((day(365), 500.0))
    lot.interest.append((day(365), 20.0))
    res = trade_metrics(lot, Instrument("S", "Aktie"), PriceBook({}), TaxModel(cfg.tax, PriceBook({}), now), cfg, now)
    d = res.data
    assert d["irr"] == pytest.approx(0.10, abs=1e-6)
    tax = 100 * 0.26375
    assert d["taxes"]["sale"] == pytest.approx(tax, abs=0.01)  # Ausgabe auf Cent gerundet
    assert d["irr_after_tax"] == pytest.approx((1100 - tax) / 1000 - 1, abs=1e-6)
    eq_end = 1100 - tax - 500 - 20
    assert d["credit"]["equity_irr"] == pytest.approx(eq_end / 500 - 1, abs=1e-6)
    assert d["credit"]["effective_rate"] == pytest.approx(0.04, abs=1e-4)
    assert d["credit"]["profit_net"] == pytest.approx(eq_end - 500, abs=0.01)
    # Zinsen nicht absetzbar → Break-even = 4 % / (1 − 26,375 %)
    assert d["credit"]["break_even_irr"] == pytest.approx(0.04 / (1 - 0.26375), abs=1e-4)


# ------------------------------------------------------------ Kassenbuch

def _tx(id_, when, **kw):
    base = {"id": id_, "status": "SETTLED", "last_event_datetime": when.isoformat(), "is_cancellation": False}
    return {**base, **kw}


def test_ledger_classification():
    cfg = parse_config({})
    txs = [
        _tx("1", day(0), summary_type="BrokerCashTransactionSummary", cash_transaction_type="DEPOSIT", amount="1000"),
        _tx("2", day(1), summary_type="BrokerCashTransactionSummary", cash_transaction_type="INTEREST", amount="-3.10", description="x"),
        _tx("3", day(2), summary_type="BrokerCashTransactionSummary", cash_transaction_type="INTEREST", amount="2.00", description="Sollzinsen Kredit"),
        _tx("4", day(3), summary_type="BrokerCashTransactionSummary", cash_transaction_type="INTEREST", amount="1.50", description="Zinsen Guthaben"),
        _tx("5", day(4), summary_type="BrokerCashTransactionSummary", cash_transaction_type="WITHDRAWAL", amount="100"),
        _tx("6", day(5), summary_type="BrokerCashTransactionSummary", cash_transaction_type="MYSTERY", amount="5"),
        _tx("7", day(6), summary_type="BrokerSecurityTransactionSummary", side="BUY", isin="X", quantity="2", amount="201",
            _details={"security_trade": {"number_of_shares": {"filled": "2"}, "average_price": "100",
                                         "trade_transaction_amounts": {"transaction_fee": "1", "venue_fee": "0"}}}),
        _tx("8", day(7), status="CANCELLED", summary_type="BrokerSecurityTransactionSummary", side="BUY", isin="X", quantity="1", amount="1"),
    ]
    res = build_ledger(txs, cfg.credit)
    kinds = [(e.id, e.kind, e.cash) for e in res.events]
    assert ("1", "deposit", 1000.0) in kinds
    assert ("2", "loan_interest", -3.10) in kinds
    assert ("3", "loan_interest", -2.0) in kinds
    assert ("4", "interest_income", 1.5) in kinds
    assert ("5", "withdrawal", -100.0) in kinds
    assert ("7", "buy", -201.0) in kinds
    assert res.unknown_types == {"MYSTERY": 1}
    buy = next(e for e in res.events if e.id == "7")
    assert buy.fees == 1.0 and buy.price == 100.0
    assert all(e.id != "8" for e in res.events)


def ev(id_, n, kind, cash, isin=None, units=0.0, price=None):
    return LedgerEvent(id_, day(n), kind, cash, isin, units, price)


def test_credit_attribution_with_earmark_and_sale_first_repayment():
    events = [
        ev("d1", 0, "deposit", 1000),
        ev("bA", 1, "buy", -3000, "A", 30, 100),  # 2000 € Kredit → Trade A
        ev("i1", 31, "loan_interest", -10),  # vollständig A
        ev("d2", 40, "deposit", 500),  # zweckgebunden …
        ev("bB", 42, "buy", -450, "B", 4.5, 100),  # … für B → kein neuer Kredit
        ev("sA", 60, "sell", 1500, "A", 15, 100),  # Verkauf A tilgt zuerst A
    ]
    lots = build_lots(events)
    rep = CreditReport(mode="auto")
    allocate_auto(events, lots, rep, earmark_days=5)
    a = next(lot for lot in lots.lots if lot.isin == "A")
    b = next(lot for lot in lots.lots if lot.isin == "B")
    assert a.credit_draws[0][1] == pytest.approx(2000)
    assert not b.credit_draws  # durch Einzahlung finanziert
    assert a.interest == [(day(31), pytest.approx(10.0))]
    # Kapitalisierte Zinsen (10) + Rest der Einzahlung (50) nach Ablauf der Zweckbindung
    assert rep.reconstructed_loan == pytest.approx(2000 + 10 - 50 - 1500)
    assert a.credit_outstanding == pytest.approx(rep.reconstructed_loan)
    assert rep.interest_unallocated == pytest.approx(0)


def test_fifo_and_dividend_allocation():
    events = [
        ev("b1", 0, "buy", -1000, "X", 10, 100),
        ev("b2", 10, "buy", -1100, "X", 10, 110),
        ev("dv", 20, "dividend", 40, "X"),
        ev("s1", 30, "sell", 1800, "X", 15, 120),
    ]
    res = build_lots(events)
    l1, l2 = res.lots
    assert l1.is_closed and l1.exits[0].units == 10
    assert l2.remaining_units == pytest.approx(5)
    assert [d.net for d in l1.dividends] == [20.0] and [d.net for d in l2.dividends] == [20.0]
    assert res.exit_event_lot["s1"] == [(l1.id, 10), (l2.id, 5)]


# ----------------------------------------------------------- Notion-Import

NOTION_CSV = """﻿Name Investment,Ticker,Open,Open (€),Close,Close (€),Units,Dividends (€),Current (€),IRR (%),Kreditfinanziert?
Demo Welt ETF (Acc),EUNL.DE,21/07/2026,€10.004,,,"1,000",€0.00,€8.81,-30.55%,No
Demo Aktie,ABC.DE,"July 7, 2025",€160.80,"March 3, 2026",€171.20,100,€44.15,€158.72,3.31%,Yes
,,,,,,,,,,
"""


def test_notion_csv_parsing(tmp_path):
    f = tmp_path / "trades.csv"
    f.write_text(NOTION_CSV, encoding="utf-8")
    rows = read_trades_csv(f)
    assert len(rows) == 2
    a, b = rows
    assert a["open_price"] == pytest.approx(10.004) and a["units"] == 1000 and a["close_date"] is None
    assert b["credit_flag"] is True and b["close_price"] == 171.2 and b["dividends"] == pytest.approx(44.15)
    assert b["open_date"].date().isoformat() == "2025-07-07"
    assert parse_number("1.234,56 €", "comma") == pytest.approx(1234.56)
    assert parse_date("3. März 2024").date().isoformat() == "2024-03-03"
    assert parse_date("2024-01-05 → 2024-02-01").date().isoformat() == "2024-01-05"


def test_manual_lots_dedupe_and_credit_flag_transfer(tmp_path):
    cfg = parse_config({"instruments": {"IE00B4L5Y983": {"ticker": "EUNL.DE"}}})
    f = tmp_path / "trades.csv"
    f.write_text(NOTION_CSV, encoding="utf-8")
    rows = read_trades_csv(f)
    rows[0]["credit_flag"] = True
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    manual, warnings = manual_to_lots(rows, cfg, now)
    assert manual[0].isin == "IE00B4L5Y983"
    assert any("ABC.DE" in w for w in warnings)
    scalable = Lot("L-s", "IE00B4L5Y983", "x", datetime(2026, 7, 22, tzinfo=timezone.utc), 1000, 10.0)
    kept, notes = dedupe_manual(manual, [scalable], days=3)
    assert len(kept) == 1 and notes
    assert scalable.flagged_credit  # Markierung aus Notion übernommen
    assert manual[1].dividends[0].approximate


# ------------------------------------------------------ End-to-End (Demo)

@pytest.fixture()
def synced(demo):
    store = PortfolioStore(demo.cfg.state_dir / "portfolio.db")
    rep = sync_portfolio(demo.client, store, demo.cfg, sleep=lambda s: None)
    assert rep.transactions > 40 and rep.details_pending == 0
    yield demo, store
    store.close()


def test_demo_portfolio_analysis(synced):
    demo, store = synced
    doc = analyse_portfolio(store, demo.cfg)
    json.dumps(doc)  # vollständig serialisierbar
    assert doc["schema"] == "stopguard.portfolio/1"
    s = doc["summary"]
    assert s["irr"]["trades"] is not None and s["irr"]["account"] is not None
    assert s["counts"]["trades_credit"] == 1 and s["counts"]["trades_closed"] == 2
    credit_trade = doc["credit"]["trades"][0]
    assert credit_trade["isin"] == "LU0274208692"
    assert credit_trade["credit"]["interest"] > 0
    assert doc["credit"]["interest_unallocated"] == pytest.approx(0, abs=0.01)
    rec = doc["data_quality"]["reconciliation"]
    assert rec["loan_reconstructed"] == pytest.approx(rec["loan_reported"], abs=0.05)
    assert not [w for w in doc["data_quality"]["warnings"] if "Bestand" in w]
    tesla = next(t for t in doc["trades"] if t["isin"] == "US88160R1014")
    assert tesla["status"] == "closed" and tesla["profit"] > 0 and tesla["benchmark"]["irr"] is not None
    years = {y["year"]: y for y in doc["taxes"]["years"]}
    assert any(y["loss_carryforward_stock"] < 0 or y["gains_stock"] is not None for y in years.values())
    ts = doc["timeseries"]
    assert ts["portfolio"] and ts["twr"] and ts["irr"] and ts["source"] == "scalable_chart"
    assert -1 < doc["periods"]["twr_summary"]["since_start"] < 1
    origins = {t["origin"] for t in doc["trades"]}
    assert {"single", "savings_plan"} <= origins


def test_api_endpoints(synced):
    demo, store = synced
    doc = analyse_portfolio(store, demo.cfg)
    server = serve(PortfolioCache(lambda: doc), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/v1"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            return json.loads(resp.read())

    try:
        assert get("/health")["schema"] == "stopguard.portfolio/1"
        assert get("/summary")["counts"]["trades"] == len(doc["trades"])
        singles = get("/trades?origin=single&sort=-irr")
        assert singles and all(t["origin"] == "single" for t in singles)
        tid = singles[0]["id"]
        assert get(f"/trades/{tid}")["cashflows"]
        pos = get("/positions/LU0274208692")
        assert pos["trades"] and pos["timeseries"]
        with pytest.raises(urllib.error.HTTPError):
            get("/nope")
    finally:
        server.shutdown()
        server.server_close()


def test_portfolio_cli_commands(demo, monkeypatch, capsys):
    from test_cli_config import _patch_runner
    from stopguard import cli

    _patch_runner(monkeypatch)
    cfg = str(demo.config_path)
    assert cli.main(["-c", cfg, "portfolio", "sync"]) == 0
    for view in ("summary", "positions", "credit", "taxes", "income", "instruments"):
        assert cli.main(["-c", cfg, "portfolio", view]) == 0
    assert cli.main(["-c", cfg, "portfolio", "trades", "--no-plans", "--sort", "profit"]) == 0
    out = demo.cfg.state_dir / "export.json"
    assert cli.main(["-c", cfg, "portfolio", "export", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["schema"] == "stopguard.portfolio/1"
    text = capsys.readouterr().out
    assert "IRR EK" in text and "Freibetrag" in text
