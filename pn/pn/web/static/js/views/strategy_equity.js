// Range equity panel of the tree browser (right column): both players' hand-vs-range equity at the node
// from GET /api/solves/{name}/equity (figures, split bar, distribution chart with hover readout and
// filter dots, equity buckets that filter the grid). Weights are the node reach; blocked combos excluded.
import { h, pct, num } from "../core.js";
import { cardText } from "../deck.js";
import { shareRow, pairHead } from "./strategy_classes.js";

export const BUCKETS = [
  { key: "75-100", lo: 0.75, label: "75–100%" }, { key: "50-75", lo: 0.5, label: "50–75%" },
  { key: "25-50", lo: 0.25, label: "25–50%" }, { key: "0-25", lo: 0, label: "0–25%" },
];
const bucketOf = (key) => BUCKETS.find((b) => b.key === key) || null;
export const isBucket = (key) => !!bucketOf(key);
export const bucketLabel = (key) => (bucketOf(key) || {}).label || key;
/** Equity e falls in bucket `key` (upper edge inclusive only for the top bucket). */
export function inBucket(e, key) {
  const b = bucketOf(key);
  if (!b || e === null || e === undefined) return false;
  const hi = b.lo === 0.75 ? Infinity : b.lo + 0.25;
  return e >= b.lo && e < hi;
}

const W = 300, H = 120, PAD = 4, MAX_DOTS = 60, SAMPLES = 100;
const yOf = (e) => PAD + (1 - e) * (H - 2 * PAD);

/** Valid equity payload for these combos (2 players, per_combo aligned), else null. */
export function checkEquity(eq, n) {
  const ok = eq && Array.isArray(eq.players) && eq.players.length === 2
    && eq.players.every((p) => Array.isArray(p.per_combo) && p.per_combo.length === n);
  return ok ? eq : null;
}

const curves = new WeakMap(); // eq payload -> per-player curve (reach is fixed per node)
/** {items: [{i, e, q}] weakest first, pts, samples[0..100], tot} per player; q = reach-weighted percentile. */
function curveOf(eq, reaches, live) {
  if (curves.has(eq)) return curves.get(eq);
  const out = eq.players.map((pl, p) => {
    const reach = reaches[p] || [];
    const items = [];
    pl.per_combo.forEach((e, i) => { if (e !== null && live[i] && (reach[i] || 0) > 0) items.push({ i, e, w: reach[i] }); });
    items.sort((a, b) => a.e - b.e);
    const tot = items.reduce((s, x) => s + x.w, 0);
    let cum = 0;
    for (const x of items) { x.q = tot ? (cum + x.w / 2) / tot : 0; cum += x.w; }
    const at = (q) => { // equity at percentile q (linear between neighbouring combos)
      if (!items.length) return null;
      let lo = 0, hi = items.length - 1;
      if (q <= items[0].q) return items[0].e;
      if (q >= items[hi].q) return items[hi].e;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (items[m].q <= q) lo = m; else hi = m; }
      const a = items[lo], b = items[hi];
      return a.e + ((b.e - a.e) * (q - a.q)) / (b.q - a.q || 1);
    };
    const samples = Array.from({ length: SAMPLES + 1 }, (_, k) => at(k / SAMPLES));
    const pts = items.map((x) => `${(x.q * W).toFixed(1)},${yOf(x.e).toFixed(1)}`).join(" ");
    return { items, pts, samples, tot };
  });
  curves.set(eq, out);
  return out;
}

const ord = (n) => { const m = n % 100; return n + (m >= 11 && m <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] || "th"); };

