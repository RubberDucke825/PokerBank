"""Suit permutations between a solved library flop document and the dealt cards.

A permutation `perm` is a tuple of 4 suit indices (suits "cdhs", pn.solve.flops.SUITS):
perm[doc suit] = actual suit. Reading a document for real cards maps them through
`inverse(perm)`; a request lifted from the document is mapped back with `perm`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from pn import ranges
from pn.solve.flops import SUITS
from pn.solve.request import SpotRequest

Perm = tuple[int, int, int, int]
IDENTITY: Perm = (0, 1, 2, 3)


def inverse(perm: Perm) -> Perm:
    out = [0] * 4
    for doc, actual in enumerate(perm):
        out[actual] = doc
    return tuple(out)  # type: ignore[return-value]


def permute_card(card: str, perm: Perm) -> str:
    return card[0] + SUITS[perm[SUITS.index(card[1].lower())]]


def permute_cards(cards: Sequence[str], perm: Perm) -> tuple[str, ...]:
    return tuple(permute_card(c, perm) for c in cards)


def permute_combo(combo: str, perm: Perm) -> str:
    """'AhKh' -> the canonical combo (pn.ranges.canonical_combo) of its image."""
    return ranges.canonical_combo(permute_card(combo[:2], perm) + permute_card(combo[2:], perm))


def permute_range(text: str, perm: Perm) -> str:
    return ranges.to_text({permute_combo(c, perm): w for c, w in ranges.parse(text).items()})


def permute_request(req: SpotRequest, perm: Perm) -> SpotRequest:
    """`req` with its board, hero cards, seat ranges and range overrides mapped by `perm`."""
    if perm == IDENTITY:
        return req
    seats = tuple(s if s.range is None else replace(s, range=permute_range(s.range, perm))
                  for s in req.seats)  # fmt: skip
    return replace(req, seats=seats, board=permute_cards(req.board, perm),
                   hero_cards=permute_cards(req.hero_cards, perm),
                   overrides={k: permute_range(v, perm) for k, v in req.overrides.items()})  # fmt: skip
