// Top strip of the tree browser (GTO Wizard style): the board card, then one card per action node on
// the line (position + stack, every action with its size in bb, the taken one filled, siblings
// clickable), small cards for dealt runouts, and the current node outlined in the accent.
import { h, num, cards } from "../core.js";
import { actionLabel, actionBb } from "./strategy_parts.js";

const STREETS = { 3: "Flop", 4: "Turn", 5: "River" };
export const streetName = (n) => STREETS[n] || "Flop";

/** {name: "Bet", size: "2.2"}: the action word and its chips in bb (no size on check / fold / call). `a.name` overrides the word. */
export function actionSize(a, bb) {
  const lab = a.name ? { name: a.name, size: "" } : actionLabel(a.token);
  const chips = actionBb(a, bb);
  const sized = chips !== null && lab.name !== "Call";
  return { name: lab.name, size: sized ? num(chips, chips < 10 ? 1 : 0) : "", pct: lab.size };
}

const bbText = (chips, bb) => num(chips / bb, chips / bb < 10 ? 1 : 0);

function boardCard({ board, node, bb, onRoot }) {
  const stat = (k, v) => h("span", {}, `${k} `, h("b", {}, v));
  const eff = node.stacks ? Math.min(...node.stacks) : null;
  return h("button", { type: "button", class: "ln-card ln-board", onclick: onRoot, title: "Back to the root" },
    h("span", { class: "ln-head" }, h("span", { class: "label" }, streetName(board.length))),
    cards(board),
    h("span", { class: "ln-stats" }, node.pot !== undefined ? stat("POT", bbText(node.pot, bb)) : null,
      eff !== null ? stat("EFF", bbText(eff, bb)) : null, eff !== null && node.pot ? stat("SPR", num(eff / node.pot, 1)) : null));
}

function nodeCard({ node, pos, taken, current, jump, bb, nBoard }) {
  const stack = node.stacks && node.player !== undefined ? bbText(node.stacks[node.player], bb) : "";
  const head = h("span", { class: "ln-head" }, h("span", { class: "pos-tag" }, pos), h("span", { class: "mono" }, stack));
  if (node.type !== "action") {
    const what = node.type === "chance" ? `deal ${node.street}` : node.kind === "leaf" ? `deal ${streetName(nBoard + 1).toLowerCase()}` : String(node.kind).replace("_", " ");
    return h("div", { class: "ln-card is-current", "aria-current": "step" }, h("span", { class: "ln-head" }, h("span", { class: "label" }, "now")),
      h("span", { class: "ln-end" }, what));
  }
  return h("div", { class: `ln-card${current ? " is-current" : ""}`, "aria-current": current ? "step" : null }, head,
    h("ul", { class: "ln-acts" }, node.actions.map((a) => {
      const s = actionSize(a, bb);
      const label = [h("span", {}, s.name), s.size ? h("span", { class: "mono" }, s.size) : null];
      if (current) return h("li", { class: "ln-act" }, label);
      const isTaken = a.token === taken;
      return h("li", {}, h("button", { type: "button", class: `ln-act${isTaken ? " is-taken" : ""}`, "aria-pressed": String(isTaken),
        onclick: () => jump(a.token) }, label));
    })));
}

const dealCard = (street, dealt) => h("div", { class: "ln-card ln-deal" },
  h("span", { class: "ln-head" }, h("span", { class: "label" }, street)), cards(dealt));

/**
 * items: [{deal: [cards], street} | {node, pos, taken, jump} ], last item = the current node.
 * Returns a horizontally scrolling nav.
 */
export function lineStrip({ board, items, bb, onRoot }) {
  const current = items[items.length - 1].node;
  const els = items.map((it, i) => (it.deal ? dealCard(it.street, it.deal)
    : nodeCard({ ...it, bb, nBoard: board.length, current: i === items.length - 1 })));
  return h("nav", { class: "line-strip", "aria-label": "Line" }, boardCard({ board, node: current, bb, onRoot }), els);
}
