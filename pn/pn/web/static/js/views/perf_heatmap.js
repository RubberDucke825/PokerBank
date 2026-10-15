// Performance heatmap (Plan 15): rows = positions in table order, columns = spot families grouped by street; a cell
// is every /api/train/performance row with that position and family. Colour = GTO closeness on a diverging --bad ->
// --warn -> --good scale (performance.css); the % and the graded count are printed in every filled cell.
import { h, pct, showTip, hideTip } from "../core.js";

export const MIN_N = 5; // cells with fewer graded decisions are striped and muted (plan: "grey out cells with n < 5")
const OTHER = "other"; // the backend's family for NULL / unknown spots: always its own trailing group
export const STREETS = [["preflop", "Preflop"], ["flop", "Flop"], ["turn", "Turn"], ["river", "River"], [OTHER, "Other"]];
// Closeness bins, low edge of each step; step k gets class pf-b<k> (performance.css).
export const BINS = [0, 0.5, 0.7, 0.85, 0.95];
const ANY = "Any"; // row for rows without a position

export const binOf = (c) => (c === null || c === undefined ? null : BINS.reduce((k, lo, i) => (c >= lo ? i : k), 0));

/** axes.families entry -> {id, label, short, street}: accepts {id, label, street?} objects, [id, label] pairs or bare ids. */
function family(f, rows) {
  const id = typeof f === "string" ? f : Array.isArray(f) ? f[0] : f.id;
  const label = typeof f === "string" ? f : Array.isArray(f) ? f[1] || f[0] : f.label || f.id;
  const row = rows.find((r) => r.family === id);
  const parts = String(id).split("|");
  const street = id === OTHER ? OTHER
    : (f && f.street) || (row && row.street) || STREETS.map(([s]) => s).find((s) => parts.includes(s)) || "preflop";
  const short = street === "preflop" || street === OTHER ? label : label.replace(` ${street}`, ""); // the street is the group header
  return { id, label, short, street };
}

/** Sum rows into one cell: counts add, closeness is a graded-weighted mean, RNG match a mean weighted by each row's
 *  rolled decisions (rng_n, the backend's rng_match denominator). */
export function mergeRows(rows) {
  const c = { rows, n: 0, graded: 0, best: 0, mixed: 0, blunders: 0, closeness: null, rng_match: null, last_played: null };
  let cw = 0, cs = 0, rw = 0, rs = 0;
  for (const r of rows) {
    c.n += r.n || 0; c.graded += r.graded || 0; c.best += r.best || 0; c.mixed += r.mixed || 0; c.blunders += r.blunders || 0;
    if (r.closeness !== null && r.closeness !== undefined && r.graded) { cw += r.graded; cs += r.closeness * r.graded; }
    if (r.rng_match !== null && r.rng_match !== undefined && r.rng_n) { rw += r.rng_n; rs += r.rng_match * r.rng_n; }
    if (r.last_played && (!c.last_played || r.last_played > c.last_played)) c.last_played = r.last_played;
  }
  if (cw) c.closeness = cs / cw;
  if (rw) c.rng_match = rs / rw;
  return c;
}

/** API body -> {positions, groups: [{street, label, fams}], fams, cell(pos, famId)}. Empty streets are dropped. */
export function buildModel(data) {
  const rows = data.rows || [];
  const axes = data.axes || {};
  const fams = (axes.families || []).map((f) => family(f, rows));
  for (const r of rows) if (r.family && !fams.some((f) => f.id === r.family)) fams.push(family(r.family, rows)); // never drop data
  const positions = [...(axes.positions || [])];
  for (const r of rows) {
    const p = r.position || ANY;
    if (!positions.includes(p)) positions.push(p);
  }
  const cells = new Map();
  for (const r of rows) {
    const k = `${r.position || ANY}:${r.family}`;
    if (!cells.has(k)) cells.set(k, []);
    cells.get(k).push(r);
  }
  const merged = new Map([...cells].map(([k, rs]) => [k, mergeRows(rs)]));
  const used = new Set(rows.map((r) => r.family));
  const groups = STREETS.map(([street, label]) => ({ street, label, fams: fams.filter((f) => f.street === street && used.has(f.id)) }))
    .filter((g) => g.fams.length);
  return { positions: positions.filter((p) => rows.some((r) => (r.position || ANY) === p)), groups,
    fams: groups.flatMap((g) => g.fams), cell: (pos, fam) => merged.get(`${pos}:${fam}`) || null };
}

const gradeLine = (c) => `best ${c.best} · mixed ${c.mixed} · blunders ${c.blunders}`;
export const cellName = (pos, fam) => `${pos} · ${fam.label}`;
/** Thin = fewer than MIN_N graded decisions (closeness only comes from graded ones). */
export const isThin = (c) => c.graded < MIN_N;

/** Full text for aria-label (the visible cell only prints % and n). */
function ariaText(pos, fam, c) {
  if (!c) return `${cellName(pos, fam)}: no decisions`;
  const close = c.closeness === null ? "not graded" : `GTO closeness ${pct(c.closeness)}`;
  return `${cellName(pos, fam)}: ${close}, ${c.n} decision${c.n === 1 ? "" : "s"}, ${c.graded} graded, ${gradeLine(c)}` +
    (c.rng_match === null ? "" : `, RNG match ${pct(c.rng_match)}`) + (isThin(c) ? ", thin sample" : "");
}

