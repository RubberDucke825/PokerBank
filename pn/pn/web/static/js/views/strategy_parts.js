// Tree-browser building blocks: per-class aggregation, action labels, hand-class composition
// (Breakdown tab) and the solve meta row. No EVs anywhere (Plan 7: TexasSolver
// documents carry strategies only).
import { h, num, pct } from "../core.js";
import { comboClass } from "../grid.js";
import { classify, MADE, DRAWS } from "../handclass.js";
import { SUIT_GLYPH } from "../deck.js";

export const sum = (xs) => xs.reduce((a, b) => a + (b || 0), 0);

/**
 * Per hand class: combos, summed reach and reach-weighted action frequencies.
 * `reach` defaults to node.reach; `keep(i)` filters combos (hand-class category filter).
 */
export function aggregate(combos, node, reach = node.reach, keep = null) {
  const acc = {};
  const n = node.actions ? node.actions.length : 0;
  combos.forEach((c, i) => {
    const cls = comboClass(c);
    const a = (acc[cls] ||= { reach: 0, n: 0, freq: new Array(n).fill(0), combos: [] });
    a.n += 1;
    const r = reach ? reach[i] || 0 : 0;
    if (r <= 0 || (keep && !keep(i))) return;
    a.reach += r;
    if (node.strategy && node.strategy[i]) node.strategy[i].forEach((f, k) => { a.freq[k] += r * f; });
    a.combos.push(i);
  });
  return acc;
}

/** "bet33" -> {name: "Bet", size: "33%"}; check / call / fold / allin as words. */
export function actionLabel(token) {
  const t = token.toLowerCase();
  const m = t.match(/^(bet|raise|donk)(\d+(?:\.\d+)?)$/);
  if (m) return { name: m[1][0].toUpperCase() + m[1].slice(1), size: `${m[2]}%` };
  if (t === "allin") return { name: "All-in", size: "" };
  return { name: t[0].toUpperCase() + t.slice(1), size: "" };
}

/** Chips of an action in bb (amount is cents put in by the action), or null. */
export const actionBb = (a, bb) => (a.amount === undefined || a.amount === null ? null : a.amount / bb);

/** Frequencies per action: the node summary when present, else reach-weighted from the grid. */
export function frequencies(node, agg) {
  const toks = node.actions.map((a) => a.token);
  const s = node.summary || {};
  if (toks.every((t) => typeof s[t] === "number")) return toks.map((t) => s[t]);
  const tot = toks.map((_, k) => sum(Object.values(agg).map((a) => a.freq[k])));
  const all = sum(tot);
  return tot.map((v) => (all ? v / all : 0));
}

/** "AhKd" as two suit-coloured glyph pairs. */
export const comboText = (c) => [c.slice(0, 2), c.slice(2, 4)].map((x) =>
  h("span", { class: `ink-${x[1]}` }, x[0], SUIT_GLYPH[x[1]]));

/** Stacked frequency bar (flex segments). */
export const mixBar = (freqs, colors, cls = "mix-bar") => {
  const tot = sum(freqs);
  return h("span", { class: cls, "aria-hidden": "true" }, freqs.map((f, k) =>
    h("span", { style: { flexGrow: String(tot ? f / tot : 0), background: colors[k] } })));
};

/**
 * Range composition by made hand and draw for a combo weight vector (`reach`), with the action mix
 * inside each category when `node` is the player's own action node. Board-blocked combos are skipped.
 * Rows: {key, kind, w, share, mix[]}; `total` = weighted combos.
 */
