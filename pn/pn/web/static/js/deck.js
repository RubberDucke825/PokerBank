// 52-card deck picker, board slots and board texture helpers (Strategy builder + runout picker).
import { h, RANKS } from "./core.js";

export const SUITS = "shdc";
export const SUIT_GLYPH = { s: "♠", h: "♥", d: "♦", c: "♣" };
export const CARD_RE = /^[2-9TJQKA][shdc]$/;
export const ALL_CARDS = [...SUITS].flatMap((s) => [...RANKS].map((r) => r + s));

/** "Kd" -> "K♦" (plain text, for labels). */
export const cardText = (c) => `${c[0]}${SUIT_GLYPH[c[1]] || c[1]}`;

/**
 * 4x13 deck. `blocked` cards are disabled; `selected` are pressed. `onPick(card)` fires on click.
 * Returns {el, update({blocked, selected})} so callers can re-sync without rebuilding.
 */
export function deck({ blocked = [], selected = [], onPick, label = "Card picker", reasons = {} }) {
  const btns = new Map();
  const el = h("div", { class: "deck", role: "group", "aria-label": label },
    ALL_CARDS.map((c) => {
      const b = h("button", { type: "button", class: `deck-card suit-${c[1]}`, "aria-pressed": "false",
        onclick: () => onPick(c) }, c[0], SUIT_GLYPH[c[1]]);
      btns.set(c, b);
      return b;
    }));
  function update(next) {
    const bl = new Set(next.blocked || []), sel = new Set(next.selected || []);
    for (const [c, b] of btns) {
      const off = bl.has(c);
      b.disabled = off;
      b.setAttribute("aria-pressed", String(sel.has(c)));
      b.setAttribute("aria-label", off ? `${c} (${reasons[c] || "unavailable"})` : c);
    }
  }
  update({ blocked, selected });
  return { el, update };
}

/** n distinct random cards not in `dead`. */
export function randomCards(n, dead = []) {
  const pool = ALL_CARDS.filter((c) => !dead.includes(c));
  const out = [];
  while (out.length < n && pool.length) out.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
  return out;
}

const RANK_VALUE = (c) => 14 - RANKS.indexOf(c[0]); // A=14 ... 2=2

/** Texture tags of a flop (first three cards): suits, pairing, high card, connectedness. */
export function texture(board) {
  const flop = (board || []).slice(0, 3);
  if (flop.length < 3) return [];
  const suits = new Set(flop.map((c) => c[1])).size;
  const vals = flop.map(RANK_VALUE).sort((a, b) => b - a);
  const tags = [suits === 1 ? "monotone" : suits === 2 ? "two-tone" : "rainbow"];
  if (new Set(vals).size < 3) tags.push("paired");
  const hi = vals[0];
  tags.push(hi === 14 ? "A-high" : hi === 13 ? "K-high" : hi === 12 ? "Q-high" : hi >= 10 ? "J/T-high" : "low");
  const uniq = [...new Set(vals)];
  if (uniq.length === 3 && uniq[0] - uniq[2] <= 4) tags.push("connected");
  return tags;
}
