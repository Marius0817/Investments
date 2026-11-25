"""Dünner, robuster Wrapper um die offizielle Scalable CLI (`sc ... --json`).

Referenz: https://github.com/ScalableCapital/scalable-cli (public-docs/*.md)

Wichtige Eigenschaften der CLI, die hier berücksichtigt werden:
- Jede Ausgabe mit ``--json`` ist ein Umschlag ``{"ok": bool, "command", "data" | "error", "hints"}``.
- Exit-Codes: 10 Validierung, 20 Auth/Konfiguration, 30 Netzwerk/Backend, 1 sonstige.
- ``refresh_retryable`` (Exit 30) darf mit demselben Befehl wiederholt werden.
- ``rate_limited`` wird NICHT automatisch wiederholt – wir warten nur bei Lesebefehlen.
- Orders sind zweistufig: Vorschau (Phase 1) → ausdrückliche Bestätigung → Phase 2 mit ``--confirm``.
  Phase 2 wird von diesem Modul nie selbstständig ausgelöst (siehe executor.py).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from stopguard.models import (
    Holding,
    OpenOrder,
    PriceAlert,
    Quote,
    Transaction,
    fmt_decimal,
    fmt_price,
    parse_time,
    to_decimal,
)

log = logging.getLogger(__name__)

AUTH_CODES = {
    "no_session",
    "refresh_relogin_required",
    "auth_grant_not_enabled",
    "session_changed",
    "secret_storage_unavailable",
}
OPEN_ORDER_STATUSES = ("CREATED", "REQUESTED", "PENDING", "PARTIAL_FILLED", "CANCEL_REQUESTED")


class ScError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int, command: str, hints: list[str] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.command = command
        self.hints = hints or []

    @property
    def is_auth_problem(self) -> bool:
        return self.code in AUTH_CODES or self.exit_code == 20

    @property
    def is_device_locked(self) -> bool:
        return self.code == "device_locked"

    @property
    def is_rate_limited(self) -> bool:
        return self.code == "rate_limited"

    @property
    def submission_outcome_unknown(self) -> bool:
        """Bei Phase 2: Ergebnis unklar → NICHT wiederholen, erst Transaktionen prüfen.

        Laut Scalable-Doku gilt das auch für Rate-Limits („check trade outcomes before
        retrying submissions“). Nur Validierungsfehler (Exit 10) und Auth-Fehler (Exit 20)
        gelten als sicher nicht eingereicht.
        """
        if self.code in ("order_submission_failed", "network_error", "internal_error", "timeout", "trade_response_invalid"):
            return True
        return self.exit_code not in (10, 20)

    def friendly(self) -> str:
        text = f"{self.message} [{self.code}]"
        if self.hints:
            text += " – " + " ".join(self.hints)
        return text


class ScNotInstalled(RuntimeError):
    pass


@dataclass
class OrderSpec:
    """Eine Verkaufsorder, wie sie an ``sc broker trade sell`` übergeben wird."""

    isin: str
    shares: Decimal
    order_type: str  # market | stop | stop-limit | trailing-stop | limit
    stop_price: Decimal | None = None
    limit_price: Decimal | None = None
    trail_percent: Decimal | None = None
    venue: str | None = None

    def args(self) -> list[str]:
        a = ["broker", "trade", "sell", "--isin", self.isin, "--shares", fmt_decimal(self.shares), "--order-type", self.order_type]
        if self.order_type in ("stop", "stop-limit") and self.stop_price is not None:
            a += ["--stop-price", fmt_decimal(self.stop_price)]
        if self.order_type in ("limit", "stop-limit") and self.limit_price is not None:
            a += ["--limit-price", fmt_decimal(self.limit_price)]
        if self.order_type == "trailing-stop" and self.trail_percent is not None:
            a += ["--trail-percent", fmt_decimal(self.trail_percent)]
        if self.venue:
            a += ["--venue", self.venue]
        return a

    def describe(self) -> str:
        if self.order_type == "trailing-stop":
            return f"Trailing-Stop {fmt_decimal(self.trail_percent or Decimal(0))}% · {fmt_decimal(self.shares)} Stk."
        if self.order_type == "stop-limit":
            return (
                f"Stop-Limit {fmt_price(self.stop_price)}/{fmt_price(self.limit_price)}"
                f" · {fmt_decimal(self.shares)} Stk."
            )
        if self.order_type == "stop":
            return f"Stop {fmt_price(self.stop_price)} · {fmt_decimal(self.shares)} Stk."
        if self.order_type == "market":
            return f"Market-Verkauf · {fmt_decimal(self.shares)} Stk."
        return f"{self.order_type} · {fmt_decimal(self.shares)} Stk."

    def to_dict(self) -> dict[str, Any]:
        return {
            "isin": self.isin,
            "shares": fmt_decimal(self.shares),
            "order_type": self.order_type,
            "stop_price": fmt_decimal(self.stop_price) if self.stop_price is not None else None,
            "limit_price": fmt_decimal(self.limit_price) if self.limit_price is not None else None,
            "trail_percent": fmt_decimal(self.trail_percent) if self.trail_percent is not None else None,
            "venue": self.venue,
        }


@dataclass
class ScClient:
    command: list[str]
    quote_source: str = "consolidated"
    timeout: int = 90
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    read_retries: int = 2
    calls: list[list[str]] = field(default_factory=list)

    # ------------------------------------------------------------------ core
    def run(self, args: list[str], *, read_only: bool = True) -> Any:
        full = [*self.command, *args, "--json"]
        attempts = self.read_retries + 1 if read_only else 1
        last_error: ScError | None = None
        for attempt in range(attempts):
            self.calls.append(list(args))
            try:
                proc = self.runner(full, capture_output=True, text=True, timeout=self.timeout)
            except FileNotFoundError as exc:
                raise ScNotInstalled(
                    f"Scalable CLI nicht gefunden ({self.command[0]}). Installation: "
                    "brew tap ScalableCapital/tap && brew install scalable-cli"
                ) from exc
            except subprocess.TimeoutExpired:
                last_error = ScError("timeout", f"Zeitüberschreitung nach {self.timeout}s", 30, " ".join(args))
                if not read_only:
                    raise last_error from None
                continue
            envelope = self._parse_envelope(proc.stdout)
            if envelope is None:
                stderr = (proc.stderr or "").strip().splitlines()
                raise ScError(
                    "unparseable_output",
                    f"Keine JSON-Antwort von sc (exit {proc.returncode}): {stderr[-1] if stderr else proc.stdout[:200]}",
                    proc.returncode or 1,
                    " ".join(args),
                )
            if envelope.get("ok"):
                return envelope.get("data")
            err = envelope.get("error") or {}
            last_error = ScError(
                str(err.get("code", "unknown")),
                str(err.get("message", "")),
                proc.returncode,
                str(envelope.get("command", " ".join(args))),
                list(envelope.get("hints") or []),
            )
            if last_error.code == "refresh_retryable" and attempt + 1 < attempts:
                time.sleep(2)
                continue
            if read_only and last_error.is_rate_limited and attempt + 1 < attempts:
                wait = self._retry_after(last_error.message) or 20 * (attempt + 1)
                log.warning("Rate-Limit erreicht – warte %ss", wait)
                time.sleep(wait)
                continue
            raise last_error
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_envelope(stdout: str) -> dict[str, Any] | None:
        for line in reversed((stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and "ok" in data:
                    return data
        try:
            data = json.loads(stdout)
            return data if isinstance(data, dict) and "ok" in data else None
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _retry_after(message: str) -> int | None:
        m = re.search(r"retry after (\d+)", message, re.IGNORECASE)
        return min(int(m.group(1)), 300) if m else None

    # ------------------------------------------------------------- identity
    def version(self) -> str:
        try:
            proc = self.runner([*self.command, "--version"], capture_output=True, text=True, timeout=30)
        except FileNotFoundError as exc:
            raise ScNotInstalled(f"Scalable CLI nicht gefunden ({self.command[0]})") from exc
        return (proc.stdout or proc.stderr).strip()

    def whoami(self) -> dict[str, Any]:
        return self.run(["whoami"])

    def capabilities(self) -> dict[str, Any]:
        return self.run(["capabilities"])

    def order_venues(self) -> list[str]:
        data = self.run(["broker", "order-venues"])
        return [str(v.get("venue")) for v in (data or {}).get("venues", [])]

    # ---------------------------------------------------------------- reads
    def holdings(self) -> tuple[list[Holding], str | None]:
        data = self.run(["broker", "holdings", "--quote-source", self.quote_source])
        items = []
        for it in data.get("items", []):
            if it.get("kind", "security") != "security" or not it.get("isin"):
                continue
            qty = to_decimal(it.get("quantity"))
            if qty is None or qty <= 0:
                continue
            items.append(
                Holding(
                    isin=str(it["isin"]),
                    name=str(it.get("name") or it["isin"]),
                    quantity=qty,
                    blocked_quantity=to_decimal(it.get("blocked_quantity")) or Decimal(0),
                    pending_quantity=to_decimal(it.get("pending_quantity")) or Decimal(0),
                    fifo_price=to_decimal(it.get("fifo_price")),
                    valuation=to_decimal(it.get("valuation")),
                    mid_price=to_decimal(it.get("quote_mid_price")),
                    currency=it.get("quote_currency") or it.get("valuation_currency"),
                    quote_outdated=it.get("quote_is_outdated"),
                    security_type=it.get("security_type"),
                )
            )
        portfolio_id = data.get("portfolio_id")
        return items, str(portfolio_id) if portfolio_id is not None else None

    def quote(self, isin: str) -> Quote:
        data = self.run(["broker", "quote", "--isin", isin, "--quote-source", self.quote_source])
        return Quote(
            isin=isin,
            bid=to_decimal(data.get("quote_bid_price")),
            ask=to_decimal(data.get("quote_ask_price")),
            mid=to_decimal(data.get("quote_mid_price")),
            timestamp=parse_time(data.get("quote_timestamp_utc")),
            outdated=data.get("quote_is_outdated"),
            venue=data.get("quote_venue"),
            currency=data.get("quote_currency"),
            name=data.get("name"),
        )

    def transactions(
        self,
        *,
        statuses: tuple[str, ...] = (),
        isin: str | None = None,
        from_time: str | None = None,
        max_pages: int = 50,
    ) -> list[Transaction]:
        result: list[Transaction] = []
        cursor: str | None = None
        for _ in range(max_pages):
            args = ["broker", "transactions", "--page-size", "100"]
            for s in statuses:
                args += ["--status", s]
            if isin:
                args += ["--isin", isin]
            if from_time:
                args += ["--from-time", from_time]
            if cursor:
                args += ["--cursor", cursor]
            data = self.run(args)
            items = data.get("items", [])
            result.extend(Transaction.from_json(it) for it in items)
            cursor = data.get("cursor")
            if not cursor or not items:
                break
        return result

    def transaction_details(self, transaction_id: str) -> dict[str, Any]:
        return self.run(["broker", "transaction", "details", "--transaction-id", transaction_id])

    def open_orders(self) -> list[OpenOrder]:
        """Offene Wertpapierorders inkl. Details (Ordertyp, Trailing-Info, Gültigkeit)."""
        orders: list[OpenOrder] = []
        for tx in self.transactions(statuses=OPEN_ORDER_STATUSES):
            if not tx.is_open_order or not tx.isin:
                continue
            try:
                details = self.transaction_details(tx.id)
            except ScError as exc:
                log.warning("Details für Order %s nicht abrufbar: %s", tx.id, exc.friendly())
                details = {}
            orders.append(order_from_details(tx, details))
        return orders

    def price_alerts(self, active_only: bool = True) -> list[PriceAlert]:
        args = ["broker", "price-alerts"] + (["--active-only"] if active_only else [])
        data = self.run(args)
        return [_alert(it) for it in (data or {}).get("items", []) if it.get("isin")]

    def add_price_alert(self, isin: str, price: Decimal) -> list[PriceAlert]:
        data = self.run(["broker", "price-alerts", "add", "--isin", isin, "--price", fmt_decimal(price)], read_only=False)
        return [_alert(it, isin) for it in (data or {}).get("items", [])]

    def remove_price_alert(self, alert_id: str) -> None:
        self.run(["broker", "price-alerts", "remove", "--alert-id", alert_id], read_only=False)

    def cash_breakdown(self) -> dict[str, Any]:
        """Cash, Kaufkraft, Kreditlinie (``available_credit_line``) und Kredit (``loaned``)."""
        return self.run(["broker", "cash-breakdown"]) or {}

    def portfolio_chart(self, timeframe: str = "max") -> list[dict[str, Any]]:
        """Historische Depotbewertung: Punkte mit ``valuation``, ``absolute_return``, ``timestamp_utc``."""
        data = self.run(["broker", "chart", "--timeframe", timeframe]) or {}
        return list(data.get("data_points") or [])

    def candlesticks(self, isin: str, timeframe: str = "one_year") -> list[dict[str, Any]]:
        data = self.run(["broker", "candlesticks", "--isin", isin, "--timeframe", timeframe, "--quote-source", self.quote_source])
        return list((data or {}).get("candlesticks", []))

    # --------------------------------------------------------------- trades
    def trade_preview(self, spec: OrderSpec) -> dict[str, Any]:
        """Phase 1: nur Vorschau – legt KEINE Order an."""
        return self.run(spec.args(), read_only=False)

    def trade_submit(self, spec: OrderSpec, confirmation_id: str) -> dict[str, Any]:
        """Phase 2: verbindliche Order. Nur nach ausdrücklicher Bestätigung aufrufen."""
        return self.run([*spec.args(), "--confirm", confirmation_id], read_only=False)

    def trade_cancel(self, order_id: str) -> dict[str, Any]:
        return self.run(["broker", "trade", "cancel", "--order-id", order_id], read_only=False)


def _alert(it: dict[str, Any], isin: str | None = None) -> PriceAlert:
    return PriceAlert(
        alert_id=str(it.get("alert_id")),
        isin=str(it.get("isin") or isin),
        price=to_decimal(it.get("price")) or Decimal(0),
        is_active=bool(it.get("is_active", True)),
        direction=it.get("direction"),
        triggered=parse_time(it.get("triggered_timestamp_utc")),
    )


def order_from_details(tx: Transaction, details: dict[str, Any]) -> OpenOrder:
    trade = (details or {}).get("security_trade") or {}
    trailing = trade.get("trailing_stop_info") or None
    stop = to_decimal(trade.get("stop_price")) if trade else None
    if stop is None:
        stop = tx.stop_price
    limit = to_decimal(trade.get("limit_price")) if trade else None
    if limit is None:
        limit = tx.limit_price
    if trailing:
        kind = "trailing-stop"
        stop = to_decimal(trailing.get("stop_price")) or stop
    elif stop is not None and limit is not None:
        kind = "stop-limit"
    elif stop is not None:
        kind = "stop"
    elif limit is not None:
        kind = "limit"
    else:
        kind = "market" if trade else "unknown"
    shares = trade.get("number_of_shares") or {}
    qty = to_decimal(shares.get("total")) if isinstance(shares, dict) else to_decimal(shares)
    return OpenOrder(
        order_id=tx.id,
        isin=tx.isin or "",
        side=(trade.get("side") or tx.side or "").upper(),
        status=(trade.get("status") or tx.status or "").upper(),
        quantity=qty if qty is not None else tx.quantity,
        stop_price=stop,
        limit_price=limit,
        kind=kind,
        trail_type=(trailing or {}).get("trail_type"),
        trail_offset=to_decimal((trailing or {}).get("trail_offset")),
        valid_until=parse_time(trade.get("valid_until")),
        venue=trade.get("trading_venue"),
        created=tx.time,
    )
