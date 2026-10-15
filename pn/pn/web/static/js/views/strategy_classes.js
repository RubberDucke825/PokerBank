// Hand classes panel of the tree browser (right column): share of each position's range per made hand
// and draw (classify() in handclass.js), weighted by node reach, board-blocked combos removed. Rows filter
// the grid. Also the per-board helpers the browser shares: live (unblocked) mask and combo classes.
import { h, pct } from "../core.js";
import { classify, MADE, DRAWS } from "../handclass.js";

const MIN_SHARE = 0.0005; // rows with both shares under 0.05% are hidden
const HI_RATIO = 1.15; // highlight the larger share when it beats the other by more than 15%

const memo = new Map(); // board key -> {live, cls}; boards per page are few
function perBoard(combos, board) {
  const key = board.join("");
  let m = memo.get(key);
  if (!m || m.combos !== combos) {
    const on = new Set(board);
    const live = combos.map((c) => !on.has(c.slice(0, 2)) && !on.has(c.slice(2, 4)));
    m = { combos, live, cls: combos.map((c, i) => (live[i] ? classify(c, board) : null)) };
    if (memo.size > 16) memo.clear();
    memo.set(key, m);
  }
  return m;
}

/** live[i]: combo i shares no card with the board. */
export const liveMask = (combos, board) => perBoard(combos, board).live;
/** cls[i]: {made, draws[]} of combo i on the board, null when blocked. */
export const comboClasses = (combos, board) => perBoard(combos, board).cls;
/** Combo class `c` ({made, draws}) contains hand class `name`. */
export const hasClass = (c, name) => !!c && (c.made === name || c.draws.includes(name));
export const isClassName = (name) => MADE.includes(name) || DRAWS.includes(name);

/** Sum of reach over live combos (the tab count and the equity panel agree). */
export function liveCombos(reach, live, keep = null) {
  let t = 0;
  for (let i = 0; i < live.length; i++) if (live[i] && (!keep || keep(i))) t += (reach && reach[i]) || 0;
  return t;
}

/** [{name, kind, s: [share0, share1]}] for both players' reach vectors. */
export function classShares(combos, board, reaches) {
  const { live, cls } = perBoard(combos, board);
  const names = [...MADE, ...DRAWS];
  const w = reaches.map(() => new Map(names.map((n) => [n, 0])));
  const tot = reaches.map((reach, p) => {
    let t = 0;
    cls.forEach((c, i) => {
      const r = live[i] ? (reach && reach[i]) || 0 : 0;
      if (r <= 0 || !c) return;
      t += r;
      for (const n of [c.made, ...c.draws]) if (n) w[p].set(n, w[p].get(n) + r);
    });
    return t;
  });
  return names.map((name) => ({ name, kind: MADE.includes(name) ? "made" : "draw",
    s: reaches.map((_, p) => (tot[p] ? w[p].get(name) / tot[p] : 0)) }))
    .filter((r) => r.s.some((x) => x > MIN_SHARE));
}

export const fmtShare = (x) => (x <= 0 ? "–" : x < 0.01 ? "<1%" : pct(x, x < 0.1 ? 1 : 0));

/** Paired-share row (hand classes and equity buckets): name, two shares, two 3px bars scaled to `max`. */
export function shareRow({ name, s, max, on, title, onClick, fmt = fmtShare, nameClass = "" }) {
  const hi0 = s[0] > s[1] * HI_RATIO, hi1 = s[1] > s[0] * HI_RATIO;
  const w = (x) => ({ transform: `scaleX(${max > 0 ? Math.min(1, x / max) : 0})` });
  return h("button", { type: "button", class: `pair-row${on ? " is-on" : ""}`, "aria-pressed": String(on), title, onclick: onClick },
    h("span", { class: `pair-name${nameClass}` }, name),
    h("span", { class: `pair-v mono${hi0 ? " is-hi0" : ""}` }, fmt(s[0])),
    h("span", { class: `pair-v p1 mono${hi1 ? " is-hi1" : ""}` }, fmt(s[1])),
    h("span", { class: "pair-bars", "aria-hidden": "true" }, h("i", { class: "b0", style: w(s[0]) }), h("i", { class: "b1", style: w(s[1]) })));
}

/** Column header shared by the buckets table and the classes panel. */
export const pairHead = (title, pos, kicker = false) => h("div", { class: "pair-head" },
  h("span", { class: kicker ? "label" : "pair-sub" }, title),
  h("span", { class: "pair-pos p0" }, pos(0)), h("span", { class: "pair-pos p1" }, pos(1)));

/** Hand classes panel. `rows` from classShares; `active` the hand-class filter; `onPick(name | null)`. */
export function classesPanel({ rows, pos, active, onPick }) {
  const max = Math.max(0.01, ...rows.map((r) => Math.max(...r.s)));
  const row = (r) => {
    const on = active === r.name;
    return shareRow({ name: r.name, s: r.s, max, on, title: on ? "Click to clear the filter" : `Filter the grid to ${r.name}`,
      onClick: () => onPick(on ? null : r.name) });
  };
  const made = rows.filter((r) => r.kind === "made"), draws = rows.filter((r) => r.kind === "draw");
  return h("section", { class: "side-block hc-panel", "aria-label": "Hand classes" },
    pairHead("Hand classes", pos, true),
    rows.length ? [
      made.length ? h("div", { class: "pair-group" }, h("span", { class: "pair-sub" }, "Made hands"), made.map(row)) : null,
      draws.length ? h("div", { class: "pair-group" }, h("span", { class: "pair-sub" }, "Draws · overlap made hands"), draws.map(row)) : null]
      : h("p", { class: "note" }, "Empty ranges."),
    h("p", { class: "st-hint" }, "Share of each position's range, by combos. Click a class to filter the grid to it."));
}
