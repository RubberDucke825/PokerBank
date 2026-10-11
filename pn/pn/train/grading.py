"""Frequency grading without EVs (contracts/texas_backend.md "Consumers", contracts/trainer.md
"Plan 7 additions"), GTO Wizard style: the solver's frequency of the chosen action for the exact
combo (postflop) or hand class (preflop chart / GTO Wizard) against the most frequent action.

best    chosen frequency >= BEST_SHARE x the max action frequency
mixed   chosen frequency >= MIXED_SHARE x the max action frequency
blunder otherwise
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BEST_SHARE = 0.5
MIXED_SHARE = 0.1
BEST, MIXED, BLUNDER = "best", "mixed", "blunder"
GRADES = (BEST, MIXED, BLUNDER)
UNGRADED = "none"  # feedback.grade of a decision with no frequencies (no solve, off tree, ...)


@dataclass(frozen=True)
class GradeResult:
    grade: str  # best | mixed | blunder
    freq: float  # normalised frequency of the chosen action (0 when it is not an option)
    best_token: str  # the most frequent action (first one on ties, in the given order)
    best_freq: float  # its normalised frequency


def grade(freqs: Mapping[str, float], chosen: str) -> GradeResult:
    """Grade `chosen` against `freqs` (action token -> frequency, any non-negative scale; they
    are normalised to sum 1). ValueError when no action has a positive frequency."""
    clean = {t: max(0.0, float(f)) for t, f in freqs.items()}
    total = sum(clean.values())
    if total <= 0:
        raise ValueError("no action frequencies to grade against")
    best_token = max(clean, key=clean.__getitem__)
    top = clean[best_token]
    mine = clean.get(chosen, 0.0)
    kind = BEST if mine >= BEST_SHARE * top else MIXED if mine >= MIXED_SHARE * top else BLUNDER
    return GradeResult(kind, round(mine / total, 4), best_token, round(top / total, 4))


def try_grade(freqs: Mapping[str, float] | None, chosen: str) -> GradeResult | None:
    """grade(), or None without frequencies."""
    if not freqs:
        return None
    try:
        return grade(freqs, chosen)
    except ValueError:
        return None


def feedback_fields(freqs: Mapping[str, float] | None, chosen: str) -> dict[str, Any]:
    """The grading fields shared by every feedback (trainer preflop and postflop): grade, freq,
    best, best_freq, freq_error (= 1 - freq). Without frequencies: grade "none", the rest null."""
    g = try_grade(freqs, chosen)
    if g is None:
        return {"grade": UNGRADED, "freq": None, "best": None, "best_freq": None,
                "freq_error": None}  # fmt: skip
    return {"grade": g.grade, "freq": g.freq, "best": g.best_token, "best_freq": g.best_freq,
            "freq_error": round(1.0 - g.freq, 4)}  # fmt: skip
