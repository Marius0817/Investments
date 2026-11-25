"""Erzeugt eine Demo-Umgebung (simuliertes Depot + Konfiguration) zum gefahrlosen Ausprobieren."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from stopguard.fake_sc import iso, s


def _buy(tx_id: str, isin: str, when: datetime, qty: Decimal, price: Decimal, plan: bool = False) -> dict:
    return {
        "id": tx_id,
        "summary_type": "BrokerSecurityTransactionSummary",
        "currency": "EUR",
        "type": "SECURITY_TRANSACTION",
        "status": "SETTLED",
        "is_cancellation": False,
        "last_event_datetime": iso(when),
        "description": "Savings plan" if plan else "Buy",
        "custodian": "BAADER_BANK",
        "isin": isin,
        "security_transaction_type": "SAVINGS_PLAN" if plan else "SINGLE",
        "quantity": s(qty),
        "amount": s((qty * price + (Decimal(0) if plan else Decimal("0.99"))).quantize(Decimal("0.01"))),
        "side": "BUY",
        "limit_price": None,
        "stop_price": None,
    }


def build_world(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    world: dict = {
        "portfolio_id": "demo-portfolio",
        "securities": {},
        "holdings": {},
        "transactions": [],
        "orders": {},
        "alerts": [],
        "next_id": 100,
    }
    n = 0

    def tx_id() -> str:
        nonlocal n
        n += 1
        return f"tx-demo-{n:03d}"

    def add(isin: str, name: str, bid: str, drift: float = 0.1, vol: float = 0.25) -> None:
        world["securities"][isin] = {"name": name, "bid": bid, "ask": s(Decimal(bid) * Decimal("1.002")), "drift": drift, "vol": vol}

    # 1) Problem 2: Order veraltet – gekauft bei 180, Stop bei 171 (−5 %), Kurs inzwischen +20 %
    add("US0378331005", "Apple Inc. (Demo)", "216.00", 0.25, 0.25)
    world["holdings"]["US0378331005"] = {"quantity": "10", "fifo_price": "180.099"}
    world["transactions"].append(_buy(tx_id(), "US0378331005", now - timedelta(days=62), Decimal(10), Decimal(180)))
    world["orders"]["order-legacy-1"] = {
        "isin": "US0378331005",
        "side": "SELL",
        "status": "PENDING",
        "order_type": "stop",
        "shares": "10",
        "stop_price": "171",
        "venue": "GETTEX",
        "created": iso(now - timedelta(days=62)),
        "updated": iso(now - timedelta(days=62)),
        "valid_until": iso(now + timedelta(days=298)),
    }

    # 2) Problem 1: Sparplan seit 14 Monaten, Stop nach IRR (Mindestrendite absichern)
    add("IE00B4L5Y983", "iShares Core MSCI World (Demo)", "104.20", 0.09, 0.14)
    qty_total = Decimal(0)
    cost = Decimal(0)
    for m in range(14, 0, -1):
        price = Decimal("80") + Decimal(14 - m) * Decimal("1.6")
        qty = (Decimal(200) / price).quantize(Decimal("0.000001"))
        qty_total += qty
        cost += Decimal(200)
        world["transactions"].append(_buy(tx_id(), "IE00B4L5Y983", now - timedelta(days=30 * m + 3), qty, price, plan=True))
    world["holdings"]["IE00B4L5Y983"] = {"quantity": s(qty_total), "fifo_price": s((cost / qty_total).quantize(Decimal("0.0001")))}

    # 3) Problem 3, Phase „Aufbau“: frisch gekauft, −8 % → Verluste bis 20 % werden toleriert
    add("US67066G1040", "NVIDIA Corp. (Demo)", "119.60", 0.3, 0.45)
    world["holdings"]["US67066G1040"] = {"quantity": "5", "fifo_price": "130.198"}
    world["transactions"].append(_buy(tx_id(), "US67066G1040", now - timedelta(days=20), Decimal(5), Decimal(130)))

    # 4) Problem 3, Phase 2: 5 % IRR erreicht → Trailing 5 %
    add("DE0007164600", "SAP SE (Demo)", "214.00", 0.12, 0.22)
    world["holdings"]["DE0007164600"] = {"quantity": "8", "fifo_price": "200.124"}
    world["transactions"].append(_buy(tx_id(), "DE0007164600", now - timedelta(days=160), Decimal(8), Decimal(200)))

    # 5) Kursalarm-Modus (keine Order, Push über die Scalable-App)
    add("DE0008404005", "Allianz SE (Demo)", "352.40", 0.08, 0.18)
    world["holdings"]["DE0008404005"] = {"quantity": "6", "fifo_price": "290.165"}
    world["transactions"].append(_buy(tx_id(), "DE0008404005", now - timedelta(days=400), Decimal(6), Decimal(290)))
    world["transactions"].append(
        {
            "id": tx_id(),
            "summary_type": "BrokerCashTransactionSummary",
            "currency": "EUR",
            "type": "CASH_TRANSACTION",
            "status": "SETTLED",
            "is_cancellation": False,
            "last_event_datetime": iso(now - timedelta(days=200)),
            "description": "Dividend",
            "related_isin": "DE0008404005",
            "cash_transaction_type": "DISTRIBUTION",
            "amount": "82.80",
        }
    )

    # 6) nicht konfigurierte Position → nur Beobachtung
    add("IE00BK5BQT80", "Vanguard FTSE All-World (Demo)", "128.50", 0.08, 0.13)
    world["holdings"]["IE00BK5BQT80"] = {"quantity": "20", "fifo_price": "121.30"}
    world["transactions"].append(_buy(tx_id(), "IE00BK5BQT80", now - timedelta(days=300), Decimal(20), Decimal("121.25")))
    return world


DEMO_CONFIG = """# Demo-Konfiguration – arbeitet gegen ein SIMULIERTES Depot (kein echter Broker).
[scalable]
sc_command = {sc_command}
venue = "gettex"

[engine]
default_strategy = "irr_gate"
unlisted_positions = "watch"

[schedule]
active_hours = "always"   # Demo: jederzeit vollständige Läufe
weekdays_only = false

[notify]
channels = ["console"]

[paths]
state_dir = {state_dir}

[[positions]]
isin = "US0378331005"      # Apple: veraltete Stop-Order → Trailing (Problem 2)
strategy = "trailing_5"

[[positions]]
isin = "IE00B4L5Y983"      # MSCI World Sparplan: Stop nach IRR (Problem 1)
strategy = "irr_lock"

[[positions]]
isin = "US67066G1040"      # NVIDIA: 20 % Verlust tolerieren bis 5 % IRR (Problem 3)
strategy = "irr_gate"

[[positions]]
isin = "DE0007164600"      # SAP: 5 % IRR erreicht → Trailing 5 % (Problem 3)
strategy = "irr_gate"

[[positions]]
isin = "DE0008404005"      # Allianz: nur Kursalarm in der Scalable-App pflegen
strategy = "trailing_5"
execution = "alert"
"""


def init_demo(directory: Path) -> Path:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    world_path = directory / "demo-world.json"
    world_path.write_text(json.dumps(build_world(), indent=2, ensure_ascii=False), encoding="utf-8")
    sc_command = json.dumps([sys.executable, "-m", "stopguard.fake_sc", "--world", str(world_path)])
    config_path = directory / "stopguard.toml"
    config_path.write_text(
        DEMO_CONFIG.format(sc_command=sc_command, state_dir=json.dumps(str(directory / "state"))), encoding="utf-8"
    )
    return config_path
