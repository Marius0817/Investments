import json
from pathlib import Path

import pytest

from stopguard import cli
from stopguard.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parents[1] / "src" / "stopguard" / "data" / "config.example.toml"


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE)
    assert "mein_gate" in cfg.strategies
    assert len(cfg.strategies["mein_gate"].phases) == 3
    assert cfg.engine.min_change_pct == 1.0


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"engine": {"unknown": 1}}, "Unbekannte Schlüssel"),
        ({"positions": [{"isin": "XX"}]}, "ISIN"),
        ({"positions": [{"isin": "US0378331005", "strategy": "nope"}]}, "unbekannte Strategie"),
        ({"strategies": {"s": {"phases": [{"name": "a", "activate": {"irr_pct": 5}}]}}}, "erste Phase"),
        ({"strategies": {"s": {"phases": [{"name": "a"}, {"name": "b", "trailing_pct": 5}]}}}, "activate"),
        ({"strategies": {"s": {"phases": [{"name": "a", "trailing_pct": 0.1}]}}}, "0,5"),
        (
            {"strategies": {"s": {"phases": [{"name": "a", "max_loss_pct": 5, "trailing_pct": 5, "order_type": "trailing-stop"}]}}},
            "nur mit",
        ),
        ({"scalable": {"venue": "nyse"}}, "venue"),
        ({"engine": {"min_change_pct": "1"}}, "unerwarteter Typ"),
        ({"notify": {"channels": ["fax"]}}, "unbekannter Kanal"),
    ],
)
def test_config_errors(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def _patch_runner(monkeypatch):
    from conftest import inprocess_runner
    from stopguard.sc import ScClient

    orig_init = ScClient.__init__

    def init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.runner = inprocess_runner

    monkeypatch.setattr(ScClient, "__init__", init)


def test_status_json(demo, monkeypatch, capsys):
    _patch_runner(monkeypatch)
    assert cli.main(["-c", str(demo.config_path), "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["positions"]) == 8  # inkl. Kredit-Trade und Dividenden-ETF (nicht konfiguriert → watch)
    assert {a["kind"] for a in data["actions"]} == {"replace", "place", "alert_set"}


def test_run_notifies_once_and_syncs_alerts(demo, monkeypatch):
    _patch_runner(monkeypatch)
    sent = []
    monkeypatch.setattr(cli.Notifier, "send", lambda self, t, m, lvl="info": sent.append((t, lvl)) or [])
    assert cli.main(["-c", str(demo.config_path), "run", "-q"]) == 0
    titles = [t for t, _ in sent]
    assert any("Order-Anpassung" in t for t in titles)
    assert len(demo.world()["alerts"]) == 1  # Kursalarm automatisch gesetzt
    sent.clear()
    assert cli.main(["-c", str(demo.config_path), "run", "-q"]) == 0
    assert not [t for t, _ in sent if "Order-Anpassung" in t]  # dedupliziert


def test_backtest_command(demo, monkeypatch, capsys):
    _patch_runner(monkeypatch)
    assert cli.main(["-c", str(demo.config_path), "backtest", "DE0007164600", "-s", "irr_gate", "-s", "trailing_5"]) == 0
    out = capsys.readouterr().out
    assert "irr_gate" in out and "trailing_5" in out


def test_keepalive_outside_trading_window(demo, monkeypatch):
    _patch_runner(monkeypatch)
    monkeypatch.setattr(cli, "_in_active_window", lambda ctx: False)
    assert cli.main(["-c", str(demo.config_path), "run", "-q"]) == 0
    state = demo.store.load()
    assert state.last_keepalive_at is not None and state.last_run_at is None
    # zweiter Aufruf kurz danach: kein erneuter Request
    assert cli.main(["-c", str(demo.config_path), "run", "-q"]) == 0
    assert demo.store.load().last_keepalive_at == state.last_keepalive_at
