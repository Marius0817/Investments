import { meter } from "../charts.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { badge, money, pageHead, percent, segmented, sortableTable, tag } from "../ui.js";
import { hasData, noData, protection } from "./common.js";

let statusFilter = "open";
const sortState = { key: null, dir: "desc" };

export async function render_(main) {
  const doc = await data.portfolio();
  if (!hasData(doc)) { render(main, html`${pageHead("Positionen")}${noData()}`); return null; }
  let stops = data.cached("stops");
  const draw = () => {
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
    <div class="filter-row" id="pos-filter">${segmented("status", "Positionen filtern", [["open", "Offen"], ["closed", "Geschlossen"], ["all", "Alle"]], statusFilter)}</div>
    <div id="positions-body"></div>
    <p class="muted small">IRR p. a.: Jahresrendite aus allen Käufen, Verkäufen und Ausschüttungen der Position (offene Stücke zum aktuellen Kurs). Kurzfristig gehaltene Positionen zeigen dabei oft extreme Werte.</p>
  `);
  draw();
  const off = on($("#pos-filter", main), "change", "input[name=status]", (ev, input) => {
    statusFilter = input.value;
    $("#pos-filter", main).querySelectorAll("label").forEach((l) => l.classList.toggle("selected", l.contains(input)));
    draw();
  });
  // Stop-Schutz kommt von Scalable und dauert länger – Tabelle danach ergänzen.
  if (!stops) data.stops().then((s) => { stops = s; if (main.isConnected) draw(); }).catch(() => {});
  return off;
}

export { render_ as render };
