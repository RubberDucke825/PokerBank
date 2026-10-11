// Hand-class breakdown (made hands + draws) of a combo on a board, computed client-side.
const RANK_STR = "23456789TJQKA";
const rankOf = (c) => RANK_STR.indexOf(c[0]);

export const MADE = ["Straight flush", "Quads", "Full house", "Flush", "Straight", "Set", "Trips",
  "Two pair", "Overpair", "Top pair", "Underpair", "Middle pair", "Weak pair", "Ace high", "No made hand"];
export const DRAWS = ["Flush draw", "OESD", "Gutshot", "Backdoor FD"];

const CAT = { HIGH: 0, PAIR: 1, TWO: 2, TRIPS: 3, STRAIGHT: 4, FLUSH: 5, FULL: 6, QUADS: 7, SF: 8 };

function straightTop(mask) {
  for (let top = 12; top >= 4; top--) {
    const need = 0b11111 << (top - 4);
    if ((mask & need) === need) return top;
  }
  return (mask & 0b1000000001111) === 0b1000000001111 ? 3 : -1;
}

/** Category only (enough for classing). */
function category(cardsList) {
  const bySuit = {};
  const counts = new Array(13).fill(0);
  let mask = 0;
  for (const c of cardsList) {
    const r = rankOf(c);
    counts[r]++;
    mask |= 1 << r;
    (bySuit[c[1]] ||= []).push(r);
  }
  for (const rs of Object.values(bySuit)) {
    if (rs.length >= 5) {
      const m = rs.reduce((a, r) => a | (1 << r), 0);
      return straightTop(m) >= 0 ? CAT.SF : CAT.FLUSH;
    }
  }
  const groups = counts.filter((n) => n > 0).sort((a, b) => b - a);
  if (groups[0] >= 4) return CAT.QUADS;
  if (groups[0] === 3 && groups[1] >= 2) return CAT.FULL;
  if (straightTop(mask) >= 0) return CAT.STRAIGHT;
  if (groups[0] === 3) return CAT.TRIPS;
  if (groups[0] === 2 && groups[1] === 2) return CAT.TWO;
  return groups[0] === 2 ? CAT.PAIR : CAT.HIGH;
}

const HIGH_NAMES = { [CAT.SF]: "Straight flush", [CAT.QUADS]: "Quads", [CAT.FULL]: "Full house",
  [CAT.FLUSH]: "Flush", [CAT.STRAIGHT]: "Straight" };

function pairClass(hole, board) {
  const hr = hole.map(rankOf), br = board.map(rankOf);
  const bcount = {};
  for (const r of br) bcount[r] = (bcount[r] || 0) + 1;
  const distinct = Object.keys(bcount).map(Number).sort((a, b) => b - a);
  if (hr[0] === hr[1]) {
    if (bcount[hr[0]]) return "Set";
    if (hr[0] > distinct[0]) return "Overpair";
    if (distinct.length > 1 && hr[0] > distinct[1]) return "Underpair";
    return "Weak pair";
  }
  const hits = hr.filter((r) => bcount[r]);
  if (hits.some((r) => bcount[r] >= 2)) return "Trips";
  if (hits.length === 2) return "Two pair";
  if (hits.length === 1) {
    const idx = distinct.indexOf(hits[0]);
    return idx === 0 ? "Top pair" : idx === 1 ? "Middle pair" : "Weak pair";
  }
  return hr.includes(12) ? "Ace high" : "No made hand";
}

function draws(hole, board, made) {
  if (board.length >= 5) return [];
  const out = [];
  const all = [...hole, ...board];
  const madeRank = MADE.indexOf(made);
  if (madeRank > MADE.indexOf("Flush")) {
    for (const s of "shdc") {
      const n = all.filter((c) => c[1] === s).length;
      const holeIn = hole.some((c) => c[1] === s);
      if (holeIn && n === 4) out.push("Flush draw");
      else if (holeIn && n === 3 && board.length === 3 && hole.every((c) => c[1] === s)) out.push("Backdoor FD");
    }
  }
  if (madeRank > MADE.indexOf("Straight")) {
    const m = all.reduce((a, c) => a | (1 << rankOf(c)), 0);
    const bm = board.reduce((a, c) => a | (1 << rankOf(c)), 0);
    let outs = 0;
    for (let x = 0; x < 13; x++) {
      if (m & (1 << x)) continue;
      if (straightTop(m | (1 << x)) >= 0 && straightTop(bm | (1 << x)) < 0) outs++;
    }
    if (outs >= 2) out.push("OESD");
    else if (outs === 1) out.push("Gutshot");
  }
  return out;
}

/** {made, draws[]} for a combo string ("AhKd") on a board (["Ks","7d","2c"]). */
export function classify(combo, board) {
  const hole = [combo.slice(0, 2), combo.slice(2, 4)];
  if (!board || board.length < 3) return { made: null, draws: [] };
  const full = category([...hole, ...board]);
  const boardCat = category(board);
  let made;
  if (full >= CAT.STRAIGHT && full > boardCat) made = HIGH_NAMES[full];
  else if (full === CAT.FULL && boardCat === CAT.TRIPS) made = "Full house";
  else made = pairClass(hole, board);
  return { made, draws: draws(hole, board, made) };
}
