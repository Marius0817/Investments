from datetime import datetime, timedelta, timezone

from stopguard.state import State

APPLE, MSCI, NVDA, SAP, ALV, VWRL = (
    "US0378331005",
    "IE00B4L5Y983",
    "US67066G1040",
    "DE0007164600",
    "DE0008404005",
    "IE00BK5BQT80",
)


def kinds(report):
    return {(a.isin, a.kind) for a in report.actions}


def test_demo_plan_covers_all_three_problems(demo):
    state = State()
    r = demo.analyse(state)
    assert kinds(r) == {
        (APPLE, "replace"),  # Problem 2: veraltete Order → Trailing
        (MSCI, "place"),  # Problem 1: IRR-Untergrenze
        (NVDA, "place"),  # Problem 3, Phase 1: −20 %
        (SAP, "place"),  # Problem 3, Phase 2: Trailing 5 %
        (ALV, "alert_set"),
    }
    by = {a.isin: a for a in r.analyses}
    assert by[NVDA].phase.name == "Aufbau"
    assert str(by[NVDA].target.stop) == "104.15"
    assert by[SAP].phase.name == "Gewinne sichern"
    assert by[SAP].target.order_type == "trailing-stop"
    assert by[VWRL].cfg.execution == "watch"
    apple = next(a for a in r.actions if a.isin == APPLE)
    assert apple.cancel_order_id == "order-legacy-1"
    assert apple.spec.order_type == "trailing-stop"
    # bestehende Order wurde übernommen
    assert state.positions[APPLE].managed_order_id == "order-legacy-1"


def test_hysteresis_and_trailing_update(demo):
    state = State()
    demo.analyse(state)
    # Stop-Order für MSCI (Kombination Trailing 10 % + IRR-Untergrenze → klassischer Stop)
    demo.edit_world(
        lambda w: w["orders"].update(
            {
                "o-msci": {
                    "isin": MSCI, "side": "SELL", "status": "PENDING", "order_type": "stop",
                    "shares": w["holdings"][MSCI]["quantity"], "stop_price": "93.78",
                    "created": "2026-01-01T00:00:00Z", "valid_until": "2030-01-01T00:00:00Z",
                }
            }
        )
    )
    demo.move(MSCI, 0.5)
    r = demo.analyse(state, isins={MSCI})
    assert r.actions == []  # +0,5 % → unter der Hysterese von 1 %
    demo.move(MSCI, 3)
    r = demo.analyse(state, isins={MSCI})
    assert len(r.actions) == 1 and r.actions[0].kind == "replace"
    assert "nachziehen" in r.actions[0].reason
    # Kurs fällt wieder → Stop wird nie gesenkt
    demo.move(MSCI, -2)
    r = demo.analyse(state, isins={MSCI})
    assert r.actions and r.actions[0].spec.stop_price > 93.78


def test_breach_proposes_sell(demo):
    state = State()
    demo.analyse(state)
    demo.move(NVDA, -15)  # 119.60 → 101.66 < Soll-Stop 104.15, keine Order im Markt
    r = demo.analyse(state, isins={NVDA})
    assert r.actions[0].kind == "sell_now" and r.actions[0].urgent
    assert r.actions[0].spec.order_type == "market"
    assert any(e.level == "urgent" for e in r.events)


def test_quantity_change_and_expiry_trigger_replace(demo):
    state = State()
    demo.edit_world(
        lambda w: w["orders"].update(
            {
                "o-nvda": {
                    "isin": NVDA, "side": "SELL", "status": "PENDING", "order_type": "stop",
                    "shares": "5", "stop_price": "104.15", "created": "2026-01-01T00:00:00Z",
                    "valid_until": (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            }
        )
    )
    r = demo.analyse(state, isins={NVDA})
    assert r.actions[0].kind == "replace" and "läuft am" in r.actions[0].reason
    demo.edit_world(lambda w: w["holdings"][NVDA].update({"quantity": "7"}))
    r = demo.analyse(state, isins={NVDA})
    assert "Stückzahl 5 → 7" in r.actions[0].reason


def test_unresolved_submission_blocks_planning(demo):
    state = State()
    state.position(NVDA).unresolved_submission = {"at": "x", "spec": {}, "confirmation_id": "c"}
    r = demo.analyse(state, isins={NVDA})
    assert r.actions == []
    assert any("resolve" in i for i in r.analyses[0].issues)


def test_other_sell_order_prevents_automation(demo):
    state = State()
    demo.edit_world(
        lambda w: w["orders"].update(
            {
                "o-tp": {
                    "isin": NVDA, "side": "SELL", "status": "PENDING", "order_type": "limit",
                    "shares": "5", "limit_price": "160", "created": "2026-01-01T00:00:00Z",
                }
            }
        )
    )
    r = demo.analyse(state, isins={NVDA})
    assert r.actions == []
    assert any("Weitere Verkaufsorder" in i for i in r.analyses[0].issues)


def test_filled_stop_is_reported(demo):
    state = State()
    demo.analyse(state)
    # Apple: alte Stop-Order (171) wird ausgelöst
    demo.move(APPLE, -25)
    r = demo.analyse(state)
    assert any("ausgeführt" in e.title for e in r.events)
    assert APPLE not in {a.isin for a in r.analyses}  # Position ist verkauft


def test_strategy_change_resets_ratchet(demo):
    state = State()
    demo.analyse(state, isins={NVDA})
    assert state.positions[NVDA].last_target_stop == "104.15"
    demo.cfg.positions[NVDA].strategy = "irr_gate_open"  # ohne Verlustgrenze in Phase 1
    r = demo.analyse(state, isins={NVDA})
    assert r.analyses[0].target.stop is None
    assert state.positions[NVDA].last_target_stop is None
    assert r.actions == []


def test_phase_change_event_only_after_first_run(demo):
    state = State()
    r = demo.analyse(state)
    assert not [e for e in r.events if "neue Phase" in e.title]  # Erstbewertung meldet nichts
    state.positions[SAP].phase_index = 0  # SAP war vorher in „Aufbau“
    state.positions[SAP].phase_name = "Aufbau"
    r = demo.analyse(state, isins={SAP})
    assert [e for e in r.events if "neue Phase" in e.title]
