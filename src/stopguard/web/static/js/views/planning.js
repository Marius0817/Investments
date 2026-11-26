// Auswertung „Rebalancing“ (Zielgewichte + Rechner) und „Prognose“ (Vermögensentwicklung).
// Gerechnet wird auf dem Server (portfolio/planning.py) – die Oberfläche zeigt nur an.

import { api } from "../api.js";
import { mountChart } from "../charts.js";
import { $, $$, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, errorBox, progress, segmented, tiles, toast } from "../ui.js";

function debounce(fn, ms = 400) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

const parseNum = fmt.parseNumber;

function deviationBar(dev) {
  // Abweichung vom Ziel in Prozentpunkten, ±15 pp entspricht voller Breite
  const w = Math.min(1, Math.abs(dev) / 0.15) * 50;
  return html`<span class="devbar" role="img" aria-label="${fmt.pct(dev, 1, true)} gegenüber dem Ziel"><span class="devbar-fill ${dev < 0 ? "under" : "over"}" style="${dev < 0 ? `right:50%` : `left:50%`};width:${w.toFixed(1)}%"></span></span>`;
}

// ---------------------------------------------------------------- Rebalancing

const calc = { amount: "500", mode: "buy", min: "" };

export function rebalancingPanel() {
  return {
    content: html`
      <p class="muted small">Lege fest, wie viel jede Position am Depot ausmachen soll. Der Rechner zeigt dann, wie sich ein Betrag – etwa deine nächste
        Sparrate – am besten verteilen lässt. stopguard kauft nichts: Käufe führst du selbst in der Scalable-App aus.</p>
      <div id="rb-body">${progress("Zielgewichte werden geladen …")}</div>
    `,
    mount(panel) {
      let alive = true;
      let state = null;
      const body = $("#rb-body", panel);
      const load = async () => {
        try {
          state = await api.get("targets");
          if (!alive) return;
          draw();
        } catch (err) {
          if (alive) render(body, errorBox(err));
        }
      };
      const rows = () => {
        const byIsin = new Map(state.positions.map((p) => [p.isin, p]));
        const keys = [...new Set([...state.positions.map((p) => p.isin), ...Object.keys(state.targets)])];
        return keys.map((isin) => ({ isin, name: (byIsin.get(isin) || {}).name || isin, weight: (byIsin.get(isin) || {}).weight || 0, target: state.targets[isin] || 0 }));
      };
      const draw = () => {
        const list = rows().sort((a, b) => b.target - a.target || b.weight - a.weight);
        const hasTargets = Object.keys(state.targets).length > 0;
        render(body, html`
          <section aria-labelledby="h-targets">
            <div class="card-head"><h2 id="h-targets">Zielgewichte</h2>${state.updated_at ? html`<span class="muted small">gespeichert ${fmt.relTime(state.updated_at)}</span>` : ""}</div>
            ${hasTargets ? "" : alertBox("info", html`Noch keine Zielgewichte. Mit <b>„Aktuelle Gewichte übernehmen“</b> startest du vom heutigen Stand und passt dann an.`, { small: true })}
            <form id="targets-form" class="stack">
              <div class="table-scroll"><table class="table targets-table stacked">
                <thead><tr><th scope="col">Position</th><th class="num" scope="col">Aktuell</th><th class="num" scope="col">Ziel %</th><th scope="col">Abweichung</th></tr></thead>
                <tbody>${list.map((r) => html`<tr>
                  <th scope="row">${r.name}<div class="muted small mono">${r.isin}</div></th>
                  <td class="num" data-label="Aktuell">${fmt.pct(r.weight, 1)}</td>
                  <td class="num" data-label="Ziel %"><input class="target-input" type="text" inputmode="decimal" name="${r.isin}" value="${r.target ? fmt.num(r.target, r.target % 1 ? 1 : 0) : ""}" aria-label="Zielgewicht ${r.name} in Prozent" placeholder="0"></td>
                  <td data-label="Abweichung">${r.target ? deviationBar(r.weight - r.target / 100) : html`<span class="muted small">ohne Ziel</span>`}</td>
                </tr>`)}</tbody>
              </table></div>
              <div class="row targets-actions">
                <span class="small" id="target-sum" aria-live="polite"></span>
                <span class="grow"></span>
                <button type="button" class="btn" data-action="from-current">Aktuelle Gewichte übernehmen</button>
                <button type="submit" class="btn primary" id="targets-save">Zielgewichte speichern</button>
              </div>
            </form>
          </section>
          <section class="section-gap" aria-labelledby="h-calc">
            <h2 id="h-calc">Rechner</h2>
            <form id="calc-form" class="filter-row">
              <label class="inline-field">Betrag<input type="text" inputmode="decimal" name="amount" value="${calc.amount}" size="8" aria-describedby="amount-hint"> €</label>
              ${segmented("mode", "Verfahren", [["buy", "Nur kaufen"], ["full", "Auf Ziel bringen"]], calc.mode)}
              <label class="inline-field">Mindestens je Kauf<input type="text" inputmode="decimal" name="min" value="${calc.min}" size="5" placeholder="0"> €</label>
            </form>
            <p class="muted small" id="amount-hint">„Nur kaufen“ verteilt den Betrag auf die Positionen, die am weitesten unter ihrem Ziel liegen – ohne Verkäufe, also ohne Steuern.
              „Auf Ziel bringen“ rechnet exakt auf die Zielgewichte, auch mit Verkäufen, und schätzt die Steuer darauf.</p>
            <div id="calc-result" aria-live="polite"></div>
          </section>
        `);
        updateSum();
        runCalc();
      };
      const updateSum = () => {
        const form = $("#targets-form", panel);
        if (!form) return;
        const total = $$(".target-input", form).reduce((a, i) => a + (parseNum(i.value || "0") || 0), 0);
        const ok = Math.abs(total - 100) <= 0.5 || total === 0;
        $("#target-sum", panel).innerHTML = html`Summe <b class="num ${ok ? "" : "neg"}">${fmt.num(total, 1)} %</b>${ok ? "" : html` – muss 100 % ergeben`}`.text;
        $("#targets-save", panel).disabled = !ok;
      };
      const runCalc = debounce(async () => {
        const out = $("#calc-result", panel);
        if (!out) return;
        if (!Object.keys(state.targets).length) { render(out, html`<p class="muted small">Sobald Zielgewichte gespeichert sind, erscheint hier die Verteilung.</p>`); return; }
        const amount = parseNum(calc.amount || "0");
        const min = parseNum(calc.min || "0");
        if (amount === null || amount < 0 || min === null || min < 0) { render(out, alertBox("warn", "Bitte Beträge als Zahl eingeben, z. B. 500 oder 1.250,50.", { small: true })); return; }
        try {
          const r = await api.get(`rebalance?amount=${amount}&mode=${calc.mode}&min_order=${min}`);
          if (!alive) return;
          render(out, calcResult(r));
        } catch (err) {
          if (alive) render(out, errorBox(err));
        }
      }, 250);
      const offs = [
        on(panel, "input", ".target-input", updateSum),
        on(panel, "click", "[data-action=from-current]", () => {
          const inputs = $$(".target-input", panel);
          const weights = rows();
          // auf 0,5 % runden und Rundungsrest der größten Position zuschlagen
          const rounded = weights.map((r) => Math.round(r.weight * 200) / 2);
          const diff = 100 - rounded.reduce((a, v) => a + v, 0);
          const iMax = weights.reduce((b, r, i) => (r.weight > weights[b].weight ? i : b), 0);
          rounded[iMax] = Math.round((rounded[iMax] + diff) * 10) / 10;
          inputs.forEach((inp) => { const i = weights.findIndex((r) => r.isin === inp.name); inp.value = rounded[i] ? fmt.num(rounded[i], rounded[i] % 1 ? 1 : 0) : ""; });
          updateSum();
        }),
        on(panel, "submit", "#targets-form", async (ev, form) => {
          ev.preventDefault();
          const targets = {};
          for (const inp of $$(".target-input", form)) {
            const v = parseNum(inp.value || "0");
            if (v === null || v < 0 || v > 100) { toast(`Ungültiger Wert bei ${inp.getAttribute("aria-label")}.`, { error: true }); inp.focus(); return; }
            if (v > 0) targets[inp.name] = v;
          }
          const btn = $("#targets-save", panel);
          btn.disabled = true;
          btn.textContent = "Wird gespeichert …";
          try {
            state = await api.post("targets", { targets });
            toast("Zielgewichte gespeichert.");
            if (alive) draw();
          } catch (err) {
            toast(`Nicht gespeichert: ${err.message}`, { error: true });
            btn.disabled = false;
            btn.textContent = "Zielgewichte speichern";
          }
        }),
        on(panel, "input", "#calc-form input[type=text]", (ev, inp) => { calc[inp.name] = inp.value; runCalc(); }),
        on(panel, "change", "#calc-form input[name=mode]", (ev, inp) => {
          calc.mode = inp.value;
          $$("#calc-form .segmented label", panel).forEach((l) => l.classList.toggle("selected", l.contains(inp)));
          runCalc();
        }),
        on(panel, "submit", "#calc-form", (ev) => { ev.preventDefault(); runCalc(); }),
      ];
      load();
      return () => { alive = false; offs.forEach((o) => o()); };
    },
  };
}

