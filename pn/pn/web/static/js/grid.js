// 13x13 hand-class grid with per-cell action bars, plus action colour conventions.
import { h, gridClass, RANKS } from "./core.js";

// GTO Wizard conventions: fold blue, check/call green, bets/raises red (bigger = darker), all-in deepest.
const RAISE_SHADES = ["#e8705f", "#d4483f", "#b3302f", "#922326"];
export const ACTION_COLORS = { fold: "#4a86d9", check: "#3fa66a", call: "#3fa66a", allin: "#6e1220" };

export function actionKind(token) {
  const t = token.toLowerCase();
  if (t === "fold" || t === "f") return "fold";
  if (t === "check" || t === "x") return "check";
  if (t === "call" || t === "c") return "call";
  if (t.startsWith("allin") || t === "a") return "allin";
  return "raise";
}

/** Colours for an ordered action list; aggressive actions get darker shades as size grows. */
export function actionColors(tokens) {
  const aggr = tokens.filter((t) => actionKind(t) === "raise");
  const sizes = aggr.map((t) => parseFloat(t.replace(/[^\d.]/g, "")) || 0);
  const order = [...aggr].sort((a, b) => sizes[aggr.indexOf(a)] - sizes[aggr.indexOf(b)]);
  return tokens.map((t) => {
    const k = actionKind(t);
    if (k !== "raise") return ACTION_COLORS[k];
    const rank = order.indexOf(t);
    const step = order.length <= 1 ? 1 : Math.round((rank * (RAISE_SHADES.length - 1)) / (order.length - 1));
    return RAISE_SHADES[Math.min(step, RAISE_SHADES.length - 1)];
  });
}

/**
 * CSS background for a cell: stacked bars left->right. Default fill: bars filled to `weight` of the
 * height. `fill: "full"` (GTO Wizard): bars run the full height, the coloured part is `weight` wide.
 */
export function cellBackground(segments, weight, { fill } = {}) {
  const total = segments.reduce((s, x) => s + x.freq, 0);
  if (!total || weight <= 0) return "";
  let acc = 0;
  const stops = [];
  for (const s of segments) {
    if (s.freq <= 0) continue;
    const a = (100 * acc) / total;
    acc += s.freq;
    const b = (100 * acc) / total;
    stops.push(`${s.color} ${a.toFixed(2)}% ${b.toFixed(2)}%`);
  }
  const image = `linear-gradient(to right, ${stops.join(", ")})`;
  if (fill === "full") {
    const wid = Math.min(100, 100 * weight).toFixed(1);
    return { backgroundImage: image, backgroundSize: `${wid}% 100%`, backgroundPosition: "left", backgroundRepeat: "no-repeat" };
  }
  const hgt = Math.max(4, Math.min(100, 100 * weight)).toFixed(1);
  return { backgroundImage: image, backgroundSize: `100% ${hgt}%`, backgroundPosition: "bottom", backgroundRepeat: "no-repeat" };
}

/**
 * cells: {cls: {weight: 0..1, segments: [{freq, color}], label?: string}}
 * opts.onFocus(cls, el) / opts.onBlur() for hover + keyboard focus, opts.caption for a11y,
 * opts.fill passed to cellBackground, opts.class a modifier class on the grid (e.g. "gw").
 * opts.selected: a pinned class; its cell gets .is-selected and the grid .has-pin (CSS dims the rest).
 */
export function renderGrid(cells, opts = {}) {
  const klass = ["grid13", opts.class, opts.selected ? "has-pin" : null].filter(Boolean).join(" ");
  const grid = h("div", { class: klass, role: "grid", "aria-label": opts.caption || "Hand grid" });
  const els = [];
  for (let i = 0; i < 13; i++) {
    const row = h("div", { class: "grid-row", role: "row" });
    for (let j = 0; j < 13; j++) {
      const cls = gridClass(i, j);
      const c = cells[cls];
      const style = c ? cellBackground(c.segments, c.weight, { fill: opts.fill }) : "";
      const kind = i === j ? "pair" : i < j ? "suited" : "offsuit";
      const el = h("div", {
        class: `cell ${kind}${c ? "" : " empty"}${opts.selected === cls ? " is-selected" : ""}`, role: "gridcell",
        "aria-selected": opts.selected ? String(opts.selected === cls) : null, tabindex: (opts.selected ? opts.selected === cls : i === 0 && j === 0) ? 0 : -1,
        "aria-label": c && c.label ? `${cls}: ${c.label}` : cls, dataset: { cls, i, j }, style,
      }, h("span", { class: "cell-name" }, cls), c && c.sub ? h("span", { class: "cell-sub" }, c.sub) : null);
      els.push(el);
      row.append(el);
    }
    grid.append(row);
  }
  const at = (i, j) => els[i * 13 + j];
  grid.addEventListener("keydown", (e) => {
    const el = e.target.closest(".cell");
    if (!el) return;
    let i = +el.dataset.i, j = +el.dataset.j;
    const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (!moves[e.key]) return;
    e.preventDefault();
    i = Math.max(0, Math.min(12, i + moves[e.key][0]));
    j = Math.max(0, Math.min(12, j + moves[e.key][1]));
    el.tabIndex = -1;
    const next = at(i, j);
    next.tabIndex = 0;
    next.focus();
  });
  if (opts.onFocus) {
    const enter = (e) => { const el = e.target.closest(".cell"); if (el) opts.onFocus(el.dataset.cls, el); };
    grid.addEventListener("mouseover", enter);
    grid.addEventListener("focusin", enter);
    const leave = () => opts.onBlur && opts.onBlur();
    grid.addEventListener("mouseleave", leave);
    grid.addEventListener("focusout", leave);
  }
  return grid;
}

/** Single-colour range grid from {cls: weight}. */
export function rangeGrid(weights, color = "#d4483f", opts = {}) {
  const cells = {};
  for (const [cls, w] of Object.entries(weights || {})) {
    if (w > 0) cells[cls] = { weight: 1, segments: [{ freq: w, color }, { freq: 1 - w, color: "transparent" }],
                             label: `${Math.round(100 * w)}%` };
  }
  return renderGrid(cells, opts);
}

export function legend(tokens, colors, values) {
  return h("ul", { class: "legend" }, tokens.map((t, k) =>
    h("li", {}, h("span", { class: "swatch", style: { background: colors[k] } }), t,
      values ? h("b", {}, values[k]) : null)));
}

export const ALL_CLASSES = (() => {
  const out = [];
  for (let i = 0; i < 13; i++) for (let j = 0; j < 13; j++) out.push(gridClass(i, j));
  return out;
})();

/** Hand class of a combo string like "AhKd" (higher rank first or not). */
export function comboClass(c) {
  const r1 = c[0], r2 = c[2];
  if (r1 === r2) return r1 + r2;
  const [hi, lo] = RANKS.indexOf(r1) < RANKS.indexOf(r2) ? [r1, r2] : [r2, r1];
  return hi + lo + (c[1] === c[3] ? "s" : "o");
}
