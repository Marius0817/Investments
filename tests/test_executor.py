import time

from stopguard.engine import apply_alert_actions
from stopguard.executor import Executor, resolve_unresolved
from stopguard.state import State

from test_engine import ALV, APPLE, NVDA, SAP


class Asker:
    """Simuliert die Antworten im Terminal und protokolliert die Fragen."""

    def __init__(self, answers=None, default=True):
        self.answers = list(answers or [])
        self.default = default
        self.questions = []

    def __call__(self, prompt, strict=False):
        self.questions.append((prompt, strict))
        return self.answers.pop(0) if self.answers else self.default


def executor(demo, state, asker, out=None):
    return Executor(
        demo.client, demo.store, state, demo.notifier, out=out or (lambda s: None), ask=asker, sleep=lambda s: None, cancel_wait_seconds=1
    )


def test_full_apply_places_all_orders(demo):
    state = State()
    report = demo.analyse(state)
    asker = Asker(default=True)
    result = executor(demo, state, asker).apply(report.order_actions())
    assert not result.failed and not result.unprotected
    assert len(result.done) == 4
    world = demo.world()
    assert world["orders"]["order-legacy-1"]["status"] == "CANCELLED"
    open_orders = {o["isin"]: o for o in world["orders"].values() if o["status"] == "PENDING"}
    assert open_orders[APPLE]["order_type"] == "trailing-stop"
    assert open_orders[SAP]["order_type"] == "trailing-stop"
    assert open_orders[NVDA]["stop_price"] == "104.15"
    # jede Order wurde mit der strikten Abfrage („ja“) bestätigt
    assert sum(1 for _, strict in asker.questions if strict) == 4
    # Alarm automatisch, danach ist nichts mehr zu tun
    apply_alert_actions(demo.client, state, demo.store, report.alert_actions())
    assert len(demo.world()["alerts"]) == 1
    again = demo.analyse(state)
    assert again.actions == []
    assert state.positions[ALV].managed_alert_id


def test_phase2_never_runs_without_confirmation(demo):
    state = State()
    report = demo.analyse(state, isins={NVDA, SAP})
    asker = Asker(default=False)
    result = executor(demo, state, asker).apply(report.order_actions())
    assert len(result.skipped) == 2
    assert not any("--confirm" in call for call in demo.client.calls)
    assert all(o["status"] != "PENDING" or o["isin"] not in (NVDA, SAP) for o in demo.world()["orders"].values())


def test_declining_after_cancel_first_reports_unprotected(demo):
    state = State()
    report = demo.analyse(state, isins={APPLE})
    # 1. „alte Order zuerst stornieren?“ → ja, 2. „verbindlich platzieren?“ → nein
    result = executor(demo, state, Asker([True, False])).apply(report.order_actions())
    assert result.unprotected
    assert demo.world()["orders"]["order-legacy-1"]["status"] == "CANCELLED"


def test_unknown_outcome_is_not_retried_and_can_be_resolved(demo):
    state = State()
    report = demo.analyse(state, isins={NVDA})
    demo.edit_world(lambda w: w.update({"fail_next_submit": "unknown"}))
    result = executor(demo, state, Asker(default=True)).apply(report.order_actions())
    assert result.failed and "unklar" in result.failed[0]
    assert state.positions[NVDA].unresolved_submission is not None
    submits = [c for c in demo.client.calls if "--confirm" in c]
    assert len(submits) == 1  # kein automatischer Wiederholungsversuch

    # Klärung: keine Order vorhanden → mit derselben Bestätigungs-ID erneut einreichen
    assert resolve_unresolved(demo.client, demo.store, state, out=lambda s: None, ask=Asker(default=True)) == 0
    assert state.positions[NVDA].unresolved_submission is None
    pending = [o for o in demo.world()["orders"].values() if o["isin"] == NVDA and o["status"] == "PENDING"]
    assert len(pending) == 1


def test_expired_confirmation_is_not_submitted(demo, monkeypatch):
    state = State()
    report = demo.analyse(state, isins={NVDA})
    real_time = time.time
    calls = {"n": 0}

    def fake_ask(prompt, strict=False):
        # Nutzer lässt sich > 15 Minuten Zeit
        calls["n"] += 1
        monkeypatch.setattr(time, "time", lambda: real_time() + 16 * 60)
        return True

    result = executor(demo, state, fake_ask).apply(report.order_actions())
    assert result.unprotected or result.failed or result.skipped
    assert not any("--confirm" in c for c in demo.client.calls)