function calcResult(r) {
  if (r.error) return html`<p class="muted">${r.error}</p>`;
  const full = r.mode === "full";
  const rows = r.rows.filter((x) => x.target > 0 || x.buy || x.sell);
  return html`
    <div class="meta-row">
      <span><b>${r.orders}</b> Order${r.orders === 1 ? "" : "s"}</span>
      ${r.total_buy ? html`<span>Käufe <b>${fmt.eur(r.total_buy, 0)}</b></span>` : ""}
      ${r.total_sell ? html`<span>Verkäufe <b>${fmt.eur(r.total_sell, 0)}</b></span>` : ""}
      ${full && r.tax_estimate ? html`<span>Steuer ca. <b>${fmt.eur(r.tax_estimate, 0)}</b></span>` : ""}
      <span>Abweichung vom Ziel <b>${fmt.pct(r.drift_now, 1)}</b> → <b>${fmt.pct(r.drift_after, 1)}</b></span>
    </div>
    <div class="table-scroll"><table class="table stacked">
      <thead><tr><th scope="col">Position</th><th class="num" scope="col">Jetzt</th><th class="num" scope="col">Ziel</th><th class="num" scope="col">Kaufen</th>${full ? html`<th class="num" scope="col">Verkaufen</th><th class="num" scope="col">Steuer ca.</th>` : ""}<th class="num" scope="col">Danach</th></tr></thead>
      <tbody>${rows.map((x) => html`<tr>
        <th scope="row">${x.held ? html`<a href="#/positionen/${encodeURIComponent(x.isin)}">${x.name}</a>` : html`${x.name} <span class="muted small">(noch nicht im Depot)</span>`}</th>
        <td class="num" data-label="Jetzt">${fmt.pct(x.weight_now, 1)}</td>
        <td class="num" data-label="Ziel">${fmt.pct(x.target, 1)}</td>
        <td class="num" data-label="Kaufen">${x.buy ? html`<b>${fmt.eur(x.buy, 2)}</b>` : html`<span class="muted">–</span>`}</td>
        ${full ? html`<td class="num" data-label="Verkaufen">${x.sell ? fmt.eur(x.sell, 2) : html`<span class="muted">–</span>`}</td><td class="num" data-label="Steuer ca.">${x.tax_estimate ? fmt.eur(x.tax_estimate, 2) : html`<span class="muted">–</span>`}</td>` : ""}
        <td class="num" data-label="Danach">${fmt.pct(x.weight_after, 1)}</td>
      </tr>`)}</tbody>
    </table></div>
    ${r.untargeted && r.untargeted.length ? html`<p class="muted small">Ohne Ziel und deshalb nicht berücksichtigt: ${r.untargeted.map((u, i) => html`${i ? ", " : ""}${u.name}`)}.</p>` : ""}
    <p class="muted small">${r.note}${full ? " Die Steuer ist anteilig aus der latenten Steuer der Position geschätzt (FIFO, Teilfreistellung, ohne Freibetrag)." : ""}</p>
  `;
}

