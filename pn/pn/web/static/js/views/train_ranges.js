// Trainer: villain's and hero's ranges at a graded postflop decision (main column, under the verdict).
// Now: reach and equity from the street solve at feedback.grid_ref (GET /api/solves/{name}/ranges|equity?node=).
// Line modes: what each range did step by step, on the previous street (preflop_line, or line_breakdown at the
// solve's parent) or earlier on this street (line_breakdown?node=): black = not in range, then action colours.
import { h, api, pct, num, errorBox, showTip, hideTip } from "../core.js";
import { renderGrid, comboClass, actionColors } from "../grid.js";
import { aggregate, actionLabel, sum } from "./strategy_parts.js";
import { streetAbbr, loadPrefs, savePrefs } from "./train_util.js";

const RANGE_COLOR = "var(--accent)";
export const OUT_COLOR = "#000"; // combos not in range entering the step
const PREV = { flop: "preflop", turn: "flop", river: "turn" };
const cap = (s) => s[0].toUpperCase() + s.slice(1);
const enc = encodeURIComponent;

const cache = new Map(); // key -> promise; settled solves and finished preflop lines never change
function memo(key, make) {
  if (!cache.has(key)) {
    const p = make();
    p.catch(() => cache.delete(key));
    cache.set(key, p);
  }
  return cache.get(key);
}
function load(ref) {
  const base = `/api/solves/${enc(ref.name)}`;
  const node = `node=${enc(ref.node_id)}`;
  return memo(`now|${ref.name}|${ref.node_id}`, () =>
    Promise.all([api(base), api(`${base}/ranges?${node}`), api(`${base}/equity?${node}`).catch(() => null)])
      .then(([meta, rng, eq]) => ({ meta, rng, eq })));
}
const breakdown = (name, node) => memo(`line|${name}|${node}`, () =>
  api(`/api/solves/${enc(name)}/line_breakdown?node=${enc(node)}`));
const preflopLine = (sid, hand) => memo(`pf|${sid}|${hand}`, () => api(`/api/train/${enc(sid)}/preflop_line`));

