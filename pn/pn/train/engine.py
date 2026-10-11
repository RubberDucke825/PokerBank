"""No-limit hold'em betting state for the trainer (amounts in cents, immutable).

Players are indexed in preflop action order (pn.adapter.state.PREFLOP_ORDER); postflop order
follows POSTFLOP_ORDER. Raise amounts are street commitments ("raise to"), like the tree's
action `amount`. An all-in is capped at what the deepest other live player can match, as in
crates/tree, so no uncalled excess is ever bet.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pn.adapter.state import BOARD_LEN, STREETS, order_key

ALLIN_THRESHOLD = 0.67  # a raise committing more than this share of the stack becomes all-in


class IllegalAction(ValueError):
    """The action is not legal in this state."""


@dataclass(frozen=True)
class Act:
    street: str
    seat: int
    kind: str  # fold | check | call | raise (bets included)
    to: int  # street commitment after the action
    added: int  # chips put in by the action
    allin: bool = False


@dataclass(frozen=True)
class State:
    positions: tuple[str, ...]  # preflop action order
    street: str
    board: tuple[str, ...]
    stacks: tuple[int, ...]  # behind
    committed: tuple[int, ...]  # this street
    contributed: tuple[int, ...]  # whole hand
    folded: tuple[bool, ...]
    acted: tuple[bool, ...]  # acted since the last raise (this street)
    last_raise: int  # size of the last raise increment (min-raise)
    bets: int  # bets/raises this street (preflop: the big blind counts as one)
    to_act: int | None
    sb: int
    bb: int
    history: tuple[Act, ...] = ()

    @property
    def n(self) -> int:
        return len(self.positions)

    @property
    def pot(self) -> int:
        return sum(self.contributed)

    @property
    def highest(self) -> int:
        return max(self.committed)

    def live(self, i: int) -> bool:
        """In the hand and able to act (not folded, chips behind)."""
        return not self.folded[i] and self.stacks[i] > 0

    def in_hand(self) -> list[int]:
        return [i for i in range(self.n) if not self.folded[i]]

    def to_call(self, i: int) -> int:
        return min(self.highest - self.committed[i], self.stacks[i])

    def max_to(self, i: int) -> int:
        own = self.committed[i] + self.stacks[i]
        others = max((self.committed[q] + self.stacks[q] for q in self.in_hand() if q != i),
                     default=0)  # fmt: skip
        return min(own, others)

    def min_raise_to(self) -> int:
        return self.highest + max(self.last_raise, self.bb)

    def can_raise(self, i: int) -> bool:
        return self.max_to(i) > self.highest

    def order(self) -> list[int]:
        """Seat indices in this street's action order."""
        return sorted(range(self.n), key=lambda i: order_key(self.positions[i], self.street))

    def seat_of(self, position: str) -> int:
        return self.positions.index(position)


def start(positions: tuple[str, ...], stack: int, sb: int, bb: int) -> State:
    """Blinds posted, first preflop seat to act. Every seat starts with `stack`."""
    n = len(positions)
    posted = [{"SB": sb, "BB": bb}.get(p, 0) for p in positions]
    st = State(positions=tuple(positions), street="preflop", board=(),
               stacks=tuple(stack - x for x in posted), committed=tuple(posted),
               contributed=tuple(posted), folded=(False,) * n, acted=(False,) * n,
               last_raise=bb, bets=1, to_act=None, sb=sb, bb=bb)  # fmt: skip
    return replace(st, to_act=_next(st, None))


def _next(st: State, after: int | None) -> int | None:
    """Next seat due to act after `after` (None: from the start of the street), or None when
    the betting round is closed."""
    if len(st.in_hand()) < 2:
        return None
    live = [i for i in st.order() if st.live(i)]
    if not live:
        return None
    if len(live) == 1 and st.committed[live[0]] >= st.highest:
        return None  # nobody left to bet against
    order = st.order()
    start_at = order.index(after) + 1 if after is not None else 0
    for k in range(len(order)):
        i = order[(start_at + k) % len(order)]
        if not st.live(i):
            continue
        if st.committed[i] < st.highest or not st.acted[i]:
            return i
    return None


