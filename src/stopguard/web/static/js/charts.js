// SVG-Diagramme im Amboss-Stil, nach den Dataviz-Regeln:
// dünne Marken (Linie 2 px, Balken ≤ 24 px, 4 px gerundete Datenenden), Haarlinien-Raster,
// 2 px Flächenabstand zwischen Segmenten, 2 px Flächenring um Punkte, Legende ab zwei Reihen,
// Fadenkreuz-Tooltip (Maus und Tastatur) und zu jedem Diagramm eine Tabellenansicht.
// Farben kommen ausschließlich aus CSS-Klassen (c1, c2, c3, deemph) – siehe app.css.

import { escapeHtml, html, raw } from "./dom.js";
import * as fmt from "./fmt.js";

const NS = "http://www.w3.org/2000/svg";
const BAR_MAX = 24;
const R = 4;

// ---------------------------------------------------------------- Hilfen

function niceStep(span, count) {
  const step0 = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(step0));
  const err = step0 / mag;
  return (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
}

export function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { lo: 0, hi: 1, ticks: [0, 1] };
  if (min === max) { min -= Math.abs(min) * 0.1 || 1; max += Math.abs(max) * 0.1 || 1; }
  const step = niceStep(max - min, count);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
  return { lo, hi, ticks };
}

function toTime(d) { return typeof d === "number" ? d : new Date(d.length === 10 ? d + "T12:00:00Z" : d).getTime(); }

function timeTicks(t0, t1, maxLabels) {
  const span = (t1 - t0) / 86400000;
  const out = [];
  if (span > 3 * 365) {
    const y0 = new Date(t0).getUTCFullYear() + 1;
    const y1 = new Date(t1).getUTCFullYear();
    const every = Math.max(1, Math.ceil((y1 - y0 + 1) / maxLabels));
    for (let y = y0; y <= y1; y += every) out.push({ t: Date.UTC(y, 0, 1), label: String(y) });
    return out;
  }
  const d = new Date(t0);
  const months = [];
  for (let k = 1; ; k++) {
    const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + k, 1); // Monatsüberlauf regelt Date.UTC
    if (t > t1) break;
    months.push(t);
  }
  const every = Math.max(1, Math.ceil(months.length / maxLabels));
  const fmtM = new Intl.DateTimeFormat("de-DE", { month: "short", year: "2-digit", timeZone: "UTC" });
  months.forEach((t, i) => { if (i % every === 0) out.push({ t, label: fmtM.format(t) }); });
  if (!out.length) out.push({ t: t0, label: fmt.dateShort(new Date(t0).toISOString()) });
  return out;
}

function estWidth(text, px = 12) { return String(text).length * px * 0.58; }

function roundedBar(x, y0, y1, w, r = R) {
  // Datenende gerundet, Basis eckig. y0 = Basislinie, y1 = Datenende.
  const h = Math.abs(y1 - y0);
  if (h < 0.5) return "";
  const rr = Math.min(r, h, w / 2);
  if (y1 < y0) {
    return `M${x},${y0}V${y1 + rr}Q${x},${y1} ${x + rr},${y1}H${x + w - rr}Q${x + w},${y1} ${x + w},${y1 + rr}V${y0}Z`;
  }
  return `M${x},${y0}V${y1 - rr}Q${x},${y1} ${x + rr},${y1}H${x + w - rr}Q${x + w},${y1} ${x + w},${y1 - rr}V${y0}Z`;
}

function rect(x, y, w, h) { return `M${x},${y}h${w}v${h}h${-w}Z`; }

// ---------------------------------------------------------------- Tooltip

