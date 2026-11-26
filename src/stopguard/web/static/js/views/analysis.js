import { meter, rankedBars } from "../charts.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, money, pageHead, percent, sortableTable, tiles } from "../ui.js";
import { hasData, mountCharts, noData, twrChartSpec } from "./common.js";
import { diversificationPanel } from "./diversification.js";
import { dividendsPanel } from "./dividends.js";
import { insightsPanel } from "./insights.js";
import { projectionPanel, rebalancingPanel } from "./planning.js";
import { monthlySection, mountMonthly, riskSection, terSection, vapSection } from "./extras.js";

const TABS = [
  ["performance", "Performance"],
  ["hinweise", "Hinweise"],
  ["diversifikation", "Diversifikation"],
  ["dividenden", "Dividenden"],
  ["rebalancing", "Rebalancing"],
  ["prognose", "Prognose"],
  ["kredit", "Kredit"],
  ["steuern", "Steuern"],
  ["kosten", "Kosten"],
];
const ALIASES = { ertraege: "dividenden" };

let focusTab = false;

export async function render_(main, [tabParam]) {
  const doc = await data.portfolio();
  if (!hasData(doc)) { render(main, html`${pageHead("Auswertung")}${noData()}`); return null; }
  const wanted = ALIASES[tabParam] || tabParam;
  const tab = TABS.some(([k]) => k === wanted) ? wanted : "performance";
  render(main, html`
    ${pageHead("Auswertung", { meta: html`<span>Stand <b>${fmt.dateTime(doc.as_of)}</b></span><span>Steuersatz <b>${fmt.pct(doc.settings.tax.effective_rate, 3)}</b></span>` })}
    <div class="tabs" role="tablist" aria-label="Auswertungen">${TABS.map(
      ([k, l]) => html`<button role="tab" type="button" id="tab-${k}" aria-controls="panel" aria-selected="${k === tab}" tabindex="${k === tab ? 0 : -1}" class="${k === tab ? "active" : ""}" data-tab="${k}">${l}${k === "hinweise" && (doc.insights || []).length ? html` <span class="tab-count">${doc.insights.length}</span>` : ""}</button>`,
    )}</div>
    <div class="card tab-panel" id="panel" role="tabpanel" aria-labelledby="tab-${tab}"></div>
  `);
  const active = $(`#tab-${tab}`, main);
  active.scrollIntoView({ block: "nearest", inline: "nearest" });
  // Mit Pfeiltasten gewechselt: Fokus bleibt auf dem Reiter (ARIA-Tabs-Muster).
  if (focusTab) { active.focus({ preventScroll: true }); focusTab = false; }
  const panel = $("#panel", main);
  const { content, charts, mount } = PANELS[tab](doc);
  render(panel, content);
  const dispose = mountCharts(panel, charts || {});
  const unmount = mount ? mount(panel) : null;
  const offTabs = on(main, "click", "[data-tab]", (ev, btn) => { location.hash = `#/auswertung/${btn.dataset.tab}`; });
  const offKeys = on(main, "keydown", "[role=tab]", (ev, btn) => {
    const i = TABS.findIndex(([k]) => k === btn.dataset.tab);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[ev.key];
    if (next === undefined) return;
    ev.preventDefault();
    focusTab = true;
    const k = TABS[(next + TABS.length) % TABS.length][0];
    location.hash = `#/auswertung/${k}`;
  });
  return () => { dispose(); offTabs(); offKeys(); if (typeof unmount === "function") unmount(); };
}

export { render_ as render };

const eurAxis = (v) => fmt.eurShort(v);

// ---------------------------------------------------------------- Performance

