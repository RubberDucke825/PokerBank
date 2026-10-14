// Preflop navigator (/strategy?table=6&depth=100&line=LJ:fold,HJ:raise): GTO Wizard-style study of
// preflop lines. Top: table / depth setup; a strip with one card per seat (click an option to act or
// to rewrite the line from that seat). Left: the actor's 13x13 strategy grid; right: Actions tiles.
// At the end of preflop the Actions column turns into the board picker: the heads-up flop's solved
// boards as tiles with a rank/card filter and texture chips (open in the tree browser), or a summary.
// Keyboard: 1-9 choose the nth option, Backspace removes the last step.
import { h, api, num, pct, cards, setQuery, navigate, errorBox, showTip, hideTip } from "../core.js";
import { renderGrid, actionColors, ALL_CLASSES } from "../grid.js";
import { actionsBlock } from "./strategy_hands.js";
import { seatStrip, bbText } from "./study_strip.js";

const COMBOS = 1326;
const MAX_KEY_OPTION = 9;
const POT_LABEL = { srp: "single raised pot", "3bp": "3-bet pot", "4bp+": "4-bet+ pot", limped: "limped pot" };
const enc = encodeURIComponent;

const seg = (label, options, value, onPick) => h("div", { class: "seg", role: "group", "aria-label": label },
  options.map(([v, text]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(v === value), onclick: () => onPick(v) }, text)));

/** "Kc9c8d" -> ["Kc", "9c", "8d"]. */
const boardCards = (b) => b.match(/../g) || [];
const RANKS = "AKQJT98765432";

