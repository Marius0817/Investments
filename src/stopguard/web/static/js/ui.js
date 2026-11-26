// Wiederverwendbare Bausteine nach den Amboss-Leitfäden.

import { $, html, on } from "./dom.js";
import * as fmt from "./fmt.js";

// ---------------------------------------------------------------- Kopf & Zustände

export function pageHead(title, { meta = "", actions = "", badges = "" } = {}) {
  return html`<div class="page-head">
    <div class="stack page-title">
      ${badges ? html`<div class="title-row"><h1 tabindex="-1">${title}</h1><span class="row">${badges}</span></div>` : html`<h1 tabindex="-1">${title}</h1>`}
      ${meta ? html`<div class="meta-row">${meta}</div>` : ""}
    </div>
    ${actions ? html`<div class="row">${actions}</div>` : ""}
  </div>`;
}

export function progress(text) {
  return html`<div class="progress" role="status"><span class="spinner" aria-hidden="true"></span><span>${text}</span></div>`;
}

export function loadingCard(text) {
  return html`<div class="card">${progress(text)}</div>`;
}

export function alertBox(level, content, { small = false } = {}) {
  return html`<div class="alert ${level}${small ? " small" : ""}" role="${level === "error" ? "alert" : "note"}">${content}</div>`;
}

/** Fehler in drei Teilen: was, warum, was jetzt. */
export function errorBox(err, nextStep) {
  const msg = (err && err.message) || String(err);
  const hint = (err && err.body && err.body.hint) || nextStep;
  return alertBox("error", html`<b>Das hat nicht geklappt.</b> ${msg}${hint ? html` ${hint}` : ""}`);
}

export function emptyCard(title, text, action = "") {
  return html`<div class="card empty">
    <h2>${title}</h2>
    <p class="muted small">${text}</p>
    ${action ? html`<div>${action}</div>` : ""}
  </div>`;
}

export function badge(state, text) {
  return html`<span class="badge ${state}">${text}</span>`;
}

export function tag(text) {
  return html`<span class="tag">${text}</span>`;
}

// ---------------------------------------------------------------- Kacheln

export function tiles(items) {
  const list = items.filter(Boolean);
  return html`<div class="tiles ${list.length === 4 ? "n4" : ""}">${list.map(tile)}</div>`;
}

export function tile({ label, value, note = "", side = "", meter = "", toneValue = null, href = "" }) {
  const cls = toneValue === null ? "" : fmt.tone(toneValue);
  const body = html`
    <div class="tile-head">${label}${side ? html`<span>${side}</span>` : ""}</div>
    <div class="tile-value ${cls}">${value}</div>
    ${meter}
    ${note ? html`<div class="tile-note">${note}</div>` : ""}`;
  return href ? html`<a class="tile tile-link" href="${href}">${body}</a>` : html`<div class="tile">${body}</div>`;
}

// ---------------------------------------------------------------- Zahlen

export function money(v, { sign = false, digits = 2 } = {}) {
  return html`<span class="num ${sign ? fmt.tone(v) : ""}">${sign ? fmt.signedEur(v, digits) : fmt.eur(v, digits)}</span>`;
}

export function percent(v, { sign = true, digits = 1 } = {}) {
  return html`<span class="num ${sign ? fmt.tone(v) : ""}">${fmt.pct(v, digits, sign)}</span>`;
}

// ---------------------------------------------------------------- Filterleiste

/** Segment-Auswahl (Radiogruppe) – eine Zeile über dem Inhalt, den sie filtert. */
export function segmented(name, label, options, value) {
  return html`<fieldset class="segmented" role="radiogroup" aria-label="${label}">
    <legend class="sr-only">${label}</legend>
    ${options.map(
      ([v, text]) => html`<label class="${v === value ? "selected" : ""}"><input type="radio" name="${name}" value="${v}" ${v === value ? html`checked` : ""}><span>${text}</span></label>`,
    )}
  </fieldset>`;
}