function performance(doc) {
  const t = doc.summary.twr || {};
  const irr = doc.summary.irr;
  const years = doc.periods.years || [];
  const bench = doc.settings.benchmark.name;
  const dd = (doc.timeseries.drawdown || []).map((r) => [r.date, r.drawdown]);
  // IRR erst ab drei Monaten Historie – davor sind Jahresrenditen extrem und wenig aussagekräftig
  const firstBuy = (doc.trades || []).reduce((a, t) => (t.open_date < a ? t.open_date : a), "9999-12-31");
  const irrFrom = new Date(new Date(firstBuy).getTime() + 91 * 86400000).toISOString().slice(0, 10);
  const irrRows = (doc.timeseries.irr || []).filter((r) => r.irr !== null && r.date >= irrFrom);
  return {
    content: html`
      ${tiles([
        { label: "Seit Start", side: "zeitgewichtet", value: fmt.pct(t.since_start, 1, true), toneValue: t.since_start, note: html`${bench}: ${fmt.pct(t.benchmark_since_start, 1, true)}` },
        { label: "Pro Jahr", side: "TWR", value: fmt.pct(t.annualized, 1, true), toneValue: t.annualized, note: t.annualized === null ? "Erst ab einem Jahr Historie." : "annualisiert" },
        { label: "Dieses Jahr", value: fmt.pct(t.ytd, 1, true), toneValue: t.ytd, note: "seit 1. Januar" },
        { label: "Letzte 12 Monate", value: fmt.pct(t.one_year, 1, true), toneValue: t.one_year, note: "zeitgewichtet" },
        { label: "Größter Rückgang", value: fmt.pct(t.max_drawdown, 1), note: "vom Höchststand bis zum Tief" },
        { label: "IRR Konto", value: fmt.pct(irr.account, 1, true), toneValue: irr.account, note: "Ein- und Auszahlungen gegen das heutige Nettovermögen" },
      ])}
      ${riskSection(doc)}
      <h2>Wertentwicklung gegenüber ${bench}</h2>
      <figure data-chart="twr"></figure>
      <div class="two-col">
        <div class="stack"><h2>Rückgang vom Höchststand</h2><p class="muted small">Wie weit das Depot zu jedem Zeitpunkt unter seinem bisherigen Höchststand lag.</p><figure data-chart="dd"></figure></div>
        <div class="stack"><h2>IRR im Zeitverlauf</h2><p class="muted small">Jahresrendite aller Trades bis zum jeweiligen Monatsende, ab drei Monaten Historie – zeigt, ob deine Rendite stabiler wird.</p><figure data-chart="irr"></figure></div>
      </div>
      <h2>Rendite je Kalenderjahr</h2>
      <figure data-chart="years"></figure>
      ${monthlySection(doc)}
      <p class="muted small">Zeitgewichtete Rendite (TWR) ist mit einem Index vergleichbar, weil Ein- und Auszahlungen sie nicht verzerren. Die IRR dagegen misst, was dein Geld tatsächlich verdient hat – inklusive des Zeitpunkts deiner Käufe.</p>
    `,
    charts: {
      twr: { ...twrChartSpec(doc, "max"), height: 280 },
      dd: { type: "line", label: "Rückgang vom Höchststand (Drawdown)", series: [{ name: "Rückgang", cls: "c3", area: true, points: dd }], y: { format: (v) => fmt.pct(v, 1), axis: (v) => fmt.pct(v, 0), zero: true }, endLabel: false },
      irr: {
        type: "line",
        label: "IRR aller Trades im Zeitverlauf",
        series: [
          { name: "vor Steuern", cls: "c1", points: irrRows.map((r) => [r.date, r.irr]) },
          { name: "nach Steuern (ohne latente)", cls: "c2", points: irrRows.filter((r) => r.irr_after_tax_excl_latent !== null).map((r) => [r.date, r.irr_after_tax_excl_latent]) },
        ],
        y: { format: (v) => fmt.pct(v, 1, true), axis: (v) => fmt.pct(v, 0), zero: true },
      },
      years: {
        type: "bar",
        label: "Zeitgewichtete Rendite je Kalenderjahr im Vergleich zum Benchmark",
        categories: years.map((y) => String(y.year) + (y.partial ? "*" : "")),
        categoryLabel: "Jahr",
        series: [
          { name: "Depot", cls: "c1", values: years.map((y) => y.twr || 0) },
          { name: bench, cls: "deemph", values: years.map((y) => y.benchmark || 0) },
        ],
        y: { format: (v) => fmt.pct(v, 1, true), axis: (v) => fmt.pct(v, 0) },
        caption: "* unvollständiges Jahr (Start bzw. laufendes Jahr)",
      },
    },
    mount: (panel) => mountMonthly(panel, doc),
  };
}

// ---------------------------------------------------------------- Kredit

