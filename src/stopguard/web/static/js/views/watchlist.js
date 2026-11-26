// Watchlist: Wertpapiere beobachten, Zielkurs festlegen, Kursalarm bei Scalable setzen (keine Order).

import { api } from "../api.js";
import { sparkline } from "../charts.js";
import { data } from "../data.js";
import { $, html, on, render } from "../dom.js";
import * as fmt from "../fmt.js";
import { alertBox, badge, confirmDialog, errorBox, pageHead, progress, toast } from "../ui.js";

function change(v, label) {
  return html`<span class="wl-change"><span class="muted small">${label}</span> <span class="num ${fmt.tone(v)}">${fmt.pct(v, 1, true)}</span></span>`;
}

function card(it, look) {
  const inEtf = look.get(it.isin);
  return html`<li class="card wl-item" data-isin="${it.isin}">
    <div class="wl-head">
      <div><b>${it.name || it.isin}</b><div class="muted small mono">${it.isin}</div></div>
      <div class="wl-price"><span class="num wl-last">${it.price ? fmt.eur(it.price) : fmt.DASH}</span>
        ${it.quote_time ? html`<span class="muted small">${it.outdated ? "Kurs nicht aktuell · " : ""}${fmt.time(it.quote_time)} Uhr</span>` : ""}</div>
    </div>
    ${it.error ? alertBox("warn", html`Kein Kurs: ${it.error}`, { small: true }) : html`
      <div class="wl-chart" title="Verlauf der letzten 12 Monate">${sparkline(it.history || [], { cls: "c1" })}</div>
      <div class="wl-stats">
        ${it.change ? html`${change(it.change["1m"], "1 M")}${change(it.change["3m"], "3 M")}${change(it.change["1y"], "1 J")}` : ""}
        ${it.from_high !== undefined ? html`<span class="wl-change"><span class="muted small">vom 52-W-Hoch</span> <span class="num">${fmt.pct(it.from_high, 1)}</span></span>` : ""}
      </div>`}
    <div class="wl-target">
      ${it.target_price
        ? html`<span>Zielkurs <b class="num">${fmt.eur(it.target_price)}</b>${it.to_target !== undefined ? html` · ${it.target_reached ? badge("done", "erreicht") : html`noch <span class="num">${fmt.pct(-it.to_target, 1)}</span> entfernt`}` : ""}</span>`
        : html`<span class="muted small">kein Zielkurs</span>`}
      ${(it.alerts || []).length ? html`<span class="small">${badge("", "Kursalarm")} bei ${it.alerts.map((a, i) => html`${i ? ", " : ""}${fmt.eur(a.price)}`)}</span>` : ""}
    </div>
    ${it.note ? html`<p class="small wl-note">${it.note}</p>` : ""}
    ${inEtf ? html`<p class="small muted">Steckt bereits mit ${fmt.eur(inEtf.via_funds + inEtf.direct, 0)} in deinem Depot${inEtf.via_funds ? " (über ETFs)" : ""}.</p>` : ""}
    <div class="row wl-actions">
      ${it.target_price && !(it.alerts || []).some((a) => Math.abs(a.price - it.target_price) < 0.005)
        ? html`<button type="button" class="btn small" data-action="alert" data-isin="${it.isin}" data-price="${it.target_price}">Kursalarm bei ${fmt.eur(it.target_price)} setzen</button>` : ""}
      <button type="button" class="btn small" data-action="edit" data-isin="${it.isin}">Bearbeiten</button>
      <button type="button" class="btn small ghost" data-action="remove" data-isin="${it.isin}">Entfernen</button>
    </div>
  </li>`;
}

function form(item = null) {
  return html`<form id="wl-form" class="wl-form" aria-label="${item ? "Eintrag bearbeiten" : "Wertpapier hinzufügen"}">
    <label class="field">ISIN<input type="text" name="isin" required maxlength="14" autocomplete="off" spellcheck="false" value="${item ? item.isin : ""}" ${item ? "readonly" : ""} placeholder="z. B. US5949181045"></label>
    <label class="field">Zielkurs (optional)<span class="input-unit"><input type="text" inputmode="decimal" name="target_price" value="${item && item.target_price ? fmt.num(item.target_price, 2) : ""}"><span class="unit">€</span></span></label>
    <label class="field grow">Notiz (optional)<input type="text" name="note" maxlength="500" value="${item ? item.note || "" : ""}"></label>
    <div class="row"><button type="submit" class="btn primary">${item ? "Speichern" : "Hinzufügen"}</button>${item ? html`<button type="button" class="btn" data-action="cancel-edit">Abbrechen</button>` : ""}</div>
  </form>`;
}

