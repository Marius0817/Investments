// Einstieg: Router (Hash-basiert, funktioniert unter jedem Unterpfad), Topbar, Anmeldung.

import { api, ApiError, onAuthRequired } from "./api.js";
import { data } from "./data.js";
import { $, html, onNextRender, render } from "./dom.js";
import * as fmt from "./fmt.js";
import { errorBox, loadingCard } from "./ui.js";

const NAV = [
  ["#/", "Übersicht", "overview"],
  ["#/positionen", "Positionen", "positions"],
  ["#/trades", "Trades", "trades"],
  ["#/stops", "Stops", "stops"],
  ["#/auswertung", "Auswertung", "analysis"],
];

const ROUTES = [
  [/^\/?$/, "overview", "Übersicht", () => import("./views/overview.js")],
  [/^\/positionen\/?$/, "positions", "Positionen", () => import("./views/positions.js")],
  [/^\/positionen\/([^/]+)$/, "positions", "Position", () => import("./views/position.js")],
  [/^\/trades\/?$/, "trades", "Trades", () => import("./views/trades.js")],
  [/^\/trades\/([^/]+)$/, "trades", "Trade", () => import("./views/trade.js")],
  [/^\/stops\/?$/, "stops", "Stops", () => import("./views/stops.js")],
  [/^\/stops\/(aenderung|vorschau)\/([^/]+)$/, "stops", "Order prüfen", () => import("./views/order.js")],
  [/^\/auswertung(?:\/([a-z]+))?\/?$/, "analysis", "Auswertung", () => import("./views/analysis.js")],
  [/^\/daten\/?$/, "data", "Daten & Einstellungen", () => import("./views/daten.js")],
];

let cleanup = null;
let renderToken = 0;

function renderNav(active) {
  render($("#nav"), NAV.map(([href, label, key]) =>
    html`<a href="${href}" class="${key === active ? "active" : ""}" ${key === active ? html`aria-current="page"` : ""}>${label}</a>`));
}

async function renderStatus() {
  const box = $("#status");
  try {
    const app = await data.app();
    const sync = app.last_sync_at;
    const ageH = sync ? (Date.now() - new Date(sync).getTime()) / 3.6e6 : null;
    const dot = ageH === null ? "off" : ageH < 26 ? "ok" : "warn";
    const exp = app.session && app.session.expires_at ? (new Date(app.session.expires_at).getTime() - Date.now()) / 3.6e6 : null;
    render(box, html`
      ${app.demo ? html`<span class="badge wait">Demo</span><span class="sep">·</span>` : ""}
      ${exp !== null && exp < 24 ? html`<span class="dot warn"></span><span>Login läuft ${exp < 0 ? "ab" : "bald ab"}</span><span class="sep">·</span>` : ""}
      <a href="#/daten" class="status-link"><span class="dot ${dot}"></span>${sync ? html`Daten ${fmt.relTime(sync)}` : "Noch keine Daten"}</a>
    `);
  } catch {
    render(box, html`<span class="dot off"></span><span>Server nicht erreichbar</span>`);
  }
}

function showLogin(message) {
  const main = $("#main");
  renderNav(null);
  render($("#status"), "");
  render(main, html`<div class="card narrow">
    <h1 tabindex="-1">Anmelden</h1>
    <p class="muted small">Diese stopguard-Oberfläche ist mit einem Zugangsschlüssel geschützt. Er steht in der Umgebungsvariable <code>STOPGUARD_WEB_TOKEN</code> des Servers.</p>
    ${message ? html`<p class="alert error small">${message}</p>` : ""}
    <form class="stack" id="login-form">
      <label>Zugangsschlüssel<input type="password" id="login-token" autocomplete="current-password" required></label>
      <button class="btn primary" type="submit">Anmelden</button>
    </form>
  </div>`);
  $("#login-token").focus();
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button");
    btn.disabled = true;
    btn.textContent = "Wird geprüft …";
    try {
      await api.post("session", { token: $("#login-token").value });
      data.invalidate();
      route();
    } catch (err) {
      showLogin(err instanceof ApiError ? err.message : "Anmeldung fehlgeschlagen.");
    }
  });
}

async function route() {
  const token = ++renderToken;
  const path = decodeURIComponent((location.hash || "#/").slice(1)) || "/";
  const match = ROUTES.map((r) => [r, path.match(r[0])]).find(([, m]) => m);
  const main = $("#main");
  if (cleanup) { try { cleanup(); } catch { /* egal */ } cleanup = null; }
  if (!match) {
    renderNav(null);
    render(main, html`<div class="card empty"><h1 tabindex="-1">Seite nicht gefunden</h1><p class="muted small">Diese Adresse gibt es in stopguard nicht.</p><div><a class="btn" href="#/">Zur Übersicht</a></div></div>`);
    return;
  }
  const [[, key, title, loader], m] = match;
  renderNav(key);
  document.title = `${title} · stopguard`;
  // Jede Ansicht bekommt einen eigenen Container: Eine langsame, inzwischen verlassene
  // Ansicht rendert dann ins Leere statt über die neue Seite.
  const view = document.createElement("div");
  view.className = "view";
  const show = () => { if (token === renderToken && view.parentNode !== main) main.replaceChildren(view); };
  if (!main.firstElementChild) { render(view, loadingCard("Seite wird geladen …")); show(); }
  for (const old of main.children) if (old !== view) old.classList.add("is-stale");
  main.setAttribute("aria-busy", "true");
  // Bis die neue Ansicht etwas zeigt, bleibt die alte (abgeblendet) sichtbar. Eingehängt wird
  // synchron beim ersten render(), damit die Ansicht direkt danach Fokus setzen kann.
  onNextRender(view, show);
  try {
    const mod = await loader();
    if (token !== renderToken) return;
    const result = await mod.render(view, m.slice(1), { title: (t) => { if (token === renderToken) document.title = `${t} · stopguard`; } });
    if (token !== renderToken) {
      if (typeof result === "function") result();
      return;
    }
    show();
    cleanup = typeof result === "function" ? result : null;
    // Fokus auf die Überschrift: Screenreader erfahren vom Seitenwechsel, Tab startet im Inhalt.
    // (Hat die Ansicht den Fokus schon bewusst gesetzt, z. B. auf einen Reiter, bleibt er dort.)
    const h1 = view.querySelector("h1");
    if (h1 && !view.contains(document.activeElement)) h1.focus({ preventScroll: true });
  } catch (err) {
    if (token !== renderToken) return;
    if (err instanceof ApiError && err.status === 401 && err.code === "login_required") { showLogin(); return; }
    render(view, html`<div class="stack"><h1 tabindex="-1">${title}</h1>${errorBox(err, "Bitte prüfe, ob „stopguard web“ läuft, und lade die Seite neu.")}</div>`);
    show();
  } finally {
    if (token === renderToken) main.removeAttribute("aria-busy");
  }
  renderStatus();
}

onAuthRequired(() => showLogin());
// Sprunglink: ändert nicht den Hash (der gehört dem Router), sondern setzt nur den Fokus.
document.querySelector(".skip-link").addEventListener("click", (ev) => { ev.preventDefault(); $("#main").focus(); });
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
route();
