// Trainer table: felt, board, pot, seats on a ring (hero bottom centre), dealer button, bets, result.
import { h, cards } from "../core.js";
import { bb, signed, signCls, toBB, lastActions } from "./train_util.js";

const DEALER_OFFSET = 0.42; // radians clockwise of the seat, so the button never sits under the seat's cards

function seatEl(state, s, idx, n, last) {
  const ang = Math.PI / 2 + (2 * Math.PI * idx) / n; // hero (seat 0) at the bottom, clockwise
  // Horizontal placement is resolved in CSS (train.css .train-table [style*="--cx"]) so a seat plate always
  // stays inside the table box whatever its width; r scales the ring (1 = seats, smaller = dealer/bets).
  const pos = (r, a = ang) => `--cx: ${(Math.cos(a) * r).toFixed(4)}; top: ${(50 + 46 * r * Math.sin(a) * 0.92).toFixed(2)}%`;
  const shown = s.is_hero ? state.hero.cards
    : state.result ? (state.result.showdown.find((x) => x.position === s.position) || {}).cards : null;
  const folded = !s.in_hand;
  const acting = s.position === state.to_act;
  const cls = ["seat", s.is_hero && "is-hero", folded && "is-folded", acting && "is-acting", s.allin && "is-allin"].filter(Boolean).join(" ");
  return [
    h("div", { class: cls, style: pos(1), "aria-label": `${s.position} ${s.name}` },
      shown ? cards(shown) : folded ? null : h("span", { class: "card-back", "aria-hidden": "true" }),
      h("div", { class: "seat-plate" },
        h("span", { class: "pos-tag" }, s.position),
        h("span", { class: "seat-name", title: s.name }, s.is_hero ? "You" : s.name),
        h("span", { class: "seat-stack" }, bb(toBB(state, s.stack)))),
      last ? h("span", { class: `seat-act act-${last.split(" ")[0].replace("-", "")}` }, last) : null),
    s.position === state.button ? h("span", { class: "dealer", style: pos(0.74, ang + DEALER_OFFSET), title: "Dealer button" }, "D") : null,
    s.committed > 0 ? h("span", { class: "bet-tag", style: pos(0.57) }, bb(toBB(state, s.committed))) : null,
  ];
}

function resultEl(state) {
  const r = state.result;
  const me = toBB(state, r.net[state.hero.position] || 0);
  const winners = Object.entries(r.won).map(([p, v]) => `${p} wins ${bb(toBB(state, v))} bb`).join(" · ");
  return h("div", { class: "train-result", role: "status" },
    h("p", { class: `train-result-big ${signCls(me)}` }, `${signed(me)} bb`),
    h("p", { class: "muted" }, r.hero_folded ? "You folded" : r.showdown.length ? "Showdown" : "Uncalled", winners ? ` · ${winners}` : ""));
}

/** `seenBoard`: cards already on screen; only newly dealt ones get the fade-up class. */
export function tableEl(state, seenBoard = 0) {
  const n = state.seats.length;
  const last = lastActions(state);
  const board = state.result ? state.result.board : state.board;
  const boardEl = board.length ? cards(board) : h("span", { class: "label" }, state.street);
  if (board.length) [...boardEl.children].forEach((c, k) => { if (k >= seenBoard) c.classList.add("is-dealt"); });
  return h("div", { class: "train-table" },
    h("div", { class: "train-felt" }),
    h("div", { class: "train-center" },
      boardEl,
      h("p", { class: "train-pot" }, "POT ", h("b", {}, bb(toBB(state, state.result ? state.result.amount : state.pot))), " bb"),
      state.result ? resultEl(state) : null),
    state.seats.map((s, i) => seatEl(state, s, i, n, last[s.position])));
}
