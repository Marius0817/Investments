from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stopguard.finance import CashFlow, build_cash_flows, floor_price_for_irr, position_returns, xirr
from stopguard.models import Holding, Transaction

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def d(y, m, day):
    return datetime(y, m, day, tzinfo=timezone.utc)


def test_xirr_simple_year():
    assert xirr([(T0, -100), (T0 + timedelta(days=365), 110)]) == pytest.approx(0.10, abs=1e-9)


def test_xirr_excel_reference():
    # Beispiel aus der Excel-Dokumentation zu XINTZINSFUSS/XIRR → 37,34 %
    flows = [
        (d(2008, 1, 1), -10000),
        (d(2008, 3, 1), 2750),
        (d(2008, 10, 30), 4250),
        (d(2009, 2, 15), 3250),
        (d(2009, 4, 1), 2750),
    ]
    assert xirr(flows) == pytest.approx(0.373362535, abs=1e-6)


def test_xirr_loss_and_no_solution():
    assert xirr([(T0, -100), (T0 + timedelta(days=365), 80)]) == pytest.approx(-0.20, abs=1e-9)
    assert xirr([(T0, -100), (T0 + timedelta(days=10), -50)]) is None
    assert xirr([(T0, -100)]) is None


def test_floor_price_single_buy():
    flows = [CashFlow(T0, -100.0, 1.0)]
    at = T0 + timedelta(days=365)
    assert floor_price_for_irr(flows, 1.0, 0.05, at) == pytest.approx(105.0)
    assert floor_price_for_irr(flows, 1.0, 0.0, at) == pytest.approx(100.0)
    assert floor_price_for_irr(flows, 1.0, 0.0, at, exit_costs=1.0) == pytest.approx(101.0)


def test_floor_price_is_consistent_with_xirr():
    flows = [
        CashFlow(T0, -1000.0, 10),
        CashFlow(T0 + timedelta(days=90), -500.0, 4),
        CashFlow(T0 + timedelta(days=200), 30.0, 0, "distribution"),
        CashFlow(T0 + timedelta(days=250), 400.0, -3, "sell"),
    ]
    at = T0 + timedelta(days=500)
    price = floor_price_for_irr(flows, 11.0, 0.07, at)
    irr = xirr([(f.when, f.amount) for f in flows] + [(at, 11.0 * price)])
    assert irr == pytest.approx(0.07, abs=1e-7)


def _tx(id_, when, side, qty, amount, isin="X"):
    return Transaction.from_json(
        {
            "id": id_,
            "summary_type": "BrokerSecurityTransactionSummary",
            "status": "SETTLED",
            "last_event_datetime": when.isoformat(),
            "isin": isin,
            "side": side,
            "quantity": str(qty),
            "amount": str(amount),
        }
    )


def test_cash_flows_reset_after_full_sale_and_dividends():
    txs = [
        _tx("1", d(2024, 1, 1), "BUY", 10, 1000),
        _tx("2", d(2024, 6, 1), "SELL", 10, 1200),  # komplett verkauft
        _tx("3", d(2025, 1, 1), "BUY", 5, 600),  # neue Haltephase
        Transaction.from_json(
            {
                "id": "4",
                "summary_type": "BrokerCashTransactionSummary",
                "status": "SETTLED",
                "last_event_datetime": d(2025, 3, 1).isoformat(),
                "related_isin": "X",
                "cash_transaction_type": "DISTRIBUTION",
                "amount": "12.5",
            }
        ),
        _tx("5", d(2025, 4, 1), "BUY", 5, 650),
    ]
    holding = Holding(isin="X", name="X", quantity=Decimal(10), fifo_price=Decimal(125))
    flows, quality, _ = build_cash_flows("X", txs, holding)
    assert quality == "exact"
    assert [f.kind for f in flows] == ["buy", "distribution", "buy"]
    assert sum(f.amount for f in flows) == pytest.approx(-600 + 12.5 - 650)


def test_cash_flows_fallback_when_history_incomplete():
    txs = [_tx("1", d(2024, 1, 1), "BUY", 3, 300)]  # Depot hat aber 10 Stück (Übertrag)
    holding = Holding(isin="X", name="X", quantity=Decimal(10), fifo_price=Decimal(90))
    flows, quality, notes = build_cash_flows("X", txs, holding)
    assert quality == "approx"
    assert flows[0].amount == pytest.approx(-900)
    assert notes


def test_cash_flows_override():
    holding = Holding(isin="X", name="X", quantity=Decimal(4), fifo_price=Decimal(50))
    flows, quality, _ = build_cash_flows("X", [], holding, entry_date=d(2023, 5, 1), entry_price=Decimal(40))
    assert quality == "override"
    assert flows[0].amount == pytest.approx(-160)


def test_position_returns():
    flows = [CashFlow(T0, -1000.0, 10)]
    r = position_returns(flows, 10, 110.0, T0 + timedelta(days=365))
    assert r.irr == pytest.approx(0.10, abs=1e-9)
    assert r.total_return == pytest.approx(0.10)
    assert r.holding_days == pytest.approx(365)