// ---------------------------------------------------------------- Tabellen

/**
 * Sortierbare Tabelle. columns: {key, label, num?, sort?(row), render(row)}
 * Liefert {html, mount(root)}. Sortierung per Klick/Tastatur auf die Spaltenköpfe (aria-sort).
 */
/**
 * Sortierbare Tabelle. `state` ({ key, dir }) kann von außen übergeben werden, damit die
 * gewählte Sortierung ein Neuzeichnen (z. B. nach Filterwechsel) übersteht.
 */
export function sortableTable(id, columns, rows, { sortKey = null, dir = "desc", rowHref = null, empty = "Keine Einträge.", state = null, limit = null } = {}) {
  if (state && !state.key) { state.key = sortKey; state.dir = dir; }
  state = state || { key: sortKey, dir };
  const sorted = () => {
    if (!state.key) return rows;
    const col = columns.find((c) => c.key === state.key);
    const val = col.sort || ((r) => r[col.key]);
    const out = [...rows].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      const c = typeof va === "string" ? va.localeCompare(vb, "de") : va - vb;
      return state.dir === "asc" ? c : -c;
    });
    return out;
  };
  const head = () => html`<tr>${columns.map((c) => {
    const active = state.key === c.key;
    const aria = active ? (state.dir === "asc" ? "ascending" : "descending") : "none";
    const arrow = active ? (state.dir === "asc" ? "↑" : "↓") : "";
    return c.sortable === false
      ? html`<th scope="col" class="${c.num ? "num" : ""}">${c.label}</th>`
      : html`<th scope="col" class="${c.num ? "num" : ""}" aria-sort="${aria}"><button type="button" class="th-sort" data-sort="${c.key}">${c.label}<span class="sort-arrow" aria-hidden="true">${arrow}</span></button></th>`;
  })}</tr>`;
  const body = () => {
    const list = limit ? sorted().slice(0, limit) : sorted();
    if (!list.length) return html`<tr><td colspan="${columns.length}" class="muted">${empty}</td></tr>`;
    return list.map(
      (r) => html`<tr class="${rowHref ? "row-link" : ""} ${r._rowClass || ""}" ${rowHref ? html`data-href="${rowHref(r)}"` : ""}>${columns.map(
        (c, i) => html`<td class="${c.num ? "num" : ""} ${c.cls || ""}" ${i ? html`data-label="${c.label}"` : ""}>${c.render ? c.render(r) : r[c.key]}</td>`,
      )}</tr>`,
    );
  };
  // Auf schmalen Bildschirmen werden die Zeilen gestapelt; sortiert wird dann über diese Auswahl.
  const sortable = columns.filter((c) => c.sortable !== false);
  const sortControl = () => html`<label class="inline-field">Sortieren nach
      <select data-sort-select>${sortable.map((c) => html`<option value="${c.key}" ${c.key === state.key ? "selected" : ""}>${c.label}</option>`)}</select></label>
    <button type="button" class="btn small" data-sort-dir aria-label="Reihenfolge umkehren, derzeit ${state.dir === "asc" ? "aufsteigend" : "absteigend"}">${state.dir === "asc" ? "↑ aufsteigend" : "↓ absteigend"}</button>`;
  const markup = html`<div class="table-sort-sm" id="${id}-sort">${sortable.length > 1 ? sortControl() : ""}</div>
    <div class="table-scroll"><table class="table stacked" id="${id}"><thead>${head()}</thead><tbody>${body()}</tbody></table></div>`;
  const mount = (root) => {
    const table = $(`#${id}`, root);
    if (!table) return;
    const control = $(`#${id}-sort`, root);
    const redraw = (focusSel) => {
      table.tHead.innerHTML = head().text;
      table.tBodies[0].innerHTML = body().text;
      if (control && sortable.length > 1) control.innerHTML = sortControl().text;
      const el = focusSel && (table.querySelector(focusSel) || (control && control.querySelector(focusSel)));
      if (el) el.focus();
    };
    on(table, "click", "[data-sort]", (ev, btn) => {
      const key = btn.dataset.sort;
      if (state.key === key) state.dir = state.dir === "asc" ? "desc" : "asc";
      else { state.key = key; state.dir = columns.find((c) => c.key === key).num ? "desc" : "asc"; }
      redraw(`[data-sort="${key}"]`);
    });
    if (control) {
      on(control, "change", "[data-sort-select]", (ev, sel) => {
        state.key = sel.value;
        state.dir = columns.find((c) => c.key === sel.value).num ? "desc" : "asc";
        redraw("[data-sort-select]");
      });
      on(control, "click", "[data-sort-dir]", () => {
        state.dir = state.dir === "asc" ? "desc" : "asc";
        redraw("[data-sort-dir]");
      });
    }
    if (rowHref) {
      on(table, "click", "tr.row-link", (ev, tr) => {
        if (ev.target.closest("a, button, input, label")) return;
        location.hash = tr.dataset.href;
      });
    }
  };
  return { html: markup, mount };
}

