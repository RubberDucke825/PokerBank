// Spot builder (/strategy/new): heads-up postflop spot from a preflop line (or hand-edited ranges),
// a 3-5 card board, a sizing preset or custom sizes; solved through /api/solves/new with live
// progress, then opened in the tree browser.
import { h, api, num, navigate, cards } from "../core.js";
import { rangeEditor, comboCount } from "../range_editor.js";
import { deck, randomCards, cardText } from "../deck.js";
import { pageHead } from "./ui.js";
import { progressPanel, runWithPanel, jobErrorBox, postJson } from "./strategy_jobs.js";

const STREETS = { 3: "flop", 4: "turn", 5: "river" };
const SIZE_STREETS = [["flop_bets", "Flop bets"], ["turn_bets", "Turn bets"], ["river_bets", "River bets"], ["raise", "Raises"]];
const PRESET_NOTES = {
  trainer: "Fast. Rough strategies, good enough to drill.",
  review: "Balanced. The default for reviewing a hand.",
  library: "Accurate. For spots you will come back to.",
};

const seg = (label, options, value, onPick) => {
  const el = h("div", { class: "seg", role: "group", "aria-label": label },
    options.map(([v, text]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(v === value),
      onclick: (e) => {
        el.querySelectorAll(".seg-opt").forEach((x) => x.setAttribute("aria-pressed", String(x === e.currentTarget)));
        onPick(v);
      } }, text)));
  return el;
};

const step = (n, title, ...body) => h("section", { class: "build-step", "aria-labelledby": `step-${n}` },
  h("header", { class: "step-head" }, h("span", { class: "step-n mono" }, String(n).padStart(2, "0")),
    h("h2", { id: `step-${n}` }, title)), ...body);

const parseSizes = (text) => {
  const t = text.trim().toLowerCase();
  if (t === "auto") return "auto";
  const xs = t.split(/[\s,]+/).filter(Boolean).map(Number);
  return xs.length && xs.every((x) => x > 0) ? xs : null;
};
const sizesText = (v) => (v === "auto" ? "auto" : (v || []).join(", "));

/** Prefill from the preflop navigator: ?table=6|8&depth=100|200&line=srp|3bp&positions=A,B (either order). */
const lineIdFrom = (lines, q) => {
  const want = (q.get("positions") || "").split(",").filter(Boolean).sort().join(",");
  const l = lines.find((x) => x.line === q.get("line") && [...x.positions].sort().join(",") === want);
  return l ? l.id : "srp:BTN:BB";
};

