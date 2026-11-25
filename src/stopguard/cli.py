"""Kommandozeile von stopguard."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import subprocess
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from stopguard import __version__, ui
from stopguard.config import Config, ConfigError, load_config
from stopguard.engine import Engine, Event, PositionAnalysis, Report, apply_alert_actions
from stopguard.executor import Executor, resolve_unresolved
from stopguard.models import fmt_decimal, fmt_price, parse_time, utcnow
from stopguard.notify import Notifier
from stopguard.sc import ScClient, ScError, ScNotInstalled
from stopguard.state import LockBusy, State, StateStore

log = logging.getLogger("stopguard")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_AUTH = 0, 1, 2, 20
SESSION_MAX_DAYS = 7


# ---------------------------------------------------------------------------
# Kontext
# ---------------------------------------------------------------------------

class Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg: Config = load_config(Path(args.config) if args.config else None)
        self.store = StateStore(self.cfg.state_dir)
        self.client = ScClient(
            command=self.cfg.scalable.sc_command,
            quote_source=self.cfg.scalable.quote_source,
            timeout=self.cfg.scalable.timeout_seconds,
        )
        self.notifier = Notifier(self.cfg.notify, dry_run=getattr(args, "dry_run", False))

    def engine(self, state: State) -> Engine:
        return Engine(self.cfg, self.client, self.store, state)


def _isins(args: argparse.Namespace) -> set[str] | None:
    values = getattr(args, "isin", None)
    return {v.upper() for v in values} if values else None


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------

def _stop_text(a: PositionAnalysis) -> str:
    t = a.target
    if t.stop is None:
        return ui.c("kein Stop", "dim")
    if t.order_type == "trailing-stop":
        return f"Trail {fmt_decimal(t.trail_percent or Decimal(0))}% (≈{fmt_price(t.stop)})"
    return fmt_price(t.stop)


def _order_text(a: PositionAnalysis) -> str:
    if a.cfg.execution == "alert":
        return f"Alarm {fmt_decimal(a.managed_alert.price)}" if a.managed_alert else ui.c("kein Alarm", "dim")
    if a.managed_order:
        return a.managed_order.describe()
    if a.protective_orders:
        return f"{len(a.protective_orders)} Stop-Orders"
    return ui.c("keine", "yellow") if a.cfg.execution == "order" and a.target.stop is not None else "–"


def _action_text(a: PositionAnalysis, report: Report) -> str:
    acts = [x for x in report.actions if x.isin == a.isin]
    if acts:
        labels = {
            "place": "anlegen",
            "replace": "anpassen",
            "cancel": "stornieren",
            "sell_now": "VERKAUFEN?",
            "alert_set": "Alarm setzen",
            "alert_remove": "Alarm weg",
        }
        text = ", ".join(labels.get(x.kind, x.kind) for x in acts)
        return ui.c(text, "red" if any(x.urgent for x in acts) else "yellow", "bold")
    if a.breach:
        return ui.c("STOP ERREICHT", "red", "bold")
    if a.cfg.execution == "watch":
        return ui.c("beobachten", "dim")
    return ui.c("ok", "green")


def print_status(report: Report, *, show_issues: bool = True) -> None:
    rows = []
    for a in report.analyses:
        rows.append(
            [
                ui.shorten(a.name, 30),
                fmt_decimal(a.holding.quantity),
                fmt_price(a.price),
                ui.colored_pct(a.gain),
                ui.colored_pct(a.returns.irr) + ("*" if a.returns.quality != "exact" else ""),
                ui.shorten(f"{a.phase_index + 1}/{len(a.strategy.phases)} {a.phase.name}", 22),
                _stop_text(a),
                _order_text(a),
                ui.colored_pct(a.distance_to_stop) if a.distance_to_stop is not None else "–",
                _action_text(a, report),
            ]
        )
    print(
        ui.table(
            ["Papier", "Stk.", "Kurs", "G/V", "IRR p.a.", "Phase", "Soll-Stop", "Order/Alarm", "Abstand", "Aktion"],
            rows,
            align="lrrrrlllrl",
        )
    )
    if any(a.returns.quality != "exact" for a in report.analyses):
        print(ui.c("* IRR genähert (Historie unvollständig) – Details mit 'stopguard explain <ISIN>'", "dim"))
    if show_issues:
        for a in report.analyses:
            for issue in a.issues:
                print(ui.c(f"  ⓘ {ui.shorten(a.name, 30)}: {issue}", "dim"))
    for e in report.events:
        if e.level != "info":
            color = "red" if e.level == "urgent" else "yellow"
            print(ui.c(f"  ⚠ {e.title}: {e.message}", color))


def print_plan(report: Report) -> None:
    acts = report.actions
    if not acts:
        print(ui.c("✔ Keine Änderungen nötig – alle Schutzorders sind aktuell.", "green"))
        return
    print(ui.c(f"\n{len(acts)} empfohlene Maßnahme(n):", "bold"))
    for i, a in enumerate(acts, start=1):
        auto = "" if a.needs_confirmation else ui.c(" (automatisch)", "dim")
        print(ui.c(f"  {i}. {a.name} – {a.headline()}{auto}", "bold", "red" if a.urgent else "cyan"))
        print(f"     Grund: {a.reason}")
        if a.current:
            print(f"     Ist:   {a.current}")
        if a.spec:
            print(f"     Neu:   {a.spec.describe()} · {a.spec.venue}")
    if report.order_actions():
        print(ui.c("\n→ Ausführen mit: stopguard apply   (jede Order wird einzeln mit Vorschau bestätigt)", "bold"))


def analysis_dict(a: PositionAnalysis) -> dict[str, Any]:
    return {
        "isin": a.isin,
        "name": a.name,
        "quantity": fmt_decimal(a.holding.quantity),
        "price": fmt_decimal(a.price) if a.price is not None else None,
        "cost_price": fmt_decimal(a.holding.fifo_price) if a.holding.fifo_price is not None else None,
        "gain": a.gain,
        "irr": a.returns.irr,
        "irr_quality": a.returns.quality,
        "total_return": a.returns.total_return,
        "holding_days": round(a.returns.holding_days, 1),
        "strategy": a.strategy.name,
        "execution": a.cfg.execution,
        "phase": {"index": a.phase_index, "name": a.phase.name, "rule": a.phase.describe(), "reason": a.activation_reason},
        "high_water_mark": fmt_decimal(a.high_water_mark) if a.high_water_mark is not None else None,
        "target": {
            "stop": fmt_decimal(a.target.stop) if a.target.stop is not None else None,
            "order_type": a.target.order_type,
            "trail_percent": fmt_decimal(a.target.trail_percent) if a.target.trail_percent is not None else None,
            "components": [{"key": c.key, "price": round(c.price, 4), "label": c.label} for c in a.target.components],
        },
        "order": a.managed_order.describe() if a.managed_order else None,
        "order_id": a.managed_order.order_id if a.managed_order else None,
        "breach": a.breach,
        "issues": a.issues,
    }


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

def _analyse(ctx: Context, state: State, *, isins=None) -> Report:
    return ctx.engine(state).analyse(isins=isins)


def cmd_status(ctx: Context) -> int:
    with ctx.store.lock(wait=True):
        state = ctx.store.load()
        report = _analyse(ctx, state, isins=_isins(ctx.args))
        ctx.store.save(state)
    if ctx.args.json:
        print(
            json.dumps(
                {
                    "at": report.at.isoformat(),
                    "portfolio_id": report.portfolio_id,
                    "positions": [analysis_dict(a) for a in report.analyses],
                    "actions": [
                        {"kind": x.kind, "isin": x.isin, "reason": x.reason, "spec": x.spec.to_dict() if x.spec else None}
                        for x in report.actions
                    ],
                    "events": [vars(e) for e in report.events],
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return EXIT_OK
    print(ui.c(f"stopguard · Depot {report.portfolio_id or '–'} · {report.at.astimezone():%d.%m.%Y %H:%M}", "bold"))
    print_status(report)
    if ctx.args.plan:
        print_plan(report)
    elif report.actions:
        print(ui.c(f"\n{len(report.actions)} Maßnahme(n) empfohlen → 'stopguard plan' / 'stopguard apply'", "yellow"))
    return EXIT_OK


def cmd_plan(ctx: Context) -> int:
    ctx.args.plan = True
    ctx.args.json = False
    return cmd_status(ctx)


def cmd_explain(ctx: Context) -> int:
    isin = ctx.args.isin_pos.upper()
    with ctx.store.lock(wait=True):
        state = ctx.store.load()
        report = _analyse(ctx, state, isins={isin})
        ctx.store.save(state)
    if not report.analyses:
        print(f"{isin} ist nicht im Depot oder nicht konfiguriert (engine.unlisted_positions).")
        return EXIT_ERROR
    a = report.analyses[0]
    r = a.returns
    print(ui.c(f"{a.name} ({a.isin})", "bold"))
    print(f"  Bestand {fmt_decimal(a.holding.quantity)} Stk. · Kurs ({ctx.cfg.engine.price_basis}) {fmt_decimal(a.price) if a.price else '–'}"
          f" · Einstand {fmt_decimal(a.holding.fifo_price) if a.holding.fifo_price else '–'}")
    print(ui.c("\nZahlungsreihe (Basis der IRR)", "bold"))
    rows = [[f.when.astimezone().strftime("%d.%m.%Y"), f.kind, f"{f.amount:,.2f}", f"{f.shares_delta:g}"] for f in r.flows]
    if a.price is not None:
        rows.append([report.at.astimezone().strftime("%d.%m.%Y"), "Bewertung", f"{r.market_value:,.2f}", ""])
    print(ui.table(["Datum", "Art", "Betrag EUR", "Stk."], rows, align="llrr"))
    print(f"\n  Datenqualität: {r.quality}  ·  Haltedauer {r.holding_days:.0f} Tage")
    print(f"  Investiert {r.invested:,.2f} · Rückflüsse {r.received:,.2f} · Wert {r.market_value:,.2f}")
    print(f"  Gesamtrendite {ui.colored_pct(r.total_return)} · IRR {ui.colored_pct(r.irr, 2)} p.a.")
    for n in r.notes:
        print(ui.c(f"  ⓘ {n}", "dim"))

    print(ui.c(f"\nStrategie „{a.strategy.name}“", "bold") + (f" – {a.strategy.description}" if a.strategy.description else ""))
    for i, ph in enumerate(a.strategy.phases):
        marker = ui.c("▶", "green") if i == a.phase_index else " "
        cond = "immer (Startphase)" if i == 0 else ph.activate.describe()
        print(f"  {marker} {i + 1}. {ph.name}: {ph.describe()}")
        print(ui.c(f"       Aktivierung: {cond}{'' if ph.sticky else ' (nicht dauerhaft)'}", "dim"))
    ps = state.position(a.isin)
    if ps.phase_since:
        since = parse_time(ps.phase_since)
        print(f"  Aktuelle Phase seit {since.astimezone():%d.%m.%Y %H:%M}" + (f" – Auslöser: {ps.activation_reason}" if ps.activation_reason else ""))
    if a.high_water_mark is not None:
        print(f"  Hochpunkt seit Phasenbeginn: {fmt_decimal(a.high_water_mark)}")

    print(ui.c("\nStop-Berechnung (wirksam ist der höchste Wert)", "bold"))
    if not a.target.components:
        print("  keine Stop-Komponente aktiv")
    for comp in sorted(a.target.components, key=lambda c: -c.price):
        print(f"  {comp.price:10.2f}  {comp.label}")
    print(f"  → Soll-Stop: {_stop_text(a)}" + (f" · Ordertyp {a.target.order_type}" if a.target.order_type else ""))
    for n in a.target.notes:
        print(ui.c(f"  ⓘ {n}", "dim"))
    print(ui.c("\nIst-Zustand", "bold"))
    print(f"  {_order_text(a)}")
    for o in a.other_sell_orders:
        print(f"  weitere Order: {o.describe()} ({o.order_id})")
    acts = [x for x in report.actions if x.isin == a.isin]
    for x in acts:
        print(ui.c(f"  → {x.headline()}: {x.reason}", "yellow"))
    return EXIT_OK


def cmd_apply(ctx: Context) -> int:
    with ctx.store.lock(wait=True):
        state = ctx.store.load()
        report = _analyse(ctx, state, isins=_isins(ctx.args))
        print_status(report, show_issues=False)
        alert_lines = apply_alert_actions(ctx.client, state, ctx.store, report.alert_actions())
        for line in alert_lines:
            print(ui.c(f"  ✔ {line}", "green"))
        actions = report.order_actions()
        if not actions:
            ctx.store.save(state)
            print(ui.c("\n✔ Keine Order-Änderungen nötig.", "green"))
            return EXIT_OK
        print(ui.c(f"\n{len(actions)} Order-Änderung(en). Jede Order wird mit der offiziellen Scalable-Vorschau "
                   "angezeigt und erst nach deiner ausdrücklichen Bestätigung („ja“) platziert.", "bold"))
        executor = Executor(ctx.client, ctx.store, state, ctx.notifier)
        try:
            result = executor.apply(actions)
        finally:
            ctx.store.save(state)
    print(ui.c("\nZusammenfassung", "bold"))
    for line in result.done:
        print(ui.c(f"  ✔ {line}", "green"))
    for line in result.skipped:
        print(ui.c(f"  – übersprungen: {line}", "dim"))
    for line in result.failed:
        print(ui.c(f"  ✖ {line}", "red"))
    for line in result.unprotected:
        print(ui.c(f"  ⚠ {line}", "red", "bold"))
    return EXIT_ERROR if (result.failed or result.unprotected) else EXIT_OK


def _session_events(state: State) -> list[Event]:
    events: list[Event] = []
    login = parse_time(state.login_at)
    if login is not None:
        expires = login + timedelta(days=SESSION_MAX_DAYS)
        left = expires - utcnow()
        if left <= timedelta(days=1):
            events.append(
                Event(
                    "warning",
                    "Scalable-Login läuft ab",
                    f"Die CLI-Sitzung endet spätestens {expires.astimezone():%d.%m. %H:%M}. Bitte 'stopguard login' ausführen.",
                    f"session-expiry:{state.login_at}",
                )
            )
    return events


def _notify_events(ctx: Context, state: State, events: list[Event]) -> int:
    sent = 0
    hours = ctx.cfg.notify.remind_after_hours
    for e in events:
        remind = hours / 4 if e.level == "urgent" else hours
        if not state.should_notify(e.key, remind):
            continue
        ctx.notifier.send(e.title, e.message, e.level)
        state.mark_notified(e.key)
        sent += 1
    # alte Einträge aufräumen
    cutoff = utcnow() - timedelta(days=30)
    for key, at in list(state.notified.items()):
        t = parse_time(at)
        if t is not None and t < cutoff:
            del state.notified[key]
    return sent


def keepalive(ctx: Context) -> int:
    """Außerhalb der Handelszeiten nur die Session am Leben halten.

    Die Scalable CLI beendet eine Session nach 24 h ohne authentifizierte Anfrage
    (Lesezugriffe zählen). Ohne Keepalive wäre z. B. nach jedem Wochenende ein neuer
    Login nötig. Das Maximum von 7 Tagen ab Login lässt sich dadurch nicht verlängern.
    """
    with ctx.store.lock():
        state = ctx.store.load()
        seen = [t for t in (parse_time(state.last_keepalive_at), parse_time(state.last_run_at)) if t is not None]
        if seen and utcnow() - max(seen) < timedelta(hours=ctx.cfg.schedule.keepalive_hours):
            return EXIT_OK
        try:
            ctx.client.order_venues()
        except ScError as exc:
            if exc.is_device_locked:
                return EXIT_OK
            if exc.is_auth_problem:
                _notify_events(
                    ctx,
                    state,
                    [Event("warning", "Scalable-Login erforderlich", f"{exc.friendly()} → 'stopguard login' ausführen.", "auth-required")],
                )
                ctx.store.save(state)
                return EXIT_AUTH
            log.warning("Keepalive fehlgeschlagen: %s", exc.friendly())
            return EXIT_ERROR
        state.last_keepalive_at = utcnow().isoformat()
        _notify_events(ctx, state, _session_events(state))
        ctx.store.save(state)
    log.info("Außerhalb der Handelszeit – Session-Keepalive ausgeführt")
    return EXIT_OK


def run_once(ctx: Context, *, force: bool = False) -> int:
    if not force and not _in_active_window(ctx):
        return keepalive(ctx)
    with ctx.store.lock():
        state = ctx.store.load()
        state.last_run_at = utcnow().isoformat()
        try:
            report = _analyse(ctx, state)
        except ScError as exc:
            if exc.is_device_locked:
                log.info("Mac ist gesperrt – Secure-Enclave-Schlüssel nicht nutzbar, Lauf übersprungen.")
                ctx.store.save(state)
                return EXIT_OK
            state.last_run_ok = False
            if exc.is_auth_problem:
                _notify_events(
                    ctx,
                    state,
                    [Event("warning", "Scalable-Login erforderlich", f"{exc.friendly()} → 'stopguard login' ausführen.", "auth-required")],
                )
                ctx.store.save(state)
                return EXIT_AUTH
            _notify_events(ctx, state, [Event("warning", "stopguard: Fehler beim Abruf", exc.friendly(), f"run-error:{exc.code}")])
            ctx.store.save(state)
            return EXIT_ERROR
        except ScNotInstalled as exc:
            log.error("%s", exc)
            return EXIT_CONFIG

        events = list(report.events) + _session_events(state)
        for line in apply_alert_actions(ctx.client, state, ctx.store, report.alert_actions()):
            log.info("%s", line)
        order_actions = report.order_actions()
        if order_actions:
            sig = hashlib.sha256(
                "|".join(sorted(f"{a.kind}:{a.isin}:{a.spec.order_type if a.spec else ''}" for a in order_actions)).encode()
            ).hexdigest()[:12]
            lines = [f"• {a.name}: {a.headline()} – {a.reason}" for a in order_actions]
            events.append(
                Event(
                    "urgent" if any(a.urgent for a in order_actions) else "warning",
                    f"{len(order_actions)} Order-Anpassung(en) empfohlen",
                    "\n".join(lines) + "\n→ 'stopguard apply' ausführen",
                    f"actions:{sig}",
                )
            )
        sent = _notify_events(ctx, state, events)
        state.last_run_ok = True
        ctx.store.save(state)
        _auto_portfolio_sync(ctx)
    log.info(
        "Lauf ok: %d Positionen, %d Order-Maßnahmen, %d Benachrichtigungen",
        len(report.analyses),
        len(order_actions),
        sent,
    )
    if not ctx.args.quiet:
        print_status(report)
    return EXIT_OK


def _auto_portfolio_sync(ctx: Context) -> None:
    """Portfolio-Datenbank im Hintergrund aktuell halten (Fehler brechen den Lauf nicht ab)."""
    hours = ctx.cfg.portfolio.auto_sync_hours
    if not hours:
        return
    from stopguard.portfolio.store import PortfolioStore
    from stopguard.portfolio.sync import sync_portfolio

    try:
        store = PortfolioStore(ctx.cfg.state_dir / "portfolio.db")
    except Exception as exc:  # noqa: BLE001
        log.warning("Portfolio-Datenbank nicht verfügbar: %s", exc)
        return
    try:
        last = parse_time(store.get_meta("last_sync_at"))
        if last is not None and utcnow() - last < timedelta(hours=hours):
            return
        rep = sync_portfolio(ctx.client, store, ctx.cfg, max_details=100)
        log.info("Portfolio-Sync: %d Transaktionen, %d Details (offen %d)", rep.transactions, rep.details, rep.details_pending)
    except ScError as exc:
        log.warning("Portfolio-Sync fehlgeschlagen: %s", exc.friendly())
    except Exception as exc:  # noqa: BLE001
        log.warning("Portfolio-Sync fehlgeschlagen: %s", exc)
    finally:
        store.close()


def cmd_run(ctx: Context) -> int:
    try:
        return run_once(ctx, force=ctx.args.force)
    except LockBusy:
        log.info("Anderer stopguard-Prozess aktiv (z. B. 'apply') – Lauf übersprungen.")
        return EXIT_OK


def _in_active_window(ctx: Context) -> bool:
    sch = ctx.cfg.schedule
    try:
        from zoneinfo import ZoneInfo

        local = datetime.now(ZoneInfo(sch.timezone))
    except Exception:  # noqa: BLE001
        local = datetime.now()
    if sch.weekdays_only and local.weekday() >= 5:
        return False
    if sch.active_hours is None:
        return True
    start, end = sch.active_hours
    return start <= local.time() <= end


def cmd_daemon(ctx: Context) -> int:
    interval = max(5, ctx.cfg.schedule.interval_minutes) * 60
    log.info("stopguard daemon gestartet – Intervall %s Min.", interval // 60)
    ctx.args.quiet = True
    while True:
        try:
            run_once(ctx)  # außerhalb des Zeitfensters nur Keepalive
        except LockBusy:
            log.info("Anderer stopguard-Prozess aktiv (z. B. 'apply') – Lauf übersprungen.")
        except Exception as exc:  # noqa: BLE001 – Daemon soll weiterlaufen
            log.exception("Lauf fehlgeschlagen: %s", exc)
        if ctx.args.once:
            return EXIT_OK
        time.sleep(interval)


def cmd_resolve(ctx: Context) -> int:
    with ctx.store.lock():
        state = ctx.store.load()
        try:
            return resolve_unresolved(ctx.client, ctx.store, state)
        finally:
            ctx.store.save(state)


def cmd_login(ctx: Context) -> int:
    print("Starte 'sc login' (OAuth-Device-Login). Bitte den Login selbst im Browser abschließen.")
    proc = subprocess.run([*ctx.cfg.scalable.sc_command, "login", *(["--local-read-only"] if ctx.args.read_only else [])])
    if proc.returncode == 0:
        with ctx.store.lock(wait=True):
            state = ctx.store.load()
            state.login_at = utcnow().isoformat()
            state.notified.pop("auth-required", None)
            ctx.store.save(state)
        print(ui.c(f"✔ Angemeldet. Spätestens in {SESSION_MAX_DAYS} Tagen ist ein neuer Login nötig.", "green"))
    return proc.returncode


def cmd_doctor(ctx: Context) -> int:
    ok = True

    def check(label: str, fn) -> Any:
        nonlocal ok
        try:
            value = fn()
            print(ui.c("  ✔ ", "green") + f"{label}: {value}")
            return value
        except Exception as exc:  # noqa: BLE001
            ok = False
            msg = exc.friendly() if isinstance(exc, ScError) else str(exc)
            print(ui.c("  ✖ ", "red") + f"{label}: {msg}")
            return None

    print(ui.c("stopguard doctor", "bold"))
    check("Python", lambda: f"{platform.python_version()} ({sys.executable})")
    check("Konfiguration", lambda: str(ctx.cfg.source or "keine Datei – Standardwerte"))
    check("Zustandsverzeichnis", lambda: (ctx.store.ensure_dir(), str(ctx.store.dir))[1])
    check("Scalable CLI", ctx.client.version)
    who = check("Session (sc whoami)", lambda: "aktiv" if ctx.client.whoami() is not None else "?")
    if who:
        check("Fähigkeiten", lambda: ", ".join(c for c in ctx.client.capabilities().get("commands", []) if "trade" in c) or "keine Trade-Befehle")
        check("Handelsplätze", lambda: ", ".join(ctx.client.order_venues()) or "keine")
        check("Depot", lambda: f"{len(ctx.client.holdings()[0])} Positionen")
    for isin, p in ctx.cfg.positions.items():
        check(f"Position {isin}", lambda p=p: f"Strategie {p.strategy}, Modus {p.execution}, Schutz {p.protect_pct:g}%")
    if "ntfy" in ctx.cfg.notify.channels and not ctx.cfg.notify.ntfy_url:
        ok = False
        print(ui.c("  ✖ ", "red") + "notify.ntfy_url fehlt")
    state = ctx.store.load()
    if state.login_at:
        login = parse_time(state.login_at)
        print(f"  ⓘ Letzter Login über stopguard: {login.astimezone():%d.%m.%Y %H:%M} (max. {SESSION_MAX_DAYS} Tage gültig)")
    if state.last_run_at:
        print(f"  ⓘ Letzter Lauf: {parse_time(state.last_run_at).astimezone():%d.%m.%Y %H:%M} – {'ok' if state.last_run_ok else 'Fehler'}")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_notify_test(ctx: Context) -> int:
    errors = ctx.notifier.send("stopguard Test", "Wenn du das liest, funktionieren die Benachrichtigungen. 👍", "info")
    for e in errors:
        print(ui.c(f"✖ {e}", "red"))
    if not errors:
        print(ui.c(f"✔ Gesendet über: {', '.join(ctx.cfg.notify.channels)}", "green"))
    return EXIT_ERROR if errors else EXIT_OK


def cmd_strategies(ctx: Context) -> int:
    for name, st in sorted(ctx.cfg.strategies.items()):
        print(ui.c(name, "bold") + (f" – {st.description}" if st.description else ""))
        for i, ph in enumerate(st.phases):
            cond = "Start" if i == 0 else ph.activate.describe()
            print(f"   {i + 1}. {ph.name} [{cond}]: {ph.describe()} · Ordertyp {ph.order_type}")
    return EXIT_OK


def cmd_history(ctx: Context) -> int:
    for e in ctx.store.read_audit(ctx.args.limit):
        at = parse_time(e.pop("at"))
        event = e.pop("event")
        print(f"{at.astimezone():%d.%m.%Y %H:%M}  {ui.c(event, 'bold'):<28} {json.dumps(e, ensure_ascii=False, default=str)}")
    return EXIT_OK


def cmd_backtest(ctx: Context) -> int:
    from stopguard.backtest import parse_candles, run_backtest

    isin = ctx.args.isin_pos.upper()
    candles = parse_candles(ctx.client.candlesticks(isin, ctx.args.timeframe))
    if len(candles) < 10:
        print("Zu wenige Kursdaten für einen Backtest.")
        return EXIT_ERROR
    names = ctx.args.strategy or [ctx.cfg.position_for(isin).strategy if ctx.cfg.position_for(isin) else ctx.cfg.engine.default_strategy]
    rows = []
    results = []
    for name in names:
        res = run_backtest(ctx.cfg.strategy(name), candles, venue=ctx.cfg.scalable.venue, decimals=ctx.cfg.engine.price_decimals)
        results.append(res)
        rows.append(
            [
                name,
                f"{res.entry_at.astimezone():%d.%m.%y} @ {res.entry_price:.2f}",
                (f"{res.exit_at.astimezone():%d.%m.%y} @ {res.exit_price:.2f}" if res.exit_at else "–"),
                ui.colored_pct(res.total_return),
                ui.colored_pct(res.irr),
                ui.colored_pct(res.buy_hold_return),
                f"{res.max_drawdown * 100:.1f}%",
                res.exit_reason,
            ]
        )
    print(ui.c(f"Backtest {isin} · {len(candles)} Kerzen ({ctx.args.timeframe}) · Einmalkauf am ersten Tag", "bold"))
    print(ui.table(["Strategie", "Kauf", "Verkauf", "Rendite", "IRR p.a.", "Buy&Hold", "max. DD", "Ergebnis"], rows, align="lllrrrrl"))
    if ctx.args.verbose_timeline:
        for res in results:
            print(ui.c(f"\nVerlauf {res.strategy}", "bold"))
            for at, text in res.timeline:
                print(f"  {at.astimezone():%d.%m.%Y}  {text}")
    print(ui.c("Hinweis: vereinfachte Simulation ohne Gebühren/Steuern/Slippage – keine Prognose.", "dim"))
    return EXIT_OK


CONFIG_TEMPLATE = Path(__file__).with_name("data") / "config.example.toml"


def cmd_init(ctx_args: argparse.Namespace) -> int:
    target = Path(ctx_args.path).expanduser()
    if target.exists() and not ctx_args.force:
        print(f"{target} existiert bereits (--force zum Überschreiben).")
        return EXIT_ERROR
    text = CONFIG_TEMPLATE.read_text(encoding="utf-8")
    if ctx_args.from_depot:
        cfg = load_config(None) if not ctx_args.config else load_config(Path(ctx_args.config))
        client = ScClient(command=cfg.scalable.sc_command, quote_source=cfg.scalable.quote_source)
        holdings, _ = client.holdings()
        blocks = ["\n# --- aus dem Depot übernommen (bitte Strategie/Modus prüfen) ---"]
        for h in holdings:
            blocks.append(
                f'\n[[positions]]\nisin = "{h.isin}"   # {h.name}\nstrategy = "irr_gate"\nexecution = "watch"   # erst beobachten, dann auf "order" umstellen\n'
            )
        text += "".join(blocks)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(ui.c(f"✔ Konfiguration geschrieben: {target}", "green"))
    print("Nächste Schritte: Datei anpassen → 'stopguard doctor' → 'stopguard status'")
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    from stopguard.demo import init_demo

    if args.demo_cmd == "init":
        config_path = init_demo(Path(args.dir))
        print(ui.c(f"✔ Demo-Umgebung angelegt: {config_path.parent}", "green"))
        print("Ausprobieren (simuliertes Depot, kein echter Broker):")
        print(f"  stopguard -c {config_path} status")
        print(f"  stopguard -c {config_path} explain US0378331005")
        print(f"  stopguard -c {config_path} apply")
        print(f"  stopguard -c {config_path} demo move SAP 5     # Kurs um +5 % bewegen")
        print(f"  stopguard -c {config_path} backtest DE0007164600 -s irr_gate -s trailing_5")
        return EXIT_OK
    cfg = load_config(Path(args.config) if args.config else None)
    cmd = cfg.scalable.sc_command
    if "stopguard.fake_sc" not in " ".join(cmd):
        print("'demo move' funktioniert nur mit der Demo-Konfiguration (simuliertes Depot).")
        return EXIT_ERROR
    world_path = Path(cmd[cmd.index("--world") + 1])
    world = json.loads(world_path.read_text(encoding="utf-8"))
    target = args.isin_or_name.upper()
    isin = next(
        (i for i, s in world["securities"].items() if i == target or target in s["name"].upper()),
        None,
    )
    if isin is None:
        print(f"Unbekanntes Papier: {args.isin_or_name}")
        return EXIT_ERROR
    proc = subprocess.run([*cmd, "sim", "move", isin, str(args.pct)], capture_output=True, text=True)
    print(proc.stdout.strip() or proc.stderr.strip())
    return proc.returncode


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stopguard",
        description="Dynamische Stop-Loss-Verwaltung für Scalable Capital (über die offizielle Scalable CLI).",
    )
    p.add_argument("-c", "--config", help="Pfad zur Konfiguration (Standard: ./stopguard.toml oder ~/.config/stopguard/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="ausführliche Protokollierung")
    p.add_argument("--version", action="version", version=f"stopguard {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="BEFEHL")

    s = sub.add_parser("status", help="Übersicht aller Positionen, Phasen und Stops")
    s.add_argument("--isin", action="append", help="nur diese ISIN(s)")
    s.add_argument("--json", action="store_true", help="maschinenlesbare Ausgabe")
    s.add_argument("--plan", action="store_true", help="zusätzlich Maßnahmen anzeigen")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("plan", help="empfohlene Order-Anpassungen anzeigen (ändert nichts)")
    s.add_argument("--isin", action="append")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("explain", help="Rechenweg für eine Position (IRR, Phase, Stop)")
    s.add_argument("isin_pos", metavar="ISIN")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("apply", help="Order-Anpassungen interaktiv ausführen (mit Vorschau + Bestätigung)")
    s.add_argument("--isin", action="append")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("run", help="ein Überwachungslauf (für launchd/cron/systemd)")
    s.add_argument("--quiet", "-q", action="store_true")
    s.add_argument("--force", action="store_true", help="auch außerhalb von schedule.active_hours vollständig prüfen")
    s.add_argument("--dry-run", action="store_true", help="Benachrichtigungen nur protokollieren")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("daemon", help="Dauerbetrieb mit internem Zeitplan (Docker/Server)")
    s.add_argument("--once", action="store_true", help="nur einen Durchlauf")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("resolve", help="unklare Ordereinreichungen klären (Scalable-Recovery-Prozedur)")
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("login", help="'sc login' ausführen und Login-Zeitpunkt merken")
    s.add_argument("--read-only", action="store_true", help="lokal schreibgeschützte Session")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("doctor", help="Installation, Login und Konfiguration prüfen")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("notify-test", help="Testbenachrichtigung senden")
    s.set_defaults(func=cmd_notify_test)

    s = sub.add_parser("strategies", help="verfügbare Strategien anzeigen")
    s.set_defaults(func=cmd_strategies)

    s = sub.add_parser("history", help="Protokoll aller Aktionen (audit.jsonl)")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("backtest", help="Strategie auf historischen Kursen testen")
    s.add_argument("isin_pos", metavar="ISIN")
    s.add_argument("-s", "--strategy", action="append", help="Strategie(n) zum Vergleich")
    s.add_argument(
        "--timeframe",
        default="one_year",
        choices=["one_month", "three_months", "six_months", "year_to_date", "one_year", "max"],
    )
    s.add_argument("--timeline", dest="verbose_timeline", action="store_true", help="Phasen-/Stop-Verlauf anzeigen")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("init", help="Beispielkonfiguration anlegen")
    s.add_argument("--path", default=str(Path.home() / ".config" / "stopguard" / "config.toml"))
    s.add_argument("--from-depot", action="store_true", help="Positionen aus dem Depot übernehmen")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init, raw=True)

    from stopguard.portfolio.cli import add_parser as add_portfolio_parser

    add_portfolio_parser(sub)

    s = sub.add_parser("demo", help="Demo mit simuliertem Depot")
    dsub = s.add_subparsers(dest="demo_cmd", required=True)
    d = dsub.add_parser("init", help="Demo-Umgebung anlegen")
    d.add_argument("dir", nargs="?", default="./stopguard-demo")
    d = dsub.add_parser("move", help="Kurs im simulierten Depot bewegen")
    d.add_argument("isin_or_name")
    d.add_argument("pct", type=float, help="Veränderung in Prozent, z. B. 5 oder -3")
    s.set_defaults(func=cmd_demo, raw=True)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s" if args.command in ("run", "daemon") else "%(message)s",
        stream=sys.stderr,
    )
    try:
        if getattr(args, "raw", False):
            return args.func(args)
        ctx = Context(args)
        return args.func(ctx)
    except ConfigError as exc:
        print(ui.c(f"Konfigurationsfehler: {exc}", "red"), file=sys.stderr)
        return EXIT_CONFIG
    except ScNotInstalled as exc:
        print(ui.c(str(exc), "red"), file=sys.stderr)
        return EXIT_CONFIG
    except ScError as exc:
        print(ui.c(f"Scalable CLI: {exc.friendly()}", "red"), file=sys.stderr)
        if exc.is_auth_problem:
            print("→ 'stopguard login' ausführen und den Login im Browser abschließen.", file=sys.stderr)
            return EXIT_AUTH
        if exc.is_device_locked:
            print("→ Mac entsperren und erneut versuchen.", file=sys.stderr)
        return EXIT_ERROR
    except RuntimeError as exc:
        print(ui.c(str(exc), "red"), file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130
    except BrokenPipeError:  # z. B. 'stopguard history | head'
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