let tooltipEl = null;
function tooltip() {
  if (!tooltipEl) {
    tooltipEl = document.createElement("div");
    tooltipEl.className = "chart-tooltip";
    tooltipEl.setAttribute("role", "tooltip");
    tooltipEl.hidden = true;
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}

function showTooltip(title, rows, clientX, clientY) {
  const tip = tooltip();
  tip.replaceChildren();
  const head = document.createElement("div");
  head.className = "chart-tooltip-title";
  head.textContent = title;
  tip.appendChild(head);
  for (const row of rows) {
    const line = document.createElement("div");
    line.className = "chart-tooltip-row";
    if (row.cls) {
      const key = document.createElement("span");
      key.className = `key ${row.shape === "rect" ? "rect" : "line"} ${row.cls}`;
      line.appendChild(key);
    }
    const value = document.createElement("strong");
    value.textContent = row.value;
    line.appendChild(value);
    if (row.name) {
      const name = document.createElement("span");
      name.className = "muted";
      name.textContent = row.name;
      line.appendChild(name);
    }
    tip.appendChild(line);
  }
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  let x = clientX + 14;
  let y = clientY - box.height - 10;
  if (x + box.width > window.innerWidth - 8) x = clientX - box.width - 14;
  if (y < 8) y = clientY + 16;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}

function hideTooltip() { if (tooltipEl) tooltipEl.hidden = true; }

// ---------------------------------------------------------------- Rahmen

function legendHtml(series, shape) {
  if (series.length < 2) return "";
  return html`<ul class="chart-legend" aria-label="Legende">${series.map(
    (s) => html`<li><span class="key ${shape} ${s.cls}" aria-hidden="true"></span>${s.name}</li>`,
  )}</ul>`;
}

function tableHtml(spec) {
  const t = spec.table || autoTable(spec);
  return html`<div class="table-scroll"><table class="table">
    <thead><tr>${t.columns.map((c, i) => html`<th class="${i ? "num" : ""}" scope="col">${c}</th>`)}</tr></thead>
    <tbody>${t.rows.map((r) => html`<tr>${r.map((v, i) => (i ? html`<td class="num">${v}</td>` : html`<th scope="row">${v}</th>`))}</tr>`)}</tbody>
  </table></div>`;
}

function autoTable(spec) {
  const yf = (spec.y && spec.y.format) || ((v) => fmt.num(v, 2));
  if (spec.type === "line") {
    const base = spec.series[0].points;
    const step = Math.max(1, Math.ceil(base.length / 60));
    const rows = [];
    for (let i = base.length - 1; i >= 0; i -= step) {
      const t = toTime(base[i][0]);
      rows.push([fmt.date(base[i][0]), ...spec.series.map((s) => { const p = valueAt(s.points, t); return p === null ? fmt.DASH : yf(p); })]);
    }
    return { columns: ["Datum", ...spec.series.map((s) => s.name)], rows };
  }
  if (spec.type === "bar") {
    const xf = spec.xFormat || ((c) => c);
    return {
      columns: [spec.categoryLabel || "Zeitraum", ...spec.series.map((s) => s.name)],
      rows: spec.categories.map((c, i) => [xf(c), ...spec.series.map((s) => yf(s.values[i] || 0))]).reverse(),
    };
  }
  if (spec.type === "scatter") {
    const xf = spec.x.format;
    return {
      columns: ["Trade", spec.x.label, spec.y.label, "Gruppe"],
      rows: spec.series.flatMap((s) => s.points.map((p) => [p.label, xf(p.x), yf(p.y), s.name])),
    };
  }
  return { columns: ["", ""], rows: [] };
}

function valueAt(points, t) {
  let lo = 0;
  let hi = points.length - 1;
  if (hi < 0 || toTime(points[0][0]) > t) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (toTime(points[mid][0]) <= t) lo = mid; else hi = mid - 1;
  }
  return points[lo][1];
}

/**
 * Bindet ein Diagramm in ein <figure class="chart">-Element ein.
 * Gibt eine Funktion zum Aufräumen zurück.
 */
export function mountChart(figure, spec) {
  const shape = spec.type === "line" ? "line" : spec.type === "scatter" ? "dot" : "rect";
  const series = spec.series || [];
  figure.classList.add("chart");
  figure.innerHTML = html`
    <div class="chart-head">
      ${legendHtml(series, shape)}
      <button type="button" class="btn tiny chart-toggle" aria-pressed="false">Als Tabelle</button>
    </div>
    <div class="chart-plot" tabindex="0" role="img" aria-label="${spec.label || ""}"></div>
    <div class="chart-table" hidden>${tableHtml(spec)}</div>
    ${spec.caption ? html`<figcaption class="muted small">${spec.caption}</figcaption>` : ""}
  `.text;
  const plot = figure.querySelector(".chart-plot");
  const toggle = figure.querySelector(".chart-toggle");
  const tableBox = figure.querySelector(".chart-table");
  toggle.addEventListener("click", () => {
    const asTable = toggle.getAttribute("aria-pressed") !== "true";
    toggle.setAttribute("aria-pressed", String(asTable));
    toggle.textContent = asTable ? "Als Diagramm" : "Als Tabelle";
    tableBox.hidden = !asTable;
    plot.hidden = asTable;
  });

  let width = 0;
  let cleanup = () => {};
  const draw = () => {
    const w = Math.max(240, Math.floor(plot.clientWidth || figure.clientWidth || 600));
    if (w === width) return;
    width = w;
    cleanup();
    const renderer = { line: drawLine, bar: drawBar, scatter: drawScatter }[spec.type];
    cleanup = renderer ? renderer(plot, spec, w) || (() => {}) : () => {};
  };
  const ro = new ResizeObserver(() => draw());
  ro.observe(figure);
  draw();
  return () => { ro.disconnect(); cleanup(); hideTooltip(); };
}

