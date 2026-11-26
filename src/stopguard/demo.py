"""Erzeugt eine Demo-Umgebung (simuliertes Depot + Konfiguration) zum gefahrlosen Ausprobieren."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from stopguard.fake_sc import iso, s


def _trade(
    tx_id: str,
    isin: str,
    when: datetime,
    qty: Decimal,
    price: Decimal,
    *,
    side: str = "BUY",
    plan: bool = False,
    fee: Decimal | None = None,
    tax: Decimal = Decimal(0),
    name: str | None = None,
) -> dict:
    fee = Decimal(0) if plan else (Decimal("0.99") if fee is None else fee)
    market = (qty * price).quantize(Decimal("0.01"))
    amount = market + fee if side == "BUY" else market - fee - tax
    return {
        "id": tx_id,
        "summary_type": "BrokerSecurityTransactionSummary",
        "currency": "EUR",
        "type": "SECURITY_TRANSACTION",
        "status": "SETTLED",
        "is_cancellation": False,
        "last_event_datetime": iso(when),
        "description": "Savings plan" if plan else ("Buy" if side == "BUY" else "Sell"),
        "custodian": "BAADER_BANK",
        "isin": isin,
        "security_transaction_type": "SAVINGS_PLAN" if plan else "SINGLE",
        "quantity": s(qty),
        "amount": s(amount),
        "side": side,
        "limit_price": None,
        "stop_price": None,
        "_details": {
            "security": {"isin": isin, "name": name},
            "security_trade": {
                "status": "SETTLED",
                "side": side,
                "number_of_shares": {"filled": s(qty), "total": s(qty)},
                "average_price": s(price),
                "total_amount": s(amount),
                "trade_transaction_amounts": {"transaction_fee": s(fee), "venue_fee": "0", "tax_amount": s(tax)},
            },
        },
    }


def _cash(tx_id: str, when: datetime, ctype: str, amount: Decimal, *, isin: str | None = None, desc: str = "", details: dict | None = None) -> dict:
    tx = {
        "id": tx_id,
        "summary_type": "BrokerCashTransactionSummary",
        "currency": "EUR",
        "type": "CASH_TRANSACTION",
        "status": "SETTLED",
        "is_cancellation": False,
        "last_event_datetime": iso(when),
        "description": desc or ctype.title(),
        "related_isin": isin,
        "cash_transaction_type": ctype,
        "amount": s(amount),
    }
    if details:
        tx["_details"] = details
    return tx


def _signed(tx: dict) -> Decimal:
    """Kontowirkung einer Demo-Buchung (für die Kontosimulation)."""
    amt = Decimal(tx["amount"])
    if tx["summary_type"] == "BrokerSecurityTransactionSummary":
        return -amt if tx["side"] == "BUY" else amt
    t = tx["cash_transaction_type"]
    if t in ("DEPOSIT", "DISTRIBUTION", "TAX_RETURN"):
        return abs(amt)
    if t == "INTEREST":
        return amt
    return -abs(amt)


LOAN_RATE = Decimal("0.039")  # Sollzins Wertpapierkredit p.a. (Demo)


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
    tx = world["transactions"]

    def tx_id() -> str:
        nonlocal n
        n += 1
        return f"tx-demo-{n:03d}"

    def ago(days: float) -> datetime:
        return (now - timedelta(days=days)).replace(hour=9, minute=30, second=0, microsecond=0)

    def add(isin: str, name: str, bid: str, drift: float = 0.1, vol: float = 0.25) -> None:
        world["securities"][isin] = {"name": name, "bid": bid, "ask": s(Decimal(bid) * Decimal("1.002")), "drift": drift, "vol": vol}

    def buy(isin: str, days: float, qty, price, **kw) -> None:
        tx.append(_trade(tx_id(), isin, ago(days), Decimal(str(qty)), Decimal(str(price)), name=world["securities"][isin]["name"], **kw))

    def sell(isin: str, days: float, qty, price, tax="0") -> None:
        tx.append(_trade(tx_id(), isin, ago(days), Decimal(str(qty)), Decimal(str(price)), side="SELL", tax=Decimal(tax), name=world["securities"][isin]["name"]))

    def deposit(days: float, amount) -> None:
        tx.append(_cash(tx_id(), ago(days) - timedelta(hours=2), "DEPOSIT", Decimal(str(amount)), desc="Einzahlung"))

    tx.append(_cash(tx_id(), ago(430), "DEPOSIT", Decimal(7000), desc="Einzahlung"))

    def dividend(isin: str, days: float, gross: str, tax: str, desc: str) -> None:
        net = Decimal(gross) - Decimal(tax)
        tx.append(
            _cash(
                tx_id(), ago(days), "DISTRIBUTION", net, isin=isin, desc=desc,
                details={"cash": {"cash_transaction_type": "DISTRIBUTION", "amount": s(net),
                                  "tax_details": {"gross_amount": gross, "tax_amount": tax}}},
            )
        )

    # 1) Problem 2: Order veraltet – gekauft bei 180, Stop bei 171 (−5 %), Kurs inzwischen +20 %
    add("US0378331005", "Apple Inc. (Demo)", "216.00", 0.25, 0.25)
    world["holdings"]["US0378331005"] = {"quantity": "10", "fifo_price": "180.099"}
    deposit(64, 1850)
    buy("US0378331005", 62, 10, 180)
    dividend("US0378331005", 40, "2.32", "0.61", "Dividende Apple")
    world["orders"]["order-legacy-1"] = {
        "isin": "US0378331005",
        "side": "SELL",
        "status": "PENDING",
        "order_type": "stop",
        "shares": "10",
        "stop_price": "171",
        "venue": "GETTEX",
        "created": iso(ago(62)),
        "updated": iso(ago(62)),
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
        cost += (qty * price).quantize(Decimal("0.01"))
        deposit(30 * m + 5, 200)
        buy("IE00B4L5Y983", 30 * m + 3, qty, price, plan=True)
    world["holdings"]["IE00B4L5Y983"] = {"quantity": s(qty_total), "fifo_price": s((cost / qty_total).quantize(Decimal("0.0001")))}
    vap_day = datetime(now.year, 1, 2, 8, tzinfo=timezone.utc)
    if ago(30 * 14 + 3) < vap_day < now:
        tx.append(_cash(tx_id(), vap_day, "TAX", Decimal("4.87"), isin="IE00B4L5Y983", desc=f"Vorabpauschale {now.year - 1}"))

    # 3) Problem 3, Phase „Aufbau“: frisch gekauft, −8 % → Verluste bis 20 % werden toleriert
    add("US67066G1040", "NVIDIA Corp. (Demo)", "119.60", 0.3, 0.45)
    world["holdings"]["US67066G1040"] = {"quantity": "5", "fifo_price": "130.198"}
    deposit(22, 700)
    buy("US67066G1040", 20, 5, 130)

    # 4) Problem 3, Phase 2: 5 % IRR erreicht → Trailing 5 %
    add("DE0007164600", "SAP SE (Demo)", "214.00", 0.12, 0.22)
    world["holdings"]["DE0007164600"] = {"quantity": "8", "fifo_price": "200.124"}
    deposit(162, 1650)
    buy("DE0007164600", 160, 8, 200)
    dividend("DE0007164600", 150, "18.80", "4.96", "Dividende SAP")

    # 5) Kursalarm-Modus + Dividende mit Steuerabzug
    add("DE0008404005", "Allianz SE (Demo)", "352.40", 0.08, 0.18)
    world["holdings"]["DE0008404005"] = {"quantity": "6", "fifo_price": "290.165"}
    buy("DE0008404005", 400, 6, 290)
    tx.append(
        _cash(
            tx_id(), ago(200), "DISTRIBUTION", Decimal("82.78"), isin="DE0008404005", desc="Dividende Allianz",
            details={"cash": {"cash_transaction_type": "DISTRIBUTION", "amount": "82.78",
                              "tax_details": {"gross_amount": "112.44", "tax_amount": "29.66"}}},
        )
    )

    # 6) nicht konfigurierte Position → nur Beobachtung
    add("IE00BK5BQT80", "Vanguard FTSE All-World (Demo)", "128.50", 0.08, 0.13)
    world["holdings"]["IE00BK5BQT80"] = {"quantity": "20", "fifo_price": "121.30"}
    buy("IE00BK5BQT80", 300, 20, "121.25")

    # 6b) ausschüttender Dividenden-ETF: quartalsweise Zahlungen (Dividendenprognose)
    add("IE00B8GKDB10", "Vanguard FTSE All-World High Dividend Yield (Demo)", "66.40", 0.07, 0.12)
    world["holdings"]["IE00B8GKDB10"] = {"quantity": "50", "fifo_price": "60.0198"}
    deposit(422, 3000)
    buy("IE00B8GKDB10", 420, 50, 60)
    for days, gross, tax in ((355, "21.40", "3.95"), (264, "27.10", "5.00"), (173, "22.30", "4.12"), (82, "24.60", "4.54")):
        dividend("IE00B8GKDB10", days, gross, tax, "Ausschüttung Vanguard High Dividend")

    # Watchlist (nicht im Depot) – Kurse und Verlauf über 'broker quote' / 'broker candlesticks'
    add("NL0010273215", "ASML Holding (Demo)", "688.00", 0.15, 0.35)
    add("US5949181045", "Microsoft Corp. (Demo)", "394.50", 0.12, 0.22)
    add("DK0062498333", "Novo Nordisk (Demo)", "64.20", -0.1, 0.35)

    # 7) abgeschlossene Trades: Gewinn (Steuer durch Verlusttopf verrechnet) und Verlust
    add("US88160R1014", "Tesla Inc. (Demo)", "250.00", 0.2, 0.55)
    buy("US88160R1014", 300, 5, 200)
    add("DE000ZAL1111", "Zalando SE (Demo)", "22.00", 0.0, 0.4)
    buy("DE000ZAL1111", 250, 50, 30)
    sell("DE000ZAL1111", 130, 50, 24)  # Verlust → Aktienverlusttopf
    sell("US88160R1014", 100, 5, 260)  # Gewinn < Verlusttopf → keine Steuer einbehalten

    # 8) kreditfinanzierter Trade (Wertpapierkredit), später Teilverkauf
    add("LU0274208692", "Xtrackers MSCI World Swap (Demo)", "111.00", 0.1, 0.15)
    buy("LU0274208692", 180, 60, 100)
    sell("LU0274208692", 40, 15, 108, tax="21.85")
    world["holdings"]["LU0274208692"] = {"quantity": "45", "fifo_price": "100.0165"}

    # Kontosimulation: monatliche Sollzinsen auf negativen Saldo
    tx.sort(key=lambda t: t["last_event_datetime"])
    events = [(datetime.fromisoformat(t["last_event_datetime"].replace("Z", "+00:00")), _signed(t)) for t in tx]
    start = events[0][0].replace(hour=0, minute=0)
    cash = Decimal(0)
    neg_days = Decimal(0)
    i = 0
    day = start
    while day < now:
        nxt = day + timedelta(days=1)
        while i < len(events) and events[i][0] < nxt:
            cash += events[i][1]
            i += 1
        if cash < 0:
            neg_days += -cash
        if nxt.day == 1 and neg_days > 0:
            interest = (neg_days * LOAN_RATE / 365).quantize(Decimal("0.01"))
            when = nxt.replace(hour=6)
            if interest > 0 and when < now:
                tx.append(_cash(tx_id(), when, "INTEREST", -interest, desc="Sollzinsen Wertpapierkredit"))
                cash -= interest
            neg_days = Decimal(0)
        day = nxt
    while i < len(events):
        cash += events[i][1]
        i += 1
    tx.sort(key=lambda t: t["last_event_datetime"])

    sec_value = sum(Decimal(h["quantity"]) * Decimal(world["securities"][k]["bid"]) for k, h in world["holdings"].items())
    loaned = max(Decimal(0), -cash)
    limit = (sec_value * Decimal("0.5")).quantize(Decimal("0.01"))
    world["account"] = {
        "cash_balance": s(max(cash, Decimal(0)).quantize(Decimal("0.01"))),
        "loaned": s(loaned.quantize(Decimal("0.01"))),
        "available_credit_line": s(max(Decimal(0), limit - loaned).quantize(Decimal("0.01"))),
        "buying_power": s((max(cash, Decimal(0)) + max(Decimal(0), limit - loaned)).quantize(Decimal("0.01"))),
        "buying_power_without_credit": s(max(cash, Decimal(0)).quantize(Decimal("0.01"))),
    }
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

[portfolio]
benchmark_isin = "IE00B4L5Y983"
benchmark_name = "MSCI World (Demo)"
details_delay_seconds = 0

[credit]
mode = "auto"

# Klassifizierung für Diversifikation und ETF-Durchschau.
# Die Gewichte der ETFs sind vereinfachte DEMO-Werte – für das echte Depot aus dem Factsheet übernehmen.
[instruments.IE00B4L5Y983]
ter = 0.20                       # laufende Kosten in % p. a. (DEMO-Wert)
ticker = "EUNL.DE"
kind = "equity_fund"
distributing = false
tags = ["ETF", "Welt"]

[instruments.IE00B4L5Y983.exposure]
countries = {{ US = 72, JP = 5.5, GB = 3.5, CA = 3, FR = 2.7, CH = 2.4, DE = 2.2 }}
sectors = {{ "Informationstechnologie" = 26, "Finanzen" = 16, "Industrie" = 11, "Gesundheit" = 10, "Nicht-Basiskonsum" = 10, "Kommunikation" = 8, "Basiskonsum" = 6, "Energie" = 4, "Material" = 3, "Versorger" = 2.5, "Immobilien" = 2 }}
currencies = {{ USD = 72, EUR = 9, JPY = 5.5, GBP = 3.5, CAD = 3, CHF = 2.4 }}
holdings = {{ US0378331005 = 4.8, US67066G1040 = 4.6, US5949181045 = 4.3, "Amazon" = 2.7, "Alphabet" = 2.5, "Meta Platforms" = 1.8 }}

[instruments.LU0274208692]
ter = 0.45                       # laufende Kosten in % p. a. (DEMO-Wert)
ticker = "XDWD.DE"
kind = "equity_fund"
distributing = false
tags = ["ETF", "Welt", "Kredit"]

[instruments.LU0274208692.exposure]
countries = {{ US = 72, JP = 5.5, GB = 3.5, CA = 3, FR = 2.7, CH = 2.4, DE = 2.2 }}
sectors = {{ "Informationstechnologie" = 26, "Finanzen" = 16, "Industrie" = 11, "Gesundheit" = 10, "Nicht-Basiskonsum" = 10, "Kommunikation" = 8, "Basiskonsum" = 6, "Energie" = 4, "Material" = 3, "Versorger" = 2.5, "Immobilien" = 2 }}
currencies = {{ USD = 72, EUR = 9, JPY = 5.5, GBP = 3.5, CAD = 3, CHF = 2.4 }}
holdings = {{ US0378331005 = 4.8, US67066G1040 = 4.6, US5949181045 = 4.3, "Amazon" = 2.7, "Alphabet" = 2.5, "Meta Platforms" = 1.8 }}

[instruments.IE00BK5BQT80]
ter = 0.19                       # laufende Kosten in % p. a. (DEMO-Wert)
ticker = "VWCE.DE"
kind = "equity_fund"
distributing = false
tags = ["ETF", "Welt"]

[instruments.IE00BK5BQT80.exposure]
countries = {{ US = 62, JP = 5.5, GB = 3.3, CN = 2.8, CA = 2.8, FR = 2.4, CH = 2.2, DE = 2, TW = 2, IN = 2 }}
sectors = {{ "Informationstechnologie" = 25, "Finanzen" = 17, "Industrie" = 11, "Gesundheit" = 10, "Nicht-Basiskonsum" = 10, "Kommunikation" = 8, "Basiskonsum" = 6, "Energie" = 4, "Material" = 4, "Versorger" = 3, "Immobilien" = 2 }}
holdings = {{ US0378331005 = 4.0, US67066G1040 = 4.0, US5949181045 = 3.7, "Amazon" = 2.3, "Alphabet" = 2.2 }}

[instruments.IE00B8GKDB10]
ter = 0.29                       # laufende Kosten in % p. a. (DEMO-Wert)
ticker = "VHYL.DE"
kind = "equity_fund"
distributing = true
tags = ["ETF", "Dividende"]

[instruments.IE00B8GKDB10.exposure]
countries = {{ US = 42, JP = 6, GB = 6, CH = 6, CA = 4, AU = 4, DE = 3, FR = 3, CN = 3, TW = 3 }}
sectors = {{ "Finanzen" = 24, "Gesundheit" = 11, "Industrie" = 11, "Basiskonsum" = 10, "Informationstechnologie" = 9, "Energie" = 8, "Versorger" = 7, "Nicht-Basiskonsum" = 6, "Material" = 5, "Kommunikation" = 5, "Immobilien" = 1 }}

[instruments.US0378331005]
ticker = "APC.DE"
tags = ["Tech", "USA"]
sector = "Informationstechnologie"

[instruments.US67066G1040]
ticker = "NVD.DE"
tags = ["Tech", "USA"]
sector = "Informationstechnologie"

[instruments.DE0007164600]
sector = "Informationstechnologie"

[instruments.DE0008404005]
sector = "Finanzen"

[instruments.US5949181045]
name = "Microsoft"

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


DEMO_WATCHLIST = [
    {"isin": "NL0010273215", "name": "ASML Holding (Demo)", "note": "Nachkaufen, falls der Kurs zurückkommt", "target_price": 640.0},
    {"isin": "US5949181045", "name": "Microsoft Corp. (Demo)", "note": "steckt schon in den Welt-ETFs", "target_price": None},
    {"isin": "DK0062498333", "name": "Novo Nordisk (Demo)", "note": "", "target_price": 55.0},
]

DEMO_TARGETS = {
    "IE00B4L5Y983": 30, "IE00BK5BQT80": 20, "LU0274208692": 15, "IE00B8GKDB10": 15,
    "US0378331005": 5, "DE0007164600": 5, "DE0008404005": 5, "US67066G1040": 5,
}


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
    state = directory / "state"
    state.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    if not (state / "watchlist.json").exists():
        items = [{**i, "added_at": now} for i in DEMO_WATCHLIST]
        (state / "watchlist.json").write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False), encoding="utf-8")
    if not (state / "targets.json").exists():
        (state / "targets.json").write_text(json.dumps({"targets": DEMO_TARGETS, "updated_at": now}, indent=2), encoding="utf-8")
    return config_path
