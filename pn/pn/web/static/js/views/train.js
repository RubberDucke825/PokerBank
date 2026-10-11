// Trainer: play hands at a 6/8-max table against GTO bots or modelled players, with a feedback
// drawer after every hero decision. State shape: contracts/trainer.md (amounts in cents).
import { h, api, cards, pct, num, setQuery, errorBox, showTip, hideTip } from "../core.js";
import { renderGrid, legend, actionColors, actionKind, comboClass } from "../grid.js";
import { aggregate } from "./strategy.js";
import { spotGrid } from "./charts.js";
import { pageHead, section, table, empty } from "./ui.js";
import { leaksPanel } from "./leaks_panel.js";

const SEATS = { 6: ["LJ", "HJ", "CO", "BTN", "SB", "BB"], 8: ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"] };
const SETUP_KEY = "pn.train.setup";
const PROGRESS_KEY = "pn.train.progress"; // {open, window}
const ALL_HANDS = 1_000_000; // the stats API needs window >= 1; this covers every stored hand
const WINDOWS = [[250, "250"], [1000, "1000"], [ALL_HANDS, "all"]];
const STREETS = ["preflop", "flop", "turn", "river"];
const DRILL_DEFAULT = 0.8;
const LEAKS_SHOWN = 10; // rows before "Show all"
const POSITIONS = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"];
const SVG_NS = "http://www.w3.org/2000/svg";
const POLL_MS = 500;
const PREFLOP_SOURCE = { pokercoaching: "PokerCoaching charts", gtowizard: "GTO Wizard frequencies", chart: "the hand-written charts" };
const KEY_KIND = { f: ["fold"], c: ["call"], x: ["check"], a: ["allin"] };

// ------------------------------------------------------------------ small helpers
const bb = (x) => `${num(x, x % 1 ? 1 : 0)}`;
const signed = (x) => `${x > 0 ? "+" : ""}${num(x, 1)}`;
const signCls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
const toBB = (state, cents) => cents / (state.bb || 100);
const heroToAct = (state) => state.legal_actions.length > 0;

function loadSetup() {
  try { return JSON.parse(localStorage.getItem(SETUP_KEY)) || {}; } catch { return {}; }
}
function saveSetup(s) {
  try { localStorage.setItem(SETUP_KEY, JSON.stringify(s)); } catch { /* storage blocked */ }
}
function loadProgress() {
  try { return JSON.parse(localStorage.getItem(PROGRESS_KEY)) || {}; } catch { return {}; }
}
function saveProgress(p) {
  try { localStorage.setItem(PROGRESS_KEY, JSON.stringify(p)); } catch { /* storage blocked */ }
}

/** "bet33" -> "Bet 33%", "raise" -> "Raise", "allin" -> "All-in". */
function tokenName(token) {
  const m = token.match(/^([a-z]+?)(\d+)?$/i);
  if (!m) return token;
  const word = { allin: "All-in", fold: "Fold", check: "Check", call: "Call", bet: "Bet", raise: "Raise" }[m[1]] || m[1];
  return m[2] ? `${word} ${m[2]}%` : word;
}

/** Keyboard letter -> index of the legal action (b = first bet, r = first raise). */
function shortcutFor(legal) {
  const keys = {};
  legal.forEach((a, k) => {
    const kind = actionKind(a.token);
    for (const [key, kinds] of Object.entries(KEY_KIND)) if (kinds.includes(kind) && !(key in keys)) keys[key] = k;
    if (kind === "raise") {
      const key = a.token.startsWith("bet") ? "b" : "r";
      if (!(key in keys)) keys[key] = k;
    }
  });
  return keys;
}

/** Last action per position on the current street ("fold" sticks across streets). */
function lastActions(state) {
  const out = {};
  for (const a of state.history) {
    if (a.street !== state.street && a.token !== "fold") continue;
    const name = a.allin ? "all-in" : tokenName(a.token).toLowerCase();
    out[a.position] = a.amount === null || a.amount === undefined ? name : `${name} ${bb(toBB(state, a.amount))}`;
  }
  return out;
}

// ------------------------------------------------------------------ setup
async function setupView(onStart, notice) {
  let players = [];
  try { players = await api("/api/train/players"); } catch { /* no DB: GTO bots only */ }
  const saved = loadSetup();
  const sel = (id, options, value) => h("select", { id },
    options.map(([v, t]) => h("option", { value: v, selected: String(v) === String(value) }, t)));
  const field = (id, text, el) => h("label", { class: "field", for: id }, h("span", {}, text), el);

  const tableSel = sel("tr-table", [[6, "6-max"], [8, "8-max"]], saved.table || 6);
  const depthSel = sel("tr-depth", [[100, "100 bb"], [200, "200 bb"]], saved.depth || 100);
  const rngBox = h("input", { type: "checkbox", id: "tr-rng", checked: saved.rng_enabled !== false });
  const drillPct = Math.round(100 * (typeof saved.drill_share === "number" ? saved.drill_share : DRILL_DEFAULT));
  const drillOut = h("output", { for: "tr-drill", class: "drill-out" }, `${drillPct}%`);
  const drillRange = h("input", { type: "range", id: "tr-drill", min: 0, max: 100, step: 5, value: drillPct,
    "aria-describedby": "tr-drill-note", oninput: () => { drillOut.textContent = `${drillRange.value}%`; } });
  const heroSel = h("select", { id: "tr-hero" });
  const opps = h("div", { class: "train-opps" });
  const oppOptions = [["", "GTO bot"], ...players.map((p) => [p.player_id, `${p.name} · ${p.hands} hands`])];

  const draw = () => {
    const n = +tableSel.value;
    const heroVal = heroSel.value || saved.hero_seat || "";
    heroSel.replaceChildren(...[["", "Random"], ...SEATS[n].map((p) => [p, p])].map(([v, t]) =>
      h("option", { value: v, selected: v === heroVal }, t)));
    const prev = [...opps.querySelectorAll("select")].map((s) => s.value);
    const was = prev.length ? prev : saved.opponents || [];
    opps.replaceChildren(...Array.from({ length: n - 1 }, (_, k) =>
      field(`tr-opp-${k}`, `Seat ${k + 1} left of hero`, sel(`tr-opp-${k}`, oppOptions, was[k] || ""))));
  };
  tableSel.addEventListener("change", draw);
  draw();

  const status = h("p", { class: "warn", role: "status" }, notice || "");
  const btn = h("button", { type: "submit", class: "btn" }, "Deal");
  const form = h("form", { class: "train-setup", onsubmit: async (e) => {
    e.preventDefault();
    const body = { table: +tableSel.value, depth: +depthSel.value, source: "gto", // the only trainer source (Plan 7)
      hero_seat: heroSel.value || null, opponents: [...opps.querySelectorAll("select")].map((s) => s.value || null),
      rng_enabled: rngBox.checked, drill_share: +drillRange.value / 100 };
    saveSetup(body);
    btn.disabled = true;
    status.textContent = "Dealing…";
    try { onStart(await api("/api/train/new", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })); }
    catch (err) { status.textContent = err.message; btn.disabled = false; }
  } },
    h("div", { class: "filters" }, field("tr-table", "Table", tableSel), field("tr-depth", "Depth", depthSel),
      field("tr-hero", "Hero seat", heroSel),
      h("label", { class: "check-field", for: "tr-rng" }, rngBox, h("span", {}, "RNG for mixed spots")),
      h("label", { class: "field drill-field", for: "tr-drill" }, h("span", {}, "Drill share"), h("span", { class: "drill-row" }, drillRange, drillOut))),
    h("p", { class: "note", id: "tr-drill-note" }, "Share of hands dealt into the spots where you blunder most (see Leaks under Progress). Drill hands are not labelled while you play."),
    h("p", { class: "note" }, "Opponents clockwise from hero. A named player only labels the seat: every villain plays ",
      "the GTO strategy (charts preflop, solver postflop)."),
    opps, h("div", { class: "filters" }, btn, status));
  return h("div", { class: "page" },
    pageHead("Trainer", "Play", "Six or eight handed against GTO bots. Every decision is graded by the solver's frequency for your action."),
    section("New session", form));
}