function kredit(doc) {
  const c = doc.credit;
  const line = c.available_credit_line !== null && c.loan !== null ? c.loan + c.available_credit_line : null;
  const trades = c.trades || [];
  const table = sortableTable("credit-table", [
    { key: "name", label: "Trade", render: (t) => html`<a href="#/trades/${encodeURIComponent(t.id)}">${t.name}</a><div class="muted small">${fmt.date(t.open_date)}${t.credit.interest_estimated ? " · Zinsen geschätzt" : ""}</div>` },
    { key: "fin", label: "Kredit", num: true, sort: (t) => t.credit.financed, render: (t) => html`${money(t.credit.financed, { digits: 0 })}<div class="muted small">${fmt.pct(t.credit.leverage, 0)} des Kaufs</div>` },
    { key: "out", label: "offen", num: true, sort: (t) => t.credit.outstanding, render: (t) => money(t.credit.outstanding, { digits: 0 }) },
    { key: "int", label: "Zinsen", num: true, sort: (t) => t.credit.interest, render: (t) => html`${money(t.credit.interest)}<div class="muted small">${fmt.pct(t.credit.effective_rate, 2)} p. a.</div>` },
    { key: "irr", label: "IRR", num: true, render: (t) => percent(t.irr) },
    { key: "irr_after_tax", label: "nach Steuern", num: true, render: (t) => percent(t.irr_after_tax) },
    { key: "eq", label: "Eigenkapital", num: true, sort: (t) => t.credit.equity_irr, render: (t) => percent(t.credit.equity_irr) },
    { key: "be", label: "Break-even", num: true, sort: (t) => t.credit.break_even_irr, render: (t) => fmt.pct(t.credit.break_even_irr, 1) },
    { key: "net", label: "Netto", num: true, sort: (t) => t.credit.profit_net, render: (t) => money(t.credit.profit_net, { sign: true, digits: 0 }) },
  ], trades, { sortKey: "fin", rowHref: (t) => `#/trades/${encodeURIComponent(t.id)}`, empty: "Keine kreditfinanzierten Trades erkannt." });
  const months = c.interest_monthly || [];
  return {
    content: html`
      ${tiles([
        { label: "Kreditsaldo", value: fmt.eur(c.loan || 0, 0), note: c.loan ? `Modus „${c.mode}“` : "Derzeit kein Kredit." },
        line
          ? { label: "Kreditrahmen", value: fmt.pct(c.loan / line, 0), meter: meter(c.loan / line), note: `von ${fmt.eur(line, 0)} genutzt` }
          : { label: "Kreditrahmen", value: fmt.DASH, note: "Kreditrahmen von Scalable unbekannt." },
        { label: "Zinsen", side: "12 Monate", value: fmt.eur(c.interest_12m, 0), note: c.interest_12m_estimated ? `davon geschätzt ${fmt.eur(c.interest_12m_estimated, 0)}` : "laut Scalable-Buchungen" },
        { label: "Effektiver Zinssatz", value: fmt.pct(c.effective_rate_12m, 2), note: c.effective_rate_12m === null ? "Noch keine Zinsbuchungen." : "Zinsen ÷ durchschnittlicher Saldo" },
        { label: "Beleihung", value: fmt.pct(c.loan_to_value, 1), note: "Kredit ÷ Depotwert" },
        c.stress
          ? { label: "Puffer", value: fmt.pct(-c.stress.drop_until_limit, 0), note: "Kursrückgang, bis der Kreditrahmen ausgeschöpft wäre (Schätzung)" }
          : { label: "Puffer", value: fmt.DASH, note: "Ohne Kredit kein Risiko einer Nachschussforderung." },
      ])}
      ${alertBox("info", html`Kreditzinsen sind bei der Abgeltungsteuer <b>nicht absetzbar</b> (§ 20 Abs. 9 EStG). Ein kreditfinanzierter Trade lohnt sich daher erst, wenn seine Rendite vor Steuern über dem „Break-even“ liegt – dem Zinssatz geteilt durch den Anteil, der nach Steuern übrig bleibt.`)}
      <h2>Kreditfinanzierte Trades</h2>
      ${table.html}
      <div class="two-col">
        <div class="stack"><h2>Kreditsaldo</h2>${(c.balance || []).length > 1 ? html`<figure data-chart="balance"></figure>` : html`<p class="muted small">Kein Verlauf vorhanden.</p>`}</div>
        <div class="stack"><h2>Zinsen je Monat</h2>${months.length ? html`<figure data-chart="interest"></figure>` : html`<p class="muted small">Noch keine Zinsbuchungen.</p>`}</div>
      </div>
      <p class="muted small">Zuordnung (Modus „auto“): Rutscht das Konto durch einen Kauf ins Minus, ist der Fehlbetrag Kredit dieses Trades. Verkaufserlöse tilgen zuerst den eigenen Kredit, Einzahlungen bezahlen zuerst Käufe der folgenden Tage. Jede Zinsbuchung von Scalable wird nach Saldo-Tagen verteilt.</p>
    `,
    charts: {
      balance: { type: "line", label: "Kreditsaldo im Zeitverlauf", series: [{ name: "Kredit", cls: "c2", area: true, step: true, points: (c.balance || []).map((r) => [r.date, r.loan]) }], y: { format: (v) => fmt.eur(v, 0), axis: eurAxis, zero: true } },
      interest: { type: "bar", label: "Kreditzinsen je Monat", categories: months.map((m) => m.month), xFormat: (m) => fmt.monthLabel(m), categoryLabel: "Monat", series: [{ name: "Zinsen", cls: "c2", values: months.map((m) => m.interest) }], y: { format: (v) => fmt.eur(v, 2), axis: (v) => fmt.eur(v, 0) } },
    },
    mount: (panel) => table.mount(panel),
  };
}