function chart({ cv, pos, dots, shownP, bucket }) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  const el = (tag, attrs) => { const x = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) x.setAttribute(k, v); svg.append(x); };
  el("line", { class: "eq-guide", x1: 0, x2: W, y1: H / 2, y2: H / 2 });
  el("line", { class: "eq-guide", x1: W / 2, x2: W / 2, y1: 0, y2: H });
  el("polyline", { class: "eq-line p1", points: cv[1].pts });
  el("polyline", { class: "eq-line p0", points: cv[0].pts });
  const marks = [];
  if (dots) [0, 1].forEach((p) => {
    if (bucket && p !== shownP) return;
    const hits = cv[p].items.filter((x) => dots(p, x.i));
    const step = Math.max(1, Math.ceil(hits.length / MAX_DOTS));
    hits.forEach((x, k) => {
      if (k % step) return;
      marks.push(h("span", { class: `eq-dot p${p}`, "aria-hidden": "true",
        style: { left: `${(100 * x.q).toFixed(2)}%`, top: `${((100 * yOf(x.e)) / H).toFixed(2)}%` } }));
    });
  });
  const idle = marks.length ? "Dots: combos in the current filter, placed on each player's curve."
    : "Hover the chart to read equity at any point in the range.";
  const vline = h("span", { class: "eq-vline", hidden: true, "aria-hidden": "true" });
  const readout = h("p", { class: "eq-readout mono" }, idle);
  const move = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const v = Math.max(0, Math.min(1, (e.clientX - r.left) / (r.width || 1)));
    const qi = Math.round(v * SAMPLES);
    vline.hidden = false;
    vline.style.left = `${(100 * v).toFixed(2)}%`;
    readout.textContent = `${ord(qi)} percentile · ${pos(0)} ${pct(cv[0].samples[qi], 1)} · ${pos(1)} ${pct(cv[1].samples[qi], 1)}`;
  };
  const leave = () => { vline.hidden = true; readout.textContent = idle; };
  const plot = h("div", { class: "eq-plot", role: "img", onmousemove: move, onmouseleave: leave,
    "aria-label": `Equity distribution, weakest to strongest combo: ${pos(0)} and ${pos(1)}` }, svg, marks, vline);
  return [h("div", { class: "eq-chart" },
    h("div", { class: "eq-y mono", "aria-hidden": "true" }, h("span", {}, "100"), h("span", {}, "50"), h("span", {}, "0")),
    plot, h("span", {}),
    h("div", { class: "eq-x mono", "aria-hidden": "true" }, h("span", {}, "weakest"), h("span", {}, "50th pct"), h("span", {}, "strongest"))),
  readout];
}

/**
 * Range equity panel. `status`: "loading" | "error" | "ok" (with `eq`). `reaches`: both players' node
 * reach; `live`: unblocked mask; `dots(p, i)` marks filtered combos (null = none); `bucket` the active
 * bucket key (applies to `shownP`); `onBucket(key | null)`.
 */
export function equityPanel({ status, eq, reaches, live, board, pos, dots, bucket, shownP, onBucket }) {
  const head = h("div", { class: "head" }, h("span", { class: "label" }, "Range equity"),
    h("span", { class: "eq-where mono", title: eq ? `${eq.method} · ${eq.runouts} runouts` : null }, `${board.map(cardText).join(" ")} · at this node`));
  const box = (...kids) => h("section", { class: "side-block eq-panel", "aria-label": "Range equity", "aria-busy": String(status === "loading") }, head, kids);
  if (status === "loading") return box(h("p", { class: "note eq-wait" }, "Computing equity…"));
  if (status !== "ok") return box(h("p", { class: "note" }, "Equity unavailable for this node."));
  const cv = curveOf(eq, reaches, live);
  const e = eq.players.map((p) => p.equity);
  const tot = (e[0] || 0) + (e[1] || 0);
  const w0 = tot ? (e[0] || 0) / tot : 0.5;
  const fig = (p) => h("span", { class: `eq-fig p${p}` }, h("span", { class: "eq-who mono" }, `${p ? "IP" : "OOP"} ${pos(p)}`),
    h("b", { class: "eq-v mono" }, pct(e[p], 1)),
    h("span", { class: "eq-n mono" }, `${num(eq.players[p].combos ?? cv[p].tot, 0)} combos`));
  const share = (p, key) => (cv[p].tot ? cv[p].items.reduce((s, x) => s + (inBucket(x.e, key) ? x.w : 0), 0) / cv[p].tot : 0);
  const rows = BUCKETS.map((b) => {
    const on = bucket === b.key;
    return shareRow({ name: b.label, s: [share(0, b.key), share(1, b.key)], max: 1, on, fmt: (x) => pct(x), nameClass: " mono",
      title: on ? "Click to clear the filter" : `Filter the grid to ${pos(shownP)} hands with ${b.label} equity`,
      onClick: () => onBucket(on ? null : b.key) });
  });
  return box(
    h("div", { class: "eq-figs" }, fig(0), fig(1)),
    h("div", { class: "eq-bar", "aria-hidden": "true" }, h("span", { class: "a", style: { width: `${(100 * w0).toFixed(1)}%` } }), h("span", { class: "b" })),
    chart({ cv, pos, dots, shownP, bucket }),
    h("div", { class: "pair-table" }, pairHead("Equity buckets", pos), rows));
}
