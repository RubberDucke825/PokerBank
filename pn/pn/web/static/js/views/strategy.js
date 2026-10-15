// Tree browser (/strategy/:name?node=<tokens>&view=&act=&cls=&cat=&hc=&bucket=&side=): GTO Wizard-style
// study view of one solve. Top strip: Back, then node cards across subgames (current actions play; the
// chance card opens a deal popover). Left: Strategy | Ranges | Breakdown tabs over the 13x13 grid with a
// filter-chip row. Right: Actions tiles (hover previews, click pins, Play / double-click / Enter plays),
// runout picker, Range equity and Hand classes panels (rows filter the grid), solve facts.
// Keys: left/right cycle the pinned action, 1-9 pin, Enter play, Backspace back, Esc clear, ? sheet.
import { h, api, num, pct, setQuery, navigate, errorBox, showTip, hideTip, onKeys, shortcutsSheet } from "../core.js";
import { renderGrid, actionColors, comboClass, ALL_CLASSES } from "../grid.js";
import { deck, CARD_RE, ALL_CARDS, cardText } from "../deck.js";
import { aggregate, actionLabel, frequencies, comboText, mixBar, composition,
  compositionPanel, metaRow, sum, filterChips } from "./strategy_parts.js";
import { progressPanel, runWithPanel, jobErrorBox } from "./strategy_jobs.js";
import { lineStrip, streetName, actionSize, dealPopover } from "./strategy_strip.js";
import { actionsBlock, actionCombos, syncTiles } from "./strategy_hands.js";
import { liveMask, comboClasses, hasClass, isClassName, liveCombos, classShares, classesPanel } from "./strategy_classes.js";
import { equityPanel, checkEquity, inBucket, isBucket, bucketLabel } from "./strategy_equity.js";

export { aggregate }; // train.js grid