/** {prev, street}: step lists (null = unavailable) for the previous street and this street up to the decision. */
async function loadLines(fb, ctx) {
  const ref = fb.grid_ref;
  const here = await breakdown(ref.name, ref.node_id).catch(() => null);
  let prev = null;
  if (fb.street === "flop") prev = ctx.sid ? await preflopLine(ctx.sid, ctx.hand).catch(() => null) : null;
  else if (here && here.parent) prev = await breakdown(here.parent.name, here.parent.node).catch(() => null);
  const steps = (r) => (r && r.steps && r.steps.length ? r.steps : null);
  return { prev: steps(prev), street: steps(here) };
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

/**
 * Pure: a line step -> full-fill grid cells. Each class is split left->right by action frequency over the
 * in-range share (reach/n); the rest of the cell (all of it at reach 0) is black. Also per-action combo counts.
 */
export function lineCells(step) {
  const colors = actionColors(step.actions);
  const names = step.actions.map((t) => { const a = actionLabel(t); return a.size ? `${a.name} ${a.size}` : a.name; });
  const combos = step.actions.map(() => 0);
  const cells = {};
  let total = 0;
  for (const [cls, c] of Object.entries(step.classes || {})) {
    const share = c.n ? Math.min(1, (c.reach || 0) / c.n) : 0;
    const tot = sum(c.freq || []);
    if (share <= 0 || !tot) {
      cells[cls] = { weight: 1, segments: [{ freq: 1, color: OUT_COLOR }], label: "not in range", tip: `${cls} · not in range` };
      continue;
    }
    const fr = c.freq.map((f) => f / tot);
    fr.forEach((f, k) => { combos[k] += c.reach * f; });
    total += c.reach;
    const mix = step.actions.map((_, k) => `${names[k]} ${pct(fr[k])}`).join(" · ");
    cells[cls] = { weight: 1, label: mix,
      segments: [...fr.map((f, k) => ({ freq: f * share, color: colors[k] })), { freq: 1 - share, color: OUT_COLOR }],
      tip: `${cls} · ${num(c.reach, 1)} of ${c.n} combos · ${mix}` };
  }
  return { cells, colors, names, combos, total };
}

function markHero(grid, heroCls) {
  const mine = heroCls && grid.querySelector(`[data-cls="${heroCls}"]`);
  if (mine) mine.classList.add("hero-cell");
}

function playerBlock(who, cells, total, equity, heroCls) {
  const grid = renderGrid(cells, { caption: `${who} range`,
    onFocus: (cls, el) => cells[cls] && showTip(el, cells[cls].tip), onBlur: hideTip });
  markHero(grid, heroCls);
  return h("figure", { class: "tr-range" },
    h("figcaption", { class: "tr-range-head" },
      h("span", { class: "tr-range-who" }, who),
      h("span", { class: "tr-range-stats" }, `${num(total, 0)} combos`,
        equity === null || equity === undefined ? null : h("b", {}, ` · ${pct(equity, 1)} eq`))),
    grid);
}

/** One player's step: its action split grid plus a legend with combos per action, the taken one marked. */
function stepBlock(who, step, heroCls, isSel) {
  const r = lineCells(step);
  const grid = renderGrid(r.cells, { fill: "full", class: "gw tr-line", caption: `${who} ${step.position} by action`,
    onFocus: (cls, el) => r.cells[cls] && showTip(el, r.cells[cls].tip), onBlur: hideTip });
  markHero(grid, heroCls);
  const legend = h("ul", { class: "legend tr-legend" }, step.actions.map((t, k) =>
    h("li", { class: t === step.taken ? "is-taken" : null },
      h("span", { class: "swatch", style: { background: r.colors[k] } }), t === step.taken ? `▸ ${r.names[k]}` : r.names[k],
      h("b", {}, num(r.combos[k], 0)))),
  h("li", {}, h("span", { class: "swatch tr-out" }), "Not in range"));
  return h("figure", { class: `tr-range${isSel ? " is-sel" : ""}` },
    h("figcaption", { class: "tr-range-head" },
      h("span", { class: "tr-range-who" }, `${who} · ${actionLabel(step.taken || "?").name}`),
      h("span", { class: "tr-range-stats" }, `${num(r.total, 0)} combos in`)),
    grid, legend);
}

const stepText = (s) => { const a = actionLabel(s.taken || "?"); return `${s.position} ${a.name.toLowerCase()}${a.size ? ` ${a.size}` : ""}`; };

/** Line mode body: a clickable step strip, then villain (left) and hero (right) at the selected step. */
function lineView(steps, heroPos, vilPos, heroCls, label) {
  const box = h("div", { class: "tr-line-view" });
  const paint = (sel) => {
    const latest = (pos) => { for (let i = sel; i >= 0; i--) if (steps[i].position === pos) return i; return -1; };
    const actor = steps[sel].position;
    const leftPos = actor !== heroPos ? actor : vilPos;
    const slot = (pos, who, cls) => {
      const i = latest(pos);
      return i < 0 ? h("figure", { class: "tr-range tr-range-none" }, h("p", { class: "note" }, `${who} has not acted yet.`))
        : stepBlock(who, steps[i], cls, i === sel);
    };
    const strip = h("ol", { class: "tr-steps", "aria-label": `${label} steps` }, steps.map((s, i) =>
      h("li", {}, h("button", { type: "button", class: "tr-step", "aria-pressed": String(i === sel),
        onclick: () => paint(i) }, stepText(s)))));
    box.replaceChildren(strip,
      h("div", { class: "tr-ranges-grids" },
        slot(leftPos, `Villain ${leftPos}`, null),
        slot(heroPos, `You ${heroPos}`, heroCls)));
  };
  paint(steps.length - 1);
  return box;
}

/**
 * Ranges block for a graded decision (null when the decision has no solve grid, e.g. preflop).
 * heroCards: hero's two cards (outlined in the hero grid). ctx: {sid, hand} for the preflop line.
 */
export function rangesPanel(fb, heroCards, ctx = {}) {
  const ref = fb && fb.grid_ref;
  if (!ref || ref.kind !== "solve" || fb.street === "preflop") return null;
  const heroCls = comboClass(heroCards.join(""));
  const prevName = `${cap(PREV[fb.street] || "previous")} line`;
  const body = h("div", { class: "tr-ranges-body" }, h("p", { class: "loading" }, "Loading ranges…"));
  const seg = h("div", { class: "seg tr-modes", role: "group", "aria-label": "Range view" });
  const el = h("section", { class: "tr-ranges", "aria-label": "Ranges at this decision" },
    h("div", { class: "tr-ranges-head" },
      h("span", { class: "label" }, "Ranges at this decision"),
      h("span", { class: "tr-ranges-meta" }, `${streetAbbr(fb.street)} · ${(fb.board || []).join(" ")}`)),
    seg, body);
  let mode = loadPrefs().rangesMode || "now";
  let lines = { prev: null, street: null };
  let now = null; // {hero, vill} positions once the Now view resolves
  const opts = () => [["now", "Now"], ["prev", prevName], ["street", "This street"]].filter(([m]) => m === "now" || lines[m]);
  const drawSeg = () => {
    const list = opts();
    seg.hidden = list.length < 2;
    seg.replaceChildren(...list.map(([m, txt]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(m === mode),
      onclick: () => { mode = m; savePrefs({ rangesMode: m }); draw(); } }, txt)));
  };
  const nowView = load(ref).then(({ meta, rng, eq }) => {
    const players = rng.players.filter((p) => p.reach);
    const hero = players.find((p) => p.position === fb.position);
    const vill = players.find((p) => p !== hero);
    if (!hero || !vill) return [h("p", { class: "note" }, "Ranges unavailable for this decision.")];
    now = { hero: hero.position, vill: vill.position };
    const eqOf = (p) => (eq ? (eq.players.find((x) => x.position === p.position) || {}).equity : null);
    // the solve's own board: library flops are dealt suit-permuted, so fb.board can differ by suits
    const board = (eq && eq.board) || meta.board || [];
    const block = (p, who, cls) => { const r = rangeCells(meta.combos, p.reach, board); return playerBlock(who, r.cells, r.total, eqOf(p), cls); };
    return [h("div", { class: "tr-ranges-grids" },
        block(vill, `Villain ${vill.position}`, null),
        block(hero, `You ${hero.position}`, heroCls)),
      h("p", { class: "note" }, "Ranges reaching this decision, before your action · cell shade = share of the class's combos in range · your hand is outlined.")];
  });
  nowView.catch(() => {}); // a line mode may be showing when it fails; draw() reports it in Now
  const lineBody = (steps, label) => {
    const vilPos = (now && now.vill) || ([...steps].reverse().find((s) => s.position !== fb.position) || {}).position;
    return [lineView(steps, fb.position, vilPos, heroCls, label),
      h("p", { class: "note" }, `${label}: what each range did at the step · black = not in range entering it · `
        + "blue fold, green check/call, red bet/raise (darker = bigger) · coloured width = share of combos in range · "
        + "▸ marks the action taken · click a step to replay it · your hand is outlined.")];
  };
  function draw() {
    if (mode !== "now" && !lines[mode]) mode = "now";
    drawSeg();
    if (mode === "now") nowView.then((n) => { if (mode === "now") body.replaceChildren(...n); })
      .catch((e) => body.replaceChildren(errorBox(e)));
    else body.replaceChildren(...lineBody(lines[mode], mode === "prev" ? prevName : "This street"));
  }
  const want = mode; // the saved line mode shows once its steps arrive
  draw();
  loadLines(fb, ctx).then((l) => {
    lines = l;
    if (want !== "now" && lines[want]) mode = want;
    nowView.then(draw, draw);
  });
  return el;
}
