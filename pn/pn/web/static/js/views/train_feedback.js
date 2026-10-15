// Trainer Feedback tab: "This hand" decision pills, then the selected decision's verdict head, RNG bucket bar,
// solver frequencies and the range grid (open by default, G toggles). Feedback shape: contracts/trainer.md.
import { h, api, cards, pct, errorBox, showTip, hideTip } from "../core.js";
import { renderGrid, legend, actionColors, comboClass } from "../grid.js";
import { aggregate } from "./strategy.js";
import { spotGrid } from "./charts.js";
import { tokenName, optLabel, streetAbbr } from "./train_util.js";

const PREFLOP_SOURCE = { pokercoaching: "PokerCoaching charts", gtowizard: "GTO Wizard frequencies", chart: "the hand-written charts" };
// Backend notes for ungraded hero decisions shown as a quiet state (no verdict colour, buckets or RNG note).
const QUIET_NOTES = {
  multiway_unsolvable: ["Multiway, not graded", "the trainer grades 2-player streets only", "3+ players on this street: not solved, not graded."],
  combo_outside_range: ["Not graded", "your hand is outside the solved range", "Library flops are not widened to your cards: this hand is outside the solved range."],
  off_tree: ["Not graded", "the hand left the solved tree", "No leaf of the solved tree matches this line, so the street was not solved."],
};
// Frequency grades (contracts/trainer.md "Plan 7 additions"): best >= 50% of the top action's frequency,
// mixed >= 10%, blunder below. Level = the verdict colour class (components.css .fb-status / .grade-*).
export const GRADE_LEVEL = { best: "ok", mixed: "warn", blunder: "bad" };
const GRADE_STATUS = { best: "● BEST", mixed: "● MIXED", blunder: "● BLUNDER" };
const API_CACHE_MAX = 64;

const apiCache = new Map(); // GET url -> promise: the grid re-renders on every pill / toggle
function cachedApi(url) {
  if (!apiCache.has(url)) {
    if (apiCache.size >= API_CACHE_MAX) apiCache.delete(apiCache.keys().next().value);
    const p = api(url);
    p.catch(() => apiCache.delete(url)); // never cache a failure
    apiCache.set(url, p);
  }
  return apiCache.get(url);
}

/** Verdict parts: status kicker, "best …" line and the chosen-action frequency, for every grading case. */
export function verdictParts(fb) {
  if (GRADE_LEVEL[fb.grade]) {
    const top = fb.best === fb.chosen;
    return { lvl: GRADE_LEVEL[fb.grade], status: GRADE_STATUS[fb.grade],
      best: top ? "the solver's most frequent action" : `best ${tokenName(fb.best)} ${pct(fb.best_freq)}`,
      figure: pct(fb.freq), title: `solver frequency of ${tokenName(fb.chosen)}` +
        (top ? "" : ` (best ${tokenName(fb.best)} ${pct(fb.best_freq)})`) };
  }
  if (QUIET_NOTES[fb.note]) {
    const [status, best, title] = QUIET_NOTES[fb.note];
    return { lvl: "mute", status, best, figure: "", title };
  }
  if (fb.note === "solver_unavailable") {
    return { lvl: "na", status: "● NOT GRADED", best: "solver unavailable", figure: "n/a", title: "Solver unavailable: this decision is not graded." };
  }
  return { lvl: "na", status: "● UNGRADED", best: "no solve for this spot", figure: "n/a", title: "No solve for this spot: ungraded." };
}

function freqBar(actions, colors) {
  return h("div", { class: "fb-bar", role: "img", "aria-label": actions.map((a) => `${a.token} ${pct(a.freq)}`).join(", ") },
    actions.map((a, k) => (a.freq > 0 ? h("span", { style: { flexGrow: a.freq, background: colors[k] }, title: `${a.token} ${pct(a.freq, 1)}` }) : null)));
}

/** RNG buckets on a 1-100 scale (passive left, aggressive right) with the drawn number as a marker. */
function bucketBar(fb, colorOf) {
  const width = (b) => b.to - b.from + 1;
  const hasRng = fb.rng !== null && fb.rng !== undefined;
  const label = fb.buckets.map((b) => `${tokenName(b.token)} ${b.from}–${b.to}`).join(", ") + (hasRng ? `; RNG ${fb.rng}` : "");
  return h("div", { class: "fb-bar", role: "img", "aria-label": `RNG buckets: ${label}` },
    fb.buckets.map((b) => h("span", { style: { flexGrow: width(b), background: colorOf(b.token) }, title: `${tokenName(b.token)} ${b.from}–${b.to}` })),
    hasRng ? h("span", { class: "rng-marker", "data-n": fb.rng, style: `left:${fb.rng - 0.5}%` }) : null);
}

