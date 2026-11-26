"""Hinweise zum Depot – regelbasiert und nachvollziehbar.

Jeder Hinweis nennt die Zahl, auf der er beruht, und wohin man zum Nachprüfen
klickt. Es sind Beobachtungen, keine Anlageempfehlungen: stopguard sagt, *was*
auffällt, nicht *was* zu tun ist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from stopguard.config import Config

LEVEL_ORDER = {"warn": 0, "info": 1, "good": 2}


def _pct(v: float, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f} %".replace(".", ",")


def _eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", ".")


def build_insights(doc: dict[str, Any], cfg: Config, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(id_: str, level: str, category: str, title: str, message: str, *, link: str | None = None, value: float | None = None) -> None:
        out.append({"id": id_, "level": level, "category": category, "title": title, "message": message, "link": link, "value": value})

    positions = [p for p in doc.get("positions", []) if p["status"] == "open" and (p["value"] or 0) > 0]
    summary = doc.get("summary", {})
    total = sum(p["value"] for p in positions)
    if not positions or total <= 0:
        return []

    # --- Klumpenrisiko: Einzelaktien und Einzelwerte inkl. ETF-Durchschau
    limit = cfg.portfolio.concentration_limit_pct / 100
    for p in positions:
        if p["kind"] == "stock" and p["value"] / total > limit:
            add(
                f"concentration:{p['isin']}", "warn", "Risiko", f"{p['name']}: {_pct(p['value'] / total)} des Depots",
                f"Eine einzelne Aktie macht mehr als {cfg.portfolio.concentration_limit_pct:g} % deines Depots aus. "
                "Ein Kurseinbruch dieser Aktie wirkt sich entsprechend stark auf dein Vermögen aus.",
                link=f"#/positionen/{p['isin']}", value=p["value"] / total,
            )
    look = ((doc.get("allocation") or {}).get("look_through") or {})
    for item in look.get("overlap", [])[:3]:
        add(
            f"overlap:{item['key']}", "info", "Risiko", f"{item['name']} steckt auch in deinen ETFs",
            f"Direkt {_eur(item['direct'])}, über Fonds weitere {_eur(item['via_funds'])} – zusammen {_pct(item['weight'])} des Depots.",
            link="#/auswertung/diversifikation", value=item["weight"],
        )

    # --- Regionen und Branchen
    alloc = doc.get("allocation") or {}
    regions = [r for r in alloc.get("by_region", []) if r["key"] not in ("Nicht zugeordnet", "Sonstige")]
    if regions and regions[0]["weight"] > 0.7:
        add(
            "region", "info", "Diversifikation", f"{_pct(regions[0]['weight'], 0)} in {regions[0]['key']}",
            "Der größte Teil deines Depots hängt an einer Region. Welche Länder dahinterstehen, zeigt die Aufteilung nach Ländern.",
            link="#/auswertung/diversifikation", value=regions[0]["weight"],
        )
    sectors = [r for r in alloc.get("by_sector", []) if r["key"] not in ("Nicht zugeordnet", "Sonstige")]
    if sectors and sectors[0]["weight"] > 0.4:
        add(
            "sector", "warn", "Diversifikation", f"{_pct(sectors[0]['weight'], 0)} in einer Branche ({sectors[0]['key']})",
            "Fällt diese Branche gemeinsam, fällt ein großer Teil deines Depots mit.",
            link="#/auswertung/diversifikation", value=sectors[0]["weight"],
        )
    coverage = alloc.get("coverage") or {}
    if coverage.get("sector") is not None and coverage["sector"] < 0.7:
        add(
            "classify", "info", "Daten", f"{_pct(1 - coverage['sector'], 0)} des Depots ohne Branche",
            "Ordne Branchen und Regionen in der Konfiguration zu ([instruments.<ISIN>] sector, region, exposure), "
            "damit die Diversifikation vollständig ist.",
            link="#/auswertung/diversifikation", value=1 - coverage["sector"],
        )

    # --- Performance gegenüber dem Benchmark (zeitgewichtet, 12 Monate)
    twr = summary.get("twr") or {}
    bench_name = ((doc.get("settings") or {}).get("benchmark") or {}).get("name") or "Benchmark"
    one_year, bench_1y = twr.get("one_year"), None
    ts = (doc.get("timeseries") or {}).get("twr") or []
    if ts:
        last = ts[-1]
        start = next((r for r in ts if r["date"] >= (now.replace(year=now.year - 1)).date().isoformat()), None)
        if start and start is not last and start.get("benchmark") and last.get("benchmark"):
            bench_1y = last["benchmark"] / start["benchmark"] - 1
    if one_year is not None and bench_1y is not None and ts and (ts[-1]["date"] > ts[0]["date"]):
        diff = one_year - bench_1y
        if diff < -0.05:
            add(
                "benchmark", "info", "Performance", f"{_pct(-diff)} hinter {bench_name} (12 Monate)",
                f"Dein Depot: {_pct(one_year)}, {bench_name}: {_pct(bench_1y)} – zeitgewichtet, also ohne Einfluss deiner Ein- und Auszahlungen.",
                link="#/auswertung/performance", value=diff,
            )
        elif diff > 0.02:
            add(
                "benchmark", "good", "Performance", f"{_pct(diff)} vor {bench_name} (12 Monate)",
                f"Dein Depot: {_pct(one_year)}, {bench_name}: {_pct(bench_1y)} (zeitgewichtet).",
                link="#/auswertung/performance", value=diff,
            )
    dd = (doc.get("timeseries") or {}).get("drawdown") or []
    if dd and dd[-1].get("drawdown") is not None and dd[-1]["drawdown"] < -0.1:
        add(
            "drawdown", "info", "Performance", f"Depot {_pct(-dd[-1]['drawdown'], 0)} unter dem Höchststand",
            "Zeitgewichtet gemessen, also unabhängig von Ein- und Auszahlungen.",
            link="#/auswertung/performance", value=dd[-1]["drawdown"],
        )

    # --- Kredit
    credit = doc.get("credit") or {}
    if (credit.get("loan") or 0) > 0:
        weak = [t for t in credit.get("trades", []) if (t.get("credit") or {}).get("equity_irr") is not None and t["credit"]["equity_irr"] < 0]
        if weak:
            add(
                "credit_negative", "warn", "Kredit", f"{len(weak)} Kredit-Trade{'s' if len(weak) > 1 else ''} mit negativer Eigenkapital-Rendite",
                "Nach Steuern und Sollzinsen bleibt bei diesen Trades weniger übrig, als du an eigenem Geld eingesetzt hast.",
                link="#/auswertung/kredit", value=len(weak),
            )
        util = (credit.get("stress") or {}).get("utilization")
        if util is not None and util > 0.5:
            add(
                "credit_utilization", "warn", "Kredit", f"Kreditrahmen zu {_pct(util, 0)} genutzt",
                f"Fällt der Depotwert um etwa {_pct((credit['stress'] or {}).get('drop_until_limit') or 0, 0)}, ist der Rahmen ausgeschöpft (vereinfachte Schätzung).",
                link="#/auswertung/kredit", value=util,
            )
        rate = credit.get("effective_rate_12m")
        irr = (summary.get("irr") or {}).get("account")
        if rate is not None and irr is not None and irr < rate:
            add(
                "credit_rate", "warn", "Kredit", "Sollzins höher als deine Depotrendite",
                f"Effektiver Zins der letzten 12 Monate {_pct(rate)} p. a., Rendite deines Kontos {_pct(irr)} p. a. Kreditzinsen sind steuerlich nicht absetzbar.",
                link="#/auswertung/kredit", value=rate - irr,
            )

    # --- Steuern
    taxes = doc.get("taxes") or {}
    allowance = taxes.get("allowance") or {}
    remaining = allowance.get("remaining_this_year") or 0
    if remaining >= 100 and now.month >= 10 and allowance.get("candidates"):
        add(
            "allowance", "info", "Steuern", f"Noch {_eur(remaining)} Freibetrag in {now.year}",
            "Bis Jahresende kannst du Gewinne in dieser Höhe steuerfrei realisieren – nicht genutzter Freibetrag verfällt.",
            link="#/auswertung/steuern", value=remaining,
        )
    years = taxes.get("years") or []
    this_year = next((y for y in years if y.get("year") == now.year), None)
    losers = [p for p in positions if (p["unrealized_pl"] or 0) < -50]
    if this_year and (this_year.get("tax_actual") or 0) > 0 and losers:
        loss = -sum(p["unrealized_pl"] for p in losers)
        add(
            "loss_offset", "info", "Steuern", f"{_eur(loss)} offene Verluste bei gezahlter Steuer",
            f"In {now.year} wurde Steuer einbehalten, gleichzeitig {'liegen' if len(losers) > 1 else 'liegt'} {len(losers)} Position{'en' if len(losers) > 1 else ''} im Minus. "
            "Realisierte Verluste werden mit Gewinnen verrechnet (Aktienverluste nur mit Aktiengewinnen).",
            link="#/auswertung/steuern", value=loss,
        )

    # --- Kosten: kleine Einzelkäufe mit Ordergebühr
    small = [t for t in doc.get("trades", []) if t.get("origin") == "single" and t.get("invested") and t.get("fees") and t["fees"] / t["invested"] > 0.01]
    if len(small) >= 2:
        add(
            "fees_small_orders", "info", "Kosten", f"{len(small)} Käufe mit mehr als 1 % Gebühren",
            "Bei kleinen Einzelkäufen fällt die feste Ordergebühr stark ins Gewicht. Sparpläne sind bei Scalable in der Regel gebührenfrei.",
            link="#/auswertung/kosten", value=len(small),
        )

    # --- Liquidität
    cash = summary.get("cash") or 0
    net = summary.get("net_worth") or 0
    if net > 0 and cash > 1000 and cash / net > cfg.portfolio.cash_hint_pct / 100:
        add(
            "cash", "info", "Liquidität", f"{_eur(cash)} nicht investiert ({_pct(cash / net, 0)})",
            "Guthaben auf dem Verrechnungskonto. Ob es als Reserve gedacht ist, entscheidest du – hier nur zur Übersicht.",
            link="#/", value=cash / net,
        )

    # --- Dividenden
    forecast = (doc.get("income") or {}).get("forecast") or {}
    trailing = (doc.get("income") or {}).get("trailing_12m") or 0
    nxt = forecast.get("next_12m") or 0
    if nxt and trailing and nxt > trailing * 1.15:
        add(
            "dividends_up", "good", "Dividenden", f"Erwartete Ausschüttungen {_pct(nxt / trailing - 1, 0)} höher",
            f"Prognose der nächsten 12 Monate {_eur(nxt)} gegenüber {_eur(trailing)} in den letzten 12 Monaten – vor allem durch Zukäufe.",
            link="#/auswertung/dividenden", value=nxt,
        )

    # --- Laufende Produktkosten (TER) und doppelte ETFs
    ongoing = (doc.get("costs") or {}).get("ongoing") or {}
    if ongoing.get("weighted_ter") is not None and ongoing["weighted_ter"] >= 0.004:
        add(
            "ter_total", "info", "Kosten", f"Laufende Kosten {_pct(ongoing['weighted_ter'], 2)} p. a. (≈ {_eur(ongoing['annual_cost'])} im Jahr)",
            "Die TER der Fonds steckt im Kurs und taucht in keiner Buchung auf. Über Jahrzehnte macht sie einen spürbaren Unterschied.",
            link="#/auswertung/kosten", value=ongoing["weighted_ter"],
        )
    for row in [r for r in ongoing.get("by_position", []) if r["ter"] >= 0.004][:2]:
        add(
            f"ter:{row['isin']}", "info", "Kosten", f"{row['name']}: TER {_pct(row['ter'], 2)}",
            f"Rund {_eur(row['annual_cost'])} pro Jahr an laufenden Kosten für diesen Fonds.",
            link="#/auswertung/kosten", value=row["ter"],
        )
    ter_of = {r["isin"]: r["ter"] for r in ongoing.get("by_position", [])}
    for pair in ((alloc.get("fund_overlap") or {}).get("pairs") or [])[:2]:
        if pair["similarity"] >= 0.9:
            a, b = pair["a"], pair["b"]
            cost = ""
            ta, tb = ter_of.get(a["isin"]), ter_of.get(b["isin"])
            if ta is not None and tb is not None and abs(ta - tb) >= 0.0015:
                hi, lo = (a, b) if ta > tb else (b, a)
                cost = f" {hi['name']} kostet {_pct(max(ta, tb), 2)} p. a., {lo['name']} {_pct(min(ta, tb), 2)}."
            add(
                f"fund_overlap:{a['isin']}:{b['isin']}", "info", "Diversifikation",
                f"{a['name']} und {b['name']} sind sich zu {_pct(pair['similarity'], 0)} ähnlich",
                "Beide Fonds verteilen sich fast gleich auf Länder und Branchen – zusammen streuen sie kaum breiter als einer allein." + cost,
                link="#/auswertung/diversifikation", value=pair["similarity"],
            )

    # --- Vorabpauschale im Januar: Guthaben bereithalten
    vap = (doc.get("taxes") or {}).get("vorabpauschale_preview") or {}
    if now.month >= 11 and (vap.get("tax_after_allowance") or 0) > 0:
        add(
            "vap_cash", "warn" if cash < vap["tax_after_allowance"] else "info", "Steuern",
            f"Anfang Januar ca. {_eur(vap['tax_after_allowance'])} Steuer auf die Vorabpauschale",
            f"Sie wird vom Verrechnungskonto eingezogen – derzeit liegen dort {_eur(cash)}. "
            "Der Freistellungsauftrag des neuen Jahres ist schon abgezogen.",
            link="#/auswertung/steuern", value=vap["tax_after_allowance"],
        )

    # --- Positionen mit großem Verlust (nur Information, mit Bezug zur Stop-Regel)
    for p in sorted(positions, key=lambda x: x["unrealized_pct"] or 0)[:2]:
        if (p["unrealized_pct"] or 0) < -0.25:
            rule = (p.get("stopguard") or {}).get("phase")
            add(
                f"loser:{p['isin']}", "info", "Positionen", f"{p['name']} {_pct(-(p['unrealized_pct']), 0)} im Minus",
                f"Aktuelle Stop-Phase: {rule}." if rule else "Für diese Position gibt es keine Stop-Regel.",
                link=f"#/positionen/{p['isin']}", value=p["unrealized_pct"],
            )

    out.sort(key=lambda i: (LEVEL_ORDER.get(i["level"], 9), i["category"]))
    return out
