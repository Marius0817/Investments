import { meter, mountTreemap } from "../charts.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { badge, money, pageHead, percent, segmented, sortableTable, tag } from "../ui.js";
import { hasData, noData, protection } from "./common.js";

let statusFilter = "open";
let view = "table";
let period = "1m";
const sortState = { key: null, dir: "desc" };

const PERIODS = [["1w", "1 Woche"], ["1m", "1 Monat"], ["3m", "3 Monate"], ["ytd", "seit Jahresbeginn"], ["1y", "1 Jahr"], ["max", "seit Kauf"]];
// Schwellen je Zeitraum für die divergierende Farbskala (drei Stufen je Richtung + neutrale Mitte)
const SCALE = { "1w": 0.03, "1m": 0.06, "3m": 0.1, ytd: 0.15, "1y": 0.2, max: 0.3 };

function perfOf(p) {
  return period === "max" ? p.unrealized_pct : (p.performance || {})[period];
}

function heatClass(v) {
  if (v === null || v === undefined) return "hm-na";
  const s = SCALE[period];
  const a = Math.abs(v);
  const step = a < s / 6 ? 0 : a < s / 2 ? 1 : a < s ? 2 : 3;
  return step === 0 ? "hm-0" : `hm-${v > 0 ? "p" : "n"}${step}`;
}

function heatmapHtml() {
  const s = SCALE[period];
  return html`<section class="card" aria-label="Heatmap der Positionen">
    <div class="filter-row"><label class="inline-field">Zeitraum<select id="hm-period">${PERIODS.map(([k, l]) => html`<option value="${k}" ${k === period ? "selected" : ""}>${l}</option>`)}</select></label>
      <div class="hm-legend" aria-hidden="true"><span>≤ ${fmt.pct(-s, 0)}</span>${["n3", "n2", "n1", "0", "p1", "p2", "p3"].map((k) => html`<span class="hm-swatch hm-${k}"></span>`)}<span>≥ ${fmt.pct(s, 0, true)}</span></div></div>
    <div class="treemap" id="treemap"></div>
    <p class="muted small">Fläche = Wert der Position, Farbe = Kursentwicklung im gewählten Zeitraum (blau gestiegen, rot gefallen; „seit Kauf“ = Kursgewinn auf den Einstand). Ein Klick öffnet die Position.</p>
  </section>`;
}

