import { data } from "../data.js";
import { $, html, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, badge, errorBox, kvTable, money, pageHead, percent, progress, sortableTable, stepper, tag, tiles } from "../ui.js";
import { figure, mountCharts, protection } from "./common.js";

export async function render_(main, [isinParam], ctx) {
  const isin = decodeURIComponent(isinParam);
  const doc = await data.portfolio();
  const pos = doc.positions.find((p) => p.isin === isin);
  if (!pos) {
    render(main, html`${pageHead("Position nicht gefunden")}${alertBox("warn", html`Zu <code>${isin}</code> gibt es keine Daten. <a href="#/positionen">Zur Liste der Positionen</a>`)}`);
    return null;
  }
  ctx.title(pos.name);
  const trades = doc.trades.filter((t) => t.isin === isin);
  const series = doc.timeseries.positions[isin] || [];
  const latent = pos.latent_tax;
  render(main, html`
    <p class="crumbs"><a href="#/positionen">Positionen</a> /</p>
    ${pageHead(pos.name, {
      badges: html`${badge(pos.status === "open" ? "done" : "", pos.status === "open" ? "im Depot" : "geschlossen")}${pos.credit_outstanding ? badge("busy", "Kredit") : ""}`,
      meta: html`<span class="mono">${pos.isin}</span>${pos.ticker ? html`<span>${tag(pos.ticker)}</span>` : ""}<span>${fmt.KIND_LABELS[pos.kind] || pos.kind}</span><span>erster Kauf <b>${fmt.date(pos.first_buy)}</b></span><span>${fmt.days(pos.holding_days)} gehalten</span>`,
    })}
    ${tiles([
      { label: "Wert", value: fmt.eur(pos.value, 0), note: html`${fmt.units(pos.units)} Stk. zu ${fmt.eur(pos.current_price)}` },
      { label: "Kursgewinn", value: fmt.signedEur(pos.unrealized_pl, 0), toneValue: pos.unrealized_pl, note: html`${fmt.pct(pos.unrealized_pct, 1, true)} auf ${fmt.eur(pos.cost_basis, 0)} Einstand` },
      { label: "IRR p. a.", value: fmt.pct(pos.irr, 1, true), toneValue: pos.irr, note: html`nach Steuern ${fmt.pct(pos.irr_after_tax, 1, true)}${pos.equity_irr !== null ? html` · Eigenkapital ${fmt.pct(pos.equity_irr, 1, true)}` : ""}` },
      { label: "Realisiert", value: fmt.signedEur(pos.realized_pl, 0), toneValue: pos.realized_pl, note: "Gewinn aus bereits verkauften Stücken" },
      { label: "Erträge", value: fmt.eur(pos.dividends, 0), note: pos.dividends ? "Ausschüttungen seit dem ersten Kauf" : "Keine Ausschüttungen erfasst." },
      { label: "Steuer bei Verkauf", side: "heute", value: fmt.eur(latent, 0), note: "Latente Steuer auf den offenen Kursgewinn (Modell)." },
    ])}
    <div class="two-col">
      <section class="card" aria-labelledby="h-pv">
        <div class="card-head"><h2 id="h-pv">Wertverlauf</h2></div>
        ${series.length > 1 ? figure("pv") : html`<p class="muted small">Für einen Verlauf liegen noch zu wenige Kurse vor.</p>`}
      </section>
      <section class="card" aria-labelledby="h-stop">
        <div class="card-head"><h2 id="h-stop">Stop-Schutz</h2><a class="small" href="#/stops">Alle Stops</a></div>
        <div id="stop-box">${progress("Aktueller Kurs und Stop werden ermittelt …")}</div>
      </section>
    </div>
    <section class="card section-gap" aria-labelledby="h-tr">
      <div class="card-head"><h2 id="h-tr">Trades dieser Position</h2></div>
      <div id="pos-trades"></div>
    </section>
  `);
  const table = sortableTable("pos-trades-table", [
    { key: "open_date", label: "Kauf", render: (t) => html`<a href="#/trades/${encodeURIComponent(t.id)}">${fmt.date(t.open_date)}</a><div class="muted small">${fmt.ORIGIN_LABELS[t.origin] || t.origin}</div>` },
    { key: "status", label: "Status", render: (t) => badge(t.status === "closed" ? "" : "done", fmt.STATUS_LABELS[t.status]) },
    { key: "units", label: "Stück", num: true, render: (t) => fmt.units(t.units) },
    { key: "buy_price", label: "Kaufkurs", num: true, render: (t) => fmt.eur(t.buy_price) },
    { key: "profit", label: "Gewinn", num: true, render: (t) => money(t.profit, { sign: true, digits: 0 }) },
    { key: "irr", label: "IRR p. a.", num: true, render: (t) => percent(t.irr) },
    { key: "irr_after_tax", label: "nach Steuern", num: true, render: (t) => percent(t.irr_after_tax) },
  ], trades, { sortKey: "open_date", rowHref: (t) => `#/trades/${encodeURIComponent(t.id)}` });
  render($("#pos-trades", main), table.html);
  table.mount(main);
  const dispose = mountCharts(main, {
    pv: { type: "line", label: `Wert der Position ${pos.name} im Zeitverlauf`, series: [{ name: "Wert", cls: "c1", area: true, points: series }], y: { format: (v) => fmt.eur(v, 0), axis: (v) => fmt.eurShort(v) } },
  });
  loadStop(main, isin);
  return dispose;
}