// ---------------------------------------------------------------- Steuern

function steuern(doc) {
  const tx = doc.taxes;
  const years = tx.years || [];
  const al = tx.allowance;
  const used = al.annual - (al.remaining_this_year || 0);
  const now = new Date().getFullYear();
  const cur = years.find((y) => y.year === now);
  return {
    content: html`
      ${tiles([
        { label: "Steuersatz", value: fmt.pct(tx.effective_rate, 3), note: tx.rate_formula },
        { label: "Freibetrag frei", side: String(now), value: fmt.eur(al.remaining_this_year, 0), meter: meter(al.annual ? used / al.annual : 0), note: `von ${fmt.eur(al.annual, 0)} Freistellungsauftrag genutzt: ${fmt.eur(used, 0)}` },
        { label: "Steuer bei Verkauf", side: "heute", value: fmt.eur(tx.latent_total, 0), note: "Latente Steuer auf alle offenen Gewinne (Modell)" },
        { label: "Steuer dieses Jahr", side: "Modell", value: fmt.eur(cur ? cur.tax_model : 0, 0), note: html`laut Scalable einbehalten: ${fmt.eur(cur ? cur.tax_actual : 0, 0)}` },
        { label: "Verlusttopf Aktien", value: fmt.eur(cur ? -(cur.loss_carryforward_stock || 0) : 0, 0), note: "nur mit Aktiengewinnen verrechenbar" },
        { label: "Verlusttopf Sonstige", value: fmt.eur(cur ? -(cur.loss_carryforward_other || 0) : 0, 0), note: "mit allen Kapitalerträgen verrechenbar" },
      ])}
      <h2>Steuern je Jahr</h2>
      <figure data-chart="taxyears"></figure>
      <div class="table-scroll"><table class="table">
        <thead><tr><th scope="col">Jahr</th><th class="num" scope="col">Aktien</th><th class="num" scope="col">Sonstige</th><th class="num" scope="col">Ausschüttungen</th><th class="num" scope="col">Vorabpauschale</th><th class="num" scope="col">Steuerpflichtig</th><th class="num" scope="col">Freibetrag</th><th class="num" scope="col">Steuer (Modell)</th><th class="num" scope="col">Steuer (Scalable)</th></tr></thead>
        <tbody>${years.slice().reverse().map((y) => html`<tr><th scope="row">${y.year}</th><td class="num">${money(y.gains_stock, { sign: true, digits: 0 })}</td><td class="num">${money(y.gains_other, { sign: true, digits: 0 })}</td><td class="num">${money(y.dividends_taxable, { digits: 0 })}</td><td class="num">${money(y.vorabpauschale, { digits: 0 })}</td><td class="num">${money(y.taxable, { digits: 0 })}</td><td class="num">${money(y.allowance_used, { digits: 0 })}</td><td class="num">${money(y.tax_model, { digits: 0 })}</td><td class="num">${money(y.tax_actual, { digits: 0 })}</td></tr>`)}</tbody>
      </table></div>
      <p class="muted small">Gewinne nach Teilfreistellung. Aktienverluste mindern nur Aktiengewinne, übrige Verluste alle Erträge; nicht genutzte Verluste werden vorgetragen.</p>
      ${al.candidates.length && al.remaining_this_year > 0 ? html`
        <h2>Freibetrag ${now} nutzen</h2>
        <p class="small">Bis ${fmt.eur(al.remaining_this_year, 0)} steuerpflichtiger Gewinn bleibt dieses Jahr steuerfrei. Verkaufen und direkt wieder kaufen erhöht den Einstand – die Steuer fällt später nicht an.</p>
        <h3 class="overline">Steuerpflichtiger Gewinn je Position</h3>
        ${rankedBars(groupCandidates(al.candidates).slice(0, 8), { format: (v) => fmt.eur(v, 0) })}
        <p class="muted small">Verkäufe werden nach FIFO zugeordnet: Es werden immer zuerst die ältesten Stücke einer Position verkauft.</p>
      ` : ""}
      ${vapSection(doc)}
      ${alertBox("info", tx.disclaimer, { small: true })}
    `,
    charts: {
      taxyears: {
        type: "bar",
        label: "Steuern je Jahr – Modell und tatsächlich einbehalten",
        categories: years.map((y) => String(y.year)),
        categoryLabel: "Jahr",
        series: [
          { name: "Modell", cls: "c1", values: years.map((y) => y.tax_model || 0) },
          { name: "von Scalable einbehalten", cls: "c2", values: years.map((y) => y.tax_actual || 0) },
        ],
        y: { format: (v) => fmt.eur(v, 0), axis: (v) => fmt.eur(v, 0) },
      },
    },
  };
}