function rngNote(fb, colorOf) {
  const pure = fb.pure ? h("span", { class: "badge", title: "one action at 99% or more: any number in its bucket matches" }, "pure spot") : null;
  if (fb.rng === null || fb.rng === undefined || !fb.rng_action) return pure ? h("p", { class: "fb-rng-note" }, pure) : null;
  const verdict = fb.matched_rng
    ? h("span", { class: "pos", title: "your action is the one whose bucket holds the RNG" }, "matched")
    : h("span", { class: "missed", title: "the RNG fell in another action's bucket" }, "missed");
  return h("p", { class: "fb-rng-note" }, `RNG ${fb.rng} landed in `, h("b", { style: { color: colorOf(fb.rng_action) } }, tokenName(fb.rng_action)),
    ` · you chose ${tokenName(fb.chosen)} · `, verdict, pure);
}

function fbHead(fb, heroCls) {
  const v = verdictParts(fb);
  const board = fb.board && fb.board.length ? fb.board : null;
  return h("div", { class: "fb-head" },
    h("div", { class: "fb-head-row" }, h("span", { class: "label" }, `${fb.street} · ${fb.position} · ${heroCls}`),
      h("span", { class: `fb-status ${v.lvl === "ok" ? "" : v.lvl === "na" ? "warn" : v.lvl}` }, v.status)),
    h("p", { class: "fb-verdict" }, h("span", { class: "chosen" }, tokenName(fb.chosen)), h("span", { class: "best" }, v.best),
      h("span", { class: `freq grade-${v.lvl}`, title: v.title, "aria-label": v.title }, v.figure)),
    fb.retry ? h("span", { class: "badge fb-retry" }, "retry · not counted") : null,
    board ? cards(board) : null);
}

/** Grid cell tooltip: class, then one swatch row per action with its share. */
const cellTip = (cells) => (cls, el) => {
  const c = cells[cls];
  showTip(el, h("div", {}, h("strong", {}, cls), c && c.rows
    ? h("ul", { class: "tip-list" }, c.rows.map(([name, color, f]) => h("li", {}, h("span", { class: "swatch", style: { background: color } }), `${name} ${pct(f, 1)}`)))
    : h("span", {}, " ", c ? c.label : "not in range")));
};

/** Preflop chart grid (charts view renderer): GTO Wizard spot or PokerCoaching chart spot. */
async function chartGrid(ref) {
  if (ref.kind === "gtow") {
    const spot = await cachedApi(`/api/gtow/spot?key=${encodeURIComponent(ref.key)}`);
    return { ...spotGrid(spot, `GTO Wizard ${ref.key}`), note: `GTO Wizard ${ref.key.replaceAll("|", " · ")}` };
  }
  const qs = new URLSearchParams({ table: ref.table, situation: ref.situation, position: ref.position });
  if (ref.other) qs.set("other", ref.other);
  const spot = await cachedApi(`/api/charts/pc?${qs}`);
  return { ...spotGrid(spot, `PokerCoaching ${spot.key}`), note: `PokerCoaching 100bb chart · ${spot.key}` };
}

async function solveGrid(ref) {
  const base = `/api/solves/${encodeURIComponent(ref.name)}`;
  const [meta, node] = await Promise.all([cachedApi(base), cachedApi(`${base}/nodes/${encodeURIComponent(ref.node_id)}`)]);
  const toks = node.actions.map((a) => a.token);
  const colors = actionColors(toks);
  const cells = {};
  for (const [cls, a] of Object.entries(aggregate(meta.combos, node))) {
    const tot = a.freq.reduce((s, x) => s + x, 0);
    if (!tot) continue;
    cells[cls] = { weight: a.reach / a.n, segments: a.freq.map((f, k) => ({ freq: f / tot, color: colors[k] })),
      label: toks.map((t, k) => `${tokenName(t)} ${pct(a.freq[k] / tot)}`).join(", "),
      rows: toks.map((t, k) => [tokenName(t), colors[k], a.freq[k] / tot]).filter((r) => r[2] > 0) };
  }
  const grid = renderGrid(cells, { caption: "Solver strategy by hand class", onFocus: cellTip(cells), onBlur: hideTip });
  return { grid, leg: legend(toks.map(tokenName), colors), note: "Solver strategy at this node; bar height = reach." };
}

