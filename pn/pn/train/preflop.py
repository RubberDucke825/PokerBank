"""Preflop for the trainer: map a betting state to a GTO Wizard situation, villain action
sampling and hero grading (PokerCoaching charts -> GTO Wizard frequencies -> hand-written chart
fallback), GTO Wizard raise sizes with [sizes.preflop]-style fallbacks.

Action codes follow the GTO Wizard file: R raise, A all-in, C call, F fold, X check.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pn import ranges
from pn.adapter.state import order_key
from pn.profile import charts, gtowizard
from pn.train import grading
from pn.train.engine import State, raise_cap

CODES = ("R", "A", "C", "F", "X")
OPEN_BB = 2.5  # GTO Wizard 2.5x opens
LIMPER_BB = 1.0  # +1bb per limper
THREE_BET_IP, THREE_BET_OOP, SQUEEZE_PER_CALLER = 3.0, 3.5, 1.0  # x the open
FOUR_BET = 2.2  # x the 3-bet


@dataclass(frozen=True)
class Situation:
    kind: str | None  # rfi | vs_limp | limp_vs_raise | vs_open | cold | vs_3bet | vs_4bet | ...
    seats: tuple[str, ...]  # positions for gtowizard.situation_key (after `kind`)
    raisers: tuple[str, ...]  # positions of the raisers so far, in order
    limpers: int
    callers: int  # callers of the last raise (squeeze when > 0)
    key: str | None  # GTO Wizard spot key without the depth prefix

    @property
    def exact(self) -> bool:
        """The line exists in the GTO Wizard tree (no limpers, no callers)."""
        return self.key is not None and self.limpers == 0 and self.callers == 0


def situation(st: State, actor: int) -> Situation:
    pos = st.positions
    raisers: list[str] = []
    limpers = callers = 0
    for a in st.history:
        if a.street != "preflop":
            break
        if a.kind == "raise":
            raisers.append(pos[a.seat])
            callers = 0
        elif a.kind == "call":
            if raisers:
                callers += 1
            else:
                limpers += 1
    me = pos[actor]
    n = len(raisers)
    kind: str | None = None
    seats: tuple[str, ...] = ()
    if n == 0:
        if limpers == 0:
            kind, seats = "rfi", (me,)
        elif me == "BB" and limpers == 1 and _limper(st) == "SB":
            kind, seats = "vs_limp", ("SB", "BB")
    elif n == 1 and me == "SB" and raisers[0] == "BB" and limpers == 1 and _limper(st) == "SB":
        kind, seats = "limp_vs_raise", ("SB", "BB")  # SB limped, BB raised (no GTO Wizard key)
    elif n == 1 and me != raisers[0]:
        kind, seats = "vs_open", (raisers[0], me)
    elif n == 2:
        o, t = raisers
        if me == o:
            kind, seats = "vs_3bet", (o, t)
        elif me != t:
            kind, seats = "cold", (o, t, me)
    elif n == 3 and raisers[2] == raisers[0] and me == raisers[1]:
        kind, seats = "vs_4bet", (raisers[0], raisers[1])
    elif n == 4 and raisers[2:] == raisers[:2] and me == raisers[0]:
        kind, seats = "vs_5bet", (raisers[0], raisers[1])
    elif n == 5 and raisers[2:4] == raisers[:2] and me == raisers[1]:
        kind, seats = "vs_6bet", (raisers[0], raisers[1])
    key = gtowizard.situation_key((kind, *seats)) if kind else None
    return Situation(kind, seats, tuple(raisers), limpers, callers, key)


def _limper(st: State) -> str | None:
    return next((st.positions[a.seat] for a in st.history if a.kind == "call"), None)


# ------------------------------------------------------------------ GTO Wizard


@lru_cache(maxsize=1)
def _freqs() -> dict:
    try:
        return gtowizard.load_freqs()
    except FileNotFoundError:
        return {}


def has_freqs() -> bool:
    """Whether the GTO Wizard frequencies (data/gtowizard) are on this machine. The trainer's
    raise sizes come from them; without the file the engine sizes by defaults, so a flop spot
    (and its cache key) differs from one built where the file exists."""
    return bool(_freqs())


def gtow_spot(key: str | None, depth_bb: int, freqs: Mapping | None = None) -> tuple[str, dict] | None:
    """(full key, spot) at the nearest GTO Wizard depth that has `key`."""
    if key is None:
        return None
    data = _freqs() if freqs is None else freqs
    for d in sorted(charts.GTOW_DEPTHS, key=lambda d: abs(d - depth_bb)):
        spot = data.get(f"{d}|{key}")
        if spot is not None:
            return f"{d}|{key}", spot
    return None


def cell_dist(spot: Mapping, cls: str) -> dict[str, float]:
    cell = spot["cells"].get(cls) or {}
    known = {k: float(v) for k, v in cell.items() if k in CODES and float(v) > 0}
    tot = sum(known.values())
    return {k: v / tot for k, v in known.items()} if tot else {}


# ------------------------------------------------------------------ PokerCoaching

# Situation kind -> the PokerCoaching chart actions tried in order, as (code, chart action).
# A combo takes the first action whose range holds it, else folds. Pure charts, so the result
# is a single action at frequency 1. SB first in: raise = `open SB`, limp = `limp SB`.
PC_STEPS = {
    "rfi": (("R", "open"), ("C", "limp")),
    "vs_open": (("R", "3bet"), ("C", "call")),
    "vs_3bet": (("R", "4bet"), ("C", "call_3bet")),
    "limp_vs_raise": (("R", "3bet_limp_vs_raise"), ("C", "call_limp_vs_raise")),
}


def pc_other(kind: str, seats: tuple[str, ...]) -> str | None:
    """The other player of a PokerCoaching chart key: opener, 3-bettor or BB raiser."""
    if kind == "vs_open":
        return seats[0]
    if kind == "vs_3bet":
        return seats[1]
    return "BB" if kind == "limp_vs_raise" else None


def pc_dist(table: str, kind: str | None, position: str, other: str | None,
            combo: str, players: int | None = None) -> tuple[dict[str, float], str] | None:  # fmt: skip
    """(code -> frequency, chart key) from the PokerCoaching charts, or None when they do not
    cover the spot (the first action's chart is missing: vs 4-bet and later, cold calls,
    squeezes, BB vs SB limp, heads-up, ...). `players`: as charts.pc_key."""
    steps = PC_STEPS.get(kind or "")
    if steps is None:
        return None
    key = charts.pc_key(table, steps[0][1], position, other, players=players)
    if key is None or charts.pc_range(table, steps[0][1], position, other, players=players) is None:
        return None
    combo = ranges.canonical_combo(combo)
    dist: dict[str, float] = {}
    left = 1.0
    for code, action in steps:
        rng = charts.pc_range(table, action, position, other, players=players) or {}
        w = float(rng.get(combo, 0.0)) * left
        if w > 0:
            dist[code] = dist.get(code, 0.0) + w
            left -= w
    if left > 1e-9:
        dist["F"] = left
    return dist, key


def pc_class_dists(table: str, kind: str | None, position: str,
                   other: str | None) -> tuple[dict[str, dict[str, float]], str] | None:  # fmt: skip
    """(hand class -> code -> frequency, chart key) for a whole PokerCoaching spot, the same
    steps as `pc_dist` (pure charts: one action per class); None when PokerCoaching has no chart."""
    steps = PC_STEPS.get(kind or "")
    if steps is None:
        return None
    key = charts.pc_key(table, steps[0][1], position, other)
    rngs = [(code, charts.pc_range(table, action, position, other)) for code, action in steps]
    if key is None or rngs[0][1] is None:
        return None
    out: dict[str, dict[str, float]] = {}
    for cls in ranges.all_classes():
        combos = ranges.class_combos(cls)
        dist: dict[str, float] = {}
        left = 1.0
        for code, rng in rngs:
            w = sum((rng or {}).get(c, 0.0) for c in combos) / len(combos) * left
            if w > 0:
                dist[code] = dist.get(code, 0.0) + w
                left -= w
        if left > 1e-9:
            dist["F"] = left
        out[cls] = dist
    return out, key


def _pc_line(sit: Situation) -> bool:
    """PokerCoaching only covers clean lines: no callers, no limpers (except SB limp vs BB raise)."""
    return not (sit.kind is None or sit.callers or (sit.limpers and sit.kind != "limp_vs_raise"))


def pc_decision(st: State, i: int, sit: Situation, combo: str,
                table: str) -> tuple[dict[str, float], str] | None:  # fmt: skip
    if not _pc_line(sit):
        return None
    return pc_dist(table, sit.kind, st.positions[i], pc_other(sit.kind, sit.seats), combo)


def pc_ref(st: State, i: int, table: str) -> dict[str, str | None] | None:
    """feedback.grid_ref of a PokerCoaching-graded decision: the chart spot that
    GET /api/charts/pc renders (`key` = the chart key in the decision's origin)."""
    sit = situation(st, i)
    if not _pc_line(sit):
        return None
    pos, other = st.positions[i], pc_other(sit.kind, sit.seats)
    steps = PC_STEPS.get(sit.kind or "")
    key = charts.pc_key(table, steps[0][1], pos, other) if steps else None
    if key is None:
        return None
    return {"kind": "pc", "key": key, "table": table, "situation": sit.kind, "position": pos,
            "other": other}  # fmt: skip


# ------------------------------------------------------------------ chart fallback


def _chart_weight(table: str, action: str, position: str, other: str | None, depth: int,
                  combo: str) -> float:  # fmt: skip
    try:
        rng = charts.chart_range(table, action, position, other, depth)
    except (charts.ChartError, KeyError):
        return 0.0
    return float(rng.get(ranges.canonical_combo(combo), 0.0))


def chart_dist(st: State, actor: int, sit: Situation, combo: str, table: str,
               depth: int) -> dict[str, float]:  # fmt: skip
    """Pure chart: aggressive action with the chart weight of the combo, else the continuing
    action, else fold. Chosen by the number of raises so far (limped / multiway pots use the
    closest heads-up chart)."""
    me, n = st.positions[actor], len(sit.raisers)
    last = sit.raisers[-1] if sit.raisers else None
    if n == 0:
        steps = [("R", "open", None)]
    elif n == 1:
        steps = [("R", "3bet", last), ("C", "call", last)]
    elif n == 2:
        steps = [("R", "4bet", last), ("C", "call_3bet", last)]
    else:
        steps = [("A", "5bet", sit.raisers[0]), ("C", "call_4bet", sit.raisers[0])]
    dist: dict[str, float] = {}
    left = 1.0
    for code, action, other in steps:
        w = _chart_weight(table, action, me, other, depth, combo) * left
        if w > 0:
            dist[code] = dist.get(code, 0.0) + w
            left -= w
    if left > 1e-9:
        dist["F"] = left
    return dist


def class_percentile(cls: str) -> float:
    """Combo-weighted midpoint percentile of a class in the preflop ranking (0 = strongest)."""
    end = charts.class_percentiles()[cls]
    return end - ranges.class_n_combos(cls) / ranges.N_COMBOS / 2


# ------------------------------------------------------------------ sizes and actions


def raise_to(st: State, actor: int, sit: Situation, spot: Mapping | None) -> int:
    """Raise-to in cents: the GTO Wizard size when the line is exact, else 2.5bb open
    (+1bb per limper), 3-bet 3x IP / 3.5x OOP (+1x per caller), 4-bet 2.2x, 5-bet all-in."""
    bb = st.bb
    if sit.exact and spot is not None and spot.get("raise_to_bb"):
        return round(float(spot["raise_to_bb"]) * bb)
    n = len(sit.raisers)
    if n == 0:
        return round((OPEN_BB + LIMPER_BB * sit.limpers) * bb)
    if n == 1:
        opener = sit.raisers[0]
        ip = order_key(st.positions[actor], "flop") > order_key(opener, "flop")
        mult = (THREE_BET_IP if ip else THREE_BET_OOP) + SQUEEZE_PER_CALLER * sit.callers
        return round(st.highest * mult)
    if n == 2:
        return round(st.highest * FOUR_BET)
    return st.max_to(actor)


def code_action(st: State, i: int, code: str, size_to: int) -> tuple[str, int | None]:
    """Engine action (kind, to) for a GTO Wizard code in this state."""
    owe = st.to_call(i)
    if code in ("R", "A") and st.can_raise(i):
        return raise_cap(st, i, size_to) if code == "R" else ("allin", st.max_to(i))
    if code in ("R", "A", "C"):
        return ("call", None) if owe > 0 else ("check", None)
    return ("fold", None) if owe > 0 else ("check", None)


def legal_actions(st: State, i: int, size_to: int) -> list[dict[str, Any]]:
    """Hero's preflop options: [{token, kind, to}] (to = street commitment after the action)."""
    owe = st.to_call(i)
    out: list[dict[str, Any]] = []
    if owe > 0:
        out.append({"token": "fold", "kind": "fold", "to": st.committed[i]})
        out.append({"token": "call", "kind": "call", "to": st.committed[i] + owe})
    else:
        out.append({"token": "check", "kind": "check", "to": st.committed[i]})
    if st.can_raise(i):
        kind, to = raise_cap(st, i, size_to)
        if kind == "raise":
            out.append({"token": "raise", "kind": "raise", "to": to})
        out.append({"token": "allin", "kind": "allin", "to": st.max_to(i)})
    return out


# ------------------------------------------------------------------ decisions


PC, GTOW, CHART = "pokercoaching", "gtowizard", "chart"  # feedback.preflop_source


@dataclass(frozen=True)
class Decision:
    dist: dict[str, float]  # code -> frequency
    origin: str  # pc:<chart key> | gtow:<key> | chart
    spot: dict | None  # GTO Wizard spot used for the size (None: formula)
    source: str = CHART  # pokercoaching | gtowizard | chart


def gto_decision(st: State, i: int, combo: str, table: str, depth: int,
                 freqs: Mapping | None = None) -> Decision:  # fmt: skip
    """PokerCoaching charts first (pure), then GTO Wizard class frequencies, then the
    hand-written charts. The GTO Wizard spot still sets the raise size when the line is exact."""
    sit = situation(st, i)
    found = gtow_spot(sit.key, depth, freqs)
    size_spot = found[1] if found else None
    pc = pc_decision(st, i, sit, combo, table)
    if pc is not None:
        return Decision(pc[0], f"pc:{pc[1]}", size_spot, PC)
    if found is not None:
        dist = cell_dist(found[1], ranges.combo_class(combo))
        if dist:
            return Decision(dist, f"gtow:{found[0]}", found[1], GTOW)
    return Decision(chart_dist(st, i, sit, combo, table, depth), "chart", size_spot, CHART)


def sample(dist: Mapping[str, float], rng: random.Random) -> str:
    codes = [c for c in CODES if dist.get(c, 0) > 0]
    if not codes:
        return "F"
    return rng.choices(codes, weights=[dist[c] for c in codes])[0]


def villain_action(st: State, i: int, combo: str, *, rng: random.Random, table: str,
                   depth: int,
                   freqs: Mapping | None = None) -> tuple[str, int | None, str]:  # fmt: skip
    """(kind, to, origin) for villain seat `i`: the PokerCoaching / GTO Wizard / chart action
    (Plan 7: no profile blend, every villain plays the chart / GTO strategy), kept heads-up
    by heads_up_dist."""
    sit = situation(st, i)
    gto = gto_decision(st, i, combo, table, depth, freqs)
    size = raise_to(st, i, sit, gto.spot)
    dist = sb_limp_as_raise(gto.dist, st, i, sit, combo, table)
    code = sample(heads_up_dist(st, i, dist, size), rng)
    kind, to = code_action(st, i, code, size)
    return kind, to, gto.origin


def sb_limp_as_raise(dist: Mapping[str, float], st: State, i: int, sit: Situation, combo: str,
                     table: str) -> Mapping[str, float]:  # fmt: skip
    """Bots never limp: the SB's chart limps become an open raise for the hands PokerCoaching
    continues with after a BB raise (value re-raises and calls), a fold for the rest."""
    limp = dist.get("C", 0.0)
    if not limp or sit.kind != "rfi" or st.positions[i] != "SB":
        return dist
    c = ranges.canonical_combo(combo)
    keep = max(float((charts.pc_range(table, action, "SB", "BB") or {}).get(c, 0.0))
               for action in ("3bet_value_limp_vs_raise", "call_limp_vs_raise"))  # fmt: skip
    out = {k: v for k, v in dist.items() if k != "C"}
    out["R"] = out.get("R", 0.0) + limp * keep
    out["F"] = out.get("F", 0.0) + limp * (1 - keep)
    return out


def heads_up_dist(st: State, i: int, dist: Mapping[str, float], size_to: int) -> dict[str, float]:
    """`dist` without the actions that would make a limped or 3+ player pot (bots only; the
    trainer grades heads-up pots, user 2026-10-16): no limp (the SB included), a call only
    against a lone raiser, a raise only with at most one other player in. Dropped weight goes
    to the kept actions in proportion, all of it to fold (check when free) when none is kept."""
    seats = {a.seat for a in st.history if a.kind in ("call", "raise")}
    others = sum(1 for j in seats if j != i and not st.folded[j])
    raised = any(a.kind == "raise" for a in st.history)

    def ok(code: str) -> bool:
        kind, _ = code_action(st, i, code, size_to)
        if kind == "call":
            return raised and others == 1
        if kind in ("raise", "allin"):
            return others <= 1
        return True

    kept = {c: f for c, f in dist.items() if f > 0 and ok(c)}
    tot = sum(kept.values())
    if not tot:
        return {"F": 1.0}  # code_action turns F into a check when nothing is owed
    return {c: f / tot for c, f in kept.items()}


def hero_grade(st: State, i: int, combo: str, chosen: str, table: str, depth: int,
               freqs: Mapping | None = None) -> dict[str, Any]:  # fmt: skip
    """Frequency of each legal option for hero's hand class (PokerCoaching: 1 or 0; EV not
    available preflop), graded like postflop decisions (pn.train.grading)."""
    gto = gto_decision(st, i, combo, table, depth, freqs)
    sit = situation(st, i)
    size = raise_to(st, i, sit, gto.spot)
    opts = legal_actions(st, i, size)
    freq = {o["token"]: 0.0 for o in opts}
    for code, f in gto.dist.items():
        kind, _ = code_action(st, i, code, size)
        if kind in freq:
            freq[kind] += f
    return {
        "street": "preflop", "chosen": chosen, "origin": gto.origin, "preflop_source": gto.source,
        **grading.feedback_fields(freq, chosen),
        "situation": sit.key, "class": ranges.combo_class(combo),
        "actions": [{"token": o["token"], "to": o["to"], "freq": round(freq[o["token"]], 4),
                     "ev_bb": None} for o in opts],
        "loss_bb": None,
    }  # fmt: skip
