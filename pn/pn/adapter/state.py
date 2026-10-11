"""Replay a normalized hand (contracts/hand_history.md) to the state at a street start or at
any action, and classify each player's preflop line. Pure functions over hand dicts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

STREETS = ("preflop", "flop", "turn", "river")
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
# Action order used by the tree crate (crates/tree/DEVIATIONS.md).
PREFLOP_ORDER = ("UTG", "UTG1", "UTG2", "UTG3", "LJ", "HJ", "CO", "BTN", "SB", "BB", "STR")
POSTFLOP_ORDER = ("SB", "BB", "STR", "UTG", "UTG1", "UTG2", "UTG3", "LJ", "HJ", "CO", "BTN")
POSTS = frozenset({"post_sb", "post_bb", "post_missed_bb", "post_straddle", "post_missing_sb",
                   "post_ante"})  # fmt: skip
LIVE_POSTS = frozenset({"post_sb", "post_bb", "post_missed_bb", "post_straddle"})
AGGRESSIVE = frozenset({"bet", "raise"})


class HandStateError(ValueError):
    """The hand cannot be replayed to the requested point."""


def street_index(street: str) -> int:
    if street not in STREETS:
        raise HandStateError(f"bad street {street!r}")
    return STREETS.index(street)


def board_cards(hand: Mapping[str, Any], street: str | None = None) -> list[str]:
    b = hand.get("board") or {}
    cards = [*b.get("flop", []), *([b["turn"]] if b.get("turn") else []),
             *([b["river"]] if b.get("river") else [])]  # fmt: skip
    return cards if street is None else cards[: BOARD_LEN[street]]


def positions(hand: Mapping[str, Any]) -> dict[str, str]:
    return {p["player_id"]: p["position"] for p in hand["players"]}


def order_key(position: str | None, street: str) -> int:
    order = PREFLOP_ORDER if street == "preflop" else POSTFLOP_ORDER
    return order.index(position) if position in order else len(order)


@dataclass(frozen=True)
class StreetState:
    """Everything known at one point of the hand (amounts in cents)."""

    street: str
    pot: int
    stacks: dict[str, int]  # behind, every dealt player
    contributed: dict[str, int]  # whole hand so far, after uncalled refunds
    committed_street: dict[str, int]  # this street so far (live chips only)
    folded: frozenset[str]
    allin: frozenset[str]
    board: list[str]
    last_aggressor: str | None  # last bettor/raiser of the previous street
    street_aggressor: str | None  # last bettor/raiser of this street so far
    actions: list[dict[str, Any]] = field(default_factory=list)  # this street's actions so far

    def in_hand(self, hand: Mapping[str, Any]) -> list[str]:
        """Non-folded players in action order for this street."""
        pos = positions(hand)
        alive = [pid for pid in pos if pid not in self.folded]
        return sorted(alive, key=lambda pid: order_key(pos[pid], self.street))


def _refund(committed: dict[str, int]) -> tuple[str, int] | None:
    """Uncalled excess of the street's top commitment over the second highest."""
    if not committed:
        return None
    ranked = sorted(committed.items(), key=lambda kv: -kv[1])
    top_pid, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    return (top_pid, top - second) if top > second else None


def replay(
    hand: Mapping[str, Any], street: str, *, upto_seq: int | None = None
) -> StreetState:
    """State at the start of `street` (upto_seq=None), or just before action `upto_seq`
    (which must be on `street`). Uncalled bets of completed streets are refunded."""
    target = street_index(street)
    stack0 = {p["player_id"]: int(p["stack_start"]) for p in hand["players"]}
    contributed = dict.fromkeys(stack0, 0)
    folded: set[str] = set()
    allin: set[str] = set()
    committed: dict[str, int] = {}
    cur = 0
    aggressor: str | None = None
    prev_aggressor: str | None = None
    street_actions: list[dict[str, Any]] = []

    def close_street() -> None:
        nonlocal committed, aggressor, prev_aggressor, street_actions
        r = _refund(committed)
        if r is not None and len(stack0) > 1:
            contributed[r[0]] -= r[1]
        prev_aggressor, aggressor, committed, street_actions = aggressor, None, {}, []

    for a in hand["actions"]:
        idx = street_index(a["street"])
        if upto_seq is not None and a["seq"] >= upto_seq:
            break
        if idx > target:
            break
        while cur < idx:
            close_street()
            cur += 1
        if upto_seq is None and idx == target:
            break
        pid = a["player_id"]
        contributed[pid] = contributed.get(pid, 0) + int(a["amount"])
        if a["kind"] not in POSTS or a["kind"] in LIVE_POSTS:
            committed[pid] = committed.get(pid, 0) + int(a["amount"])
        if a["kind"] == "fold":
            folded.add(pid)
        if a["kind"] in AGGRESSIVE:
            aggressor = pid
        if a.get("allin"):
            allin.add(pid)
        street_actions.append(a)
    while cur < target:
        close_street()
        cur += 1
    stacks = {pid: stack0[pid] - contributed.get(pid, 0) for pid in stack0}
    allin |= {pid for pid, s in stacks.items() if s <= 0 and pid not in folded}
    return StreetState(
        street=street, pot=sum(contributed.values()), stacks=stacks, contributed=contributed,
        committed_street=committed, folded=frozenset(folded), allin=frozenset(allin),
        board=board_cards(hand, street), last_aggressor=prev_aggressor,
        street_aggressor=aggressor, actions=street_actions,
    )  # fmt: skip