function svgEl(w, h) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  return svg;
}

function interact(plot, { onMove, onKey, onLeave, onClick }) {
  const move = (ev) => onMove(ev);
  const leave = () => { hideTooltip(); onLeave && onLeave(); };
  const key = (ev) => onKey && onKey(ev);
  const click = (ev) => onClick && onClick(ev);
  plot.addEventListener("pointermove", move);
  plot.addEventListener("pointerleave", leave);
  plot.addEventListener("keydown", key);
  plot.addEventListener("blur", leave);
  plot.addEventListener("click", click);
  return () => {
    plot.removeEventListener("pointermove", move);
    plot.removeEventListener("pointerleave", leave);
    plot.removeEventListener("keydown", key);
    plot.removeEventListener("blur", leave);
    plot.removeEventListener("click", click);
  };
}

// ---------------------------------------------------------------- Linien

function drawLine(plot, spec, W) {
  const H = spec.height || 240;
  const yf = (spec.y && spec.y.format) || ((v) => fmt.num(v));
  const axisF = (spec.y && spec.y.axis) || yf;
  const all = spec.series.flatMap((s) => s.points);
  if (!all.length) { plot.innerHTML = '<p class="muted small chart-empty">Für diesen Zeitraum liegen keine Werte vor.</p>'; return null; }
  const t0 = Math.min(...all.map((p) => toTime(p[0])));
  const t1 = Math.max(...all.map((p) => toTime(p[0])));
  let vmin = Math.min(...all.map((p) => p[1]));
  let vmax = Math.max(...all.map((p) => p[1]));
  if (spec.y && spec.y.zero) { vmin = Math.min(0, vmin); vmax = Math.max(0, vmax); }
  const { lo, hi, ticks } = niceTicks(vmin, vmax, H < 200 ? 3 : 4);
  const left = Math.max(...ticks.map((t) => estWidth(axisF(t)))) + 10;
  const M = { t: 18, r: 10, b: 26, l: left };
  const pw = W - M.l - M.r;
  const ph = H - M.t - M.b;
  const x = (t) => M.l + (t1 === t0 ? pw / 2 : ((t - t0) / (t1 - t0)) * pw);
  const y = (v) => M.t + ph - ((v - lo) / (hi - lo || 1)) * ph;
  const svg = svgEl(W, H);
  let out = "";
  for (const t of ticks) {
    out += `<line class="${t === 0 && lo < 0 ? "axis" : "grid"}" x1="${M.l}" x2="${W - M.r}" y1="${y(t)}" y2="${y(t)}"/>`;
    out += `<text class="tick" x="${M.l - 8}" y="${y(t) + 4}" text-anchor="end">${escapeHtml(axisF(t))}</text>`;
  }
  out += `<line class="axis" x1="${M.l}" x2="${W - M.r}" y1="${M.t + ph}" y2="${M.t + ph}"/>`;
  for (const tk of timeTicks(t0, t1, Math.max(2, Math.floor(pw / 80)))) {
    const xx = x(tk.t);
    if (xx < M.l - 1 || xx > W - M.r + 1) continue;
    out += `<text class="tick" x="${xx}" y="${H - 8}" text-anchor="middle">${escapeHtml(tk.label)}</text>`;
  }
  const base = y(Math.max(lo, Math.min(0, hi)));
  spec.series.forEach((s) => {
    if (!s.points.length) return;
    let d = "";
    s.points.forEach((p, i) => {
      const px = x(toTime(p[0]));
      const py = y(p[1]);
      if (i === 0) d += `M${px},${py}`;
      else if (s.step) d += `H${px}V${py}`;
      else d += `L${px},${py}`;
    });
    if (s.area) {
      const first = x(toTime(s.points[0][0]));
      const last = x(toTime(s.points[s.points.length - 1][0]));
      out += `<path class="mark-area ${s.cls}" d="${d}V${base}H${first}Z"/>`;
    }
    out += `<path class="mark-line ${s.cls}" d="${d}"/>`;
  });
  // Direktbeschriftung: nur der Endwert der ersten Reihe
  const s0 = spec.series[0];
  if (s0 && s0.points.length && spec.endLabel !== false) {
    const lp = s0.points[s0.points.length - 1];
    const lx = x(toTime(lp[0]));
    const ly = y(lp[1]);
    out += `<circle class="mark-dot ${s0.cls}" cx="${lx}" cy="${ly}" r="4"/>`;
    out += `<text class="end-label" x="${lx - 6}" y="${Math.max(12, ly - 9)}" text-anchor="end">${escapeHtml(yf(lp[1]))}</text>`;
  }
  out += `<g class="hover" visibility="hidden"><line class="crosshair" y1="${M.t}" y2="${M.t + ph}"/>${spec.series
    .map((s, i) => `<circle class="mark-dot ${s.cls}" data-i="${i}" r="4"/>`)
    .join("")}</g>`;
  svg.innerHTML = out;
  plot.replaceChildren(svg);
  const hover = svg.querySelector(".hover");
  const masterPts = s0.points;
  let idx = masterPts.length - 1;
  const show = (i, clientX, clientY) => {
    idx = Math.max(0, Math.min(masterPts.length - 1, i));
    const t = toTime(masterPts[idx][0]);
    const xx = x(t);
    hover.setAttribute("visibility", "visible");
    const line = hover.querySelector("line");
    line.setAttribute("x1", xx);
    line.setAttribute("x2", xx);
    const rows = [];
    spec.series.forEach((s, si) => {
      const v = valueAt(s.points, t);
      const dot = hover.querySelector(`circle[data-i="${si}"]`);
      if (v === null) { dot.setAttribute("visibility", "hidden"); return; }
      dot.setAttribute("visibility", "visible");
      dot.setAttribute("cx", xx);
      dot.setAttribute("cy", y(v));
      rows.push({ cls: s.cls, value: yf(v), name: spec.series.length > 1 ? s.name : "" });
    });
    const box = plot.getBoundingClientRect();
    const sx = box.left + (xx / W) * box.width;
    showTooltip(fmt.date(masterPts[idx][0]), rows, clientX ?? sx, clientY ?? box.top + M.t + 20);
  };
  return interact(plot, {
    onMove: (ev) => {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      const t = t0 + ((px - M.l) / pw) * (t1 - t0);
      let best = 0;
      let bestD = Infinity;
      masterPts.forEach((p, i) => { const dd = Math.abs(toTime(p[0]) - t); if (dd < bestD) { bestD = dd; best = i; } });
      show(best, ev.clientX, ev.clientY);
    },
    onKey: (ev) => {
      const step = ev.shiftKey ? 10 : 1;
      if (ev.key === "ArrowLeft") { show(idx - step); ev.preventDefault(); }
      else if (ev.key === "ArrowRight") { show(idx + step); ev.preventDefault(); }
      else if (ev.key === "Home") { show(0); ev.preventDefault(); }
      else if (ev.key === "End") { show(masterPts.length - 1); ev.preventDefault(); }
      else if (ev.key === "Escape") { hideTooltip(); hover.setAttribute("visibility", "hidden"); }
    },
    onLeave: () => hover.setAttribute("visibility", "hidden"),
  });
}

