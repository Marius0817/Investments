// Auswertung „Dividenden“: erhalten und erwartet, Kalender der nächsten 12 Monate, Rendite auf Einstand.

import { rankedBars } from "../charts.js";
import { html } from "../dom.js";
import * as fmt from "../fmt.js";
import { badge, sortableTable, tiles } from "../ui.js";

const CONFIDENCE = {
  high: ["done", "volles Jahr"],
  medium: ["", "Rhythmus erkannt"],
  low: ["busy", "eine Zahlung"],
};

function addMonths(ym, k) {
  const [y, m] = ym.split("-").map(Number);
  const t = y * 12 + (m - 1) + k;
  return `${Math.floor(t / 12)}-${String((t % 12) + 1).padStart(2, "0")}`;
}

export function dividendsPanel(doc) {
  const inc = doc.income || {};
  const fc = inc.forecast || { monthly: [], payments: [], by_position: [], next_12m: 0 };
  const now = (doc.as_of || new Date().toISOString()).slice(0, 7);
  // letzte 11 Monate + aktueller Monat (erhalten) und die folgenden Monate der Prognose
  const cats = [...Array.from({ length: 12 }, (_, i) => addMonths(now, i - 11)), ...fc.monthly.map((m) => m.month).filter((m) => m > now)];
  const received = new Map((inc.monthly || []).map((m) => [m.month, m.dividends]));
  const expected = new Map(fc.monthly.map((m) => [m.month, m.amount]));
  const years = (inc.by_year || []).slice().reverse();
  const lastGrowth = years.find((y) => y.growth !== null && y.growth !== undefined);
  const interest12 = (inc.monthly || []).filter((m) => m.month > addMonths(now, -12)).reduce((a, m) => a + (m.interest || 0), 0);
  const table = sortableTable("div-table", [
    { key: "name", label: "Position", render: (p) => html`<a href="#/positionen/${encodeURIComponent(p.isin)}">${p.name}</a><div class="muted small">${p.frequency_label}</div>` },
    { key: "amount", label: "Erwartet 12 M", num: true, render: (p) => fmt.eur(p.amount, 2) },
    { key: "trailing_12m", label: "Erhalten 12 M", num: true, render: (p) => fmt.eur(p.trailing_12m, 2) },
    { key: "yield_current", label: "Rendite", num: true, render: (p) => fmt.pct(p.yield_current, 2) },
    { key: "yield_on_cost", label: "auf Einstand", num: true, render: (p) => fmt.pct(p.yield_on_cost, 2) },
    { key: "confidence", label: "Datenbasis", sortable: false, render: (p) => { const [c, t] = CONFIDENCE[p.confidence] || CONFIDENCE.low; return badge(c, t); } },
  ], fc.by_position, { sortKey: "amount", empty: "Noch keine Ausschüttungen im Depot." });

  return {
    content: html`
      ${tiles([
        { label: "Erwartet", side: "nächste 12 Monate", value: fmt.eur(fc.next_12m, 0), note: html`≈ ${fmt.eur((fc.next_12m || 0) / 12, 0)} pro Monat · Schätzung` },
        { label: "Dividendenrendite", value: fmt.pct(fc.yield_current, 2), note: "erwartete Ausschüttungen ÷ heutiger Wert" },
        { label: "Rendite auf Einstand", side: "Yield on Cost", value: fmt.pct(fc.yield_on_cost, 2), note: "erwartete Ausschüttungen ÷ dein Kaufpreis" },
        { label: "Erhalten", side: "12 Monate", value: fmt.eur(inc.trailing_12m, 0), note: lastGrowth ? html`${fmt.pct(lastGrowth.growth, 0, true)} in ${lastGrowth.year} ggü. Vorjahr` : "netto, wie gutgeschrieben" },
      ])}
      <h2>Erhalten und erwartet je Monat</h2>
      ${cats.length ? html`<figure data-chart="divs"></figure>` : ""}
      <p class="muted small">Prognose: ${fc.basis}. Ausschüttungen können gekürzt oder verschoben werden – die Werte sind Schätzungen, netto nach einbehaltener Steuer.</p>

      <h2>Dividendenkalender</h2>
      <ol class="div-calendar">${fc.monthly.filter((m, i) => i < 12 || m.amount).map((m) => html`<li class="div-month ${m.amount ? "" : "empty"}">
        <div class="div-month-head"><b>${fmt.monthLong(m.month)}</b><span class="num">${m.amount ? fmt.eur(m.amount, 2) : "–"}</span></div>
        ${m.items.length
          ? html`<ul class="plain small">${m.items.map((it) => html`<li><span class="muted">${fmt.dateShort(it.date)}</span> <a href="#/positionen/${encodeURIComponent(it.isin)}" title="${it.name}">${it.name}</a> <span class="num">${fmt.eur(it.amount, 2)}</span></li>`)}</ul>`
          : html`<p class="muted small">keine Zahlung erwartet</p>`}
      </li>`)}</ol>

      <h2>Nach Position</h2>
      ${table.html}
      ${(inc.by_position || []).length ? html`<h2>Seit Beginn erhalten</h2>${rankedBars(inc.by_position.map((p) => ({ label: p.name, value: p.dividends, href: `#/positionen/${encodeURIComponent(p.isin)}` })), { format: (v) => fmt.eur(v, 2) })}` : ""}
      ${years.length ? html`<h2>Je Jahr</h2><div class="table-scroll"><table class="table"><thead><tr><th scope="col">Jahr</th><th class="num" scope="col">Ausschüttungen</th><th class="num" scope="col">ggü. Vorjahr</th></tr></thead>
        <tbody>${years.map((y) => html`<tr><th scope="row">${y.year}</th><td class="num">${fmt.eur(y.dividends, 2)}</td><td class="num">${y.growth === null || y.growth === undefined ? html`<span class="muted">–</span>` : fmt.pct(y.growth, 1, true)}</td></tr>`)}</tbody></table></div>
        <p class="muted small">Die Veränderung erscheint erst ab dem zweiten vollen Jahr – das erste Jahr ist meist angebrochen.${interest12 ? html` Zinsen auf Guthaben in den letzten 12 Monaten: ${fmt.eur(interest12, 2)}.` : ""}</p>` : ""}
    `,
    charts: {
      divs: {
        type: "bar",
        stacked: true,
        label: "Ausschüttungen je Monat: erhalten (letzte 12 Monate) und erwartet (nächste 12 Monate)",
        categories: cats,
        xFormat: (m) => fmt.monthLabel(m),
        categoryLabel: "Monat",
        series: [
          { name: "Erhalten", cls: "c1", values: cats.map((m) => received.get(m) || 0) },
          { name: "Erwartet (Schätzung)", cls: "c2", values: cats.map((m) => (m >= now ? expected.get(m) || 0 : 0)) },
        ],
        y: { format: (v) => fmt.eur(v, 2), axis: (v) => fmt.eur(v, 0) },
      },
    },
    mount(panel) { table.mount(panel); },
  };
}
