import { data } from "../data.js";
import { html, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, badge, kvTable, money, pageHead, percent, tag, tiles } from "../ui.js";
import { mountCharts } from "./common.js";

export async function render_(main, [idParam], ctx) {
  const id = decodeURIComponent(idParam);
  const doc = await data.portfolio();
  const t = doc.trades.find((x) => x.id === id);
  if (!t) {
    render(main, html`${pageHead("Trade nicht gefunden")}${alertBox("warn", html`Diesen Trade gibt es nicht (mehr). <a href="#/trades">Zur Trade-Liste</a>`)}`);
    return null;
  }
  ctx.title(t.name);
  const c = t.credit;
  const b = t.benchmark;
  const tx = t.taxes;
  const bench = doc.settings.benchmark.name;
  const flows = t.cashflows.filter((f) => f.type !== "valuation" || t.status !== "closed");
  // Zahlungen je Tag zusammengefasst, Zuflüsse und Abflüsse getrennt
  const byDate = new Map();
  for (const f of flows) {
    const e = byDate.get(f.date) || { in: 0, out: 0 };
    if (f.amount >= 0) e.in += f.amount; else e.out += f.amount;
    byDate.set(f.date, e);
  }
  const dates = [...byDate.keys()].sort();
  const showFlowChart = dates.length >= 2 && dates.length <= 60;

  render(main, html`
    <p class="crumbs"><a href="#/trades">Trades</a> / <a href="#/positionen/${encodeURIComponent(t.isin)}">${t.name}</a> /</p>
    ${pageHead(`${t.name}`, {
      badges: html`${badge(t.status === "closed" ? "" : "done", fmt.STATUS_LABELS[t.status])}${c ? badge("busy", "kreditfinanziert") : ""}`,
      meta: html`<span>Kauf <b>${fmt.date(t.open_date)}</b></span><span>${t.close_date ? html`Verkauf <b>${fmt.date(t.close_date)}</b>` : "noch im Depot"}</span><span>${fmt.days(t.holding_days)}</span><span>${fmt.ORIGIN_LABELS[t.origin] || t.origin}</span>${t.ticker ? html`<span>${tag(t.ticker)}</span>` : ""}`,
    })}
    ${tiles([
      { label: "Einsatz", value: fmt.eur(t.invested, 0), note: html`${fmt.units(t.units)} Stk. zu ${fmt.eur(t.buy_price)}` },
      { label: "Gewinn", side: "vor Steuern", value: fmt.signedEur(t.profit, 0), toneValue: t.profit, note: html`${fmt.pct(t.return_pct, 1, true)} auf den Einsatz` },
      { label: "IRR p. a.", value: fmt.pct(t.irr, 1, true), toneValue: t.irr, note: "Jahresrendite vor Steuern" },
      { label: "IRR nach Steuern", value: fmt.pct(t.irr_after_tax, 1, true), toneValue: t.irr_after_tax, note: html`Steuern ${fmt.eur(tx.total, 0)} (Modell)` },
      b
        ? { label: "Benchmark", side: bench, value: fmt.pct(b.irr, 1, true), note: html`Alpha ${fmt.pct(b.alpha_irr, 1, true)} · ${fmt.signedEur(b.outperformance, 0)}` }
        : { label: "Benchmark", value: fmt.DASH, note: "Kein Vergleich möglich (keine Kurse oder der Trade ist der Benchmark)." },
      c
        ? { label: "IRR Eigenkapital", value: fmt.pct(c.equity_irr, 1, true), toneValue: c.equity_irr, note: html`nach Steuern und ${fmt.eur(c.interest, 0)} Zinsen` }
        : { label: "Erträge", value: fmt.eur(t.dividends, 0), note: t.dividends ? "Ausschüttungen während der Haltedauer" : "Keine Ausschüttungen." },
    ])}
    ${showFlowChart ? html`<section class="card" aria-labelledby="h-flows">
      <div class="card-head"><h2 id="h-flows">Zahlungsströme</h2></div>
      <p class="muted small">Was zu welchem Zeitpunkt geflossen ist – Grundlage der IRR. Zuflüsse oberhalb, Abflüsse unterhalb der Nulllinie.</p>
      <figure data-chart="flows"></figure>
    </section>` : ""}
    <div class="two-col section-gap">
      <section class="card" aria-labelledby="h-tax">
        <div class="card-head"><h2 id="h-tax">Steuern</h2><span class="muted small">${tx.method === "actual" ? "laut Scalable" : "Modellrechnung"}</span></div>
        ${kvTable([
          ["Verkauf", money(tx.sale)],
          ["Ausschüttungen", money(tx.dividends)],
          ["Vorabpauschale", money(tx.vorabpauschale)],
          ["Bei Verkauf heute", money(tx.latent)],
          ["Summe", html`<b>${money(tx.total)}</b>`],
          ["Gewinn nach Steuern", money(t.profit_after_tax, { sign: true })],
        ])}
      </section>
      ${c ? html`<section class="card" aria-labelledby="h-credit">
        <div class="card-head"><h2 id="h-credit">Kredit</h2>${c.interest_estimated ? html`<span class="muted small">Zinsen geschätzt</span>` : ""}</div>
        ${kvTable([
          ["Finanziert", html`${money(c.financed)} <span class="muted small">(${fmt.pct(c.leverage, 0)} des Kaufs)</span>`],
          ["Noch offen", money(c.outstanding)],
          ["Zinsen", html`${money(c.interest)} <span class="muted small">effektiv ${fmt.pct(c.effective_rate, 2)} p. a.</span>`],
          ["Eigenes Kapital (max.)", money(c.equity_invested)],
          ["Nettogewinn", money(c.profit_net, { sign: true })],
          ["IRR Eigenkapital", percent(c.equity_irr)],
          ["Nötige Rendite (Break-even)", html`${fmt.pct(c.break_even_irr, 1)} <span class="muted small">vor Steuern, um die Zinsen zu decken</span>`],
          ["Spread", html`${percent(c.spread)} <span class="muted small">IRR minus Zinssatz</span>`],
        ])}
      </section>` : html`<section class="card" aria-labelledby="h-bench">
        <div class="card-head"><h2 id="h-bench">Vergleich mit ${bench}</h2></div>
        ${b ? kvTable([
          ["IRR Trade", percent(t.irr)],
          ["IRR Benchmark", percent(b.irr)],
          ["Alpha", percent(b.alpha_irr)],
          ["Gewinn Trade", money(t.profit, { sign: true })],
          ["Gewinn Benchmark", money(b.profit, { sign: true })],
          ["Mehrertrag", money(b.outperformance, { sign: true })],
        ]) : html`<p class="muted small">Für diesen Trade liegt kein Benchmark-Vergleich vor.</p>`}
      </section>`}
    </div>
    <section class="card section-gap" aria-labelledby="h-cf">
      <div class="card-head"><h2 id="h-cf">Alle Zahlungen</h2><span class="muted small">aus deiner Sicht: Abflüsse negativ</span></div>
      <div class="table-scroll"><table class="table">
        <thead><tr><th scope="col">Datum</th><th scope="col">Art</th><th scope="col" class="num">Betrag</th></tr></thead>
        <tbody>${t.cashflows.map((f) => html`<tr><td>${fmt.date(f.date)}</td><td>${fmt.CASHFLOW_LABELS[f.type] || f.type}</td><td class="num">${money(f.amount, { sign: true })}</td></tr>`)}</tbody>
      </table></div>
    </section>
    ${t.notes.length ? html`<div class="stack section-gap">${t.notes.map((n) => alertBox("info", n, { small: true }))}</div>` : ""}
  `);
  return showFlowChart
    ? mountCharts(main, {
        flows: {
          type: "bar",
          stacked: true,
          label: "Zahlungsströme des Trades je Tag",
          categories: dates,
          xFormat: (d, short) => (short ? fmt.dateShort(d) : fmt.date(d)),
          categoryLabel: "Datum",
          series: [
            { name: "Zuflüsse", cls: "c1", values: dates.map((d) => byDate.get(d).in) },
            { name: "Abflüsse", cls: "c2", values: dates.map((d) => byDate.get(d).out) },
          ],
          y: { format: (v) => fmt.signedEur(v, 0), axis: (v) => fmt.eurShort(v) },
        },
      })
    : null;
}

export { render_ as render };
