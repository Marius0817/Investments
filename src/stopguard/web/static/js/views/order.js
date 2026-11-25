// Order prüfen und bestätigen – die einzige Stelle, an der der Browser eine Order platziert.
// Ablauf wie von der Scalable CLI verlangt: Vorschau (Phase 1) → ausdrückliche Zustimmung → Phase 2.

import { api } from "../api.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, errorBox, kvTable, pageHead, progress, toast } from "../ui.js";

export async function render_(main, [mode, param], ctx) {
  const value = decodeURIComponent(param);
  let preview;
  if (mode === "aenderung") {
    render(main, html`${pageHead("Order prüfen")}<div class="card">${progress("Scalable erstellt die Vorschau mit allen Kosten …")}</div>`);
    preview = await data.mutate(request(main, value, false), "stops");
    if (!preview) return null;
  } else {
    try {
      preview = await api.get(`orders/${encodeURIComponent(value)}`);
    } catch (err) {
      render(main, html`${pageHead("Order prüfen")}${errorBox(err, html`Fordere die Vorschau unter <a href="#/stops">Stops</a> neu an.`)}`);
      return null;
    }
  }
  if (preview.status === "needs_cancel_first") return renderCancelFirst(main, preview, value);
  history.replaceState(null, "", `#/stops/vorschau/${preview.id}`);
  ctx.title(`Order prüfen · ${preview.action.name}`);
  return renderPreview(main, preview);
}

export { render_ as render };

async function request(main, key, cancelFirst) {
  try {
    return await api.post("orders/preview", { key, cancel_first: cancelFirst });
  } catch (err) {
    render(main, html`${pageHead("Order prüfen")}${errorBox(err, html`Zurück zu den <a href="#/stops">Stops</a>.`)}`);
    data.invalidate("stops");
    return null;
  }
}

function renderCancelFirst(main, preview, key) {
  const a = preview.action;
  render(main, html`
    <p class="crumbs"><a href="#/stops">Stops</a> /</p>
    ${pageHead("Alte Order zuerst stornieren?", { meta: html`<span>${a.name}</span>` })}
    <section class="card">
      ${alertBox("warn", html`<b>${preview.message}</b><br><span class="small">Meldung von Scalable: ${preview.error}</span>`)}
      ${kvTable([["Bisher", html`<span class="mono">${a.current || "–"}</span>`], ["Neu", html`<span class="mono">${a.new}</span>`], ["Grund", a.reason]])}
      <p class="small">Nach dem Stornieren zeigt dir stopguard die Vorschau der neuen Order. Lehnst du sie dann ab, bleibt die Position <b>ohne Schutz</b>, bis du eine neue Order platzierst.</p>
      <div class="row"><button class="btn danger" type="button" data-action="cancel-first">Alte Order stornieren und Vorschau anfordern</button><a class="btn" href="#/stops">Nichts ändern</a></div>
    </section>
  `);
  let disposePreview = null;
  let disposed = false;
  const off = on(main, "click", "[data-action=cancel-first]", async (ev, btn) => {
    btn.disabled = true;
    btn.textContent = "Order wird storniert …";
    const p = await data.mutate(request(main, key, true), "stops");
    if (!p || disposed) return;
    off();
    history.replaceState(null, "", `#/stops/vorschau/${p.id}`);
    disposePreview = renderPreview(main, p);
  });
  return () => { disposed = true; off(); if (disposePreview) disposePreview(); };
}

function presentation(pres) {
  const sections = pres.sections || {};
  const order = pres.section_order || Object.keys(sections);
  if (!order.length) return html`<p class="muted">Scalable hat keine strukturierte Vorschau geliefert.</p>`;
  return order.map((key) => {
    const sec = sections[key] || {};
    return html`<div class="stack"><h3>${sec.title || key}</h3>${kvTable((sec.fields || []).map((f) => [f.label, html`<span class="${typeof f.value === "number" ? "num" : ""}">${value(f.value)}</span>`]))}</div>`;
  });
}

