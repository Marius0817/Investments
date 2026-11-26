// Deutsche Formatierung für Beträge, Prozente und Datumsangaben.

const LOCALE = "de-DE";
const cache = new Map();
function nf(opts) {
  const key = JSON.stringify(opts);
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(LOCALE, opts));
  return cache.get(key);
}

export const DASH = "–";

export function eur(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  if (Math.abs(v) < 0.5 * 10 ** -digits) v = 0; // keine „−0 €“
  return nf({ style: "currency", currency: "EUR", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v).replace("-", "−");
}

/** Kompakte Beträge für Achsen und Kacheln: 12.900 € → „12,9 Tsd. €“ nur ab 100.000. */
export function eurShort(v) {
  if (v === null || v === undefined) return DASH;
  const a = Math.abs(v);
  if (a >= 1e6) return nf({ maximumFractionDigits: 1 }).format(v / 1e6) + " Mio. €";
  if (a >= 1e5) return nf({ maximumFractionDigits: 0 }).format(v / 1e3) + " Tsd. €";
  return eur(v, 0);
}

export function num(v, digits = 0) {
  if (v === null || v === undefined) return DASH;
  return nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
}

export function units(v) {
  if (v === null || v === undefined) return DASH;
  return nf({ maximumFractionDigits: 6 }).format(v);
}

/** Dezimalbruch → Prozent. sign: Vorzeichen auch bei positiven Werten. */
export function pct(v, digits = 1, sign = false) {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  const text = nf({ style: "percent", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
  return sign && v > 0 ? "+" + text : text.replace("-", "−");
}

export function signedEur(v, digits = 2) {
  if (v === null || v === undefined) return DASH;
  const text = eur(Math.abs(v), digits);
  if (v > 0.004) return "+" + text;
  if (v < -0.004) return "−" + text;
  return text;
}

/** Vorzeichen-Klasse für Gewinne/Verluste (Farbe ist nie das einzige Signal – das Vorzeichen steht dabei). */
export function tone(v) {
  if (v === null || v === undefined) return "";
  return v > 0.00005 ? "pos" : v < -0.00005 ? "neg" : "";
}

function toDate(s) {
  if (!s) return null;
  if (s instanceof Date) return s;
  const d = new Date(s.length === 10 ? s + "T12:00:00Z" : s);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function date(s) {
  const d = toDate(s);
  return d ? d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "numeric" }) : DASH;
}

export function dateShort(s) {
  const d = toDate(s);
  return d ? d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" }) : DASH;
}

export function monthLabel(ym) {
  const d = toDate(ym.length === 7 ? ym + "-15" : ym);
  return d ? d.toLocaleDateString(LOCALE, { month: "short", year: "2-digit" }) : ym;
}

export function dateTime(s) {
  const d = toDate(s);
  return d ? d.toLocaleString(LOCALE, { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : DASH;
}

export function time(s) {
  const d = toDate(s);
  return d ? d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : DASH;
}

export function relTime(s) {
  const d = toDate(s);
  if (!d) return DASH;
  const sec = (Date.now() - d.getTime()) / 1000;
  if (sec < 60) return "gerade eben";
  if (sec < 3600) return `vor ${Math.round(sec / 60)} Minuten`;
  if (sec < 86400) { const h = Math.round(sec / 3600); return `vor ${h} Stunde${h === 1 ? "" : "n"}`; }
  const days = Math.round(sec / 86400);
  if (days < 30) return `vor ${days} Tag${days === 1 ? "" : "en"}`;
  return `am ${date(s)}`;
}

export function days(v) {
  if (v === null || v === undefined) return DASH;
  const n = Math.round(v);
  if (n < 60) return `${n} Tag${n === 1 ? "" : "e"}`;
  if (n < 730) return `${Math.round(n / 30.4)} Monate`;
  return `${num(n / 365, 1)} Jahre`;
}

export const KIND_LABELS = {
  stock: "Aktie",
  equity_fund: "Aktienfonds/ETF",
  mixed_fund: "Mischfonds",
  real_estate_fund: "Immobilienfonds",
  foreign_real_estate_fund: "Immobilienfonds (Ausland)",
  bond_fund: "Rentenfonds",
  other: "Sonstige",
};

export const ORIGIN_LABELS = { single: "Einzelkauf", savings_plan: "Sparplan", transfer: "Depotübertrag", import: "Import" };
export const STATUS_LABELS = { open: "offen", partial: "teilverkauft", closed: "geschlossen" };

export const CASHFLOW_LABELS = {
  buy: "Kauf",
  sell: "Verkauf",
  dividend: "Ausschüttung",
  valuation: "Wert heute",
  tax: "Steuer Verkauf",
  dividend_tax: "Steuer Ausschüttung",
  vap_tax: "Vorabpauschale",
  latent_tax: "Steuer bei Verkauf heute",
  loan_draw: "Kredit aufgenommen",
  loan_repayment: "Kredit getilgt",
  interest: "Kreditzinsen",
  interest_tax_saving: "Steuerersparnis Zinsen",
  loan_outstanding: "Kredit offen",
};
