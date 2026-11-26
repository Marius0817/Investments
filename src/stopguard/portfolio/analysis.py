"""Gesamtauswertung: setzt Kassenbuch, Trades, Kredit, Steuern und Zeitreihen zusammen.

Ergebnis ist ein JSON-fähiges Dokument (Schema ``stopguard.portfolio/1``),
beschrieben in docs/portfolio-data.md.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from stopguard import __version__
from stopguard.config import Config
from stopguard.finance import xirr
from stopguard.models import parse_time, utcnow
from stopguard.portfolio.credit import CreditReport, allocate_credit
from stopguard.portfolio.dividends import add_growth, build_dividend_forecast
from stopguard.portfolio.exposure import build_exposure
from stopguard.portfolio.insights import build_insights
from stopguard.portfolio.ledger import build_ledger
from stopguard.portfolio.lots import apply_credit_flags, build_instruments, build_lots, dedupe_manual, manual_to_lots
from stopguard.portfolio.metrics import Flow, TradeResult, aggregate_positions, irr_of, trade_metrics
from stopguard.portfolio.model import Instrument, LedgerEvent, Lot
from stopguard.portfolio.prices import PriceBook
from stopguard.portfolio.store import PortfolioStore
from stopguard.portfolio.tax import BASISZINS, TaxModel

SCHEMA = "stopguard.portfolio/1"

CHART_HINTS = [
    {"dataset": "timeseries.portfolio", "chart": "area", "x": "date", "y": ["value", "invested"], "title": "Depotwert vs. investiertes Kapital"},
    {"dataset": "timeseries.portfolio", "chart": "line", "x": "date", "y": ["net_worth", "loan"], "title": "Vermögen und Kredit"},
    {"dataset": "timeseries.twr", "chart": "line", "x": "date", "y": ["portfolio", "benchmark"], "title": "Performance (TWR, indexiert) vs. Benchmark"},
    {"dataset": "timeseries.drawdown", "chart": "area", "x": "date", "y": ["drawdown"], "title": "Drawdown"},
    {"dataset": "timeseries.irr", "chart": "line", "x": "date", "y": ["irr", "irr_after_tax"], "title": "IRR-Verlauf"},
    {"dataset": "timeseries.positions", "chart": "stacked_area", "x": "date", "series": "isin", "title": "Positionswerte im Zeitverlauf"},
    {"dataset": "allocation.by_position", "chart": "donut", "value": "value", "label": "name", "title": "Aufteilung nach Position"},
    {"dataset": "allocation.by_kind", "chart": "donut", "value": "value", "label": "kind", "title": "Aufteilung nach Anlageart"},
    {"dataset": "trades", "chart": "scatter", "x": "holding_days", "y": "irr", "size": "invested", "color": "status", "title": "Trades: Haltedauer vs. IRR"},
    {"dataset": "trades", "chart": "bar", "x": "name", "y": ["profit", "profit_after_tax"], "title": "Gewinn je Trade vor/nach Steuern"},
    {"dataset": "trades", "chart": "bar", "x": "name", "y": ["irr", "benchmark.irr"], "title": "Trade-IRR vs. Benchmark im selben Zeitraum"},
    {"dataset": "credit.trades", "chart": "bar", "x": "name", "y": ["irr", "irr_after_tax", "credit.equity_irr"], "title": "Kredit-Trades: Asset- vs. Eigenkapital-IRR"},
    {"dataset": "credit.balance", "chart": "step_line", "x": "date", "y": ["loan"], "title": "Kreditsaldo"},
    {"dataset": "costs.monthly", "chart": "stacked_bar", "x": "month", "y": ["interest", "fees", "taxes"], "title": "Kosten je Monat"},
    {"dataset": "income.monthly", "chart": "stacked_bar", "x": "month", "y": ["dividends", "interest"], "title": "Erträge je Monat"},
    {"dataset": "periods.years", "chart": "bar", "x": "year", "y": ["twr", "benchmark"], "title": "Rendite je Kalenderjahr (TWR) vs. Benchmark"},
    {"dataset": "taxes.years", "chart": "stacked_bar", "x": "year", "y": ["tax_model", "tax_actual"], "title": "Steuern je Jahr (Modell vs. tatsächlich)"},
    {"dataset": "income.forecast.monthly", "chart": "bar", "x": "month", "y": ["amount"], "title": "Erwartete Ausschüttungen der nächsten 12 Monate"},
    {"dataset": "allocation.by_region", "chart": "bar_ranked", "value": "value", "label": "key", "title": "Aufteilung nach Region (mit ETF-Durchschau)"},
    {"dataset": "allocation.by_sector", "chart": "bar_ranked", "value": "value", "label": "key", "title": "Aufteilung nach Branche"},
    {"dataset": "allocation.look_through.items", "chart": "bar_stacked", "label": "name", "y": ["direct", "via_funds"], "title": "Größte Einzelwerte direkt und über Fonds"},
    {"dataset": "positions", "chart": "treemap", "size": "value", "color": "performance.1m", "label": "name", "title": "Heatmap: Gewicht und Kursentwicklung"},
]


def _r(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


def _date(d: datetime) -> str:
    return d.date().isoformat()


def _month(d: datetime) -> str:
    return d.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Zeitraster
# ---------------------------------------------------------------------------

def date_grid(start: datetime, end: datetime, resolution: str) -> list[datetime]:
    start = start.replace(hour=23, minute=59, second=0, microsecond=0)
    out: list[datetime] = []
    if resolution == "monthly":
        y, m = start.year, start.month
        while True:
            nxt = datetime(y + m // 12, m % 12 + 1, 1, tzinfo=timezone.utc) - timedelta(minutes=1)
            if nxt >= end:
                break
            if nxt >= start:
                out.append(nxt)
            y, m = y + m // 12, m % 12 + 1
    else:
        step = timedelta(days=1 if resolution == "daily" else 7)
        t = start
        while t < end:
            out.append(t)
            t += step
    out.append(end)
    return out


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------

def analyse_portfolio(store: PortfolioStore, cfg: Config, *, now: datetime | None = None, stop_states: dict | None = None) -> dict[str, Any]:
    now = now or utcnow()
    quality: dict[str, Any] = {"warnings": [], "notes": []}

    txs = store.transactions()
    ledger = build_ledger(txs, cfg.credit)
    events = ledger.events
    if ledger.unknown_types:
        quality["warnings"].append(
            "Unbekannte Buchungstypen (als 'other' verbucht): "
            + ", ".join(f"{k} ×{v}" for k, v in sorted(ledger.unknown_types.items()))
        )
    if ledger.without_details:
        quality["notes"].append(
            f"{ledger.without_details} Käufe/Verkäufe ohne Detaildaten – Gebühren/Steuern dort unbekannt ('stopguard portfolio sync')"
        )

    holdings = store.holdings()
    current = {h["isin"]: h["mid_price"] for h in holdings if h.get("mid_price")}
    prices = PriceBook(store.prices(), current)

    names: dict[str, str] = {h["isin"]: h["name"] for h in holdings if h.get("name")}
    for t in txs:
        sec = ((t.get("_details") or {}).get("security") or {})
        if sec.get("isin") and sec.get("name"):
            names.setdefault(sec["isin"], sec["name"])

    lots_res = build_lots(events)
    quality["warnings"].extend(lots_res.warnings)
    manual_lots, mwarn = manual_to_lots(store.manual_lots(), cfg, now)
    quality["warnings"].extend(mwarn)
    manual_lots, dnotes = dedupe_manual(manual_lots, lots_res.lots, cfg.portfolio.dedupe_days)
    if dnotes:
        quality["notes"].append(f"{len(dnotes)} importierte Trades waren bereits in den Scalable-Daten enthalten")
    for lot in manual_lots:
        names.setdefault(lot.isin, lot.name)
    all_lots: list[Lot] = lots_res.lots + manual_lots
    apply_credit_flags(all_lots, cfg)

    instruments = build_instruments(cfg, holdings, names)
    for lot in all_lots:
        inst = instruments.setdefault(lot.isin, Instrument(lot.isin, lot.name))
        lot.name = inst.name
        if inst.ticker and not lot.ticker:
            lot.ticker = inst.ticker

    # Bestandsabgleich
    for h in holdings:
        units = sum(lot.remaining_units for lot in lots_res.lots if lot.isin == h["isin"])
        if h.get("quantity") is not None and abs(units - h["quantity"]) > max(1e-4, 1e-4 * h["quantity"]):
            quality["warnings"].append(
                f"{h.get('name')}: Bestand laut Scalable {h['quantity']:g} Stk., aus Transaktionen {units:g} Stk."
            )

    account = store.latest_account()
    credit_report = allocate_credit(
        cfg.credit.mode,
        events,
        lots_res,
        manual_lots,
        cfg.credit,
        now,
        account.get("loaned") if account else None,
    )
    quality["warnings"].extend(credit_report.warnings)

    tax = TaxModel(cfg.tax, prices, now)
    for lot in all_lots:
        tax.apply_vorabpauschale(lot, instruments[lot.isin])

    results = [trade_metrics(lot, instruments[lot.isin], prices, tax, cfg, now) for lot in all_lots]
    results.sort(key=lambda r: r.lot.open_date)
    quality["notes"].extend(sorted(tax.notes))

    positions = aggregate_positions(results, now, stop_states or {})
    for pos in positions:
        pos["performance"] = price_performance(pos["isin"], prices, now) if pos["status"] == "open" else None
    trades = [r.data for r in results]

    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "generator": f"stopguard {__version__}",
        "generated_at": utcnow().isoformat(),
        "as_of": now.isoformat(),
        "currency": "EUR",
        "settings": {
            "benchmark": {"isin": cfg.portfolio.benchmark_isin, "name": cfg.portfolio.benchmark_name},
            "tax": {
                "enabled": cfg.tax.enabled,
                "method": cfg.tax.method,
                "effective_rate": round(tax.rate, 6),
                "church_tax_pct": cfg.tax.church_tax_pct,
                "loss_offset": cfg.tax.loss_offset,
                "vorabpauschale": cfg.tax.vorabpauschale,
                "interest_deductible": cfg.tax.interest_deductible,
                "allowance_eur": cfg.tax.allowance_eur,
            },
            "credit_mode": cfg.credit.mode,
            "resolution": cfg.portfolio.resolution,
        },
        "positions": positions,
        "trades": trades,
    }
    doc["summary"] = build_summary(results, positions, events, account, credit_report, store, cfg, now, prices)
    doc["credit"] = build_credit(results, credit_report, account, events, now)
    doc["taxes"] = build_taxes(results, events, tax, cfg, now, prices)
    doc["income"] = build_income(results, events)
    doc["income"]["by_year"] = add_growth(doc["income"]["by_year"], min((r.lot.open_date.year for r in results), default=None))
    doc["income"]["forecast"] = build_dividend_forecast(results, positions, now)
    doc["costs"] = build_costs(results, events, credit_report)
    doc["allocation"] = build_allocation(positions)
    doc["allocation"].update(build_exposure(positions, instruments))
    doc["activities"] = build_activities(events, {i: inst.name for i, inst in instruments.items()})
    doc["timeseries"] = build_timeseries(results, events, credit_report, store, cfg, now, prices)
    doc["periods"] = build_periods(doc["timeseries"], events, now)
    doc["summary"]["twr"] = doc["periods"]["twr_summary"]
    doc["data_quality"] = {
        **quality,
        "sources": {
            "scalable_transactions": len(txs),
            "ledger_events": len(events),
            "imported_trades": len(manual_lots),
            "price_series": prices.series_count(),
            "chart_points": len(store.chart()),
            "last_sync_at": store.get_meta("last_sync_at"),
        },
        "reconciliation": {
            "cash_reconstructed": _r(credit_report.reconstructed_cash),
            "cash_reported": _r(account.get("cash_balance")) if account else None,
            "loan_reconstructed": _r(credit_report.reconstructed_loan),
            "loan_reported": _r(account.get("loaned")) if account else None,
        },
    }
    doc["insights"] = build_insights(doc, cfg, now)
    doc["chart_hints"] = CHART_HINTS
    return doc


PERFORMANCE_PERIODS = (("1w", 7), ("1m", 30), ("3m", 91), ("1y", 365))


def price_performance(isin: str, prices: PriceBook, now: datetime) -> dict[str, float | None]:
    """Kursentwicklung (ohne Zu-/Verkäufe) über feste Zeiträume – Grundlage der Heatmap."""
    cur = prices.current(isin)
    out: dict[str, float | None] = {}
    if not cur:
        return {k: None for k, _ in PERFORMANCE_PERIODS} | {"ytd": None}
    for key, days in PERFORMANCE_PERIODS:
        base = prices.at(isin, now - timedelta(days=days), tolerance_days=3)
        out[key] = round(cur / base - 1, 5) if base else None
    jan1 = datetime(now.year, 1, 1, tzinfo=timezone.utc) - timedelta(minutes=1)
    base = prices.at(isin, jan1, tolerance_days=3)
    out["ytd"] = round(cur / base - 1, 5) if base else None
    return out


ACTIVITY_KINDS = (
    "deposit", "withdrawal", "buy", "sell", "dividend", "interest_income", "loan_interest", "fee", "tax", "tax_refund",
    "transfer_in", "transfer_out", "other",
)


def build_activities(events: list[LedgerEvent], names: dict[str, str]) -> list[dict[str, Any]]:
    """Alle Buchungen des Verrechnungskontos, neueste zuerst (Scalable-Daten; Importe erscheinen als Trades)."""
    out = []
    for e in sorted(events, key=lambda x: x.when, reverse=True):
        out.append({
            "id": e.id,
            "date": e.when.isoformat(),
            "kind": e.kind if e.kind in ACTIVITY_KINDS else "other",
            "isin": e.isin,
            "name": names.get(e.isin, e.isin) if e.isin else None,
            "units": _r(e.units, 6) if e.units else None,
            "price": _r(e.price, 4),
            "amount": _r(e.cash),
            "fees": _r(e.fees) if e.fees else None,
            "tax": _r(e.tax) if e.tax else None,
            "gross": _r(e.gross),
            "description": e.description,
        })
    return out


# ---------------------------------------------------------------------------
# Abschnitte
# ---------------------------------------------------------------------------

def build_summary(
    results: list[TradeResult],
    positions: list[dict[str, Any]],
    events: list[LedgerEvent],
    account: dict[str, Any] | None,
    credit: CreditReport,
    store: PortfolioStore,
    cfg: Config,
    now: datetime,
    prices: PriceBook,
) -> dict[str, Any]:
    value = sum(p["value"] or 0 for p in positions)
    cash = account.get("cash_balance") if account else None
    if cash is None and credit.reconstructed_cash is not None:
        cash = credit.reconstructed_cash
    loan = account.get("loaned") if account else None
    if loan is None and credit.reconstructed_loan is not None and cfg.credit.mode == "auto":
        loan = credit.reconstructed_loan
    manual_debt = sum(r.lot.credit_outstanding for r in results if r.lot.source != "scalable")
    liabilities = max(loan or 0.0, -(cash or 0.0) if (cash or 0) < 0 else 0.0) + manual_debt
    net_worth = value + max(cash or 0.0, 0.0) - liabilities

    pre = [f for r in results for f in r.flows_pre]
    post = [f for r in results for f in r.flows_post]
    eq = [f for r in results for f in r.flows_equity]

    ext = [(e.when, -e.cash) for e in events if e.external]  # Einzahlung = Abfluss beim Anleger
    account_irr = None
    if ext and any(a < 0 for _, a in ext):
        sec_value_scalable = sum(r.data["current_value"] or 0 for r in results if r.lot.source == "scalable")
        terminal = sec_value_scalable + (cash or 0.0) - (loan or 0.0 if (cash or 0) >= 0 else 0.0)
        account_irr = xirr(ext + [(now, terminal)])

    closed = [r for r in results if r.lot.is_closed]
    wins = [r for r in closed if (r.data["profit"] or 0) > 0]
    ranked = sorted((r for r in results if r.data["irr"] is not None and r.data["holding_days"] >= 30), key=lambda r: r.data["irr"])

    def brief(r: TradeResult) -> dict[str, Any]:
        return {"id": r.lot.id, "name": r.data["name"], "irr": r.data["irr"], "profit": r.data["profit"], "status": r.data["status"]}

    return {
        "securities_value": _r(value),
        "cash": _r(cash),
        "loan": _r(loan),
        "net_worth": _r(net_worth),
        "cost_basis_open": _r(sum(p["cost_basis"] or 0 for p in positions)),
        "unrealized_pl": _r(sum(p["unrealized_pl"] or 0 for p in positions)),
        "realized_pl": _r(sum(r.data["realized_pl"] or 0 for r in results)),
        "dividends": _r(sum(r.data["dividends"] or 0 for r in results)),
        "fees": _r(sum(r.data["fees"] or 0 for r in results)),
        "interest_paid": _r(sum(sum(a for _, a in r.lot.interest) for r in results) + credit.interest_unallocated),
        "latent_tax": _r(sum(r.data["taxes"]["latent"] or 0 for r in results)),
        "net_deposits": _r(-sum(a for _, a in ext)) if ext else None,
        "irr": {
            "trades": irr_of(pre),
            "trades_after_tax": irr_of(post),
            "trades_after_tax_and_interest": irr_of(eq),
            "account": account_irr,
        },
        "counts": {
            "positions_open": sum(1 for p in positions if p["status"] == "open"),
            "trades": len(results),
            "trades_open": sum(1 for r in results if not r.lot.is_closed),
            "trades_closed": len(closed),
            "trades_credit": sum(1 for r in results if r.lot.credit_draws),
            "win_rate_closed": round(len(wins) / len(closed), 4) if closed else None,
        },
        "best_trades": [brief(r) for r in reversed(ranked[-3:])],
        "worst_trades": [brief(r) for r in ranked[:3]],
    }


def build_credit(results: list[TradeResult], report: CreditReport, account: dict[str, Any] | None, events: list[LedgerEvent], now: datetime) -> dict[str, Any]:
    credit_trades = [r.data for r in results if r.lot.credit_draws]
    loan = account.get("loaned") if account else report.reconstructed_loan
    available = account.get("available_credit_line") if account else None
    sec_value = sum(r.data["current_value"] or 0 for r in results)
    year_ago = now - timedelta(days=365)
    actual_12m = sum(a for d, a in report.interest_events if d >= year_ago)  # echte Scalable-Zinsbuchungen
    estimated_12m = sum(a for r in results if r.lot.interest_estimated for d, a in r.lot.interest if d >= year_ago)
    interest_12m = actual_12m + estimated_12m
    avg_balance = _avg_balance(report.balance_series, year_ago, now)
    stress = None
    if loan and available is not None and loan + available > 0:
        # Annahme: Beleihungsgrenze sinkt proportional zum Depotwert
        stress = {
            "utilization": round(loan / (loan + available), 4),
            "drop_until_limit": round(1 - loan / (loan + available), 4),
            "assumption": "Kreditlinie schwankt proportional zum Depotwert (vereinfachte Schätzung)",
        }
    monthly: dict[str, float] = defaultdict(float)
    for d, a in report.interest_events:
        monthly[_month(d)] += a
    for r in results:
        if r.lot.interest_estimated:
            for d, a in r.lot.interest:
                monthly[_month(d)] += a
    return {
        "mode": report.mode,
        "loan": _r(loan),
        "available_credit_line": _r(available),
        "loan_to_value": round(loan / sec_value, 4) if (loan and sec_value) else None,
        "interest_total": _r(report.interest_total),
        "interest_12m": _r(interest_12m),
        "interest_12m_estimated": _r(estimated_12m),
        # effektiver Satz nur aus echten Zinsbuchungen und dem Scalable-Kreditsaldo
        "effective_rate_12m": round(actual_12m / avg_balance, 5) if (avg_balance > 1 and actual_12m) else None,
        "interest_unallocated": _r(report.interest_unallocated),
        "overdraft_draws": _r(report.overdraft_draws),
        "artifacts_removed": report.artifacts_removed,
        "stress": stress,
        "trades": credit_trades,
        "balance": [{"date": _date(d), "loan": round(b, 2)} for d, b in _dedupe_daily(report.balance_series)],
        "interest_monthly": [{"month": m, "interest": round(v, 2)} for m, v in sorted(monthly.items())],
    }


def _dedupe_daily(series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    out: dict[str, tuple[datetime, float]] = {}
    for d, b in series:
        out[_date(d)] = (d, b)
    return [out[k] for k in sorted(out)]


def _avg_balance(series: list[tuple[datetime, float]], start: datetime, end: datetime) -> float:
    if not series:
        return 0.0
    total = 0.0
    bal = 0.0
    prev = start
    for d, b in series:
        if d <= start:
            bal = b
            continue
        if d > end:
            break
        total += bal * (d - prev).total_seconds()
        bal, prev = b, d
    total += bal * (end - prev).total_seconds()
    return total / (end - start).total_seconds()


def build_taxes(results: list[TradeResult], events: list[LedgerEvent], tax: TaxModel, cfg: Config, now: datetime, prices: PriceBook) -> dict[str, Any]:
    """Jahresübersicht mit Verlusttöpfen (Aktien / sonstige) und Freistellungsauftrag."""
    years: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in results:
        lot, inst = r.lot, r.inst
        tf = inst.teilfreistellung
        for e in lot.exits:
            gain = tax.sale_gain(lot, inst, e.units, e.price, e.fees, e.when) * (1 - tf)
            pot = "stock" if inst.kind == "stock" else "other"
            years[e.when.year][f"gains_{pot}"] += gain
        for d in lot.dividends:
            gross, _ = tax.dividend_tax(d, inst)
            if d.gross is not None:
                years[d.when.year]["dividends"] += gross * (1 - tf)
        for (when, vap_unit) in lot.vap_per_unit:
            units = lot.units_at(when - timedelta(days=3))
            years[when.year]["vorabpauschale"] += vap_unit * units * (1 - tf)
    for e in events:
        y = e.when.year
        if e.kind == "interest_income":
            years[y]["interest_income"] += e.cash
        if e.kind == "tax":
            years[y]["tax_actual"] += -e.cash
        elif e.kind == "tax_refund":
            years[y]["tax_actual"] -= e.cash
        elif e.kind in ("sell", "dividend") and e.tax:
            years[y]["tax_actual"] += e.tax

    out = []
    stock_carry = other_carry = 0.0
    allowance = cfg.tax.allowance_eur
    for y in sorted(years):
        v = years[y]
        stock = v["gains_stock"] + stock_carry
        other = v["gains_other"] + v["dividends"] + v["interest_income"] + v["vorabpauschale"] + other_carry
        # Aktienverluste nur mit Aktiengewinnen verrechenbar
        if stock < 0:
            stock_carry, stock = stock, 0.0
        else:
            stock_carry = 0.0
        taxable = stock + other
        if taxable < 0:
            other_carry, taxable = taxable, 0.0
        else:
            other_carry = 0.0
        used = min(allowance, taxable)
        tax_model = (taxable - used) * tax.rate
        out.append(
            {
                "year": y,
                "gains_stock": _r(v["gains_stock"]),
                "gains_other": _r(v["gains_other"]),
                "dividends_taxable": _r(v["dividends"]),
                "interest_income": _r(v["interest_income"]),
                "vorabpauschale": _r(v["vorabpauschale"]),
                "taxable": _r(taxable),
                "allowance_used": _r(used),
                "loss_carryforward_stock": _r(stock_carry),
                "loss_carryforward_other": _r(other_carry),
                "tax_model": _r(tax_model),
                "tax_actual": _r(v["tax_actual"]),
            }
        )
    this_year = next((row for row in out if row["year"] == now.year), None)
    remaining = allowance - (this_year["allowance_used"] if this_year else 0.0)
    harvest = []
    for r in results:
        if r.lot.remaining_units <= 0 or r.current_price is None:
            continue
        tf = r.inst.teilfreistellung
        gain = tax.sale_gain(r.lot, r.inst, r.lot.remaining_units, r.current_price, 0.0, now)
        if gain > 0:
            harvest.append(
                {
                    "trade_id": r.lot.id,
                    "isin": r.lot.isin,
                    "name": r.data["name"],
                    "unrealized_gain": _r(gain),
                    "taxable_gain": _r(gain * (1 - tf)),
                    "teilfreistellung": tf,
                }
            )
    harvest.sort(key=lambda h: -(h["taxable_gain"] or 0))
    return {
        "effective_rate": round(tax.rate, 6),
        "rate_formula": "25 % Abgeltungsteuer + 5,5 % Soli" + (f" + {cfg.tax.church_tax_pct:g} % KiSt" if cfg.tax.church_tax_pct else ""),
        "basiszins": {str(k): v for k, v in sorted({**BASISZINS, **cfg.tax.basiszins}.items())},
        "years": out,
        "allowance": {
            "annual": allowance,
            "remaining_this_year": _r(max(0.0, remaining)),
            "hint": "Steuerfrei realisierbarer Gewinn (nach Teilfreistellung) bis zur Höhe des verbleibenden Freibetrags",
            "candidates": harvest[:50],
        },
        "latent_total": _r(sum(r.data["taxes"]["latent"] or 0 for r in results)),
        "disclaimer": "Modellrechnung, keine Steuerberatung. Quellensteuer, Verlustverrechnungsreihenfolge und Ersatzbemessungsgrundlagen können abweichen.",
    }


def build_income(results: list[TradeResult], events: list[LedgerEvent]) -> dict[str, Any]:
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_position: dict[str, float] = defaultdict(float)
    by_year: dict[int, float] = defaultdict(float)
    names: dict[str, str] = {}
    for r in results:
        names[r.lot.isin] = r.data["name"]
        for d in r.lot.dividends:
            monthly[_month(d.when)]["dividends"] += d.net
            by_position[r.lot.isin] += d.net
            by_year[d.when.year] += d.net
    for e in events:
        if e.kind == "interest_income":
            monthly[_month(e.when)]["interest"] += e.cash
    value = sum(r.data["current_value"] or 0 for r in results)
    last12 = sum(v["dividends"] for m, v in monthly.items() if m >= _month(utcnow() - timedelta(days=365)))
    return {
        "monthly": [{"month": m, "dividends": round(v["dividends"], 2), "interest": round(v["interest"], 2)} for m, v in sorted(monthly.items())],
        "by_year": [{"year": y, "dividends": round(v, 2)} for y, v in sorted(by_year.items())],
        "by_position": sorted(
            ({"isin": k, "name": names.get(k, k), "dividends": round(v, 2)} for k, v in by_position.items() if v),
            key=lambda x: -x["dividends"],
        ),
        "trailing_12m": round(last12, 2),
        "yield_12m": round(last12 / value, 5) if value else None,
    }


def build_costs(results: list[TradeResult], events: list[LedgerEvent], credit: CreditReport) -> dict[str, Any]:
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in events:
        m = _month(e.when)
        if e.kind in ("buy", "sell"):
            monthly[m]["fees"] += e.fees
        if e.kind == "fee":
            monthly[m]["fees"] += -e.cash
        if e.kind == "loan_interest":
            monthly[m]["interest"] += -e.cash
        if e.kind == "tax":
            monthly[m]["taxes"] += -e.cash
        if e.kind == "tax_refund":
            monthly[m]["taxes"] -= e.cash
        if e.kind in ("sell", "dividend") and e.tax:
            monthly[m]["taxes"] += e.tax
    for r in results:
        if r.lot.source != "scalable":
            monthly[_month(r.lot.open_date)]["fees"] += r.lot.fees
        if r.lot.interest_estimated:
            for d, a in r.lot.interest:
                monthly[_month(d)]["interest_estimated"] += a
    rows = [
        {
            "month": m,
            "fees": round(v["fees"], 2),
            "interest": round(v["interest"], 2),
            "interest_estimated": round(v["interest_estimated"], 2),
            "taxes": round(v["taxes"], 2),
        }
        for m, v in sorted(monthly.items())
    ]
    years: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        y = row["month"][:4]
        for k in ("fees", "interest", "interest_estimated", "taxes"):
            years[y][k] += row[k]
    return {
        "monthly": rows,
        "by_year": [{"year": int(y), **{k: round(v, 2) for k, v in vals.items()}} for y, vals in sorted(years.items())],
    }


def build_allocation(positions: list[dict[str, Any]]) -> dict[str, Any]:
    open_pos = [p for p in positions if p["status"] == "open"]
    total = sum(p["value"] or 0 for p in open_pos) or 1.0

    def group(key) -> list[dict[str, Any]]:
        acc: dict[str, float] = defaultdict(float)
        for p in open_pos:
            for k in key(p):
                acc[k] += p["value"] or 0
        return sorted(({"key": k, "value": round(v, 2), "weight": round(v / total, 5)} for k, v in acc.items()), key=lambda x: -x["value"])

    return {
        "by_position": [
            {"isin": p["isin"], "name": p["name"], "value": p["value"], "weight": p["weight"]} for p in open_pos
        ],
        "by_kind": group(lambda p: [p["kind"]]),
        "by_tag": group(lambda p: p["tags"] or ["ohne Tag"]),
        "credit_financed": round(sum(p["credit_outstanding"] or 0 for p in open_pos) / total, 5),
    }


def build_timeseries(
    results: list[TradeResult],
    events: list[LedgerEvent],
    credit: CreditReport,
    store: PortfolioStore,
    cfg: Config,
    now: datetime,
    prices: PriceBook,
) -> dict[str, Any]:
    if not results:
        return {"resolution": cfg.portfolio.resolution, "portfolio": [], "positions": {}, "twr": [], "drawdown": [], "irr": [], "source": None}
    start = min(r.lot.open_date for r in results)
    grid = date_grid(start, now, cfg.portfolio.resolution)

    # Kassenbuch kumuliert
    ev_sorted = sorted(events, key=lambda e: e.when)
    balance_series = credit.balance_series
    portfolio_rows = []
    positions: dict[str, list[list[Any]]] = defaultdict(list)
    ei = bi = 0
    cash = 0.0
    loan_auto = 0.0
    deposits = 0.0
    for d in grid:
        while ei < len(ev_sorted) and ev_sorted[ei].when <= d:
            cash += ev_sorted[ei].cash
            if ev_sorted[ei].external:
                deposits += ev_sorted[ei].cash
            ei += 1
        while bi < len(balance_series) and balance_series[bi][0] <= d:
            loan_auto = balance_series[bi][1]
            bi += 1
        value = invested = loan_manual = 0.0
        per_isin: dict[str, float] = defaultdict(float)
        for r in results:
            lot = r.lot
            units = lot.units_at(d)
            if units <= 0:
                continue
            p = prices.at(lot.isin, d) if d < now else (r.current_price if r.current_price is not None else prices.at(lot.isin, d))
            if p is None:
                p = lot.manual_current_price if d >= now - timedelta(days=1) and lot.manual_current_price else lot.price
            v = units * p
            value += v
            per_isin[lot.isin] += v
            invested += units * lot.price + (lot.fees * units / lot.units if lot.units else 0)
            if lot.source != "scalable" or cfg.credit.mode == "flags":
                drawn = sum(a for t, a in lot.credit_draws if t <= d)
                repaid = sum(a for t, a in lot.credit_repayments if t <= d)
                loan_manual += max(0.0, drawn - repaid)
        loan = loan_manual + (loan_auto if cfg.credit.mode == "auto" else 0.0)
        cash_pos = max(cash, 0.0) if events else 0.0
        portfolio_rows.append(
            {
                "date": _date(d),
                "value": round(value, 2),
                "invested": round(invested, 2),
                "cash": round(cash_pos, 2) if events else None,
                "loan": round(loan, 2),
                "net_worth": round(value + cash_pos - loan, 2),
                "net_deposits": round(deposits, 2) if events else None,
            }
        )
        for isin, v in per_isin.items():
            positions[isin].append([_date(d), round(v, 2)])

    # TWR (zeitgewichtet) – je Periode nach Modified Dietz, damit große Zuflüsse bei
    # dünnem Raster die Rendite nicht verzerren: r = (V1 − V0 − F) / (V0 + w·F)
    chart = store.chart()
    twr_points: list[tuple[datetime, float]] = []
    source = "reconstructed"
    if len(chart) >= 3:
        source = "scalable_chart"
        idx = 100.0
        prev_v = prev_i = None
        for ts, v, ar in chart:
            t = parse_time(ts)
            if t is None or v is None:
                continue
            inv = v - (ar or 0.0)
            if prev_v is not None:
                flow = inv - prev_i  # Zeitpunkt innerhalb der Periode unbekannt → w = 0,5
                denom = prev_v + 0.5 * flow
                if denom > 1e-9:
                    idx *= 1 + (v - prev_v - flow) / denom
            if v > 0 or twr_points:
                twr_points.append((t, idx))
            prev_v, prev_i = v, inv
    else:
        idx = 100.0
        trade_flows = sorted(
            (w, -a) for r in results for w, a, k in r.flows_pre if k in ("buy", "sell", "dividend")
        )  # + = Kapital fließt ins Depot
        fi = 0
        prev_value = None
        prev_d = None
        for row, d in zip(portfolio_rows, grid, strict=False):
            v = row["value"]
            flow = weighted = 0.0
            while fi < len(trade_flows) and trade_flows[fi][0] <= d:
                w, a = trade_flows[fi]
                if prev_d is not None and w > prev_d:
                    span = (d - prev_d).total_seconds() or 1.0
                    flow += a
                    weighted += a * (d - w).total_seconds() / span
                fi += 1
            if prev_value:
                denom = prev_value + weighted
                if denom > 1e-9:
                    idx *= 1 + (v - prev_value - flow) / denom
            if v > 0:
                twr_points.append((d, idx))
            prev_value, prev_d = (v if v > 0 else None), d
            if prev_value is None:
                prev_d = d

    bench = cfg.portfolio.benchmark_isin
    twr_rows = []
    b0 = None
    for t, i in twr_points:
        b = prices.at(bench, t) if bench else None
        if b is not None and b0 is None:
            b0 = (b, i)
        twr_rows.append(
            {
                "date": _date(t),
                "portfolio": round(i, 4),
                "benchmark": round(b0[1] * b / b0[0], 4) if (b is not None and b0) else None,
            }
        )
    peak = 0.0
    drawdown = []
    for row in twr_rows:
        peak = max(peak, row["portfolio"])
        drawdown.append({"date": row["date"], "drawdown": round(row["portfolio"] / peak - 1, 5) if peak else 0.0})

    # IRR-Verlauf (monatlich)
    irr_rows = []
    month_ends = date_grid(start, now, "monthly")
    for d in month_ends:
        pre: list[Flow] = []
        post: list[Flow] = []
        for r in results:
            if r.lot.open_date > d:
                continue
            pre += [f for f in r.flows_pre if f[0] <= d and f[2] != "valuation"]
            post += [f for f in r.flows_post if f[0] <= d and f[2] not in ("valuation", "latent_tax")]
            units = r.lot.units_at(d)
            if units > 0:
                p = prices.at(r.lot.isin, d) if d < now else r.current_price
                if p is None:
                    p = r.lot.price
                pre.append((d, units * p, "valuation"))
                post.append((d, units * p, "valuation"))
        irr_rows.append({"date": _date(d), "irr": _r(irr_of(pre), 5), "irr_after_tax_excl_latent": _r(irr_of(post), 5)})

    return {
        "resolution": cfg.portfolio.resolution,
        "source": source,
        "portfolio": portfolio_rows,
        "positions": dict(positions),
        "twr": twr_rows,
        "drawdown": drawdown,
        "irr": irr_rows,
    }


def build_periods(ts: dict[str, Any], events: list[LedgerEvent], now: datetime) -> dict[str, Any]:
    twr = ts.get("twr") or []

    def twr_between(a: str, b: str, key: str = "portfolio") -> float | None:
        pts = [r for r in twr if r[key] is not None]
        start = next((r for r in reversed(pts) if r["date"] <= a), pts[0] if pts else None)
        end = next((r for r in reversed(pts) if r["date"] <= b), None)
        if not start or not end or start[key] in (None, 0):
            return None
        return end[key] / start[key] - 1

    years = []
    if twr:
        first_year = int(twr[0]["date"][:4])
        for y in range(first_year, now.year + 1):
            a = f"{y - 1}-12-31" if y > first_year else twr[0]["date"]
            b = f"{y}-12-31"
            years.append(
                {
                    "year": y,
                    "twr": _r(twr_between(a, b), 5),
                    "benchmark": _r(twr_between(a, b, "benchmark"), 5),
                    "partial": y == now.year or y == first_year,
                }
            )
    today = _date(now)
    first = twr[0]["date"] if twr else None
    span_years = ((now - datetime.fromisoformat(first).replace(tzinfo=timezone.utc)).days / 365) if first else 0
    since = twr_between(first, today) if first else None
    summary = {
        "since_start": _r(since, 5),
        "annualized": _r((1 + since) ** (1 / span_years) - 1, 5) if (since is not None and span_years >= 1) else None,
        "ytd": _r(twr_between(f"{now.year - 1}-12-31", today), 5),
        "one_year": _r(twr_between(_date(now - timedelta(days=365)), today), 5),
        "benchmark_since_start": _r(twr_between(first, today, "benchmark"), 5) if first else None,
        "max_drawdown": min((r["drawdown"] for r in ts.get("drawdown") or []), default=None),
    }
    flows: dict[int, float] = defaultdict(float)
    for e in events:
        if e.external:
            flows[e.when.year] += e.cash
    for row in years:
        row["net_deposits"] = _r(flows.get(row["year"], 0.0))
    return {"years": years, "twr_summary": summary}
