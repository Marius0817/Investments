"""Unterbefehle ``stopguard portfolio …`` inkl. Terminal-Berichte."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from stopguard import ui
from stopguard.portfolio.analysis import analyse_portfolio
from stopguard.portfolio.importers import ImportError_, read_trades_csv
from stopguard.portfolio.store import PortfolioStore
from stopguard.portfolio.sync import sync_portfolio
from stopguard.state import StateStore

log = logging.getLogger(__name__)


def db_path(ctx) -> Path:
    return Path(ctx.cfg.state_dir) / "portfolio.db"


def open_store(ctx) -> PortfolioStore:
    return PortfolioStore(db_path(ctx))


def compute(ctx) -> dict[str, Any]:
    store = open_store(ctx)
    try:
        stop_states = StateStore(ctx.cfg.state_dir).load().positions
        return analyse_portfolio(store, ctx.cfg, stop_states=stop_states)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------

def eur(v: float | None, digits: int = 0) -> str:
    if v is None:
        return "–"
    text = f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def pct(v: float | None, digits: int = 1) -> str:
    return ui.colored_pct(v, digits)


def print_summary(doc: dict[str, Any]) -> None:
    s = doc["summary"]
    irr = s["irr"]
    twr = s.get("twr") or {}
    cr = doc["credit"]
    print(ui.c(f"Portfolio · Stand {doc['as_of'][:16].replace('T', ' ')} UTC", "bold"))
    rows = [
        ["Depotwert", eur(s["securities_value"]), "Einstand offen", eur(s["cost_basis_open"])],
        ["Cash", eur(s["cash"]), "Unrealisiert", eur(s["unrealized_pl"])],
        ["Kredit", eur(s["loan"]), "Realisiert", eur(s["realized_pl"])],
        ["Nettovermögen", eur(s["net_worth"]), "Dividenden", eur(s["dividends"])],
        ["Netto-Einzahlungen", eur(s["net_deposits"]), "Gebühren / Zinsen", f"{eur(s['fees'])} / {eur(s['interest_paid'])}"],
    ]
    print(ui.table(["Vermögen", "", "Ergebnis", ""], rows, align="lrlr"))
    print()
    print(ui.table(
        ["Rendite", "Wert", "Erklärung"],
        [
            ["IRR Trades (vor Steuern)", pct(irr["trades"], 2), "alle Käufe/Verkäufe/Dividenden, offene Positionen zum Kurswert"],
            ["IRR nach Steuern", pct(irr["trades_after_tax"], 2), "inkl. Vorabpauschale, latenter Steuer auf offene Gewinne"],
            ["IRR nach Steuern & Zinsen", pct(irr["trades_after_tax_and_interest"], 2), "Eigenkapitalsicht inkl. Kredit"],
            ["IRR Konto", pct(irr["account"], 2), "Ein-/Auszahlungen vs. Nettovermögen (Scalable-Konto)"],
            ["TWR seit Start", pct(twr.get("since_start"), 2), f"p.a. {pct(twr.get('annualized'), 2)} · Benchmark {pct(twr.get('benchmark_since_start'), 2)}"],
            ["TWR YTD / 1 Jahr", f"{pct(twr.get('ytd'), 2)} / {pct(twr.get('one_year'), 2)}", f"max. Drawdown {pct(twr.get('max_drawdown'), 1)}"],
        ],
        align="lrl",
    ))
    c = s["counts"]
    print(f"\n{c['positions_open']} offene Positionen · {c['trades']} Trades ({c['trades_open']} offen, {c['trades_closed']} geschlossen, "
          f"{c['trades_credit']} kreditfinanziert) · Trefferquote geschlossen {pct(c['win_rate_closed'], 0)}")
    if cr.get("loan"):
        st = cr.get("stress") or {}
        print(ui.c(
            f"Kredit {eur(cr['loan'])} · Beleihung {pct(cr.get('loan_to_value'), 1)} · Zinsen 12M {eur(cr.get('interest_12m'))} "
            f"({pct(cr.get('effective_rate_12m'), 2)} p.a.)"
            + (f" · Puffer bis Kreditlinie ≈ {pct(-st['drop_until_limit'], 0)} Depotwert" if st.get("drop_until_limit") is not None else ""),
            "yellow",
        ))
    _print_quality(doc)


def _print_quality(doc: dict[str, Any]) -> None:
    dq = doc["data_quality"]
    for w in dq.get("warnings", [])[:8]:
        print(ui.c(f"  ⚠ {w}", "yellow"))
    for n in dq.get("notes", [])[:5]:
        print(ui.c(f"  ⓘ {n}", "dim"))


def print_positions(doc: dict[str, Any]) -> None:
    rows = []
    for p in doc["positions"]:
        if p["status"] != "open":
            continue
        rows.append([
            ui.shorten(p["name"], 32), f"{p['units']:g}", eur(p["value"]), pct(p["weight"], 1), pct(p["unrealized_pct"]),
            eur(p["dividends"]), pct(p["irr"]), pct(p["irr_after_tax"]),
            pct(p["equity_irr"]) if p.get("equity_irr") is not None else "–",
            (p.get("stopguard") or {}).get("phase") or "–",
        ])
    print(ui.table(["Position", "Stk.", "Wert", "Anteil", "G/V", "Divid.", "IRR", "IRR n. St.", "IRR EK", "Stop-Phase"], rows, align="lrrrrrrrrl"))


def print_trades(doc: dict[str, Any], args: argparse.Namespace) -> None:
    trades = doc["trades"]
    if args.status:
        trades = [t for t in trades if t["status"] in args.status.split(",")]
    if args.credit:
        trades = [t for t in trades if t.get("credit")]
    if args.isin:
        trades = [t for t in trades if t["isin"] == args.isin.upper()]
    if args.no_plans:
        trades = [t for t in trades if t["origin"] != "savings_plan"]
    key = args.sort.lstrip("-")
    trades = sorted(trades, key=lambda t: (t.get(key) is None, t.get(key) or 0), reverse=not args.sort.startswith("-") and key != "open_date")
    rows = []
    for t in trades[: args.limit]:
        b = t.get("benchmark") or {}
        cr = t.get("credit") or {}
        rows.append([
            ui.shorten(t["name"], 28), t["open_date"], t["close_date"] or {"open": "offen", "partial": "teilw."}.get(t["status"], "–"),
            f"{t['holding_days']:.0f}", eur(t["invested"]), eur(t["profit"]), pct(t["return_pct"]), pct(t["irr"]),
            pct(t["irr_after_tax"]), pct(b.get("irr")), pct(b.get("alpha_irr")),
            pct(cr.get("equity_irr")) if cr else "",
        ])
    print(ui.table(
        ["Trade", "Kauf", "Verkauf", "Tage", "Einsatz", "Gewinn", "Rendite", "IRR", "IRR n. St.", "Benchm.", "Alpha", "IRR EK"],
        rows, align="lllrrrrrrrrr",
    ))
    print(ui.c(f"{len(trades)} Trades · Benchmark: {doc['settings']['benchmark']['name']} · IRR EK = Eigenkapital nach Steuern & Kreditzinsen", "dim"))


def print_credit(doc: dict[str, Any]) -> None:
    cr = doc["credit"]
    print(ui.c(f"Kredit (Modus {cr['mode']})", "bold"))
    print(f"  Saldo {eur(cr['loan'], 2)} · freie Kreditlinie {eur(cr['available_credit_line'])} · Beleihung {pct(cr['loan_to_value'], 1)}")
    print(f"  Zinsen gesamt {eur(cr['interest_total'], 2)} · 12 Monate {eur(cr['interest_12m'], 2)} ({pct(cr['effective_rate_12m'], 2)} p.a.)"
          f" · nicht zuordenbar {eur(cr['interest_unallocated'], 2)}")
    if cr.get("stress"):
        print(f"  Stresstest: Kreditlinie zu {pct(cr['stress']['utilization'], 0)} genutzt – ein Kursrückgang von ca. "
              f"{pct(-cr['stress']['drop_until_limit'], 0)} würde sie ausschöpfen ({cr['stress']['assumption']})")
    rows = []
    for t in cr["trades"]:
        c = t["credit"]
        rows.append([
            ui.shorten(t["name"], 28), t["open_date"], eur(c["financed"]), pct(c["leverage"], 0), eur(c["outstanding"]),
            eur(c["interest"], 2) + ("*" if c["interest_estimated"] else ""), pct(c["effective_rate"], 2),
            pct(t["irr"]), pct(t["irr_after_tax"]), pct(c["equity_irr"]), eur(c["profit_net"]), pct(c["break_even_irr"]),
        ])
    if rows:
        print()
        print(ui.table(
            ["Trade", "Kauf", "Kredit", "Hebel", "offen", "Zinsen", "Zinssatz", "IRR", "IRR n. St.", "IRR EK", "Netto", "Break-even"],
            rows, align="llrrrrrrrrrr",
        ))
        print(ui.c("IRR EK = Rendite auf das eingesetzte Eigenkapital nach Steuern und Zinsen · Break-even = nötige Vorsteuer-Rendite, "
                   "um die Zinsen zu decken (Zinsen sind nicht steuerlich absetzbar) · * geschätzt", "dim"))
    else:
        print("  Keine kreditfinanzierten Trades erkannt.")


def print_taxes(doc: dict[str, Any]) -> None:
    tx = doc["taxes"]
    print(ui.c(f"Steuern · Satz {tx['effective_rate'] * 100:.3f} % ({tx['rate_formula']})", "bold"))
    rows = [
        [y["year"], eur(y["gains_stock"]), eur(y["gains_other"]), eur(y["dividends_taxable"]), eur(y["vorabpauschale"]),
         eur(y["taxable"]), eur(y["allowance_used"]), eur(y["loss_carryforward_stock"]), eur(y["tax_model"]), eur(y["tax_actual"])]
        for y in tx["years"]
    ]
    print(ui.table(
        ["Jahr", "Aktien", "Sonstige", "Dividenden", "Vorabp.", "Steuerpfl.", "Freibetrag", "Verlust Aktien", "Steuer (Modell)", "Steuer (Scalable)"],
        rows, align="lrrrrrrrrr",
    ))
    al = tx["allowance"]
    print(f"\nFreibetrag {eur(al['annual'])} · dieses Jahr noch frei: {eur(al['remaining_this_year'])} · latente Steuer offener Gewinne: {eur(tx['latent_total'])}")
    if al["candidates"] and al["remaining_this_year"]:
        print(ui.c("Kandidaten, um den Freibetrag zu nutzen (Gewinn realisieren und ggf. wieder kaufen):", "dim"))
        for c in al["candidates"][:5]:
            print(ui.c(f"  {c['name']}: steuerpflichtiger Gewinn {eur(c['taxable_gain'])} (unrealisiert {eur(c['unrealized_gain'])})", "dim"))
    print(ui.c(tx["disclaimer"], "dim"))


def print_income(doc: dict[str, Any]) -> None:
    inc = doc["income"]
    print(ui.c(f"Dividenden letzte 12 Monate: {eur(inc['trailing_12m'], 2)} · Rendite {pct(inc['yield_12m'], 2)}", "bold"))
    print(ui.table(["Jahr", "Dividenden"], [[y["year"], eur(y["dividends"], 2)] for y in inc["by_year"]], align="lr"))
    print()
    print(ui.table(["Position", "Dividenden"], [[ui.shorten(p["name"], 36), eur(p["dividends"], 2)] for p in inc["by_position"]], align="lr"))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def cmd_sync(ctx) -> int:
    store = open_store(ctx)
    try:
        rep = sync_portfolio(
            ctx.client, store, ctx.cfg,
            details=False if ctx.args.no_details else None,
            prices=not ctx.args.no_prices,
            max_details=ctx.args.max_details,
            progress=lambda s: print(ui.c(s, "dim")),
        )
    finally:
        store.close()
    print(ui.c(
        f"✔ Sync: {rep.transactions} Transaktionen, {rep.details} Details (offen: {rep.details_pending}), "
        f"{sum(rep.prices.values())} Kurse für {len(rep.prices)} Papiere, {rep.chart_points} Depotverlaufspunkte, {rep.holdings} Positionen",
        "green",
    ))
    for w in rep.warnings[:10]:
        print(ui.c(f"  ⚠ {w}", "yellow"))
    return 0


def cmd_report(ctx) -> int:
    doc = compute(ctx)
    view = ctx.args.pf_command
    if view == "summary":
        print_summary(doc)
    elif view == "positions":
        print_positions(doc)
    elif view == "trades":
        print_trades(doc, ctx.args)
    elif view == "credit":
        print_credit(doc)
    elif view == "taxes":
        print_taxes(doc)
    elif view == "income":
        print_income(doc)
    return 0


def cmd_export(ctx) -> int:
    doc = compute(ctx)
    text = json.dumps(doc, ensure_ascii=False, indent=2 if ctx.args.pretty else None, default=str)
    if ctx.args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        out = Path(ctx.args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(ui.c(f"✔ Export geschrieben: {out} ({len(text) / 1024:.0f} KB, Schema {doc['schema']})", "green"))
    return 0


def cmd_serve(ctx) -> int:
    from stopguard.portfolio.api import PortfolioCache, serve

    cache = PortfolioCache(lambda: compute(ctx), ttl_seconds=ctx.args.ttl)
    cache.get()
    server = serve(cache, ctx.args.host, ctx.args.port, ctx.args.allow_origin)
    print(ui.c(f"✔ Portfolio-API läuft: http://{ctx.args.host}:{ctx.args.port}/api/v1/summary  (Strg+C beendet)", "green"))
    if ctx.args.host not in ("127.0.0.1", "localhost", "::1"):
        print(ui.c("  ⚠ Die API ist nicht nur lokal erreichbar und hat keine Authentifizierung – nur im vertrauenswürdigen Netz verwenden.", "yellow"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_import(ctx) -> int:
    source = ctx.args.source
    try:
        rows = read_trades_csv(Path(ctx.args.file), source=source, decimal=ctx.args.decimal)
    except (ImportError_, OSError) as exc:
        print(ui.c(f"Import fehlgeschlagen: {exc}", "red"))
        return 1
    store = open_store(ctx)
    try:
        if ctx.args.replace:
            removed = store.delete_manual_lots(source)
            print(ui.c(f"{removed} bisher importierte Trades aus '{source}' entfernt", "dim"))
        n = store.upsert_manual_lots(rows)
    finally:
        store.close()
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker") and not r.get("isin")})
    mapped = {ic.ticker.upper() for ic in ctx.cfg.instruments.values() if ic.ticker}
    unmapped = [t for t in tickers if t.upper() not in mapped]
    print(ui.c(f"✔ {n} Trades importiert ({sum(1 for r in rows if r['credit_flag'])} kreditfinanziert, "
               f"{sum(1 for r in rows if r['close_date'])} geschlossen)", "green"))
    if unmapped:
        print(ui.c("Für diese Ticker fehlt die ISIN – bitte in der Konfiguration ergänzen, damit Kurse und Dubletten-Erkennung funktionieren:", "yellow"))
        for t in unmapped:
            print(f'  [instruments."ISIN"]  ticker = "{t}"')
    print("Bereits in Scalable enthaltene Trades werden bei der Auswertung automatisch erkannt (gleiche ISIN, ±Tage, Stückzahl).")
    return 0


def cmd_instruments(ctx) -> int:
    doc = compute(ctx)
    from stopguard.portfolio.model import TEILFREISTELLUNG

    seen: dict[str, dict[str, Any]] = {}
    for t in doc["trades"]:
        seen.setdefault(t["isin"], {"isin": t["isin"], "name": t["name"], "ticker": t["ticker"], "kind": t["kind"], "trades": 0})
        seen[t["isin"]]["trades"] += 1
    rows = [
        [v["isin"], ui.shorten(v["name"], 34), v["ticker"] or "–", v["kind"], f"{TEILFREISTELLUNG.get(v['kind'], 0) * 100:.0f} %", v["trades"]]
        for v in sorted(seen.values(), key=lambda x: x["name"])
    ]
    print(ui.table(["ISIN", "Name", "Ticker", "Art", "Teilfreist.", "Trades"], rows, align="lllllr"))
    print(ui.c("Art/Teilfreistellung/Ticker anpassen: [instruments.\"ISIN\"] kind = \"equity_fund\" | \"stock\" | … in der Konfiguration", "dim"))
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("portfolio", help="Portfolio-Tracking: IRR, Trade-Vergleich, Kredit, Steuern, Export/API")
    psub = p.add_subparsers(dest="pf_command", required=True, metavar="BEFEHL")

    s = psub.add_parser("sync", help="Daten von Scalable laden (Transaktionen, Details, Kurse, Depotverlauf)")
    s.add_argument("--no-details", action="store_true", help="keine Detailabfragen (Gebühren/Steuern)")
    s.add_argument("--no-prices", action="store_true", help="keine Kurshistorie laden")
    s.add_argument("--max-details", type=int, help="höchstens N Detailabfragen pro Lauf (Rate-Limits)")
    s.set_defaults(func=cmd_sync)

    for name, help_text in (
        ("summary", "Kennzahlen des Gesamtdepots"),
        ("positions", "Positionen mit IRR vor/nach Steuern"),
        ("credit", "kreditfinanzierte Trades, Zinsen, Stresstest"),
        ("taxes", "Steuern je Jahr, Freibetrag, latente Steuer"),
        ("income", "Dividenden"),
    ):
        psub.add_parser(name, help=help_text).set_defaults(func=cmd_report)

    s = psub.add_parser("trades", help="alle Trades im Vergleich (inkl. Benchmark)")
    s.add_argument("--status", help="open,partial,closed")
    s.add_argument("--isin")
    s.add_argument("--credit", action="store_true", help="nur kreditfinanzierte")
    s.add_argument("--no-plans", action="store_true", help="Sparplan-Ausführungen ausblenden")
    s.add_argument("--sort", default="irr", help="Feld, z. B. irr, profit, holding_days, -open_date (Minus = aufsteigend)")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_report)

    s = psub.add_parser("export", help="komplette Auswertung als JSON (für UI/Weiterverarbeitung)")
    s.add_argument("--out", default="portfolio.json", help="Datei oder '-' für stdout")
    s.add_argument("--pretty", action="store_true")
    s.set_defaults(func=cmd_export)

    s = psub.add_parser("serve", help="lokale JSON-API für eine UI starten")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--ttl", type=int, default=300, help="Sekunden, bis die Auswertung neu berechnet wird")
    s.add_argument("--allow-origin", default="*", help="CORS-Header für die UI-Entwicklung")
    s.set_defaults(func=cmd_serve)

    s = psub.add_parser("import", help="Trades aus Notion-Export (CSV) oder eigener CSV übernehmen")
    s.add_argument("file")
    s.add_argument("--source", default="notion", help="Quellname (z. B. notion, comdirect)")
    s.add_argument("--decimal", choices=["auto", "point", "comma"], default="auto")
    s.add_argument("--replace", action="store_true", help="vorher alle Trades dieser Quelle entfernen")
    s.set_defaults(func=cmd_import)

    s = psub.add_parser("instruments", help="Wertpapiere, Ticker und steuerliche Einordnung anzeigen")
    s.set_defaults(func=cmd_instruments)
