// Abschnitte nach dem Vorbild von extraETF: Risikokennzahlen, Monatsrenditen, laufende Kosten (TER),
// Vorabpauschale-Vorschau und ETF-Überschneidungen. Alle Werte kommen fertig vom Server.

import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, segmented, tiles } from "../ui.js";

const MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];

function ratio(v) {
  return v === null || v === undefined ? fmt.DASH : fmt.num(v, 2);
}

// ---------------------------------------------------------------- Risiko

export function riskSection(doc) {
  const r = (doc.summary || {}).risk;
  const w = r && r.windows ? r.windows.since_start : null;
  const y = r && r.windows ? r.windows["1y"] : null;
  const bench = doc.settings.benchmark.name;
  if (!w) {
    return html`<h2>Risiko</h2><p class="muted">Für Risikokennzahlen braucht es mindestens ein paar Monate Kursverlauf. Sie erscheinen, sobald genug Daten vorliegen.</p>`;
  }
  const one = (key, f) => (y && y[key] !== null && y[key] !== undefined ? html` · 12 Monate ${f(y[key])}` : "");
  return html`
    <h2>Risiko</h2>
    ${tiles([
      { label: "Schwankung", side: "p. a.", value: fmt.pct(w.volatility, 1), note: html`${bench}: ${fmt.pct(w.benchmark_volatility, 1)}${one("volatility", (v) => fmt.pct(v, 1))}` },
      { label: "Sharpe-Ratio", value: ratio(w.sharpe), note: html`Rendite über dem risikofreien Zins je Einheit Schwankung${one("sharpe", ratio)}` },
      { label: "Sortino-Ratio", value: ratio(w.sortino), note: "wie Sharpe, zählt aber nur Schwankung nach unten" },
      { label: "Größter Rückgang", value: fmt.pct(w.max_drawdown, 1), note: html`${bench}: ${fmt.pct(w.benchmark_max_drawdown, 1)}` },
      { label: "Beta", side: bench, value: ratio(w.beta), note: "1 = bewegt sich wie der Benchmark, darunter ruhiger" },
      { label: "Korrelation", side: bench, value: ratio(w.correlation), note: html`Gleichlauf mit dem Benchmark (−1 bis 1)${w.tracking_error !== null && w.tracking_error !== undefined ? html` · Tracking Error ${fmt.pct(w.tracking_error, 1)}` : ""}` },
    ])}
    <p class="muted small">Seit ${fmt.date(w.from)}: ${fmt.pct(w.return_cumulative, 1, true)}${w.return_annualised !== null ? html` (${fmt.pct(w.return_annualised, 1, true)} p. a.)` : ""}
      · ${fmt.pct(w.positive_periods, 0)} der ${r.resolution === "daily" ? "Tage" : r.resolution === "monthly" ? "Monate" : "Wochen"} im Plus
      ${r.best_month ? html` · bester Monat ${fmt.monthLong(r.best_month.month)} ${fmt.pct(r.best_month.twr, 1, true)}, schwächster ${fmt.monthLong(r.worst_month.month)} ${fmt.pct(r.worst_month.twr, 1, true)}` : ""}.</p>
    <details class="details-table"><summary>Was bedeuten die Kennzahlen?</summary>
      <dl class="small glossary">
        <dt>Schwankung (Volatilität)</dt><dd>Wie stark die Rendite um ihren Durchschnitt schwankt, hochgerechnet auf ein Jahr. Breite Aktien-ETFs liegen langfristig oft bei 15–20 %.</dd>
        <dt>Sharpe-Ratio</dt><dd>(Rendite p. a. − risikofreier Zins) ÷ Schwankung. Je höher, desto mehr Rendite pro eingegangenem Risiko. Risikofreier Zins: ${fmt.pct(r.risk_free, 1)} p. a. (<code>portfolio.risk_free_pct</code>).</dd>
        <dt>Sortino-Ratio</dt><dd>Wie die Sharpe-Ratio, bestraft aber nur Rückgänge – Ausschläge nach oben zählen nicht als Risiko.</dd>
        <dt>Beta und Korrelation</dt><dd>Wie stark und wie gleichläufig sich dein Depot mit ${bench} bewegt. Beta 0,8 heißt: fällt der Benchmark um 10 %, fiel dein Depot im Schnitt um 8 %.</dd>
        <dt>Tracking Error</dt><dd>Wie stark deine Rendite von der des Benchmarks abweicht (Schwankung der Differenz).</dd>
      </dl>
      <p class="muted small">${r.note}</p>
    </details>
  `;
}

