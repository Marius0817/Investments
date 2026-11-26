// Hinweise zum Depot (regelbasiert, vom Server berechnet) – Liste für Übersicht und Auswertung.

import { html } from "../dom.js";
import * as fmt from "../fmt.js";
import { badge } from "../ui.js";

const LEVEL = { warn: ["busy", "Achtung"], info: ["", "Hinweis"], good: ["done", "Positiv"] };

export function insightList(items, { limit = null, compact = false } = {}) {
  const list = limit ? items.slice(0, limit) : items;
  return html`<ul class="insights ${compact ? "compact" : ""}">${list.map((i) => {
    const [cls, text] = LEVEL[i.level] || LEVEL.info;
    return html`<li class="insight level-${i.level}">
      <div class="insight-head">${badge(cls, text)}<span class="muted small">${i.category}</span></div>
      <div class="insight-body"><b>${fmt.maskText(i.title)}</b>${compact ? "" : html`<p class="small">${fmt.maskText(i.message)}</p>`}</div>
      ${i.link ? html`<a class="small insight-link" href="${i.link}">Ansehen<span class="sr-only">: ${fmt.maskText(i.title)}</span></a>` : ""}
    </li>`;
  })}</ul>`;
}

export function insightsPanel(doc) {
  const items = doc.insights || [];
  const byCat = new Map();
  for (const i of items) {
    if (!byCat.has(i.category)) byCat.set(i.category, []);
    byCat.get(i.category).push(i);
  }
  return {
    content: html`
      <p class="muted small">stopguard prüft dein Depot bei jeder Auswertung auf Auffälligkeiten – Klumpenrisiken, Kosten, Kredit, Steuern, Performance.
        Jeder Hinweis nennt die Zahl dahinter und führt zur passenden Ansicht. Es sind Beobachtungen, keine Anlageempfehlungen.</p>
      ${items.length
        ? [...byCat.entries()].map(([cat, list]) => html`<section class="stack" aria-label="${cat}"><h2>${cat}</h2>${insightList(list)}</section>`)
        : html`<div class="empty-inline"><b>Keine Auffälligkeiten.</b><p class="muted small">Sobald etwas auffällt – etwa eine einzelne Aktie mit sehr hohem Anteil oder ungenutzter Freibetrag zum Jahresende –, erscheint es hier.</p></div>`}
    `,
  };
}
