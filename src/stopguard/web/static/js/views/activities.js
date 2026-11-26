// Alle Buchungen des Verrechnungskontos (wie „Aktivitäten“ in Portfolio-Trackern): filtern, suchen, als CSV sichern.

import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { money, pageHead, sortableTable } from "../ui.js";
import { hasData, noData, tradesSubnav } from "./common.js";

const GROUPS = [
  ["all", "Alle Arten", null],
  ["trades", "Käufe und Verkäufe", ["buy", "sell"]],
  ["income", "Ausschüttungen und Zinsen", ["dividend", "interest_income"]],
  ["cash", "Ein- und Auszahlungen", ["deposit", "withdrawal", "transfer_in", "transfer_out"]],
  ["costs", "Gebühren, Steuern, Sollzinsen", ["fee", "tax", "tax_refund", "loan_interest"]],
];
const PAGE = 100;
const filters = { group: "all", year: "all", q: "" };
const sortState = { key: null, dir: "desc" };

// Beschreibungen von Scalable sind teils englisch oder wiederholen nur die Art
const DESCRIPTIONS = { "Savings plan": "Sparplan", Buy: null, Sell: null, Deposit: null, Withdrawal: null };
function describe(a) {
  const d = (a.description || "").trim();
  if (!d || d === fmt.ACTIVITY_LABELS[a.kind]) return null;
  return d in DESCRIPTIONS ? DESCRIPTIONS[d] : d;
}

function applyFilters(list) {
  const kinds = GROUPS.find(([k]) => k === filters.group)[2];
  const q = filters.q.trim().toLowerCase();
  return list.filter((a) =>
    (!kinds || kinds.includes(a.kind)) &&
    (filters.year === "all" || a.date.startsWith(filters.year)) &&
    (!q || (a.name || "").toLowerCase().includes(q) || (a.isin || "").toLowerCase().includes(q) || (a.description || "").toLowerCase().includes(q) || (fmt.ACTIVITY_LABELS[a.kind] || "").toLowerCase().includes(q)));
}

function csv(list) {
  // Excel (de) erwartet Semikolon und Dezimalkomma
  const num = (v) => (v === null || v === undefined ? "" : String(v).replace(".", ","));
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["Datum", "Art", "Wertpapier", "ISIN", "Stück", "Kurs", "Betrag (EUR)", "Gebühren", "Steuer", "Beschreibung"]];
  for (const a of list) {
    rows.push([a.date.slice(0, 10), fmt.ACTIVITY_LABELS[a.kind] || a.kind, a.name || "", a.isin || "", num(a.units), num(a.price), num(a.amount), num(a.fees), num(a.tax), a.description || ""]);
  }
  return "﻿" + rows.map((r) => r.map((v, i) => ([4, 5, 6, 7, 8].includes(i) ? v : esc(v))).join(";")).join("\r\n");
}

export async function render_(main) {
  const doc = await data.portfolio();
  if (!hasData(doc)) { render(main, html`${pageHead("Buchungen")}${noData()}`); return null; }
  const all = doc.activities || [];
  const years = [...new Set(all.map((a) => a.date.slice(0, 4)))].sort().reverse();
  let shown = PAGE;
  render(main, html`
    ${pageHead("Buchungen", { meta: html`<span><b>${all.length}</b> Buchungen von Scalable</span><span>importierte Notion-Trades erscheinen unter Trades</span>` })}
    ${tradesSubnav("buchungen")}
    <form class="filter-row" id="act-filter" role="search" aria-label="Buchungen filtern">
      <label class="inline-field">Art<select name="group">${GROUPS.map(([k, l]) => html`<option value="${k}" ${k === filters.group ? "selected" : ""}>${l}</option>`)}</select></label>
      <label class="inline-field">Jahr<select name="year"><option value="all">alle</option>${years.map((y) => html`<option value="${y}" ${y === filters.year ? "selected" : ""}>${y}</option>`)}</select></label>
      <label class="inline-field grow">Suche<input type="text" name="q" value="${filters.q}" placeholder="Wertpapier, ISIN, Beschreibung"></label>
      <button type="button" class="btn small" data-action="csv">Als CSV sichern</button>
    </form>
    <section class="card" aria-labelledby="h-act">
      <div class="card-head"><h2 id="h-act">Buchungen</h2><span class="muted small" id="act-sum"></span></div>
      <div id="act-table"></div>
      <div id="act-more"></div>
    </section>
  `);
  const draw = () => {
    const list = applyFilters(all);
    const inflow = list.filter((a) => a.amount > 0).reduce((s, a) => s + a.amount, 0);
    const outflow = list.filter((a) => a.amount < 0).reduce((s, a) => s + a.amount, 0);
    $("#act-sum", main).textContent = `${list.length} Einträge · Zuflüsse ${fmt.eur(inflow, 0)} · Abflüsse ${fmt.eur(-outflow, 0)}`;
    const table = sortableTable("activities-table", [
      { key: "date", label: "Datum", render: (a) => html`${fmt.date(a.date)}<div class="muted small">${fmt.time(a.date)} Uhr</div>` },
      { key: "kind", label: "Art", sort: (a) => fmt.ACTIVITY_LABELS[a.kind] || a.kind, render: (a) => { const d = describe(a); return html`${fmt.ACTIVITY_LABELS[a.kind] || a.kind}${d ? html`<div class="muted small">${d}</div>` : ""}`; } },
      { key: "name", label: "Wertpapier", render: (a) => (a.isin ? html`<a href="#/positionen/${encodeURIComponent(a.isin)}">${a.name || a.isin}</a>` : html`<span class="muted">–</span>`) },
      { key: "units", label: "Stück", num: true, render: (a) => (a.units ? fmt.units(a.units) : html`<span class="muted">–</span>`) },
      { key: "price", label: "Kurs", num: true, render: (a) => (a.price ? fmt.eur(a.price) : html`<span class="muted">–</span>`) },
      { key: "costs", label: "Gebühren/Steuer", num: true, sort: (a) => (a.fees || 0) + (a.tax || 0), render: (a) => (a.fees || a.tax ? fmt.eur((a.fees || 0) + (a.tax || 0)) : html`<span class="muted">–</span>`) },
      { key: "amount", label: "Betrag", num: true, render: (a) => money(a.amount, { sign: true }) },
    ], list, { sortKey: "date", state: sortState, limit: shown, empty: "Keine Buchungen für diese Auswahl." });
    render($("#act-table", main), table.html);
    table.mount(main);
    render($("#act-more", main), list.length > shown
      ? html`<div class="row"><button type="button" class="btn small" data-action="more">Weitere ${Math.min(PAGE, list.length - shown)} anzeigen</button><span class="muted small">${shown} von ${list.length}</span></div>`
      : "");
  };
  draw();
  const form = $("#act-filter", main);
  const update = () => {
    const fd = new FormData(form);
    filters.group = fd.get("group");
    filters.year = fd.get("year");
    filters.q = fd.get("q") || "";
    shown = PAGE;
    draw();
  };
  const offs = [
    on(form, "change", "select", update),
    on(form, "input", "input[name=q]", update),
    on(main, "click", "[data-action=more]", () => { shown += PAGE; draw(); }),
    on(main, "click", "[data-action=csv]", () => {
      const blob = new Blob([csv(applyFilters(all))], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `stopguard-buchungen-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    }),
  ];
  form.addEventListener("submit", (ev) => ev.preventDefault());
  return () => offs.forEach((o) => o());
}

export { render_ as render };
