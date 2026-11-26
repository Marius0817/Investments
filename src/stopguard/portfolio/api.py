"""Lokale, schreibgeschützte JSON-API für eine spätere UI (nur Standardbibliothek).

    stopguard portfolio serve            → http://127.0.0.1:8765/api/v1/summary

Endpunkte (GET):
    /api/v1/health                 Status, Zeitpunkt der Auswertung, letzter Sync
    /api/v1/portfolio              komplettes Dokument (Schema stopguard.portfolio/1)
    /api/v1/<abschnitt>            summary | positions | trades | credit | taxes | income |
                                   costs | allocation | timeseries | periods | data_quality | chart_hints
    /api/v1/trades/<id>            einzelner Trade inkl. Zahlungsströmen
    /api/v1/positions/<isin>       einzelne Position inkl. ihrer Trades und Zeitreihe
Parameter:
    ?refresh=1                     Auswertung neu berechnen (ohne Scalable-Abfrage)
    /api/v1/trades?status=open&origin=single&isin=...&credit=1&sort=-irr
"""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

SECTIONS = (
    "summary", "positions", "trades", "credit", "taxes", "income", "costs",
    "allocation", "timeseries", "periods", "data_quality", "chart_hints", "settings",
    "activities", "insights",
)


class PortfolioCache:
    def __init__(self, compute: Callable[[], dict[str, Any]], ttl_seconds: int = 300):
        self.compute = compute
        self.ttl = ttl_seconds
        self._doc: dict[str, Any] | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if refresh or self._doc is None or time.time() - self._at > self.ttl:
                self._doc = self.compute()
                self._at = time.time()
            return self._doc


def _filter_trades(trades: list[dict[str, Any]], q: dict[str, list[str]]) -> list[dict[str, Any]]:
    out = trades
    if "status" in q:
        wanted = set(q["status"][0].split(","))
        out = [t for t in out if t["status"] in wanted]
    if "isin" in q:
        out = [t for t in out if t["isin"] == q["isin"][0].upper()]
    if "origin" in q:
        wanted = set(q["origin"][0].split(","))
        out = [t for t in out if t.get("origin") in wanted]
    if q.get("credit", ["0"])[0] in ("1", "true"):
        out = [t for t in out if t.get("credit")]
    if "sort" in q:
        key = q["sort"][0]
        rev = key.startswith("-")
        key = key.lstrip("-")
        out = sorted(out, key=lambda t: (t.get(key) is None, t.get(key) or 0), reverse=rev)
        if rev:  # None-Werte trotzdem ans Ende
            out = [t for t in out if t.get(key) is not None] + [t for t in out if t.get(key) is None]
    return out


def make_handler(cache: PortfolioCache, allow_origin: str = "*"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stopguard-api/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # leiser Standard-Log
            pass

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802 – CORS-Preflight
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = parse_qs(url.query)
            parts = [p for p in url.path.split("/") if p]
            if parts[:2] != ["api", "v1"]:
                self._send(404, {"error": "not_found", "hint": "Pfad /api/v1/... verwenden"})
                return
            try:
                doc = cache.get(refresh=q.get("refresh", ["0"])[0] in ("1", "true"))
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "analysis_failed", "message": str(exc)})
                return
            rest = parts[2:]
            if not rest or rest == ["portfolio"]:
                self._send(200, doc)
            elif rest == ["health"]:
                self._send(200, {"ok": True, "schema": doc.get("schema"), "as_of": doc.get("as_of"),
                                 "last_sync_at": (doc.get("data_quality") or {}).get("sources", {}).get("last_sync_at")})
            elif rest[0] == "trades" and len(rest) == 2:
                trade = next((t for t in doc["trades"] if t["id"] == rest[1]), None)
                self._send(200 if trade else 404, trade or {"error": "not_found"})
            elif rest[0] == "trades":
                self._send(200, _filter_trades(doc["trades"], q))
            elif rest[0] == "positions" and len(rest) == 2:
                isin = rest[1].upper()
                pos = next((p for p in doc["positions"] if p["isin"] == isin), None)
                if pos is None:
                    self._send(404, {"error": "not_found"})
                else:
                    self._send(200, {
                        **pos,
                        "trades": [t for t in doc["trades"] if t["isin"] == isin],
                        "timeseries": doc["timeseries"]["positions"].get(isin, []),
                    })
            elif rest[0] in SECTIONS and len(rest) == 1:
                self._send(200, doc.get(rest[0]))
            else:
                self._send(404, {"error": "not_found", "sections": list(SECTIONS)})

    return Handler


def serve(cache: PortfolioCache, host: str = "127.0.0.1", port: int = 8765, allow_origin: str = "*") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(cache, allow_origin))
