// Trainer: play hands at a 6/8-max table against GTO bots or modelled players. Every hero decision gets an
// inline verdict under the action bar and a Feedback tab in the side column (with Leaks and Progress tabs).
// State shape: contracts/trainer.md (amounts in cents). Setup: train_setup.js; table: train_table.js;
// action bar + verdict: train_actions.js; feedback tab: train_feedback.js; tabs + progress: train_side.js.
import { h, api, setQuery, toast, shortcutsSheet, onKeys } from "../core.js";
import { leaksPanel, savedParams } from "./leaks_panel.js";
import { setupView } from "./train_setup.js";
import { tableEl } from "./train_table.js";
import { barContent, verdictEl, hintEl } from "./train_actions.js";
import { feedbackBody, GRADE_LEVEL } from "./train_feedback.js";
import { rangesPanel } from "./train_ranges.js";
import { sideTabs, progressPanel, rollingLine } from "./train_side.js";
import { heroToAct, loadSetup, loadPrefs, savePrefs, shortcutFor, TRAIN_KEYS } from "./train_util.js";

const POLL_MS = 500;
const AUTO_DEAL_S = 3; // auto-deal countdown, seconds
const CONFIRM_NEW_SESSION = true; // two-step "New session" once a decision was graded
const ARM_MS = 3500;
const NARROW = "(max-width: 1100px)"; // side column stacks under the table

const fbKey = (fb) => (fb ? `${fb.hand}|${fb.street}|${fb.chosen}|${fb.pot}|${fb.rng}|${!!fb.retry}` : null);
const POST = (body) => ({ method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) });