export async function studyView(_params, q) {
  const st = {
    table: q.get("table") === "8" ? 8 : 6,
    depth: q.get("depth") === "200" ? 200 : 100,
    steps: (q.get("line") || "").split(",").filter(Boolean),
    sel: -1,
  };
  const root = h("div", { class: "page study" });
  const setupBox = h("div", { class: "study-setup" });
  const body = h("div", { class: "study-body" });
  root.append(setupBox, body);
  let data = null;
  let gridBox = null;
  let seq = 0;

  // ---------------------------------------------------------------- state changes
  const setLine = (steps) => { st.steps = steps; st.sel = -1; return load(); };
  const reset = () => setLine([]);
  /** Click on a seat's option: append for the actor, else rewrite from that seat's last step. */
  function pick(position, token) {
    const step = `${position}:${token}`;
    if (data && position === data.actor) return setLine([...st.steps, step]);
    const i = st.steps.map((s) => s.split(":")[0]).lastIndexOf(position);
    return i < 0 ? null : setLine([...st.steps.slice(0, i), step]);
  }
  const choose = (k) => {
    const actor = data && data.seats.find((s) => s.position === data.actor);
    if (actor && actor.options[k]) pick(actor.position, actor.options[k].token);
  };

  function drawSetup() {
    setupBox.replaceChildren(
      h("div", { class: "field" }, h("span", {}, "Table"),
        seg("Table", [[6, "6-max"], [8, "8-max"]], st.table, (v) => { if (v !== st.table) { st.table = v; reset(); } })),
      h("div", { class: "field" }, h("span", {}, "Depth"),
        seg("Depth", [[100, "100 bb"], [200, "200 bb"]], st.depth, (v) => { if (v !== st.depth) { st.depth = v; reset(); } })),
      h("div", { class: "study-links" },
        h("button", { type: "button", class: "btn ghost", onclick: reset, disabled: !st.steps.length }, "Reset line"),
        h("a", { href: "/strategy/library", "data-link": true, class: "back-link" }, "Library →")));
  }

  // ---------------------------------------------------------------- load
  async function load() {
    const mine = ++seq;
    const line = st.steps.join(",");
    setQuery({ table: st.table, depth: st.depth, line: line || null });
    drawSetup();
    let d;
    try {
      d = await api(`/api/study/preflop?table=${st.table}&depth=${st.depth}&line=${enc(line)}`);
    } catch (e) {
      if (mine !== seq) return;
      data = null;
      body.replaceChildren(errorBox(e), h("p", {}, h("button", { type: "button", class: "btn", onclick: reset }, "Reset line")));
      return;
    }
    if (mine !== seq) return;
    data = d;
    st.steps = d.line ? d.line.split(",") : [];
    setQuery({ line: d.line || null });
    drawSetup();
    draw();
  }

  // ---------------------------------------------------------------- grid
  const actorSeat = () => data.seats.find((s) => s.position === data.actor);
  function gridCells(toks, colors) {
    const cells = {};
    for (const cls of ALL_CLASSES) {
      const dist = (data.cells || {})[cls] || {};
      const segs = toks.map((t, k) => ({ freq: dist[t] || 0, color: colors[k] }));
      const one = st.sel >= 0 ? segs[st.sel] : null;
      cells[cls] = { weight: 1, segments: one ? [one, { freq: 1 - one.freq, color: "transparent" }] : segs,
        sub: one ? String(Math.round(100 * one.freq)) : null,
        label: toks.map((t, k) => `${t} ${pct(segs[k].freq)}`).join(", ") };
    }
    return cells;
  }
  function paintGrid() {
    if (!gridBox || !data.actor) return;
    const seat = actorSeat();
    const toks = seat.options.map((o) => o.token);
    const colors = actionColors(toks);
    const tip = (cls) => {
      const dist = (data.cells || {})[cls] || {};
      return h("div", {}, h("b", {}, cls), h("ul", { class: "tip-list" }, seat.options.map((o, k) =>
        h("li", {}, h("span", { style: { color: colors[k] } }, o.label), h("span", { class: "mono" }, pct(dist[o.token] || 0, 1))))));
    };
    gridBox.replaceChildren(renderGrid(gridCells(toks, colors), { fill: "full", class: "gw", caption: `${data.actor} strategy by hand class`,
      onFocus: (cls, el) => showTip(el, tip(cls)), onBlur: hideTip }));
  }
  function select(k) {
    if (st.sel === k) return;
    st.sel = k;
    root.querySelectorAll(".act-tile").forEach((el, i) => { el.classList.toggle("is-sel", i === k); el.setAttribute("aria-pressed", String(i === k)); });
    const all = root.querySelector(".act-all");
    if (all) all.setAttribute("aria-pressed", String(k < 0));
    paintGrid();
  }

  // ---------------------------------------------------------------- columns
  function leftActor() {
    const s = data.situation;
    gridBox = h("div", { class: "grid-box" });
    return [h("header", { class: "study-head" }, h("h2", {}, `${data.actor} to act`),
      s ? h("span", { class: `src-tag src-${s.source}`, title: s.origin }, s.label) : null,
      h("span", { class: "tab-count mono" }, `pot ${bbText(data.pot, data.bb)} bb`)), gridBox];
  }
  function rightActor() {
    const seat = actorSeat();
    const toks = seat.options.map((o) => o.token);
    const freqs = toks.map((t) => (data.summary || {})[t] || 0);
    const node = { actions: seat.options.map((o) => ({ token: o.token, amount: null, name: o.label })) };
    return actionsBlock({ node, bb: data.bb, colors: actionColors(toks), freqs, combosPer: freqs.map((f) => f * COMBOS), sel: st.sel,
      onSelect: select, onPlay: choose,
      keys: [h("kbd", {}, "1"), "–", h("kbd", {}, String(Math.min(MAX_KEY_OPTION, toks.length))), " choose ", h("kbd", {}, "⌫"), " back"] });
  }

  function endSummary(end) {
    const pot = `${bbText(end.pot, data.bb)} bb`;
    const m = end.matchup;
    const text = end.kind === "flop"
      ? `Flop: ${(m ? m.positions : end.players).join(" vs ")}${m ? `, ${POT_LABEL[m.pot_type] || m.pot_type}` : ""}, pot ${pot}`
      : `Hand decided preflop: ${end.players.join(", ")} wins ${pot}`;
    return [h("header", { class: "study-head" }, h("h2", {}, "Preflop over")),
      h("section", { class: "end-card", "aria-label": "End of preflop" },
        h("p", { class: "end-line" }, text),
        h("div", { class: "end-stacks" }, Object.entries(end.stacks || {}).map(([p, c]) =>
          h("span", {}, h("span", { class: "label" }, p), h("b", { class: "mono" }, `${bbText(c, data.bb)} bb`)))),
        end.note ? h("p", { class: "note" }, end.note) : null)];
  }

  // ---------------------------------------------------------------- board picker
  // At the end of preflop the Actions column becomes the board picker: the solved flops of the
  // heads-up matchup as tiles (weight = raw flops the canonical board stands for), narrowed by a
  // typed prefix ("K98" by ranks, "Kc9c8d" by exact cards) and texture chips. Enter opens the first.
  const TEXTURES = [["all", "All"], ["rainbow", "Rainbow"], ["two-tone", "Two-tone"], ["mono", "Mono"], ["paired", "Paired"]];
  const PAGE = 48;
  const bp = { query: "", texture: "all", shown: PAGE };
  const texture = (b) => {
    const cs = boardCards(b);
    const suits = new Set(cs.map((c) => c[1])).size;
    return { suits: suits === 3 ? "rainbow" : suits === 2 ? "two-tone" : "mono", paired: new Set(cs.map((c) => c[0])).size < 3 };
  };
  const matches = (b) => {
    const t = texture(b.board);
    if (bp.texture === "paired" ? !t.paired : bp.texture !== "all" && t.suits !== bp.texture) return false;
    const q = bp.query.replace(/\s+/g, "");
    if (!q) return true;
    const ranks = boardCards(b.board).map((c) => c[0]).join("");
    return /^[AKQJT2-9]+$/i.test(q) ? ranks.startsWith(q.toUpperCase()) : b.board.toLowerCase().startsWith(q.toLowerCase());
  };
  const open = (b) => navigate(`/strategy/${enc(b.solve)}`);

  function boardPicker(end) {
    const rankKey = (b) => boardCards(b).map((c) => String(RANKS.indexOf(c[0])).padStart(2, "0")).join("");  // A-high first
    const rows = (end.boards || []).slice().sort((a, b) => b.weight - a.weight || rankKey(a.board).localeCompare(rankKey(b.board)));
    const hits = rows.filter(matches);
    const box = h("div", { class: "act-tiles board-tiles", role: "list", "aria-label": "Solved flops" });
    const more = h("button", { type: "button", class: "chip", onclick: () => { bp.shown += PAGE; paint(); } });
    const count = h("span", { class: "muted mono" });
    const paint = () => {
      count.textContent = hits.length === rows.length ? `${rows.length} solved` : `${hits.length} of ${rows.length}`;
      box.replaceChildren(...hits.slice(0, bp.shown).map((b) => h("button", { type: "button", class: "act-tile board-tile", role: "listitem",
        title: `${b.board} · stands for ${num(b.weight, 0)} flops`, onclick: () => open(b) },
        cards(boardCards(b.board)), h("span", { class: "board-w mono" }, num(b.weight, 0)))));
      if (!hits.length) box.replaceChildren(h("p", { class: "note" }, rows.length ? "No solved flop matches." : "No solved flop for this matchup yet."));
      more.textContent = `Show ${Math.min(PAGE, hits.length - bp.shown)} more`;
      more.hidden = hits.length <= bp.shown;
    };
    const input = h("input", { type: "search", class: "board-q", placeholder: "K98 or Kc9c8d", value: bp.query, "aria-label": "Filter flops",
      oninput: (e) => { bp.query = e.target.value; bp.shown = PAGE; redraw(); },
      onkeydown: (e) => { if (e.key === "Enter" && hits[0]) open(hits[0]); } });
    const chips = h("div", { class: "board-chips", role: "group", "aria-label": "Texture" }, TEXTURES.map(([v, text]) =>
      h("button", { type: "button", class: "chip", "aria-pressed": String(bp.texture === v), onclick: () => { bp.texture = v; bp.shown = PAGE; redraw(); } }, text)));
    function redraw() { const q = input.value; paintPicker(end); const el = body.querySelector(".board-q"); if (el) { el.focus(); el.setSelectionRange(q.length, q.length); } }
    paint();
    return h("section", { class: "side-block acts-block board-pick", "aria-label": "Board" },
      h("div", { class: "head" }, h("span", { class: "label" }, "Board"), count),
      h("div", { class: "board-filter" }, input, chips),
      box, more,
      end.solvable && end.matchup ? h("button", { type: "button", class: "btn solve-btn", onclick: () => {
        const qs = new URLSearchParams({ table: st.table, depth: st.depth, street: "flop", line: end.matchup.pot_type, positions: end.players.join(",") });
        navigate(`/strategy/new?${qs}`);
      } }, "Solve another board") : null);
  }
  function paintPicker(end) {
    const old = body.querySelector(".board-pick");
    if (old) old.replaceWith(boardPicker(end));
  }

  function endBoards(end) {
    if (end.kind === "flop") return [boardPicker(end)];
    return end.solvable && end.matchup ? [h("section", { class: "side-block" }, h("p", { class: "note" }, "Solved flops are heads-up only."))] : [];
  }

  function draw() {
    gridBox = null;
    if (data.actor === null || data.actor === undefined) st.sel = -1;
    const end = data.end;
    const left = end ? endSummary(end) : leftActor();
    const right = end ? endBoards(end) : [rightActor()];
    body.replaceChildren(h("div", { class: "strat-surface" },
      seatStrip({ seats: data.seats, actor: data.actor, bb: data.bb, onPick: pick }),
      h("div", { class: "two-col" }, h("div", { class: "main-col" }, left), h("div", { class: "side-col" }, right))));
    paintGrid();
  }

  // ---------------------------------------------------------------- keyboard
  const onKey = (e) => {
    if (!root.isConnected) { document.removeEventListener("keydown", onKey); return; }
    if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (/^[1-9]$/.test(e.key)) { e.preventDefault(); choose(+e.key - 1); }
    else if (e.key === "Backspace" && st.steps.length) { e.preventDefault(); setLine(st.steps.slice(0, -1)); }
  };
  document.addEventListener("keydown", onKey);

  await load();
  return root;
}