// ---------------------------------------------------------------- Monatsrenditen

const MONTH_MODES = [["twr", "Depot"], ["benchmark", "Benchmark"], ["diff", "Differenz"]];
let monthMode = "twr";

function monthClass(v) {
  if (v === null || v === undefined) return "";
  const a = Math.abs(v);
  const step = a < 0.01 ? 0 : a < 0.03 ? 1 : a < 0.06 ? 2 : 3;
  return step === 0 ? "hm-0" : `hm-${v > 0 ? "p" : "n"}${step}`;
}

function monthValue(m) {
  if (!m) return null;
  if (monthMode === "diff") return m.twr !== null && m.benchmark !== null ? m.twr - m.benchmark : null;
  return m[monthMode];
}

function monthlyTable(doc) {
  const months = (doc.periods || {}).months || [];
  const years = [...new Set(months.map((m) => m.month.slice(0, 4)))].sort().reverse();
  const byKey = new Map(months.map((m) => [m.month, m]));
  const yearRow = new Map(((doc.periods || {}).years || []).map((y) => [String(y.year), y]));
  const label = MONTH_MODES.find(([k]) => k === monthMode)[1];
  return html`<div class="table-scroll"><table class="table month-table">
    <caption class="sr-only">Monatsrenditen ${label}, zeitgewichtet</caption>
    <thead><tr><th scope="col">Jahr</th>${MONTHS.map((m) => html`<th scope="col" class="num">${m}</th>`)}<th scope="col" class="num">Gesamt</th></tr></thead>
    <tbody>${years.map((y) => {
      const yr = yearRow.get(y);
      const total = yr ? (monthMode === "diff" ? (yr.twr !== null && yr.benchmark !== null ? yr.twr - yr.benchmark : null) : yr[monthMode]) : null;
      return html`<tr><th scope="row">${y}</th>${MONTHS.map((_, i) => {
        const m = byKey.get(`${y}-${String(i + 1).padStart(2, "0")}`);
        const v = monthValue(m);
        return html`<td class="num month-cell ${monthClass(v)} ${m && m.partial ? "partial" : ""}">${v === null || v === undefined ? "" : fmt.pct(v, 1, true)}${m && m.partial ? html`<span class="sr-only"> (angebrochen)</span>` : ""}</td>`;
      })}<td class="num month-total">${total === null || total === undefined ? "–" : fmt.pct(total, 1, true)}${yr && yr.partial ? "*" : ""}</td></tr>`;
    })}</tbody>
  </table></div>`;
}

export function monthlySection(doc) {
  const months = (doc.periods || {}).months || [];
  if (!months.length) return "";
  return html`
    <h2>Monatsrenditen</h2>
    <div class="filter-row" id="month-mode">${segmented("month-mode", "Anzeige", MONTH_MODES, monthMode)}</div>
    <div id="month-table">${monthlyTable(doc)}</div>
    <p class="muted small">Zeitgewichtet, also ohne Einfluss deiner Ein- und Auszahlungen. Kursiv = angebrochener Monat, * = angebrochenes Jahr.
      Blau = gestiegen, rot = gefallen; das Vorzeichen steht immer dabei.</p>
  `;
}

export function mountMonthly(panel, doc) {
  const box = $("#month-mode", panel);
  if (!box) return () => {};
  return on(box, "change", "input[name=month-mode]", (ev, input) => {
    monthMode = input.value;
    box.querySelectorAll("label").forEach((l) => l.classList.toggle("selected", l.contains(input)));
    render($("#month-table", panel), monthlyTable(doc));
  });
}

