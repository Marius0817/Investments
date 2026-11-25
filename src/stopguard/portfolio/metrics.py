"""Kennzahlen je Trade, je Position und für das Gesamtdepot."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from stopguard.config import Config
from stopguard.finance import xirr
from stopguard.portfolio.model import Instrument, Lot
from stopguard.portfolio.prices import PriceBook
from stopguard.portfolio.tax import TaxModel

Flow = tuple[datetime, float, str]  # (Zeitpunkt, Betrag aus Anlegersicht, Art)


def irr_of(flows: list[Flow]) -> float | None:
    """XIRR über Zahlungen (gleiche Zeitpunkte zusammengefasst); unplausible Werte → None."""
    merged: dict[datetime, float] = defaultdict(float)
    for when, amount, _ in flows:
        merged[when] += amount
    pts = [(d, a) for d, a in sorted(merged.items()) if abs(a) > 1e-9]
    if len(pts) < 2:
        return None
    r = xirr(pts)
    if r is None or r > 100 or r < -0.9999:
        return None
    return r


def _round(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


def _ratio(v: float | None, n: int = 6) -> float | None:
    return None if v is None else round(v, n)


def peak_capital(flows: list[Flow]) -> float:
    """Maximal gebundenes eigenes Kapital (größter negativer kumulierter Saldo)."""
    bal = peak = 0.0
    for _, a, _ in sorted(flows, key=lambda f: f[0]):
        bal += a
        peak = max(peak, -bal)
    return peak


@dataclass
class TradeResult:
    lot: Lot
    inst: Instrument
    current_price: float | None
    flows_pre: list[Flow]
    flows_post: list[Flow]
    flows_equity: list[Flow]
    data: dict[str, Any] = field(default_factory=dict)


def trade_metrics(lot: Lot, inst: Instrument, prices: PriceBook, tax: TaxModel, cfg: Config, now: datetime) -> TradeResult:
    price_now = prices.current(lot.isin)
    if price_now is None:
        price_now = lot.manual_current_price
    rem = lot.remaining_units
    open_value = rem * price_now if (rem > 0 and price_now is not None) else 0.0
    notes = list(lot.notes)
    if rem > 0 and price_now is None:
        notes.append("Kein aktueller Kurs – offener Teil mit 0 bewertet")

    # ---------------- Vorsteuer (Asset-Sicht) ----------------
    pre: list[Flow] = [(lot.open_date, -lot.invested, "buy")]
    dividends_cash = 0.0
    dividend_tax = 0.0
    for d in lot.dividends:
        inflow, dtax = tax.dividend_tax(d, inst)
        pre.append((d.when, inflow, "dividend"))
        dividends_cash += inflow
        dividend_tax += dtax
    proceeds = 0.0
    sold_cost = 0.0
    for e in lot.exits:
        amount = e.units * e.price - e.fees
        proceeds += amount
        sold_cost += e.units * lot.price + (lot.fees * e.units / lot.units if lot.units else 0.0)
        pre.append((e.when, amount, "sell"))
    if rem > 0:
        pre.append((now, open_value, "valuation"))

    # ---------------- Steuern ----------------
    post = list(pre)
    sale_tax = 0.0
    for e in lot.exits:
        t = tax.exit_tax(lot, inst, e)
        sale_tax += t
        if t:
            post.append((e.when, -t, "tax"))
    for d in lot.dividends:
        _, dtax = tax.dividend_tax(d, inst)
        if dtax:
            post.append((d.when, -dtax, "dividend_tax"))
    vap_total = 0.0
    for when, t in lot.vap_tax:
        vap_total += t
        post.append((when, -t, "vap_tax"))
    latent = tax.latent_tax(lot, inst, price_now) if (rem > 0 and price_now is not None) else 0.0
    if latent:
        post.append((now, -latent, "latent_tax"))

    # ---------------- Kredit (Eigenkapital-Sicht) ----------------
    equity = list(post)
    for when, a in lot.credit_draws:
        equity.append((when, a, "loan_draw"))
    for when, a in lot.credit_repayments:
        equity.append((when, -a, "loan_repayment"))
    interest_total = 0.0
    for when, a in lot.interest:
        interest_total += a
        equity.append((when, -a, "interest"))
        saved = tax.interest_tax_effect(a)
        if saved:
            equity.append((when, saved, "interest_tax_saving"))
    outstanding = lot.credit_outstanding
    if outstanding > 0.005:
        equity.append((lot.close_date or now, -outstanding, "loan_outstanding"))

    end = lot.close_date or now
    holding_days = (end - lot.open_date).total_seconds() / 86400
    profit_pre = sum(a for _, a, _ in pre)
    profit_post = sum(a for _, a, _ in post)
    profit_net = sum(a for _, a, _ in equity)
    irr_pre = irr_of(pre)
    irr_post = irr_of(post)

    credit: dict[str, Any] | None = None
    if lot.credit_draws:
        financed = lot.credit_financed
        bal_years = _balance_years(lot, end)
        eff_rate = interest_total / bal_years if bal_years > 0 else None
        peak_equity = peak_capital(equity)
        equity_irr = irr_of(equity) if peak_equity >= 0.01 * lot.invested else None
        credit = {
            "financed": _round(financed),
            "leverage": _ratio(financed / lot.invested if lot.invested else None, 4),
            "outstanding": _round(outstanding),
            "interest": _round(interest_total),
            "interest_estimated": lot.interest_estimated,
            "effective_rate": _ratio(eff_rate, 5),
            "equity_invested": _round(peak_equity),
            "equity_irr": _ratio(equity_irr),
            "return_on_equity": _ratio(profit_net / peak_equity if peak_equity > 0 else None),
            "profit_net": _round(profit_net),
            "break_even_irr": _ratio(tax.break_even_irr(eff_rate, inst) if eff_rate is not None else None),
            "spread": _ratio(irr_pre - eff_rate if (irr_pre is not None and eff_rate is not None) else None),
        }
        if equity_irr is None:
            notes.append("Nahezu vollständig fremdfinanziert – Eigenkapital-IRR nicht aussagekräftig, siehe return_on_equity/profit_net")

    # ---------------- Benchmark ----------------
    bench_isin = inst.benchmark or cfg.portfolio.benchmark_isin
    benchmark = None
    if bench_isin and bench_isin != lot.isin:
        p0 = prices.at(bench_isin, lot.open_date)
        if p0:
            bflows: list[Flow] = [(lot.open_date, -lot.invested, "buy")]
            ok = True
            for e in lot.exits:
                pe = prices.at(bench_isin, e.when)
                if pe is None:
                    ok = False
                    break
                bflows.append((e.when, lot.invested * e.units / lot.units * pe / p0, "sell"))
            if ok and rem > 0:
                pn = prices.current(bench_isin)
                if pn is None:
                    ok = False
                else:
                    bflows.append((now, lot.invested * rem / lot.units * pn / p0, "valuation"))
            if ok:
                b_irr = irr_of(bflows)
                b_profit = sum(a for _, a, _ in bflows)
                benchmark = {
                    "isin": bench_isin,
                    "irr": _ratio(b_irr),
                    "profit": _round(b_profit),
                    "alpha_irr": _ratio(irr_pre - b_irr if (irr_pre is not None and b_irr is not None) else None),
                    "outperformance": _round(profit_pre - b_profit),
                }

    status = "closed" if lot.is_closed else ("partial" if lot.exits else "open")
    avg_sell = (sum(e.units * e.price for e in lot.exits) / sum(e.units for e in lot.exits)) if lot.exits else None
    data = {
        "id": lot.id,
        "source": lot.source,
        "origin": lot.origin,
        "isin": lot.isin,
        "ticker": lot.ticker or inst.ticker,
        "name": inst.name if inst.name != inst.isin else lot.name,
        "kind": inst.kind,
        "tags": inst.tags or lot.tags,
        "status": status,
        "open_date": lot.open_date.date().isoformat(),
        "close_date": lot.close_date.date().isoformat() if lot.close_date else None,
        "holding_days": round(holding_days, 1),
        "units": round(lot.units, 6),
        "remaining_units": round(rem, 6),
        "buy_price": _round(lot.price, 4),
        "avg_sell_price": _round(avg_sell, 4),
        "current_price": _round(price_now, 4) if rem > 0 else None,
        "invested": _round(lot.invested),
        "fees": _round(lot.fees + sum(e.fees for e in lot.exits)),
        "proceeds": _round(proceeds),
        "current_value": _round(open_value),
        "dividends": _round(dividends_cash),
        "realized_pl": _round(proceeds - sold_cost) if lot.exits else 0.0,
        "unrealized_pl": _round(open_value - (lot.invested - sold_cost)) if rem > 0 else 0.0,
        "profit": _round(profit_pre),
        "return_pct": _ratio(profit_pre / lot.invested if lot.invested else None),
        "irr": _ratio(irr_pre),
        "taxes": {
            "method": cfg.tax.method,
            "sale": _round(sale_tax),
            "dividends": _round(dividend_tax),
            "vorabpauschale": _round(vap_total),
            "latent": _round(latent),
            "total": _round(sale_tax + dividend_tax + vap_total + latent),
        },
        "profit_after_tax": _round(profit_post),
        "irr_after_tax": _ratio(irr_post),
        "credit": credit,
        "benchmark": benchmark,
        "notes": notes,
        "cashflows": [
            {"date": w.date().isoformat(), "type": k, "amount": round(a, 2)} for w, a, k in sorted(equity, key=lambda f: f[0])
        ],
    }
    return TradeResult(lot, inst, price_now, pre, post, equity, data)


def _balance_years(lot: Lot, end: datetime) -> float:
    events = sorted([(d, a) for d, a in lot.credit_draws] + [(d, -a) for d, a in lot.credit_repayments])
    if not events:
        return 0.0
    total = 0.0
    bal = 0.0
    prev = events[0][0]
    for d, delta in events:
        total += bal * (d - prev).total_seconds()
        bal = max(0.0, bal + delta)
        prev = d
    if end > prev:
        total += bal * (end - prev).total_seconds()
    return total / 86400 / 365


def aggregate_positions(results: list[TradeResult], now: datetime, stop_states: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[TradeResult]] = defaultdict(list)
    for r in results:
        groups[r.lot.isin].append(r)
    positions = []
    total_value = sum(r.data["current_value"] or 0 for r in results)
    for isin, rs in groups.items():
        open_rs = [r for r in rs if r.lot.remaining_units > 0]
        units = sum(r.lot.remaining_units for r in open_rs)
        value = sum(r.data["current_value"] or 0 for r in open_rs)
        cost = sum(
            r.lot.remaining_units * r.lot.price + (r.lot.fees * r.lot.remaining_units / r.lot.units if r.lot.units else 0)
            for r in open_rs
        )
        inst = rs[0].inst
        pre = [f for r in rs for f in r.flows_pre]
        post = [f for r in rs for f in r.flows_post]
        eq = [f for r in rs for f in r.flows_equity]
        has_credit = any(r.lot.credit_draws for r in rs)
        first = min(r.lot.open_date for r in rs)
        ps = stop_states.get(isin)
        positions.append(
            {
                "isin": isin,
                "name": rs[0].data["name"],
                "ticker": rs[0].data["ticker"],
                "kind": inst.kind,
                "tags": inst.tags,
                "status": "open" if units > 1e-9 else "closed",
                "units": round(units, 6),
                "avg_buy_price": _round(cost / units, 4) if units > 0 else None,
                "current_price": _round(open_rs[0].current_price, 4) if open_rs and open_rs[0].current_price is not None else None,
                "cost_basis": _round(cost),
                "value": _round(value),
                "weight": _ratio(value / total_value if total_value else None, 5),
                "unrealized_pl": _round(value - cost),
                "unrealized_pct": _ratio((value - cost) / cost if cost else None),
                "realized_pl": _round(sum(r.data["realized_pl"] or 0 for r in rs)),
                "dividends": _round(sum(r.data["dividends"] or 0 for r in rs)),
                "fees": _round(sum(r.data["fees"] or 0 for r in rs)),
                "profit": _round(sum(r.data["profit"] or 0 for r in rs)),
                "irr": _ratio(irr_of(pre)),
                "irr_after_tax": _ratio(irr_of(post)),
                "equity_irr": _ratio(irr_of(eq)) if has_credit else None,
                "latent_tax": _round(sum(r.data["taxes"]["latent"] or 0 for r in open_rs)),
                "credit_outstanding": _round(sum(r.lot.credit_outstanding for r in rs)),
                "interest": _round(sum(sum(a for _, a in r.lot.interest) for r in rs)),
                "first_buy": first.date().isoformat(),
                "holding_days": round((now - first).total_seconds() / 86400, 1),
                "trades": len(rs),
                "open_trades": len(open_rs),
                "stopguard": (
                    {"strategy": ps.strategy, "phase": ps.phase_name, "target_stop": ps.last_target_stop, "order_id": ps.managed_order_id}
                    if ps is not None
                    else None
                ),
            }
        )
    positions.sort(key=lambda p: -(p["value"] or 0))
    return positions