// ------------------------------------------------------------------ table
const DEALER_OFFSET = 0.42; // radians clockwise of the seat, so the button never sits under the seat's cards

function seatEl(state, s, idx, n, last) {
  const ang = Math.PI / 2 + (2 * Math.PI * idx) / n; // hero (seat 0) at the bottom, clockwise
  // Horizontal placement is resolved in CSS (train.css .train-table [style*="--cx"]) so a seat plate always
  // stays inside the table box whatever its width; r scales the ring (1 = seats, smaller = dealer/bets).
  const pos = (r, a = ang) => `--cx: ${(Math.cos(a) * r).toFixed(4)}; top: ${(50 + 46 * r * Math.sin(a) * 0.92).toFixed(2)}%`;
  const shown = s.is_hero ? state.hero.cards
    : state.result ? (state.result.showdown.find((x) => x.position === s.position) || {}).cards : null;
  const folded = !s.in_hand;
  const acting = s.position === state.to_act;
  const cls = ["seat", s.is_hero && "is-hero", folded && "is-folded", acting && "is-acting", s.allin && "is-allin"].filter(Boolean).join(" ");
  return [
    h("div", { class: cls, style: pos(1), "aria-label": `${s.position} ${s.name}` },
      shown ? cards(shown) : folded ? null : h("span", { class: "card-back", "aria-hidden": "true" }),
      h("div", { class: "seat-plate" },
        h("span", { class: "pos-tag" }, s.position),
        h("span", { class: "seat-name", title: s.name }, s.is_hero ? "You" : s.name),
        h("span", { class: "seat-stack" }, bb(toBB(state, s.stack)))),
      last ? h("span", { class: `seat-act act-${last.split(" ")[0].replace("-", "")}` }, last) : null),
    s.position === state.button ? h("span", { class: "dealer", style: pos(0.74, ang + DEALER_OFFSET), title: "Dealer button" }, "D") : null,
    s.committed > 0 ? h("span", { class: "bet-tag", style: pos(0.57) }, bb(toBB(state, s.committed))) : null,
  ];
}

