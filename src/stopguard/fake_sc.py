"""Simulierte Scalable CLI für Demo und Tests – KEIN Zugriff auf echte Konten.

Bildet die JSON-Ausgaben der offiziellen ``sc``-Befehle nach, die stopguard nutzt
(Stand Scalable CLI 1.3.0). Zustand liegt in einer JSON-„Welt“-Datei.

Aufruf wie ``sc``:  python -m stopguard.fake_sc --world welt.json broker holdings --json
Zusatzbefehl:       python -m stopguard.fake_sc --world welt.json sim move <ISIN> <+/-Prozent>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

OPEN = ("CREATED", "REQUESTED", "PENDING", "PARTIAL_FILLED", "CANCEL_REQUESTED")


class Fail(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 10):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def s(x: Decimal) -> str:
    t = format(x.normalize(), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def now(world: dict[str, Any]) -> datetime:
    if world.get("now"):
        return datetime.fromisoformat(world["now"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Welt laden/speichern
# ---------------------------------------------------------------------------

def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, world: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(world, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def next_id(world: dict[str, Any], prefix: str) -> str:
    world["next_id"] = int(world.get("next_id", 1)) + 1
    return f"{prefix}-{world['next_id']:05d}"


def blocked(world: dict[str, Any], isin: str) -> Decimal:
    return sum(
        (D(o["shares"]) for o in world["orders"].values() if o["isin"] == isin and o["status"] in OPEN and o["side"] == "SELL"),
        Decimal(0),
    )


def bid_ask(world: dict[str, Any], isin: str) -> tuple[Decimal, Decimal]:
    sec = world["securities"][isin]
    bid = D(sec["bid"])
    ask = D(sec.get("ask") or bid * Decimal("1.002"))
    return bid, ask


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

def cmd_holdings(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    items = []
    for isin, h in world["holdings"].items():
        if D(h["quantity"]) <= 0:
            continue
        sec = world["securities"][isin]
        bid, ask = bid_ask(world, isin)
        mid = (bid + ask) / 2
        items.append(
            {
                "kind": "security",
                "isin": isin,
                "name": sec["name"],
                "security_type": sec.get("type", "STOCK"),
                "quantity": h["quantity"],
                "pending_quantity": "0",
                "blocked_quantity": s(blocked(world, isin)),
                "fifo_price": h.get("fifo_price"),
                "valuation": s((D(h["quantity"]) * mid).quantize(Decimal("0.01"))),
                "valuation_currency": "EUR",
                "quote_venue": "SEIX",
                "quote_mid_price": s(mid.quantize(Decimal("0.0001"))),
                "quote_currency": "EUR",
                "quote_timestamp_utc": iso(now(world)),
                "quote_is_outdated": bool(world.get("market_closed", False)),
            }
        )
    return {
        "requested_quote_source": "CONSOLIDATED",
        "account_id": "demo-account",
        "portfolio_id": world.get("portfolio_id", "demo-portfolio"),
        "count": len(items),
        "security_count": len(items),
        "crypto_count": 0,
        "items": items,
        "crypto_items": [],
    }


def cmd_quote(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    if a.isin not in world["securities"]:
        raise Fail("broker_input_invalid", f"Unknown ISIN {a.isin}")
    sec = world["securities"][a.isin]
    bid, ask = bid_ask(world, a.isin)
    return {
        "isin": a.isin,
        "name": sec["name"],
        "security_type": sec.get("type", "STOCK"),
        "quote_venue": "SEIX",
        "quote_mid_price": s(((bid + ask) / 2).quantize(Decimal("0.0001"))),
        "quote_bid_price": s(bid),
        "quote_ask_price": s(ask),
        "quote_currency": "EUR",
        "quote_timestamp_utc": iso(now(world)),
        "quote_is_outdated": bool(world.get("market_closed", False)),
        "quote_performances": [],
    }


def _order_summary(oid: str, o: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": oid,
        "summary_type": "BrokerSecurityTransactionSummary",
        "currency": "EUR",
        "type": "SECURITY_TRANSACTION",
        "status": o["status"],
        "is_cancellation": False,
        "last_event_datetime": o.get("updated") or o["created"],
        "description": "Sell",
        "custodian": "BAADER_BANK",
        "unknown_summary_type": False,
        "isin": o["isin"],
        "security_transaction_type": "SINGLE",
        "quantity": o["shares"],
        "amount": o.get("amount"),
        "side": o["side"],
        "limit_price": o.get("limit_price"),
        "stop_price": o.get("stop_price"),
    }


def cmd_transactions(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    items = [dict(t) for t in world["transactions"]]
    items += [_order_summary(oid, o) for oid, o in world["orders"].items()]
    if a.status:
        items = [t for t in items if t.get("status") in a.status]
    if a.isin:
        items = [t for t in items if (t.get("isin") or t.get("related_isin")) == a.isin]
    if a.from_time:
        ft = datetime.fromisoformat(a.from_time.replace("Z", "+00:00"))
        items = [t for t in items if datetime.fromisoformat(t["last_event_datetime"].replace("Z", "+00:00")) >= ft]
    items.sort(key=lambda t: t["last_event_datetime"], reverse=True)
    start = int(a.cursor or 0)
    page = items[start : start + a.page_size]
    nxt = start + a.page_size
    return {
        "account_id": "demo-account",
        "portfolio_id": world.get("portfolio_id", "demo-portfolio"),
        "cursor": str(nxt) if nxt < len(items) else None,
        "total": len(items),
        "count": len(page),
        "items": page,
        "crypto": {"cursor": None, "total": 0, "count": 0, "items": []},
    }


def cmd_details(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    oid = a.transaction_id
    if oid in world["orders"]:
        o = world["orders"][oid]
        sec = world["securities"][o["isin"]]
        trailing = None
        if o["order_type"] == "trailing-stop":
            trailing = {
                "trail_type": "PERCENTAGE",
                "stop_price": o.get("stop_price"),
                "stop_price_source": "last_known",
                "refresh_status": "not_required",
                "trail_offset": float(o["trail_percent"]),
                "latest_stop_price_timestamp": o.get("updated"),
            }
        return {
            "id": oid,
            "detail_type": "security_trade",
            "type": "SECURITY_TRANSACTION",
            "currency": "EUR",
            "is_pending": o["status"] in OPEN,
            "is_cancellation": False,
            "security": {"isin": o["isin"], "name": sec["name"], "security_type": sec.get("type", "STOCK")},
            "security_trade": {
                "status": o["status"],
                "side": o["side"],
                "order_kind": "SINGLE",
                "number_of_shares": {"filled": "0", "total": o["shares"]},
                "average_price": o.get("average_price"),
                "limit_price": o.get("limit_price"),
                "stop_price": o.get("stop_price") if o["order_type"] != "trailing-stop" else o.get("stop_price"),
                "valid_until": o.get("valid_until"),
                "trading_venue": o.get("venue", "GETTEX"),
                "trailing_stop_info": trailing,
            },
        }
    for t in world["transactions"]:
        if t["id"] == oid:
            return {"id": oid, "type": t.get("type"), "status": t.get("status"), "security_trade": {"status": t.get("status")}}
    raise Fail("broker_transaction_not_found", f"BROKER_TRANSACTION_NOT_FOUND: {oid}")


def cmd_alerts(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    if a.sub == "add":
        isin = a.isin
        bid, _ = bid_ask(world, isin)
        alert = {
            "alert_id": next_id(world, "alert"),
            "direction": "DOWN" if D(a.price) < bid else "UP",
            "is_active": True,
            "price": a.price,
            "triggered_timestamp_utc": None,
            "isin": isin,
            "name": world["securities"][isin]["name"],
        }
        world["alerts"].append(alert)
        return {"isin": isin, "can_add_new": True, "items": [x for x in world["alerts"] if x["isin"] == isin]}
    if a.sub == "remove":
        before = len(world["alerts"])
        world["alerts"] = [x for x in world["alerts"] if x["alert_id"] != a.alert_id]
        if len(world["alerts"]) == before:
            raise Fail("broker_input_invalid", "alert not found")
        return {"removed": a.alert_id}
    items = [x for x in world["alerts"] if x["is_active"] or not a.active_only]
    return {"count": len(items), "items": items}


def _candles(world: dict[str, Any], isin: str, days: int) -> list[dict[str, Any]]:
    """Deterministische synthetische Tageskerzen, die beim aktuellen Kurs enden."""
    seed = int(hashlib.sha256(isin.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    sec = world["securities"][isin]
    drift = float(sec.get("drift", 0.10)) / 252
    vol = float(sec.get("vol", 0.25)) / math.sqrt(252)
    rets = [rng.gauss(drift, vol) for _ in range(days)]
    end_price = float(D(sec["bid"]))
    closes = [end_price]
    for r in reversed(rets[1:]):
        closes.append(closes[-1] / math.exp(r))
    closes.reverse()
    out = []
    t_end = now(world).replace(hour=17, minute=30, second=0, microsecond=0)
    for i, close in enumerate(closes):
        day = t_end - timedelta(days=len(closes) - 1 - i)
        prev = closes[i - 1] if i else close
        hi = max(prev, close) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(prev, close) * (1 - abs(rng.gauss(0, vol / 2)))
        out.append(
            {
                "open": f"{prev:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "close": f"{close:.4f}",
                "start_time_utc": iso(day.replace(hour=7)),
                "end_time_utc": iso(day),
            }
        )
    return out


TIMEFRAME_DAYS = {"one_month": 30, "three_months": 91, "six_months": 182, "year_to_date": 330, "one_year": 365, "max": 1825}


def cmd_candles(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    days = TIMEFRAME_DAYS.get(a.timeframe, 365)
    return {"isin": a.isin, "timeframe": a.timeframe, "source": "consolidated", "currency": "EUR", "candlesticks": _candles(world, a.isin, days)}


def _presentation(spec: dict[str, Any], bid: Decimal, venue: str) -> dict[str, Any]:
    est = (D(spec["shares"]) * D(spec.get("stop_price") or bid)).quantize(Decimal("0.01"))
    fields = [
        ("order", "ISIN", spec["isin"]),
        ("order", "Side", "SELL"),
        ("order", "Order type", spec["order_type"]),
        ("order", "Shares", spec["shares"]),
        ("order", "Stop price", spec.get("stop_price")),
        ("order", "Limit price", spec.get("limit_price")),
        ("order", "Trailing distance (%)", spec.get("trail_percent")),
        ("order", "Current bid", s(bid)),
        ("order", "Execution venue", venue),
        ("order", "Validity", "up to 360 days"),
        ("costs", "Order fee (EUR)", "0.99"),
        ("costs", "Estimated order volume (EUR)", s(est)),
        ("costs", "Exit costs total (EUR)", "0.99"),
        ("disclosure", "Execution instruction", f"I instruct Scalable Capital to place this order for execution on {venue} valid for up to 360 days."),
        ("disclosure", "Market data", "DEMO – simulierte Daten, keine echte Order"),
    ]
    titles = {"order": "Order", "costs": "Ex-ante costs", "disclosure": "Disclosures"}
    sections: dict[str, Any] = {k: {"title": v, "fields": []} for k, v in titles.items()}
    for sec, label, value in fields:
        sections[sec]["fields"].append({"path": f"/demo/{label}", "label": label, "value": value})
    return {"format": "markdown_sections", "section_order": list(titles), "sections": sections}


def cmd_trade_sell(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    isin = a.isin
    if isin not in world["holdings"]:
        raise Fail("trade_input_invalid", "TRADE_INPUT_INVALID: no position for isin")
    shares = D(a.shares)
    qty = D(world["holdings"][isin]["quantity"])
    bid, _ = bid_ask(world, isin)
    venue = (a.venue or "gettex").upper()
    spec = {
        "isin": isin,
        "shares": a.shares,
        "order_type": a.order_type,
        "stop_price": a.stop_price,
        "limit_price": a.limit_price,
        "trail_percent": a.trail_percent,
        "venue": venue,
    }
    if a.order_type in ("stop", "stop-limit") and not a.stop_price:
        raise Fail("trade_input_invalid", "TRADE_INPUT_INVALID: --stop-price required")
    if a.order_type == "trailing-stop":
        if not a.trail_percent or D(a.trail_percent) < Decimal("0.5"):
            raise Fail("trade_input_invalid", "TRADE_INPUT_INVALID: trail distance minimum is 0.5%")
        if venue not in ("GETTEX", "EIX", "SEIX", "MUNC"):
            raise Fail("trade_input_invalid", "TRADE_INPUT_INVALID: trailing stop only on EIX or gettex")
    signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

    if not a.confirm:  # Phase 1
        if shares > qty - blocked(world, isin):
            raise Fail("trade_input_invalid", "TRADE_INPUT_INVALID: shares exceed sellable quantity (blocked by open orders)")
        cid = "scb1_" + hashlib.sha256(f"{signature}{time.time()}".encode()).hexdigest()[:16]
        expires = int(time.time()) + 15 * 60
        world.setdefault("confirmations", {})[cid] = {"signature": signature, "expires": expires, "used": False}
        initial_stop = None
        if a.order_type == "trailing-stop":
            initial_stop = s((bid * (1 - D(a.trail_percent) / 100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        return {
            "account_id": "demo-account",
            "portfolio_id": world.get("portfolio_id"),
            "result": {"intent": {**spec, "side": "SELL", "initial_stop_price": initial_stop}},
            "confirmation": {"id": cid, "expires_at_epoch": expires},
            "compliance": {
                "requires_explicit_user_confirmation_between_phases": True,
                "forbid_automatic_phase_2_execution": True,
            },
            "presentation": _presentation({**spec, "stop_price": spec["stop_price"] or initial_stop}, bid, venue),
            "next_step": "confirm_with_id",
        }

    # Phase 2
    conf = world.get("confirmations", {}).get(a.confirm)
    if conf is None:
        raise Fail("confirmation_not_found", "CONFIRMATION_NOT_FOUND")
    if conf["used"]:
        raise Fail("confirmation_already_used", "CONFIRMATION_ALREADY_USED")
    if conf["expires"] < time.time():
        raise Fail("confirmation_expired", "CONFIRMATION_EXPIRED")
    if conf["signature"] != signature:
        raise Fail("confirmation_fields_mismatch", "CONFIRMATION_FIELDS_MISMATCH: repeat exact phase 1 arguments")
    failure = world.pop("fail_next_submit", None)
    if failure == "unknown":
        raise Fail("order_submission_failed", "Order submission failed: connection reset (outcome unknown)", 30)
    if failure == "rejected":
        raise Fail("trade_not_tradable", "Backend rejected order: security not tradable at venue", 10)
    if shares > qty - blocked(world, isin):
        raise Fail("trade_input_invalid", "TRADE_INPUT_INVALID: shares exceed sellable quantity (blocked by open orders)")
    conf["used"] = True
    oid = next_id(world, "order")
    t = now(world)
    order = {
        **{k: v for k, v in spec.items() if v is not None},
        "side": "SELL",
        "status": "PENDING",
        "created": iso(t),
        "updated": iso(t),
        "valid_until": iso(t + timedelta(days=360)),
    }
    if a.order_type == "trailing-stop":
        order["stop_price"] = s((bid * (1 - D(a.trail_percent) / 100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    world["orders"][oid] = order
    if a.order_type == "market":
        _fill(world, oid, bid)
    return {
        "result": {"intent": {**spec, "side": "SELL"}, "order_submission": {"submitted": True, "order_id": oid, "is_marketable": a.order_type == "market"}},
        "confirmation": {"id": a.confirm, "consumed": True},
        "next_step": "completed",
    }


def _fill(world: dict[str, Any], oid: str, price: Decimal) -> None:
    o = world["orders"][oid]
    o["status"] = "FILLED"
    o["average_price"] = s(price)
    o["updated"] = iso(now(world))
    h = world["holdings"][o["isin"]]
    h["quantity"] = s(D(h["quantity"]) - D(o["shares"]))
    world["transactions"].append(
        {
            "id": next_id(world, "tx"),
            "summary_type": "BrokerSecurityTransactionSummary",
            "currency": "EUR",
            "type": "SECURITY_TRANSACTION",
            "status": "FILLED",
            "is_cancellation": False,
            "last_event_datetime": iso(now(world)),
            "description": "Sell",
            "isin": o["isin"],
            "security_transaction_type": "SINGLE",
            "quantity": o["shares"],
            "amount": s((D(o["shares"]) * price).quantize(Decimal("0.01"))),
            "side": "SELL",
            "limit_price": None,
            "stop_price": o.get("stop_price"),
        }
    )
    o["amount"] = s((D(o["shares"]) * price).quantize(Decimal("0.01")))


def cmd_trade_cancel(world: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    o = world["orders"].get(a.order_id)
    if o is None or o["status"] not in OPEN:
        raise Fail("broker_input_invalid", "order not cancellable")
    o["status"] = "CANCELLED"
    o["updated"] = iso(now(world))
    return {"result": {"order_id": a.order_id, "accepted": True}}


def sim_move(world: dict[str, Any], isin: str, pct: float) -> list[str]:
    """Bewegt den Kurs und lässt Stops/Trailing-Stops wie beim Broker reagieren."""
    sec = world["securities"][isin]
    bid = D(sec["bid"]) * (1 + D(pct) / 100)
    sec["bid"] = s(bid.quantize(Decimal("0.0001")))
    sec["ask"] = s((bid * Decimal("1.002")).quantize(Decimal("0.0001")))
    log = [f"{sec['name']}: Geldkurs jetzt {sec['bid']}"]
    for oid, o in world["orders"].items():
        if o["isin"] != isin or o["status"] not in OPEN:
            continue
        if o["order_type"] == "trailing-stop":
            new_stop = (bid * (1 - D(o["trail_percent"]) / 100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if new_stop > D(o["stop_price"]):
                o["stop_price"] = s(new_stop)
                o["updated"] = iso(now(world))
                log.append(f"  Trailing-Stop {oid} nachgezogen auf {o['stop_price']}")
        if o.get("stop_price") and bid <= D(o["stop_price"]):
            _fill(world, oid, bid)
            log.append(f"  Stop {oid} AUSGELÖST bei {s(bid)}")
    return log


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sc (fake)")
    p.add_argument("--world", required=False)
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    for name in ("whoami", "capabilities"):
        x = sub.add_parser(name)
        x.add_argument("--json", action="store_true")
    broker = sub.add_parser("broker").add_subparsers(dest="bcmd")

    x = broker.add_parser("holdings")
    x.add_argument("--quote-source")
    x.add_argument("--portfolio-id")
    x.add_argument("--json", action="store_true")
    x = broker.add_parser("quote")
    x.add_argument("--isin", required=True)
    x.add_argument("--quote-source")
    x.add_argument("--portfolio-id")
    x.add_argument("--json", action="store_true")
    x = broker.add_parser("transactions")
    x.add_argument("--page-size", type=int, default=20)
    x.add_argument("--status", action="append")
    x.add_argument("--type-filter", action="append")
    x.add_argument("--isin")
    x.add_argument("--from-time")
    x.add_argument("--cursor")
    x.add_argument("--portfolio-id")
    x.add_argument("--json", action="store_true")
    t = broker.add_parser("transaction").add_subparsers(dest="tcmd")
    x = t.add_parser("details")
    x.add_argument("--transaction-id", required=True)
    x.add_argument("--portfolio-id")
    x.add_argument("--json", action="store_true")
    x = broker.add_parser("order-venues")
    x.add_argument("--json", action="store_true")
    x = broker.add_parser("candlesticks")
    x.add_argument("--isin", required=True)
    x.add_argument("--timeframe", default="one_month")
    x.add_argument("--quote-source")
    x.add_argument("--json", action="store_true")
    pa = broker.add_parser("price-alerts")
    pa.add_argument("--active-only", action="store_true")
    pa.add_argument("--json", action="store_true")
    pas = pa.add_subparsers(dest="sub")
    x = pas.add_parser("add")
    x.add_argument("--isin", required=True)
    x.add_argument("--price", required=True)
    x.add_argument("--json", action="store_true")
    x = pas.add_parser("remove")
    x.add_argument("--alert-id", required=True)
    x.add_argument("--json", action="store_true")
    tr = broker.add_parser("trade").add_subparsers(dest="trcmd")
    x = tr.add_parser("sell")
    x.add_argument("--isin", required=True)
    x.add_argument("--shares", required=True)
    x.add_argument("--order-type", default="market")
    x.add_argument("--stop-price")
    x.add_argument("--limit-price")
    x.add_argument("--trail-percent")
    x.add_argument("--trail-amount")
    x.add_argument("--venue")
    x.add_argument("--confirm")
    x.add_argument("--json", action="store_true")
    x = tr.add_parser("cancel")
    x.add_argument("--order-id", required=True)
    x.add_argument("--portfolio-id")
    x.add_argument("--json", action="store_true")
    sim = sub.add_parser("sim").add_subparsers(dest="simcmd")
    x = sim.add_parser("move")
    x.add_argument("isin")
    x.add_argument("pct", type=float)
    return p


def dispatch(world: dict[str, Any], a: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if a.cmd == "whoami":
        return "whoami", {"user": "demo", "session": {"local_read_only": False}}
    if a.cmd == "capabilities":
        return "capabilities", {"version": "1.3.0-fake", "commands": ["broker.trade.sell", "broker.trade.cancel"], "local_trade_controls": {}}
    if a.cmd == "broker":
        if a.bcmd == "holdings":
            return "broker holdings", cmd_holdings(world, a)
        if a.bcmd == "quote":
            return "broker quote", cmd_quote(world, a)
        if a.bcmd == "transactions":
            return "broker transactions", cmd_transactions(world, a)
        if a.bcmd == "transaction":
            return "broker transaction details", cmd_details(world, a)
        if a.bcmd == "order-venues":
            return "broker order-venues", {"count": 3, "venues": [{"venue": v, "label": v} for v in ("gettex", "eix", "xetra")]}
        if a.bcmd == "candlesticks":
            return "broker candlesticks", cmd_candles(world, a)
        if a.bcmd == "price-alerts":
            return "broker price-alerts", cmd_alerts(world, a)
        if a.bcmd == "trade" and a.trcmd == "sell":
            return "broker trade sell", cmd_trade_sell(world, a)
        if a.bcmd == "trade" and a.trcmd == "cancel":
            return "broker trade cancel", cmd_trade_cancel(world, a)
    raise Fail("invalid_input", "unsupported command in fake sc")


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.version:
        print("sc 1.3.0 (stopguard fake – Demo, kein echter Broker)")
        return 0
    if not a.world:
        print("--world <datei> erforderlich", file=sys.stderr)
        return 1
    path = Path(a.world)
    world = load(path)
    if a.cmd == "sim":
        for line in sim_move(world, a.isin, a.pct):
            print(line)
        save(path, world)
        return 0
    try:
        command, data = dispatch(world, a)
    except Fail as exc:
        print(json.dumps({"ok": False, "command": " ".join(sys.argv[1:3]), "error": {"code": exc.code, "message": exc.message}, "hints": []}))
        save(path, world)
        return exc.exit_code
    save(path, world)
    print(json.dumps({"ok": True, "command": command, "data": data}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