export async function render_(main) {
  const doc = await data.portfolio();
  if (!hasData(doc)) { render(main, html`${pageHead("Positionen")}${noData()}`); return null; }
  let stops = data.cached("stops");
  let disposeMap = () => {};
  const drawMap = () => {
    disposeMap();
    render($("#positions-body", main), heatmapHtml());
    const open = doc.positions.filter((p) => p.status === "open" && p.value > 0);
    disposeMap = mountTreemap($("#treemap", main), open.map((p) => ({ ...p, perf: perfOf(p), href: `#/positionen/${encodeURIComponent(p.isin)}` })), {
      colorClass: (r) => heatClass(r.perf),
      label: (r) => r.name.replace(/ \(Demo\)$/, ""),
      sub: (r) => `${r.perf === null || r.perf === undefined ? "keine Kurse" : fmt.pct(r.perf, 1, true)} · Anteil ${fmt.pct(r.value / total, 1)}`,
    });
  };
  const total = doc.positions.filter((p) => p.status === "open").reduce((a, p) => a + (p.value || 0), 0) || 1;
  const draw = () => {
    if (view === "heatmap") { drawMap(); return; }
    disposeMap();
    disposeMap = () => {};
    // Fokus auf einem Sortier-Schalter über das Neuzeichnen retten
    const active = document.activeElement;
    const focusSort = active && main.contains(active) && active.dataset ? active.dataset.sort : null;
    const rows = doc.positions.filter((p) => statusFilter === "all" || p.status === statusFilter);
    const total = doc.summary.securities_value || 1;
    const stopBy = Object.fromEntries(((stops && stops.positions) || []).map((p) => [p.isin, p]));
    const table = sortableTable("positions-table", [
      {
        key: "name", label: "Position",
        render: (p) => html`<a href="#/positionen/${encodeURIComponent(p.isin)}">${p.name}</a>${p.ticker ? tag(p.ticker) : ""}
          <div class="muted small">${fmt.KIND_LABELS[p.kind] || p.kind} · ${p.trades} Trade${p.trades === 1 ? "" : "s"}</div>`,
      },
      { key: "value", label: "Wert", num: true, render: (p) => money(p.value, { digits: 0 }) },
      {
        key: "weight", label: "Anteil", num: true,
        render: (p) => html`<div class="weight-cell"><span>${fmt.pct(p.value / total, 1)}</span>${meter(p.value / total)}</div>`,
      },
      {
        key: "unrealized_pct", label: "Kursgewinn", num: true,
        render: (p) => html`${percent(p.unrealized_pct)}<div class="small">${money(p.unrealized_pl, { sign: true, digits: 0 })}</div>`,
      },
      { key: "irr", label: "IRR p. a.", num: true, render: (p) => percent(p.irr) },
      { key: "irr_after_tax", label: "nach Steuern", num: true, render: (p) => percent(p.irr_after_tax) },
      { key: "dividends", label: "Erträge", num: true, render: (p) => (p.dividends ? money(p.dividends, { digits: 0 }) : fmt.DASH) },
      {
        key: "stop", label: "Stop-Schutz", sortable: false,
        render: (p) => {
          const s = stopBy[p.isin];
          if (!s) return p.stopguard && p.stopguard.phase ? html`<span class="badge">${p.stopguard.phase}</span>` : html`<span class="muted small">–</span>`;
          const [cls, text] = protection(s);
          return html`${badge(cls, text)}<div class="muted small">${s.target.stop ? `Stop ${fmt.eur(s.effective_stop || Number(s.target.stop))}` : "kein Stop"}</div>`;
        },
      },
    ], rows, { sortKey: "value", state: sortState, rowHref: (p) => `#/positionen/${encodeURIComponent(p.isin)}`, empty: "Keine Positionen in dieser Auswahl." });
    render($("#positions-body", main), html`<section class="card" aria-label="Positionen">${table.html}</section>`);
    table.mount(main);
    if (focusSort) { const el = $(`[data-sort="${focusSort}"]`, main); if (el) el.focus(); }
  };
  const open = doc.positions.filter((p) => p.status === "open").length;
  render(main, html`
    ${pageHead("Positionen", {
      meta: html`<span><b>${open}</b> offen</span><span>Depotwert <b>${fmt.eur(doc.summary.securities_value, 0)}</b></span><span>unrealisiert <b>${fmt.signedEur(doc.summary.unrealized_pl, 0)}</b></span>`,
    })}
    <div class="filter-row" id="pos-filter">${segmented("view", "Darstellung", [["table", "Tabelle"], ["heatmap", "Heatmap"]], view)}
      <span id="status-filter" ${view === "heatmap" ? "hidden" : ""}>${segmented("status", "Positionen filtern", [["open", "Offen"], ["closed", "Geschlossen"], ["all", "Alle"]], statusFilter)}</span></div>
    <div id="positions-body"></div>
    <p class="muted small">IRR p. a.: Jahresrendite aus allen Käufen, Verkäufen und Ausschüttungen der Position (offene Stücke zum aktuellen Kurs). Kurzfristig gehaltene Positionen zeigen dabei oft extreme Werte.</p>
  `);
  draw();
  const select = (name, input) => $("#pos-filter", main).querySelectorAll(`input[name=${name}]`).forEach((i) => i.closest("label").classList.toggle("selected", i === input));
  const offs = [
    on($("#pos-filter", main), "change", "input[name=status]", (ev, input) => { statusFilter = input.value; select("status", input); draw(); }),
    on($("#pos-filter", main), "change", "input[name=view]", (ev, input) => {
      view = input.value;
      select("view", input);
      $("#status-filter", main).hidden = view === "heatmap";
      draw();
    }),
    on(main, "change", "#hm-period", (ev, sel) => { period = sel.value; drawMap(); $("#hm-period", main).focus(); }),
  ];
  const off = () => { offs.forEach((o) => o()); disposeMap(); };
  // Stop-Schutz kommt von Scalable und dauert länger – Tabelle danach ergänzen.
  if (!stops) data.stops().then((s) => { stops = s; if (main.isConnected) draw(); }).catch(() => {});
  return off;
}

export { render_ as render };