def _set(t: tuple, i: int, v) -> tuple:
    return (*t[:i], v, *t[i + 1 :])


def apply(st: State, kind: str, to: int | None = None) -> State:
    """New state after seat `st.to_act` takes `kind` (fold|check|call|raise|allin); raise
    needs `to` (street commitment). Raises that cannot be matched are capped at max_to."""
    i = st.to_act
    if i is None:
        raise IllegalAction("nobody is due to act")
    owe = st.to_call(i)
    if kind == "allin":
        kind, to = ("raise", st.max_to(i)) if st.can_raise(i) else ("call", None)
    if kind == "fold":
        if owe == 0:
            raise IllegalAction("fold when checking is free")
        new = replace(st, folded=_set(st.folded, i, True))
        return _after(new, Act(st.street, i, "fold", st.committed[i], 0), i)
    if kind == "check":
        if owe > 0:
            raise IllegalAction(f"cannot check facing {owe}")
        new = replace(st, acted=_set(st.acted, i, True))
        return _after(new, Act(st.street, i, "check", st.committed[i], 0), i)
    if kind == "call":
        if owe == 0:
            raise IllegalAction("nothing to call")
        return _put(st, i, owe, "call")
    if kind == "raise":
        if to is None or not st.can_raise(i):
            raise IllegalAction("raise not possible")
        to = min(to, st.max_to(i))
        if to <= st.highest:
            raise IllegalAction(f"raise to {to} does not exceed {st.highest}")
        if to < st.min_raise_to() and to < st.max_to(i):
            raise IllegalAction(f"raise to {to} below the minimum {st.min_raise_to()}")
        return _put(st, i, to - st.committed[i], "raise")
    raise IllegalAction(f"unknown action {kind!r}")


def _put(st: State, i: int, added: int, kind: str) -> State:
    to = st.committed[i] + added
    raised = kind == "raise"
    acted = tuple(q == i for q in range(st.n)) if raised else _set(st.acted, i, True)
    new = replace(st, stacks=_set(st.stacks, i, st.stacks[i] - added),
                  committed=_set(st.committed, i, to),
                  contributed=_set(st.contributed, i, st.contributed[i] + added), acted=acted,
                  last_raise=max(st.last_raise, to - st.highest) if raised else st.last_raise,
                  bets=st.bets + raised)  # fmt: skip
    return _after(new, Act(st.street, i, kind, to, added, new.stacks[i] == 0), i)


def _after(st: State, act: Act, i: int) -> State:
    st = replace(st, history=(*st.history, act))
    return replace(st, to_act=_next(st, i))


def round_closed(st: State) -> bool:
    return st.to_act is None


def hand_decided(st: State) -> bool:
    """One player left, or no further betting possible on any street."""
    if len(st.in_hand()) < 2:
        return True
    return round_closed(st) and sum(1 for i in st.in_hand() if st.stacks[i] > 0) < 2


def next_street(st: State, cards: tuple[str, ...]) -> State:
    """Deal `cards` and open the next street's betting."""
    nxt = STREETS[STREETS.index(st.street) + 1]
    board = (*st.board, *cards)
    if len(board) != BOARD_LEN[nxt]:
        raise IllegalAction(f"{nxt} needs {BOARD_LEN[nxt]} board cards")
    new = replace(st, street=nxt, board=board, committed=(0,) * st.n, acted=(False,) * st.n,
                  last_raise=st.bb, bets=0, to_act=None)  # fmt: skip
    return replace(new, to_act=_next(new, None))


def raise_cap(st: State, i: int, to: int) -> tuple[str, int]:
    """('raise', to) or ('allin', max_to) when `to` reaches the stack or commits more than
    ALLIN_THRESHOLD of it (the tree's rule). `to` is lifted to the minimum raise."""
    max_to = st.max_to(i)
    to = max(to, st.min_raise_to())
    eff = max_to - st.committed[i]
    if to >= max_to or (to - st.committed[i]) > ALLIN_THRESHOLD * eff:
        return "allin", max_to
    return "raise", to
