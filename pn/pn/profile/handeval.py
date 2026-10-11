"""Small pure-Python 5..7 card evaluator and showdown strength classes.

Only used for profiling (strength class of shown hands, preflop ranking generation); the solver
uses the Rust evaluator.
"""

from __future__ import annotations

from collections import Counter

from pn.ranges import card_to_int

HIGH_CARD, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)

STRENGTH_CLASSES = (
    "straight_flush",
    "quads",
    "full_house",
    "flush",
    "straight",
    "set",
    "trips",
    "two_pair",
    "overpair",
    "top_pair",
    "second_pair",
    "weak_pair",
    "air",
)
MADE_BLUFF_CLASSES = frozenset({"air"})


def _encode(cat: int, ranks: list[int]) -> int:
    v = cat
    for r in (ranks + [0, 0, 0, 0, 0])[:5]:
        v = (v << 4) | r
    return v


def _straight_high(mask: int) -> int:
    """Highest straight top rank in a 13-bit rank mask, or -1. Wheel = rank 3 (five)."""
    for top in range(12, 3, -1):
        need = 0b11111 << (top - 4)
        if mask & need == need:
            return top
    if mask & 0b1000000001111 == 0b1000000001111:
        return 3
    return -1


def evaluate(cards: list[int]) -> int:
    """Strength of the best 5-card hand from 5..7 int cards. Higher is better."""
    by_suit: dict[int, list[int]] = {}
    for c in cards:
        by_suit.setdefault(c & 3, []).append(c >> 2)
    for suit_ranks in by_suit.values():
        if len(suit_ranks) >= 5:
            mask = 0
            for r in suit_ranks:
                mask |= 1 << r
            sh = _straight_high(mask)
            if sh >= 0:
                return _encode(STRAIGHT_FLUSH, [sh])
            flush_value = _encode(FLUSH, sorted(suit_ranks, reverse=True)[:5])
            break
    else:
        flush_value = -1

    counts = Counter(c >> 2 for c in cards)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    ranks_desc = sorted(counts, reverse=True)
    if groups[0][1] == 4:
        quad = groups[0][0]
        kicker = max(r for r in ranks_desc if r != quad)
        return _encode(QUADS, [quad, kicker])
    trips = [r for r, n in groups if n == 3]
    pairs = [r for r, n in groups if n == 2]
    if trips and (len(trips) > 1 or pairs):
        t = trips[0]
        p = max([x for x in trips[1:]] + pairs)
        return _encode(FULL_HOUSE, [t, p])
    if flush_value >= 0:
        return flush_value
    mask = 0
    for r in counts:
        mask |= 1 << r
    sh = _straight_high(mask)
    if sh >= 0:
        return _encode(STRAIGHT, [sh])
    if trips:
        t = trips[0]
        return _encode(TRIPS, [t] + [r for r in ranks_desc if r != t][:2])
    if len(pairs) >= 2:
        p1, p2 = pairs[0], pairs[1]
        kicker = max(r for r in ranks_desc if r not in (p1, p2))
        return _encode(TWO_PAIR, [p1, p2, kicker])
    if pairs:
        p = pairs[0]
        return _encode(PAIR, [p] + [r for r in ranks_desc if r != p][:3])
    return _encode(HIGH_CARD, ranks_desc[:5])


def category(value: int) -> int:
    return value >> 20


_CAT_NAMES = {
    STRAIGHT_FLUSH: "straight_flush",
    QUADS: "quads",
    FULL_HOUSE: "full_house",
    FLUSH: "flush",
    STRAIGHT: "straight",
}


def strength_class(hole: list[str], board: list[str]) -> str:
    """Classify a shown hand vs the board (see STRENGTH_CLASSES). Missed draws -> `air`.

    Rules: hands that only play the board are `air`; made hands >= straight use the evaluator
    category; pocket pair + board card = `set`, one hole card + board pair = `trips`; both hole
    cards pairing distinct board ranks = `two_pair`; a single pair is classified by the paired
    rank's place among distinct board ranks (top/second/weak) or vs the top board card for
    pocket pairs (overpair / second_pair / weak_pair).
    """
    h = [card_to_int(c) for c in hole]
    b = [card_to_int(c) for c in board]
    if len(b) < 3:
        return "air"
    full = evaluate(h + b)
    cat = category(full)
    if cat >= STRAIGHT:
        if len(b) == 5 and evaluate(b) == full:
            return "air"
        return _CAT_NAMES[cat]
    board_ranks = [c >> 2 for c in b]
    hole_ranks = [c >> 2 for c in h]
    bcount = Counter(board_ranks)
    distinct_board = sorted(bcount, reverse=True)
    pocket = hole_ranks[0] == hole_ranks[1]
    if pocket and hole_ranks[0] in bcount:
        return "set" if bcount[hole_ranks[0]] == 1 else "quads"
    matched = sorted({r for r in hole_ranks if r in bcount}, reverse=True)
    if any(bcount[r] >= 2 for r in matched):
        return "trips"
    if len(matched) >= 2:
        return "two_pair"
    if pocket:
        if hole_ranks[0] > distinct_board[0]:
            return "overpair"
        if len(distinct_board) > 1 and hole_ranks[0] > distinct_board[1]:
            return "second_pair"
        return "weak_pair"
    if matched:
        idx = distinct_board.index(matched[0])
        return ("top_pair", "second_pair")[idx] if idx < 2 else "weak_pair"
    return "air"
