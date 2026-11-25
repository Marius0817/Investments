"""Kurshistorie mit „letzter bekannter Kurs“-Abfrage."""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta


class PriceBook:
    def __init__(self, series: dict[str, list[tuple[str, float]]], current: dict[str, float] | None = None):
        self._dates: dict[str, list[str]] = {}
        self._closes: dict[str, list[float]] = {}
        for isin, points in series.items():
            pts = sorted(points)
            self._dates[isin] = [d for d, _ in pts]
            self._closes[isin] = [c for _, c in pts]
        self._current = dict(current or {})

    def has(self, isin: str) -> bool:
        return bool(self._dates.get(isin)) or isin in self._current

    def series_count(self) -> int:
        return sum(1 for d in self._dates.values() if d)

    def first_date(self, isin: str) -> str | None:
        d = self._dates.get(isin)
        return d[0] if d else None

    def at(self, isin: str, when: datetime, *, tolerance_days: int = 10) -> float | None:
        """Letzter Schlusskurs am oder vor ``when`` (kurz vor Beginn der Historie: erster Kurs)."""
        dates = self._dates.get(isin)
        if not dates:
            return None
        key = when.date().isoformat()
        i = bisect.bisect_right(dates, key)
        if i == 0:
            first = datetime.fromisoformat(dates[0])
            if (first.date() - when.date()) <= timedelta(days=tolerance_days):
                return self._closes[isin][0]
            return None
        return self._closes[isin][i - 1]

    def current(self, isin: str) -> float | None:
        if isin in self._current:
            return self._current[isin]
        closes = self._closes.get(isin)
        return closes[-1] if closes else None
