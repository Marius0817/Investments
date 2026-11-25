"""Einfacher Backtest einer Strategie auf historischen Kerzen (sc broker candlesticks).

Annahmen: Einmalkauf zum Schlusskurs am Starttag, tägliche Neuberechnung des Stops
zum Tagesschluss, Auslösung, wenn das Tagestief den Stop des Vortags erreicht
(bei Kurslücke zum Eröffnungskurs). Gebühren/Steuern bleiben unberücksichtigt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from stopguard.config import Strategy
from stopguard.finance import CashFlow, xirr
from stopguard.models import parse_time, to_decimal
from stopguard.strategy import Metrics, advance_phase, build_target, compute_components


@dataclass
class Candle:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float


def parse_candles(raw: list[dict]) -> list[Candle]:
    out = []
    for c in raw:
        end = parse_time(c.get("end_time_utc"))
        start = parse_time(c.get("start_time_utc")) or end
        vals = [to_decimal(c.get(k)) for k in ("open", "high", "low", "close")]
        if end is None or any(v is None for v in vals):
            continue
        o, h, lo, cl = (float(v) for v in vals)  # type: ignore[arg-type]
        out.append(Candle(start, end, o, h, lo, cl))  # type: ignore[arg-type]
    out.sort(key=lambda c: c.end)
    return out


@dataclass
class BacktestResult:
    strategy: str
    entry_at: datetime
    entry_price: float
    exit_at: datetime | None
    exit_price: float
    exit_reason: str
    total_return: float
    irr: float | None
    buy_hold_return: float
    max_drawdown: float
    timeline: list[tuple[datetime, str]] = field(default_factory=list)


def run_backtest(
    strategy: Strategy,
    candles: list[Candle],
    *,
    entry_index: int = 0,
    venue: str = "gettex",
    decimals: int = 2,
    only_raise: bool = True,
) -> BacktestResult:
    if len(candles) < entry_index + 2:
        raise ValueError("Zu wenige Kerzen für einen Backtest")
    entry = candles[entry_index]
    entry_price = entry.close
    flows = [CashFlow(entry.end, -entry_price, 1.0, "buy")]
    phase_idx = 0
    hwm = entry_price
    last_target: Decimal | None = None
    stop: float | None = None
    timeline: list[tuple[datetime, str]] = [(entry.end, f"Kauf zu {entry_price:.2f} – Phase „{strategy.phases[0].name}“")]
    peak = entry_price
    max_dd = 0.0

    def compute_stop(at: datetime, price: float) -> float | None:
        nonlocal last_target
        phase = strategy.phases[phase_idx]
        comps = compute_components(
            phase, cost_price=entry_price, high_water_mark=hwm, flows=flows, shares=1.0, at=at
        )
        target = build_target(
            phase,
            comps,
            price=price,
            venue=venue,
            decimals=decimals,
            min_distance_pct=0.0,
            previous_target=last_target if only_raise else None,
            only_raise=only_raise,
        )
        if target.stop is None:
            return None
        if last_target is None or target.stop > last_target:
            last_target = target.stop
        return float(target.stop)

    stop = compute_stop(entry.end, entry_price)
    if stop is not None:
        timeline.append((entry.end, f"Erster Stop {stop:.2f}"))

    for c in candles[entry_index + 1 :]:
        # 1) Auslösung im Tagesverlauf
        if stop is not None and c.low <= stop:
            exit_price = c.open if c.open < stop else stop
            timeline.append((c.end, f"Stop {stop:.2f} ausgelöst → Verkauf zu {exit_price:.2f}"))
            return _result(strategy, entry, entry_price, c.end, exit_price, "Stop ausgelöst", flows, candles, max_dd, timeline)

        peak = max(peak, c.high)
        max_dd = max(max_dd, 1 - c.low / peak)

        # 2) Tagesschluss: Kennzahlen, Phase, Hochpunkt, neuer Stop
        days = (c.end - entry.end).total_seconds() / 86400
        irr = xirr([(entry.end, -entry_price), (c.end, c.close)])
        m = Metrics(price=c.close, cost_price=entry_price, irr=irr, total_return=c.close / entry_price - 1, holding_days=days)
        decision = advance_phase(strategy, phase_idx, m)
        if decision.index != phase_idx:
            phase_idx = decision.index
            hwm = c.close
            timeline.append((c.end, f"Phase „{strategy.phases[phase_idx].name}“ ({decision.reason}) bei {c.close:.2f}"))
        else:
            hwm = max(hwm, c.high)
        new_stop = compute_stop(c.end, c.close)
        if new_stop is not None and (stop is None or abs(new_stop - stop) / max(stop, 1e-9) >= 0.0001):
            if stop is None:
                timeline.append((c.end, f"Stop gesetzt {new_stop:.2f}"))
            stop = new_stop

    last = candles[-1]
    return _result(strategy, entry, entry_price, None, last.close, "Ende des Zeitraums (noch investiert)", flows, candles, max_dd, timeline)


def _result(strategy, entry, entry_price, exit_at, exit_price, reason, flows, candles, max_dd, timeline) -> BacktestResult:
    end_time = exit_at or candles[-1].end
    irr = xirr([(f.when, f.amount) for f in flows] + [(end_time, exit_price)])
    if exit_at is None:
        timeline.append((end_time, f"Ende: Kurs {exit_price:.2f}"))
    return BacktestResult(
        strategy=strategy.name,
        entry_at=entry.end,
        entry_price=entry_price,
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=reason,
        total_return=exit_price / entry_price - 1,
        irr=irr,
        buy_hold_return=candles[-1].close / entry_price - 1,
        max_drawdown=max_dd,
        timeline=timeline,
    )
