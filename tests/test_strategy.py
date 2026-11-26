from datetime import datetime, timezone
from decimal import Decimal

from stopguard.config import BUILTIN_STRATEGIES, parse_phase, parse_strategy
from stopguard.strategy import Metrics, advance_phase, build_target, compute_components

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
IRR_GATE = parse_strategy("irr_gate", BUILTIN_STRATEGIES["irr_gate"])


def m(irr=None, days=0.0, price=100.0, cost=100.0):
    return Metrics(price=price, cost_price=cost, irr=irr, total_return=None, holding_days=days)


def test_gate_waits_for_min_holding_days():
    # IRR ist nach wenigen Tagen schnell hoch (annualisiert) – Mindesthaltedauer schützt
    assert advance_phase(IRR_GATE, 0, m(irr=0.40, days=10)).index == 0
    d = advance_phase(IRR_GATE, 0, m(irr=0.051, days=31))
    assert d.index == 1 and d.changed
    assert "IRR 5,1 % p. a." in d.reason


def test_gate_is_sticky_once_reached():
    # IRR fällt später wieder unter 5 % → Phase bleibt
    d = advance_phase(IRR_GATE, 1, m(irr=0.01, days=200))
    assert d.index == 1 and not d.changed


def test_non_sticky_phase_falls_back():
    st = parse_strategy(
        "x",
        {"phases": [{"name": "a", "max_loss_pct": 20}, {"name": "b", "activate": {"gain_pct": 10}, "sticky": False, "trailing_pct": 5}]},
    )
    assert advance_phase(st, 0, m(price=111, cost=100)).index == 1
    assert advance_phase(st, 1, m(price=105, cost=100)).index == 0


def test_can_jump_directly_to_later_phase():
    st = parse_strategy(
        "x",
        {
            "phases": [
                {"name": "a"},
                {"name": "b", "activate": {"gain_pct": 5}, "trailing_pct": 10},
                {"name": "c", "activate": {"gain_pct": 20}, "trailing_pct": 5},
            ]
        },
    )
    assert advance_phase(st, 0, m(price=125, cost=100)).index == 2


def _target(phase, *, price, cost=100.0, hwm=None, previous=None, venue="gettex", only_raise=True):
    comps = compute_components(phase, cost_price=cost, high_water_mark=hwm, flows=[], shares=1, at=NOW)
    return build_target(
        phase, comps, price=price, venue=venue, decimals=2, min_distance_pct=1.0, previous_target=previous, only_raise=only_raise
    )


def test_max_of_components_and_order_type():
    ph = parse_phase({"name": "p", "max_loss_pct": 20, "trailing_pct": 5}, "t", 0)
    t = _target(ph, price=120, hwm=120)
    assert t.stop == Decimal("114.00")  # Trailing (114) > max_loss (80)
    assert t.order_type == "stop"  # Kombination → kein natives Trailing


def test_trailing_only_uses_native_trailing_on_gettex_but_not_xetra():
    ph = parse_phase({"name": "p", "trailing_pct": 5}, "t", 0)
    t = _target(ph, price=200, hwm=200)
    assert t.order_type == "trailing-stop" and t.trail_percent == Decimal("5.0")
    t2 = _target(ph, price=200, hwm=200, venue="xetra")
    assert t2.order_type == "stop" and t2.stop == Decimal("190.00")


def test_ratchet_holds_previous_level():
    ph = parse_phase({"name": "p", "max_loss_pct": 10}, "t", 0)
    t = _target(ph, price=100, cost=90, previous=Decimal("95"))
    assert t.stop == Decimal("95") and t.held_by_ratchet
    t2 = _target(ph, price=100, cost=90, previous=Decimal("95"), only_raise=False)
    assert t2.stop == Decimal("81.00")


def test_clamped_to_min_distance():
    ph = parse_phase({"name": "p", "lock_gain_pct": 0}, "t", 0)
    t = _target(ph, price=100.5, cost=100)
    assert t.clamped and t.stop == Decimal("99.49")


def test_stop_limit_limit_price():
    ph = parse_phase({"name": "p", "max_loss_pct": 10, "order_type": "stop-limit", "limit_offset_pct": 2}, "t", 0)
    t = _target(ph, price=100, cost=100)
    assert t.stop == Decimal("90.00") and t.limit_price == Decimal("88.20")


def test_no_components_means_no_stop():
    ph = parse_phase({"name": "p"}, "t", 0)
    assert _target(ph, price=100).stop is None
