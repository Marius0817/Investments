"""Terminal-Ausgabe: Tabellen, Farben, Rückfragen."""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Any, Iterable, Sequence

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"

CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}


def c(text: str, *styles: str) -> str:
    if not _COLOR or not styles:
        return text
    return "\033[" + ";".join(CODES[s] for s in styles) + "m" + text + "\033[0m"


def _width(text: str) -> int:
    clean = _strip_ansi(text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in clean)


def _strip_ansi(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            i = j + 1 if j != -1 else len(text)
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]], align: str | None = None) -> str:
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [_width(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], _width(v))
    align = align or "l" * len(headers)

    def fmt(cells: Sequence[str]) -> str:
        parts = []
        for i, v in enumerate(cells):
            pad = widths[i] - _width(v)
            parts.append((" " * pad + v) if align[i] == "r" else (v + " " * pad))
        return "  ".join(parts).rstrip()

    lines = [c(fmt(list(headers)), "bold"), c("  ".join("─" * w for w in widths), "dim")]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def shorten(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def ask_yes(prompt: str, *, strict: bool = False) -> bool:
    """Rückfrage im Terminal. ``strict``: nur die ausgeschriebene Antwort „ja“ zählt."""
    if not sys.stdin.isatty():
        print(c("Keine interaktive Konsole – Abbruch (Bestätigung erforderlich).", "red"))
        return False
    hint = "Zum Bestätigen „ja“ eintippen" if strict else "[j/N]"
    try:
        answer = input(f"{prompt} {c(hint, 'dim')}: ").strip().lower()
    except EOFError:
        return False
    if strict:
        return answer == "ja"
    return answer in ("j", "ja", "y", "yes")


def colored_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "–"
    text = f"{value * 100:+.{digits}f}%"
    return c(text, "green" if value >= 0 else "red")