function value(v) {
  // Werte unverändert anzeigen (Vorgabe der Scalable CLI: preserve_exact_values, null sichtbar)
  if (v === null || v === undefined) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function renderPreview(main, p) {
  const a = p.action;
  let timer = null;
  const expires = p.expires_at_epoch ? p.expires_at_epoch * 1000 : null;
  render(main, html`
    <p class="crumbs"><a href="#/stops">Stops</a> /</p>
    ${pageHead("Order prüfen", { meta: html`<span>${a.name}</span><span class="mono">${a.isin}</span>` })}
    <div class="stack">
      ${p.cancelled_first ? alertBox("warn", html`<b>Die alte Order ist bereits storniert.</b> Die Position ist ungeschützt, bis du diese Order platzierst.`) : ""}
      ${alertBox("info", html`Das ist die <b>Vorschau von Scalable</b> – es wurde noch <b>keine Order</b> angelegt. Prüfe alle Angaben; platziert wird erst mit dem Schalter unten. <span id="expiry"></span>`)}
    </div>
    <div class="two-col section-gap">
      <section class="card" aria-labelledby="h-what">
        <div class="card-head"><h2 id="h-what">Was sich ändert</h2></div>
        ${kvTable([
          ["Aktion", a.headline],
          ["Grund", a.reason],
          a.current ? ["Bisher", html`<span class="mono">${a.current}</span>`] : null,
          ["Neu", html`<span class="mono">${a.new}</span>`],
          a.spec && a.spec.venue ? ["Handelsplatz", a.spec.venue] : null,
        ])}
        ${p.replaces_order_id && !p.cancelled_first ? html`<p class="small">Nach deiner Bestätigung storniert stopguard zuerst die bestehende Order <span class="mono">${p.replaces_order_id}</span> und reicht danach die neue ein.</p>` : ""}
      </section>
      <section class="card" aria-labelledby="h-scalable">
        <div class="card-head"><h2 id="h-scalable">Angaben von Scalable</h2></div>
        <p class="muted small">Vollständig und unverändert übernommen, inklusive Kosten.</p>
        ${presentation(p.presentation)}
      </section>
    </div>
    <section class="card section-gap" aria-labelledby="h-confirm" id="confirm-card">
      <div class="card-head"><h2 id="h-confirm">Auftrag erteilen</h2></div>
      <label class="inline-check"><input type="checkbox" id="checked"> <span>Ich habe alle Angaben geprüft und möchte diese Order bei Scalable erteilen.</span></label>
      <div class="row">
        <button class="btn big primary" type="button" id="place" disabled>Order verbindlich platzieren</button>
        <button class="btn" type="button" id="discard">Verwerfen</button>
      </div>
      <div id="result" aria-live="polite"></div>
    </section>
  `);
  const tick = () => {
    const el = $("#expiry", main);
    if (!el || !expires) return;
    const left = Math.round((expires - Date.now()) / 60000);
    el.textContent = left > 0 ? `Die Bestätigung ist noch ${left} Minute${left === 1 ? "" : "n"} gültig (bis ${fmt.time(new Date(expires).toISOString())} Uhr).` : "Die Bestätigung ist abgelaufen – bitte eine neue Vorschau anfordern.";
    if (left <= 0) $("#place", main).disabled = true;
  };
  tick();
  timer = setInterval(tick, 15000);
  const checkbox = $("#checked", main);
  const place = $("#place", main);
  checkbox.addEventListener("change", () => { place.disabled = !checkbox.checked || (expires && Date.now() > expires); });
  const card = $("#confirm-card", main);
  const result = $("#result", main);
  place.addEventListener("click", async () => {
    place.disabled = true;
    checkbox.disabled = true;
    $("#discard", main).disabled = true;
    place.textContent = a.cancel_order_id && !p.cancelled_first ? "Alte Order wird storniert, neue eingereicht …" : "Order wird eingereicht …";
    clearInterval(timer);
    try {
      const res = await data.mutate(api.post(`orders/${p.id}/confirm`, { confirm: true }), "stops", "portfolio");
      toast(`Order platziert: ${a.name}.`);
      // Wurde die Seite inzwischen verlassen, reicht die Meldung.
      if (!card.isConnected) return;
      render(card, html`<div class="card-head"><h2>Order platziert</h2></div>
        ${alertBox("info", html`<b>${res.description}</b> ist bei Scalable eingereicht${res.order_id ? html` (Order <span class="mono">${res.order_id}</span>)` : ""}. stopguard verwaltet sie ab jetzt.`)}
        <div><a class="btn" href="#/stops">Zurück zu den Stops</a></div>`);
      $("a", card).focus();
    } catch (err) {
      const unprotected = err.body && err.body.unprotected;
      if (!result.isConnected) {
        toast(`Order für ${a.name} nicht platziert: ${err.message}`, { error: true });
        return;
      }
      render(result, html`<div class="stack">${errorBox(err)}${unprotected ? alertBox("warn", html`<b>Die Position ist derzeit ungeschützt.</b> Bitte in der Scalable-App prüfen und unter <a href="#/stops">Stops</a> eine neue Vorschau anfordern.`) : ""}</div>`);
      place.textContent = "Nicht platziert";
    }
  });
  $("#discard", main).addEventListener("click", async () => {
    clearInterval(timer);
    const res = await data.mutate(api.post(`orders/${p.id}/discard`, {}), "stops").catch(() => ({}));
    if (res.unprotected) toast("Verworfen – die Position ist ohne Schutz.", { error: true });
    else toast("Vorschau verworfen – es wurde nichts geändert.");
    location.hash = "#/stops";
  });
  return () => clearInterval(timer);
}