const RANGE_COLOR = "var(--accent)";
const TIP_COMBOS = 12;
const HOVER_PREVIEW = true; // hovering a tile previews it in the grid
const VIEWS = ["strategy", "ranges", "breakdown"];
const enc = encodeURIComponent;
const SHORTCUTS = [{ title: "Strategy", rows: [
  [["←", "→"], "cycle All → action 1 … n"], ["1–9", "pin the nth action"], ["↵", "play the pinned action"],
  ["⌫", "back one step"], ["Esc", "close the popover, then clear the latest filter"], ["?", "show this sheet"]] }];

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
const valid = (v, ok) => (v && ok(v) ? v : null);

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
    sel: intOr(q.get("act"), -1), hover: -1, side: q.get("side") === "0" || q.get("side") === "1" ? +q.get("side") : null,
    cls: valid(q.get("cls"), (v) => ALL_CLASSES.includes(v)), cat: valid(q.get("cat"), isClassName),
    hc: valid(q.get("hc"), isClassName), bucket: valid(q.get("bucket"), isBucket),
  };
  const root = h("div", { class: "page browser" });
  let seq = 0, eqSeq = 0;
  let current = null; // {node, rng}
  let pop = null; // open deal popover {close}
  let eqState = { status: "loading", eq: null };
  const eqDone = new Map(); // path -> checked equity payload (or null when unavailable)

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
      hc: st.hc, bucket: st.bucket, side: st.side, act: st.sel >= 0 ? st.sel : null });
  }
  function go(tokens) {
    closePop(false);
    Object.assign(st, { tokens, sel: -1, hover: -1, cls: null, cat: null, hc: null, bucket: null, side: null });
    return draw();
  }
  const canUp = () => st.tokens.length > 0 || !!meta.parent;
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

  // ---------------------------------------------------------------- deal popover (chance nodes)
  function setTrigger(open) {
    const t = root.querySelector(".ln-pick");
    if (!t) return null;
    t.setAttribute("aria-expanded", String(open));
    t.classList.toggle("is-open", open);
    const caret = t.querySelector(".ln-caret");
    if (caret) caret.textContent = open ? "▲" : "▼";
    return t;
  }
  function closePop(refocus = true) {
    if (!pop) return;
    const inside = pop.el.contains(document.activeElement);
    pop.close();
    pop = null;
    const t = setTrigger(false);
    if (t && refocus && inside) t.focus({ preventScroll: true });
  }
  function togglePop() {
    if (pop) { closePop(); return; }
    const node = current && current.node;
    const anchor = root.querySelector(".ln-pick");
    if (!node || node.type !== "chance" || !anchor) return;
    hideTip();
    const board = boardAt(st.tokens);
    const allowed = new Set(Object.keys(node.children || {}));
    pop = dealPopover({ anchor, street: node.street, board, blocked: ALL_CARDS.filter((c) => !allowed.has(c)),
      onPick: (c) => go([...st.tokens, c]), onClose: () => closePop() });
    setTrigger(true);
  }

  // ---------------------------------------------------------------- filters
  /** Shown player and whether the grid shows its action mix (Strategy tab at its own decision). */
  function shown(node) {
    const acts = node.type === "action";
    if (acts && st.view !== "ranges") return { p: node.player, isActor: true };
    return { p: st.side !== null ? st.side : acts ? node.player : 0, isActor: false };
  }
  const eqOk = () => eqState.status === "ok" && eqState.eq;
  /** Combo filter for player p from the Breakdown group, hand class and equity bucket (null = none). */
  function comboFilter(p, board) {
    const cl = comboClasses(combos, board);
    const conds = [];
    if (st.cat) conds.push((i) => hasClass(cl[i], st.cat));
    if (st.hc) conds.push((i) => hasClass(cl[i], st.hc));
    if (st.bucket && eqOk()) { const pc = eqState.eq.players[p].per_combo; conds.push((i) => inBucket(pc[i], st.bucket)); }
    return conds.length ? (i) => conds.every((f) => f(i)) : null;
  }
  /** Chart dots: combos matching every active filter, per player (null when no cell / class / bucket is pinned). */
  function dotFilter(board) {
    if (!st.cls && !st.hc && !st.bucket) return null;
    const cl = comboClasses(combos, board);
    const eq = eqOk() ? eqState.eq : null;
    return (p, i) => (!st.cls || comboClass(combos[i]) === st.cls) && (!st.hc || hasClass(cl[i], st.hc))
      && (!st.cat || hasClass(cl[i], st.cat)) && (!st.bucket || (eq && inBucket(eq.players[p].per_combo[i], st.bucket)));
  }
  /** Breakdown tab has no grid: a filter change there rebuilds the left column. */
  function refilter() {
    if (st.view === "breakdown") return draw();
    paintGrid();
    paintPanels();
    return null;
  }
  /** Hand-class / bucket rows: filter, leaving Breakdown for the grid. */
  function pickFilter(changes) {
    Object.assign(st, changes, { cls: null });
    if (st.view === "breakdown") { st.view = "strategy"; draw(); } else refilter();
  }
  /** Esc: newest-first clear order (popover, cell, Breakdown group, hand class, bucket, pinned action). */
  function clearLatest() {
    if (pop) { closePop(); return true; }
    for (const k of ["cls", "cat", "hc", "bucket"]) if (st[k]) { st[k] = null; refilter(); return true; }
    if (st.sel >= 0) { setSel(-1); return true; }
    return false;
  }

  // ---------------------------------------------------------------- grid
  /** Grid cells for the shown player: action mix when it acts here, its range otherwise. */
  function gridCells() {
    const { node, rng } = current;
    const board = boardAt(st.tokens);
    const { p, isActor } = shown(node);
    const reach = rng.players[p].reach || [];
    const keep = comboFilter(p, board);
    const eff = st.hover >= 0 ? st.hover : st.sel;
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
        segments: eff < 0 ? segs : [segs[eff], { freq: 1 - segs[eff].freq, color: "transparent" }],
        sub: eff >= 0 ? String(Math.round(100 * segs[eff].freq)) : null };
    }
    return { cells, agg, colors, isActor, reach, board, p, keep };
  }

  let gridBox = null, chipsBox = null, countEl = null, actsEl = null, eqEl = null, hcEl = null;
  function paintGrid() {
    if (!current) return;
    const g = gridCells();
    syncQuery();
    const live = liveMask(combos, g.board);
    const all = liveCombos(g.reach, live), shownN = g.keep ? liveCombos(g.reach, live, g.keep) : all;
    if (countEl) { // " combos" drops on narrow screens when filtered (strategy.css)
      countEl.replaceChildren(g.keep ? `${num(shownN, 0)} of ${num(all, 0)}` : num(all, 0), h("span", { class: g.keep ? "tc-unit" : null }, " combos"));
      countEl.title = countEl.textContent;
    }
    if (chipsBox) chipsBox.replaceChildren(filterChips(chipList(g)) || "");
    const eff = st.hover >= 0 ? st.hover : st.sel;
    root.querySelectorAll(".ln-card.is-current .ln-act").forEach((el, k) => el.classList.toggle("is-hi", k === eff));
    syncTiles(actsEl, st);
    if (!gridBox) return;
    const grid = renderGrid(g.cells, { fill: "full", class: "gw", selected: st.cls,
      caption: g.isActor ? "Strategy by hand class" : `${pos(g.p)} range by hand class`,
      onFocus: (cls, el) => showTip(el, cellTip(cls, g)), onBlur: hideTip });
    grid.addEventListener("click", (e) => { const el = e.target.closest(".cell"); if (el) pinCell(el.dataset.cls); });
    gridBox.replaceChildren(grid);
  }
  /** Pin / unpin a grid cell in place (cells are unchanged; renderGrid's `selected` marks it on repaint). */
  function pinCell(cls) {
    const grid = gridBox && gridBox.querySelector(".grid13");
    const el = grid && grid.querySelector(`[data-cls="${cls}"]`);
    if (!el || (st.cls !== cls && el.classList.contains("empty"))) return; // nothing to pin
    st.cls = st.cls === cls ? null : cls;
    grid.classList.toggle("has-pin", !!st.cls);
    grid.querySelectorAll(".cell").forEach((c) => {
      const on = c.dataset.cls === st.cls;
      c.classList.toggle("is-selected", on);
      if (st.cls) c.setAttribute("aria-selected", String(on)); else c.removeAttribute("aria-selected");
    });
    syncQuery();
    if (chipsBox) chipsBox.replaceChildren(filterChips(chipList(gridCells())) || "");
    paintPanels();
  }

  function chipList(g) {
    const node = current.node;
    const out = [];
    const clear = (k) => () => { st[k] = null; return refilter(); };
    if (st.sel >= 0 && node.type === "action") {
      const s = actionSize(node.actions[st.sel], bb);
      out.push({ label: `${s.name}${s.size ? ` ${s.size}` : ""}`, swatch: g.colors[st.sel], onClear: () => setSel(-1) });
    }
    if (st.cat) out.push({ label: st.cat, swatch: "var(--accent)", onClear: clear("cat") });
    if (st.hc) out.push({ label: st.hc, swatch: "var(--accent)", onClear: clear("hc") });
    if (st.bucket) out.push({ label: `${pos(g.p)} equity ${bucketLabel(st.bucket)}`, swatch: "var(--accent)", onClear: clear("bucket") });
    if (st.cls) out.push({ label: st.cls, swatch: "var(--ink)", onClear: clear("cls") });
    // The chips are rebuilt on clear: move focus to the next chip's x, else the grid's tab stop (not <body>).
    return out.map((c, i) => ({ ...c, onClear: () => Promise.resolve(c.onClear()).then(() => refocusChip(i)) }));
  }
  function refocusChip(i) {
    if (!root.isConnected) return;
    const xs = chipsBox ? chipsBox.querySelectorAll(".fchip-x") : [];
    const t = xs[Math.min(i, xs.length - 1)] || (gridBox && gridBox.querySelector('.cell[tabindex="0"]'));
    if (t) t.focus({ preventScroll: true });
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
      a.combos.length > TIP_COMBOS ? h("span", { class: "muted" }, `+${a.combos.length - TIP_COMBOS} more`) : null);
  }

  function playerSeg(node) {
    const { p } = shown(node);
    return h("div", { class: "seg", role: "group", "aria-label": "Show range of" }, [0, 1].map((i) =>
      h("button", { type: "button", class: "seg-opt", "aria-pressed": String(i === p),
        onclick: () => { st.side = i; st.cls = null; st.cat = null; draw(); } },
        `${i === 0 ? "OOP" : "IP"} ${pos(i)}${node.type === "action" && node.player === i ? " ·  acts" : ""}`)));
  }

  // ---------------------------------------------------------------- left column
  function tabBar() {
    const setView = (v) => { st.view = v; st.hover = -1; draw(); };
    countEl = h("span", { class: "tab-count mono" });
    return h("div", { class: "view-tabs" },
      h("div", { role: "tablist", "aria-label": "View" }, VIEWS.map((v) =>
        h("button", { type: "button", role: "tab", class: "view-tab", "aria-selected": String(st.view === v), onclick: () => setView(v) },
          v[0].toUpperCase() + v.slice(1)))), countEl);
  }

  function leftCol(node, rng) {
    const out = [tabBar()];
    gridBox = null; chipsBox = null;
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
    chipsBox = h("div", { class: "chips-box" });
    gridBox = h("div", { class: "grid-box" });
    out.push(chipsBox, gridBox, h("p", { class: "st-hint" }, "Hover a cell for its mix · click to pin it."));
    return out;
  }

  // ---------------------------------------------------------------- right column
  function setSel(k) {
    st.sel = k;
    st.hover = -1;
    paintGrid();
  }
  function preview(k) {
    if (st.hover === k) return;
    st.hover = k;
    paintGrid();
  }

  function rightCol(node, rng) {
    const out = [];
    actsEl = null;
    if (node.type === "action") {
      const reach = rng.players[node.player].reach || node.reach;
      const freqs = frequencies(node, aggregate(combos, node, reach));
      actsEl = actionsBlock({ node, bb, colors: actionColors(node.actions.map((a) => a.token)), freqs, combosPer: actionCombos(node, reach),
        sel: st.sel, hoverPreview: HOVER_PREVIEW, onPreview: preview, onPin: (k) => setSel(st.sel === k ? -1 : k),
        onPlay: (k) => go([...st.tokens, node.actions[k].token]) });
      out.push(actsEl);
    } else if (node.type === "chance" || node.kind === "leaf") {
      out.push(boardAt(st.tokens).length < 5 || node.type === "chance" ? runoutPicker(node)
        : h("section", { class: "side-block" }, h("p", { class: "node-head" }, "River complete: showdown.")));
    } else {
      out.push(h("section", { class: "side-block" }, h("p", { class: "node-head" },
        `Hand over: ${node.kind === "fold" ? "fold" : String(node.kind).replace("_", " ")} · pot ${num(node.pot / bb, 1)} bb`)));
    }
    eqEl = h("section", { class: "side-block eq-panel" });
    hcEl = h("section", { class: "side-block hc-panel" });
    out.push(eqEl, hcEl, metaRow(meta));
    return out;
  }

  /** Range equity + hand classes (right column); both players' node reach, blocked combos removed. */
  function paintPanels() {
    if (!current || !eqEl || current.rng.players.length < 2) return;
    const { node, rng } = current;
    const board = boardAt(st.tokens);
    const reaches = [0, 1].map((p) => rng.players[p].reach || []);
    const eq = equityPanel({ status: eqState.status, eq: eqState.eq, reaches, live: liveMask(combos, board), board, pos,
      dots: dotFilter(board), bucket: st.bucket, shownP: shown(node).p, onBucket: (b) => pickFilter({ bucket: b }) });
    const hc = classesPanel({ rows: classShares(combos, board, reaches), pos, active: st.hc, onPick: (n) => pickFilter({ hc: n }) });
    eqEl.replaceWith(eq);
    hcEl.replaceWith(hc);
    eqEl = eq; hcEl = hc;
  }

  /** Equity at the current node; stale responses (node changed meanwhile) are dropped. */
  function loadEquity() {
    const key = st.tokens.join(",");
    const mine = ++eqSeq;
    const settle = (eq) => {
      eqDone.set(key, eq);
      if (mine !== eqSeq) return;
      eqState = { status: eq ? "ok" : "error", eq };
      if (st.bucket) paintGrid();
      paintPanels();
    };
    if (eqDone.has(key)) { const eq = eqDone.get(key); eqState = { status: eq ? "ok" : "error", eq }; return; }
    eqState = { status: "loading", eq: null };
    api(`/api/solves/${enc(name)}/equity?path=${enc(key)}`).then((eq) => settle(checkEquity(eq, combos.length)), () => {
      if (mine !== eqSeq) return;
      eqState = { status: "error", eq: null };
      if (st.bucket) paintGrid();
      paintPanels();
    });
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
    closePop(false);
    let node, rng, items;
    try { [[node, rng], items] = await Promise.all([fetchNode(st.tokens), lineItems()]); } catch (e) {
      if (mine === seq) root.replaceChildren(errorBox(e), h("p", {}, h("a", { href: "/strategy/library", "data-link": true }, "← Saved solves")));
      return;
    }
    if (mine !== seq) return;
    current = { node, rng };
    if (node.type !== "action" || st.sel >= node.actions.length) st.sel = -1;
    if (node.type !== "action") st.hover = -1;
    const strip = lineStrip({ board: boardAt(st.tokens), bb, items: [...items, { node, pos: node.type === "action" ? pos(node.player) : "" }],
      onRoot: () => (chain.length > 1 ? navigate(`/strategy/${enc(chain[0].name)}`) : go([])),
      onBack: canUp() ? up : null, onPlay: (k) => go([...st.tokens, node.actions[k].token]),
      deal: node.type === "chance" ? { open: false, onToggle: togglePop } : null });
    const main = leftCol(node, rng);
    const side = rightCol(node, rng);
    root.replaceChildren(h("div", { class: "strat-surface" }, strip,
      h("div", { class: "two-col" }, h("div", { class: "main-col" }, main), h("div", { class: "side-col" }, side))));
    strip.scrollLeft = strip.scrollWidth; // current node in view on narrow screens
    loadEquity();
    paintGrid();
    paintPanels();
  }

  // ---------------------------------------------------------------- keyboard
  onKeys((e) => {
    if (!current || e.defaultPrevented) return;
    if (e.key === "?") { e.preventDefault(); shortcutsSheet(SHORTCUTS); return; }
    if (e.key === "Escape") { if (clearLatest()) e.preventDefault(); return; }
    const node = current.node;
    const n = node.type === "action" ? node.actions.length : 0;
    const cell = e.target instanceof Element ? e.target.closest(".grid13 .cell") : null;
    if (cell && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pinCell(cell.dataset.cls); return; }
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      if (!n || cell || pop) return;
      e.preventDefault();
      const d = e.key === "ArrowRight" ? 1 : -1;
      setSel(((st.sel + 1 + d + n + 1) % (n + 1)) - 1); // cycles all -> a0 .. an-1 -> all
    } else if (/^[1-9]$/.test(e.key) && n && +e.key <= n && !pop) {
      e.preventDefault();
      const k = +e.key - 1;
      setSel(st.sel === k ? -1 : k);
    } else if (e.key === "Enter" && n && st.sel >= 0 && !e.target.closest("button, a")) {
      e.preventDefault();
      go([...st.tokens, node.actions[st.sel].token]);
    } else if (e.key === "Backspace") {
      e.preventDefault();
      up();
    }
  }, root);

  await draw();
  return root;
}