// ---------------------------------------------------------------- Erträge

// ---------------------------------------------------------------- Kosten

function kosten(doc) {
  const months = (doc.costs.monthly || []).filter((m) => m.fees || m.interest || m.taxes || m.interest_estimated);
  const years = doc.costs.by_year || [];
  const sum = (k) => years.reduce((a, y) => a + (y[k] || 0), 0);
  return {
    content: html`
      ${tiles([
        { label: "Gebühren", side: "gesamt", value: fmt.eur(sum("fees"), 0), note: "Order- und Handelsplatzgebühren" },
        { label: "Kreditzinsen", side: "gesamt", value: fmt.eur(sum("interest") + sum("interest_estimated"), 0), note: sum("interest_estimated") ? `davon geschätzt ${fmt.eur(sum("interest_estimated"), 0)}` : "laut Scalable" },
        { label: "Steuern", side: "gesamt", value: fmt.eur(sum("taxes"), 0), note: "von Scalable einbehalten (inkl. Vorabpauschale)" },
      ])}
      <h2>Kosten je Monat</h2>
      ${months.length ? html`<figure data-chart="costs"></figure>` : html`<p class="muted">Noch keine Kosten erfasst.</p>`}
      <h2>Je Jahr</h2>
      ${kvTableYears(years)}
      ${terSection(doc)}
    `,
    charts: {
      costs: {
        type: "bar",
        stacked: true,
        label: "Gebühren, Kreditzinsen und Steuern je Monat",
        categories: months.map((m) => m.month),
        xFormat: (m) => fmt.monthLabel(m),
        categoryLabel: "Monat",
        series: [
          { name: "Gebühren", cls: "c1", values: months.map((m) => m.fees) },
          { name: "Kreditzinsen", cls: "c2", values: months.map((m) => m.interest + (m.interest_estimated || 0)) },
          { name: "Steuern", cls: "c3", values: months.map((m) => Math.max(0, m.taxes)) },
        ],
        y: { format: (v) => fmt.eur(v, 2), axis: (v) => fmt.eur(v, 0) },
      },
    },
  };
}

function kvTableYears(years) {
  if (!years.length) return html`<p class="muted small">–</p>`;
  return html`<div class="table-scroll"><table class="table"><thead><tr><th scope="col">Jahr</th><th class="num" scope="col">Gebühren</th><th class="num" scope="col">Kreditzinsen</th><th class="num" scope="col">Steuern</th></tr></thead>
    <tbody>${years.slice().reverse().map((y) => html`<tr><th scope="row">${y.year}</th><td class="num">${money(y.fees)}</td><td class="num">${money((y.interest || 0) + (y.interest_estimated || 0))}</td><td class="num">${money(y.taxes)}</td></tr>`)}</tbody></table></div>`;
}

function groupCandidates(cands) {
  const by = new Map();
  for (const c of cands) {
    const key = c.isin || c.name;
    const e = by.get(key) || { label: c.name, value: 0, href: c.isin ? `#/positionen/${encodeURIComponent(c.isin)}` : "" };
    e.value += c.taxable_gain || 0;
    by.set(key, e);
  }
  return [...by.values()].sort((a, b) => b.value - a.value);
}

const PANELS = {
  performance,
  hinweise: insightsPanel,
  diversifikation: diversificationPanel,
  dividenden: dividendsPanel,
  rebalancing: rebalancingPanel,
  prognose: projectionPanel,
  kredit,
  steuern,
  kosten,
};