export { render_ as render };

async function loadStop(main, isin) {
  const box = $("#stop-box", main);
  try {
    const stops = await data.stops();
    if (!box.isConnected) return;
    const s = stops.positions.find((p) => p.isin === isin);
    if (!s) {
      render(box, html`<p class="muted">Diese Position wird nicht von stopguard verwaltet (nicht im Depot oder <code>engine.unlisted_positions = "ignore"</code>).</p>`);
      return;
    }
    render(box, stopDetails(s));
  } catch (err) {
    if (box.isConnected) render(box, errorBox(err));
  }
}

export function stopDetails(s) {
  const [cls, text] = protection(s);
  const cur = (s.phases || []).find((p) => p.state === "current");
  const comps = (s.target.components || []).slice().sort((a, b) => b.price - a.price);
  const exec = { order: "Stop-Order bei Scalable", alert: "Kursalarm in der Scalable-App", watch: "nur beobachten" }[s.execution] || s.execution;
  const orderActions = (s.actions || []).filter((a) => a.needs_confirmation);
  return html`
    <div class="row">${badge(cls, text)}<span class="muted small">Strategie <b>${s.strategy}</b> · ${exec}</span></div>
    ${stepper(s.phases)}
    <p class="phase-desc">${cur ? html`<b>${cur.name}:</b> ${cur.rule}.` : ""}${s.phase.reason ? html` Aktiv seit ${fmt.date(s.phase_since)} – ausgelöst durch ${s.phase.reason}.` : ""}
      ${(s.phases || []).filter((p) => p.state === "upcoming").slice(0, 1).map((p) => html` Nächste Phase „${p.name}“ bei ${p.condition}.`)}</p>
    ${kvTable([
      ["Aktueller Kurs", html`<span class="num">${fmt.eur(s.price ? Number(s.price) : null)}</span>`],
      ["Soll-Stop", s.target.stop ? html`<span class="num">${s.target.order_type === "trailing-stop" ? `Trailing ${s.target.trail_percent} % (≈ ${fmt.eur(Number(s.target.stop))})` : fmt.eur(Number(s.target.stop))}</span>` : "kein Stop in dieser Phase"],
      s.distance !== null && s.distance !== undefined ? ["Abstand zum Stop", html`<span class="num">${fmt.pct(s.distance, 1)}</span> <span class="muted small">unter dem aktuellen Kurs</span>`] : null,
      ["Ist-Zustand", s.managed_order ? s.managed_order.description : s.managed_alert ? `Kursalarm ${fmt.eur(s.managed_alert.price)}` : html`<span class="muted">keine Order</span>`],
      s.high_water_mark ? ["Hoch seit Phasenbeginn", html`<span class="num">${fmt.eur(Number(s.high_water_mark))}</span>`] : null,
      comps.length ? ["Berechnung", html`<ul class="plain">${comps.map((c, i) => html`<li><span class="num">${fmt.eur(c.price)}</span> <span class="${i ? "muted" : ""}">${c.label}${i === 0 ? " – maßgeblich" : ""}</span></li>`)}</ul>`] : null,
    ])}
    ${(s.issues || []).map((i) => alertBox("info", i, { small: true }))}
    ${orderActions.map((a) => html`<div class="card-inner">
      <div class="card-head"><b>${a.headline}</b>${a.urgent ? badge("failed", "dringend") : ""}</div>
      <p class="small">${a.reason}</p>
      ${a.new ? html`<p class="small">Neu: <span class="mono">${a.new}</span>${a.current ? html` · bisher <span class="mono">${a.current}</span>` : ""}</p>` : ""}
      <div><a class="btn small" href="#/stops/aenderung/${encodeURIComponent(a.key)}">Vorschau von Scalable anfordern</a></div>
    </div>`)}
  `;
}
