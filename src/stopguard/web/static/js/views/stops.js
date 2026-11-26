import { api } from "../api.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, badge, confirmDialog, errorBox, pageHead, percent, progress, sortableTable, toast } from "../ui.js";
import { protection } from "./common.js";

export async function render_(main, params, ctx) {
  let alive = true;
  // Inhalt neu zeichnen; die Klick-Handler unten werden nur einmal registriert.
  const draw = async (refresh = false) => {
    let stops;
    try {
      stops = await data.stops(refresh);
    } catch (err) {
      if (alive) render(main, html`${pageHead("Stops")}${errorBox(err)}`);
      return;
    }
    const history = await api.get("history?limit=40").catch(() => []);
    if (alive) drawStops(main, stops, history);
  };
  const offs = [
    on(main, "click", "[data-action=refresh]", async (ev, btn) => {
      btn.disabled = true;
      btn.textContent = "Wird neu berechnet …";
      await draw(true);
    }),
    on(main, "click", "[data-action=alerts]", async (ev, btn) => {
      btn.disabled = true;
      btn.textContent = "Wird abgeglichen …";
      try {
        const res = await data.mutate(api.post("alerts/sync", {}), "stops");
        toast(res.lines.length ? "Kursalarme abgeglichen." : "Kursalarme waren schon aktuell.");
        await draw(true);
      } catch (err) {
        toast(`Kursalarme konnten nicht abgeglichen werden: ${err.message}`, { error: true });
        btn.disabled = false;
        btn.textContent = "Kursalarme jetzt abgleichen";
      }
    }),
    on(main, "click", "[data-resolve]", async (ev, btn) => {
      const isin = btn.dataset.isin;
      const decision = btn.dataset.resolve;
      const ok = await confirmDialog({
        title: decision === "adopt" ? "Offene Order übernehmen" : "Keine Order angelegt",
        body: decision === "adopt"
          ? html`<p>stopguard übernimmt die offene Verkaufsorder als verwaltete Schutz-Order und plant wieder normal.</p>`
          : html`<p>Bestätige nur, wenn du in der Scalable-App (Aufträge) geprüft hast, dass <b>keine</b> Order angelegt wurde. Danach kann eine neue Vorschau angefordert werden.</p>`,
        confirmLabel: decision === "adopt" ? "Order übernehmen" : "Ja, keine Order vorhanden",
        strict: true,
      });
      if (!ok) return;
      try {
        await data.mutate(api.post(`unresolved/${encodeURIComponent(isin)}`, { decision }), "stops", "app");
        toast("Einreichung geklärt.");
        await draw(true);
      } catch (err) {
        toast(err.message, { error: true });
      }
    }),
  ];
  render(main, html`${pageHead("Stops", { meta: html`<span>Kurse werden bei Scalable abgefragt …</span>` })}${progress("Positionen, offene Orders und Kurse werden geladen …")}`);
  await draw(false);
  return () => { alive = false; offs.forEach((o) => o()); };
}

