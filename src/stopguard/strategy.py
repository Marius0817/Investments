"""Reine Strategie-Logik: Phasenwechsel und Stop-Berechnung.

Wird sowohl im Live-Betrieb (engine.py) als auch im Backtest verwendet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from stopguard.config import Condition, Phase, Strategy
from stopguard.finance import CashFlow, floor_price_for_irr
from stopguard.models import fmt_decimal, round_down


def _de(value: float, digits: int = 1) -> str:
    """Zahl mit deutschem Dezimalkomma (für Begründungstexte)."""
    return f"{value:.{digits}f}".replace(".", ",")


@dataclass
class Metrics:
    price: float | None
    cost_price: float | None  # Einstandskurs je Stück
    irr: float | None  # p.a.
    total_return: float | None
    holding_days: float

    @property
    def gain(self) -> float | None:
        if self.price is None or not self.cost_price:
            return None
        return self.price / self.cost_price - 1


def condition_met(cond: Condition, m: Metrics) -> tuple[bool, str]:
    """Prüft alle gesetzten Kriterien (UND-Verknüpfung). Liefert (erfüllt, Begründung)."""
    reasons: list[str] = []
    if cond.irr_pct is not None:
        if m.irr is None or m.irr * 100 < cond.irr_pct:
            return False, ""
        reasons.append(f"IRR {_de(m.irr * 100)} % p. a.")
    if cond.gain_pct is not None:
        if m.gain is None or m.gain * 100 < cond.gain_pct:
            return False, ""
        reasons.append(f"Kursgewinn {_de(m.gain * 100)} %")
    if cond.total_return_pct is not None:
        if m.total_return is None or m.total_return * 100 < cond.total_return_pct:
            return False, ""
        reasons.append(f"Gesamtrendite {_de(m.total_return * 100)} %")
    if cond.min_holding_days is not None:
        if m.holding_days < cond.min_holding_days:
            return False, ""
        reasons.append(f"{m.holding_days:.0f} Tage gehalten")
    if cond.price_above is not None:
        if m.price is None or m.price < cond.price_above:
            return False, ""
        reasons.append(f"Kurs {_de(m.price, 2)}")
    return True, ", ".join(reasons)


@dataclass
class PhaseDecision:
    index: int
    changed: bool
    reason: str | None


def advance_phase(strategy: Strategy, current: int, m: Metrics) -> PhaseDecision:
    """Ermittelt die gültige Phase.

    - Phasen sind standardmäßig *sticky*: einmal erreicht (z. B. „erstmals 5 % IRR“)
      bleibt die Phase aktiv, auch wenn die IRR später wieder fällt.
    - Es kann auch direkt in eine spätere Phase gesprungen werden.
    - Nicht-sticky Phasen fallen zurück, sobald ihre Bedingung nicht mehr erfüllt ist.
    """
    phases = strategy.phases
    current = max(0, min(current, len(phases) - 1))
    # Rückfall nicht-sticky Phasen
    while current > 0 and not phases[current].sticky and not condition_met(phases[current].activate, m)[0]:
        current -= 1
    for idx in range(len(phases) - 1, current, -1):
        ok, why = condition_met(phases[idx].activate, m)
        if ok:
            return PhaseDecision(idx, True, why)
    return PhaseDecision(current, False, None)


@dataclass
class StopComponent:
    key: str
    price: float
    label: str


@dataclass
class StopTarget:
    stop: Decimal | None
    components: list[StopComponent] = field(default_factory=list)
    order_type: str | None = None  # stop | stop-limit | trailing-stop
    trail_percent: Decimal | None = None
    limit_price: Decimal | None = None
    held_by_ratchet: bool = False
    clamped: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def binding(self) -> StopComponent | None:
        return max(self.components, key=lambda c: c.price) if self.components else None


def compute_components(
    phase: Phase,
    *,
    cost_price: float | None,
    high_water_mark: float | None,
    flows: list[CashFlow],
    shares: float,
    at: datetime,
    exit_costs: float = 0.0,
) -> list[StopComponent]:
    comps: list[StopComponent] = []
    if phase.max_loss_pct is not None and cost_price:
        comps.append(
            StopComponent("max_loss", cost_price * (1 - phase.max_loss_pct / 100), f"Einstand −{phase.max_loss_pct:g}%")
        )
    if phase.trailing_pct is not None and high_water_mark:
        comps.append(
            StopComponent(
                "trailing", high_water_mark * (1 - phase.trailing_pct / 100), f"Hoch {_de(high_water_mark, 2)} −{phase.trailing_pct:g} %"
            )
        )
    if phase.irr_floor_pct is not None and flows:
        floor = floor_price_for_irr(flows, shares, phase.irr_floor_pct / 100, at, exit_costs)
        if floor is not None:
            comps.append(StopComponent("irr_floor", floor, f"IRR-Untergrenze {phase.irr_floor_pct:g}% p.a."))
    if phase.lock_gain_pct is not None and cost_price:
        comps.append(
            StopComponent("lock_gain", cost_price * (1 + phase.lock_gain_pct / 100), f"Einstand +{phase.lock_gain_pct:g}%")
        )
    if phase.stop_price is not None:
        comps.append(StopComponent("fixed", phase.stop_price, "fester Stop"))
    return comps


def resolve_order_type(phase: Phase, venue: str) -> tuple[str, str | None]:
    """'auto' → natives Trailing, wenn möglich (nur Trailing-Komponente, Handelsplatz gettex/EIX)."""
    if phase.order_type == "auto":
        if phase.trailing_only() and venue in ("gettex", "eix"):
            return "trailing-stop", None
        return "stop", None
    if phase.order_type == "trailing-stop" and venue not in ("gettex", "eix"):
        return "stop", f"Natives Trailing nur auf gettex/EIX möglich (Handelsplatz {venue}) – nutze Stop mit Nachziehen"
    return phase.order_type, None


def build_target(
    phase: Phase,
    components: list[StopComponent],
    *,
    price: float | None,
    venue: str,
    decimals: int,
    min_distance_pct: float,
    previous_target: Decimal | None,
    only_raise: bool,
) -> StopTarget:
    if not components:
        return StopTarget(stop=None, components=[], notes=["Phase ohne Stop – Verluste werden hingenommen"])
    order_type, note = resolve_order_type(phase, venue)
    raw = max(c.price for c in components)
    stop = round_down(Decimal(str(raw)), decimals)
    target = StopTarget(stop=stop, components=components, order_type=order_type)
    if note:
        target.notes.append(note)
    if only_raise and previous_target is not None and stop < previous_target:
        target.stop = previous_target
        target.held_by_ratchet = True
        target.notes.append(f"Soll-Stop würde sinken – bleibt bei {fmt_decimal(previous_target)} (only_raise)")
    if order_type == "trailing-stop":
        target.trail_percent = Decimal(str(phase.trailing_pct))
    if price is not None and target.stop is not None and order_type != "trailing-stop":
        max_allowed = round_down(Decimal(str(price * (1 - min_distance_pct / 100))), decimals)
        if max_allowed < target.stop < Decimal(str(price)):
            target.stop = max_allowed
            target.clamped = True
            target.notes.append(f"Stop auf {fmt_decimal(max_allowed)} begrenzt (Mindestabstand {min_distance_pct:g}% zum Kurs)")
    if order_type == "stop-limit" and target.stop is not None:
        target.limit_price = round_down(target.stop * (1 - Decimal(str(phase.limit_offset_pct)) / 100), decimals)
    return target