// ---------------------------------------------------------------- Laufende Kosten (TER)

export function terSection(doc) {
  const o = (doc.costs || {}).ongoing;
  if (!o) return "";
  const p20 = (o.projection || []).find((x) => x.years === 20);
  return html`
    <h2>Laufende Produktkosten (TER)</h2>
    <p class="muted small">Die laufenden Kosten der Fonds werden nicht gebucht, sondern stecken im Kurs – deshalb fehlen sie in den Kosten oben.</p>
    ${o.weighted_ter === null
      ? html`<p class="muted">Für deine Fonds ist noch keine TER hinterlegt.</p>`
      : tiles([
        { label: "Laufende Kosten", side: "gewichtet", value: fmt.pct(o.weighted_ter, 2), note: "p. a. über das ganze Depot (Aktien mit 0 %)" },
        { label: "Pro Jahr", value: fmt.eur(o.annual_cost, 0), note: "beim heutigen Depotwert" },
        { label: "Kosteneffekt", side: "20 Jahre", value: p20 ? fmt.eur(p20.cost_effect, 0) : fmt.DASH, note: html`weniger Vermögen bei ${fmt.pct(o.assumed_return, 0)} Rendite p. a., ohne Einzahlungen` },
      ])}
    ${(o.by_position || []).length ? html`<div class="table-scroll"><table class="table stacked">
      <thead><tr><th scope="col">Fonds</th><th scope="col" class="num">Wert</th><th scope="col" class="num">TER p. a.</th><th scope="col" class="num">Kosten pro Jahr</th></tr></thead>
      <tbody>${o.by_position.map((r) => html`<tr><th scope="row"><a href="#/positionen/${encodeURIComponent(r.isin)}">${r.name}</a></th>
        <td class="num" data-label="Wert">${fmt.eur(r.value, 0)}</td><td class="num" data-label="TER p. a.">${fmt.pct(r.ter, 2)}</td><td class="num" data-label="Kosten pro Jahr">${fmt.eur(r.annual_cost, 2)}</td></tr>`)}</tbody>
    </table></div>` : ""}
    ${(o.missing || []).length ? alertBox("info", html`<b>Ohne TER:</b> ${o.missing.map((m, i) => html`${i ? ", " : ""}${m.name}`)}. Aus dem Factsheet ergänzen, z. B.
      <code>[instruments.${o.missing[0].isin}] ter = 0.20</code> (Prozent p. a.).`, { small: true }) : ""}
    <p class="muted small">${o.note}</p>
  `;
}

// ---------------------------------------------------------------- Vorabpauschale

export function vapSection(doc) {
  const v = (doc.taxes || {}).vorabpauschale_preview;
  if (!v) return "";
  const next = v.year + 1;
  if (!v.available) {
    return html`<h2>Vorabpauschale Anfang ${next}</h2><p class="muted">Der Basiszins ${v.year} ist noch nicht hinterlegt (<code>tax.basiszins = { ${v.year} = … }</code>).</p>`;
  }
  return html`
    <h2>Vorabpauschale Anfang ${next}</h2>
    <p class="muted small">Für thesaurierende Fonds wird jedes Jahr ein Teil des Wertzuwachses vorab versteuert – Basiszins ${v.year}: ${fmt.num(v.basiszins, 2)} %.
      Scalable zieht die Steuer Anfang ${next} vom Verrechnungskonto ein.</p>
    ${v.by_position.length
      ? html`${tiles([
          { label: "Vorabpauschale", side: "geschätzt", value: fmt.eur(v.total_vap, 0), note: html`davon steuerpflichtig ${fmt.eur(v.total_taxable, 0)} (Teilfreistellung)` },
          { label: "Steuer darauf", side: "ohne Freibetrag", value: fmt.eur(v.tax_before_allowance, 0), note: "wenn der Freistellungsauftrag schon verbraucht ist" },
          { label: "Steuer fällig", side: `Freibetrag ${next}`, value: fmt.eur(v.tax_after_allowance, 0), note: html`nach ${fmt.eur(v.allowance_next_year, 0)} Freistellungsauftrag ${next}` },
        ])}
        <div class="table-scroll"><table class="table stacked">
          <thead><tr><th scope="col">Fonds</th><th scope="col" class="num">Vorabpauschale</th><th scope="col" class="num">steuerpflichtig</th><th scope="col" class="num">Steuer max.</th></tr></thead>
          <tbody>${v.by_position.map((r) => html`<tr><th scope="row"><a href="#/positionen/${encodeURIComponent(r.isin)}">${r.name}</a></th>
            <td class="num" data-label="Vorabpauschale">${fmt.eur(r.vap, 2)}</td><td class="num" data-label="steuerpflichtig">${fmt.eur(r.taxable, 2)}</td><td class="num" data-label="Steuer max.">${fmt.eur(r.tax, 2)}</td></tr>`)}</tbody>
        </table></div>`
      : html`<p class="muted">Keine thesaurierenden Fonds mit Wertzuwachs in ${v.year} – keine Vorabpauschale zu erwarten.</p>`}
    <p class="muted small">${v.note}</p>
  `;
}

