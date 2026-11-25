"""SQLite-Speicher für Rohdaten (Transaktionen, Details, Kurse, Depotverlauf, Importe).

Die Datenbank ist bewusst einfach gehalten und dokumentiert (docs/portfolio-data.md),
damit eine spätere UI sie bei Bedarf auch direkt lesen kann.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Rohdaten aus 'sc broker transactions' (+ Details aus 'sc broker transaction details')
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    time TEXT,
    summary_type TEXT,
    type TEXT,
    status TEXT,
    isin TEXT,
    side TEXT,
    quantity REAL,
    amount REAL,
    cash_type TEXT,
    description TEXT,
    raw TEXT NOT NULL,
    details TEXT,
    details_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tx_time ON transactions(time);
CREATE INDEX IF NOT EXISTS ix_tx_isin ON transactions(isin);

-- Tagesschlusskurse (aus 'sc broker candlesticks')
CREATE TABLE IF NOT EXISTS prices (
    isin TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (isin, date)
);

-- Depotverlauf laut Scalable ('sc broker chart')
CREATE TABLE IF NOT EXISTS portfolio_chart (
    timestamp TEXT PRIMARY KEY,
    valuation REAL,
    absolute_return REAL
);

-- aktueller Bestand ('sc broker holdings')
CREATE TABLE IF NOT EXISTS holdings (
    isin TEXT PRIMARY KEY,
    name TEXT,
    quantity REAL,
    fifo_price REAL,
    mid_price REAL,
    security_type TEXT,
    updated_at TEXT
);

-- tägliche Momentaufnahmen (eigene Historie, unabhängig von Scalable-Charts)
CREATE TABLE IF NOT EXISTS position_snapshots (
    date TEXT NOT NULL,
    isin TEXT NOT NULL,
    units REAL,
    price REAL,
    value REAL,
    PRIMARY KEY (date, isin)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    date TEXT PRIMARY KEY,
    cash_balance REAL,
    loaned REAL,
    available_credit_line REAL,
    buying_power REAL,
    securities_value REAL,
    raw TEXT
);

-- importierte Trades (Notion-Export, CSV, andere Broker)
CREATE TABLE IF NOT EXISTS manual_lots (
    id TEXT PRIMARY KEY,
    source TEXT,
    isin TEXT,
    ticker TEXT,
    name TEXT,
    open_date TEXT,
    open_price REAL,
    units REAL,
    fees REAL,
    close_date TEXT,
    close_price REAL,
    dividends REAL,
    current_price REAL,
    credit_flag INTEGER,
    credit_amount REAL,
    raw TEXT
);
"""


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


class PortfolioStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ----------------------------------------------------- transactions
    def upsert_transactions(self, items: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.tx() as c:
            for it in items:
                n += 1
                c.execute(
                    """INSERT INTO transactions(id, time, summary_type, type, status, isin, side, quantity, amount,
                                                cash_type, description, raw)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET time=excluded.time, status=excluded.status, raw=excluded.raw,
                           quantity=excluded.quantity, amount=excluded.amount, description=excluded.description""",
                    (
                        str(it.get("id")),
                        it.get("last_event_datetime"),
                        it.get("summary_type"),
                        it.get("type"),
                        it.get("status"),
                        it.get("isin") or it.get("related_isin"),
                        it.get("side"),
                        _num(it.get("quantity")),
                        _num(it.get("amount")),
                        it.get("cash_transaction_type") or it.get("non_trade_security_transaction_type"),
                        it.get("description"),
                        json.dumps(it, ensure_ascii=False),
                    ),
                )
        return n

    def set_details(self, tx_id: str, details: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE transactions SET details = ?, details_fetched_at = ? WHERE id = ?",
                (json.dumps(details, ensure_ascii=False), datetime.now().isoformat(), tx_id),
            )

    def transactions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT raw, details FROM transactions ORDER BY time").fetchall()
        out = []
        for r in rows:
            item = json.loads(r["raw"])
            item["_details"] = json.loads(r["details"]) if r["details"] else None
            out.append(item)
        return out

    def transactions_missing_details(self, kinds: tuple[str, ...]) -> list[str]:
        q = ",".join("?" * len(kinds))
        rows = self.conn.execute(
            f"""SELECT id FROM transactions WHERE details IS NULL AND status IN ('FILLED','SETTLED','CONFIRMED','PARTIAL_FILLED')
                AND (summary_type = 'BrokerSecurityTransactionSummary' OR cash_type IN ({q})) ORDER BY time""",
            kinds,
        ).fetchall()
        return [r["id"] for r in rows]

    def last_transaction_time(self) -> str | None:
        row = self.conn.execute("SELECT MAX(time) AS t FROM transactions").fetchone()
        return row["t"] if row else None

    # ------------------------------------------------------------ prices
    def upsert_prices(self, isin: str, points: Iterable[tuple[str, float]]) -> int:
        n = 0
        with self.tx() as c:
            for d, close in points:
                n += 1
                c.execute(
                    "INSERT INTO prices(isin, date, close) VALUES(?,?,?) ON CONFLICT(isin, date) DO UPDATE SET close = excluded.close",
                    (isin, d, close),
                )
        return n

    def prices(self) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = {}
        for r in self.conn.execute("SELECT isin, date, close FROM prices ORDER BY isin, date"):
            out.setdefault(r["isin"], []).append((r["date"], r["close"]))
        return out

    def price_range(self, isin: str) -> tuple[str | None, str | None]:
        r = self.conn.execute("SELECT MIN(date) a, MAX(date) b FROM prices WHERE isin = ?", (isin,)).fetchone()
        return (r["a"], r["b"]) if r else (None, None)

    # -------------------------------------------------------- chart/holdings
    def upsert_chart(self, points: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.tx() as c:
            for p in points:
                n += 1
                c.execute(
                    """INSERT INTO portfolio_chart(timestamp, valuation, absolute_return) VALUES(?,?,?)
                       ON CONFLICT(timestamp) DO UPDATE SET valuation=excluded.valuation, absolute_return=excluded.absolute_return""",
                    (p.get("timestamp_utc"), _num(p.get("valuation")), _num(p.get("absolute_return"))),
                )
        return n

    def chart(self) -> list[tuple[str, float, float]]:
        return [
            (r["timestamp"], r["valuation"], r["absolute_return"])
            for r in self.conn.execute("SELECT * FROM portfolio_chart WHERE valuation IS NOT NULL ORDER BY timestamp")
        ]

    def replace_holdings(self, rows: Iterable[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM holdings")
            for h in rows:
                c.execute(
                    "INSERT INTO holdings(isin, name, quantity, fifo_price, mid_price, security_type, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (h["isin"], h.get("name"), h.get("quantity"), h.get("fifo_price"), h.get("mid_price"), h.get("security_type"), _iso(datetime.now())),
                )

    def holdings(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM holdings ORDER BY name")]

    def snapshot(self, day: str, positions: Iterable[dict[str, Any]], account: dict[str, Any] | None) -> None:
        with self.tx() as c:
            for p in positions:
                c.execute(
                    """INSERT INTO position_snapshots(date, isin, units, price, value) VALUES(?,?,?,?,?)
                       ON CONFLICT(date, isin) DO UPDATE SET units=excluded.units, price=excluded.price, value=excluded.value""",
                    (day, p["isin"], p.get("units"), p.get("price"), p.get("value")),
                )
            if account is not None:
                c.execute(
                    """INSERT INTO account_snapshots(date, cash_balance, loaned, available_credit_line, buying_power, securities_value, raw)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET cash_balance=excluded.cash_balance, loaned=excluded.loaned,
                       available_credit_line=excluded.available_credit_line, buying_power=excluded.buying_power,
                       securities_value=excluded.securities_value, raw=excluded.raw""",
                    (
                        day,
                        _num(account.get("cash_balance")),
                        _num(account.get("loaned")),
                        _num(account.get("available_credit_line")),
                        _num(account.get("buying_power")),
                        _num(account.get("securities_value")),
                        json.dumps(account, ensure_ascii=False, default=str),
                    ),
                )

    def account_snapshots(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM account_snapshots ORDER BY date")]

    def latest_account(self) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM account_snapshots ORDER BY date DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------- manual lots
    def upsert_manual_lots(self, lots: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.tx() as c:
            for lot in lots:
                n += 1
                c.execute(
                    """INSERT INTO manual_lots(id, source, isin, ticker, name, open_date, open_price, units, fees, close_date,
                                               close_price, dividends, current_price, credit_flag, credit_amount, raw)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET isin=excluded.isin, ticker=excluded.ticker, name=excluded.name,
                         open_date=excluded.open_date, open_price=excluded.open_price, units=excluded.units, fees=excluded.fees,
                         close_date=excluded.close_date, close_price=excluded.close_price, dividends=excluded.dividends,
                         current_price=excluded.current_price, credit_flag=excluded.credit_flag,
                         credit_amount=excluded.credit_amount, raw=excluded.raw""",
                    (
                        lot["id"], lot.get("source", "csv"), lot.get("isin"), lot.get("ticker"), lot.get("name"),
                        _iso(lot.get("open_date")), lot.get("open_price"), lot.get("units"), lot.get("fees", 0.0),
                        _iso(lot.get("close_date")), lot.get("close_price"), lot.get("dividends"), lot.get("current_price"),
                        1 if lot.get("credit_flag") else 0, lot.get("credit_amount"),
                        json.dumps(lot.get("raw") or {}, ensure_ascii=False, default=str),
                    ),
                )
        return n

    def manual_lots(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM manual_lots ORDER BY open_date")]

    def delete_manual_lots(self, source: str) -> int:
        with self.tx() as c:
            return c.execute("DELETE FROM manual_lots WHERE source = ?", (source,)).rowcount


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, dict):
        v = v.get("amount", v.get("value"))
        if v is None:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
