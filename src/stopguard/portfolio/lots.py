"""Trades (Lots) aus Käufen/Verkäufen bilden – FIFO wie bei der Girosammelverwahrung (§ 20 Abs. 4 S. 7 EStG)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from stopguard.config import Config
from stopguard.models import parse_time
from stopguard.portfolio.model import Dividend, Instrument, LedgerEvent, Lot, LotExit

EPS = 1e-9


@dataclass
class LotsResult:
    lots: list[Lot]
    exit_event_lot: dict[str, list[tuple[str, float]]] = field(default_factory=dict)  # Verkauf → [(lot_id, Stück)]
    warnings: list[str] = field(default_factory=list)
    unmatched_dividends: list[LedgerEvent] = field(default_factory=list)


def guess_kind(security_type: str | None, name: str | None) -> str:
    st = (security_type or "").upper()
    nm = (name or "").lower()
    if st in ("ETF", "FUND", "ETP", "MUTUAL_FUND") or "etf" in nm or "ucits" in nm or "msci" in nm or "ftse" in nm:
        if any(w in nm for w in ("bond", "anleihe", "treasury", "govt", "aggregate", "corp")):
            return "bond_fund"
        if any(w in nm for w in ("real estate", "immobil", "reit")):
            return "real_estate_fund"
        return "equity_fund"
    if st in ("ETC", "CRYPTO_ETP", "BOND", "CERTIFICATE", "WARRANT", "KNOCKOUT"):
        return "other"
    return "stock"


def build_instruments(cfg: Config, holdings: list[dict[str, Any]], names: dict[str, str]) -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    types = {h["isin"]: h.get("security_type") for h in holdings}
    for isin in set(names) | set(types) | set(cfg.instruments):
        ic = cfg.instruments.get(isin)
        name = (ic.name if ic and ic.name else None) or names.get(isin) or isin
        kind = (ic.kind if ic and ic.kind else None) or guess_kind(types.get(isin), name)
        distributing = ic.distributing if ic else None
        if distributing is None and name:
            low = name.lower()
            if "dist" in low or "aussch" in low:
                distributing = True
            elif "acc" in low or "thes" in low:
                distributing = False
        out[isin] = Instrument(
            isin=isin,
            name=name,
            kind=kind,
            ticker=ic.ticker if ic else None,
            distributing=distributing,
            tags=list(ic.tags) if ic else [],
            security_type=types.get(isin),
            benchmark=ic.benchmark if ic else None,
        )
    return out


def build_lots(events: list[LedgerEvent], *, start_ids: int = 0) -> LotsResult:
    res = LotsResult(lots=[])
    queues: dict[str, list[Lot]] = defaultdict(list)
    all_by_isin: dict[str, list[Lot]] = defaultdict(list)
    for ev in events:
        if not ev.isin:
            continue
        if ev.kind in ("buy", "transfer_in"):
            price = ev.price if ev.price is not None else 0.0
            lot = Lot(
                id=f"L-{ev.id}",
                isin=ev.isin,
                name=ev.isin,
                open_date=ev.when,
                units=ev.units,
                price=price,
                fees=ev.fees,
                source="scalable",
                origin="transfer" if ev.kind == "transfer_in" else ("savings_plan" if (ev.raw_type or "").upper() == "SAVINGS_PLAN" else "single"),
                open_event_id=ev.id,
            )
            if ev.kind == "transfer_in":
                lot.notes.append("Depotübertrag – Einstandskurs ggf. per [instruments]/Import ergänzen")
                if ev.price is None:
                    lot.notes.append("Kein Einstandskurs bekannt")
            queues[ev.isin].append(lot)
            all_by_isin[ev.isin].append(lot)
            res.lots.append(lot)
        elif ev.kind in ("sell", "transfer_out"):
            remaining = ev.units
            parts: list[tuple[str, float]] = []
            queue = queues[ev.isin]
            while remaining > EPS and queue:
                lot = queue[0]
                take = min(lot.remaining_units, remaining)
                share = take / ev.units if ev.units else 0.0
                price = ev.price if ev.price is not None else lot.price
                lot.exits.append(
                    LotExit(ev.when, take, price, ev.fees * share, (ev.tax * share) if ev.kind == "sell" else None, ev.id)
                )
                parts.append((lot.id, take))
                remaining -= take
                if lot.remaining_units <= EPS:
                    queue.pop(0)
            if remaining > 1e-6:
                res.warnings.append(
                    f"{ev.isin}: Verkauf von {ev.units:g} Stk. am {ev.when:%d.%m.%Y} übersteigt bekannte Käufe um {remaining:g} Stk. "
                    "(Historie unvollständig?)"
                )
            res.exit_event_lot[ev.id] = parts
        elif ev.kind == "dividend":
            holders = [(lot, lot.units_at(ev.when)) for lot in all_by_isin.get(ev.isin, [])]
            holders = [(lot, u) for lot, u in holders if u > EPS]
            total = sum(u for _, u in holders)
            if not holders:
                res.unmatched_dividends.append(ev)
                continue
            for lot, u in holders:
                share = u / total
                lot.dividends.append(
                    Dividend(
                        ev.when,
                        ev.cash * share,
                        ev.gross * share if ev.gross is not None else None,
                        ev.tax * share if ev.tax else None,
                    )
                )
    return res


# ---------------------------------------------------------------------------
# Importierte Trades (Notion/CSV)
# ---------------------------------------------------------------------------

def manual_to_lots(rows: list[dict[str, Any]], cfg: Config, now: datetime) -> tuple[list[Lot], list[str]]:
    lots: list[Lot] = []
    warnings: list[str] = []
    ticker_to_isin = {ic.ticker.upper(): isin for isin, ic in cfg.instruments.items() if ic.ticker}
    for r in rows:
        isin = r.get("isin") or ticker_to_isin.get(str(r.get("ticker") or "").upper())
        key = isin or f"TICKER:{r.get('ticker') or r.get('name')}"
        open_date = parse_time(r.get("open_date"))
        if open_date is None or not r.get("units") or r.get("open_price") is None:
            warnings.append(f"Import-Trade {r.get('name')} übersprungen (Datum/Stückzahl/Kurs fehlt)")
            continue
        if not isin:
            warnings.append(f"Kein ISIN-Mapping für Ticker {r.get('ticker')} – in [instruments] ergänzen (ticker = \"…\")")
        lot = Lot(
            id=f"M-{r['id']}",
            isin=key,
            name=r.get("name") or key,
            open_date=open_date,
            units=float(r["units"]),
            price=float(r["open_price"]),
            fees=float(r.get("fees") or 0.0),
            source=r.get("source") or "csv",
            origin="import",
            flagged_credit=bool(r.get("credit_flag")),
            credit_amount=r.get("credit_amount"),
            manual_current_price=r.get("current_price"),
            ticker=r.get("ticker"),
        )
        close_date = parse_time(r.get("close_date"))
        if close_date is not None and r.get("close_price") is not None:
            lot.exits.append(LotExit(close_date, lot.units, float(r["close_price"]), 0.0, None, f"close-{r['id']}"))
        div = float(r.get("dividends") or 0.0)
        if div:
            end = close_date or now
            mid = open_date + (end - open_date) / 2
            gross = None
            if cfg.portfolio.notion_dividends == "gross":
                gross = div
            lot.dividends.append(Dividend(mid, div, gross, None, approximate=True))
            lot.notes.append("Dividenden ohne Datum importiert – zur Mitte der Haltedauer angesetzt")
        lots.append(lot)
    return lots, warnings


def dedupe_manual(manual: list[Lot], scalable: list[Lot], days: int) -> tuple[list[Lot], list[str]]:
    """Importierte Trades, die bereits aus Scalable bekannt sind, werden verworfen.
    Eine Kredit-Markierung aus dem Import wird auf den Scalable-Trade übertragen."""
    kept: list[Lot] = []
    notes: list[str] = []
    window = timedelta(days=days)
    by_isin: dict[str, list[Lot]] = defaultdict(list)
    for lot in scalable:
        by_isin[lot.isin].append(lot)
    used: set[str] = set()
    for m in manual:
        match = next(
            (
                s
                for s in by_isin.get(m.isin, [])
                if s.id not in used and abs(s.open_date - m.open_date) <= window and abs(s.units - m.units) <= max(1e-6, m.units * 0.01)
            ),
            None,
        )
        if match is None:
            kept.append(m)
            continue
        used.add(match.id)
        if m.flagged_credit:
            match.flagged_credit = True
            match.credit_amount = m.credit_amount
        notes.append(f"Import-Trade {m.name} ({m.open_date:%d.%m.%Y}) ist bereits in Scalable enthalten")
    return kept, notes


def apply_credit_flags(lots: list[Lot], cfg: Config) -> None:
    for flag in cfg.credit_flags:
        for lot in lots:
            if lot.isin == flag.isin and abs((lot.open_date - flag.open_date).days) <= cfg.portfolio.dedupe_days:
                lot.flagged_credit = True
                lot.credit_amount = flag.amount


def utc_date(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)