// ---------------------------------------------------------------- Säulen

function drawBar(plot, spec, W) {
  const H = spec.height || 220;
  const yf = (spec.y && spec.y.format) || ((v) => fmt.num(v));
  const axisF = (spec.y && spec.y.axis) || yf;
  const xf = spec.xFormat || ((c) => c);
  const n = spec.categories.length;
  if (!n) { plot.innerHTML = '<p class="muted small chart-empty">Keine Werte vorhanden.</p>'; return null; }
  const stacked = !!spec.stacked;
  let vmin = 0;
  let vmax = 0;
  for (let i = 0; i < n; i++) {
    if (stacked) {
      let pos = 0;
      let neg = 0;
      spec.series.forEach((s) => { const v = s.values[i] || 0; if (v >= 0) pos += v; else neg += v; });
      vmax = Math.max(vmax, pos);
      vmin = Math.min(vmin, neg);
    } else {
      spec.series.forEach((s) => { const v = s.values[i] || 0; vmax = Math.max(vmax, v); vmin = Math.min(vmin, v); });
    }
  }
  const { lo, hi, ticks } = niceTicks(vmin, vmax, 4);
  const left = Math.max(...ticks.map((t) => estWidth(axisF(t)))) + 10;
  const M = { t: 12, r: 8, b: 26, l: left };
  const pw = W - M.l - M.r;
  const ph = H - M.t - M.b;
  const y = (v) => M.t + ph - ((v - lo) / (hi - lo || 1)) * ph;
  const band = pw / n;
  const groups = stacked ? 1 : spec.series.length;
  const barW = Math.max(2, Math.min(BAR_MAX, (band * 0.7 - (groups - 1) * 2) / groups));
  const groupW = barW * groups + (groups - 1) * 2;
  const svg = svgEl(W, H);
  let out = "";
  for (const t of ticks) {
    out += `<line class="${t === 0 ? "axis" : "grid"}" x1="${M.l}" x2="${W - M.r}" y1="${y(t)}" y2="${y(t)}"/>`;
    out += `<text class="tick" x="${M.l - 8}" y="${y(t) + 4}" text-anchor="end">${escapeHtml(axisF(t))}</text>`;
  }
  const maxLabels = Math.max(1, Math.floor(pw / 52));
  const every = Math.max(1, Math.ceil(n / maxLabels));
  const y0 = y(0);
  for (let i = 0; i < n; i++) {
    const gx = M.l + band * i + (band - groupW) / 2;
    if (stacked) {
      let posTop = 0;
      let negTop = 0;
      const segs = spec.series.map((s) => s.values[i] || 0);
      const lastPos = segs.map((v, k) => (v > 0 ? k : -1)).filter((k) => k >= 0).pop();
      const lastNeg = segs.map((v, k) => (v < 0 ? k : -1)).filter((k) => k >= 0).pop();
      spec.series.forEach((s, k) => {
        const v = segs[k];
        if (!v) return;
        const from = v > 0 ? posTop : negTop;
        const to = from + v;
        if (v > 0) posTop = to; else negTop = to;
        const ya = y(from);
        const yb = y(to);
        const gap = from === 0 ? 0 : 2; // 2-px-Flächenabstand zum Segment darunter
        const yStart = v > 0 ? ya - gap : ya + gap;
        const isEnd = v > 0 ? k === lastPos : k === lastNeg;
        const d = isEnd ? roundedBar(gx, yStart, yb, barW) : rect(gx, Math.min(yStart, yb), barW, Math.abs(yb - yStart));
        if (d && Math.abs(yb - yStart) > 0.5) out += `<path class="mark-bar ${s.cls}" data-i="${i}" d="${d}"/>`;
      });
    } else {
      spec.series.forEach((s, k) => {
        const v = s.values[i] || 0;
        const d = roundedBar(gx + k * (barW + 2), y0, y(v), barW);
        if (d) out += `<path class="mark-bar ${s.cls}" data-i="${i}" d="${d}"/>`;
      });
    }
    if (i % every === 0) {
      out += `<text class="tick" x="${M.l + band * i + band / 2}" y="${H - 8}" text-anchor="middle">${escapeHtml(xf(spec.categories[i], true))}</text>`;
    }
  }
  out += `<rect class="band-hl" visibility="hidden" y="${M.t}" height="${ph}" width="${band}"/>`;
  svg.innerHTML = out;
  plot.replaceChildren(svg);
  const hl = svg.querySelector(".band-hl");
  let idx = n - 1;
  const show = (i, cx, cy) => {
    idx = Math.max(0, Math.min(n - 1, i));
    hl.setAttribute("visibility", "visible");
    hl.setAttribute("x", M.l + band * idx);
    svg.querySelectorAll(".mark-bar").forEach((b) => b.classList.toggle("dim", Number(b.dataset.i) !== idx));
    const rows = spec.series.map((s) => ({ cls: s.cls, shape: "rect", value: yf(s.values[idx] || 0), name: spec.series.length > 1 ? s.name : "" }));
    if (stacked && spec.series.length > 1) {
      rows.push({ value: yf(spec.series.reduce((a, s) => a + (s.values[idx] || 0), 0)), name: "Summe" });
    }
    const box = plot.getBoundingClientRect();
    const sx = box.left + ((M.l + band * idx + band / 2) / W) * box.width;
    showTooltip(xf(spec.categories[idx], false), rows, cx ?? sx, cy ?? box.top + M.t + 20);
  };
  const reset = () => {
    hl.setAttribute("visibility", "hidden");
    svg.querySelectorAll(".mark-bar.dim").forEach((b) => b.classList.remove("dim"));
  };
  return interact(plot, {
    onMove: (ev) => {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      if (px < M.l || px > W - M.r) { hideTooltip(); reset(); return; }
      show(Math.floor((px - M.l) / band), ev.clientX, ev.clientY);
    },
    onKey: (ev) => {
      if (ev.key === "ArrowLeft") { show(idx - 1); ev.preventDefault(); }
      else if (ev.key === "ArrowRight") { show(idx + 1); ev.preventDefault(); }
      else if (ev.key === "Home") { show(0); ev.preventDefault(); }
      else if (ev.key === "End") { show(n - 1); ev.preventDefault(); }
      else if (ev.key === "Escape") { hideTooltip(); reset(); }
    },
    onLeave: reset,
  });
}

