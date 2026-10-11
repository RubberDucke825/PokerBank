"""Per-spot preflop GTO frequencies for the positional matrix (data/gtowizard/*_freqs.json).

Every positional opportunity carries a GTO context (`Spot`): the GTO Wizard spot it maps to and
the action counted as success. The cell's `gto` is the mean of the spot frequencies over the
player's actual opportunities, so a BB 3-bet cell that faced mostly BTN opens is compared with
the BTN-open answer, not with an unweighted mean over openers.

Seat mapping (our labels -> GTO Wizard 6-max seats; see DEVIATIONS in positional.py):
  UTG, UTG1, UTG2, UTG3, LJ -> UTG (GTO Wizard's first seat, our 6-max LJ)
  HJ, CO, BTN, SB, BB       -> same name
  BTN_HU, BB_HU             -> SB, BB (heads-up seats: the button posts the small blind)
  STR                       -> no GTO (straddled pots are not in the data)
When two of our seats collapse onto the same GTO seat (UTG opens, LJ 3-bets), the later actor
is moved to the next GTO seat (HJ): the nearest spot where the action order is legal.

Depth: each opportunity uses the available depth (100 or 200 bb) nearest to the hand's
effective stack; spots missing at that depth (200 bb only has rfi / vs_open) fall back to 100.
Frequencies are combo-weighted over the hands that reach the node (pn.profile.gtowizard).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pn.profile import gtowizard

GTOW_ORDER = ("UTG", "HJ", "CO", "BTN", "SB", "BB")
OURS_TO_GTOW = {
    "UTG": "UTG", "UTG1": "UTG", "UTG2": "UTG", "UTG3": "UTG", "LJ": "UTG",
    "HJ": "HJ", "CO": "CO", "BTN": "BTN", "SB": "SB", "BB": "BB",
    "BTN_HU": "SB", "BB_HU": "BB",  # heads-up: the button posts the SB -> SB-vs-BB spot
}  # fmt: skip
DEFAULT_DEPTH = 100


@dataclass(frozen=True)
class Spot:
    """GTO context of one opportunity. kind: rfi | vs_open | vs_3bet | vs_4bet | cold;
    seats: our position labels in the GTO key order; action: raise | call | fold."""

    kind: str
    seats: tuple[str, ...]
    action: str
    depth_bb: float | None = None


def gtow_seats(seats: tuple[str, ...]) -> tuple[str, ...] | None:
    """Map our seats to GTO Wizard seats keeping a strictly increasing action order."""
    out: list[str] = []
    for s in seats:
        g = OURS_TO_GTOW.get(s)
        if g is None:
            return None
        idx = GTOW_ORDER.index(g)
        if out and idx <= GTOW_ORDER.index(out[-1]):
            idx = GTOW_ORDER.index(out[-1]) + 1
            if idx >= len(GTOW_ORDER):
                return None
        out.append(GTOW_ORDER[idx])
    return tuple(out)


class GtoTable:
    """Spot frequency tables per depth; `lookup(spot)` -> frequency or None."""

    def __init__(self, freqs: Mapping) -> None:
        depths = sorted({int(k.split("|", 1)[0]) for k in freqs})
        self.tables = {d: gtowizard.spot_table(freqs, d) for d in depths}

    def depths(self) -> tuple[int, ...]:
        return tuple(sorted(self.tables))

    def _depth_order(self, depth_bb: float | None) -> list[int]:
        ds = self.depths()
        if depth_bb is None:
            return sorted(ds, key=lambda d: (d != DEFAULT_DEPTH, d))
        return sorted(ds, key=lambda d: (abs(d - depth_bb), d))

    def spot_key(self, spot: Spot) -> str | None:
        seats = gtow_seats(spot.seats)
        return None if seats is None else "|".join((spot.kind, *seats))

    def lookup(self, spot: Spot) -> float | None:
        key = self.spot_key(spot)
        if key is None:
            return None
        nearest = self._depth_order(spot.depth_bb)
        # nearest depth that has the key; otherwise the default depth (documented fallback)
        for d in nearest[:1] + [DEFAULT_DEPTH]:
            f = self.tables.get(d, {}).get(key)
            if f is not None:
                return float(f[spot.action])
        return None


@lru_cache(maxsize=4)
def _cached(path: str) -> GtoTable:
    return GtoTable(gtowizard.load_freqs(Path(path)))


def load_table(path: Path | None = None) -> GtoTable | None:
    p = path or gtowizard.freqs_path()
    return _cached(str(p)) if p.exists() else None
