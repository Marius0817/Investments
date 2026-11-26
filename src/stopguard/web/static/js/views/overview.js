import { rankedBars, segmentBar, meter } from "../charts.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { insightList } from "./insights.js";
import { alertBox, badge, errorBox, pageHead, percent, progress, segmented, tiles } from "../ui.js";
import { RANGES, figure, hasData, mountCharts, noData, protection, twrChartSpec, valueChartSpec } from "./common.js";

let range = "1y";

export async function render_(main) {
  const [doc, app] = await Promise.all([data.portfolio(), data.app()]);
  if (!hasData(doc)) {
    render(main, html`${pageHead("Übersicht")}${app.demo ? demoAlert() : ""}${noData()}`);
    return null;
  }
  const s = doc.summary;
  const credit = doc.credit;
  const twr = s.twr || {};
  const bench = doc.settings.benchmark.name;
  const warnings = doc.data_quality.warnings || [];
  const kinds = (doc.allocation.by_kind || []);
  const topKinds = kinds.slice(0, 3).map((k, i) => ({ label: fmt.KIND_LABELS[k.key] || k.key, value: k.value, cls: `c${i + 1}` }));
  const rest = kinds.slice(3).reduce((a, k) => a + k.value, 0);
  if (rest > 0) topKinds.push({ label: "Sonstige", value: rest, cls: "deemph" });
  const positions = (doc.allocation.by_position || []).slice(0, 7);
  const restPos = (doc.allocation.by_position || []).slice(7).reduce((a, p) => a + (p.value || 0), 0);
  const posBars = positions.map((p) => ({ label: p.name, value: p.value, href: `#/positionen/${encodeURIComponent(p.isin)}` }));
  if (restPos > 0) posBars.push({ label: `${doc.allocation.by_position.length - 7} weitere`, value: restPos, cls: "deemph" });

  const loanLine = credit.loan && credit.available_credit_line !== null
    ? { ratio: credit.loan / (credit.loan + credit.available_credit_line), note: `von ${fmt.eur(credit.loan + credit.available_credit_line, 0)} Kreditrahmen genutzt` }
    : null;

  render(main, html`
    ${pageHead("Übersicht", {
      meta: html`<span>Stand <b>${fmt.dateTime(doc.as_of)}</b></span><span>${s.counts.positions_open} Positionen</span><span>${s.counts.trades} Trades</span>`,
      actions: html`<span id="stops-cta"></span>`,
    })}
    ${app.demo ? demoAlert() : ""}
    <div id="stops-alerts"></div>
    ${warnings.length ? alertBox("warn", html`<b>${warnings.length} Hinweis${warnings.length === 1 ? "" : "e"} zur Datenqualität.</b> ${warnings[0]} <a href="#/daten">Alle ansehen</a>`) : ""}
    ${tiles([
      { label: "Nettovermögen", value: fmt.eur(s.net_worth, 0), note: html`Depot ${fmt.eur(s.securities_value, 0)}${s.loan ? html` · Kredit ${fmt.eur(s.loan, 0)}` : ""}` },
      {
        label: "Rendite p. a. (IRR)", value: fmt.pct(s.irr.trades, 1, true), toneValue: s.irr.trades,
        note: html`nach Steuern ${fmt.pct(s.irr.trades_after_tax, 1, true)}${s.counts.trades_credit ? html` · nach Zinsen ${fmt.pct(s.irr.trades_after_tax_and_interest, 1, true)}` : ""}`,
        href: "#/auswertung/performance",
      },
      {
        label: "Wertentwicklung", side: "zeitgewichtet", value: fmt.pct(twr.since_start, 1, true), toneValue: twr.since_start,
        note: html`${bench}: ${fmt.pct(twr.benchmark_since_start, 1, true)} im selben Zeitraum`,
        href: "#/auswertung/performance",
      },
      {
        label: "Gewinn", value: fmt.signedEur((s.unrealized_pl || 0) + (s.realized_pl || 0), 0), toneValue: (s.unrealized_pl || 0) + (s.realized_pl || 0),
        note: html`unrealisiert ${fmt.signedEur(s.unrealized_pl, 0)} · realisiert ${fmt.signedEur(s.realized_pl, 0)}`,
      },
      {
        label: "Dividenden", side: "nächste 12 Monate", value: fmt.eur(((doc.income.forecast || {}).next_12m) ?? doc.income.trailing_12m, 0),
        note: html`erwartet (Schätzung) · erhalten in 12 Monaten ${fmt.eur(doc.income.trailing_12m, 0)}`, href: "#/auswertung/dividenden",
      },
      loanLine
        ? { label: "Kredit", value: fmt.eur(credit.loan, 0), meter: meter(loanLine.ratio), note: loanLine.note, href: "#/auswertung/kredit" }
        : { label: "Kredit", value: fmt.eur(credit.loan || 0, 0), note: credit.loan ? "Kreditrahmen unbekannt" : "Kein Wertpapierkredit in Anspruch genommen.", href: "#/auswertung/kredit" },
    ])}

    ${(doc.insights || []).length ? html`<section class="card section-gap" aria-labelledby="h-ins">
      <div class="card-head"><h2 id="h-ins">Hinweise</h2><a class="small" href="#/auswertung/hinweise">Alle ${doc.insights.length} ansehen</a></div>
      ${insightList(doc.insights, { limit: 3 })}
    </section>` : ""}

    <div class="filter-row" id="range-filter">${segmented("range", "Zeitraum der Diagramme", RANGES.map(([k, l]) => [k, l]), range)}</div>
    <div class="two-col">
      <section class="card" aria-labelledby="h-value">
        <div class="card-head"><h2 id="h-value">Depotwert</h2></div>
        <p class="muted small">Kurswert der Wertpapiere gegenüber dem dafür eingesetzten Kapital.</p>
        ${figure("value")}
      </section>
      <section class="card" aria-labelledby="h-twr">
        <div class="card-head"><h2 id="h-twr">Wertentwicklung</h2></div>
        <p class="muted small">Gegenüber ${bench}, beide zu Beginn des Zeitraums auf 0 % gesetzt.</p>
        ${figure("twr")}
      </section>
    </div>

    <div class="two-col section-gap">
      <section class="card" aria-labelledby="h-alloc">
        <div class="card-head"><h2 id="h-alloc">Aufteilung</h2><a href="#/positionen" class="small">Alle Positionen</a></div>
        <h3 class="overline">Nach Anlageart</h3>
        ${segmentBar(topKinds, { label: "Aufteilung nach Anlageart" })}
        <h3 class="overline">Größte Positionen</h3>
        ${rankedBars(posBars)}
        ${doc.allocation.credit_financed ? html`<p class="muted small">${fmt.pct(doc.allocation.credit_financed, 1)} des Depotwerts sind mit Kredit finanziert.</p>` : ""}
      </section>
      <section class="card" aria-labelledby="h-stops">
        <div class="card-head"><h2 id="h-stops">Stop-Schutz</h2><a href="#/stops" class="small">Zu den Stops</a></div>
        <div id="stops-card">${progress("Aktuelle Kurse werden bei Scalable abgefragt …")}</div>
      </section>
    </div>

    <section class="card section-gap" aria-labelledby="h-trades">
      <div class="card-head"><h2 id="h-trades">Trades im Vergleich</h2><a href="#/trades" class="small">Alle Trades</a></div>
      <div class="two-col trades-best">
        <div class="stack"><h3 class="overline">Stärkste (ab 30 Tagen Haltedauer)</h3>${tradeList(s.best_trades)}</div>
        <div class="stack"><h3 class="overline">Schwächste</h3>${tradeList(s.worst_trades)}</div>
      </div>
      <div class="meta-row">
        <span>Trefferquote geschlossener Trades <b>${fmt.pct(s.counts.win_rate_closed, 0)}</b></span>
        <span>offen <b>${s.counts.trades_open}</b></span><span>geschlossen <b>${s.counts.trades_closed}</b></span>
        ${s.counts.trades_credit ? html`<span>kreditfinanziert <b>${s.counts.trades_credit}</b></span>` : ""}
      </div>
    </section>
  `);

  let dispose = mountCharts(main, { value: valueChartSpec(doc, range), twr: twrChartSpec(doc, range) });
  const off = on($("#range-filter"), "change", "input[name=range]", (ev, input) => {
    range = input.value;
    $("#range-filter").querySelectorAll("label").forEach((l) => l.classList.toggle("selected", l.contains(input)));
    dispose();
    dispose = mountCharts(main, { value: valueChartSpec(doc, range), twr: twrChartSpec(doc, range) });
  });

  loadStops(main);
  return () => { dispose(); off(); };
}

