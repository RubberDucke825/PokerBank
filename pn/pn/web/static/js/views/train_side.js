// Trainer side column: Feedback | Spot importance | Progress tabs, and the Progress tab (rolling stats from
// /api/train/stats: window, KPI grid, blunder-rate sparkline, breakdowns, this session's totals).
import { h, api, pct, errorBox } from "../core.js";
import { table, empty } from "./ui.js";
import { signed, signCls, loadPrefs, savePrefs } from "./train_util.js";

const ALL_HANDS = 1_000_000; // the stats API needs window >= 1; this covers every stored hand
const WINDOWS = [[250, "250"], [1000, "1000"], [ALL_HANDS, "all"]];
const STREETS = ["preflop", "flop", "turn", "river"];
const POSITIONS = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"];
const SVG_NS = "http://www.w3.org/2000/svg";
export const TABS = [["feedback", "Feedback"], ["leaks", "Spot importance"], ["progress", "Progress"]];

/**
 * Tab bar + one panel per tab. panels: {feedback, leaks, progress} elements. onChange(tab) after a switch.
 * -> {el (tab bar), panels: [el], tab(), select(tab), dot(level|null)}
 */
export function sideTabs(panels, initial, onChange) {
  let cur = TABS.some(([k]) => k === initial) ? initial : "feedback";
  const dotEl = h("span", { class: "tab-dot", hidden: true, "aria-hidden": "true" });
  const btns = TABS.map(([k, t]) => h("button", { type: "button", role: "tab", class: "view-tab side-tab", id: `tr-tab-${k}`,
    "aria-controls": `tr-panel-${k}`, onclick: () => select(k),
    onkeydown: (e) => { // arrow keys move between tabs (WAI-ARIA tabs)
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const next = TABS[(TABS.findIndex(([x]) => x === cur) + d + TABS.length) % TABS.length][0];
      select(next);
      btns[TABS.findIndex(([x]) => x === next)].focus();
    } }, t, k === "feedback" ? dotEl : null));
  const wrapped = TABS.map(([k]) => h("div", { class: "side-panel", role: "tabpanel", id: `tr-panel-${k}`, "aria-labelledby": `tr-tab-${k}` }, panels[k]));
  function paint() {
    TABS.forEach(([k], i) => {
      const on = k === cur;
      btns[i].setAttribute("aria-selected", String(on));
      btns[i].tabIndex = on ? 0 : -1;
      wrapped[i].hidden = !on;
    });
  }
  function select(k) {
    if (k === cur) return;
    cur = k;
    if (k === "feedback") dot(null);
    paint();
    onChange(k);
  }
  function dot(lvl) {
    dotEl.hidden = !lvl;
    dotEl.className = `tab-dot${lvl ? ` lvl-${lvl}` : ""}`;
  }
  paint();
  return { el: h("div", { class: "side-tabs", role: "tablist", "aria-label": "Side panel" }, btns), panels: wrapped, tab: () => cur, select, dot };
}

export function totalsEl(t) {
  const net = t.hero_net / 100; // cents
  const row = (k, v, c) => [h("dt", {}, k), h("dd", { class: c || "" }, v)];
  return h("dl", { class: "scores fb-totals" },
    row("Hands", t.hands), row("Graded decisions", t.decisions),
    row("Best · mixed · blunders", `${t.best} · ${t.mixed} · ${t.blunders}`),
    row("Blunder rate", pct(t.blunder_rate), t.blunders ? "neg" : ""),
    row("Avg preflop freq error", pct(t.avg_freq_error)),
    row("Chips won/lost (not graded)", `${signed(net)} bb`, signCls(net)));
}

/** "last 1000 hands · blunders 3% · mixed 12% · best 85% · RNG match 87%" from state.rolling (stats shape). */
export function rollingLine(r) {
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
    table(["", "Graded", "Best", "Blund", "RNG"].map((c, k) => h("th", { scope: "col", class: k ? "num" : "" }, c)),
      keys.map((k) => {
        const g = groups[k];
        return h("tr", {}, h("th", { scope: "row" }, k.replaceAll("_", " ")),
          h("td", { class: "num", title: `${g.decisions} decisions, ${g.graded} graded` }, g.graded),
          h("td", { class: "num" }, pct(g.best_rate)),
          h("td", { class: `num ${g.blunders ? "neg" : ""}`, title: `${g.blunders} blunders` }, pct(g.blunder_rate)),
          h("td", { class: "num", title: "RNG match rate" }, pct(g.rng_match_rate)));
      })));
}

function drillCounts(st) {
  if (st.drill_hands === undefined || st.drill_hands === null) return null;
  const natural = st.natural_hands ?? st.hands - st.drill_hands;
  const landed = st.drills_landed === undefined || st.drills_landed === null ? "" : ` (${st.drills_landed} reached the spot)`;
  return h("p", { class: "note drill-counts" }, `${st.drill_hands} drill hands${landed} · ${natural} natural hands in this window`);
}

function progressBody(st, session) {
  if (!st.hands) return [empty("No stored hands for this window yet. Finish a hand to start the record."), session];
  const kpi = (k, v, c) => h("div", { class: "kpi" }, h("span", { class: "label" }, k), h("b", { class: c || "" }, v));
  return [
    h("div", { class: "kpis kpi-grid" }, kpi("Hands", st.hands), kpi("Blunders", pct(st.blunder_rate), st.blunders ? "neg" : ""),
      kpi("Mixed", pct(st.mixed_rate)), kpi("Best", pct(st.best_rate)), kpi("RNG match", pct(st.rng_match_rate)),
      kpi("Graded", `${st.graded} / ${st.decisions}`)),
    drillCounts(st),
    st.slices && st.slices.length ? sparkline(st.slices) : null,
    breakdown("By street", st.by_street, STREETS), breakdown("By position", st.by_position, POSITIONS),
    breakdown("By situation", st.by_situation, []),
    session];
}

/** Progress tab: {el, refresh()}; refresh is a no-op unless isOpen() (fetches on open, window change, hand end). */
export function progressPanel(getState, isOpen) {
  const pref = loadPrefs();
  let win = WINDOWS.some(([w]) => w === pref.window) ? pref.window : 1000;
  let seq = 0;
  const body = h("div", { class: "prog-body", "aria-live": "polite" });
  const winBtns = WINDOWS.map(([w, t]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(w === win),
    onclick: () => {
      win = w;
      winBtns.forEach((b, k) => b.setAttribute("aria-pressed", String(WINDOWS[k][0] === win)));
      savePrefs({ window: win });
      refresh();
    } }, t));
  const el = h("div", { class: "train-progress" },
    h("div", { class: "prog-tools" }, h("span", { class: "label", id: "tr-win-label" }, "Window"),
      h("div", { class: "seg", role: "group", "aria-labelledby": "tr-win-label" }, winBtns),
      h("a", { href: "/train/performance", "data-link": true, class: "pf-link" }, "Performance by spot")),
    body);

  async function refresh() {
    if (!isOpen()) return;
    const state = getState();
    const my = ++seq;
    const session = h("div", { class: "fb-session" }, h("h3", { class: "label" }, "This session"), totalsEl(state.totals));
    if (!body.childElementCount) body.replaceChildren(h("p", { class: "loading" }, "Loading progress…"));
    try {
      const st = await api(`/api/train/stats?${new URLSearchParams({ window: String(win), source: "" })}`); // all sources, like state.rolling
      if (my === seq) body.replaceChildren(...progressBody(st, session).filter(Boolean));
    } catch (e) {
      if (my === seq) body.replaceChildren(errorBox(e), session);
    }
  }
  return { el, refresh };
}
