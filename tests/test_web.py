"""Weboberfläche: Sicherheit (Host, CSRF, Token, Pfade) und der zweistufige Order-Ablauf über die API."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from stopguard.portfolio.store import PortfolioStore
from stopguard.portfolio.sync import sync_portfolio
from stopguard.web.server import WebApp, serve_web

from test_engine import APPLE, NVDA
from test_portfolio import NOTION_CSV


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None

    def request(self, method, path, body=None, headers=None, host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            h["X-Stopguard"] = "1"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        ctype = res.getheader("Content-Type") or ""
        parsed = json.loads(payload) if payload and ctype.startswith("application/json") else payload
        return res.status, parsed, res

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body={} if body is None else body, **kw)


def _start(app: WebApp):
    server = serve_web(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Client(server.server_address[1])


@pytest.fixture()
def web(demo):
    store = PortfolioStore(demo.cfg.state_dir / "portfolio.db")
    try:
        sync_portfolio(demo.client, store, demo.cfg, sleep=lambda s: None)
    finally:
        store.close()
    app = WebApp(demo.cfg, demo.client, notifier=demo.notifier)
    server, client = _start(app)
    yield app, client
    server.shutdown()
    server.server_close()


@pytest.fixture()
def web_token(demo):
    app = WebApp(demo.cfg, demo.client, token="geheim-123", notifier=demo.notifier)
    server, client = _start(app)
    yield app, client
    server.shutdown()
    server.server_close()


# ------------------------------------------------------------------ Auslieferung & Sicherheit

def test_index_static_and_security_headers(web):
    _, c = web
    status, body, res = c.get("/")
    assert status == 200 and b'id="main"' in body
    csp = res.getheader("Content-Security-Policy")
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert res.getheader("X-Content-Type-Options") == "nosniff"
    assert res.getheader("Referrer-Policy") == "no-referrer"

    status, _, res = c.get("/static/js/main.js")
    assert status == 200 and res.getheader("Content-Type").startswith("text/javascript")
    etag = res.getheader("ETag")
    status, _, _ = c.get("/static/js/main.js", headers={"If-None-Match": etag})
    assert status == 304


@pytest.mark.parametrize("path", ["/static/../server.py", "/static/../../cli.py", "/static/js/../../../web/server.py", "/static/nope.js"])
def test_static_cannot_escape_directory(web, path):
    _, c = web
    status, body, _ = c.get(path)
    assert status == 404
    assert b"import" not in (body if isinstance(body, bytes) else b"")


def test_unknown_host_is_rejected(web):
    _, c = web
    status, body, _ = c.get("/api/v1/health", host="evil.example")
    assert status == 421 and body["error"] == "host_not_allowed"
    assert c.get("/api/v1/health", host="localhost:8765")[0] == 200


def test_host_allowlist_rules(demo):
    open_app = WebApp(demo.cfg, demo.client)
    assert open_app.host_allowed("127.0.0.1:8765") and open_app.host_allowed("[::1]:8765")
    assert not open_app.host_allowed("raspberrypi.local:8765")  # ohne Token nur Loopback
    with_token = WebApp(demo.cfg, demo.client, token="x")
    assert with_token.host_allowed("raspberrypi.local:8765")
    pinned = WebApp(demo.cfg, demo.client, token="x", allowed_hosts={"schmiede.local"})
    assert pinned.host_allowed("schmiede.local:8765") and not pinned.host_allowed("other.local")


def test_post_requires_csrf_header_same_origin_and_json(web):
    _, c = web
    status, body, _ = c.request("POST", "/api/v1/alerts/sync", raw=b"{}", headers={"Content-Type": "application/json"})
    assert status == 403 and body["error"] == "csrf"
    status, body, _ = c.post("/api/v1/alerts/sync", headers={"Origin": "http://evil.example"})
    assert status == 403 and body["error"] == "csrf"
    status, body, _ = c.request(
        "POST", "/api/v1/import", raw=b"a,b", headers={"X-Stopguard": "1", "Content-Type": "text/plain"}
    )
    assert status == 415


def test_token_login_sets_strict_httponly_cookie(web_token):
    _, c = web_token
    status, body, _ = c.get("/api/v1/app")
    assert status == 401 and body["error"] == "login_required"
    # Statische Dateien (Anmeldemaske) bleiben erreichbar
    assert c.get("/")[0] == 200
    assert c.post("/api/v1/session", {"token": "falsch"})[0] == 401
    status, _, res = c.post("/api/v1/session", {"token": "geheim-123"})
    assert status == 200
    cookie = res.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    c.cookie = cookie.split(";")[0]
    assert c.get("/api/v1/app")[0] == 200
    # Bearer-Token für Skripte
    c.cookie = None
    assert c.get("/api/v1/app", headers={"Authorization": "Bearer geheim-123"})[0] == 200
    assert c.get("/api/v1/app", headers={"Authorization": "Bearer nein"})[0] == 401
    # Healthcheck ohne Anmeldung (nur Version)
    status, body, _ = c.get("/api/v1/health")
    assert status == 200 and set(body) == {"ok", "version"}


# ------------------------------------------------------------------ Daten

def test_app_portfolio_and_stops_endpoints(web):
    _, c = web
    status, info, _ = c.get("/api/v1/app")
    assert status == 200 and info["demo"] is True and info["auth"] is False
    status, doc, _ = c.get("/api/v1/portfolio")
    assert status == 200 and doc["schema"] == "stopguard.portfolio/1"
    trade_id = doc["trades"][0]["id"]
    assert c.get(f"/api/v1/trades/{trade_id}")[1]["id"] == trade_id
    assert c.get("/api/v1/trades/gibt-es-nicht")[0] == 404
    status, stops, _ = c.get("/api/v1/stops")
    assert status == 200
    keys = {a["key"] for a in stops["actions"] if a["needs_confirmation"]}
    assert f"place:{NVDA}" in keys and f"replace:{APPLE}" in keys
    nv = next(p for p in stops["positions"] if p["isin"] == NVDA)
    assert [p["state"] for p in nv["phases"]][0] == "current"
    status, _, res = c.get("/api/v1/export")
    assert status == 200 and "attachment" in res.getheader("Content-Disposition")


def test_import_endpoint(web):
    _, c = web
    status, res, _ = c.post("/api/v1/import", {"content": NOTION_CSV, "source": "notion"})
    assert status == 200 and res["imported"] == 2 and res["credit"] == 1 and res["closed"] == 1
    assert "ABC.DE" in res["unmapped_tickers"]
    assert c.post("/api/v1/import", {"content": "  "})[0] == 400


# ------------------------------------------------------------------ Order-Ablauf

def _orders(demo):
    return dict(demo.world()["orders"])


def test_preview_never_places_and_confirm_needs_explicit_flag(web, demo):
    app, c = web
    before = _orders(demo)
    status, prev, _ = c.post("/api/v1/orders/preview", {"key": f"place:{NVDA}"})
    assert status == 200 and prev["status"] == "preview"
    assert prev["presentation"]["sections"]  # vollständige Vorschau von Scalable
    assert _orders(demo) == before  # Phase 1 legt nichts an

    assert c.get(f"/api/v1/orders/{prev['id']}")[1]["id"] == prev["id"]
    # Ohne ausdrückliches confirm:true passiert nichts
    for body in ({}, {"confirm": "true"}, {"confirm": 1}):
        status, err, _ = c.post(f"/api/v1/orders/{prev['id']}/confirm", body)
        assert status == 400 and err["error"] == "confirmation_required"
    assert _orders(demo) == before

    status, res, _ = c.post(f"/api/v1/orders/{prev['id']}/confirm", {"confirm": True})
    assert status == 200 and res["status"] == "placed"
    new = {k: v for k, v in _orders(demo).items() if k not in before}
    assert len(new) == 1 and next(iter(new.values()))["isin"] == NVDA
    assert app.store.load().positions[NVDA].managed_order_id == res["order_id"]
    assert any(e["event"] == "order_placed" and e["data"].get("via") == "web" for e in c.get("/api/v1/history")[1])
    # Eine Vorschau lässt sich nur einmal bestätigen
    assert c.post(f"/api/v1/orders/{prev['id']}/confirm", {"confirm": True})[0] in (404, 410)
    # Die Empfehlung ist danach weg
    keys = {a["key"] for a in c.get("/api/v1/stops?refresh=1")[1]["actions"]}
    assert f"place:{NVDA}" not in keys


def test_replace_asks_to_cancel_first_and_discard_reports_unprotected(web, demo):
    _, c = web
    status, res, _ = c.post("/api/v1/orders/preview", {"key": f"replace:{APPLE}"})
    assert status == 200 and res["status"] == "needs_cancel_first"
    assert demo.world()["orders"]["order-legacy-1"]["status"] == "PENDING"  # nichts storniert ohne Zustimmung

    status, prev, _ = c.post("/api/v1/orders/preview", {"key": f"replace:{APPLE}", "cancel_first": True})
    assert status == 200 and prev["status"] == "preview" and prev["cancelled_first"] is True
    assert demo.world()["orders"]["order-legacy-1"]["status"] == "CANCELLED"

    status, res, _ = c.post(f"/api/v1/orders/{prev['id']}/discard")
    assert status == 200 and res["unprotected"] is True


def test_unknown_action_key_is_rejected(web):
    _, c = web
    status, err, _ = c.post("/api/v1/orders/preview", {"key": "place:DE0000000000"})
    assert status == 409 and err["error"] == "action_gone"
    assert c.post("/api/v1/orders/preview", {"key": ""})[0] == 400
