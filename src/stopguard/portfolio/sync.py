"""Abgleich mit Scalable (nur Lesezugriffe über die offizielle CLI)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from stopguard.config import Config
from stopguard.models import parse_time, utcnow
from stopguard.portfolio.store import PortfolioStore
from stopguard.sc import ScClient, ScError

log = logging.getLogger(__name__)

DETAIL_CASH_TYPES = ("DISTRIBUTION", "DIVIDEND", "REINVESTMENT_DISTRIBUTION", "TAX", "TAX_RETURN", "INTEREST")
RESYNC_WINDOW = timedelta(days=10)


@dataclass
class SyncReport:
    transactions: int = 0
    details: int = 0
    details_pending: int = 0
    prices: dict[str, int] = field(default_factory=dict)
    chart_points: int = 0
    holdings: int = 0
    warnings: list[str] = field(default_factory=list)


def sync_portfolio(
    client: ScClient,
    store: PortfolioStore,
    cfg: Config,
    *,
    details: bool | None = None,
    prices: bool = True,
    max_details: int | None = None,
    progress: Callable[[str], None] = lambda s: None,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncReport:
    rep = SyncReport()
    now = utcnow()

    # 1) Transaktionen – beim ersten Mal komplett, danach inkrementell mit Überlappung
    last = parse_time(store.get_meta("transactions_synced_at"))
    from_time = (last - RESYNC_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None
    progress("Transaktionen " + ("seit " + from_time[:10] if from_time else "(vollständige Historie)") + " …")
    txs = client.transactions(from_time=from_time, max_pages=500)
    rep.transactions = store.upsert_transactions(t.raw for t in txs)
    store.set_meta("transactions_synced_at", now.isoformat())

    # 2) Details (Gebühren, Steuern, Dividenden brutto) – einmalig je Transaktion
    if details if details is not None else cfg.portfolio.fetch_details:
        missing = store.transactions_missing_details(DETAIL_CASH_TYPES)
        todo = missing[:max_details] if max_details else missing
        if todo:
            progress(f"Details für {len(todo)} Transaktionen …")
        for i, tid in enumerate(todo):
            try:
                store.set_details(tid, client.transaction_details(tid))
                rep.details += 1
            except ScError as exc:
                if exc.is_auth_problem or exc.is_device_locked:
                    raise
                if exc.is_rate_limited:
                    rep.warnings.append("Rate-Limit bei Detailabfragen – Rest folgt beim nächsten Sync")
                    break
                rep.warnings.append(f"Details {tid}: {exc.friendly()}")
            if cfg.portfolio.details_delay_seconds and i + 1 < len(todo):
                sleep(cfg.portfolio.details_delay_seconds)
        rep.details_pending = len(missing) - rep.details

    # 3) Bestand + Kontostand (inkl. Kredit) → Momentaufnahme des Tages
    holdings, _ = client.holdings()
    rep.holdings = len(holdings)
    store.replace_holdings(
        {
            "isin": h.isin,
            "name": h.name,
            "quantity": float(h.quantity),
            "fifo_price": float(h.fifo_price) if h.fifo_price is not None else None,
            "mid_price": float(h.mid_price) if h.mid_price is not None else None,
            "security_type": h.security_type,
        }
        for h in holdings
    )
    account = None
    try:
        account = client.cash_breakdown()
    except ScError as exc:
        if exc.is_auth_problem:
            raise
        rep.warnings.append(f"cash-breakdown nicht verfügbar: {exc.friendly()}")
    sec_value = sum(float(h.valuation) for h in holdings if h.valuation is not None)
    if account is not None:
        account = {**account, "securities_value": sec_value}
    store.snapshot(
        now.date().isoformat(),
        [
            {
                "isin": h.isin,
                "units": float(h.quantity),
                "price": float(h.mid_price) if h.mid_price is not None else None,
                "value": float(h.valuation) if h.valuation is not None else None,
            }
            for h in holdings
        ],
        account,
    )

    # 4) Depotverlauf laut Scalable
    try:
        rep.chart_points = store.upsert_chart(client.portfolio_chart("max"))
    except ScError as exc:
        if exc.is_auth_problem:
            raise
        rep.warnings.append(f"Depotverlauf nicht verfügbar: {exc.friendly()}")

    # 5) Kurshistorie aller gehandelten Papiere + Benchmark
    if prices:
        isins = {t.isin for t in txs if t.isin} | {h.isin for h in holdings}
        isins |= {r["isin"] for r in store.conn.execute("SELECT DISTINCT isin FROM transactions WHERE isin IS NOT NULL")}
        isins |= {ic.benchmark for ic in cfg.instruments.values() if ic.benchmark}
        if cfg.portfolio.benchmark_isin:
            isins.add(cfg.portfolio.benchmark_isin)
        for isin in sorted(i for i in isins if i):
            first, latest = store.price_range(isin)
            timeframes = [cfg.portfolio.price_history, "one_year"] if first is None else ["one_month"]
            if latest and parse_time(latest) and now - parse_time(latest) > timedelta(days=25):
                timeframes = ["one_year"]
            count = 0
            for tf in timeframes:
                try:
                    candles = client.candlesticks(isin, tf)
                except ScError as exc:
                    if exc.is_auth_problem:
                        raise
                    rep.warnings.append(f"Kurse {isin} ({tf}): {exc.friendly()}")
                    continue
                points = []
                for c in candles:
                    t = parse_time(c.get("end_time_utc") or c.get("start_time_utc"))
                    try:
                        close = float(c.get("close"))
                    except (TypeError, ValueError):
                        continue
                    if t is not None:
                        points.append((t.date().isoformat(), close))
                count += store.upsert_prices(isin, points)
            rep.prices[isin] = count
    store.set_meta("last_sync_at", now.isoformat())
    return rep