export function composition(combos, reach, board, node = null) {
  const rows = new Map();
  let total = 0;
  const n = node && node.actions ? node.actions.length : 0;
  const dead = new Set(board);
  combos.forEach((c, i) => {
    const w = reach ? reach[i] || 0 : 0;
    if (w <= 0 || dead.has(c.slice(0, 2)) || dead.has(c.slice(2, 4))) return;
    total += w;
    const hc = classify(c, board);
    for (const [key, kind] of [[hc.made, "made"], ...hc.draws.map((d) => [d, "draw"])]) {
      if (!key) continue;
      const r = rows.get(key) || { key, kind, w: 0, mix: new Array(n).fill(0), idx: [] };
      r.w += w;
      r.idx.push(i);
      if (n && node.strategy && node.strategy[i]) node.strategy[i].forEach((f, k) => { r.mix[k] += w * f; });
      rows.set(key, r);
    }
  });
  const order = [...MADE, ...DRAWS];
  const out = [...rows.values()].map((r) => ({ ...r, share: total ? r.w / total : 0 }))
    .sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  return { rows: out, total };
}

/** Composition panel (Breakdown tab); `onPick(key | null)` filters the grid to one category. */
export function compositionPanel({ comp, colors, title, active, onPick }) {
  const row = (r) => h("button", { type: "button", class: `comp-row${active === r.key ? " is-active" : ""}`, "aria-pressed": String(active === r.key),
    onclick: () => onPick(active === r.key ? null : r.key) },
    h("span", { class: "comp-name" }, r.key),
    h("span", { class: "comp-share mono" }, pct(r.share, 1)),
    h("span", { class: "comp-track" }, h("i", { style: { transform: `scaleX(${Math.min(1, r.share / Math.max(...comp.rows.map((x) => x.share), 1e-9))})` } })),
    colors && r.mix.length ? mixBar(r.mix, colors, "mix-bar comp-mix") : h("span", { class: "comp-mix" }),
    h("span", { class: "comp-n mono" }, num(r.w, 0)));
  const made = comp.rows.filter((r) => r.kind === "made"), draws = comp.rows.filter((r) => r.kind === "draw");
  return h("div", { class: "side-block comp-panel" },
    h("div", { class: "head" }, h("span", { class: "label" }, title), h("span", { class: "muted mono" }, `${num(comp.total, 0)} combos`)),
    made.length ? h("div", { class: "comp-list" }, made.map(row)) : h("p", { class: "note" }, "Empty range."),
    draws.length ? [h("span", { class: "label comp-sub" }, "Draws"), h("div", { class: "comp-list" }, draws.map(row))] : null);
}

/**
 * "SHOWING" row above the grid: one pill per active filter ({label, swatch, onClear}) with its own
 * clear button, then the Esc hint. Null when nothing is active.
 */
export function filterChips(chips) {
  if (!chips.length) return null;
  return h("div", { class: "filter-chips", role: "group", "aria-label": "Active filters" },
    h("span", { class: "label" }, "Showing"),
    chips.map((c) => h("span", { class: "fchip" },
      h("span", { class: "fchip-sw", style: { background: c.swatch }, "aria-hidden": "true" }), c.label,
      h("button", { type: "button", class: "fchip-x", "aria-label": `Clear ${c.label}`, onclick: c.onClear }, "×"))),
    h("span", { class: "st-hint" }, h("kbd", {}, "Esc"), " clears"));
}

/** Solve facts: engine, iterations, exploitability, wall time, nodes (no EV fields). */
export function metaRow(meta) {
  const m = meta.meta || {};
  const has = (v) => v !== null && v !== undefined;
  const wall = m.wall_sec ?? meta.elapsed_sec;
  const items = [
    ["engine", m.backend], ["iterations", m.iterations],
    ["exploitability", has(m.exploitability_pct_pot) ? `${num(m.exploitability_pct_pot, 2)}% pot` : null],
    ["stop", m.stop], ["wall", has(wall) ? `${num(wall, 0)} s` : null], ["nodes", meta.node_count],
  ];
  return h("div", { class: "meta-row" }, items.map(([k, v]) => h("span", {}, `${k.toUpperCase()} `, h("b", {}, has(v) ? v : "–"))));
}