// ---------------------------------------------------------------- Streudiagramm

function drawScatter(plot, spec, W) {
  const H = spec.height || 300;
  const xf = spec.x.format;
  const yf = spec.y.format;
  const pts = spec.series.flatMap((s, si) => s.points.map((p) => ({ ...p, si, cls: s.cls, group: s.name })));
  if (!pts.length) { plot.innerHTML = '<p class="muted small chart-empty">Keine Trades für diese Auswahl.</p>'; return null; }
  const clamp = spec.y.clamp;
  const ys = pts.map((p) => (clamp ? Math.max(clamp[0], Math.min(clamp[1], p.y)) : p.y));
  const yT = niceTicks(Math.min(0, ...ys), Math.max(0, ...ys), 4);
  const xT = niceTicks(0, Math.max(...pts.map((p) => p.x)), Math.max(2, Math.floor(W / 120)));
  const left = Math.max(...yT.ticks.map((t) => estWidth(yf(t)))) + 10;
  const M = { t: 12, r: 14, b: 40, l: left };
  const pw = W - M.l - M.r;
  const ph = H - M.t - M.b;
  const x = (v) => M.l + ((v - xT.lo) / (xT.hi - xT.lo || 1)) * pw;
  const y = (v) => M.t + ph - ((v - yT.lo) / (yT.hi - yT.lo || 1)) * ph;
  const svg = svgEl(W, H);
  let out = "";
  for (const t of yT.ticks) {
    out += `<line class="${t === 0 ? "axis" : "grid"}" x1="${M.l}" x2="${W - M.r}" y1="${y(t)}" y2="${y(t)}"/>`;
    out += `<text class="tick" x="${M.l - 8}" y="${y(t) + 4}" text-anchor="end">${escapeHtml(yf(t))}</text>`;
  }
  for (const t of xT.ticks) {
    out += `<text class="tick" x="${x(t)}" y="${M.t + ph + 16}" text-anchor="middle">${escapeHtml(xf(t))}</text>`;
  }
  out += `<line class="axis" x1="${M.l}" x2="${W - M.r}" y1="${M.t + ph}" y2="${M.t + ph}"/>`;
  out += `<text class="tick axis-title" x="${M.l + pw / 2}" y="${H - 4}" text-anchor="middle">${escapeHtml(spec.x.label)}</text>`;
  pts.forEach((p, i) => {
    const vy = clamp ? Math.max(clamp[0], Math.min(clamp[1], p.y)) : p.y;
    p.px = x(p.x);
    p.py = y(vy);
    p.clamped = vy !== p.y;
    out += `<circle class="mark-dot ${p.cls}${p.clamped ? " clamped" : ""}" data-i="${i}" cx="${p.px}" cy="${p.py}" r="5"/>`;
  });
  out += `<circle class="focus-ring" visibility="hidden" r="9"/>`;
  svg.innerHTML = out;
  plot.replaceChildren(svg);
  const ring = svg.querySelector(".focus-ring");
  const order = pts.map((p, i) => i).sort((a, b) => pts[a].x - pts[b].x);
  let cur = -1;
  const show = (i, cx, cy) => {
    cur = i;
    const p = pts[i];
    ring.setAttribute("visibility", "visible");
    ring.setAttribute("cx", p.px);
    ring.setAttribute("cy", p.py);
    const rows = (spec.tooltip ? spec.tooltip(p) : [{ value: yf(p.y), name: spec.y.label }]).map((r) => ({ ...r }));
    rows.unshift({ cls: p.cls, shape: "line", value: p.group, name: "" });
    const box = plot.getBoundingClientRect();
    showTooltip(p.label, rows, cx ?? box.left + (p.px / W) * box.width, cy ?? box.top + (p.py / H) * box.height);
    plot.style.cursor = spec.onClick ? "pointer" : "";
  };
  const nearest = (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const py = ((ev.clientY - box.top) / box.height) * H;
    let best = -1;
    let bd = 24 * 24; // Trefferfläche ≥ 24 px
    pts.forEach((p, i) => { const d = (p.px - px) ** 2 + (p.py - py) ** 2; if (d < bd) { bd = d; best = i; } });
    return best;
  };
  return interact(plot, {
    onMove: (ev) => {
      const i = nearest(ev);
      if (i < 0) { hideTooltip(); ring.setAttribute("visibility", "hidden"); plot.style.cursor = ""; cur = -1; return; }
      show(i, ev.clientX, ev.clientY);
    },
    onKey: (ev) => {
      const pos = order.indexOf(cur);
      if (ev.key === "ArrowRight") { show(order[Math.min(order.length - 1, pos + 1)]); ev.preventDefault(); }
      else if (ev.key === "ArrowLeft") { show(order[Math.max(0, pos - 1)]); ev.preventDefault(); }
      else if (ev.key === "Enter" && cur >= 0 && spec.onClick) { spec.onClick(pts[cur]); }
      else if (ev.key === "Escape") { hideTooltip(); ring.setAttribute("visibility", "hidden"); }
    },
    onLeave: () => ring.setAttribute("visibility", "hidden"),
    onClick: (ev) => { const i = nearest(ev); if (i >= 0 && spec.onClick) spec.onClick(pts[i]); },
  });
}

