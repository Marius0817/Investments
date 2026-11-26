// Zwischenspeicher für die großen API-Antworten. Neu laden hält den alten Stand sichtbar.

import { api } from "./api.js";

const cache = { app: null, portfolio: null, stops: null };
const pending = {};
const mutations = new Set();
const subscribers = new Set();

export function subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); }
function notify(kind) { subscribers.forEach((fn) => fn(kind)); }

async function load(kind, path, refresh) {
  // Läuft gerade eine Änderung (z. B. Order-Bestätigung), erst deren Ergebnis abwarten –
  // sonst zeigt eine schnell gewechselte Ansicht den Stand von vorher.
  if (mutations.size) await Promise.allSettled([...mutations]);
  if (!refresh && cache[kind]) return cache[kind];
  if (pending[kind] && !refresh) return pending[kind];
  pending[kind] = api
    .get(path + (refresh ? (path.includes("?") ? "&" : "?") + "refresh=1" : ""))
    .then((data) => { cache[kind] = data; notify(kind); return data; })
    .finally(() => { delete pending[kind]; });
  return pending[kind];
}

export const data = {
  app: (refresh = false) => load("app", "app", refresh),
  portfolio: (refresh = false) => load("portfolio", "portfolio", refresh),
  stops: (refresh = false) => load("stops", "stops", refresh),
  cached: (kind) => cache[kind],
  invalidate(...kinds) { (kinds.length ? kinds : Object.keys(cache)).forEach((k) => { cache[k] = null; }); },
  /** Schreibende Anfrage: verwirft danach die genannten Daten; Ladevorgänge warten so lange. */
  mutate(promise, ...kinds) {
    const tracked = promise.finally(() => { data.invalidate(...kinds); mutations.delete(tracked); });
    mutations.add(tracked);
    return tracked;
  },
};

/** Startet den Sync und meldet den Fortschritt (Schritttext) bis zum Ende. */
export async function runSync(onStep) {
  let job = await api.post("jobs/sync", {});
  while (job.status === "running") {
    onStep && onStep(job.step, job);
    await new Promise((r) => setTimeout(r, 900));
    job = await api.get("jobs/" + job.id);
  }
  onStep && onStep(job.step, job);
  data.invalidate("portfolio", "app", "stops");
  return job;
}
