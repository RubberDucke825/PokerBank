"""Settle a finished trainer hand: side pots from contributions, 7-card ranking with
pn.profile.handeval.evaluate (full 5..7 card evaluator)."""

from __future__ import annotations

from collections.abc import Sequence

from pn.profile.handeval import evaluate
from pn.ranges import card_to_int


def side_pots(contributed: Sequence[int], folded: Sequence[bool]) -> list[tuple[int, list[int]]]:
    """[(amount, eligible seats)] from the lowest all-in level up; folded chips are dead money
    in every level they reached."""
    levels = sorted({c for c, f in zip(contributed, folded, strict=True) if not f and c > 0})
    pots: list[tuple[int, list[int]]] = []
    prev = 0
    for lvl in levels:
        amount = sum(min(c, lvl) - min(c, prev) for c in contributed)
        eligible = [i for i, (c, f) in enumerate(zip(contributed, folded, strict=True))
                    if not f and c >= lvl]  # fmt: skip
        if amount > 0:
            pots.append((amount, eligible))
        prev = lvl
    dead = sum(c - min(c, prev) for c in contributed)  # folded chips above every live level
    if dead and pots:
        pots[-1] = (pots[-1][0] + dead, pots[-1][1])
    return pots


def hand_value(hole: Sequence[str], board: Sequence[str]) -> int:
    return evaluate([card_to_int(c) for c in (*hole, *board)])


def settle(contributed: Sequence[int], folded: Sequence[bool], holes: Sequence[Sequence[str]],
           board: Sequence[str]) -> list[int]:  # fmt: skip
    """Chips won per seat (gross). One player left: they take everything. Odd chips go to the
    first winner in seat order."""
    alive = [i for i, f in enumerate(folded) if not f]
    won = [0] * len(contributed)
    if len(alive) == 1:
        won[alive[0]] = sum(contributed)
        return won
    values = {i: hand_value(holes[i], board) for i in alive}
    for amount, eligible in side_pots(contributed, folded):
        best = max(values[i] for i in eligible)
        winners = [i for i in eligible if values[i] == best]
        share, odd = divmod(amount, len(winners))
        for k, i in enumerate(winners):
            won[i] += share + (1 if k < odd else 0)
    return won