// ------------------------------------------------------------------ session view
function playView(initial, onNewSession) {
  let state = initial;
  let busy = false, pressed = null, acted = false;
  let timer = null, solvingSince = null;
  let wasOver = initial.hand_over, lastRngKey = null, lastSolveErr = null;
  const prefs = loadPrefs();
  let autoDeal = !!prefs.autoDeal;
  let gridOpen = true, gridCtl = null;
  let viewDec = null; // index into this hand's decisions; null = newest
  let lastVerdict = null, handNo = null, prevLen = 0;
  let countdown = null, cdTimer = 0; // seconds left while auto-deal counts down
  let armed = false, armTimer = 0;
  let barKey = null, nextLabel = null, fbRender = null, seenBoard = 0;
  let localPin; // pinned drill when the server has no pinned_drill field yet
  let pinHand = null, fallbackHand = null; // hand the pin was set during (not dealt for it); hand already toasted as a texture fallback
  let paramChain = Promise.resolve(); // POST /params go out one at a time, in order
  const acc = { hand: null, list: [] }; // per-hand feedback when the server has no hand_feedback yet

  const leaks = leaksPanel({ onPin: pin, onPinTexture: pinTexture, onParams: sendParams });
  const progress = progressPanel(() => state, () => tabs.tab() === "progress");
  const fbPanel = h("div", { class: "fb-panel" });
  const tabs = sideTabs({ feedback: fbPanel, leaks: leaks.el, progress: progress.el }, prefs.sideTab, (t) => {
    savePrefs({ sideTab: t });
    if (t === "progress") progress.refresh();
  });

  const summary = h("p", { class: "page-meta" });
  const rngHolder = h("div", { class: "rng-holder" });
  const drillChip = h("span", { class: "drill-chip", hidden: true });
  const newBtn = h("button", { type: "button", class: "btn ghost new-session", onclick: newSession }, "New session");
  const tableHolder = h("div", {});
  const bar = h("div", { class: "train-actions", role: "group", "aria-label": "Your action", tabindex: -1 });
  const verdictHolder = h("div", { class: "verdict-holder" });
  const rangesHolder = h("div", { class: "ranges-holder" });
  const hint = hintEl();
  const msg = h("p", { class: "warn", role: "alert" });
  const drillLine = h("p", { class: "drill-line" });
  const solveBanner = h("div", { class: "solve-banner", role: "alert", hidden: true });
  // Side column: fixed at >= 1100px, stacks under the action bar below that; it never covers the table.
  const side = h("aside", { class: "side-col train-feedback", "aria-label": "Decision feedback" }, tabs.el, tabs.panels);

  const root = h("div", { class: "page train-page" },
    h("header", { class: "page-head" },
      h("div", {}, h("span", { class: "label" }, "Trainer"), h("h1", {}, "Play")),
      rngHolder, drillChip,
      h("div", { class: "train-head-tools" }, h("a", { href: "/train/performance", "data-link": true, class: "pf-link" }, "Performance"), newBtn),
      summary, drillLine),
    h("div", { class: "train-surface" }, h("div", { class: "two-col" },
      h("div", { class: "main-col" }, tableHolder, solveBanner, bar, verdictHolder, rangesHolder, hint, msg),
      side)));

  function stop() {
    clearTimeout(timer);
    clearTimeout(armTimer);
    stopCountdown();
    offKeys();
  }

  /** POST and redraw; onOk runs after a success, before the redraw. */
  async function call(path, body, onOk) {
    if (busy) return false;
    busy = true;
    draw();
    let ok = true;
    try {
      state = await api(path, POST(body));
      msg.textContent = "";
      if (onOk) onOk();
    } catch (e) { msg.textContent = e.message; ok = false; }
    busy = false;
    pressed = null;
    if (root.isConnected) draw();
    return ok;
  }

  function act(token) {
    if (busy) return;
    pressed = token;
    acted = true;
    call(`/api/train/${state.sid}/act`, { token });
  }
  function next() {
    stopCountdown();
    call(`/api/train/${state.sid}/next`);
  }
  async function retry() {
    stopCountdown();
    // a rewind keeps hand_feedback (the abandoned try stays listed), so the verdict is cleared here
    if (await call(`/api/train/${state.sid}/retry`, null, () => { lastVerdict = null; viewDec = null; })) toast("Rewound to your last decision · this retry is not counted");
    else if (msg.textContent) toast(msg.textContent);
  }

  // ---------------------------------------------------------------- per-hand decisions
  /** This hand's graded decisions, oldest first (server hand_feedback, else accumulated from feedback). */
  function decisions() {
    if (Array.isArray(state.hand_feedback)) return state.hand_feedback;
    if (acc.hand !== state.hand_no) { acc.hand = state.hand_no; acc.list = []; }
    const fb = state.feedback;
    if (fb && fb.hand === state.hand_no && !acc.list.some((x) => fbKey(x) === fbKey(fb))) acc.list = [...acc.list, fb];
    return acc.list;
  }
  // one server snapshot per hero action on the current (possibly rewound) line
  const canRetry = () => !state.solving && state.history.some((a) => a.position === state.hero.position);

  // ---------------------------------------------------------------- drill pin
  const pinnedDrill = () => (state.pinned_drill !== undefined ? state.pinned_drill : localPin || null);
  const isPinned = (p) => !!(p && (p.spot || p.texture));
  /** "BTN vs BB - SRP · Ace flops" */
  const drillName = (p) => [p.spot && (p.label || p.spot), p.texture && `${p.texture} flops`].filter(Boolean).join(" · ");
  /** POST the pin {spot, texture} (either may be null; both null unpins). */
  async function setDrill(spot, texture, label) {
    try {
      const res = await api(`/api/train/${state.sid}/drill`, POST({ spot, texture }));
      // only the pin changes: merge it so an in-flight act / poll keeps the game state
      const fallback = spot || texture ? { spot, label: spot ? label || spot : null, texture } : null;
      const got = res && res.pinned_drill !== undefined ? res.pinned_drill : fallback;
      state = { ...state, pinned_drill: got };
      localPin = got;
      pinHand = state.hand_no;
      toast(isPinned(got) ? `Next hand drills ${drillName(got)}` : "Stopped drilling");
    } catch (e) { toast(e.message); }
    if (root.isConnected) draw();
  }
  // a texture only shapes flops: it combines with a postflop spot, and pinning a preflop spot drops it (and vice versa)
  const postflop = (spot) => typeof spot === "string" && spot.startsWith("post|");
  function pin(row) {
    const cur = pinnedDrill() || {};
    const spot = cur.spot === row.spot ? null : row.spot;
    return setDrill(spot, spot && !postflop(spot) ? null : cur.texture || null, row.label);
  }
  function pinTexture(row) {
    const cur = pinnedDrill() || {};
    const texture = cur.texture === row.texture ? null : row.texture;
    const spot = texture && !postflop(cur.spot) ? null : cur.spot || null;
    return setDrill(spot, texture, cur.label);
  }
  /** The chip is rebuilt on unpin: keep focus off <body> (the unpinned row in the importance tab, else New session). */
  async function unpinFromChip() {
    const was = pinnedDrill();
    await setDrill(null, null);
    if (!root.isConnected) return;
    if (!(tabs.tab() === "leaks" && leaks.focusRow(was))) newBtn.focus({ preventScroll: true });
  }
  /** The pinned texture had no solved flop for this hand's spot, so the server dealt another texture (contracts: dealt false). */
  const texFallback = (p) => !!(p && p.texture && p.dealt === false && state.hand_no !== pinHand
    && (state.result ? state.result.board : state.board).length >= 3);

  // ---------------------------------------------------------------- practice parameters (Plan 14)
  /** Drill weights follow the importance tab's p / pot from the next deal on; a server without the route is skipped.
   * A failed save keeps the server's params in state, so syncParams retries it at the next hand end. */
  function sendParams(p) {
    paramChain = paramChain.then(async () => {
      try {
        const res = await api(`/api/train/${state.sid}/params`, POST(p));
        state = { ...state, params: (res && res.params) || p };
      } catch (e) {
        if (/^not found$/i.test(e.message)) return; // FastAPI's bare 404: no such route (a missing session names itself)
        const sp = state.params;
        toast(`Couldn't save practice parameters (${e.message})${sp ? `: drills still use p ${sp.p_form} formations, ${sp.p_tex} textures, ${sp.pot_mode} pot` : ""}; retrying after this hand`);
      }
    });
    return paramChain;
  }
  /** A session started elsewhere (or before a change in another tab) adopts this viewer's saved parameters. */
  function syncParams() {
    const sp = state.params, mine = savedParams();
    if (sp && Object.keys(mine).some((k) => sp[k] !== mine[k])) sendParams(mine);
  }

  // ---------------------------------------------------------------- new session (two-step)
  function newSession() {
    if (CONFIRM_NEW_SESSION && !armed && (state.totals.decisions || 0) > 0) {
      armed = true;
      newBtn.classList.add("is-armed");
      newBtn.textContent = "End session? Click again";
      clearTimeout(armTimer);
      armTimer = setTimeout(disarm, ARM_MS);
      return;
    }
    stop();
    onNewSession();
  }
  function disarm() {
    armed = false;
    newBtn.classList.remove("is-armed");
    newBtn.textContent = "New session";
  }

  // ---------------------------------------------------------------- auto-deal
  function startCountdown() {
    if (countdown !== null || !state.hand_over || busy) return;
    countdown = AUTO_DEAL_S;
    barKey = null;
    drawBar();
    cdTimer = setInterval(() => {
      if (!root.isConnected) return stop();
      countdown -= 1;
      if (countdown <= 0) { next(); return; }
      if (nextLabel) nextLabel.textContent = `Next hand in ${countdown}s`;
    }, 1000);
  }
  function stopCountdown() {
    clearInterval(cdTimer);
    if (countdown === null) return;
    countdown = null;
    barKey = null;
    if (root.isConnected) drawBar();
  }
  function setAuto(on) {
    autoDeal = on;
    savePrefs({ autoDeal: on });
    if (on && state.hand_over) startCountdown();
    if (!on) stopCountdown();
  }

  // Villains act server-side: poll only while a solve runs or someone else is to act.
  function schedule() {
    clearTimeout(timer);
    if (!state.solving) solvingSince = null;
    else if (solvingSince === null) solvingSince = Date.now();
    if (busy || !(state.solving || (!heroToAct(state) && !state.hand_over))) return;
    timer = setTimeout(async () => {
      if (!root.isConnected) return stop();
      try { state = await api(`/api/train/${state.sid}`); msg.textContent = ""; } catch (e) { msg.textContent = e.message; }
      if (root.isConnected) draw();
    }, POLL_MS);
  }

  // ---------------------------------------------------------------- drawing
  function drawHead() {
    const hasRng = !state.hand_over && !state.solving && heroToAct(state)
      && state.rng_enabled !== false && state.rng !== null && state.rng !== undefined;
    const rngKey = hasRng ? `${state.hand_no}|${state.history.length}|${state.rng}|${state.replay ? 1 : 0}` : null;
    if (rngKey !== lastRngKey) {
      rngHolder.replaceChildren(...(hasRng ? [h("div", { class: "rng fresh", role: "status", "aria-label": `Random number ${state.rng}`,
        title: "Random number for mixed spots: higher = more aggressive" }, h("span", { class: "label" }, "RNG"), h("b", {}, state.rng))] : []));
    }
    lastRngKey = rngKey;
    const pinned = pinnedDrill();
    const on = isPinned(pinned);
    leaks.setPinned(pinned);
    drillChip.hidden = !on;
    const fell = texFallback(pinned);
    if (fell && fallbackHand !== state.hand_no) {
      fallbackHand = state.hand_no;
      toast(`No solved ${pinned.texture} flop for this spot: dealt another texture`);
    }
    // dealt as a pinned drill but hero (or the table) left its line: this hand plays on naturally (contracts: drill null)
    const offLine = on && pinned.dealt && !state.drill;
    if (on) {
      drillChip.replaceChildren(h("span", {}, "Drilling ", h("b", {}, drillName(pinned)),
        fell ? h("span", { class: "muted" }, " (fallback)") : offLine ? h("span", { class: "muted" }, " · this hand left the drill line") : null),
        h("button", { type: "button", class: "drill-chip-x", "aria-label": `Stop drilling ${drillName(pinned)}`,
          onclick: unpinFromChip }, "×"));
    }
    const fallback = state.solve && (state.solve.status === "failed" || state.solve.status === "fallback");
    summary.replaceChildren(...[
      h("span", {}, `${state.table}-max · ${state.depth} bb · GTO`),
      h("span", { class: "muted" }, `hand ${state.hand_no}`), ...rollingLine(state.rolling),
      state.replay ? h("span", { class: "badge", title: "Decisions in this replay are not counted" }, "replay") : null,
      fallback ? h("span", { class: "warn", title: state.solve.note || "" }, "fallback policy (no solve)") : null,
      state.solve && state.solve.note === "no_library_solve"
        ? h("span", { class: "muted", title: `${state.solve.detail || "This heads-up line has no solved flop yet (pn library fill)"}: the board ran out without postflop play.` },
          state.solve.detail && state.solve.detail.startsWith("limped") ? "limped pot not solved, ran out" : "no solved flop, ran out") : null].filter(Boolean));
    const top = (state.rolling && state.rolling.leaks_top) || [];
    const share = state.drill_share ?? loadSetup().drill_share; // state has no drill_share yet: the saved setup is the best guess
    drillLine.hidden = on || !top.length || share === 0;
    drillLine.replaceChildren(h("span", { class: "label" }, "drilling"), " ", top.slice(0, 3).join(" · "));
  }

  function drawBar() {
    const out = barContent(state, { busy, pressed, autoDeal, countdown, countdownTotal: AUTO_DEAL_S,
      solvingSecs: solvingSince ? Math.floor((Date.now() - solvingSince) / 1000) : 0, onAct: act, onNext: next, onAuto: setAuto });
    if (out.key === barKey) return;
    const hadFocus = bar.contains(document.activeElement);
    barKey = out.key;
    nextLabel = out.label;
    bar.replaceChildren(...out.nodes);
    if (hadFocus) bar.focus({ preventScroll: true }); // keep the keyboard here without landing on an action (Enter would play it)
  }

  function drawVerdict(decs) {
    verdictHolder.replaceChildren(...(lastVerdict ? [verdictEl(lastVerdict, {
      canRetry: GRADE_LEVEL[lastVerdict.grade] && lastVerdict.grade !== "best" && canRetry(),
      onRetry: retry, onWhy: why })] : []));
    hint.hidden = acted || decs.length > 0 || (state.totals.decisions || 0) > 0;
  }

  function drawFeedback(decs) {
    const sel = viewDec !== null && decs[viewDec] ? viewDec : decs.length - 1;
    const key = `${state.hand_no}|${decs.map(fbKey).join(",")}|${sel}`;
    if (key === fbRender) return;
    fbRender = key;
    const out = feedbackBody(decs, sel, { heroCards: state.hero.cards, gridOpen,
      onGrid: (open) => { gridOpen = open; },
      onPick: (i) => { viewDec = i; drawFeedback(decisions()); } });
    gridCtl = out.grid;
    fbPanel.replaceChildren(...out.nodes);
    const ranges = rangesPanel(decs[sel], state.hero.cards, { sid: state.sid, hand: state.hand_no });
    rangesHolder.replaceChildren(...(ranges ? [ranges] : []));
  }

  function why() {
    tabs.select("feedback");
    if (window.matchMedia(NARROW).matches) {
      const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      side.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
    }
  }

  function toggleGrid() {
    gridOpen = !gridOpen;
    tabs.select("feedback");
    if (gridCtl) gridCtl.set(gridOpen);
  }

  function draw() {
    setQuery({ sid: state.sid });
    const decs = decisions();
    if (state.hand_no !== handNo) { handNo = state.hand_no; prevLen = 0; lastVerdict = null; viewDec = null; seenBoard = 0; disarmIfIdle(); }
    if (decs.length > prevLen) {
      lastVerdict = decs[decs.length - 1];
      viewDec = null;
      if (tabs.tab() !== "feedback") tabs.dot(GRADE_LEVEL[lastVerdict.grade] || "mute");
    } else if (decs.length < prevLen) { lastVerdict = null; viewDec = null; } // rewound
    prevLen = decs.length;
    if ((state.solve_error || null) !== lastSolveErr) {
      lastSolveErr = state.solve_error || null;
      solveBanner.hidden = !lastSolveErr;
      if (lastSolveErr) solveBanner.replaceChildren(h("strong", {}, "Solver unavailable, this decision is not graded."),
        typeof lastSolveErr === "string" ? h("span", { class: "muted" }, ` ${lastSolveErr}`) : null);
    }
    drawHead();
    tableHolder.replaceChildren(tableEl(state, seenBoard));
    seenBoard = (state.result ? state.result.board : state.board).length;
    if (!state.hand_over && countdown !== null) stopCountdown();
    drawBar();
    drawVerdict(decs);
    drawFeedback(decs);
    leaks.mark(state);
    if (state.hand_over && !wasOver) {
      progress.refresh();
      leaks.refresh(state);
      syncParams(); // retries a practice-parameter save that failed during the hand
      if (autoDeal) startCountdown();
    }
    wasOver = state.hand_over;
    schedule();
  }
  function disarmIfIdle() { if (armed && !(state.totals.decisions > 0)) disarm(); }

  // ---------------------------------------------------------------- keys
  const offKeys = onKeys((e) => {
    const k = e.key.toLowerCase();
    if (e.key === "?") { e.preventDefault(); shortcutsSheet(TRAIN_KEYS); return; }
    if (e.key === "Escape") { if (countdown !== null) { e.preventDefault(); setAuto(false); drawBar(); } return; }
    if (k === "g") { e.preventDefault(); toggleGrid(); return; }
    if (busy || state.solving) return;
    if (k === "z") {
      if (canRetry()) { e.preventDefault(); retry(); }
      return;
    }
    const onControl = e.target instanceof Element && e.target.closest("button, a, summary, [role=tab]");
    if (state.hand_over) {
      if (k === "n" || ((e.key === " " || e.key === "Enter") && !onControl)) { e.preventDefault(); next(); }
      return;
    }
    const legal = state.legal_actions;
    if (!legal.length) return;
    const idx = /^[1-9]$/.test(k) ? +k - 1 : shortcutFor(legal)[k];
    if (idx === undefined || !legal[idx]) return;
    e.preventDefault();
    act(legal[idx].token);
  }, root);

  draw();
  if (autoDeal && state.hand_over) startCountdown(); // opened on a finished hand (reload, Back): resume auto-deal
  syncParams();
  leaks.refresh(state);
  progress.refresh();
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
