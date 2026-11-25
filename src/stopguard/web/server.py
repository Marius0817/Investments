"""HTTP-Server für Weboberfläche und API (nur Standardbibliothek).

Sicherheit
- Standardmäßig nur an 127.0.0.1 gebunden. Andere Adressen nur mit Zugangsschlüssel
  (``STOPGUARD_WEB_TOKEN``) – Anmeldung im Browser, Sitzung per HttpOnly-Cookie (SameSite=Strict).
- Schreibende Aufrufe (POST) verlangen JSON, den Kopf ``X-Stopguard: 1`` und – falls vorhanden –
  einen ``Origin`` gleich dem ``Host`` (Schutz gegen CSRF). CORS wird nicht freigegeben.
- Ohne Zugangsschlüssel werden nur lokale Host-Namen akzeptiert (Schutz gegen DNS-Rebinding).
- Strikte Content-Security-Policy, keine Inline-Skripte, keine externen Quellen.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from stopguard import __version__
from stopguard.config import Config
from stopguard.engine import Engine, apply_alert_actions
from stopguard.models import parse_time
from stopguard.notify import Notifier
from stopguard.portfolio.analysis import analyse_portfolio
from stopguard.portfolio.api import SECTIONS, PortfolioCache, _filter_trades
from stopguard.portfolio.importers import ImportError_, read_trades_csv
from stopguard.portfolio.store import PortfolioStore
from stopguard.portfolio.sync import sync_portfolio
from stopguard.sc import ScClient, ScError, ScNotInstalled
from stopguard.state import LockBusy, StateStore
from stopguard.web.orders import OrderFlow, OrderFlowError, action_dict

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
SESSION_DAYS = 30
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
)

EVENT_LABELS = {
    "order_placed": "Order platziert",
    "order_cancelled": "Order storniert",
    "order_declined": "Order verworfen",
    "order_adopted": "Bestehende Order übernommen",
    "order_filled": "Stop-Order ausgeführt",
    "order_vanished": "Order nicht mehr aktiv",
    "phase_changed": "Neue Phase",
    "strategy_changed": "Strategie geändert",
    "submission_unknown": "Einreichung unklar",
    "submission_rejected": "Order abgelehnt",
    "unresolved_cleared": "Einreichung geklärt",
    "unprotected": "Position ungeschützt",
    "alert_set": "Kursalarm gesetzt",
    "alert_remove": "Kursalarm entfernt",
    "position_closed": "Position geschlossen",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


# ---------------------------------------------------------------------------
# Hintergrundaufgaben (Sync)
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | failed
    step: str = "Wird gestartet …"
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "step": self.step, "log": self.log[-50:],
            "result": self.result, "error": self.error, "started": self.started, "finished": self.finished,
        }


# ---------------------------------------------------------------------------
# Anwendung
# ---------------------------------------------------------------------------

class WebApp:
    def __init__(
        self,
        cfg: Config,
        client: ScClient,
        *,
        token: str | None = None,
        allowed_hosts: set[str] | None = None,
        static_dir: Path = STATIC_DIR,
        notifier: Notifier | None = None,
    ):
        self.cfg = cfg
        self.sc = client
        self.token = token or None
        self.allowed_hosts = {h.lower() for h in (allowed_hosts or set())}
        self.static_dir = static_dir.resolve()
        self.store = StateStore(cfg.state_dir)
        self.notifier = notifier or Notifier(cfg.notify)
        self.orders = OrderFlow(cfg, client, self.store, self.notifier)
        self.portfolio = PortfolioCache(self._compute_portfolio, ttl_seconds=300)
        self._stops: dict[str, Any] | None = None
        self._stops_at = 0.0
        self._stops_lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self._job_lock = threading.Lock()
        self.sessions: dict[str, float] = {}
        self.demo = "stopguard.fake_sc" in " ".join(cfg.scalable.sc_command)

    # ------------------------------------------------------------ Daten
    def _compute_portfolio(self) -> dict[str, Any]:
        store = PortfolioStore(self.cfg.state_dir / "portfolio.db")
        try:
            return analyse_portfolio(store, self.cfg, stop_states=self.store.load().positions)
        finally:
            store.close()

    def stops(self, refresh: bool = False) -> dict[str, Any]:
        with self._stops_lock:
            if self._stops is not None and not refresh and time.time() - self._stops_at < 60:
                return self._stops
            try:
                with self.store.lock(wait=True):
                    state = self.store.load()
                    report = Engine(self.cfg, self.sc, self.store, state).analyse()
                    self.store.save(state)
            except ScError as exc:
                raise ApiError(
                    401 if exc.is_auth_problem else 502,
                    "scalable_auth" if exc.is_auth_problem else "scalable_error",
                    exc.friendly(),
                    hint="Im Terminal 'stopguard login' ausführen und den Login im Browser abschließen." if exc.is_auth_problem else None,
                ) from exc
            except ScNotInstalled as exc:
                raise ApiError(503, "sc_missing", str(exc)) from exc
            self._stops = stops_payload(report, state, self.cfg)
            self._stops_at = time.time()
            return self._stops

    def invalidate(self) -> None:
        self._stops = None
        self.portfolio._doc = None

    def app_info(self) -> dict[str, Any]:
        state = self.store.load()
        login = parse_time(state.login_at)
        pstore_path = self.cfg.state_dir / "portfolio.db"
        last_sync = None
        if pstore_path.exists():
            store = PortfolioStore(pstore_path)
            try:
                last_sync = store.get_meta("last_sync_at")
            finally:
                store.close()
        return {
            "version": __version__,
            "demo": self.demo,
            "config_path": str(self.cfg.source) if self.cfg.source else None,
            "state_dir": str(self.cfg.state_dir),
            "auth": bool(self.token),
            "session": {
                "login_at": state.login_at,
                "expires_at": (login + timedelta(days=7)).isoformat() if login else None,
            },
            "last_run_at": state.last_run_at,
            "last_run_ok": state.last_run_ok,
            "last_sync_at": last_sync,
            "unresolved": [isin for isin, ps in state.positions.items() if ps.unresolved_submission],
            "settings": {
                "venue": self.cfg.scalable.venue,
                "benchmark": {"isin": self.cfg.portfolio.benchmark_isin, "name": self.cfg.portfolio.benchmark_name},
                "credit_mode": self.cfg.credit.mode,
                "tax_method": self.cfg.tax.method,
                "church_tax_pct": self.cfg.tax.church_tax_pct,
                "allowance_eur": self.cfg.tax.allowance_eur,
                "min_change_pct": self.cfg.engine.min_change_pct,
                "only_raise": self.cfg.engine.only_raise,
                "notify_channels": self.cfg.notify.channels,
                "schedule": {
                    "interval_minutes": self.cfg.schedule.interval_minutes,
                    "active_hours": (
                        f"{self.cfg.schedule.active_hours[0]:%H:%M}–{self.cfg.schedule.active_hours[1]:%H:%M}"
                        if self.cfg.schedule.active_hours else "immer"
                    ),
                    "weekdays_only": self.cfg.schedule.weekdays_only,
                },
            },
            "strategies": [
                {
                    "name": st.name,
                    "description": st.description,
                    "phases": [
                        {"name": ph.name, "rule": ph.describe(), "condition": "Start" if i == 0 else ph.activate.describe(), "order_type": ph.order_type}
                        for i, ph in enumerate(st.phases)
                    ],
                }
                for st in sorted(self.cfg.strategies.values(), key=lambda s: s.name)
            ],
        }

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for e in reversed(self.store.read_audit(limit)):
            e = dict(e)
            event = e.pop("event", "")
            at = e.pop("at", None)
            out.append({"at": at, "event": event, "label": EVENT_LABELS.get(event, event), "data": e})
        return out

    # ------------------------------------------------------------ Jobs
    def start_sync(self) -> Job:
        with self._job_lock:
            running = next((j for j in self.jobs.values() if j.kind == "sync" and j.status == "running"), None)
            if running:
                return running
            job = Job(uuid.uuid4().hex[:12], "sync")
            self.jobs[job.id] = job

        def progress(text: str) -> None:
            job.step = text.rstrip(" …") + " …"
            job.log.append(text)

        def run() -> None:
            store = PortfolioStore(self.cfg.state_dir / "portfolio.db")
            try:
                rep = sync_portfolio(self.sc, store, self.cfg, progress=progress)
                job.result = {
                    "transactions": rep.transactions,
                    "details": rep.details,
                    "details_pending": rep.details_pending,
                    "prices": sum(rep.prices.values()),
                    "securities": len(rep.prices),
                    "chart_points": rep.chart_points,
                    "holdings": rep.holdings,
                    "warnings": rep.warnings,
                }
                job.status = "done"
                job.step = "Fertig"
            except ScError as exc:
                job.status, job.error = "failed", exc.friendly()
                job.step = "Abgebrochen"
            except Exception as exc:  # noqa: BLE001
                log.exception("Sync fehlgeschlagen")
                job.status, job.error = "failed", str(exc)
                job.step = "Abgebrochen"
            finally:
                store.close()
                job.finished = time.time()
                self.invalidate()

        threading.Thread(target=run, daemon=True, name=f"sync-{job.id}").start()
        return job

    # ------------------------------------------------------------ Auth
    def check_session(self, cookie_header: str | None, auth_header: str | None) -> bool:
        if not self.token:
            return True
        if auth_header and auth_header.startswith("Bearer "):
            return hmac.compare_digest(auth_header[7:].strip(), self.token)
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sg_session" and value in self.sessions:
                if self.sessions[value] > time.time():
                    return True
                del self.sessions[value]
        return False

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = time.time() + SESSION_DAYS * 86400
        return sid

    def host_allowed(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        if "*" in self.allowed_hosts:
            return True
        host = host_header.lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name in LOOPBACK or host in self.allowed_hosts or name in self.allowed_hosts:
            return True
        return bool(self.token) and not self.allowed_hosts


# ---------------------------------------------------------------------------
# Aufbereitung der Stop-Analyse
# ---------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    return float(v) if v is not None else None


def stops_payload(report, state, cfg: Config) -> dict[str, Any]:
    from stopguard.cli import analysis_dict

    positions = []
    for a in report.analyses:
        d = analysis_dict(a)
        ps = state.position(a.isin)
        d["phases"] = [
            {
                "name": ph.name,
                "rule": ph.describe(),
                "condition": "Start" if i == 0 else ph.activate.describe(),
                "state": "done" if i < a.phase_index else ("current" if i == a.phase_index else "upcoming"),
            }
            for i, ph in enumerate(a.strategy.phases)
        ]
        d["strategy_description"] = a.strategy.description
        d["phase_since"] = ps.phase_since
        d["managed_alert"] = (
            {"alert_id": a.managed_alert.alert_id, "price": _num(a.managed_alert.price)} if a.managed_alert else None
        )
        d["managed_order"] = (
            {
                "order_id": a.managed_order.order_id,
                "description": a.managed_order.describe(),
                "kind": a.managed_order.kind,
                "stop_price": _num(a.managed_order.stop_price),
                "valid_until": a.managed_order.valid_until.isoformat() if a.managed_order.valid_until else None,
            }
            if a.managed_order
            else None
        )
        d["other_orders"] = [o.describe() for o in a.other_sell_orders]
        d["distance"] = a.distance_to_stop
        d["effective_stop"] = _num(a.effective_stop)
        d["target"]["notes"] = a.target.notes
        d["unresolved"] = ps.unresolved_submission
        d["actions"] = [action_dict(x) for x in report.actions if x.isin == a.isin]
        positions.append(d)
    return {
        "at": report.at.isoformat(),
        "portfolio_id": report.portfolio_id,
        "positions": positions,
        "actions": [action_dict(x) for x in report.actions],
        "events": [vars(e) for e in report.events],
        "unresolved": [
            {"isin": isin, "info": ps.unresolved_submission} for isin, ps in state.positions.items() if ps.unresolved_submission
        ],
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"stopguard/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s – %s", self.address_string(), fmt % args)

        # ---------------------------------------------------- Antworten
        def _headers(self, status: int, ctype: str, length: int, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body), {"Cache-Control": "no-store", **(extra or {})})
            self.wfile.write(body)

        def _error(self, err: ApiError) -> None:
            self._json(err.status, {"error": err.code, "message": err.message, **{k: v for k, v in err.extra.items() if v is not None}})

        # ---------------------------------------------------- Routing
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                if not app.host_allowed(self.headers.get("Host")):
                    raise ApiError(421, "host_not_allowed", "Unbekannter Host-Name. Start mit --allowed-host <name> erlauben.")
                url = urlparse(self.path)
                path = url.path
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/favicon.ico":
                    return self._static("img/favicon.svg")
                if not path.startswith("/api/v1/"):
                    raise ApiError(404, "not_found", "Seite nicht gefunden.")
                parts = [p for p in path[len("/api/v1/"):].split("/") if p]
                q = parse_qs(url.query)
                body: dict[str, Any] = {}
                if method == "POST":
                    body = self._read_json()
                if parts == ["session"] and method == "POST":
                    return self._login(body)
                if parts == ["health"] and method == "GET":  # für Container-Healthchecks, verrät nur die Version
                    return self._json(200, {"ok": True, "version": __version__})
                if not app.check_session(self.headers.get("Cookie"), self.headers.get("Authorization")):
                    raise ApiError(401, "login_required", "Bitte mit dem Zugangsschlüssel anmelden.")
                return self._api(method, parts, q, body)
            except ApiError as err:
                self._error(err)
            except OrderFlowError as err:
                self._json(err.status, {"error": err.code, "message": err.message, **err.extra})
            except LockBusy as err:
                self._error(ApiError(409, "busy", str(err)))
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler bei %s %s: %s\n%s", method, self.path, exc, traceback.format_exc())
                self._error(ApiError(500, "internal_error", f"Interner Fehler: {exc}"))

        def _read_json(self) -> dict[str, Any]:
            if self.headers.get("X-Stopguard") != "1":
                raise ApiError(403, "csrf", "Anfrage ohne X-Stopguard-Kopf abgelehnt.")
            origin = self.headers.get("Origin")
            if origin:
                o = urlparse(origin)
                if (o.netloc or "").lower() != (self.headers.get("Host") or "").lower():
                    raise ApiError(403, "csrf", "Anfrage von fremder Herkunft abgelehnt.")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10 * 1024 * 1024:
                raise ApiError(413, "too_large", "Anfrage zu groß (max. 10 MB).")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            if ctype != "application/json":
                raise ApiError(415, "json_required", "Content-Type application/json erwartet.")
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, "bad_json", "Ungültiges JSON.") from exc
            if not isinstance(data, dict):
                raise ApiError(400, "bad_json", "JSON-Objekt erwartet.")
            return data

        def _login(self, body: dict[str, Any]) -> None:
            if not app.token:
                return self._json(200, {"ok": True, "auth": False})
            given = str(body.get("token") or "")
            if not hmac.compare_digest(given.encode(), app.token.encode()):
                time.sleep(0.5)  # Raten bremsen
                raise ApiError(401, "bad_token", "Der Zugangsschlüssel stimmt nicht.")
            sid = app.new_session()
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            cookie = f"sg_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_DAYS * 86400}{secure}"
            self._json(200, {"ok": True, "auth": True}, {"Set-Cookie": cookie})

        def _static(self, rel: str) -> None:
            target = (app.static_dir / rel).resolve()
            if app.static_dir not in target.parents:  # kein Ausbruch aus dem static-Verzeichnis
                raise ApiError(404, "not_found", "Datei nicht gefunden.")
            if not target.is_file():
                raise ApiError(404, "not_found", "Datei nicht gefunden.")
            stat = target.stat()
            etag = '"' + hashlib.sha1(f"{stat.st_mtime_ns}-{stat.st_size}".encode()).hexdigest()[:16] + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            data = target.read_bytes()
            self._headers(200, ctype, len(data), {"ETag": etag, "Cache-Control": "no-cache"})
            self.wfile.write(data)

        # ---------------------------------------------------- API
        def _api(self, method: str, parts: list[str], q: dict[str, list[str]], body: dict[str, Any]) -> None:
            refresh = q.get("refresh", ["0"])[0] in ("1", "true")
            if method == "GET":
                if parts == ["app"]:
                    return self._json(200, app.app_info())
                if parts == ["health"]:
                    return self._json(200, {"ok": True, "version": __version__})
                if parts == ["stops"]:
                    return self._json(200, app.stops(refresh=refresh))
                if parts == ["history"]:
                    return self._json(200, app.history(int(q.get("limit", ["100"])[0])))
                if parts and parts[0] == "jobs" and len(parts) == 2:
                    job = app.jobs.get(parts[1])
                    if not job:
                        raise ApiError(404, "not_found", "Auftrag nicht gefunden.")
                    return self._json(200, job.to_dict())
                if parts and parts[0] == "orders" and len(parts) == 2:
                    return self._json(200, app.orders.get(parts[1]))
                if parts and parts[0] == "unresolved" and len(parts) == 2:
                    return self._json(200, {"candidates": app.orders.candidates(parts[1].upper())})
                if parts == ["export"]:
                    doc = app.portfolio.get(refresh)
                    body_bytes = json.dumps(doc, ensure_ascii=False, indent=2, default=str).encode("utf-8")
                    self._headers(200, "application/json; charset=utf-8", len(body_bytes), {
                        "Content-Disposition": 'attachment; filename="stopguard-portfolio.json"',
                        "Cache-Control": "no-store",
                    })
                    self.wfile.write(body_bytes)
                    return None
                # Portfolio-Abschnitte (wie 'stopguard portfolio serve')
                doc = app.portfolio.get(refresh)
                if parts == ["portfolio"] or not parts:
                    return self._json(200, doc)
                if parts[0] == "trades" and len(parts) == 2:
                    trade = next((t for t in doc["trades"] if t["id"] == parts[1]), None)
                    if trade is None:
                        raise ApiError(404, "not_found", "Trade nicht gefunden.")
                    return self._json(200, trade)
                if parts[0] == "trades":
                    return self._json(200, _filter_trades(doc["trades"], q))
                if parts[0] == "positions" and len(parts) == 2:
                    isin = parts[1].upper()
                    pos = next((p for p in doc["positions"] if p["isin"] == isin), None)
                    if pos is None:
                        raise ApiError(404, "not_found", "Position nicht gefunden.")
                    return self._json(200, {
                        **pos,
                        "trades": [t for t in doc["trades"] if t["isin"] == isin],
                        "timeseries": doc["timeseries"]["positions"].get(isin, []),
                    })
                if len(parts) == 1 and parts[0] in SECTIONS:
                    return self._json(200, doc.get(parts[0]))
                raise ApiError(404, "not_found", "Unbekannter Endpunkt.")

            # POST
            if parts == ["jobs", "sync"]:
                return self._json(202, app.start_sync().to_dict())
            if parts == ["orders", "preview"]:
                key = str(body.get("key") or "")
                if ":" not in key:
                    raise ApiError(400, "bad_request", "Feld 'key' fehlt.")
                result = app.orders.preview(key, cancel_first=bool(body.get("cancel_first")))
                return self._json(200, result)
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "confirm":
                if body.get("confirm") is not True:
                    raise ApiError(400, "confirmation_required", "Ausdrückliche Bestätigung fehlt (confirm: true).")
                result = app.orders.confirm(parts[1])
                app.invalidate()
                return self._json(200, result)
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "discard":
                return self._json(200, app.orders.discard(parts[1]))
            if len(parts) == 2 and parts[0] == "unresolved":
                result = app.orders.resolve(parts[1].upper(), str(body.get("decision") or ""))
                app.invalidate()
                return self._json(200, result)
            if parts == ["alerts", "sync"]:
                with app.store.lock(wait=True):
                    state = app.store.load()
                    report = Engine(app.cfg, app.sc, app.store, state).analyse()
                    lines = apply_alert_actions(app.sc, state, app.store, report.alert_actions())
                    app.store.save(state)
                app.invalidate()
                return self._json(200, {"lines": lines})
            if parts == ["import"]:
                return self._json(200, self._import(body))
            raise ApiError(404, "not_found", "Unbekannter Endpunkt.")

        def _import(self, body: dict[str, Any]) -> dict[str, Any]:
            import tempfile

            content = body.get("content")
            source = str(body.get("source") or "notion").strip() or "notion"
            if not isinstance(content, str) or not content.strip():
                raise ApiError(400, "bad_request", "Die Datei ist leer.")
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
                fh.write(content)
                tmp = Path(fh.name)
            try:
                rows = read_trades_csv(tmp, source=source, decimal=str(body.get("decimal") or "auto"))
            except ImportError_ as exc:
                raise ApiError(422, "import_failed", f"Die Datei konnte nicht gelesen werden: {exc}") from exc
            finally:
                tmp.unlink(missing_ok=True)
            store = PortfolioStore(app.cfg.state_dir / "portfolio.db")
            try:
                removed = store.delete_manual_lots(source) if body.get("replace") else 0
                n = store.upsert_manual_lots(rows)
            finally:
                store.close()
            app.invalidate()
            mapped = {ic.ticker.upper() for ic in app.cfg.instruments.values() if ic.ticker}
            unmapped = sorted({r["ticker"] for r in rows if r.get("ticker") and not r.get("isin") and r["ticker"].upper() not in mapped})
            return {
                "imported": n,
                "removed": removed,
                "credit": sum(1 for r in rows if r["credit_flag"]),
                "closed": sum(1 for r in rows if r["close_date"]),
                "unmapped_tickers": unmapped,
            }

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Abgebrochene Verbindungen (Tab geschlossen, Neuladen) sind normal – kein Traceback.
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            log.debug("Verbindung von %s abgebrochen: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


def serve_web(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    return _Server((host, port), make_handler(app))


def resolve_token(explicit: str | None) -> str | None:
    return explicit or os.environ.get("STOPGUARD_WEB_TOKEN") or None