export function kvTable(rows) {
  return html`<table class="table kv"><tbody>${rows
    .filter(Boolean)
    .map(([k, v]) => html`<tr><th scope="row">${k}</th><td>${v}</td></tr>`)}</tbody></table>`;
}

// ---------------------------------------------------------------- Toasts

export function toast(text, { error = false } = {}) {
  const box = $("#toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast${error ? " error" : ""}`;
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => el.remove(), error ? 8000 : 4500);
}

// ---------------------------------------------------------------- Dialog

/**
 * Bestätigungsdialog für zerstörende/folgenreiche Schritte. Fokus bleibt im Dialog,
 * Escape bricht ab (außer bei strict), der bestätigende Schalter nennt das Verb.
 */
export function confirmDialog({ title, body, confirmLabel, danger = false, strict = false }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const wrap = document.createElement("div");
    wrap.className = "modal";
    wrap.innerHTML = html`<div class="modal-body dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      <div class="modal-head"><h2 id="dlg-title">${title}</h2></div>
      <div class="stack">${body}</div>
      <div class="row dialog-actions">
        <button type="button" class="btn" data-r="0">Abbrechen</button>
        <button type="button" class="btn ${danger ? "danger" : "primary"}" data-r="1">${confirmLabel}</button>
      </div>
    </div>`.text;
    document.body.appendChild(wrap);
    const buttons = wrap.querySelectorAll("button");
    const close = (r) => { wrap.remove(); document.removeEventListener("keydown", key, true); if (prev && prev.focus) prev.focus(); resolve(r); };
    const key = (ev) => {
      if (ev.key === "Escape" && !strict) { ev.preventDefault(); close(false); }
      if (ev.key === "Tab") {
        const f = Array.from(wrap.querySelectorAll("button, a, input, select, textarea"));
        const i = f.indexOf(document.activeElement);
        if (ev.shiftKey && i <= 0) { f[f.length - 1].focus(); ev.preventDefault(); }
        else if (!ev.shiftKey && i === f.length - 1) { f[0].focus(); ev.preventDefault(); }
      }
    };
    document.addEventListener("keydown", key, true);
    wrap.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-r]");
      if (b) close(b.dataset.r === "1");
      else if (ev.target === wrap && !strict) close(false);
    });
    buttons[0].focus();
  });
}

// ---------------------------------------------------------------- Phasen als Stepper

export function stepper(phases) {
  return html`<ol class="stepper" aria-label="Phasen der Strategie">${phases.map(
    (p) => html`<li class="${p.state === "upcoming" ? "" : p.state}" ${p.state === "current" ? html`aria-current="step"` : ""}><span class="step-dot" aria-hidden="true"></span>${p.name}</li>`,
  )}</ol>`;
}

export const PHASE_BADGE = { current: "busy", done: "done", upcoming: "" };