def streets_reached(hand: Mapping[str, Any]) -> list[str]:
    n = len(board_cards(hand))
    return [s for s in STREETS if BOARD_LEN[s] <= n]


def first_voluntary_seq(hand: Mapping[str, Any], player_id: str, street: str) -> int | None:
    for a in hand["actions"]:
        if a["street"] == street and a["player_id"] == player_id and a["kind"] not in POSTS:
            return a["seq"]
    return None


# ------------------------------------------------------------------ preflop lines


@dataclass(frozen=True)
class Line:
    """A player's preflop line. `action` is a chart action (pn.profile.charts) or one of
    limp / check / cold_call_3bet / cold_4bet / call_5bet; `vs` is the relevant other seat."""

    action: str
    vs: str | None = None
    history: tuple[str, ...] = ()


def preflop_lines(
    hand: Mapping[str, Any], *, upto_seq: int | None = None
) -> dict[str, Line]:
    """Classify each player's voluntary preflop action sequence."""
    pos = positions(hand)
    raises = 0
    raisers: list[str] = []
    lines: dict[str, Line] = {}
    for a in _preflop_voluntary(hand["actions"], upto_seq):
        pid, kind = a["player_id"], a["kind"]
        prev = lines.get(pid)
        hist = (prev.history if prev else ()) + (kind,)
        if kind in AGGRESSIVE:
            raises += 1
            vs = pos.get(raisers[-1]) if raisers else None
            action = {1: "open", 2: "3bet", 3: "4bet"}.get(raises, "5bet")
            if raises == 3 and raisers and pid != raisers[0]:
                action = "cold_4bet"
            raisers.append(pid)
            lines[pid] = Line(action, vs, hist)
        elif kind == "call":
            vs = pos.get(raisers[-1]) if raisers else None
            if raises == 0:
                action = "limp"
            elif raises == 1:
                action = "call"
            elif raises == 2:
                action = "call_3bet" if pid == raisers[0] else "cold_call_3bet"
            elif raises == 3:
                action = "call_4bet" if len(raisers) > 1 and pid == raisers[1] else "call_5bet"
            else:
                action = "call_5bet"
            lines[pid] = Line(action, vs, hist)
        elif kind == "check" and pid not in lines:
            lines[pid] = Line("check", None, hist)
        elif kind == "fold":
            lines[pid] = Line("fold", prev.vs if prev else None, hist)
    return lines


def _preflop_voluntary(actions: Iterable[Mapping], upto_seq: int | None) -> Iterable[Mapping]:
    for a in actions:
        if upto_seq is not None and a["seq"] >= upto_seq:
            return
        if a["street"] != "preflop":
            return
        if a["kind"] not in POSTS:
            yield a


# ------------------------------------------------------------------ next to act


def next_to_act(hand: Mapping[str, Any], state: StreetState) -> str | None:
    """Player due to act at `state` (None: the betting round is closed)."""
    pos = positions(hand)
    order = sorted(pos, key=lambda pid: order_key(pos[pid], state.street))
    live = [pid for pid in order if pid not in state.folded and pid not in state.allin]
    if len([pid for pid in order if pid not in state.folded]) < 2 or not live:
        return None
    highest = max(state.committed_street.values(), default=0)
    acted: set[str] = set()
    last_actor: str | None = None
    for a in state.actions:
        if a["kind"] in POSTS:
            continue
        if a["kind"] in AGGRESSIVE:
            acted = set()
        acted.add(a["player_id"])
        last_actor = a["player_id"]
    start = (order.index(last_actor) + 1) if last_actor in order else 0
    for i in range(len(order)):
        pid = order[(start + i) % len(order)]
        if pid not in live:
            continue
        if state.committed_street.get(pid, 0) < highest:
            return pid
        if pid not in acted and len(live) > 1:
            return pid
    return None
