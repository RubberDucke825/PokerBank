"""Preflop-line ranges for solver spots: chart ranges (pn.profile.charts) narrowed along the
line, defaults where no chart exists, dead-card removal."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pn import ranges
from pn.adapter.state import Line
from pn.profile import charts

# Fallbacks when no chart covers a line (pure ranges, sensible live-game defaults).
DEFAULTS = {
    "open": "22+,A2s+,K9s+,Q9s+,J9s+,T9s,98s,87s,76s,ATo+,KJo+,QJo",
    "limp": "22+,A2s+,K2s+,Q5s+,J7s+,T7s+,96s+,85s+,75s+,64s+,54s,A2o+,K9o+,QTo+,JTo",
    "call": "22-JJ,A2s-AQs,K9s+,Q9s+,J9s+,T8s+,97s+,86s+,75s+,65s,54s,AJo-AQo,KJo+",
    "3bet": "TT+,AJs+,KQs,A5s-A4s,AKo",
    "call_3bet": "77-QQ,ATs+,KJs+,QJs,JTs,AQo+",
    "cold_call_3bet": "TT-QQ,AQs+,KQs,AKo",
    "4bet": "QQ+,AKs,AKo,A5s",
    "cold_4bet": "QQ+,AKs,AKo",
    "call_4bet": "JJ-QQ,AKs,AQs,AKo",
    "5bet": "KK+,AKs",
    "call_5bet": "QQ+,AKs",
}
ANY = "22+,A2+,K2+,Q2+,J2+,T2+,92+,82+,72+,62+,52+,42+,32"
# Chart lookups: line action -> (chart action, which seat is `opener` for the lookup).
CHART_ACTION = {"open": "open", "call": "call", "3bet": "3bet", "call_3bet": "call_3bet",
                "4bet": "4bet", "call_4bet": "call_4bet", "5bet": "5bet"}  # fmt: skip
# Narrowing: the chart range of a later line is intersected with the earlier line's range.
NARROW_FROM = {"call_3bet": "open", "4bet": "open", "call_4bet": "3bet", "5bet": "3bet"}


def any_two() -> dict[str, float]:
    return ranges.parse(ANY)


def _chart(table: str, action: str, position: str, vs: str | None, depth_bb: float) -> dict | None:
    chart_action = CHART_ACTION.get(action)
    if chart_action is None:
        return None
    try:
        return charts.chart_range(table, chart_action, position, vs, depth_bb)
    except (charts.ChartError, KeyError):
        return None


def intersect(a: Mapping[str, float], b: Mapping[str, float]) -> dict[str, float]:
    return {c: min(w, b[c]) for c, w in a.items() if c in b and min(w, b[c]) > 0}


def line_range(line: Line | None, position: str, table: str, depth_bb: float,
               opener_of_3bet: str | None = None) -> tuple[dict[str, float], str]:  # fmt: skip
    """(range, source) for a player's preflop line from charts, with defaults as fallback."""
    if line is None or line.action == "check":
        return any_two(), "any_two"
    action = line.action
    rng = _chart(table, action, position, line.vs, depth_bb)
    source = f"chart:{action}"
    if not rng:
        if action == "limp":
            rng = _chart(table, "open", position, None, depth_bb)
            source = "chart:open(limp)"
        if not rng:
            rng, source = ranges.parse(DEFAULTS.get(action, DEFAULTS["call"])), f"default:{action}"
    earlier = NARROW_FROM.get(action)
    if earlier:
        vs = opener_of_3bet if earlier == "3bet" else None
        base = _chart(table, earlier, position, vs, depth_bb)
        if base:
            narrowed = intersect(rng, base)
            if narrowed:
                rng, source = narrowed, f"{source}&{earlier}"
    return rng, source


def remove_dead(rng: Mapping[str, float], dead: Iterable[str]) -> dict[str, float]:
    dead = set(dead)
    return {c: w for c, w in rng.items() if c[:2] not in dead and c[2:] not in dead}