// ---------------------------------------------------------------- HTML-Diagramme

/** Rangliste als waagerechte Balken (eine Farbe), Wert am Balkenende. */
export function rankedBars(items, { format = (v) => fmt.eur(v, 0), cls = "c1", max } = {}) {
  const top = max || Math.max(...items.map((i) => Math.abs(i.value)), 1);
  return html`<ul class="hbars">${items.map(
    (it) => html`<li class="hbar-row">
      <span class="hbar-label">${it.href ? html`<a href="${it.href}">${it.label}</a>` : it.label}</span>
      <span class="hbar-track"><span class="hbar-fill ${it.cls || cls}" style="width:calc((100% - 120px) * ${Math.max(0.005, Math.abs(it.value) / top).toFixed(4)})"></span><span class="hbar-value">${format(it.value)}</span></span>
    </li>`,
  )}</ul>`;
}

/** Dünner Segmentbalken (ein Ganzes, ≤ 4 Teile) mit Legende, die Namen und Werte trägt. */
export function segmentBar(items, { format = (v) => fmt.eur(v, 0), label = "Aufteilung" } = {}) {
  const total = items.reduce((a, i) => a + Math.max(0, i.value), 0) || 1;
  const desc = items.map((i) => `${i.label} ${fmt.pct(i.value / total, 0)}`).join(", ");
  return html`<div class="segbar" role="img" aria-label="${label}: ${desc}">${items
    .filter((i) => i.value > 0)
    .map((i) => html`<span class="seg ${i.cls}" style="flex-grow:${(i.value / total).toFixed(5)}" title="${i.label}: ${fmt.pct(i.value / total, 1)}"></span>`)}</div>
  <ul class="seg-legend">${items.map(
    (i) => html`<li><span class="key rect ${i.cls}" aria-hidden="true"></span><span>${i.label}</span><b>${fmt.pct(i.value / total, 1)}</b><span class="muted">${format(i.value)}</span></li>`,
  )}</ul>`;
}

/** Messbalken mit benannter Skala (Amboss .meter): ab 75 % warn, ab 90 % danger. */
export function meter(ratio) {
  const r = Math.max(0, Math.min(1, ratio || 0));
  const cls = r >= 0.9 ? "danger" : r >= 0.75 ? "warn" : "";
  return html`<div class="meter" role="presentation"><span class="${cls}" style="width:${(r * 100).toFixed(1)}%"></span></div>`;
}

export { raw };