function resultEl(state) {
  const r = state.result;
  const me = toBB(state, r.net[state.hero.position] || 0);
  const winners = Object.entries(r.won).map(([p, v]) => `${p} wins ${bb(toBB(state, v))} bb`).join(" · ");
  return h("div", { class: "train-result", role: "status" },
    h("p", { class: `train-result-big ${signCls(me)}` }, `${signed(me)} bb`),
    h("p", { class: "muted" }, r.hero_folded ? "You folded" : r.showdown.length ? "Showdown" : "Uncalled", winners ? ` · ${winners}` : ""));
}

function tableEl(state) {
  const n = state.seats.length;
  const last = lastActions(state);
  const board = state.result ? state.result.board : state.board;
  return h("div", { class: "train-table" },
    h("div", { class: "train-felt" }),
    h("div", { class: "train-center" },
      board.length ? cards(board) : h("span", { class: "label" }, state.street),
      h("p", { class: "train-pot" }, "POT ", h("b", {}, bb(toBB(state, state.result ? state.result.amount : state.pot))), " bb"),
      state.result ? resultEl(state) : null),
    state.seats.map((s, i) => seatEl(state, s, i, n, last[s.position])));
}

// ------------------------------------------------------------------ feedback column
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

// Backend notes for ungraded hero decisions shown as a quiet state (no verdict colour, buckets or RNG note).
const QUIET_NOTES = {
  multiway_unsolvable: ["Multiway, not graded", "the trainer grades 2-player streets only", "3+ players on this street: not solved, not graded."],
  combo_outside_range: ["Not graded", "your hand is outside the solved range", "Library flops are not widened to your cards: this hand is outside the solved range."],
  off_tree: ["Not graded", "the hand left the solved tree", "No leaf of the solved tree matches this line, so the street was not solved."],
};
// Frequency grades (contracts/trainer.md "Plan 7 additions"): best >= 50% of the top action's frequency,
// mixed >= 10%, blunder below. Level = the verdict colour class (components.css .fb-status / .grade-*).
const GRADE_LEVEL = { best: "ok", mixed: "warn", blunder: "bad" };
const GRADE_STATUS = { best: "● BEST", mixed: "● MIXED", blunder: "● BLUNDER" };