export async function render_(main, params, ctx, { refresh = false } = {}) {
  render(main, html`
    ${pageHead("Watchlist", { meta: html`<span>Kurse über die offizielle Scalable CLI</span>`, actions: html`<button class="btn" type="button" data-action="refresh">Kurse aktualisieren</button>` })}
    <section class="card" aria-labelledby="h-add"><h2 id="h-add">Wertpapier beobachten</h2><div id="wl-form-box">${form()}</div>
      <p class="muted small">Die ISIN findest du in der Scalable-App beim Wertpapier. Ein Zielkurs lässt sich als <b>Kursalarm bei Scalable</b> hinterlegen – das ist keine Order, die App benachrichtigt dich nur.</p></section>
    <div id="wl-list" class="section-gap">${progress("Kurse werden bei Scalable abgefragt …")}</div>
  `);
  let alive = true;
  let items = [];
  const look = new Map();
  data.portfolio().then((doc) => {
    for (const i of ((doc.allocation || {}).look_through || {}).items || []) if (i.isin) look.set(i.isin, i);
  }).catch(() => {});
  const load = async (fresh) => {
    try {
      const res = await api.get(`watchlist${fresh ? "?refresh=1" : ""}`);
      if (!alive) return;
      items = res.items;
      render($("#wl-list", main), html`
        ${res.alerts_error ? alertBox("warn", html`Kursalarme konnten nicht geladen werden: ${res.alerts_error}`, { small: true }) : ""}
        ${items.length
          ? html`<ul class="wl-grid">${items.map((it) => card(it, look))}</ul><p class="muted small">Stand ${fmt.time(res.fetched_at)} Uhr · Verlauf: 12 Monate</p>`
          : html`<div class="card empty"><b>Noch nichts auf der Watchlist.</b><p class="muted small">Füge oben eine ISIN hinzu, um Kurs, Verlauf und Abstand zum Zielkurs zu sehen.</p></div>`}
      `);
    } catch (err) {
      if (alive) render($("#wl-list", main), errorBox(err));
    }
  };
  load(refresh);
  const submit = async (ev, f) => {
    ev.preventDefault();
    const fd = new FormData(f);
    const target = String(fd.get("target_price") || "").trim();
    const price = target ? fmt.parseNumber(target) : null;
    if (target && (price === null || price <= 0)) { toast("Der Zielkurs muss eine Zahl größer als 0 sein.", { error: true }); return; }
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const item = await api.post("watchlist", { isin: fd.get("isin"), target_price: price, note: fd.get("note") || "" });
      toast(`${item.name || item.isin} gespeichert.`);
      render($("#wl-form-box", main), form());
      await load(true);
    } catch (err) {
      toast(err.message, { error: true });
    } finally {
      btn.disabled = false;
    }
  };
  const offs = [
    on(main, "submit", "#wl-form", submit),
    on(main, "click", "[data-action=refresh]", async (ev, btn) => { btn.disabled = true; await load(true); btn.disabled = false; }),
    on(main, "click", "[data-action=edit]", (ev, btn) => {
      const it = items.find((i) => i.isin === btn.dataset.isin);
      render($("#wl-form-box", main), form(it));
      $("#wl-form input[name=target_price]", main).focus();
      $("#h-add", main).scrollIntoView({ block: "nearest" });
    }),
    on(main, "click", "[data-action=cancel-edit]", () => render($("#wl-form-box", main), form())),
    on(main, "click", "[data-action=remove]", async (ev, btn) => {
      const it = items.find((i) => i.isin === btn.dataset.isin);
      const ok = await confirmDialog({ title: "Von der Watchlist entfernen?", body: html`<p>${it ? it.name : btn.dataset.isin} wird nicht mehr beobachtet. Kursalarme bei Scalable bleiben bestehen.</p>`, confirmLabel: "Entfernen", danger: true });
      if (!ok) return;
      try {
        await api.post(`watchlist/${encodeURIComponent(btn.dataset.isin)}/remove`, {});
        toast("Entfernt.");
        await load(false);
      } catch (err) { toast(err.message, { error: true }); }
    }),
    on(main, "click", "[data-action=alert]", async (ev, btn) => {
      const it = items.find((i) => i.isin === btn.dataset.isin);
      const price = Number(btn.dataset.price);
      const ok = await confirmDialog({
        title: "Kursalarm bei Scalable setzen",
        body: html`<p>Für <b>${it ? it.name : btn.dataset.isin}</b> wird bei Scalable ein Kursalarm bei <b>${fmt.eur(price)}</b> angelegt.
          Die Scalable-App benachrichtigt dich, sobald der Kurs ihn erreicht. <b>Es wird keine Order angelegt.</b></p>`,
        confirmLabel: "Kursalarm setzen",
      });
      if (!ok) return;
      btn.disabled = true;
      try {
        await api.post(`watchlist/${encodeURIComponent(btn.dataset.isin)}/alert`, { price });
        toast("Kursalarm gesetzt.");
        await load(true);
      } catch (err) {
        toast(`Kursalarm nicht gesetzt: ${err.message}`, { error: true });
        btn.disabled = false;
      }
    }),
  ];
  return () => { alive = false; offs.forEach((o) => o()); };
}

export { render_ as render };
