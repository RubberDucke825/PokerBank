"""Flop textures of the "Spot importance - V3" sheet (contracts/trainer.md "Plan 14 additions").

Precedence: paired (or tripled) first, then monotone, then the high card: Ace / King / Queen / Jack /
Ten, and "Low" for 4-9 high (the lowest unpaired flop is 4-high, 432: a 2- or 3-high flop is
always paired, so "Low" covers every unpaired, not monotone flop below a ten). A monotone flop can
never be paired, so the first two classes never overlap. `FLOP_COUNT` / `FLOP_SHARE` are exact over
the C(52, 3) = 22,100 flops (tests/test_train_textures.py enumerates them).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import comb

RANKS = "23456789TJQKA"
SUITS = "shdc"
HIGH = {"A": "Ace", "K": "King", "Q": "Queen", "J": "Jack", "T": "Ten"}
TEXTURES = ("Ace", "Paired", "Low", "King", "Queen", "Jack", "Ten", "Monotone")  # the sheet's order
DETAILS = {"Ace": "Ace high, unpaired, not monotone", "Paired": "Paired or tripled",
           "Low": "4-9 high, unpaired, not monotone", "King": "King high, unpaired, not monotone",
           "Queen": "Queen high, unpaired, not monotone", "Jack": "Jack high, unpaired, not monotone",
           "Ten": "Ten high, unpaired, not monotone", "Monotone": "Monotone"}  # fmt: skip
FLOPS = comb(52, 3)
_TWO_TONE_OR_RAINBOW = 4**3 - 4  # suit patterns of three distinct ranks that are not monotone


def _high_count(top: int) -> int:
    """Unpaired, not monotone flops whose highest rank index is `top` (0 = deuce)."""
    return comb(top, 2) * _TWO_TONE_OR_RAINBOW


FLOP_COUNT = {
    "Ace": _high_count(12), "King": _high_count(11), "Queen": _high_count(10),
    "Jack": _high_count(9), "Ten": _high_count(8),
    "Low": sum(_high_count(t) for t in range(2, 8)),  # 4-high .. 9-high
    "Paired": 13 * comb(4, 2) * 48 + 13 * comb(4, 3),  # one pair + kicker, trips
    "Monotone": comb(13, 3) * 4,
}  # fmt: skip
FLOP_COUNT = {t: FLOP_COUNT[t] for t in TEXTURES}
FLOP_SHARE = {t: n / FLOPS for t, n in FLOP_COUNT.items()}


def _cards(board: str | Sequence[str]) -> list[str]:
    cards = (
        [board[k : k + 2] for k in range(0, len(board), 2)]
        if isinstance(board, str)
        else list(board)
    )
    flop = cards[:3]
    if len(flop) != 3 or any(len(c) != 2 or c[0] not in RANKS or c[1] not in SUITS for c in flop):
        raise ValueError(f"not a flop: {board!r}")
    if len(set(flop)) != 3:
        raise ValueError(f"repeated card: {board!r}")
    return flop


def texture(board: str | Sequence[str]) -> str:
    """Texture of the first three cards ("Kd7s2c", "Kd7s2c3h" or ["Kd", "7s", "2c"])."""
    flop = _cards(board)
    ranks = [c[0] for c in flop]
    if len(set(ranks)) < 3:
        return "Paired"
    if len({c[1] for c in flop}) == 1:
        return "Monotone"
    top = max(ranks, key=RANKS.index)
    return HIGH.get(top, "Low")


def boards_of[V](boards: Mapping[str, V], tex: str | None) -> dict[str, V]:
    """The entries of `boards` (keyed by flop string) of texture `tex` (all of them for None)."""
    if tex is None:
        return dict(boards)
    return {b: e for b, e in boards.items() if texture(b) == tex}
