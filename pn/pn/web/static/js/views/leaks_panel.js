// Train "Spot importance" tab (Plan 14, the "Spot importance - V3" sheet): Formations | Textures | Spots, each row
// ranked by priority = freq × pot × (1 − accuracy)^p, scaled so the top row is 1 (/api/train/importance and
// /api/train/leaks, contracts/trainer.md "Plan 14 additions"). A p slider (formations p for Formations / Spots, textures
// p for Textures) and a Start | Avg pot switch sit above; both persist in pn.train.progress and go to the session
// (onParams) so its drill weights follow. Rows are buttons: a spot or formation row pins a spot drill (a formation pins
// its highest-priority spot), a texture row pins a texture (onPin / onPinTexture toggle; the caller talks to the server
// and calls setPinned). The hand's drilled spot gets a marker once the hand is over, never while hero is acting.
import { h, api, num, pct } from "../core.js";
import { loadPrefs, savePrefs } from "./train_util.js";

const TOP = 8;
const WINDOW = 1000;
const VIEWS = [["formations", "Formations"], ["textures", "Textures"], ["spots", "Spots"]];
const POTS = [["start", "Start"], ["avg", "Avg pot"]];
const P_MIN = 1, P_MAX = 5;
const SETTLE_MS = 250; // p / pot changes reach the server once the control settles
export const P_DEFAULTS = { p_form: 3, p_tex: 1, pot_mode: "start" };

const has = (x) => x !== null && x !== undefined;
const clampP = (v, d) => (Number.isInteger(v) && v >= P_MIN && v <= P_MAX ? v : d);

/** Saved practice parameters {p_form, p_tex, pot_mode}, defaults where missing or out of range. */
export function savedParams() {
  const p = loadPrefs();
  return { p_form: clampP(p.p_form, P_DEFAULTS.p_form), p_tex: clampP(p.p_tex, P_DEFAULTS.p_tex),
    pot_mode: POTS.some(([k]) => k === p.pot_mode) ? p.pot_mode : P_DEFAULTS.pot_mode };
}

function seg(label, opts, cur, onPick) {
  const id = `imp-${label.toLowerCase().replace(/\W+/g, "-")}`;
  const btns = opts.map(([k, t]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(k === cur()),
    onclick: () => { onPick(k); paint(); } }, t));
  const paint = () => btns.forEach((b, i) => b.setAttribute("aria-pressed", String(opts[i][0] === cur())));
  return { el: h("div", { class: "seg imp-seg", role: "group", "aria-label": label, id }, btns), paint };
}

