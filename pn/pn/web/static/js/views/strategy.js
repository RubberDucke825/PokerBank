// Tree browser (/strategy/:name?node=<tokens>&view=&cls=&cat=&side=&act=): GTO Wizard-style study view
// of one solve. Top strip of node cards across subgames; left: Strategy | Ranges | Breakdown tabs over the
// 13x13 grid; right: Actions tiles, Hands list (per combo), solve facts. Runout picker solves the next street.
// Keyboard: left/right select an action, Enter plays it, Backspace goes up one step.
import { h, api, num, pct, setQuery, navigate, errorBox, showTip, hideTip } from "../core.js";
import { renderGrid, actionColors, comboClass, ALL_CLASSES } from "../grid.js";
import { deck, CARD_RE, ALL_CARDS, cardText } from "../deck.js";
import { aggregate, actionLabel, frequencies, comboText, mixBar, composition,
  compositionPanel, metaRow, sum } from "./strategy_parts.js";
import { progressPanel, runWithPanel, jobErrorBox } from "./strategy_jobs.js";
import { lineStrip, streetName } from "./strategy_strip.js";
import { actionsBlock, handsBlock, actionCombos } from "./strategy_hands.js";

export { aggregate }; // train.js grid

const RANGE_COLOR = "var(--accent)";
const TIP_COMBOS = 12;
const TOP_HANDS = 40;
const VIEWS = ["strategy", "ranges", "breakdown"];
const enc = encodeURIComponent;
const CLASS_ORDER = new Map(ALL_CLASSES.map((c, i) => [c, i]));

/** [{name, meta}] from the root solve down to `meta` (subgames carry meta.parent). */
async function chainOf(meta) {
  const out = [{ name: meta.name, meta }];
  let cur = meta;
  for (let i = 0; cur.parent && i < 8; i++) {
    cur = await api(`/api/solves/${enc(cur.parent.name)}`);
    out.unshift({ name: cur.name, meta: cur });
  }
  return out;
}

const intOr = (v, d) => (v === null || v === "" || Number.isNaN(+v) ? d : +v);

