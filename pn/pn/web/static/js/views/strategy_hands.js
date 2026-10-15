// Right column of the tree browser (GTO Wizard style): the Actions block (colour tiles with frequency
// and reach-weighted combos, a stacked frequency bar). Two modes: pin (tree browser: hover previews,
// click pins, Play pill / double-click plays) and the older select mode (hover selects, click plays,
// "All actions" reset; the preflop navigator). No EVs (Plan 7).
import { h, num, pct } from "../core.js";
import { sum } from "./strategy_parts.js";
import { actionSize } from "./strategy_strip.js";

/** Reach-weighted combos per action: sum over combos of reach * frequency. */
export function actionCombos(node, reach) {
  const out = node.actions.map(() => 0);
  (node.strategy || []).forEach((row, i) => {
    const r = reach[i] || 0;
    if (r > 0 && row) row.forEach((f, k) => { out[k] += r * f; });
  });
  return out;
}

const keysHint = () => [h("kbd", {}, "←"), h("kbd", {}, "→"), " select ", h("kbd", {}, "Enter"), " play ", h("kbd", {}, "⌫"), " back"];
const HINT_IDLE = "Hover to preview an action in the grid · click to pin it.";
const HINT_IDLE_NO_HOVER = "Click an action to pin it in the grid.";
const HINT_PINNED = "Pinned. Play ▸, double-click or Enter to play it · click again to unpin.";

function tileBody(a, k, bb, freqs, combosPer) {
  const s = actionSize(a, bb);
  return [h("span", { class: "act-name" }, s.name, s.size ? h("span", { class: "mono" }, ` ${s.size}`) : null,
    s.pct ? h("small", { class: "mono" }, ` ${s.pct}`) : null),
  h("span", { class: "act-freq mono" }, pct(freqs[k], 1)),
  h("span", { class: "act-combos mono" }, num(combosPer[k], 1), h("small", {}, "combos"))];
}

/**
 * Actions block. Select mode (default): `onSelect(k)` on hover / focus (k = -1 resets), `onPlay(k)` on
 * click. Pin mode (`onPin` given): `onPreview(k)` on hover (-1 when the pointer leaves the tiles),
 * `onPin(k)` on click, `onPlay(k)` on the Play pill, double-click or Enter on the pinned tile.
 * `keys` replaces the key hint. Pin / preview state is updated in place with syncTiles().
 */
export function actionsBlock({ node, bb, colors, freqs, combosPer, sel, onSelect, onPlay, keys, onPin, onPreview, hoverPreview = true }) {
  const tot = sum(freqs);
  const stack = h("div", { class: "act-stack", "aria-hidden": "true" }, freqs.map((f, k) =>
    h("span", { style: { flexGrow: String(tot ? f / tot : 0), background: colors[k] } })));
  const head = h("div", { class: "head" }, h("span", { class: "label" }, "Actions"), h("span", { class: "keys muted" }, keys || keysHint()));
  if (!onPin) {
    const tiles = node.actions.map((a, k) => h("button", { type: "button", class: `act-tile${sel === k ? " is-sel" : ""}`, style: `--act: ${colors[k]}`,
      "aria-pressed": String(sel === k), title: "Click to play this action",
      onclick: () => onPlay(k), onmouseenter: () => onSelect(k), onfocus: () => onSelect(k) }, tileBody(a, k, bb, freqs, combosPer)));
    return h("section", { class: "side-block acts-block", "aria-label": "Actions" }, head,
      h("div", { class: "act-tiles", role: "toolbar", "aria-label": "Actions" }, tiles), stack,
      h("button", { type: "button", class: "chip act-all", "aria-pressed": String(sel < 0), onclick: () => onSelect(-1) }, "All actions"));
  }
  const cells = node.actions.map((a, k) => {
    const tile = h("button", { type: "button", class: "act-tile", style: `--act: ${colors[k]}`, "aria-pressed": "false",
      title: "Click to pin · double-click or Enter to play", onclick: () => onPin(k), ondblclick: () => onPlay(k),
      onkeydown: (e) => { if (e.key === "Enter" && tile.getAttribute("aria-pressed") === "true") { e.preventDefault(); onPlay(k); } },
      onmouseenter: () => { if (hoverPreview) onPreview(k); } }, tileBody(a, k, bb, freqs, combosPer));
    const play = h("button", { type: "button", class: "act-play", hidden: true, title: "Play this action (Enter)", onclick: () => onPlay(k) },
      "Play ", h("span", { "aria-hidden": "true" }, "▸"));
    return h("div", { class: "act-cell" }, tile, play);
  });
  const block = h("section", { class: "side-block acts-block", "aria-label": "Actions", dataset: { hover: hoverPreview ? "1" : "" } }, head,
    h("div", { class: "act-tiles", role: "toolbar", "aria-label": "Actions", onmouseleave: () => onPreview(-1) }, cells), stack,
    h("p", { class: "st-hint act-hint" }));
  syncTiles(block, { sel, hover: -1 });
  return block;
}

/** Pin mode: reflect pinned `sel` and previewed `hover` (-1 = none) on the block in place. */
export function syncTiles(block, { sel, hover }) {
  if (!block) return;
  const eff = hover >= 0 ? hover : sel;
  block.querySelector(".act-tiles").classList.toggle("has-eff", eff >= 0);
  block.querySelectorAll(".act-cell").forEach((cell, k) => {
    const tile = cell.firstChild;
    tile.classList.toggle("is-sel", k === sel);
    tile.classList.toggle("is-prev", k === hover && k !== sel);
    tile.classList.toggle("is-eff", k === eff);
    tile.setAttribute("aria-pressed", String(k === sel));
    cell.lastChild.hidden = k !== sel;
  });
  block.querySelectorAll(".act-stack > span").forEach((s, k) => s.classList.toggle("is-dim", eff >= 0 && k !== eff));
  const hint = block.querySelector(".act-hint");
  if (hint) hint.textContent = sel >= 0 ? HINT_PINNED : block.dataset.hover ? HINT_IDLE : HINT_IDLE_NO_HOVER;
}