export { render_ as render };

function demoAlert() {
  return alertBox("warn", html`<b>Demo-Modus:</b> stopguard arbeitet mit einem simulierten Depot. Orders gehen an keinen echten Broker.`);
}

function tradeList(list) {
  if (!list || !list.length) return html`<p class="muted small">Noch keine Trades mit mindestens 30 Tagen Haltedauer.</p>`;
  return html`<ul class="trade-list">${list.map(
    (t) => html`<li><span><a href="#/trades/${encodeURIComponent(t.id)}">${t.name}</a>${t.status !== "closed" ? html` <span class="tag">offen</span>` : ""}</span>
      <span class="trade-list-figures">${percent(t.irr)}<span class="muted small">${fmt.signedEur(t.profit, 0)}</span></span></li>`,
  )}</ul>`;
}

async function loadStops(main) {
  const box = $("#stops-card", main);
  try {
    const stops = await data.stops();
    if (!box.isConnected) return;
    const orders = stops.actions.filter((a) => a.needs_confirmation);
    const urgent = stops.actions.filter((a) => a.urgent);
    const counts = {};
    stops.positions.forEach((p) => { const [, text] = protection(p); counts[text] = (counts[text] || 0) + 1; });
    const cta = $("#stops-cta", main);
    if (cta && orders.length) {
      render(cta, html`<a class="btn primary" href="#/stops">${orders.length} Stop-Anpassung${orders.length === 1 ? "" : "en"} prüfen</a>`);
    }
    const alerts = $("#stops-alerts", main);
    const breaches = stops.events.filter((e) => e.level === "urgent");
    if (alerts && (breaches.length || stops.unresolved.length)) {
      render(alerts, html`<div class="stack">${breaches.map((e) => alertBox("error", html`<b>${e.title}.</b> ${e.message}`))}${stops.unresolved.length ? alertBox("error", html`<b>Ungeklärte Ordereinreichung.</b> Bitte unter <a href="#/stops">Stops</a> klären, bevor weitere Orders entstehen.`) : ""}</div>`);
    }
    render(box, html`
      ${orders.length
        ? html`<p>${orders.length} Order${orders.length === 1 ? "" : "s"} ${orders.length === 1 ? "sollte" : "sollten"} angepasst werden${urgent.length ? html`, davon <b>${urgent.length} dringend</b>` : ""}.</p>`
        : html`<p>Alle Schutz-Orders entsprechen ihren Regeln.</p>`}
      <ul class="status-list">${Object.entries(counts).map(([text, n]) => {
        const pos = stops.positions.find((p) => protection(p)[1] === text);
        return html`<li>${badge(protection(pos)[0], text)}<b>${n}</b></li>`;
      })}</ul>
      <p class="muted small">Kurse von Scalable, ${fmt.time(stops.at)} Uhr.</p>
    `);
  } catch (err) {
    if (box.isConnected) render(box, errorBox(err, "Die übrigen Zahlen stammen aus dem letzten Sync und sind davon nicht betroffen."));
  }
}