function drawStops(main, stops, history) {
  const orderActions = stops.actions.filter((a) => a.needs_confirmation);
  const alertActions = stops.actions.filter((a) => !a.needs_confirmation);
  const breaches = stops.events.filter((e) => e.level === "urgent");
  const otherEvents = stops.events.filter((e) => e.level === "warning");
  const table = sortableTable("stops-table", [
    { key: "name", label: "Position", render: (p) => html`<a href="#/positionen/${encodeURIComponent(p.isin)}">${p.name}</a><div class="muted small">${p.strategy}</div>` },
    { key: "phase", label: "Phase", sort: (p) => p.phase.index, render: (p) => html`${p.phase.name}<div class="muted small">${p.phase.index + 1} von ${p.phases.length}</div>` },
    { key: "price", label: "Kurs", num: true, sort: (p) => Number(p.price), render: (p) => fmt.eur(p.price ? Number(p.price) : null) },
    {
      key: "stop", label: "Soll-Stop", num: true, sort: (p) => Number(p.target.stop || 0),
      render: (p) => (p.target.stop
        ? html`${fmt.eur(Number(p.target.stop))}${p.target.order_type === "trailing-stop" ? html`<div class="muted small">Trailing ${p.target.trail_percent} %</div>` : ""}`
        : html`<span class="muted">kein Stop</span>`),
    },
    { key: "order", label: "Bei Scalable", sortable: false, render: (p) => (p.managed_order ? html`<span class="mono small">${p.managed_order.description}</span>` : p.managed_alert ? html`<span class="small">Alarm ${fmt.eur(p.managed_alert.price)}</span>` : html`<span class="muted small">–</span>`) },
    { key: "distance", label: "Abstand", num: true, render: (p) => (p.distance !== null && p.distance !== undefined ? html`<span class="num">${fmt.pct(p.distance, 1)}</span>` : fmt.DASH) },
    { key: "state", label: "Zustand", sort: (p) => protection(p)[1], render: (p) => { const [c, t] = protection(p); return badge(c, t); } },
  ], stops.positions, { sortKey: "state", dir: "asc", rowHref: (p) => `#/positionen/${encodeURIComponent(p.isin)}` });

  render(main, html`
    ${pageHead("Stops", {
      meta: html`<span>Kurse von Scalable, <b>${fmt.time(stops.at)} Uhr</b></span><span>${stops.positions.length} Positionen</span><span>${orderActions.length} Anpassung${orderActions.length === 1 ? "" : "en"} empfohlen</span>`,
      actions: html`<button class="btn" type="button" data-action="refresh">Neu berechnen</button>`,
    })}
    <div class="stack">
      ${breaches.map((e) => alertBox("error", html`<b>${e.title}.</b> ${e.message.replace(/ Verkauf prüfen: stopguard apply$/, "")}`))}
      ${otherEvents.map((e) => alertBox("warn", html`<b>${e.title}.</b> ${e.message}`))}
      ${stops.unresolved.map((u) => unresolvedBox(u, stops))}
    </div>

    <section class="card ${breaches.length || stops.unresolved.length ? "section-gap" : ""}" aria-labelledby="h-actions">
      <div class="card-head"><h2 id="h-actions">Empfohlene Änderungen</h2></div>
      ${orderActions.length
        ? html`<p class="muted small">Jede Order zeigt dir zuerst die vollständige Vorschau von Scalable. Platziert wird erst, wenn du dort ausdrücklich bestätigst.</p>
          <div class="stack">${orderActions.map(actionBlock)}</div>`
        : html`<p>Alle Schutz-Orders entsprechen ihren Regeln. Ändert sich ein Kurs so, dass ein Stop nachgezogen werden sollte, erscheint es hier und als Benachrichtigung.</p>`}
      ${alertActions.length ? html`<div class="card-inner">
        <div class="card-head"><b>Kursalarme</b><button class="btn small" type="button" data-action="alerts">Kursalarme jetzt abgleichen</button></div>
        <ul class="plain small">${alertActions.map((a) => html`<li>${a.name}: ${a.reason}</li>`)}</ul>
        <p class="muted small">Kursalarme sind keine Orders und werden auch beim regelmäßigen Lauf automatisch gepflegt.</p>
      </div>` : ""}
    </section>

    <section class="card section-gap" aria-labelledby="h-prot">
      <div class="card-head"><h2 id="h-prot">Schutz je Position</h2></div>
      ${table.html}
    </section>

    <section class="card section-gap" aria-labelledby="h-hist">
      <div class="card-head"><h2 id="h-hist">Verlauf</h2><span class="muted small">die letzten ${history.length} Ereignisse</span></div>
      ${history.length ? timeline(history, Object.fromEntries(stops.positions.map((p) => [p.isin, p.name]))) : html`<p class="muted small">Noch keine Ereignisse. Hier erscheinen Phasenwechsel, platzierte und stornierte Orders.</p>`}
    </section>
  `);
  table.mount(main);
}

