// Sicheres HTML-Templating: jeder eingesetzte Wert wird escaped, außer er ist selbst ein
// html``-Ergebnis (oder ein Array davon). Daten aus der API landen so nie ungeprüft im DOM.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ESC[c]);
}

class Html {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
}

function piece(value) {
  if (value === null || value === undefined || value === false) return "";
  if (value instanceof Html) return value.text;
  if (Array.isArray(value)) return value.map(piece).join("");
  return escapeHtml(value);
}

export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += piece(values[i]) + strings[i + 1];
  return new Html(out);
}

/** Bewusst unescaped – nur für intern erzeugtes Markup (z. B. SVG der Diagramme). */
export function raw(text) {
  return new Html(String(text));
}

const renderHooks = new WeakMap();

/** Ruft fn synchron beim nächsten render(el, …) auf (der Router hängt so neue Ansichten ein). */
export function onNextRender(el, fn) { renderHooks.set(el, fn); }

export function render(el, content) {
  el.innerHTML = piece(content);
  const hook = renderHooks.get(el);
  if (hook) { renderHooks.delete(el); hook(); }
  return el;
}

export function $(sel, root = document) { return root.querySelector(sel); }
export function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

/** Ereignis-Delegation: on(root, "click", "[data-action=sync]", handler) */
export function on(root, type, selector, handler, options) {
  const listener = (ev) => {
    const target = ev.target instanceof Element ? ev.target.closest(selector) : null;
    if (target && root.contains(target)) handler(ev, target);
  };
  root.addEventListener(type, listener, options);
  return () => root.removeEventListener(type, listener, options);
}

export function uid(prefix = "id") {
  uid.n = (uid.n || 0) + 1;
  return `${prefix}-${uid.n}`;
}
