// Trainer helpers shared by train.js, train_setup.js, train_table.js, train_actions.js, train_feedback.js
// and train_side.js: formatting, action names, shortcut letters, saved preferences.
import { num } from "../core.js";
import { actionKind } from "../grid.js";

export const SEATS = { 6: ["LJ", "HJ", "CO", "BTN", "SB", "BB"], 8: ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"] };
const SETUP_KEY = "pn.train.setup";
const PREFS_KEY = "pn.train.progress"; // {window, sideTab, autoDeal}
const KEY_KIND = { f: ["fold"], c: ["call"], x: ["check"], a: ["allin"] };
const STREET_ABBR = { preflop: "PF", flop: "FLOP", turn: "TURN", river: "RIVER" };

export const bb = (x) => `${num(x, x % 1 ? 1 : 0)}`;
export const signed = (x) => `${x > 0 ? "+" : ""}${num(x, 1)}`;
export const signCls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
export const toBB = (state, cents) => cents / (state.bb || 100);
export const heroToAct = (state) => state.legal_actions.length > 0;
export const streetAbbr = (s) => STREET_ABBR[s] || String(s || "").toUpperCase();

function load(key) {
  try { return JSON.parse(localStorage.getItem(key)) || {}; } catch { return {}; }
}
function save(key, v) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* storage blocked */ }
}
export const loadSetup = () => load(SETUP_KEY);
export const saveSetup = (s) => save(SETUP_KEY, s);
export const loadPrefs = () => load(PREFS_KEY);
/** Merge into the saved trainer preferences (progress window, side tab, auto-deal). */
export const savePrefs = (patch) => save(PREFS_KEY, { ...load(PREFS_KEY), ...patch });

/** "bet33" -> "Bet 33%", "raise" -> "Raise", "allin" -> "All-in". */
export function tokenName(token) {
  const m = String(token).match(/^([a-z]+?)(\d+)?$/i);
  if (!m) return token;
  const word = { allin: "All-in", fold: "Fold", check: "Check", call: "Call", bet: "Bet", raise: "Raise" }[m[1]] || m[1];
  return m[2] ? `${word} ${m[2]}%` : word;
}

/** Feedback option label: "Raise 2.5 bb" (unit) / "Raise 2.5" (pill); sized bets keep their % ("Bet 33%"). */
export function optLabel(fb, token, unit = true) {
  const name = tokenName(token);
  if (/\d/.test(token) || token === "fold" || token === "check") return name;
  const opts = fb.options || [];
  const o = opts.find((a) => a.token === token);
  if (!o || o.amount === null || o.amount === undefined) return name;
  // amounts are street commitments after the action (cents, bb = 100); a call shows the added chips like the
  // button does, from hero's commitment before it (the fold option's amount)
  const fold = opts.find((a) => a.token === "fold");
  const amt = token === "call" && fold && typeof fold.amount === "number" ? o.amount - fold.amount : o.amount;
  return `${name} ${bb(amt / 100)}${unit ? " bb" : ""}`;
}

/** Keyboard letter -> index of the legal action (b = first bet, r = first raise). */
export function shortcutFor(legal) {
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

/** "raise 2.5 bb" for a history entry. */
export function actionText(state, a) {
  const name = a.allin ? "all-in" : tokenName(a.token).toLowerCase();
  return a.amount === null || a.amount === undefined ? name : `${name} ${bb(toBB(state, a.amount))}`;
}

/** Last action per position on the current street ("fold" sticks across streets). */
export function lastActions(state) {
  const out = {};
  for (const a of state.history) {
    if (a.street !== state.street && a.token !== "fold") continue;
    out[a.position] = actionText(state, a);
  }
  return out;
}

/** Hero's latest action this hand ("raise 2.5 bb"), or null. */
export function heroLast(state) {
  const pos = state.hero && state.hero.position;
  for (let k = state.history.length - 1; k >= 0; k--) {
    const a = state.history[k];
    if (a.position === pos) return `${actionText(state, a)}${a.amount === null || a.amount === undefined ? "" : " bb"}`;
  }
  return null;
}

/** Trainer shortcut sheet sections (core.js shortcutsSheet). */
export const TRAIN_KEYS = [
  { title: "Play", rows: [
    [["F", "C", "X"], "Fold, call, check"], [["R", "B", "A"], "Raise, bet, all-in (first size)"], [["1–9"], "Nth action button"],
    [["N", "Space", "↵"], "Next hand"], ["Z", "Retry the spot you just played (not counted)"], ["G", "Show / hide the range grid"],
    ["Esc", "Cancel the auto-deal countdown"], ["?", "This sheet"]] },
  { title: "Setup", rows: [["↵", "Deal"]] },
];