export { render_ as render };

function actionBlock(a) {
  const kindLabel = { place: "neu", replace: "ersetzen", sell_now: "verkaufen", cancel: "stornieren" }[a.kind] || a.kind;
  return html`<div class="card-inner action-block">
    <div class="card-head"><div><b>${a.name}</b> <span class="muted small">${a.headline}</span></div>${a.urgent ? badge("failed", "dringend") : badge("busy", kindLabel)}</div>
    <p class="small">${a.reason}</p>
    <div class="meta-row">${a.current ? html`<span>bisher <b class="mono">${a.current}</b></span>` : ""}${a.new ? html`<span>neu <b class="mono">${a.new}</b></span>` : ""}${a.spec && a.spec.venue ? html`<span>Handelsplatz <b>${a.spec.venue}</b></span>` : ""}</div>
    <div class="row"><a class="btn small" href="#/stops/aenderung/${encodeURIComponent(a.key)}">Vorschau von Scalable anfordern</a><a class="small" href="#/positionen/${encodeURIComponent(a.isin)}">Rechenweg ansehen</a></div>
  </div>`;
}

function unresolvedBox(u, stops) {
  const pos = stops.positions.find((p) => p.isin === u.isin);
  const info = u.info || {};
  return alertBox("error", html`<div class="stack">
    <div><b>Ungeklärte Einreichung${pos ? html` · ${pos.name}` : ""}.</b> Am ${fmt.dateTime(info.at)} war das Ergebnis einer Order-Übermittlung unklar (${info.error || "Verbindungsfehler"}).
    Laut Scalable darf sie nicht blind wiederholt werden. Prüfe in der Scalable-App unter Aufträge, ob die Order existiert.</div>
    <div class="row"><button class="btn small" type="button" data-resolve="adopt" data-isin="${u.isin}">Offene Order übernehmen</button><button class="btn small" type="button" data-resolve="none" data-isin="${u.isin}">Keine Order angelegt</button></div>
  </div>`);
}

const EVENT_CLASS = {
  order_placed: ["done", "msg role-assistant kind-chat"],
  order_filled: ["done", "msg"],
  submission_unknown: ["failed", "msg kind-error"],
  submission_rejected: ["failed", "msg kind-error"],
  unprotected: ["failed", "msg kind-error"],
  phase_changed: ["busy", "msg"],
  order_cancelled: ["", "msg"],
};

function timeline(entries, names) {
  return html`<div class="timeline">${entries.map((e) => {
    const [bcls, mcls] = EVENT_CLASS[e.event] || ["", "msg"];
    const d = e.data || {};
    const who = d.via === "web" ? "Du (Browser)" : ["phase_changed", "order_filled", "order_vanished", "alert_set", "order_adopted"].includes(e.event) ? "stopguard" : "Du";
    const detail = [
      d.isin ? names[d.isin] || d.isin : null,
      d.phase ? `Phase „${d.phase}“${d.reason ? ` – ${d.reason}` : ""}` : null,
      d.spec ? `${d.spec.order_type} ${d.spec.stop_price || d.spec.trail_percent ? (d.spec.stop_price ? `Stop ${d.spec.stop_price}` : `${d.spec.trail_percent} %`) : ""} · ${d.spec.shares} Stk.` : null,
      d.order_id ? `Order ${d.order_id}` : null,
      d.error ? d.error : null,
    ].filter(Boolean).join(" · ");
    return html`<div class="${mcls}"><div class="msg-head"><b>${who}</b> <span class="muted small">${fmt.dateTime(e.at)}</span> ${badge(bcls, e.label)}</div>${detail ? html`<div class="muted small">${detail}</div>` : ""}</div>`;
  })}</div>`;
}
