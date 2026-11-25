import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { badge, money, pageHead, percent, segmented, sortableTable } from "../ui.js";
import { hasData, mountCharts, noData } from "./common.js";

const filters = { status: "all", origin: "noplan", credit: false, q: "" };

function applyFilters(trades) {
  const q = filters.q.trim().toLowerCase();
  return trades.filter((t) =>
    (filters.status === "all" || (filters.status === "open" ? t.status !== "closed" : t.status === "closed")) &&
    (filters.origin === "all" || (filters.origin === "noplan" ? t.origin !== "savings_plan" : t.origin === filters.origin)) &&
    (!filters.credit || t.credit) &&
    (!q || t.name.toLowerCase().includes(q) || t.isin.toLowerCase().includes(q) || (t.ticker || "").toLowerCase().includes(q)));
}

export async function render_(main) {
  const doc = await data.portfolio();
  if (!hasData(doc)) { render(main, html`${pageHead("Trades")}${noData()}`); return null; }
  const bench = doc.settings.benchmark.name;
  const s = doc.summary;
  render(main, html`
    ${pageHead("Trades", {
      meta: html`<span><b>${s.counts.trades}</b> Trades</span><span>Trefferquote geschlossen <b>${fmt.pct(s.counts.win_rate_closed, 0)}</b></span><span>IRR aller Trades <b>${fmt.pct(s.irr.trades, 1, true)}</b></span>`,
    })}
    <form class="filter-row" id="trade-filter" role="search" aria-label="Trades filtern">
      ${segmented("status", "Status", [["all", "Alle"], ["open", "Offen"], ["closed", "Geschlossen"]], filters.status)}
      <label class="inline-field">Herkunft
        <select name="origin">
          ${[["noplan", "ohne Sparpläne"], ["all", "alle"], ["single", "Einzelkäufe"], ["savings_plan", "Sparpläne"], ["import", "importiert"]].map(
            ([v, l]) => html`<option value="${v}" ${v === filters.origin ? html`selected` : ""}>${l}</option>`)}
        </select>
      </label>
      <label class="inline-check"><input type="checkbox" name="credit" ${filters.credit ? html`checked` : ""}> <span>nur kreditfinanziert</span></label>
      <label class="inline-field grow">Suche<input type="text" name="q" value="${filters.q}" placeholder="Name, ISIN oder Ticker"></label>
    </form>
    <section class="card" aria-labelledby="h-scatter">
      <div class="card-head"><h2 id="h-scatter">Haltedauer und Rendite</h2><span class="muted small" id="trade-count"></span></div>
      <p class="muted small">Jeder Punkt ist ein Trade. Rechts oben: lange gehalten und rentabel. Ein Klick öffnet den Trade.</p>
      <figure data-chart="scatter"></figure>
    </section>
    <section class="card section-gap" aria-label="Trades">
      <div id="trade-table"></div>
      <p class="muted small">Benchmark: derselbe Betrag zur selben Zeit in ${bench} investiert. Alpha = IRR des Trades minus IRR des Benchmarks. IRR EK = Rendite auf das eigene Kapital nach Steuern und Kreditzinsen.</p>
    </section>
  `);
  let dispose = () => {};
  const sortState = { key: null, dir: "desc" };
  const draw = () => {
    const list = applyFilters(doc.trades);
    $("#trade-count", main).textContent = `${list.length} von ${doc.trades.length} Trades`;
    const closed = list.filter((t) => t.status === "closed" && t.irr !== null);
    const open = list.filter((t) => t.status !== "closed" && t.irr !== null);
    const point = (t) => ({ x: t.holding_days, y: t.irr, label: t.name, id: t.id, t });
    dispose();
    dispose = mountCharts(main, {
      scatter: {
        type: "scatter",
        height: 320,
        label: "Streudiagramm: Haltedauer in Tagen gegen IRR pro Jahr je Trade",
        series: [
          { name: "Geschlossen", cls: "c1", points: closed.map(point) },
          { name: "Offen", cls: "c2", points: open.map(point) },
        ],
        x: { format: (v) => fmt.num(v), label: "Haltedauer in Tagen" },
        y: { format: (v) => fmt.pct(v, 0, true), label: "IRR p. a.", clamp: [-1, 1] },
        tooltip: (p) => [
          { value: fmt.pct(p.t.irr, 1, true), name: "IRR p. a." },
          { value: fmt.signedEur(p.t.profit, 0), name: `Gewinn · ${fmt.days(p.t.holding_days)}` },
        ],
        caption: "Werte über +100 % oder unter −100 % p. a. stehen hohl am Rand – meist sehr kurze Haltedauern.",
        onClick: (p) => { location.hash = `#/trades/${encodeURIComponent(p.id)}`; },
      },
    });
    const table = sortableTable("trades-table", [
      {
        key: "name", label: "Trade",
        render: (t) => html`<a href="#/trades/${encodeURIComponent(t.id)}">${t.name}</a><div class="muted small">${fmt.date(t.open_date)}${t.close_date ? html` – ${fmt.date(t.close_date)}` : ""} · ${fmt.ORIGIN_LABELS[t.origin] || t.origin}</div>`,
      },
      { key: "status", label: "Status", render: (t) => html`${badge(t.status === "closed" ? "" : "done", fmt.STATUS_LABELS[t.status])}${t.credit ? html` ${badge("busy", "Kredit")}` : ""}` },
      { key: "holding_days", label: "Tage", num: true, render: (t) => fmt.num(t.holding_days) },
      { key: "invested", label: "Einsatz", num: true, render: (t) => money(t.invested, { digits: 0 }) },
      { key: "profit", label: "Gewinn", num: true, render: (t) => money(t.profit, { sign: true, digits: 0 }) },
      { key: "irr", label: "IRR p. a.", num: true, render: (t) => percent(t.irr) },
      { key: "irr_after_tax", label: "nach Steuern", num: true, render: (t) => percent(t.irr_after_tax) },
      { key: "bench", label: "Benchmark", num: true, sort: (t) => (t.benchmark ? t.benchmark.irr : null), render: (t) => (t.benchmark ? percent(t.benchmark.irr) : fmt.DASH) },
      { key: "alpha", label: "Alpha", num: true, sort: (t) => (t.benchmark ? t.benchmark.alpha_irr : null), render: (t) => (t.benchmark ? percent(t.benchmark.alpha_irr) : fmt.DASH) },
      { key: "eq", label: "IRR EK", num: true, sort: (t) => (t.credit ? t.credit.equity_irr : null), render: (t) => (t.credit ? percent(t.credit.equity_irr) : "") },
    ], list, { sortKey: "irr", state: sortState, rowHref: (t) => `#/trades/${encodeURIComponent(t.id)}`, empty: "Keine Trades für diese Auswahl." });
    render($("#trade-table", main), table.html);
    table.mount(main);
  };
  draw();
  const form = $("#trade-filter", main);
  const update = () => {
    const fd = new FormData(form);
    filters.status = fd.get("status");
    filters.origin = fd.get("origin");
    filters.credit = fd.get("credit") === "on";
    filters.q = fd.get("q") || "";
    form.querySelectorAll(".segmented label").forEach((l) => l.classList.toggle("selected", l.querySelector("input").checked));
    draw();
  };
  const offs = [on(form, "change", "input, select", update), on(form, "input", "input[name=q]", update)];
  form.addEventListener("submit", (ev) => ev.preventDefault());
  return () => { dispose(); offs.forEach((o) => o()); };
}

export { render_ as render };