export async function strategyBrowserView(params, q) {
  const name = params.name;
  const meta = await api(`/api/solves/${enc(name)}`);
  const chain = await chainOf(meta);
  const bb = (meta.meta && meta.meta.bb) || 100;
  const combos = meta.combos;
  const players = meta.players || [];
  const pos = (p) => (players[p] || {}).position || `P${p}`;
  const cache = new Map();
  const st = {
    tokens: (q.get("node") || "").split(",").filter(Boolean),
    view: VIEWS.includes(q.get("view")) ? q.get("view") : "strategy",
    sel: intOr(q.get("act"), -1), side: q.get("side") === "0" || q.get("side") === "1" ? +q.get("side") : null,
    cls: q.get("cls") || null, cat: q.get("cat") || null,
  };
  const root = h("div", { class: "page browser" });
  let seq = 0;
  let current = null; // {node, rng}

  const fetchNode = (tokens) => {
    const key = tokens.join(",");
    if (!cache.has(key)) {
      const p = Promise.all([api(`/api/solves/${enc(name)}/walk?path=${enc(key)}`), api(`/api/solves/${enc(name)}/ranges?path=${enc(key)}`)]);
      p.catch(() => cache.delete(key));
      cache.set(key, p);
    }
    return cache.get(key);
  };
  /** Walk result of a node in this solve or a parent solve on the chain (strip cards). */
  const walkAt = (solve, tokens) => {
    if (solve === name) return fetchNode(tokens).then((r) => r[0]);
    const key = `${solve}|${tokens.join(",")}`;
    if (!cache.has(key)) {
      const p = api(`/api/solves/${enc(solve)}/walk?path=${enc(tokens.join(","))}`);
      p.catch(() => cache.delete(key));
      cache.set(key, p);
    }
    return cache.get(key);
  };
  const boardAt = (tokens) => [...meta.board, ...tokens.filter((t) => CARD_RE.test(t))];

  function syncQuery() {
    setQuery({ node: st.tokens.join(",") || null, view: st.view === "strategy" ? null : st.view, cls: st.cls, cat: st.cat,
      side: st.side, act: st.sel >= 0 ? st.sel : null });
  }
  function go(tokens) {
    st.tokens = tokens; st.sel = -1; st.cls = null; st.cat = null; st.side = null;
    return draw();
  }
  function up() {
    if (st.tokens.length) return go(st.tokens.slice(0, -1));
    if (meta.parent) navigate(`/strategy/${enc(meta.parent.name)}?node=${enc(meta.parent.path.join(","))}`);
    return null;
  }

  // ---------------------------------------------------------------- top strip
  /** Strip items for every prefix of the line across the chain, fetched in parallel (cached per path). */
  async function lineItems() {
    const levels = chain.map((lvl, li) => ({ lvl, last: li === chain.length - 1,
      toks: li === chain.length - 1 ? st.tokens : chain[li + 1].meta.parent.path }));
    const walks = await Promise.all(levels.map(({ lvl, toks }) =>
      Promise.all(toks.map((t, i) => (CARD_RE.test(t) ? null : walkAt(lvl.name, toks.slice(0, i)))))));
    const items = [];
    let nBoard = chain[0].meta.board.length;
    const deal = (cs) => { nBoard += cs.length; items.push({ deal: cs, street: streetName(nBoard) }); };
    levels.forEach(({ lvl, last, toks }, li) => {
      if (li) deal(chain[li].meta.parent.cards);
      const mp = lvl.meta.players || [];
      toks.forEach((t, i) => {
        if (CARD_RE.test(t)) { deal([t]); return; }
        const node = walks[li][i];
        const jump = (tok) => {
          const target = [...toks.slice(0, i), tok];
          if (last) go(target); else navigate(`/strategy/${enc(lvl.name)}?node=${enc(target.join(","))}`);
        };
        items.push({ node, pos: (mp[node.player] || {}).position || "", taken: t, jump });
      });
    });
    return items;
  }

  // ---------------------------------------------------------------- grid
  /** Shown player and whether the grid shows its action mix (Strategy tab at its own decision). */
  function shown(node) {
    const acts = node.type === "action";
    if (acts && st.view !== "ranges") return { p: node.player, isActor: true };
    return { p: st.side !== null ? st.side : acts ? node.player : 0, isActor: false };
  }
  function catFilter(reach, board) {
    if (!st.cat) return null;
    const row = composition(combos, reach, board).rows.find((r) => r.key === st.cat);
    if (!row) { st.cat = null; return null; }
    const set = new Set(row.idx);
    return (i) => set.has(i);
  }

  /** Grid cells for the shown player: action mix when it acts here, its range otherwise. */
  function gridCells() {
    const { node, rng } = current;
    const board = boardAt(st.tokens);
    const { p, isActor } = shown(node);
    const reach = rng.players[p].reach || [];
    const keep = catFilter(reach, board);
    const agg = aggregate(combos, isActor ? node : { actions: [] }, reach, keep);
    const colors = node.type === "action" ? actionColors(node.actions.map((a) => a.token)) : [];
    const cells = {};
    for (const [cls, a] of Object.entries(agg)) {
      if (a.reach <= 0) continue;
      const weight = a.reach / a.n;
      if (!isActor) {
        cells[cls] = { weight, segments: [{ freq: 1, color: RANGE_COLOR }], label: `${pct(weight)} of combos`, sub: String(Math.round(100 * weight)) };
        continue;
      }
      const tot = sum(a.freq);
      const segs = a.freq.map((f, k) => ({ freq: tot ? f / tot : 0, color: colors[k] }));
      cells[cls] = { weight, label: node.actions.map((x, k) => `${actionLabel(x.token).name} ${pct(segs[k].freq)}`).join(", "),
        segments: st.sel < 0 ? segs : [segs[st.sel], { freq: 1 - segs[st.sel].freq, color: "transparent" }],
        sub: st.sel >= 0 ? String(Math.round(100 * segs[st.sel].freq)) : null };
    }
    return { cells, agg, colors, isActor, reach, board, p, keep };
  }

  let gridBox = null, handsBox = null;
  function paintGrid() {
    if (!current) return;
    const g = gridCells();
    syncQuery();
    drawHands(g);
    if (!gridBox) return;
    const grid = renderGrid(g.cells, { fill: "full", class: "gw", caption: g.isActor ? "Strategy by hand class" : `${pos(g.p)} range by hand class`,
      onFocus: (cls, el) => showTip(el, cellTip(cls, g)), onBlur: hideTip });
    grid.addEventListener("click", (e) => {
      const el = e.target.closest(".cell");
      if (!el) return;
      st.cls = st.cls === el.dataset.cls ? null : el.dataset.cls;
      grid.querySelectorAll(".is-selected").forEach((x) => x.classList.remove("is-selected"));
      if (st.cls) el.classList.add("is-selected");
      syncQuery();
      drawHands(g);
    });
    if (st.cls) { const el = grid.querySelector(`[data-cls="${st.cls}"]`); if (el) el.classList.add("is-selected"); }
    gridBox.replaceChildren(grid);
  }

  function cellTip(cls, g) {
    const a = g.agg[cls];
    if (!a || !a.combos.length) return h("div", {}, h("b", {}, cls), " not in range");
    const toks = g.isActor ? current.node.actions.map((x) => x.token) : [];
    return h("div", {}, h("b", {}, cls), h("span", { class: "muted" }, ` · ${a.combos.length} combos · ${pct(a.reach / a.n)} weight`),
      h("ul", { class: "tip-list" }, a.combos.slice(0, TIP_COMBOS).map((i) => h("li", {}, comboText(combos[i]),
        g.isActor ? [mixBar(current.node.strategy[i], g.colors, "mix-bar tip-bar"),
          toks.map((t, k) => h("span", { style: { color: g.colors[k] } }, pct(current.node.strategy[i][k])))]
          : h("span", {}, pct(g.reach[i]))))),
      a.combos.length > TIP_COMBOS ? h("span", { class: "muted" }, `+${a.combos.length - TIP_COMBOS} more · click for all`) : null);
  }

  /** Hands list: the clicked class's combos, else the top combos of the shown range, by weight. */
  function drawHands(g) {
    if (!handsBox) return;
    const node = current.node;
    const live = (i) => (g.reach[i] || 0) > 0 && (!g.keep || g.keep(i));
    const all = combos.map((_, i) => i).filter((i) => live(i) && (!st.cls || comboClass(combos[i]) === st.cls));
    all.sort((a, b) => g.reach[b] - g.reach[a] || CLASS_ORDER.get(comboClass(combos[a])) - CLASS_ORDER.get(comboClass(combos[b])));
    const strategy = node.type === "action" && g.p === node.player ? node.strategy : null;
    handsBox.replaceChildren(handsBlock({ title: st.cls || `${pos(g.p)} range`, idx: st.cls ? all : all.slice(0, TOP_HANDS), total: all.length,
      combos, reach: g.reach, strategy, colors: g.colors,
      onClear: st.cls ? () => { st.cls = null; paintGrid(); } : null }));
  }

  function playerSeg(node) {
    const { p } = shown(node);
    return h("div", { class: "seg", role: "group", "aria-label": "Show range of" }, [0, 1].map((i) =>
      h("button", { type: "button", class: "seg-opt", "aria-pressed": String(i === p),
        onclick: () => { st.side = i; st.cls = null; st.cat = null; draw(); } },
        `${i === 0 ? "OOP" : "IP"} ${pos(i)}${node.type === "action" && node.player === i ? " ·  acts" : ""}`)));
  }

  // ---------------------------------------------------------------- left column
  function tabBar(node, rng) {
    const { p } = shown(node);
    const setView = (v) => { st.view = v; draw(); };
    return h("div", { class: "view-tabs" },
      h("div", { role: "tablist", "aria-label": "View" }, VIEWS.map((v) =>
        h("button", { type: "button", role: "tab", class: "view-tab", "aria-selected": String(st.view === v), onclick: () => setView(v) },
          v[0].toUpperCase() + v.slice(1)))),
      h("span", { class: "tab-count mono" }, `${num(sum(rng.players[p].reach || []), 0)} combos`));
  }

  function leftCol(node, rng) {
    const out = [tabBar(node, rng)];
    if (st.view === "breakdown") {
      const { p, isActor } = shown(node);
      const comp = composition(combos, rng.players[p].reach, boardAt(st.tokens), isActor ? node : null);
      const colors = isActor ? actionColors(node.actions.map((a) => a.token)) : null;
      if (node.type !== "action") out.push(h("div", { class: "view-bar" }, playerSeg(node)));
      out.push(h("div", { class: "breakdown" }, compositionPanel({ comp, colors, title: `${pos(p)} range · hand classes`, active: st.cat,
        onPick: (k) => { st.cat = k; st.cls = null; if (k) st.view = "strategy"; draw(); } })));
      return out;
    }
    if (st.view === "ranges" || node.type !== "action") out.push(h("div", { class: "view-bar" }, playerSeg(node)));
    if (st.cat) out.push(h("p", { class: "note filter-note" }, "Filtered to ", h("b", {}, st.cat), " · ",
      h("button", { type: "button", class: "linkish", onclick: () => { st.cat = null; draw(); } }, "show all")));
    gridBox = h("div", { class: "grid-box" });
    out.push(gridBox);
    return out;
  }

  // ---------------------------------------------------------------- right column
  function select(k) {
    if (st.sel === k) return;
    st.sel = k;
    root.querySelectorAll(".act-tile").forEach((el, i) => { el.classList.toggle("is-sel", i === k); el.setAttribute("aria-pressed", String(i === k)); });
    const all = root.querySelector(".act-all");
    if (all) all.setAttribute("aria-pressed", String(k < 0));
    paintGrid();
  }

  function rightCol(node, rng) {
    const out = [];
    if (node.type === "action") {
      const reach = rng.players[node.player].reach || node.reach;
      const freqs = frequencies(node, aggregate(combos, node, reach));
      out.push(actionsBlock({ node, bb, colors: actionColors(node.actions.map((a) => a.token)), freqs, combosPer: actionCombos(node, reach),
        sel: st.sel, onSelect: select, onPlay: (k) => go([...st.tokens, node.actions[k].token]) }));
    } else if (node.type === "chance" || node.kind === "leaf") {
      out.push(boardAt(st.tokens).length < 5 || node.type === "chance" ? runoutPicker(node)
        : h("section", { class: "side-block" }, h("p", { class: "node-head" }, "River complete: showdown.")));
    } else {
      out.push(h("section", { class: "side-block" }, h("p", { class: "node-head" },
        `Hand over: ${node.kind === "fold" ? "fold" : String(node.kind).replace("_", " ")} · pot ${num(node.pot / bb, 1)} bb`)));
    }
    handsBox = h("div", { class: "hands-box", "aria-live": "polite" });
    out.push(handsBox, metaRow(meta));
    return out;
  }

  // ---------------------------------------------------------------- chance / leaf: runout picker
  function runoutPicker(node) {
    const board = boardAt(st.tokens);
    const isChance = node.type === "chance";
    const street = isChance ? node.street : streetName(board.length + 1).toLowerCase();
    const status = h("div", { class: "runout-status" });
    const pick = async (c) => {
      if (isChance) { go([...st.tokens, c]); return; }
      d.update({ blocked: board, selected: [c] });
      const panel = progressPanel({ title: `Solving the ${street} · ${cardText(c)}` });
      status.replaceChildren(panel.el);
      try {
        const child = await runWithPanel(panel, `/api/solves/${enc(name)}/subgame`, { node_id: node.id, cards: [c] }, () => root.isConnected);
        navigate(`/strategy/${enc(child)}`);
      } catch (e) { status.replaceChildren(panel.el, jobErrorBox(e)); }
    };
    const allowed = isChance ? new Set(Object.keys(node.children || {})) : null;
    const d = deck({ label: `Deal the ${street}`, onPick: pick, reasons: Object.fromEntries(board.map((c) => [c, "on board"])) });
    d.update({ blocked: isChance ? ALL_CARDS.filter((c) => !allowed.has(c)) : board, selected: [] });
    return h("section", { class: "side-block runout" },
      h("div", { class: "head" }, h("span", { class: "label" }, `Deal the ${street}`)),
      h("p", { class: "note" }, isChance ? "Pick a card to follow the solved runout." : "Pick a card: the next street is solved from here (a few minutes for a turn)."),
      d.el, status);
  }

  // ---------------------------------------------------------------- draw
  async function draw() {
    const mine = ++seq;
    let node, rng, items;
    try { [[node, rng], items] = await Promise.all([fetchNode(st.tokens), lineItems()]); } catch (e) {
      if (mine === seq) root.replaceChildren(errorBox(e), h("p", {}, h("a", { href: "/strategy/library", "data-link": true }, "← Saved solves")));
      return;
    }
    if (mine !== seq) return;
    current = { node, rng };
    if (node.type !== "action" || st.sel >= node.actions.length) st.sel = -1;
    gridBox = null;
    const strip = lineStrip({ board: boardAt(st.tokens), bb, items: [...items, { node, pos: node.type === "action" ? pos(node.player) : "" }],
      onRoot: () => (chain.length > 1 ? navigate(`/strategy/${enc(chain[0].name)}`) : go([])) });
    const main = leftCol(node, rng);
    const side = rightCol(node, rng);
    root.replaceChildren(h("div", { class: "strat-surface" }, strip,
      h("div", { class: "two-col" }, h("div", { class: "main-col" }, main), h("div", { class: "side-col" }, side))));
    paintGrid();
  }

  // ---------------------------------------------------------------- keyboard
  const onKey = (e) => {
    if (!root.isConnected) { document.removeEventListener("keydown", onKey); return; }
    if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey || !current) return;
    const node = current.node;
    const n = node.type === "action" ? node.actions.length : 0;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      if (!n || e.target.closest(".grid13")) return;
      e.preventDefault();
      const d = e.key === "ArrowRight" ? 1 : -1;
      select(((st.sel + 1 + d + n + 1) % (n + 1)) - 1); // cycles all -> a0 .. an-1 -> all
    } else if (e.key === "Enter" && n && st.sel >= 0 && !e.target.closest("button, a")) {
      e.preventDefault();
      go([...st.tokens, node.actions[st.sel].token]);
    } else if (e.key === "Backspace") {
      e.preventDefault();
      up();
    }
  };
  document.addEventListener("keydown", onKey);

  await draw();
  return root;
}
