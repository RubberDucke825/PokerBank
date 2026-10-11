// Right column of the tree browser (GTO Wizard style): the Actions block (colour tiles with frequency
// and reach-weighted combos, a stacked frequency bar, "All actions" reset) and the Hands list (one row
// per combo: cards, strategy split as the row background, per-action % in mono). No EVs (Plan 7).
import { h, num, pct, cards } from "../core.js";
import { cellBackground } from "../grid.js";
import { sum } from "./strategy_parts.js";
import { actionSize } from "./strategy_strip.js";

const RANGE_COLOR = "var(--accent)";

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

/** Actions block. `onSelect(k)` on hover (k = -1 resets), `onPlay(k)` on click; `keys` replaces the key hint. */
export function actionsBlock({ node, bb, colors, freqs, combosPer, sel, onSelect, onPlay, keys }) {
  const tiles = node.actions.map((a, k) => {
    const s = actionSize(a, bb);
    return h("button", { type: "button", class: `act-tile${sel === k ? " is-sel" : ""}`, style: `--act: ${colors[k]}`,
      "aria-pressed": String(sel === k), title: "Click to play this action",
      onclick: () => onPlay(k), onmouseenter: () => onSelect(k), onfocus: () => onSelect(k) },
      h("span", { class: "act-name" }, s.name, s.size ? h("span", { class: "mono" }, ` ${s.size}`) : null,
        s.pct ? h("small", { class: "mono" }, ` ${s.pct}`) : null),
      h("span", { class: "act-freq mono" }, pct(freqs[k], 1)),
      h("span", { class: "act-combos mono" }, num(combosPer[k], 1), h("small", {}, "combos")));
  });
  const tot = sum(freqs);
  return h("section", { class: "side-block acts-block", "aria-label": "Actions" },
    h("div", { class: "head" }, h("span", { class: "label" }, "Actions"),
      h("span", { class: "keys muted" }, keys || keysHint())),
    h("div", { class: "act-tiles", role: "toolbar", "aria-label": "Actions" }, tiles),
    h("div", { class: "act-stack", "aria-hidden": "true" }, freqs.map((f, k) =>
      h("span", { style: { flexGrow: String(tot ? f / tot : 0), background: colors[k] } }))),
    h("button", { type: "button", class: "chip act-all", "aria-pressed": String(sel < 0), onclick: () => onSelect(-1) }, "All actions"));
}

function handRow(c, w, strat, colors) {
  const segs = strat ? strat.map((f, k) => ({ freq: f, color: colors[k] })) : [{ freq: 1, color: RANGE_COLOR }];
  return h("li", { class: "hand-row", title: `${c} · weight ${pct(w)}` },
    h("span", { class: "hand-fill", style: cellBackground(segs, w, { fill: "full" }), "aria-hidden": "true" }),
    cards([c.slice(0, 2), c.slice(2, 4)]),
    h("span", { class: "hand-pcts mono" }, strat
      ? strat.map((f, k) => h("span", { style: `--act: ${colors[k]}` }, num(100 * f, 0)))
      : h("span", {}, pct(w))));
}

/**
 * Hands list. idx: combo indices already filtered to weight > 0 and sorted; `strategy` (rows per
 * combo) is null on a non-action node or when the shown player is not the actor.
 */
export function handsBlock({ title, idx, total, combos, reach, strategy, colors, onClear }) {
  return h("section", { class: "side-block hands-block", "aria-label": "Hands" },
    h("div", { class: "head" }, h("span", { class: "label" }, "Hands",
      title ? h("span", { class: "hands-cls" }, ` · ${title}`) : null),
      h("span", { class: "muted mono" }, idx.length < total ? `${idx.length} of ${total}` : `${total}`, " hands",
        onClear ? [" · ", h("button", { type: "button", class: "linkish", onclick: onClear }, "clear")] : null)),
    idx.length ? h("ul", { class: "hands-list" }, idx.map((i) => handRow(combos[i], reach[i], strategy ? strategy[i] : null, colors)))
      : h("p", { class: "note" }, "No combos in range."));
}