/** Range grid toggle (open by default); returns {el, set(open)} so G can drive it. */
function gridSection(fb, heroCls, open, onToggle) {
  const ref = fb.grid_ref;
  if (!ref) {
    const src = PREFLOP_SOURCE[fb.preflop_source];
    return { el: h("p", { class: "note" }, src ? `No chart grid for this spot; graded by ${src}.` : "No range grid for this decision."), set: () => {} };
  }
  const out = h("div", { class: "fb-grid", hidden: true });
  const sign = h("span", { class: "mono", "aria-hidden": "true" }, "+");
  async function set(want) {
    out.hidden = !want;
    btn.setAttribute("aria-expanded", String(want));
    sign.textContent = want ? "−" : "+";
    if (!want || out.childElementCount) return;
    out.append(h("p", { class: "loading" }, "Loading grid…"));
    try {
      const { grid, leg, note } = await (ref.kind === "solve" ? solveGrid(ref) : chartGrid(ref));
      const mine = grid.querySelector(`[data-cls="${heroCls}"]`);
      if (mine) mine.classList.add("hero-cell");
      out.replaceChildren(h("div", { class: "grid-and-legend" }, grid, leg), h("p", { class: "note" }, note, ` · your hand ${heroCls} is outlined`));
    } catch (e) { out.replaceChildren(errorBox(e)); }
  }
  const btn = h("button", { type: "button", class: "btn ghost btn-row", "aria-expanded": "false", "aria-keyshortcuts": "G",
    onclick: () => { const want = out.hidden; set(want); onToggle(want); } },
    h("span", {}, `${ref.kind === "pc" ? "PokerCoaching chart" : "Range grid"} · ${heroCls} outlined`),
    h("span", { class: "fb-grid-keys" }, h("kbd", { class: "kbd-soft", "aria-hidden": "true" }, "G"), sign));
  set(open);
  return { el: h("div", { class: "fb-grid-wrap" }, btn, out), set };
}

/** "PF · Raise 2.5" (+ " · retry") */
export const pillLabel = (fb) => `${streetAbbr(fb.street)} · ${optLabel(fb, fb.chosen, false)}${fb.retry ? " · retry" : ""}`;
const levelOf = (fb) => GRADE_LEVEL[fb.grade] || "mute";

function thisHand(decs, sel, onPick) {
  const n = decs.length;
  return h("div", { class: "fb-hand" },
    h("div", { class: "fb-hand-head" }, h("span", { class: "label" }, "This hand"),
      h("span", { class: "fb-hand-count" }, n ? `${n} decision${n === 1 ? "" : "s"}` : "")),
    n ? h("div", { class: "fb-pills", role: "group", "aria-label": "Decisions this hand" }, decs.map((fb, i) =>
      h("button", { type: "button", class: "fb-pill", "aria-pressed": String(i === sel), onclick: () => onPick(i) },
        h("span", { class: `fb-dot lvl-${levelOf(fb)}`, "aria-hidden": "true" }), pillLabel(fb),
        h("span", { class: "visually-hidden" }, ` ${verdictParts(fb).status.replace("● ", "")}`))))
      : h("p", { class: "fb-rng-note" }, "Act once to see how the solver plays your hand here."));
}

/**
 * Feedback tab children. decs: this hand's graded decisions (oldest first); sel: shown index.
 * opts {heroCards, gridOpen, onGrid(open), onPick(i)} -> {nodes, grid: {set(open)} | null}.
 */
export function feedbackBody(decs, sel, opts) {
  const head = thisHand(decs, sel, opts.onPick);
  const fb = decs[sel];
  if (!fb) return { nodes: [head], grid: null };
  const colors = actionColors(fb.options.map((a) => a.token));
  const colorOf = (t) => colors[fb.options.findIndex((a) => a.token === t)] || actionColors([t])[0];
  const heroCls = fb.class || comboClass(opts.heroCards.join(""));
  const graded = !QUIET_NOTES[fb.note]; // quiet state: no buckets or RNG verdict
  const bar = !graded ? null : fb.buckets && fb.buckets.length ? bucketBar(fb, colorOf)
    : fb.options.some((a) => a.freq) ? freqBar(fb.options, colors) : null;
  const note = graded ? rngNote(fb, colorOf) : null;
  const grid = gridSection(fb, heroCls, opts.gridOpen, opts.onGrid);
  return { grid, nodes: [
    head,
    fbHead(fb, heroCls),
    bar || note ? h("div", { class: "fb-dist" }, bar, note) : null,
    h("ul", { class: "fb-actions fb-freqs" },
      h("li", { class: "head" }, h("span", {}), h("span", {}, "Action"), h("span", { class: "num" }, "Freq")),
      fb.options.map((a, k) => h("li", { class: a.token === fb.chosen ? "is-chosen" : null },
        h("span", { class: "swatch", style: { background: colors[k] } }),
        h("span", { class: "name" }, tokenName(a.token), a.token === fb.chosen ? h("small", {}, " · you") : null,
          a.token === fb.best ? h("small", {}, " · best") : null),
        h("span", { class: `num${a.token === fb.best ? " is-best" : ""}` }, a.freq === null ? "–" : pct(a.freq, 1))))),
    graded && fb.origin && !fb.origin.startsWith("gtow:") ? h("p", { class: "note" }, `Source: ${fb.origin}`) : null,
    grid.el,
  ].filter(Boolean) };
}