/** {el, refresh(state), mark(state), setPinned({spot, texture}|null), focusRow(pin)}: refresh refetches (session start, hand end, p change). */
export function leaksPanel({ onPin, onPinTexture, onParams } = {}) {
  const prefs = loadPrefs();
  let view = VIEWS.some(([k]) => k === prefs.impView) ? prefs.impView : "formations";
  let params = savedParams();
  let spots = null, imp = null; // /leaks rows, /importance body (or Error)
  let seq = 0, last = null; // last refresh state
  let drilled = null; // the spot shown as drilled (only after the hand)
  let seen = null; // {hand, drill}: the hand's last non-null drill (the server clears it when the hand leaves the line)
  let pinned = { spot: null, texture: null };
  let all = false; // "Show all" expanded
  let settle = 0; // debounce timer for onParams + refresh

  const pKey = () => (view === "textures" ? "p_tex" : "p_form");
  const out = h("output", { class: "imp-p-out", for: "imp-p" });
  const pName = h("span", { class: "imp-p-table" });
  const range = h("input", { type: "range", id: "imp-p", min: P_MIN, max: P_MAX, step: 1, "aria-describedby": "imp-p-hint",
    oninput: () => { out.textContent = range.value; },
    onchange: () => setParam(pKey(), +range.value) });
  const viewSeg = seg("Importance table", VIEWS, () => view, (k) => {
    view = k; all = false; savePrefs({ impView: k }); paintP(); draw(); announce();
  });
  const potSeg = seg("Pot size", POTS, () => params.pot_mode, (k) => setParam("pot_mode", k));
  const body = h("div", { class: "leaks-side-body" }, h("p", { class: "loading" }, "Loading spot importance…"));
  // one-line summary on user changes only; the table itself is not a live region (it is rebuilt on every hand end)
  const status = h("p", { class: "visually-hidden", role: "status" });
  const el = h("section", { class: "leaks-side", "aria-labelledby": "leaks-side-title" },
    h("div", { class: "head" }, h("h3", { class: "label", id: "leaks-side-title" }, `Spot importance · last ${WINDOW} hands`),
      h("span", { class: "leaks-side-hint" }, "click a row to drill it")),
    h("div", { class: "imp-tools" },
      h("div", { class: "imp-p" },
        h("div", { class: "imp-p-head" }, h("label", { for: "imp-p", class: "label" }, "Practice parameter ", pName), out),
        range,
        h("p", { class: "note", id: "imp-p-hint" }, "Higher puts more value on your ability to improve; 1 takes your accuracy at face value.")),
      h("div", { class: "imp-pot" }, h("span", { class: "label" }, "Pot"), potSeg.el)),
    viewSeg.el,
    status,
    body,
    h("p", { class: "note" }, "priority = freq × pot × (1 − accuracy)^p, scaled so the top row is 1. A pinned row deals every next hand into it until you clear it."));

  function paintP() {
    range.value = String(params[pKey()]);
    out.textContent = range.value;
    pName.textContent = view === "textures" ? "· textures" : "· formations";
  }

  function announce() {
    const name = { formations: "Formations", textures: "Textures", spots: "Spots" }[view];
    status.textContent = `${name} ranked with p = ${params[pKey()]}, ${params.pot_mode === "avg" ? "average" : "start"} pot`;
  }

  function setParam(k, v) {
    if (params[k] === v) return;
    params = { ...params, [k]: v };
    savePrefs({ [k]: v });
    paintP();
    potSeg.paint();
    clearTimeout(settle);
    settle = setTimeout(() => {
      if (onParams) onParams(params);
      if (last) refresh(last);
      announce();
    }, SETTLE_MS);
  }

  function shownDrill(state) {
    if (state.drill) seen = { hand: state.hand_no, drill: state.drill };
    return state.hand_over && seen && seen.hand === state.hand_no ? seen.drill : null;
  }

  const prioCell = (p) => {
    const fill = h("span", { class: "imp-bar-fill" });
    if (has(p)) fill.style.setProperty("--p", String(Math.max(0, Math.min(1, p))));
    return h("span", { class: `num imp-prio${p === 1 ? " is-top" : ""}`, title: "priority (1 = top row)" },
      has(p) ? num(p, 2) : "–", h("span", { class: "imp-bar", "aria-hidden": "true" }, fill));
  };

  /** One row: {key, name, sub, freq, pot, score, n, priority, on, here, title, onclick}. */
  function rowEl(r) {
    return h("li", { class: [r.here && "is-drilled", r.on && "is-pinned"].filter(Boolean).join(" ") || null },
      h("button", { type: "button", class: "leak-row imp-row", "data-key": r.key, "aria-pressed": String(r.on), title: r.title, onclick: r.onclick },
        h("span", { class: "swatch", "aria-hidden": "true" }),
        h("span", { class: "leak-label" }, r.name, r.sub ? h("small", { class: "imp-sub" }, r.sub) : null,
          r.here ? h("span", { class: "visually-hidden" }, " (last hand's drill)") : null),
        h("span", { class: "num", title: "how often the spot comes up" }, pct(r.freq, has(r.freq) && r.freq < 0.1 ? 1 : 0)),
        h("span", { class: "num", title: "pot in bb" }, has(r.pot) ? num(r.pot, r.pot < 100 ? 1 : 0) : "–"),
        h("span", { class: "num imp-score", title: `accuracy, shrunk toward your overall accuracy (${r.n ?? 0} decisions)` },
          pct(r.score), h("small", { class: "n" }, `n ${r.n ?? 0}`)),
        prioCell(r.priority)));
  }

  const isHere = (spot) => drilled !== null && spot === drilled.spot;
  function spotRow(l) {
    const on = l.spot === pinned.spot;
    return { key: l.spot, name: l.label || l.spot, sub: l.formation && l.formation !== l.label ? l.formation : null, freq: l.freq,
      pot: l.pot_bb, score: l.score, n: l.n, priority: l.priority, on, here: isHere(l.spot) || (drilled && l.label === drilled.label),
      title: on ? `${l.spot} · click to stop drilling` : `${l.spot} · click to drill it every hand`, onclick: () => onPin && onPin(l) };
  }
  /** The formation's highest-priority spot row (from /leaks), else a bare {spot}. */
  function bestSpot(f) {
    const ids = f.spots || [];
    const rows = Array.isArray(spots) ? spots.filter((l) => ids.includes(l.spot)) : [];
    rows.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
    return rows[0] || (ids.length ? { spot: ids[0], label: f.formation } : null);
  }
  function formationRow(f) {
    const ids = f.spots || [];
    const on = ids.includes(pinned.spot);
    const best = bestSpot(f);
    return { key: f.formation, name: f.formation, sub: null, freq: f.freq, pot: f.pot_bb, score: f.score, n: f.n, priority: f.priority,
      on, here: ids.some(isHere),
      title: !best ? `${f.formation} · no drillable spot` : on ? `${f.formation} · click to stop drilling` : `${f.formation} · click to drill ${best.label || best.spot}`,
      onclick: () => {
        if (!onPin || !best) return;
        onPin(on ? (Array.isArray(spots) && spots.find((l) => l.spot === pinned.spot)) || { spot: pinned.spot } : best);
      } };
  }
  function textureRow(t) {
    const on = t.texture === pinned.texture;
    const what = t.details && t.details !== t.texture ? `${t.texture} flops (${t.details})` : `${t.texture} flops`;
    return { key: t.texture, name: t.texture, sub: null, freq: t.freq, pot: t.pot_bb, score: t.score, n: t.n,
      priority: t.priority, on, here: false,
      title: on ? `${what} · click to stop` : `${what} · click to drill postflop spots on them`,
      onclick: () => onPinTexture && onPinTexture(t) };
  }

  function current() {
    if (view === "spots") return { src: spots, list: Array.isArray(spots) ? spots.map(spotRow) : null, what: "spots" };
    const src = imp instanceof Error ? imp : imp && imp[view];
    if (!Array.isArray(src)) return { src: imp instanceof Error ? imp : imp ? new Error("this server has no importance tables yet") : null, list: null };
    return { src, list: src.map(view === "formations" ? formationRow : textureRow), what: view };
  }

  function draw() {
    const { src, list, what } = current();
    if (src instanceof Error) {
      body.replaceChildren(h("p", { class: "note" }, `Spot importance unavailable: ${src.message}`));
      return;
    }
    if (!list) return; // still loading
    if (!list.length) {
      body.replaceChildren(h("p", { class: "note" }, "Nothing to rank yet. Finish a hand to start the table."));
      return;
    }
    const name = { formations: "Formation", textures: "Texture", spots: "Spot" }[what];
    const head = h("li", { class: "cols", "aria-hidden": "true" }, h("span", {}), h("span", { class: "leak-label" }, name),
      h("span", { class: "num" }, "Freq"), h("span", { class: "num" }, "Pot"), h("span", { class: "num" }, "Score"), h("span", { class: "num" }, "Prio"));
    const shown = all ? list : list.filter((r, i) => i < TOP || r.on); // a pinned row below the fold stays visible
    const more = list.length > TOP ? h("button", { type: "button", class: "linkish leaks-more", "aria-expanded": String(all),
      onclick: () => { all = !all; draw(); } }, all ? `Show top ${TOP}` : `Show all ${list.length} ${what}`) : null;
    const had = body.contains(document.activeElement) ? document.activeElement : null;
    const key = had && had.dataset.key, wasMore = had && had.classList.contains("leaks-more");
    body.replaceChildren(...[h("ol", { class: "leaks-side-list imp-list" }, head, shown.map(rowEl)), more].filter(Boolean));
    // a rebuild (pin from the keyboard, refresh) keeps focus on the same row / button instead of dropping it to <body>
    const back = key ? rowFor(key) : wasMore ? body.querySelector(".leaks-more") : null;
    if (back) back.focus({ preventScroll: true });
  }

  const rowFor = (key) => body.querySelector(`.leak-row[data-key="${CSS.escape(key)}"]`);
  /** Focus the row of a pin ({spot, texture}) in the current view (a spot's formation in Formations), else the top row. */
  function focusRow(p) {
    const forms = imp && !(imp instanceof Error) && Array.isArray(imp.formations) ? imp.formations : [];
    const form = p && p.spot ? forms.find((f) => (f.spots || []).includes(p.spot)) : null;
    const keys = p ? [p.texture, p.spot, form && form.formation].filter(Boolean) : [];
    const row = keys.map(rowFor).find(Boolean) || body.querySelector(".leak-row");
    if (row) row.focus({ preventScroll: true });
    return !!row;
  }

  async function refresh(state) {
    const mine = ++seq;
    last = state;
    drilled = shownDrill(state);
    const q = new URLSearchParams({ window: WINDOW, table: state.table, depth: state.depth,
      p_form: params.p_form, p_tex: params.p_tex, pot: params.pot_mode });
    const get = (path) => api(`${path}?${q}`).catch((e) => e);
    const [l, i] = await Promise.all([get("/api/train/leaks"), get("/api/train/importance")]);
    if (mine !== seq) return;
    spots = l;
    imp = i;
    draw();
  }

  function mark(state) {
    const d = shownDrill(state);
    if ((d && d.spot) === (drilled && drilled.spot)) return;
    drilled = d;
    draw();
  }

  function setPinned(p) {
    const next = { spot: (p && p.spot) || null, texture: (p && p.texture) || null };
    if (next.spot === pinned.spot && next.texture === pinned.texture) return;
    pinned = next;
    draw();
  }

  paintP();
  return { el, refresh, mark, setPinned, focusRow, params: () => params };
}