/** Tooltip: the value leads (closeness), then the cell name and the counts. */
export function tipContent(pos, fam, c) {
  return h("div", { class: "pf-tip" },
    h("div", { class: "pf-tip-v" }, h("b", {}, c.closeness === null ? "–" : pct(c.closeness)), h("span", { class: "muted" }, " GTO closeness")),
    h("div", { class: "pf-tip-name" }, cellName(pos, fam)),
    h("ul", { class: "tip-list" },
      h("li", {}, `${c.n} decision${c.n === 1 ? "" : "s"} · ${c.graded} graded`),
      h("li", {}, gradeLine(c)),
      h("li", {}, `RNG match ${pct(c.rng_match)}`),
      c.rows.length > 1 ? h("li", { class: "muted" }, `${c.rows.length} spots`) : null,
      isThin(c) ? h("li", { class: "muted" }, `thin sample (fewer than ${MIN_N} graded)`) : null));
}

/**
 * Heatmap table. opts.selected "pos:fam" | null, opts.onPick(pos, fam, cell) on click / Enter / Space.
 * Roving tabindex over every cell (empty ones too, so arrows move on a regular grid, like grid.js).
 */
export function heatmap(model, opts = {}) {
  const tds = [];
  const ncol = model.fams.length;
  const head = h("thead", {},
    h("tr", { class: "pf-groups" }, h("th", { class: "pf-corner", scope: "col" }, h("span", { class: "visually-hidden" }, "Position")),
      model.groups.map((g) => h("th", { scope: "colgroup", colspan: g.fams.length, class: "pf-group" }, g.label))),
    h("tr", { class: "pf-fams" }, h("th", { class: "pf-corner", scope: "col", "aria-hidden": "true" }),
      model.groups.flatMap((g) => g.fams.map((f, k) => h("th", { scope: "col", class: `pf-fam${k === 0 ? " pf-gstart" : ""}`, title: f.label }, f.short)))));
  let first = true;
  const body = h("tbody", {}, model.positions.map((pos, i) => h("tr", {},
    h("th", { scope: "row", class: "pf-pos" }, pos),
    model.groups.flatMap((g) => g.fams.map((f, k) => {
      const c = model.cell(pos, f.id);
      const key = `${pos}:${f.id}`;
      const sel = opts.selected === key;
      const bin = c ? binOf(c.closeness) : null;
      const cls = ["pf-cell", k === 0 && "pf-gstart", !c && "is-empty", c && bin === null && "is-ungraded", bin !== null && `pf-b${bin}`,
        c && isThin(c) && "is-thin", sel && "is-selected"].filter(Boolean).join(" ");
      const tab = opts.selected ? sel : first && !!c;
      if (tab) first = false;
      const td = h("td", { class: cls, role: "gridcell", tabindex: tab ? 0 : -1, "aria-selected": String(sel),
        "aria-label": ariaText(pos, f, c) },
        c ? [h("span", { class: "pf-v", "aria-hidden": "true" }, c.closeness === null ? "–" : pct(c.closeness)),
          h("span", { class: "pf-n", "aria-hidden": "true" }, c.graded)] : null);
      td._pf = { pos, fam: f, cell: c };
      tds.push(td);
      return td;
    })))));
  if (first && tds.length) tds[0].tabIndex = 0; // nothing filled: still reachable
  const table = h("table", { class: "pf-heat", role: "grid", "aria-label": "GTO closeness by position and spot family",
    "aria-rowcount": model.positions.length + 2, "aria-colcount": ncol + 1 }, head, body);

  const pick = (td) => { if (td && td._pf.cell && opts.onPick) opts.onPick(td._pf.pos, td._pf.fam, td._pf.cell); };
  table.addEventListener("click", (e) => pick(e.target.closest("td.pf-cell")));
  table.addEventListener("keydown", (e) => {
    const td = e.target.closest("td.pf-cell");
    if (!td) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(td); return; }
    const at = tds.indexOf(td);
    let i = Math.floor(at / ncol), j = at % ncol;
    const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (e.key === "Home") j = 0;
    else if (e.key === "End") j = ncol - 1;
    else if (moves[e.key]) { i += moves[e.key][0]; j += moves[e.key][1]; }
    else return;
    e.preventDefault();
    i = Math.max(0, Math.min(model.positions.length - 1, i));
    j = Math.max(0, Math.min(ncol - 1, j));
    const next = tds[i * ncol + j];
    td.tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  });
  const enter = (e) => {
    const td = e.target.closest("td.pf-cell");
    if (td && td._pf.cell) showTip(td, tipContent(td._pf.pos, td._pf.fam, td._pf.cell));
    else hideTip();
  };
  table.addEventListener("mouseover", enter);
  table.addEventListener("focusin", enter);
  table.addEventListener("mouseleave", hideTip);
  table.addEventListener("focusout", hideTip);
  return table;
}

/** Stepped legend: one swatch per bin with its range, then the thin-sample and empty keys. */
export function heatLegend() {
  const ranges = BINS.map((lo, k) => (k === BINS.length - 1 ? `≥ ${pct(lo)}` : `${pct(lo)}–${pct(BINS[k + 1])}`));
  return h("div", { class: "pf-legend", role: "group", "aria-label": "Colour key" },
    h("span", { class: "label" }, "GTO closeness"),
    h("ol", { class: "pf-steps" }, ranges.map((r, k) => h("li", {}, h("span", { class: `pf-swatch pf-b${k}`, "aria-hidden": "true" }), r))),
    h("span", { class: "pf-key" }, h("span", { class: "pf-swatch is-thin", "aria-hidden": "true" }), `striped: fewer than ${MIN_N} graded decisions`),
    h("span", { class: "pf-key" }, h("span", { class: "pf-swatch is-ungraded", "aria-hidden": "true" }), "– : none graded"));
}
