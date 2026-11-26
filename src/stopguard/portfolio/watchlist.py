"""Watchlist: Wertpapiere beobachten, die (noch) nicht im Depot sind.

Kurse und Verlauf kommen über die offizielle Scalable CLI (``broker quote``,
``broker candlesticks``). Ein Zielkurs kann als Kursalarm bei Scalable hinterlegt
werden – das ist keine Order. Die Liste selbst liegt lokal in
``<state_dir>/watchlist.json``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from stopguard.models import parse_time, utcnow
from stopguard.sc import ScClient, ScError


class WatchlistError(ValueError):
    pass


class WatchlistStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "watchlist.json"
        self._lock = threading.Lock()

    def items(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return [i for i in data.get("items", []) if isinstance(i, dict) and i.get("isin")]

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, isin: str, *, name: str | None = None, note: str | None = None, target_price: float | None = None) -> dict[str, Any]:
        isin = normalise_isin(isin)
        if target_price is not None and target_price <= 0:
            raise WatchlistError("Der Zielkurs muss größer als 0 sein.")
        with self._lock:
            items = self.items()
            item = next((i for i in items if i["isin"] == isin), None)
            if item is None:
                if len(items) >= 100:
                    raise WatchlistError("Die Watchlist ist voll (höchstens 100 Einträge).")
                item = {"isin": isin, "added_at": utcnow().isoformat()}
                items.append(item)
            if name:
                item["name"] = name[:120]
            if note is not None:
                item["note"] = note.strip()[:500]
            item["target_price"] = target_price
            self._write(items)
            return item

    def remove(self, isin: str) -> bool:
        isin = normalise_isin(isin)
        with self._lock:
            items = self.items()
            keep = [i for i in items if i["isin"] != isin]
            if len(keep) == len(items):
                return False
            self._write(keep)
            return True


def normalise_isin(value: str) -> str:
    isin = (value or "").strip().upper().replace(" ", "")
    if len(isin) != 12 or not isin[:2].isalpha() or not isin[2:].isalnum():
        raise WatchlistError("Bitte eine gültige ISIN eingeben (12 Zeichen, z. B. US0378331005).")
    if not _isin_checksum_ok(isin):
        raise WatchlistError(f"{isin} ist keine gültige ISIN (Prüfziffer stimmt nicht).")
    return isin


def _isin_checksum_ok(isin: str) -> bool:
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(isin[-1])


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def enrich(sc: ScClient, items: list[dict[str, Any]], *, held: dict[str, float] | None = None, workers: int = 3) -> dict[str, Any]:
    """Aktuelle Kurse, 1-Jahres-Verlauf und aktive Kursalarme je Eintrag."""
    held = held or {}
    alerts: dict[str, list[dict[str, Any]]] = {}
    alert_error = None
    try:
        for a in sc.price_alerts(active_only=True):
            alerts.setdefault(a.isin, []).append({"id": a.alert_id, "price": float(a.price)})
    except ScError as exc:
        alert_error = exc.friendly()

    def one(item: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {**item, "alerts": alerts.get(item["isin"], []), "held_value": held.get(item["isin"])}
        try:
            q = sc.quote(item["isin"])
            price = float(q.mid or q.bid or 0) or None
            row.update({
                "name": item.get("name") or q.name or item["isin"],
                "price": price,
                "bid": _num(q.bid),
                "ask": _num(q.ask),
                "quote_time": q.timestamp.isoformat() if q.timestamp else None,
                "outdated": q.outdated,
            })
        except ScError as exc:
            row.update({"price": None, "error": exc.friendly()})
            return row
        try:
            candles = sc.candlesticks(item["isin"], "one_year")
        except ScError:
            candles = []
        series = []
        for c in candles:
            t = parse_time(c.get("end_time_utc") or c.get("start_time_utc"))
            close = _num(c.get("close"))
            if t is not None and close:
                series.append((t, close))
        series.sort()
        row["history"] = [[t.date().isoformat(), round(v, 4)] for t, v in series]
        price = row.get("price")
        if series and price:
            closes = [v for _, v in series]
            row["high_52w"] = round(max(closes + [price]), 4)
            row["low_52w"] = round(min(closes + [price]), 4)

            def change(days: int) -> float | None:
                cutoff = series[-1][0] - timedelta(days=days)
                base = next((v for t, v in series if t >= cutoff), None)
                return round(price / base - 1, 5) if base else None

            row["change"] = {"1m": change(30), "3m": change(91), "1y": change(365) if (series[-1][0] - series[0][0]).days > 300 else None}
            row["from_high"] = round(price / row["high_52w"] - 1, 5)
        target = item.get("target_price")
        if target and price:
            row["to_target"] = round(target / price - 1, 5)
            row["target_reached"] = price <= target
        return row

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(one, items))
    return {
        "items": rows,
        "alerts_error": alert_error,
        "fetched_at": utcnow().isoformat(),
        "seconds": round(time.monotonic() - started, 2),
    }


def add_alert(sc: ScClient, isin: str, price: float) -> list[dict[str, Any]]:
    if price <= 0:
        raise WatchlistError("Der Alarmkurs muss größer als 0 sein.")
    isin = normalise_isin(isin)
    alerts = sc.add_price_alert(isin, Decimal(str(round(price, 4))))
    return [{"id": a.alert_id, "price": float(a.price)} for a in alerts if a.isin == isin]
