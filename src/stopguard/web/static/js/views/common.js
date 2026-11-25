// Gemeinsame Diagramm-Spezifikationen und Hilfen der Ansichten.

import { mountChart } from "../charts.js";
import { $$, html } from "../dom.js";
import * as fmt from "../fmt.js";
import { emptyCard } from "../ui.js";

export const RANGES = [
  ["6m", "6 Monate", 182],
  ["1y", "1 Jahr", 365],
  ["3y", "3 Jahre", 1095],
  ["max", "Gesamt", null],
];

export function cutoff(range) {
  const r = RANGES.find((x) => x[0] === range);
  if (!r || !r[2]) return null;
  return new Date(Date.now() - r[2] * 86400000).toISOString().slice(0, 10);
}

export function sliceRows(rows, range) {
  const c = cutoff(range);
  return c ? rows.filter((r) => r.date >= c) : rows;
}

const axisEur = (v) => fmt.eurShort(v);

export function valueChartSpec(doc, range) {
  const rows = sliceRows(doc.timeseries.portfolio || [], range);
  return {
    type: "line",
    label: "Depotwert und investiertes Kapital im Zeitverlauf",
    series: [
      { name: "Depotwert", cls: "c1", area: true, points: rows.map((r) => [r.date, r.value]) },
      { name: "Investiertes Kapital (Einstand)", cls: "deemph", points: rows.map((r) => [r.date, r.invested]) },
    ],
    y: { format: (v) => fmt.eur(v, 0), axis: axisEur },
  };
}

/** TWR-Index auf den Beginn des Zeitraums normiert (= 100), Benchmark ebenso. */
export function twrChartSpec(doc, range) {
  const rows = sliceRows(doc.timeseries.twr || [], range).filter((r) => r.portfolio !== null);
  const p0 = rows.length ? rows[0].portfolio : 100;
  const b0row = rows.find((r) => r.benchmark !== null);
  const b0 = b0row ? b0row.benchmark : null;
  const bench = doc.settings.benchmark.name || "Benchmark";
  const series = [{ name: "Depot (zeitgewichtet)", cls: "c1", points: rows.map((r) => [r.date, r.portfolio / p0 - 1]) }];
  if (b0) series.push({ name: bench, cls: "deemph", points: rows.filter((r) => r.benchmark !== null).map((r) => [r.date, r.benchmark / b0 - 1]) });
  return {
    type: "line",
    label: `Zeitgewichtete Rendite des Depots im Vergleich zu ${bench}`,
    series,
    y: { format: (v) => fmt.pct(v, 1, true), axis: (v) => fmt.pct(v, 0, true), zero: true },
    caption: "Zeitgewichtete Rendite (TWR): Ein- und Auszahlungen verzerren den Verlauf nicht – deshalb vergleichbar mit einem Index.",
  };
}

/** Bindet alle <figure data-chart="key"> ein; specs: {key: spec}. Gibt Aufräumfunktion zurück. */
export function mountCharts(root, specs) {
  const disposers = $$("figure[data-chart]", root)
    .filter((f) => specs[f.dataset.chart])
    .map((f) => mountChart(f, specs[f.dataset.chart]));
  return () => disposers.forEach((d) => d());
}

export function figure(key) {
  return html`<figure data-chart="${key}"></figure>`;
}

export function noData() {
  return emptyCard(
    "Noch keine Depotdaten",
    "Lade Transaktionen, Kurse und den Depotverlauf einmalig von Scalable. Danach berechnet stopguard IRR, Steuern und Kredit für jeden Trade.",
    html`<a class="btn primary" href="#/daten">Zu „Daten laden“</a>`,
  );
}

export function hasData(doc) {
  return doc && (doc.trades || []).length > 0;
}

export function phaseBadge(pos) {
  if (!pos) return "";
  const cur = (pos.phases || []).find((p) => p.state === "current");
  return cur ? html`<span class="badge ${pos.breach ? "failed" : "wait"}">${cur.name}</span>` : "";
}

/** Zustand des Stop-Schutzes einer Position in Worten (Badge-Klasse, Text). */
export function protection(pos) {
  if (!pos) return ["", "–"];
  if (pos.breach) return ["failed", "Stop erreicht"];
  if (pos.unresolved) return ["failed", "Einreichung ungeklärt"];
  if ((pos.actions || []).some((a) => a.needs_confirmation)) return ["busy", "Anpassung empfohlen"];
  if (pos.execution === "watch") return ["", "nur beobachtet"];
  if (pos.execution === "alert") return pos.managed_alert ? ["done", "Kursalarm aktiv"] : ["wait", "Alarm fehlt"];
  if (!pos.target.stop) return ["wait", "kein Stop vorgesehen"];
  return pos.managed_order ? ["done", "geschützt"] : ["wait", "ohne Order"];
}