// ---------------------------------------------------------------- Prognose

const proj = {};

export function projectionPanel() {
  return {
    content: html`
      <p class="muted small">Wie könnte sich dein Vermögen entwickeln? stopguard simuliert 2.000 mögliche Verläufe mit der angenommenen Rendite und Schwankung.
        Das Band zeigt, in welchem Bereich 80 % der Verläufe landen – keine Vorhersage, sondern eine Einordnung.</p>
      <div id="pj-body">${progress("Deine bisherigen Werte werden ermittelt …")}</div>
    `,
    mount(panel) {
      let alive = true;
      let dispose = () => {};
      const body = $("#pj-body", panel);
      const query = () => {
        const q = new URLSearchParams();
        for (const k of ["start", "monthly", "years", "return", "volatility", "inflation", "goal"]) {
          const v = proj[k];
          if (v !== undefined && v !== "") q.set(k, String(parseNum(v)));
        }
        return q.toString();
      };
      const field = (name, label, unit, hint) => html`<label class="field">${label}
        <span class="input-unit"><input type="text" inputmode="decimal" name="${name}" value="${proj[name] ?? ""}" ${hint ? html`aria-describedby="hint-${name}"` : ""}><span class="unit">${unit}</span></span>
        ${hint ? html`<span class="muted small" id="hint-${name}">${hint}</span>` : ""}</label>`;
      const drawForm = (d) => {
        render(body, html`
          <form id="pj-form" class="pj-form">
            ${field("start", "Startkapital", "€", "heutiges Nettovermögen")}
            ${field("monthly", "Sparrate pro Monat", "€", html`Ø deiner Einzahlungen der letzten 12 Monate: ${fmt.eur(d.monthly, 0)}`)}
            ${field("years", "Zeitraum", "Jahre", "1 bis 50")}
            ${field("return", "Erwartete Rendite", "% p. a.", d.historic_return !== null && d.historic_return !== undefined ? html`dein Depot bisher (zeitgewichtet): ${fmt.pct(d.historic_return, 1)} p. a.` : "z. B. 5 % für ein breites Aktiendepot (Annahme)")}
            ${field("volatility", "Schwankung", "% p. a.", d.historic_volatility ? html`dein Depot bisher: ${fmt.pct(d.historic_volatility, 1)}` : "Aktien-ETFs historisch oft 15–20 %")}
            ${field("inflation", "Inflation", "% p. a.", "0 = Nominalwerte")}
            ${field("goal", "Ziel (optional)", "€", "z. B. 100.000")}
          </form>
          <div id="pj-result" aria-live="polite"></div>
        `);
      };
      const run = debounce(async () => {
        const out = $("#pj-result", panel);
        if (!out) return;
        try {
          const r = await api.get(`projection?${query()}`);
          if (!alive) return;
          dispose();
          render(out, resultView(r));
          const fig = $("#pj-chart", out);
          dispose = fig ? mountChart(fig, chartSpec(r)) : () => {};
        } catch (err) {
          if (alive) render(out, errorBox(err, "Bitte die Eingaben prüfen."));
        }
      }, 350);
      (async () => {
        try {
          const first = await api.get("projection?years=15");
          if (!alive) return;
          const d = first.defaults;
          if (proj.start === undefined) {
            Object.assign(proj, {
              start: fmt.num(d.start, 0),
              monthly: fmt.num(d.monthly, 0),
              years: "15",
              return: fmt.num(d.expected_return * 100, 1),
              volatility: fmt.num(d.volatility * 100, 1),
              inflation: fmt.num(d.inflation * 100, 1),
              goal: "",
            });
          }
          drawForm(d);
          run();
        } catch (err) {
          if (alive) render(body, errorBox(err));
        }
      })();
      const offs = [
        on(panel, "input", "#pj-form input", (ev, inp) => { proj[inp.name] = inp.value; run(); }),
        on(panel, "submit", "#pj-form", (ev) => { ev.preventDefault(); run(); }),
      ];
      return () => { alive = false; dispose(); offs.forEach((o) => o()); };
    },
  };
}

