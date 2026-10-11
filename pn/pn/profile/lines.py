"""Action lines (e.g. `pre:raise|flop:bet|turn:check-call`) with shown hands per line.

Line tokens: `pre:` is `raise` (player was the last preflop raiser), `call` (put chips in
voluntarily but was not the last raiser) or `check` (BB in an unraised pot); each postflop
street lists the player's actions on it joined by `-`. Only hands where the player saw a flop
have a line. A shown hand is attached to its line only when it reached showdown.

bluff_ratio = shown hands classed `air` (no pair using a hole card, missed draws included)
/ shown hands on that line; None when nothing was shown.
"""

from __future__ import annotations

from pn import ranges
from pn.profile.handeval import MADE_BLUFF_CLASSES, strength_class
from pn.profile.stats import RawStats

MAX_LINES = 30


def lines_summary(raw: RawStats, max_lines: int = MAX_LINES) -> list[dict]:
    out = []
    for line, n in raw.lines.most_common(max_lines):
        shown = [
            {
                "cards": ranges.canonical_combo("".join(sh.cards)),
                "strength_class": strength_class(sh.cards, sh.board),
                "hand_id": sh.hand_id,
            }
            for sh in raw.shown
            if sh.line == line
        ]
        bluffs = sum(1 for s in shown if s["strength_class"] in MADE_BLUFF_CLASSES)
        out.append(
            {
                "line": line,
                "n": n,
                "shown": shown,
                "bluff_ratio": round(bluffs / len(shown), 4) if shown else None,
            }
        )
    return out
