"""Import bestehender Trade-Listen: Notion-Datenbank-Export (CSV) und generisches CSV.

Notion-Spalten (Datenbank „Trades“): Name Investment, Ticker, Open, Open (€), Close,
Close (€), Units, Dividends (€), Current (€), Kreditfinanziert?
Export in Notion: ••• → Export → Markdown & CSV → die CSV-Datei der Datenbank verwenden.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Spaltenzuordnung: interner Name → mögliche Spaltennamen (klein geschrieben)
COLUMNS = {
    "name": ["name investment", "name", "investment", "wertpapier", "titel"],
    "ticker": ["ticker", "symbol"],
    "isin": ["isin"],
    "open_date": ["open", "kaufdatum", "buy date", "open date", "datum kauf"],
    "open_price": ["open (€)", "open (eur)", "kaufkurs", "buy price", "open price"],
    "units": ["units", "stück", "stueck", "anzahl", "quantity", "shares"],
    "fees": ["fees", "gebühren", "gebuehren", "kosten"],
    "close_date": ["close", "verkaufsdatum", "sell date", "close date", "datum verkauf"],
    "close_price": ["close (€)", "close (eur)", "verkaufskurs", "sell price", "close price"],
    "dividends": ["dividends (€)", "dividends", "dividenden", "dividenden (€)", "ausschüttungen"],
    "current_price": ["current (€)", "current", "aktueller kurs", "kurs"],
    "credit_flag": ["kreditfinanziert?", "kreditfinanziert", "credit", "kredit", "leveraged"],
    "credit_amount": ["kreditbetrag", "credit amount", "kredit (€)"],
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "okt": 10,
    "nov": 11, "dec": 12, "dez": 12,
}


class ImportError_(ValueError):
    pass


def parse_number(text: Any, decimal: str = "point") -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(" ", "").replace(" ", "")
    s = re.sub(r"(€|EUR|eur|\$|%)", "", s)
    if not s or s in ("-", "–"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if decimal == "comma":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def detect_decimal(values: list[str]) -> str:
    point = comma = 0
    for v in values:
        v = (v or "").strip()
        if re.search(r"\d\.\d{1,2}$|\d\.\d{4,}$", v):
            point += 1
        if re.search(r"\d,\d{1,2}$|\d,\d{4,}$", v):
            comma += 1
    return "comma" if comma > point else "point"


def parse_date(text: Any) -> datetime | None:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    s = re.split(r"\s*(?:→|->)\s*", s)[0]  # Zeitraum „a → b“: Start nehmen
    s = re.sub(r"\s+\d{1,2}:\d{2}(\s*(AM|PM))?.*$", "", s, flags=re.IGNORECASE)  # Uhrzeit abschneiden
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            pass
    m = re.match(r"([A-Za-zäÄ]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s)  # July 21, 2026
    if m and m.group(1).lower() in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)), 12, tzinfo=timezone.utc)
    m = re.match(r"(\d{1,2})\.?\s+([A-Za-zäÄ]+)\.?\s+(\d{4})", s)  # 21. Juli 2026
    if m and m.group(2).lower() in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)), 12, tzinfo=timezone.utc)
    return None


def parse_bool(text: Any) -> bool:
    return str(text or "").strip().lower() in ("yes", "ja", "true", "1", "x", "✓", "✔", "__yes__", "wahr")


def _lot_id(source: str, row: dict[str, Any]) -> str:
    key = "|".join(str(row.get(k) or "") for k in ("name", "ticker", "isin", "open_date", "units", "open_price"))
    return f"{source}-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def read_trades_csv(path: Path, *, source: str = "notion", decimal: str = "auto") -> list[dict[str, Any]]:
    raw = Path(path).read_bytes().decode("utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    if not reader.fieldnames:
        raise ImportError_("CSV ohne Kopfzeile")
    header = {h.strip().lower(): h for h in reader.fieldnames if h}
    mapping: dict[str, str] = {}
    for key, options in COLUMNS.items():
        for opt in options:
            if opt in header:
                mapping[key] = header[opt]
                break
    missing = [k for k in ("open_date", "open_price", "units") if k not in mapping]
    if missing:
        raise ImportError_(
            f"Pflichtspalten fehlen: {', '.join(missing)} (gefunden: {', '.join(reader.fieldnames)})"
        )
    rows = list(reader)
    if decimal == "auto":
        numeric_cols = [mapping[k] for k in ("open_price", "close_price", "units", "dividends", "current_price") if k in mapping]
        decimal = detect_decimal([r.get(c, "") for r in rows for c in numeric_cols])

    out: list[dict[str, Any]] = []
    for r in rows:

        def get(key: str, row: dict[str, str] = r) -> str | None:
            return row.get(mapping[key]) if key in mapping else None

        item: dict[str, Any] = {
            "source": source,
            "name": (get("name") or "").strip() or None,
            "ticker": (get("ticker") or "").strip() or None,
            "isin": ((get("isin") or "").strip().upper() or None),
            "open_date": parse_date(get("open_date")),
            "open_price": parse_number(get("open_price"), decimal),
            "units": parse_number(get("units"), decimal),
            "fees": parse_number(get("fees"), decimal) or 0.0,
            "close_date": parse_date(get("close_date")),
            "close_price": parse_number(get("close_price"), decimal),
            "dividends": parse_number(get("dividends"), decimal) or 0.0,
            "current_price": parse_number(get("current_price"), decimal),
            "credit_flag": parse_bool(get("credit_flag")),
            "credit_amount": parse_number(get("credit_amount"), decimal),
            "raw": dict(r),
        }
        if item["open_date"] is None and item["units"] is None and item["open_price"] is None:
            continue  # leere Zeile
        if item["close_date"] is None:
            item["close_price"] = None
        item["id"] = _lot_id(source, item)
        out.append(item)
    return out
