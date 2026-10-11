// Position strip of the preflop navigator (/strategy): one card per seat in preflop order with its
// stack, its options (range-wide %), the taken option filled, the seat to act outlined in the accent,
// folded seats dimmed and seats still to come showing a dash. Same card look as the tree browser strip.
import { h, num, pct } from "../core.js";

export const bbText = (cents, bb) => num(cents / bb, cents % bb ? 1 : 0);

function seatCard(seat, { bb, isActor, onPick }) {
  const head = h("span", { class: "ln-head" }, h("span", { class: "pos-tag" }, seat.position),
    h("span", { class: "mono" }, bbText(seat.stack, bb),
      seat.committed > 0 ? h("small", { class: "st-in" }, ` · ${bbText(seat.committed, bb)} in`) : null));
  const cls = `ln-card st-seat${isActor ? " is-current" : ""}${seat.status === "folded" ? " is-folded" : ""}`;
  if (seat.status === "waiting" || !seat.options.length) {
    return h("div", { class: cls }, head, h("span", { class: "st-wait muted" }, "—"));
  }
  return h("div", { class: cls, "aria-current": isActor ? "step" : null }, head,
    h("ul", { class: "ln-acts" }, seat.options.map((o) => {
      const taken = !isActor && o.token === seat.chosen;
      return h("li", {}, h("button", { type: "button", class: `ln-act${taken ? " is-taken" : ""}`, "aria-pressed": String(taken),
        title: isActor ? `${seat.position}: ${o.label}` : `Rewrite the line from ${seat.position}: ${o.label}`,
        onclick: () => onPick(seat.position, o.token) },
        h("span", {}, o.label), o.freq !== null && o.freq !== undefined ? h("span", { class: "mono" }, pct(o.freq, 1)) : null));
    })));
}

/** Horizontally scrolling strip; `onPick(position, token)` for a click on any seat's option. */
export function seatStrip({ seats, actor, bb, onPick }) {
  return h("nav", { class: "line-strip study-strip", "aria-label": "Positions" },
    seats.map((s) => seatCard(s, { bb, isActor: s.position === actor, onPick })));
}
