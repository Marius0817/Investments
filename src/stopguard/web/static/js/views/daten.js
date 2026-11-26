import { api } from "../api.js";
import { data, runSync } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, errorBox, kvTable, pageHead, progress, toast } from "../ui.js";

export async function render_(main) {
  const [app, doc] = await Promise.all([data.app(), data.portfolio().catch(() => null)]);
  const dq = doc ? doc.data_quality : { warnings: [], notes: [], sources: {}, reconciliation: {} };
  const rec = dq.reconciliation || {};
  const src = dq.sources || {};
  const st = app.settings;
  const theme = window.sgTheme ? window.sgTheme.get() : "system";
  render(main, html`
    ${pageHead("Daten & Einstellungen", { meta: html`<span>stopguard ${app.version}</span>${app.config_path ? html`<span>Konfiguration <span class="mono">${app.config_path}</span></span>` : ""}` })}
    ${app.demo ? alertBox("warn", html`<b>Demo-Modus:</b> Alle Daten stammen aus einem simulierten Depot.`) : ""}
    <div class="two-col">
      <section class="card" aria-labelledby="h-sync">
        <div class="card-head"><h2 id="h-sync">Daten von Scalable</h2></div>
        <p class="small">${app.last_sync_at ? html`Zuletzt geladen <b>${fmt.relTime(app.last_sync_at)}</b> (${fmt.dateTime(app.last_sync_at)}).` : "Noch nie geladen."}
          Geladen werden Transaktionen mit Gebühren und Steuern, Kurse, der Depotverlauf und der Kreditstand – nur lesend.</p>
        <p class="muted small">Der erste Abruf dauert länger, weil zu jeder Transaktion einmal die Details geholt werden. Danach lädt stopguard nur noch Neues; der regelmäßige Lauf tut das ohnehin alle ${st.schedule.interval_minutes >= 60 ? "paar Stunden" : "12 Stunden"}.</p>
        <div><button class="btn primary" type="button" data-action="sync">Daten von Scalable laden</button></div>
        <div id="sync-status" aria-live="polite"></div>
      </section>
      <section class="card" aria-labelledby="h-login">
        <div class="card-head"><h2 id="h-login">Anmeldung bei Scalable</h2></div>
        ${app.session.login_at
          ? html`<p class="small">Angemeldet seit ${fmt.dateTime(app.session.login_at)}. Scalable verlangt spätestens am <b>${fmt.dateTime(app.session.expires_at)}</b> eine neue Anmeldung.</p>`
          : html`<p class="small">Wann du dich zuletzt angemeldet hast, weiß stopguard nur, wenn du dich über <code>stopguard login</code> anmeldest.</p>`}
        <p class="muted small">Die Anmeldung läuft aus Sicherheitsgründen im Terminal: <code>stopguard login</code> öffnet einen Link, den du im Browser bestätigst. Eine Sitzung hält höchstens 7 Tage und endet nach 24 Stunden ohne Abfrage.</p>
        ${app.last_run_at ? html`<p class="muted small">Letzter Überwachungslauf ${fmt.relTime(app.last_run_at)} – ${app.last_run_ok ? "ohne Fehler" : "mit Fehler (Details im Protokoll)"}.</p>` : ""}
      </section>
    </div>

    <section class="card section-gap" aria-labelledby="h-quality">
      <div class="card-head"><h2 id="h-quality">Datenqualität</h2></div>
      ${(dq.warnings || []).length
        ? html`<div class="stack">${dq.warnings.map((w) => alertBox("warn", w, { small: true }))}</div>`
        : html`<p class="small">Keine Auffälligkeiten.</p>`}
      ${(dq.notes || []).map((n) => alertBox("info", n, { small: true }))}
      <div class="two-col">
        <div class="stack"><h3 class="overline">Abgleich mit Scalable</h3>${kvTable([
          ["Guthaben laut Scalable", fmt.eur(rec.cash_reported)],
          ["Guthaben rekonstruiert", fmt.eur(rec.cash_reconstructed)],
          ["Kredit laut Scalable", fmt.eur(rec.loan_reported)],
          ["Kredit rekonstruiert", fmt.eur(rec.loan_reconstructed)],
        ])}</div>
        <div class="stack"><h3 class="overline">Quellen</h3>${kvTable([
          ["Transaktionen", fmt.num(src.scalable_transactions)],
          ["Importierte Trades", fmt.num(src.imported_trades)],
          ["Kursreihen", fmt.num(src.price_series)],
          ["Punkte Depotverlauf", fmt.num(src.chart_points)],
        ])}</div>
      </div>
    </section>

    <div class="two-col section-gap">
      <section class="card" aria-labelledby="h-import">
        <div class="card-head"><h2 id="h-import">Trades importieren</h2></div>
        <p class="small">Übernimm deine bisherige Trade-Liste, z. B. die Notion-Datenbank „Trades“: in Notion <b>••• → Exportieren → Markdown &amp; CSV</b>, dann die CSV-Datei hier auswählen. Trades, die Scalable schon kennt, werden automatisch erkannt.</p>
        <form class="stack" id="import-form">
          <label>CSV-Datei<input type="file" id="import-file" accept=".csv,text/csv" required></label>
          <label>Quelle<input type="text" id="import-source" value="notion"><small class="muted">Name der Herkunft, z. B. „notion“ oder „comdirect“. Ein erneuter Import derselben Quelle aktualisiert die Trades.</small></label>
          <label class="inline-check"><input type="checkbox" id="import-replace"> <span>Bisher importierte Trades dieser Quelle vorher entfernen</span></label>
          <div><button class="btn" type="submit">Datei importieren</button></div>
        </form>
        <div id="import-result" aria-live="polite"></div>
      </section>
      <section class="card" aria-labelledby="h-export">
        <div class="card-head"><h2 id="h-export">Export</h2></div>
        <p class="small">Die komplette Auswertung als JSON – Trades, Positionen, Steuern, Kredit und alle Zeitreihen. Das Format ist in <code>docs/portfolio-data.md</code> beschrieben.</p>
        <div><a class="btn" href="api/v1/export" download>Auswertung herunterladen</a></div>
        <h3 class="overline">Darstellung</h3>
        <label>Farbschema
          <select id="theme-select">
            ${[["system", "wie das System"], ["light", "hell"], ["dark", "dunkel"]].map(([v, l]) => html`<option value="${v}" ${v === theme ? html`selected` : ""}>${l}</option>`)}
          </select>
        </label>
      </section>
    </div>

    <section class="card section-gap" aria-labelledby="h-settings">
      <div class="card-head"><h2 id="h-settings">Einstellungen</h2><span class="muted small">ändern in der Konfigurationsdatei</span></div>
      <div class="two-col">
        ${kvTable([
          ["Handelsplatz für Stops", st.venue],
          ["Benchmark", html`${st.benchmark.name} <span class="mono small">${st.benchmark.isin || ""}</span>`],
          ["Stop nachziehen ab", `${fmt.num(st.min_change_pct, 1)} % Verbesserung`],
          ["Stops nur erhöhen", st.only_raise ? "ja" : "nein"],
          ["Überwachung", `alle ${st.schedule.interval_minutes} Minuten, ${st.schedule.active_hours}${st.schedule.weekdays_only ? ", werktags" : ""}`],
        ])}
        ${kvTable([
          ["Kredit-Zuordnung", st.credit_mode === "auto" ? "automatisch aus dem Kassenbuch" : st.credit_mode === "flags" ? "nur markierte Trades" : "aus"],
          ["Steuern", st.tax_method === "model" ? "Modell (vergleichbar je Trade)" : "laut Scalable"],
          ["Kirchensteuer", st.church_tax_pct ? `${st.church_tax_pct} %` : "keine"],
          ["Freistellungsauftrag", fmt.eur(st.allowance_eur, 0)],
          ["Benachrichtigungen", (st.notify_channels || []).join(", ")],
        ])}
      </div>
      <h3 class="overline">Strategien</h3>
      <div class="strategy-list">${app.strategies.map((s) => html`<details>
        <summary>${s.name}${s.description ? html` <span class="muted small">– ${s.description}</span>` : ""}</summary>
        <ol class="small">${s.phases.map((p) => html`<li><b>${p.name}</b> (${p.condition}): ${p.rule}</li>`)}</ol>
      </details>`)}</div>
    </section>
  `);

  const offs = [
    on(main, "click", "[data-action=sync]", async (ev, btn) => {
      btn.disabled = true;
      btn.textContent = "Daten werden geladen …";
      const box = $("#sync-status", main);
      try {
        const job = await runSync((step) => { if (box.isConnected) render(box, progress(step)); });
        if (job.status === "done") {
          const r = job.result;
          toast("Daten geladen.");
          render(box, html`<div class="stack">${alertBox("info", html`${r.transactions} Transaktionen, ${r.details} Details, ${r.prices} Kurse für ${r.securities} Papiere und ${r.chart_points} Punkte des Depotverlaufs geladen.${r.details_pending ? html` ${r.details_pending} Details folgen beim nächsten Abruf.` : ""}`, { small: true })}${(r.warnings || []).map((w) => alertBox("warn", w, { small: true }))}</div>`);
        } else {
          render(box, errorBox({ message: job.error }, "Prüfe die Anmeldung bei Scalable und versuche es erneut."));
        }
      } catch (err) {
        render(box, errorBox(err));
      } finally {
        btn.disabled = false;
        btn.textContent = "Daten von Scalable laden";
      }
    }),
  ];
  $("#theme-select", main).addEventListener("change", (ev) => { window.sgTheme && window.sgTheme.set(ev.target.value); });
  $("#import-form", main).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const file = $("#import-file", main).files[0];
    const box = $("#import-result", main);
    if (!file) return;
    const btn = ev.target.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "Wird importiert …";
    try {
      const content = await file.text();
      const res = await api.post("import", { content, source: $("#import-source", main).value, replace: $("#import-replace", main).checked });
      data.invalidate("portfolio");
      toast(`${res.imported} Trades importiert.`);
      render(box, html`<div class="stack">${alertBox("info", html`<b>${res.imported} Trades importiert</b>, davon ${res.credit} kreditfinanziert und ${res.closed} geschlossen.${res.removed ? html` ${res.removed} alte Einträge ersetzt.` : ""}`, { small: true })}
        ${res.unmapped_tickers.length ? alertBox("warn", html`Für diese Ticker fehlt die ISIN – ergänze sie in der Konfiguration, damit Kurse, Benchmark und Dubletten-Erkennung funktionieren:<pre class="console">${res.unmapped_tickers.map((t) => `[instruments."ISIN"]\nticker = "${t}"\n`).join("\n")}</pre>`, { small: true }) : ""}</div>`);
    } catch (err) {
      render(box, errorBox(err, "Prüfe, ob die Datei die Spalten Open, Open (€) und Units enthält."));
    } finally {
      btn.disabled = false;
      btn.textContent = "Datei importieren";
    }
  });
  return () => offs.forEach((o) => o());
}

export { render_ as render };