// ---------------------------------------------------------------- ETF-Überschneidungen

function simClass(v) {
  if (v === null || v === undefined) return "";
  return v >= 0.9 ? "hm-p3" : v >= 0.7 ? "hm-p2" : v >= 0.4 ? "hm-p1" : "hm-0";
}

export function overlapSection(doc) {
  const fo = (doc.allocation || {}).fund_overlap;
  if (!fo || (fo.funds || []).length < 2) {
    return html`<h2>ETF-Überschneidungen</h2><p class="muted small">Sobald für mindestens zwei Fonds Länder oder Branchen hinterlegt sind (<code>[instruments.&lt;ISIN&gt;.exposure]</code>), zeigt stopguard hier, wie stark sie sich doppeln.</p>`;
  }
  const short = (n) => n.replace(/ \(Demo\)$/, "");
  const sim = new Map();
  for (const p of fo.pairs) {
    sim.set(`${p.a.isin}|${p.b.isin}`, p);
    sim.set(`${p.b.isin}|${p.a.isin}`, p);
  }
  return html`
    <h2>ETF-Überschneidungen</h2>
    <p class="muted small">Wie ähnlich sich deine Fonds nach Ländern und Branchen sind. Hohe Werte heißen: Ein zweiter Fonds streut kaum zusätzlich.</p>
    <div class="table-scroll"><table class="table overlap-table">
      <caption class="sr-only">Ähnlichkeit der Fonds untereinander in Prozent</caption>
      <thead><tr><th scope="col"><span class="sr-only">Fonds</span></th>${fo.funds.map((f) => html`<th scope="col" class="num" title="${f.name}">${short(f.name)}</th>`)}</tr></thead>
      <tbody>${fo.funds.map((a) => html`<tr><th scope="row">${short(a.name)}</th>${fo.funds.map((b) => {
        if (a.isin === b.isin) return html`<td class="num muted">–</td>`;
        const p = sim.get(`${a.isin}|${b.isin}`);
        return html`<td class="num overlap-cell ${simClass(p && p.similarity)}">${p ? fmt.pct(p.similarity, 0) : "?"}</td>`;
      })}</tr>`)}</tbody>
    </table></div>
    <ul class="plain small overlap-list">${fo.pairs.slice(0, 4).map((p) => html`<li><b>${short(p.a.name)}</b> ↔ <b>${short(p.b.name)}</b>: ${fmt.pct(p.similarity, 0)} ähnlich
      (Länder ${p.countries === null ? "–" : fmt.pct(p.countries, 0)}, Branchen ${p.sectors === null ? "–" : fmt.pct(p.sectors, 0)}${p.shared_holdings.length ? html`; gemeinsame größte Positionen: ${p.shared_holdings.slice(0, 5).join(", ")}` : ""})</li>`)}</ul>
    <p class="muted small">${fo.method}</p>
  `;
}
