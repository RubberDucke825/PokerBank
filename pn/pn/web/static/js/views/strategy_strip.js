// Top strip of the tree browser (GTO Wizard style): a Back button, the board card, then one card per
// action node on the line (position + stack, every action with its size in bb, the taken one filled,
// siblings clickable), small cards for dealt runouts, and the current node outlined in the accent (its
// actions play; at a chance node it opens the deal-card popover).
import { h, num, cards } from "../core.js";
import { deck } from "../deck.js";
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

function nodeCard({ node, pos, taken, current, jump, bb, nBoard, onPlay, deal }) {
  const stack = node.stacks && node.player !== undefined ? bbText(node.stacks[node.player], bb) : "";
  const head = h("span", { class: "ln-head" }, h("span", { class: "pos-tag" }, pos), h("span", { class: "mono" }, stack));
  if (node.type !== "action") {
    const what = node.type === "chance" ? `deal ${node.street}` : node.kind === "leaf" ? `deal ${streetName(nBoard + 1).toLowerCase()}` : String(node.kind).replace("_", " ");
    const kids = [h("span", { class: "ln-head" }, h("span", { class: "label" }, "now")), h("span", { class: "ln-end" }, what,
      deal ? h("span", { class: "ln-caret", "aria-hidden": "true" }, deal.open ? "▲" : "▼") : null)];
    if (!deal) return h("div", { class: "ln-card is-current", "aria-current": "step" }, kids);
    return h("button", { type: "button", class: `ln-card is-current ln-pick${deal.open ? " is-open" : ""}`, "aria-current": "step",
      "aria-haspopup": "dialog", "aria-expanded": String(deal.open), title: `Pick the ${node.street} card`, onclick: deal.onToggle }, kids);
  }
  return h("div", { class: `ln-card${current ? " is-current" : ""}`, "aria-current": current ? "step" : null }, head,
    h("ul", { class: "ln-acts" }, node.actions.map((a, k) => {
      const s = actionSize(a, bb);
      const label = [h("span", {}, s.name), s.size ? h("span", { class: "mono" }, s.size) : null];
      if (current) return h("li", {}, h("button", { type: "button", class: "ln-act", title: "Play this action", onclick: () => onPlay(k) }, label));
      const isTaken = a.token === taken;
      return h("li", {}, h("button", { type: "button", class: `ln-act${isTaken ? " is-taken" : ""}`, "aria-pressed": String(isTaken),
        onclick: () => jump(a.token) }, label));
    })));
}

const dealCard = (street, dealt, fresh) => h("div", { class: `ln-card ln-deal${fresh ? " is-new" : ""}` },
  h("span", { class: "ln-head" }, h("span", { class: "label" }, street)), cards(dealt));

/**
 * items: [{deal: [cards], street} | {node, pos, taken, jump} ], last item = the current node.
 * onBack (null at the root disables Back), onPlay(k) plays the current node's action k, deal = {open,
 * onToggle} makes the chance-node card a popover trigger. Returns a horizontally scrolling nav.
 */
export function lineStrip({ board, items, bb, onRoot, onBack, onPlay, deal }) {
  const current = items[items.length - 1].node;
  const els = items.map((it, i) => (it.deal ? dealCard(it.street, it.deal, i === items.length - 2)
    : nodeCard({ ...it, bb, nBoard: board.length, current: i === items.length - 1, onPlay, deal: i === items.length - 1 ? deal : null })));
  const back = h("button", { type: "button", class: "ln-back", onclick: onBack, disabled: !onBack, title: "Back one step (Backspace)", "aria-label": "Back one step" },
    h("span", { class: "ln-back-arrow", "aria-hidden": "true" }, "←"), h("span", { class: "ln-back-text" }, "Back"));
  return h("nav", { class: "line-strip", "aria-label": "Line" }, back, boardCard({ board, node: current, bb, onRoot }), els);
}

/**
 * Deal-card popover under `anchor` (fixed, clamped to the viewport): 13x4 deck, board / unsolved cards
 * disabled. A click outside closes it (`onClose`); `onPick(card)` deals. Returns {el, close}.
 */
export function dealPopover({ anchor, street, blocked, board, onPick, onClose }) {
  const title = `Deal the ${street}`;
  const d = deck({ label: title, onPick, reasons: Object.fromEntries(board.map((c) => [c, "on board"])) });
  d.update({ blocked, selected: [] });
  const dialog = h("div", { class: "deal-pop", role: "dialog", "aria-modal": "true", "aria-label": title, onkeydown: trapTab },
    h("div", { class: "head" }, h("span", { class: "label" }, title),
      h("button", { type: "button", class: "deal-pop-close", onclick: onClose, "aria-label": "Close" }, "Esc")),
    d.el, h("p", { class: "st-hint" }, "Cards on the board are disabled."));
  const scrim = h("div", { class: "deal-scrim", onclick: onClose });
  function trapTab(e) { // modal: Tab cycles inside the popover
    if (e.key !== "Tab") return;
    const f = [...dialog.querySelectorAll("button:not(:disabled), [tabindex]:not([tabindex='-1'])")];
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    const next = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i === f.length - 1 ? 0 : i + 1);
    e.preventDefault();
    f[next].focus({ preventScroll: true });
  }
  // The router swaps views without a teardown hook: close once the anchor's view leaves the DOM.
  const gone = new MutationObserver(() => { if (!anchor.isConnected) onClose(); });
  const place = () => {
    if (!anchor.isConnected) return;
    const r = anchor.getBoundingClientRect();
    const w = dialog.offsetWidth, ht = dialog.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    const top = Math.max(8, Math.min(r.bottom + 6, window.innerHeight - ht - 8));
    dialog.style.left = `${Math.round(left)}px`;
    dialog.style.top = `${Math.round(top)}px`;
  };
  const el = h("div", { class: "deal-layer" }, scrim, dialog);
  document.body.append(el);
  place();
  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);
  window.addEventListener("popstate", onClose); // left the page with the popover open
  gone.observe(document.body, { childList: true, subtree: true });
  const first = d.el.querySelector("button:not(:disabled)");
  if (first) first.focus({ preventScroll: true });
  const close = () => {
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    window.removeEventListener("popstate", onClose);
    gone.disconnect();
    el.remove();
  };
  return { el, close };
}
