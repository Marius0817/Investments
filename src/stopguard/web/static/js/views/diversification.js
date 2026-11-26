// Auswertung „Diversifikation“: Aufteilung nach Region, Land, Branche, Anlageklasse, Währung
// inklusive Durchschau durch ETFs (Gewichte aus der Konfiguration, siehe README).

import { rankedBars, stackedBars } from "../charts.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, segmented, tiles } from "../ui.js";
import { overlapSection } from "./extras.js";

const DIMS = [
  ["region", "Region"],
  ["country", "Land"],
  ["sector", "Branche"],
  ["asset_class", "Anlageklasse"],
  ["currency", "Währung"],
];
const MUTED_KEYS = new Set(["Nicht zugeordnet", "Sonstige"]);
let dim = "region";

function topKey(list) {
  const r = (list || []).find((x) => !MUTED_KEYS.has(x.key));
  return r || null;
}

function dimView(alloc) {
  const rows = alloc[`by_${dim}`] || [];
  const coverage = (alloc.coverage || {})[dim];
  const derived = (alloc.derived_share || {})[dim];
  const missing = (alloc.unassigned || {})[dim] || [];
  const label = DIMS.find(([k]) => k === dim)[1];
  return html`
    ${rows.length
      ? rankedBars(rows.map((r) => ({ label: r.key, value: r.weight, cls: MUTED_KEYS.has(r.key) ? "deemph" : "c1" })), { format: (v) => fmt.pct(v, 1) })
      : html`<p class="muted">Keine Werte.</p>`}
    <p class="muted small">${coverage !== undefined ? html`${fmt.pct(coverage, 0)} des Depots zugeordnet` : ""}${derived ? html`, davon ${fmt.pct(derived, 0)} über das Länderkürzel der ISIN abgeleitet (Sitz des Unternehmens – eine Näherung)` : ""}.</p>
    <details class="details-table"><summary>${label} als Tabelle</summary>
      <div class="table-scroll"><table class="table"><thead><tr><th scope="col">${label}</th><th class="num" scope="col">Anteil</th><th class="num" scope="col">Wert</th><th scope="col">Größte Beiträge</th></tr></thead>
      <tbody>${rows.map((r) => html`<tr><th scope="row">${r.key}</th><td class="num">${fmt.pct(r.weight, 1)}</td><td class="num">${fmt.eur(r.value, 0)}</td>
        <td class="small">${(r.positions || []).slice(0, 4).map((p, i) => html`${i ? ", " : ""}${p.name} <span class="muted">${fmt.eur(p.value, 0)}</span>`)}</td></tr>`)}</tbody></table></div>
    </details>
    ${missing.length ? alertBox("info", html`<b>${missing.length} Position${missing.length === 1 ? "" : "en"} ohne ${label}:</b> ${missing.map((m, i) => html`${i ? ", " : ""}${m.name}`)}.
      In der Konfiguration ergänzen, z. B. <code>[instruments.${missing[0].isin}] ${dim === "currency" ? 'currency = "USD"' : dim === "asset_class" ? 'asset_class = "Anleihen"' : dim === "sector" ? 'sector = "Gesundheit"' : 'country = "US"'}</code>
      oder für ETFs <code>[instruments.${missing[0].isin}.exposure] ${dim === "sector" ? "sectors" : dim === "currency" ? "currencies" : "countries"} = { … }</code> (Prozent laut Factsheet).`, { small: true }) : ""}
  `;
}

export function diversificationPanel(doc) {
  const alloc = doc.allocation || {};
  const look = alloc.look_through || { items: [], overlap: [] };
  const region = topKey(alloc.by_region);
  const sector = topKey(alloc.by_sector);
  const country = topKey(alloc.by_country);
  const single = (look.items || [])[0];
  const items = (look.items || []).slice(0, 12).map((i) => ({
    label: i.name,
    direct: i.direct,
    via_funds: i.via_funds,
    href: i.isin && doc.positions.some((p) => p.isin === i.isin) ? `#/positionen/${encodeURIComponent(i.isin)}` : "",
  }));
  return {
    content: html`
      ${tiles([
        { label: "Größte Region", value: region ? fmt.pct(region.weight, 0) : fmt.DASH, note: region ? region.key : "noch nicht zugeordnet" },
        { label: "Größtes Land", value: country ? fmt.pct(country.weight, 0) : fmt.DASH, note: country ? country.key : "noch nicht zugeordnet" },
        { label: "Größte Branche", value: sector ? fmt.pct(sector.weight, 0) : fmt.DASH, note: sector ? sector.key : "noch nicht zugeordnet" },
        { label: "Größter Einzelwert", side: "inkl. ETFs", value: single ? fmt.pct(single.weight, 1) : fmt.DASH, note: single ? single.name : "–" },
      ])}
      <h2>Aufteilung</h2>
      <div class="filter-row" id="dim-filter">${segmented("dim", "Aufteilung nach", DIMS, dim)}</div>
      <div id="dim-view" aria-live="polite">${dimView(alloc)}</div>

      <h2>Durchschau: größte Einzelwerte</h2>
      <p class="muted small">Direkt gehaltene Aktien plus ihr Anteil in deinen ETFs. Für ${look.funds_with_holdings || 0} Fonds sind die größten Positionen hinterlegt
        (<code>exposure.holdings</code> in der Konfiguration) – der Rest der Fonds ist breit gestreut und hier nicht einzeln aufgeführt.</p>
      ${items.length
        ? html`<ul class="chart-legend" aria-label="Legende"><li><span class="key rect c1" aria-hidden="true"></span>direkt im Depot</li><li><span class="key rect c2" aria-hidden="true"></span>über ETFs</li></ul>
          ${stackedBars(items, [{ key: "direct", name: "direkt", cls: "c1" }, { key: "via_funds", name: "über ETFs", cls: "c2" }])}`
        : html`<p class="muted">Keine Einzelwerte.</p>`}
      ${(look.overlap || []).length ? alertBox("info", html`<b>Überschneidungen:</b> ${look.overlap.map((o, i) => html`${i ? "; " : ""}${o.name} – direkt ${fmt.eur(o.direct, 0)}, über ETFs ${fmt.eur(o.via_funds, 0)} (zusammen ${fmt.pct(o.weight, 1)})`)}.`, { small: true }) : ""}
      ${overlapSection(doc)}
    `,
    mount(panel) {
      return on($("#dim-filter", panel), "change", "input[name=dim]", (ev, input) => {
        dim = input.value;
        $("#dim-filter", panel).querySelectorAll("label").forEach((l) => l.classList.toggle("selected", l.contains(input)));
        render($("#dim-view", panel), dimView(alloc));
      });
    },
  };
}