export async function strategyNewView(_params, q = new URLSearchParams()) {
  const table = q.get("table") === "8" ? 8 : 6;
  const [lines, presetInfo] = await Promise.all([api(`/api/solves/lines?table=${table}`), api("/api/solves/presets")]);
  const st = {
    table, depth: q.get("depth") === "200" ? 200 : 100, lineId: lineIdFrom(lines, q), line: null, board: [], preset: "review",
    edited: { oop: false, ip: false }, ranges: { oop: "", ip: "" }, pot: null, stack: null,
    custom: false, sizes: { ...presetInfo.default_sizes }, side: "oop",
  };
  let lineSeq = 0;
  let linesNow = lines;
  const presetOf = (n) => presetInfo.presets.find((p) => p.name === n) || {};

  // ---------------------------------------------------------------- 01 line
  const lineSel = h("select", { id: "nb-line", onchange: () => { st.lineId = lineSel.value; loadLine(); } });
  const drawLines = () => {
    const groups = { srp: "Single raised pot", "3bp": "3-bet pot" };
    lineSel.replaceChildren(...Object.entries(groups).map(([k, label]) => h("optgroup", { label },
      linesNow.filter((l) => l.line === k).map((l) => h("option", { value: l.id, selected: l.id === st.lineId }, l.label)))));
  };
  const potIn = h("input", { id: "nb-pot", type: "number", min: 0.5, step: 0.5, inputmode: "decimal",
    oninput: () => { st.pot = potIn.value ? Math.round(100 * +potIn.value) : null; summary(); } });
  const stackIn = h("input", { id: "nb-stack", type: "number", min: 1, step: 0.5, inputmode: "decimal",
    oninput: () => { st.stack = stackIn.value ? Math.round(100 * +stackIn.value) : null; summary(); } });
  const lineNote = h("p", { class: "note" });

  async function loadLine() {
    const mine = ++lineSeq;
    const l = linesNow.find((x) => x.id === st.lineId) || linesNow[0];
    st.lineId = l.id;
    try {
      const r = await api(`/api/solves/line?table=${st.table}&depth=${st.depth}&line=${l.line}&positions=${l.positions.join(",")}`);
      if (mine !== lineSeq) return;
      st.line = { ...r, id: l.id, label: l.label };
      st.pot = null; st.stack = null;
      potIn.value = num(r.pot / r.bb, 1); stackIn.value = num(r.stack / r.bb, 1);
      st.edited = { oop: false, ip: false };
      st.ranges = { oop: r.oop.range, ip: r.ip.range };
      lineNote.replaceChildren(`${r.oop.position} out of position, ${r.ip.position} in position. Ranges from `,
        h("span", { class: "mono" }, r.oop.origin), " / ", h("span", { class: "mono" }, r.ip.origin), ".");
      drawSide();
      await editor.setText(st.ranges[st.side]);
      summary();
    } catch (e) { lineNote.replaceChildren(jobErrorBox(e)); }
  }

  const lineStep = step(1, "Line",
    h("div", { class: "filters" },
      h("div", { class: "field" }, h("span", {}, "Table"), seg("Table", [[6, "6-max"], [8, "8-max"]], st.table, async (v) => {
        st.table = v;
        linesNow = await api(`/api/solves/lines?table=${v}`);
        if (!linesNow.some((l) => l.id === st.lineId)) st.lineId = linesNow.find((l) => l.id.endsWith(":BTN:BB"))?.id || linesNow[0].id;
        drawLines(); loadLine();
      })),
      h("div", { class: "field" }, h("span", {}, "Depth"), seg("Depth", [[100, "100 bb"], [200, "200 bb"]], st.depth, (v) => { st.depth = v; loadLine(); })),
      h("label", { class: "field grow", for: "nb-line" }, h("span", {}, "Preflop line"), lineSel)),
    h("div", { class: "filters" },
      h("label", { class: "field", for: "nb-pot" }, h("span", {}, "Pot (bb)"), potIn),
      h("label", { class: "field", for: "nb-stack" }, h("span", {}, "Effective stack (bb)"), stackIn)),
    lineNote);

  // ---------------------------------------------------------------- 02 ranges
  const editedTag = h("span", { class: "badge" });
  const editor = rangeEditor({ id: "nb-range", label: "Range", onChange: (text) => {
    if (!st.line) return;
    const side = st.side;
    if (text !== st.ranges[side]) { st.ranges = { ...st.ranges, [side]: text }; st.edited = { ...st.edited, [side]: true }; }
    drawEdited(); summary();
  } });
  const resetBtn = h("button", { type: "button", class: "btn ghost", onclick: () => {
    if (!st.line) return;
    st.ranges = { ...st.ranges, [st.side]: st.line[st.side].range };
    st.edited = { ...st.edited, [st.side]: false };
    editor.setText(st.ranges[st.side]); drawEdited();
  } }, "Reset to chart");
  const drawEdited = () => { editedTag.textContent = st.edited[st.side] ? "edited" : "chart"; editedTag.classList.toggle("is-edited", st.edited[st.side]); };
  function drawSide() {
    if (!st.line) return;
    const fresh = seg("Player", ["oop", "ip"].map((s) => [s, `${s.toUpperCase()} · ${st.line[s].position}`]), st.side, (v) => {
      st.side = v; editor.setText(st.ranges[v]); drawEdited();
    });
    rangesStep.querySelector(".seg-slot").replaceChildren(fresh);
    drawEdited();
  }
  const rangesStep = step(2, "Ranges",
    h("div", { class: "re-bar" }, h("div", { class: "seg-slot" }), editedTag, resetBtn),
    h("p", { class: "note" }, "Paint cells with the brush (click or drag), or paste range text. Both stay in sync."),
    editor.el);

  // ---------------------------------------------------------------- 03 board
  const slots = h("div", { class: "board-slots", "aria-live": "polite" });
  const boardDeck = deck({ label: "Board cards", onPick: (c) => {
    const i = st.board.indexOf(c);
    st.board = i >= 0 ? st.board.filter((x) => x !== c) : st.board.length < 5 ? [...st.board, c] : st.board;
    drawBoard();
  } });
  function drawBoard() {
    slots.replaceChildren(...[0, 1, 2, 3, 4].map((i) => {
      const c = st.board[i];
      const tag = i < 3 ? "flop" : i === 3 ? "turn" : "river";
      return h("button", { type: "button", class: `slot${c ? " filled" : ""}${i === 3 ? " gap" : ""}`, disabled: !c,
        "aria-label": c ? `Remove ${cardText(c)} (${tag})` : `${tag} slot, empty`,
        onclick: () => { st.board = st.board.slice(0, i); drawBoard(); } },
        c ? cards([c]) : h("span", { class: "card-slot" }), h("span", { class: "label" }, tag));
    }));
    boardDeck.update({ selected: st.board, blocked: [] });
    summary();
  }
  const boardStep = step(3, "Board",
    h("div", { class: "board-row" }, slots,
      h("div", { class: "board-btns" },
        h("button", { type: "button", class: "btn ghost", onclick: () => { st.board = randomCards(3); drawBoard(); } }, "Random flop"),
        h("button", { type: "button", class: "btn ghost", onclick: () => { st.board = []; drawBoard(); } }, "Clear"))),
    boardDeck.el,
    h("p", { class: "note" }, "Three cards start on the flop, four on the turn, five on the river. Click a slot to clear from it on."));

  // ---------------------------------------------------------------- 04 tree
  const presetNote = h("p", { class: "note" });
  const sizesBox = h("div", { class: "sizes", hidden: true });
  const sizeInputs = {};
  for (const [k, label] of SIZE_STREETS) {
    const inp = h("input", { id: `nb-${k}`, type: "text", value: sizesText(st.sizes[k]), "aria-describedby": "nb-sizes-help",
      oninput: () => { const v = parseSizes(inp.value); inp.toggleAttribute("aria-invalid", !v); if (v) st.sizes = { ...st.sizes, [k]: v }; summary(); } });
    sizeInputs[k] = inp;
    sizesBox.append(h("label", { class: "field", for: `nb-${k}` }, h("span", {}, `${label} (% pot)`), inp));
  }
  const allin = h("input", { id: "nb-allin", type: "number", min: 0.05, max: 1, step: 0.01, value: st.sizes.allin_threshold,
    oninput: () => { st.sizes = { ...st.sizes, allin_threshold: +allin.value }; } });
  const donk = h("input", { id: "nb-donk", type: "checkbox", checked: st.sizes.donk, onchange: () => { st.sizes = { ...st.sizes, donk: donk.checked }; } });
  sizesBox.append(
    h("label", { class: "field", for: "nb-allin" }, h("span", {}, "All-in threshold"), allin),
    h("label", { class: "check", for: "nb-donk" }, donk, " OOP may lead (donk)"),
    h("p", { class: "note", id: "nb-sizes-help" }, "Comma-separated pot percentages; flop also takes \"auto\" (by texture). A bet that commits more than the threshold of the stack becomes all-in."));
  const customToggle = h("input", { id: "nb-custom", type: "checkbox", onchange: () => { st.custom = customToggle.checked; sizesBox.hidden = !st.custom; summary(); } });
  const drawPreset = () => {
    const p = presetOf(st.preset);
    presetNote.replaceChildren(h("b", {}, `${p.max_iterations ?? "–"} iterations`), " max · stop at ",
      h("b", {}, `${p.accuracy_pct_pot ?? "–"}% pot`), " exploitability", p.max_bets_per_street ? ` · ${p.max_bets_per_street} bets per street` : "",
      ". ", PRESET_NOTES[st.preset] || "");
  };
  const treeStep = step(4, "Tree",
    h("div", { class: "field" }, h("span", {}, "Preset"), seg("Preset", presetInfo.presets.map((p) => [p.name, p.name]), st.preset, (v) => { st.preset = v; drawPreset(); summary(); })),
    presetNote,
    h("label", { class: "check", for: "nb-custom" }, customToggle, " Custom bet sizes"),
    sizesBox);

  // ---------------------------------------------------------------- summary + solve
  const ticket = h("div", { class: "ticket-body" });
  const status = h("div", { class: "ticket-status" });
  const solveBtn = h("button", { type: "button", class: "btn solve-btn", onclick: solve }, "Solve");
  const root = h("div", { class: "page builder" });

  function summary() {
    const l = st.line;
    if (!l) return;
    const bb = l.bb;
    const pot = st.pot ?? l.pot, stack = st.stack ?? l.stack;
    const street = STREETS[st.board.length];
    const p = presetOf(st.preset);
    const row = (k, v) => h("div", { class: "t-row" }, h("span", { class: "label" }, k), h("b", {}, v));
    ticket.replaceChildren(
      h("p", { class: "t-line" }, l.label),
      h("div", { class: "t-board" }, st.board.length ? cards(st.board) : h("span", { class: "muted" }, "No board yet")),
      row("Street", street || "pick 3-5 cards"),
      row("Pot", `${num(pot / bb, 1)} bb`), row("Stack", `${num(stack / bb, 1)} bb`), row("SPR", num(stack / pot, 1)),
      row(`OOP ${l.oop.position}`, `${num(comboOf("oop"), 0)} combos${st.edited.oop ? " · edited" : ""}`),
      row(`IP ${l.ip.position}`, `${num(comboOf("ip"), 0)} combos${st.edited.ip ? " · edited" : ""}`),
      row("Preset", `${st.preset} · ${p.max_iterations} it · ${p.accuracy_pct_pot}%`),
      row("Sizes", st.custom ? `F ${sizesText(st.sizes.flop_bets)} · T ${sizesText(st.sizes.turn_bets)} · R ${sizesText(st.sizes.river_bets)}` : "preset default"));
    solveBtn.disabled = !street;
  }
  const comboOf = (side) => (side === st.side ? comboCount(editor.weights()) : st.line[side].combos);

  async function solve() {
    const l = st.line;
    const street = STREETS[st.board.length];
    if (!l || !street) return;
    const body = { table: st.table, depth: st.depth, source: "gto", street, board: st.board, line: l.line,
      hero: l.oop.position, preset: st.preset, seats: [{ position: l.oop.position }, { position: l.ip.position }] };
    const edited = Object.fromEntries(["oop", "ip"].filter((s) => st.edited[s]).map((s) => [s, st.ranges[s]]));
    if (Object.keys(edited).length) body.ranges = edited;
    if (st.pot !== null && st.pot !== l.pot) body.pot = st.pot;
    if (st.stack !== null && st.stack !== l.stack) body.stack = st.stack;
    if (st.custom) body.sizes = st.sizes;
    const p = presetOf(st.preset);
    const panel = progressPanel({ title: `Solving ${st.board.map(cardText).join(" ")}`, target: p.accuracy_pct_pot, maxIterations: p.max_iterations });
    status.replaceChildren(panel.el);
    solveBtn.disabled = true;
    try {
      const name = await runWithPanel(panel, "/api/solves/new", body, () => root.isConnected);
      navigate(`/strategy/${encodeURIComponent(name)}`);
    } catch (e) {
      status.replaceChildren(panel.el, jobErrorBox(e));
      solveBtn.disabled = false;
    }
  }

  drawLines(); drawPreset(); drawBoard();
  root.append(
    pageHead("Strategy · new spot", "Build a spot", "Heads-up postflop, solved by TexasSolver. Strategies only, no EVs."),
    h("div", { class: "build-grid" },
      h("div", { class: "build-main" }, lineStep, rangesStep, boardStep, treeStep),
      h("aside", { class: "build-side", "aria-label": "Spot summary" },
        h("div", { class: "ticket" }, h("span", { class: "label" }, "Spot"), ticket, solveBtn, status),
        h("a", { href: "/strategy/library", "data-link": true, class: "back-link" }, "← Saved solves"))));
  await loadLine();
  return root;
}

// Kept for other views: cards a next-street deal needs, and that street's name.
export const cardsNeeded = (boardLen) => (boardLen === 0 ? 3 : boardLen < 5 ? 1 : 0);
export const nextStreet = (boardLen) => ["flop", "flop", "flop", "turn", "river"][boardLen] || null;
export { postJson };