function monthDate(m) {
  const d = new Date();
  d.setUTCDate(1);
  d.setUTCMonth(d.getUTCMonth() + m);
  return d.toISOString().slice(0, 10);
}

function resultView(r) {
  const f = r.final;
  const years = r.params.years;
  const real = r.real_terms ? " (heutige Kaufkraft)" : "";
  const goal = r.goal;
  return html`
    ${tiles([
      { label: "Mittlerer Verlauf", side: `in ${years} Jahren`, value: fmt.eur(f.p50, 0), note: `Hälfte der Verläufe darüber${real} · eingezahlt ${fmt.eur(f.invested, 0)}` },
      { label: "Ungünstig", side: "10 %", value: fmt.eur(f.p10, 0), note: "nur jeder zehnte Verlauf endet niedriger" },
      { label: "Günstig", side: "90 %", value: fmt.eur(f.p90, 0), note: "nur jeder zehnte Verlauf endet höher" },
      goal
        ? { label: "Ziel erreicht", side: fmt.eur(goal.amount, 0), value: fmt.pct(goal.probability, 0), note: goal.median_months ? `der Verläufe · im Mittel nach ${fmt.num(goal.median_months / 12, 1)} Jahren` : "der Verläufe – weniger als die Hälfte" }
        : { label: "Eingezahlt", value: fmt.eur(f.invested, 0), note: `Startkapital + ${years * 12} Sparraten${real}` },
    ])}
    <figure id="pj-chart"></figure>
    <p class="muted small">${r.method} Steuern, Kosten und Kreditzinsen sind nicht berücksichtigt. Vergangene Renditen sind kein Maßstab für die Zukunft.</p>
  `;
}

function chartSpec(r) {
  const s = r.series;
  return {
    type: "line",
    height: 300,
    label: `Vermögensprognose über ${r.params.years} Jahre: Median und Bandbreite der simulierten Verläufe`,
    xLabel: (d) => fmt.monthLong(d.slice(0, 7)),
    series: [
      { name: "10–90 % der Verläufe", cls: "c1 outer", band: true, points: s.map((p) => [monthDate(p.month), p.p10, p.p90]) },
      { name: "25–75 % der Verläufe", cls: "c1 inner", band: true, points: s.map((p) => [monthDate(p.month), p.p25, p.p75]) },
      { name: "Mittlerer Verlauf", cls: "c1", points: s.map((p) => [monthDate(p.month), p.p50]) },
      { name: "Eingezahlt", cls: "deemph", points: s.map((p) => [monthDate(p.month), p.invested]) },
    ],
    y: { format: (v) => fmt.eur(v, 0), axis: (v) => fmt.eurShort(v), zero: true },
  };
}
