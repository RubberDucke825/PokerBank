// Trainer: villain's and hero's ranges at a graded postflop decision (main column, under the verdict).
// Reach and equity come from the street solve at feedback.grid_ref (GET /api/solves/{name}/ranges|equity?node=).
import { h, api, pct, num, errorBox, showTip, hideTip } from "../core.js";
import { renderGrid, comboClass } from "../grid.js";
import { aggregate } from "./strategy_parts.js";
import { streetAbbr } from "./train_util.js";

const RANGE_COLOR = "var(--accent)";
const enc = encodeURIComponent;

const cache = new Map(); // grid_ref key -> promise of {meta, rng, eq}; settled solves never change
function load(ref) {
  const key = `${ref.name}|${ref.node_id}`;
  if (!cache.has(key)) {
    const base = `/api/solves/${enc(ref.name)}`;
    const node = `node=${enc(ref.node_id)}`;
    const p = Promise.all([api(base), api(`${base}/ranges?${node}`), api(`${base}/equity?${node}`).catch(() => null)])
      .then(([meta, rng, eq]) => ({ meta, rng, eq }));
    p.catch(() => cache.delete(key));
    cache.set(key, p);
  }
  return cache.get(key);
}

/** Heat cells (opacity = share of the class's combos in range); board-blocked combos dropped. */
function rangeCells(combos, reach, board) {
  const dead = new Set(board);
  const keep = (i) => !dead.has(combos[i].slice(0, 2)) && !dead.has(combos[i].slice(2));
  const cells = {};
  let total = 0;
  for (const [cls, a] of Object.entries(aggregate(combos, { actions: [] }, reach, keep))) {
    if (a.reach <= 0) continue;
    total += a.reach;
    const weight = a.reach / a.n;
    cells[cls] = { weight, segments: [{ freq: 1, color: RANGE_COLOR }], label: `${pct(weight)} of combos`,
      tip: `${cls} · ${num(a.reach, 1)} of ${a.n} combos in range` };
  }
  return { cells, total };
}

function playerBlock(who, cells, total, equity, heroCls) {
  const grid = renderGrid(cells, { caption: `${who} range`,
    onFocus: (cls, el) => cells[cls] && showTip(el, cells[cls].tip), onBlur: hideTip });
  if (heroCls) { const mine = grid.querySelector(`[data-cls="${heroCls}"]`); if (mine) mine.classList.add("hero-cell"); }
  return h("figure", { class: "tr-range" },
    h("figcaption", { class: "tr-range-head" },
      h("span", { class: "tr-range-who" }, who),
      h("span", { class: "tr-range-stats" }, `${num(total, 0)} combos`,
        equity === null || equity === undefined ? null : h("b", {}, ` · ${pct(equity, 1)} eq`))),
    grid);
}

/**
 * Ranges block for a graded decision (null when the decision has no solve grid, e.g. preflop).
 * heroCards: hero's two cards (outlined in the hero grid).
 */
export function rangesPanel(fb, heroCards) {
  const ref = fb && fb.grid_ref;
  if (!ref || ref.kind !== "solve" || fb.street === "preflop") return null;
  const body = h("div", { class: "tr-ranges-body" }, h("p", { class: "loading" }, "Loading ranges…"));
  const el = h("section", { class: "tr-ranges", "aria-label": "Ranges at this decision" },
    h("div", { class: "tr-ranges-head" },
      h("span", { class: "label" }, "Ranges at this decision"),
      h("span", { class: "tr-ranges-meta" }, `${streetAbbr(fb.street)} · ${(fb.board || []).join(" ")}`)),
    body);
  load(ref).then(({ meta, rng, eq }) => {
    const players = rng.players.filter((p) => p.reach);
    const hero = players.find((p) => p.position === fb.position);
    const vill = players.find((p) => p !== hero);
    if (!hero || !vill) { body.replaceChildren(h("p", { class: "note" }, "Ranges unavailable for this decision.")); return; }
    const eqOf = (p) => (eq ? (eq.players.find((x) => x.position === p.position) || {}).equity : null);
    // the solve's own board: library flops are dealt suit-permuted, so fb.board can differ by suits
    const board = (eq && eq.board) || meta.board || [];
    const block = (p, who, cls) => { const r = rangeCells(meta.combos, p.reach, board); return playerBlock(who, r.cells, r.total, eqOf(p), cls); };
    body.replaceChildren(
      h("div", { class: "tr-ranges-grids" },
        block(vill, `Villain ${vill.position}`, null),
        block(hero, `You ${hero.position}`, comboClass(heroCards.join("")))),
      h("p", { class: "note" }, "Ranges reaching this decision, before your action · cell shade = share of the class's combos in range · your hand is outlined."));
  }).catch((e) => body.replaceChildren(errorBox(e)));
  return el;
}
