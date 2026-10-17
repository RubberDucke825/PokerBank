"""Preflop range split along the line a trainer hand actually played (the "Ranges at this
decision" panel, previous street = preflop): one step per preflop decision of the two players
who see the flop, in order. Each step gives, per hand class, the combos of the actor's range
entering the decision (product of its own earlier taken-action frequencies) and the class's
action frequencies.

Frequencies come from the trainer's own preflop policy (pn.train.preflop.gto_decision:
PokerCoaching first, GTO Wizard, then the hand-written charts). Villain steps go through the
bot adjustments (sb_limp_as_raise, heads_up_dist) as in villain_action; hero steps use the
plain distribution hero is graded against (hero_grade).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from pn import ranges
from pn.solve.request import TABLE_SEATS
from pn.train import preflop as pre
from pn.train.engine import State, apply, start
from pn.train.flop_library import replay

ROUND = 4  # decimals of reach and frequencies
SB, BB = 50, 100  # cents, as pn.train.session deals


def preflop_line(st: State, tokens: Sequence[str], hero: int | None, table: str, depth: int,
                 freqs: Mapping | None = None) -> dict[str, Any]:  # fmt: skip
    """{"steps": [...]} for a hand past preflop with exactly two players in at the flop (else
    no steps). `tokens` align with `st.history` (Hand.tokens); `hero`: hero's seat index."""
    if st.street == "preflop":
        return {"steps": []}
    return {"steps": _walk(st, tokens, hero, table, depth, freqs)[0]}


def policy_depth(table: str, plan: tuple[tuple[str, str], ...], behind_bb: float) -> int | None:
    """The trainer depth (pn.train.model.DEPTHS) whose replay of `plan` leaves the stack behind
    (second-largest of the two players, as pn.solve.build's depth) closest to `behind_bb`: a flop
    request only knows the stacks left (150bb after a 200bb 4-bet pot). None: no depth reaches it."""
    from pn.train.model import DEPTHS

    left = {d: b for d in DEPTHS if (b := _behind(table, d, plan)) is not None}
    return min(left, key=lambda d: abs(left[d] - behind_bb)) if left else None


@lru_cache(maxsize=512)
def _behind(table: str, depth: int, plan: tuple[tuple[str, str], ...]) -> float | None:
    st = _replay(table, depth, plan)
    return sorted(st.stacks[i] for i in st.in_hand())[-2] / BB if st is not None else None


def _replay(table: str, depth: int, plan: tuple[tuple[str, str], ...]) -> State | None:
    order = {f"{n}max": o for n, o in TABLE_SEATS.items()}.get(table)
    return replay(order, depth, plan, sb=SB, bb=BB, fold_others=True) if order else None


@lru_cache(maxsize=512)
def policy_reach(table: str, depth: int, plan: tuple[tuple[str, str], ...], seat: str,
                 hero: str | None = None) -> dict[str, float] | None:  # fmt: skip
    """Combo -> reach (rounded to ROUND, zeros dropped) of `seat` along `plan` ((position, kind)
    steps, others fold; pn.train.flop_library.replay) on a `table` (6max | 8max) at `depth` bb:
    the flop range the trainer's preflop policy gives the seat. `hero`: the position whose steps
    use hero's graded distribution (else bot-adjusted). None when the plan does not reach a
    heads-up flop. Cached: do not mutate the result."""
    st = _replay(table, depth, plan)
    if st is None or seat not in st.positions:
        return None
    me = st.seat_of(seat)
    tokens = [a.kind for a in st.history]
    reach = _walk(st, tokens, st.seat_of(hero) if hero else None, table, depth, None)[1].get(me)
    if reach is None:
        return None
    out = {c: round(r, ROUND) for c, r in zip(ranges.all_combos(), reach, strict=True)}
    return {c: w for c, w in out.items() if w > 0}


def _walk(st: State, tokens: Sequence[str], hero: int | None, table: str, depth: int,
          freqs: Mapping | None) -> tuple[list[dict[str, Any]], dict[int, list[float]]]:  # fmt: skip
    """(steps, seat -> per-combo reach after its last preflop step, ranges.all_combos order) of
    the two players left after preflop; ([], {}) unless exactly two are left."""
    pf = [(a, tok) for a, tok in zip(st.history, tokens, strict=False) if a.street == "preflop"]
    folded = {a.seat for a, _ in pf if a.kind == "fold"}
    live = [i for i in range(st.n) if i not in folded]
    if len(live) != 2:
        return [], {}
    stack = max(s + c for s, c in zip(st.stacks, st.contributed, strict=True))
    cur = start(st.positions, stack, st.sb, st.bb)
    combos = ranges.all_combos()
    reach = {i: [1.0] * len(combos) for i in live}
    steps = []
    for a, tok in pf:
        if a.seat in reach:
            is_hero = a.seat == hero
            step, taken = _step(cur, a.seat, tok, reach[a.seat], is_hero, table, depth, freqs)
            steps.append(step)
            reach[a.seat] = [r * f for r, f in zip(reach[a.seat], taken, strict=True)]
        cur = apply(cur, a.kind, a.to if a.kind == "raise" else None)
    return steps, reach


def _step(st: State, i: int, taken: str, reach: list[float], is_hero: bool, table: str,
          depth: int, freqs: Mapping | None) -> tuple[dict[str, Any], list[float]]:  # fmt: skip
    """(step, per-combo frequency of the taken action) for seat `i` deciding in `st`."""
    sit = pre.situation(st, i)
    found = pre.gtow_spot(sit.key, depth, freqs)
    size = pre.raise_to(st, i, sit, found[1] if found else None)
    tokens = [o["token"] for o in pre.legal_actions(st, i, size)]
    if taken not in tokens:  # e.g. a raise the engine capped to an all-in
        taken = "allin" if taken == "raise" and "allin" in tokens else taken
    combos = ranges.all_combos()
    per_combo = [_combo_freq(st, i, c, sit, size, is_hero, table, depth, freqs) if r > 0 else {}
                 for c, r in zip(combos, reach, strict=True)]  # fmt: skip
    used = {t for f in per_combo for t, v in f.items() if v > 0}
    actions = [t for t in tokens if t != "allin" or t in used or t == taken]
    index = {c: k for k, c in enumerate(combos)}
    classes = {}
    for cls in ranges.all_classes():
        ks = [index[c] for c in ranges.class_combos(cls)]
        tot = sum(reach[k] for k in ks)
        freq = [sum(reach[k] * per_combo[k].get(t, 0.0) for k in ks) / tot if tot else 0.0
                for t in actions]  # fmt: skip
        classes[cls] = {"reach": round(tot, ROUND), "n": len(ks),
                        "freq": [round(f, ROUND) for f in freq]}  # fmt: skip
    step = {"position": st.positions[i], "actions": actions, "taken": taken, "classes": classes}
    return step, [f.get(taken, 0.0) for f in per_combo]


def _combo_freq(st: State, i: int, combo: str, sit: pre.Situation, size: int, is_hero: bool,
                table: str, depth: int, freqs: Mapping | None) -> dict[str, float]:  # fmt: skip
    """Action token -> frequency of one combo (codes mapped like hero_grade / villain_action)."""
    dist: Mapping[str, float] = pre.gto_decision(st, i, combo, table, depth, freqs).dist
    if not is_hero:
        dist = pre.heads_up_dist(st, i, pre.sb_limp_as_raise(dist, st, i, sit, combo, table), size)
    out: dict[str, float] = {}
    for code, f in dist.items():
        kind, _ = pre.code_action(st, i, code, size)
        out[kind] = out.get(kind, 0.0) + f
    return out
