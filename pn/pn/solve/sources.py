"""Seat ranges for solver spots (contracts/solve_service.md, Plan 7: GTO ranges only, no locks).

A seat's range comes from its preflop line, in the order the trainer grades preflop
(pn.train.preflop.gto_decision): PokerCoaching pure charts first, then GTO Wizard per-class
frequencies for lines PokerCoaching does not chart, then the hand-written charts (through
pn.adapter.spot_ranges.line_range, which also narrows later lines and falls back to defaults).
A preflop seat without a line (the root) is any-two.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pn import ranges
from pn.adapter import spot_ranges as sr
from pn.adapter.state import Line
from pn.profile import charts, gtowizard

GTOW_SHARE = {"open": ("R", "A"), "3bet": ("R", "A"), "call": ("C",)}

Range = dict[str, float]


class ProfileSource(Protocol):
    def get(self, player_id: str, default: Any = None) -> Mapping[str, Any] | None: ...


def profile_hands(profile: Mapping | None) -> int:
    return int(profile.get("hands", 0)) if profile else 0


def pc_line_range(position: str, line: Line, table: str) -> tuple[Range, str] | None:
    """The PokerCoaching pure chart for `line.action`, or None when PokerCoaching has no chart
    (or an empty one: a seat needs combos) for the spot (pn.profile.charts.pc_range)."""
    action = sr.CHART_ACTION.get(line.action)
    if action is None:
        return None
    rng = charts.pc_range(table, action, position, line.vs)
    if not rng:
        return None
    return rng, f"pc:{charts.pc_key(table, action, position, line.vs)}"


def gtow_line_range(position: str, line: Line, depth_bb: float) -> tuple[Range, str] | None:
    """Per-combo frequency of taking `line.action` in the GTO Wizard solution, or None."""
    shares = GTOW_SHARE.get(line.action)
    if shares is None:
        return None
    situ = ("rfi", position) if line.action == "open" else ("vs_open", line.vs, position)
    key = gtowizard.situation_key(situ) if all(situ[1:]) else None
    if key is None:
        return None
    try:
        freqs = gtowizard.load_freqs()
    except FileNotFoundError:
        return None
    for depth in sorted(charts.GTOW_DEPTHS, key=lambda d: abs(d - depth_bb)):
        spot = freqs.get(f"{depth}|{key}")
        if spot is None:
            continue
        out: Range = {}
        for cls, cell in spot["cells"].items():
            known = {k: float(v) for k, v in cell.items() if k in gtowizard.ACTION_CODES}
            tot = sum(known.values())
            w = round(sum(known.get(a, 0.0) for a in shares) / tot, 4) if tot else 0.0
            if w > 0:
                out.update({c: w for c in ranges.class_combos(cls)})
        if out:
            return out, f"gtow:{depth}|{key}"
    return None


def seat_range(*, position: str, line: Line | None, table: str, depth_bb: float,
               three_bettor: str | None) -> tuple[Range, str]:  # fmt: skip
    """(range, origin) for one seat before dead-card removal and explicit ranges."""
    if line is None or line.action == "check":
        return sr.line_range(line, position, table, depth_bb, opener_of_3bet=three_bettor)
    if sr.NARROW_FROM.get(line.action) is None:  # narrowed lines go through line_range
        pc = pc_line_range(position, line, table)
        if pc is not None:
            return pc
        gtow = gtow_line_range(position, line, depth_bb)
        if gtow is not None:
            return gtow
    return sr.line_range(line, position, table, depth_bb, opener_of_3bet=three_bettor)