/** Verdict parts: status kicker, "best …" line and the chosen-action frequency, for every grading case. */
function verdictParts(fb) {
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

function fbHead(fb, heroCls) {
  const v = verdictParts(fb);
  const board = fb.board && fb.board.length ? fb.board : null;
  return h("div", { class: "fb-head" },
    h("div", { class: "fb-head-row" }, h("span", { class: "label" }, `${fb.street} · ${fb.position} · ${heroCls}`),
      h("span", { class: `fb-status ${v.lvl === "ok" ? "" : v.lvl === "na" ? "warn" : v.lvl}` }, v.status)),
    h("p", { class: "fb-verdict" }, h("span", { class: "chosen" }, tokenName(fb.chosen)), h("span", { class: "best" }, v.best),
      h("span", { class: `freq grade-${v.lvl}`, title: v.title, "aria-label": v.title }, v.figure)),
    board ? cards(board) : null);
}

function totalsEl(t) {
  const net = t.hero_net / 100; // cents
  const row = (k, v, c) => [h("dt", {}, k), h("dd", { class: c || "" }, v)];
  return h("dl", { class: "scores fb-totals" },
    row("Hands", t.hands), row("Graded decisions", t.decisions),
    row("Best · mixed · blunders", `${t.best} · ${t.mixed} · ${t.blunders}`),
    row("Blunder rate", pct(t.blunder_rate), t.blunders ? "neg" : ""),
    row("Avg preflop freq error", pct(t.avg_freq_error)),
    row("Chips won/lost (not graded)", `${signed(net)} bb`, signCls(net)));
}

const cellTip = (cells) => (cls, el) => showTip(el, h("div", {}, h("strong", {}, cls), " ", cells[cls] ? cells[cls].label : "not in range"));

/** Preflop chart grid (charts view renderer): GTO Wizard spot or PokerCoaching chart spot. */
async function chartGrid(ref) {
  if (ref.kind === "gtow") {
    const spot = await api(`/api/gtow/spot?key=${encodeURIComponent(ref.key)}`);
    return { ...spotGrid(spot, `GTO Wizard ${ref.key}`), note: `GTO Wizard ${ref.key.replaceAll("|", " · ")}` };
  }
  const qs = new URLSearchParams({ table: ref.table, situation: ref.situation, position: ref.position });
  if (ref.other) qs.set("other", ref.other);
  const spot = await api(`/api/charts/pc?${qs}`);
  return { ...spotGrid(spot, `PokerCoaching ${spot.key}`), note: `PokerCoaching 100bb chart · ${spot.key}` };
}

const metaCache = new Map();
async function solveGrid(ref) {
  const base = `/api/solves/${encodeURIComponent(ref.name)}`;
  if (!metaCache.has(ref.name)) metaCache.set(ref.name, await api(base));
  const meta = metaCache.get(ref.name);
  const node = await api(`${base}/nodes/${encodeURIComponent(ref.node_id)}`);
  const toks = node.actions.map((a) => a.token);
  const colors = actionColors(toks);
  const cells = {};
  for (const [cls, a] of Object.entries(aggregate(meta.combos, node))) {
    const tot = a.freq.reduce((s, x) => s + x, 0);
    if (!tot) continue;
    cells[cls] = { weight: a.reach / a.n, segments: a.freq.map((f, k) => ({ freq: f / tot, color: colors[k] })),
      label: toks.map((t, k) => `${t} ${pct(a.freq[k] / tot)}`).join(", ") };
  }
  const grid = renderGrid(cells, { caption: "Solver strategy by hand class", onFocus: cellTip(cells), onBlur: hideTip });
  return { grid, leg: legend(toks, colors), note: "Solver strategy at this node; bar height = reach." };
}

function gridSection(fb, heroCls) {
  const ref = fb.grid_ref;
  if (!ref) {
    const src = PREFLOP_SOURCE[fb.preflop_source];
    return h("p", { class: "note" }, src ? `No chart grid for this spot; graded by ${src}.` : "No range grid for this decision.");
  }
  const out = h("div", { class: "fb-grid", hidden: true });
  const sign = h("span", { class: "mono", "aria-hidden": "true" }, "+");
  const toggle = async () => {
    const open = out.hidden;
    out.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    sign.textContent = open ? "−" : "+";
    if (!open || out.childElementCount) return;
    out.append(h("p", { class: "loading" }, "Loading grid…"));
    try {
      const { grid, leg, note } = await (ref.kind === "solve" ? solveGrid(ref) : chartGrid(ref));
      const mine = grid.querySelector(`[data-cls="${heroCls}"]`);
      if (mine) mine.classList.add("hero-cell");
      out.replaceChildren(h("div", { class: "grid-and-legend" }, grid, leg), h("p", { class: "note" }, note, ` · your hand ${heroCls} is outlined`));
    } catch (e) { out.replaceChildren(errorBox(e)); }
  };
  const btn = h("button", { type: "button", class: "btn ghost btn-row", "aria-expanded": "false", onclick: toggle },
    h("span", {}, `${ref.kind === "pc" ? "PokerCoaching chart" : "Range grid"} · ${heroCls} outlined`), sign);
  if (ref.kind === "pc") toggle(); // the preflop reference is shown open
  return h("div", { class: "fb-grid-wrap" }, btn, out);
}

const sessionBlock = (state) => h("div", { class: "fb-session" }, h("h3", { class: "label" }, "This session"), totalsEl(state.totals));

/** Children of the feedback column (each one gets the column's side padding); `leaks` is the persistent leaks panel. */
function feedbackBody(state, leaks) {
  const fb = state.feedback;
  if (!fb) {
    return [h("div", { class: "fb-head" }, h("div", { class: "fb-head-row" }, h("span", { class: "label" }, "Feedback")),
      h("p", { class: "fb-rng-note" }, "Act once to see how the solver plays your hand here.")), leaks, sessionBlock(state)];
  }
  const colors = actionColors(fb.options.map((a) => a.token));
  const colorOf = (t) => colors[fb.options.findIndex((a) => a.token === t)] || actionColors([t])[0];
  const heroCls = fb.class || comboClass(state.hero.cards.join(""));
  const graded = !QUIET_NOTES[fb.note]; // quiet state: no buckets or RNG verdict
  const bar = !graded ? null : fb.buckets && fb.buckets.length ? bucketBar(fb, colorOf)
    : fb.options.some((a) => a.freq) ? freqBar(fb.options, colors) : null;
  const note = graded ? rngNote(fb, colorOf) : null;
  return [
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
    gridSection(fb, heroCls),
    leaks,
    sessionBlock(state),
  ].filter(Boolean);
}

// ------------------------------------------------------------------ rolling performance
/** "last 1000 hands · blunders 3% · mixed 12% · best 85% · RNG match 87%" from state.rolling (stats shape). */
function rollingLine(r) {
  if (!r || !r.hands) return [h("span", { class: "roll muted" }, "no stored hands yet")];
  const has = (x) => x !== null && x !== undefined;
  return [h("span", { class: "roll" }, ((n) => `last ${n} hand${n === 1 ? "" : "s"}`)(Math.min(r.hands, r.window || r.hands))),
    h("span", { class: `roll ${r.blunder_rate > 0 ? "neg" : ""}` }, `blunders ${pct(r.blunder_rate)}`),
    has(r.mixed_rate) ? h("span", { class: "roll" }, `mixed ${pct(r.mixed_rate)}`) : null,
    has(r.best_rate) ? h("span", { class: "roll" }, `best ${pct(r.best_rate)}`) : null,
    has(r.rng_match_rate) ? h("span", { class: "roll" }, `RNG match ${pct(r.rng_match_rate)}`) : null].filter(Boolean);
}

function svg(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/** Blunder rate per 100-hand slice, oldest left; the scale runs from 0 (dashed line) to the worst slice. */
function sparkline(slices) {
  const W = 320, H = 64, P = 6;
  const ys = slices.map((x) => x.blunder_rate || 0);
  const hi = Math.max(0.1, ...ys);
  const x = (k) => (slices.length === 1 ? W / 2 : P + (k * (W - 2 * P)) / (slices.length - 1));
  const y = (v) => P + ((hi - v) / hi) * (H - 2 * P);
  const pts = ys.map((v, k) => `${x(k).toFixed(1)},${y(v).toFixed(1)}`);
  const last = ys[ys.length - 1];
  const prev = ys.length > 1 ? ys[ys.length - 2] : last;
  const el = svg("svg", { class: "sparkline", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
    "aria-label": `blunder rate by 100-hand slice, oldest first: ${ys.map((v) => pct(v)).join(", ")}` });
  el.append(svg("line", { class: "spark-zero", x1: 0, x2: W, y1: y(0), y2: y(0) }));
  if (slices.length > 1) el.append(svg("polyline", { class: "spark-line", points: pts.join(" ") }));
  el.append(svg("path", { class: `spark-dot ${last > prev ? "neg" : "pos"}`, d: `M${pts[pts.length - 1]}l0,0` }));
  return h("figure", { class: "spark" }, el,
    h("figcaption", { class: "note" }, `Blunder rate · ${slices.length} slice${slices.length === 1 ? "" : "s"} of up to 100 hands, oldest left · `,
      `scale 0 to ${pct(hi)} · dashed line = 0 · latest `, h("span", { class: last > prev ? "neg" : "" }, pct(last))));
}

function breakdown(title, groups, order) {
  const keys = Object.keys(groups || {});
  const rank = (k) => (order.includes(k) ? order.indexOf(k) : order.length);
  keys.sort((a, b) => (order.length ? rank(a) - rank(b) : 0) || groups[b].decisions - groups[a].decisions);
  if (!keys.length) return null;
  return h("div", { class: "prog-table" }, h("h3", { class: "label fb-sub" }, title),
    table(["", "Graded", "Best", "Blunders", "RNG match"].map((c, k) => h("th", { scope: "col", class: k ? "num" : "" }, c)),
      keys.map((k) => {
        const g = groups[k];
        return h("tr", {}, h("th", { scope: "row" }, k.replaceAll("_", " ")),
          h("td", { class: "num", title: `${g.decisions} decisions, ${g.graded} graded` }, g.graded),
          h("td", { class: "num" }, pct(g.best_rate)),
          h("td", { class: `num ${g.blunders ? "neg" : ""}`, title: `${g.blunders} blunders` }, pct(g.blunder_rate)),
          h("td", { class: "num" }, pct(g.rng_match_rate)));
      })));
}

/** Leaks from /api/train/leaks (sorted by expected blunders per 100); weight bar relative to the heaviest drill. */
function leaksTable(leaks) {
  if (leaks instanceof Error) return h("div", { class: "prog-leaks" }, h("h3", { class: "label fb-sub" }, "Leaks"), errorBox(leaks));
  if (!leaks || !leaks.length) return h("div", { class: "prog-leaks" }, h("h3", { class: "label fb-sub" }, "Leaks"), empty("No graded spots yet."));
  const rows = [...leaks].sort((a, b) => (b.expected_blunders_per_100 || 0) - (a.expected_blunders_per_100 || 0));
  const maxW = Math.max(...rows.map((l) => l.weight || 0)) || 1;
  const head = [["Spot", ""], ["Reach", "how often you get here (solver reach)"], ["Blunder rate", "share of your decisions here graded blunder, shrunk toward the overall rate"],
    ["Blunders / 100 hands", "reach × blunder rate × 100"], ["Seen", "your graded decisions in this spot"], ["Drill weight", "share of drill hands"]]
    .map(([t, tip], k) => h("th", { scope: "col", class: k && k < 5 ? "num" : "", title: tip || null }, t));
  return h("div", { class: "prog-leaks" }, h("h3", { class: "label fb-sub" }, "Leaks"),
    table(head, rows.map((l, i) => h("tr", { class: i >= LEAKS_SHOWN ? "leak-more" : null, hidden: i >= LEAKS_SHOWN },
      h("th", { scope: "row", class: "leak-spot", title: l.spot }, l.label || l.spot),
      h("td", { class: "num", "data-label": "Reach" }, pct(l.freq, l.freq !== null && l.freq < 0.1 ? 1 : 0)),
      h("td", { class: "num", "data-label": "Blunder rate", title: `${l.blunders} of ${l.n} graded` }, pct(l.blunder_rate)),
      h("td", { class: `num ${l.expected_blunders_per_100 > 0 ? "neg" : ""}`, "data-label": "Blunders / 100" }, num(l.expected_blunders_per_100, 2)),
      h("td", { class: "num", "data-label": "Seen" }, l.n),
      h("td", { class: "leak-weight", "data-label": "Drill" }, h("span", { class: "leak-weight-in" },
        h("span", { class: "weight-bar", role: "img", "aria-label": `drill weight ${pct(l.weight)}` },
          h("span", { style: { transform: `scaleX(${(l.weight || 0) / maxW})` } })), h("span", { class: "muted" }, pct(l.weight, l.weight !== null && l.weight < 0.1 ? 1 : 0))))))),
    rows.length > LEAKS_SHOWN ? h("button", { type: "button", class: "linkish", "aria-expanded": "false", onclick: (e) => {
      const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
      e.currentTarget.closest(".prog-leaks").querySelectorAll(".leak-more").forEach((tr) => { tr.hidden = !open; });
      e.currentTarget.setAttribute("aria-expanded", String(open));
      e.currentTarget.textContent = open ? `Show top ${LEAKS_SHOWN}` : `Show all ${rows.length} spots`;
    } }, `Show all ${rows.length} spots`) : null,
    h("p", { class: "note" }, "Sorted by expected blunders per 100 hands. Drills favour heavy spots, with a bonus for spots you have rarely seen."));
}

function drillCounts(st) {
  if (st.drill_hands === undefined || st.drill_hands === null) return null;
  const natural = st.natural_hands ?? st.hands - st.drill_hands;
  const landed = st.drills_landed === undefined || st.drills_landed === null ? "" : ` (${st.drills_landed} reached the spot)`;
  return h("p", { class: "note drill-counts" }, `${st.drill_hands} drill hands${landed} · ${natural} natural hands in this window`);
}

function progressBody(st, session, leaks) {
  if (!st.hands) return [empty("No stored hands for this window yet. Finish a hand to start the record."), leaksTable(leaks), session];
  const kpi = (k, v, c) => h("div", { class: "kpi" }, h("span", { class: "label" }, k), h("b", { class: c || "" }, v));
  return [
    h("div", { class: "kpis" }, kpi("Hands", st.hands), kpi("Blunders", pct(st.blunder_rate), st.blunders ? "neg" : ""),
      kpi("Mixed", pct(st.mixed_rate)), kpi("Best", pct(st.best_rate)), kpi("RNG match", pct(st.rng_match_rate)),
      kpi("Graded", `${st.graded} / ${st.decisions}`)),
    drillCounts(st),
    st.slices && st.slices.length ? sparkline(st.slices) : null,
    leaksTable(leaks),
    h("div", { class: "prog-tables" }, breakdown("By street", st.by_street, STREETS),
      breakdown("By position", st.by_position, POSITIONS), breakdown("By situation", st.by_situation, [])),
    session];
}

/** Collapsible panel below the table; fetches /api/train/stats on open, on window change and at hand end. */
function progressPanel(getState) {
  const pref = loadProgress();
  let win = WINDOWS.some(([w]) => w === pref.window) ? pref.window : 1000;
  let seq = 0;
  const body = h("div", { class: "prog-body" });
  const save = () => saveProgress({ open: el.open, window: win });
  const winBtns = WINDOWS.map(([w, t]) => h("button", { type: "button", class: "btn ghost ev-src", "aria-pressed": String(w === win),
    onclick: () => { win = w; winBtns.forEach((b, k) => b.setAttribute("aria-pressed", String(WINDOWS[k][0] === win))); save(); refresh(); } }, t));
  const el = h("details", { class: "panel train-progress", open: !!pref.open, ontoggle: () => { save(); refresh(); } },
    h("summary", {}, h("span", { class: "label panel-title" }, "Progress"), h("span", { class: "note" }, "rolling performance, leaks and drill weights")),
    h("div", { class: "prog-tools", role: "group", "aria-label": "Window in hands" }, h("span", { class: "label" }, "Window"), winBtns),
    h("div", { "aria-live": "polite" }, body));

  async function refresh() {
    if (!el.open) return;
    const state = getState();
    const my = ++seq;
    const session = h("div", {}, h("h3", { class: "label fb-sub" }, "This session"), totalsEl(state.totals));
    if (!body.childElementCount) body.replaceChildren(h("p", { class: "loading" }, "Loading progress…"));
    try {
      const q = new URLSearchParams({ window: String(win), source: "" }); // all sources, like state.rolling
      const [st, leaks] = await Promise.all([api(`/api/train/stats?${q}`),
        api(`/api/train/leaks?${new URLSearchParams({ window: win, table: state.table, depth: state.depth })}`).catch((e) => e)]); // a leaks failure keeps the stats
      if (my === seq) body.replaceChildren(...progressBody(st, session, leaks).filter(Boolean));
    } catch (e) {
      if (my === seq) body.replaceChildren(errorBox(e), session);
    }
  }
  return { el, refresh };
}

function rngBadge(n, fresh) {
  return h("div", { class: `rng${fresh ? " fresh" : ""}`, role: "status", "aria-label": `Random number ${n}`,
    title: "Random number for mixed spots: higher = more aggressive" },
    h("span", { class: "label" }, "RNG"), h("b", {}, n));
}

// ------------------------------------------------------------------ session view
function playView(initial, onNewSession) {
  let state = initial;
  let busy = false;
  let timer = null;
  let solvingSince = null;
  let lastFbKey = null;
  let wasOver = initial.hand_over;
  let lastRngKey = null;
  let lastSolveErr = null;
  const progress = progressPanel(() => state);
  const leaks = leaksPanel();

  const summary = h("p", { class: "page-meta" });
  const rngHolder = h("div", { class: "rng-holder" });
  const tableHolder = h("div", {});
  const bar = h("div", { class: "train-actions", role: "group", "aria-label": "Your action" });
  const msg = h("p", { class: "warn", role: "alert" });
  const drillLine = h("p", { class: "drill-line" });
  const solveBanner = h("div", { class: "solve-banner", role: "alert", hidden: true });
  // Feedback is a fixed column at >= 1100px and stacks under the action bar below that; it never covers the table.
  const feedback = h("aside", { class: "side-col train-feedback", "aria-label": "Decision feedback", "aria-live": "polite" });

  const root = h("div", { class: "page train-page" },
    h("header", { class: "page-head" },
      h("div", {}, h("span", { class: "label" }, "Trainer"), h("h1", {}, "Play")),
      rngHolder,
      h("div", { class: "train-head-tools" },
        h("button", { type: "button", class: "btn ghost", onclick: () => { stop(); onNewSession(); } }, "New session")),
      summary, drillLine),
    h("div", { class: "train-surface" }, h("div", { class: "two-col" },
      h("div", { class: "main-col" }, tableHolder, solveBanner, bar, msg),
      feedback)),
    progress.el);

  function stop() {
    clearTimeout(timer);
    document.removeEventListener("keydown", onKey);
  }

  async function call(path, body) {
    if (busy) return;
    busy = true;
    draw();
    try {
      const opts = { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) };
      state = await api(path, opts);
      msg.textContent = "";
    } catch (e) { msg.textContent = e.message; }
    busy = false;
    draw();
  }

  const act = (token) => call(`/api/train/${state.sid}/act`, { token });
  const next = () => call(`/api/train/${state.sid}/next`);

  // Villains act server-side: poll only while a solve runs or someone else is to act.
  function schedule() {
    clearTimeout(timer);
    if (!state.solving) solvingSince = null;
    else if (solvingSince === null) solvingSince = Date.now();
    if (busy || !(state.solving || (!heroToAct(state) && !state.hand_over))) return;
    timer = setTimeout(async () => {
      if (!root.isConnected) return stop();
      try { state = await api(`/api/train/${state.sid}`); msg.textContent = ""; } catch (e) { msg.textContent = e.message; }
      draw();
    }, POLL_MS);
  }

  function drawBar() {
    const wait = (text) => h("p", { class: "train-wait" }, h("span", { class: "dot", "aria-hidden": "true" }), text);
    const hasRng = !state.hand_over && !state.solving && heroToAct(state)
      && state.rng_enabled !== false && state.rng !== null && state.rng !== undefined;
    const rngKey = hasRng ? `${state.hand_no}|${state.history.length}|${state.rng}` : null;
    rngHolder.replaceChildren(...(hasRng ? [rngBadge(state.rng, rngKey !== lastRngKey)] : []));
    lastRngKey = rngKey;
    if (state.hand_over) {
      bar.replaceChildren(h("button", { type: "button", class: "btn train-next", disabled: busy || state.solving, onclick: next },
        "Next hand ", h("kbd", {}, "N")));
    } else if (state.solving) {
      const secs = solvingSince ? Math.floor((Date.now() - solvingSince) / 1000) : 0;
      bar.replaceChildren(wait(`solving the ${state.street}… ${secs}s`));
    } else if (!heroToAct(state)) {
      bar.replaceChildren(wait(`${state.to_act || "table"} to act…`));
    } else {
      const legal = state.legal_actions;
      const keys = shortcutFor(legal);
      const letter = Object.fromEntries(Object.entries(keys).map(([k, i]) => [i, k]));
      const colors = actionColors(legal.map((a) => a.token));
      bar.replaceChildren(...legal.map((a, k) =>
        h("button", { type: "button", class: "action-btn", style: `--act: ${colors[k]}`, disabled: busy, onclick: () => act(a.token),
          "aria-keyshortcuts": [letter[k], k < 9 ? String(k + 1) : null].filter(Boolean).join(" ") },
          h("span", { class: "act-name" }, a.label || tokenName(a.token), // server labels already carry the amount
            !a.label && a.amount !== null && a.amount !== undefined ? h("span", { class: "act-sub" }, `${bb(toBB(state, a.amount))} bb`) : null),
          h("kbd", {}, (letter[k] || (k < 9 ? String(k + 1) : "")).toUpperCase()))));
    }
  }

  function draw() {
    setQuery({ sid: state.sid });
    const fallback = state.solve && (state.solve.status === "failed" || state.solve.status === "fallback");
    summary.replaceChildren(...[
      h("span", {}, `${state.table}-max · ${state.depth} bb · GTO`),
      h("span", { class: "muted" }, `hand ${state.hand_no}`), ...rollingLine(state.rolling),
      fallback ? h("span", { class: "warn", title: state.solve.note || "" }, "fallback policy (no solve)") : null,
      state.solve && state.solve.note === "no_library_solve"
        ? h("span", { class: "muted", title: "This heads-up line has no solved flop yet (pn library fill): the board ran out without postflop play." },
          "no solved flop, ran out") : null].filter(Boolean));
    const top = (state.rolling && state.rolling.leaks_top) || [];
    const share = state.drill_share ?? loadSetup().drill_share; // state has no drill_share yet: the saved setup is the best guess
    drillLine.hidden = !top.length || share === 0;
    drillLine.replaceChildren(h("span", { class: "label" }, "drilling"), " ", top.slice(0, 3).join(" · "));
    if ((state.solve_error || null) !== lastSolveErr) {
      lastSolveErr = state.solve_error || null;
      solveBanner.hidden = !lastSolveErr;
      if (lastSolveErr) solveBanner.replaceChildren(h("strong", {}, "Solver unavailable, this decision is not graded."),
      typeof lastSolveErr === "string" ? h("span", { class: "muted" }, ` ${lastSolveErr}`) : null);
    }
    tableHolder.replaceChildren(tableEl(state));
    drawBar();
    const fb = state.feedback;
    const fbKey = fb ? `${fb.hand}|${fb.street}|${fb.chosen}|${fb.pot}` : null;
    if (fbKey !== lastFbKey || !fbKey) feedback.replaceChildren(...feedbackBody(state, leaks.el));
    lastFbKey = fbKey;
    leaks.mark(state);
    if (state.hand_over && !wasOver) { progress.refresh(); leaks.refresh(state); }
    wasOver = state.hand_over;
    schedule();
  }

  function onKey(e) {
    if (!root.isConnected) return stop();
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest("input, select, textarea")) return;
    const k = e.key.toLowerCase();
    if (busy || state.solving) return;
    if (state.hand_over) {
      if (k === "n") { e.preventDefault(); next(); }
      return;
    }
    const legal = state.legal_actions;
    if (!legal.length) return;
    const idx = /^[1-9]$/.test(k) ? +k - 1 : shortcutFor(legal)[k];
    if (idx === undefined || !legal[idx]) return;
    e.preventDefault();
    act(legal[idx].token);
  }
  document.addEventListener("keydown", onKey);

  draw();
  leaks.refresh(state);
  return root;
}

export async function trainView(_params, q) {
  const app = () => document.getElementById("app");
  const play = (st) => app().replaceChildren(playView(st, () => startSetup()));
  const startSetup = async (notice) => {
    setQuery({ sid: null });
    app().replaceChildren(await setupView(play, notice));
  };
  const sid = q.get("sid");
  if (!sid) return setupView(play);
  try {
    return playView(await api(`/api/train/${encodeURIComponent(sid)}`), () => startSetup());
  } catch (e) {
    setQuery({ sid: null });
    return setupView(play, `Session ${sid} is gone (${e.message}). Start a new one.`);
  }
}
