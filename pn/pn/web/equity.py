"""Hand-vs-range equity at a strategy-browser node (GET /api/solves/{name}/equity,
contracts/strategy_output.md "Range equity").

Every runout is enumerated (river: the one showdown, turn: 48 rivers, flop: 1176 turn+river
pairs). Hand values come from two lookup tables built once with `handeval.evaluate` (rank
multiset -> value for non-flush hands, suited-rank mask -> value for flushes), so a whole
runout x combo grid is a few numpy gathers. Card removal uses inclusion-exclusion: the weight
of opponent combos below / equal to a hand is the runout total minus the totals of the two
groups that share one of the hand's cards (plus the hand itself, which is in both)."""

from __future__ import annotations

import hashlib
import itertools
import threading
from collections import OrderedDict
from collections.abc import Sequence
from functools import cache

import numpy as np

from pn import ranges
from pn.profile.handeval import evaluate

N = ranges.N_COMBOS
BIG = 1 << 24  # > every evaluate() value; separates sort groups in composite keys
POW5 = 5 ** np.arange(13, dtype=np.int64)  # rank-count key: sum of 5**rank over the 7 cards
PLAN_CACHE = 2  # per-board runout plans (a flop plan is ~80 MB)
RESULT_CACHE = 64
MAX_COMPUTES = 2  # concurrent flop computes allocate ~150 MB of temporaries each (8 GB machine)
ROUND = 4


# ------------------------------------------------------------------ hand value tables
@cache
def _tables() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sorted rank-count keys, their non-flush values, flush value by 13-bit suited mask)."""
    keys, vals = [], []
    for ranks_ in itertools.combinations_with_replacement(range(13), 7):
        if max(ranks_.count(r) for r in set(ranks_)) > 4:
            continue
        # suits cycle over the 7 cards: <= 2 per suit (no flush), a rank's copies differ in suit
        keys.append(int(POW5[list(ranks_)].sum()))
        vals.append(evaluate([r * 4 + k % 4 for k, r in enumerate(ranks_)]))
    order = np.argsort(keys)
    flush = np.zeros(1 << 13, dtype=np.int64)
    for mask in range(1 << 13):
        rs = [r for r in range(13) if mask >> r & 1]
        if len(rs) >= 5:
            flush[mask] = evaluate([r * 4 for r in rs])
    return np.asarray(keys, dtype=np.int64)[order], np.asarray(vals, dtype=np.int64)[order], flush


@cache
def _combo_cards() -> np.ndarray:
    return np.array([(ranges.card_to_int(c[:2]), ranges.card_to_int(c[2:])) for c in ranges.all_combos()],
                    dtype=np.int64)  # fmt: skip


@cache
def _card_groups() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """members[c] = the 51 combo indices holding card c; pos_a / pos_b = flat position
    (c * 51 + k) of each combo inside the groups of its first / second card."""
    cards = _combo_cards()
    members = np.zeros((52, 51), dtype=np.int64)
    fill = np.zeros(52, dtype=np.int64)
    pos = np.zeros((N, 2), dtype=np.int64)
    for i, (a, b) in enumerate(cards):
        for j, c in enumerate((a, b)):
            members[c, fill[c]] = i
            pos[i, j] = c * 51 + fill[c]
            fill[c] += 1
    return members, pos[:, 0], pos[:, 1]


def hand_values(boards: np.ndarray) -> np.ndarray:
    """Value of every combo on every 5-card board: (R, 1326) int64; combos sharing a card
    with the board get -1."""
    keys, vals, flush = _tables()
    cards = _combo_cards()
    r_, s_ = boards >> 2, boards & 3
    key = POW5[r_].sum(1)[:, None] + (POW5[cards[:, 0] >> 2] + POW5[cards[:, 1] >> 2])[None, :]
    out = vals[np.minimum(np.searchsorted(keys, key), len(keys) - 1)]  # board-blocked: any value
    cr, cs = cards >> 2, cards & 3
    for s in range(4):
        on = s_ == s
        bcnt, bmask = on.sum(1), np.where(on, 1 << r_, 0).sum(1)
        if not (bcnt >= 3).any():
            continue
        con = cs == s
        ccnt, cmask = con.sum(1), np.where(con, 1 << cr, 0).sum(1)
        fl = (bcnt[:, None] + ccnt[None, :]) >= 5
        if fl.any():
            out = np.where(fl, flush[bmask[:, None] | cmask[None, :]], out)
    seen = np.zeros((len(boards), 52), dtype=bool)
    seen[np.arange(len(boards))[:, None], boards] = True
    return np.where(seen[:, cards[:, 0]] | seen[:, cards[:, 1]], -1, out)


# ------------------------------------------------------------------ per-board plan
def _runs(comp: np.ndarray, group: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For sorted composite keys: start of each entry's run of equal keys, end (exclusive) of
    that run, start of its group."""
    n = len(comp)
    idx = np.arange(n, dtype=np.int64)
    new = np.empty(n, dtype=bool)
    new[0], new[1:] = True, comp[1:] != comp[:-1]
    lo = np.maximum.accumulate(np.where(new, idx, 0))
    end = np.empty(n, dtype=bool)
    end[-1], end[:-1] = True, new[1:]
    hi = np.minimum.accumulate(np.where(end, idx + 1, n)[::-1])[::-1]
    gnew = np.empty(n, dtype=bool)
    gnew[0], gnew[1:] = True, group[1:] != group[:-1]
    start = np.maximum.accumulate(np.where(gnew, idx, 0))
    return lo.astype(np.int32), hi.astype(np.int32), start.astype(np.int32)


class _Plan:
    """Sort orders of one board's runouts: globally per runout, and per (runout, card) group."""

    def __init__(self, board: Sequence[int]) -> None:
        dead = set(board)
        live = [c for c in range(52) if c not in dead]
        extra = list(itertools.combinations(live, 5 - len(board)))
        self.boards = np.array([[*board, *e] for e in extra], dtype=np.int64)
        vals = hand_values(self.boards)
        self.blocked = vals < 0
        r = len(self.boards)
        rows = np.arange(r, dtype=np.int64)[:, None]
        comp = (rows * BIG + vals + 1).ravel()
        self.g_order = np.argsort(comp, kind="stable").astype(np.int32)
        self.g_runs = _runs(comp[self.g_order], np.repeat(np.arange(r), N)[self.g_order])
        members, self.pos_a, self.pos_b = _card_groups()
        flat = members.ravel()
        self.c_members = flat
        gid = (rows * 52 + np.repeat(np.arange(52), 51)[None, :]).ravel()
        comp_c = gid * BIG + (vals[:, flat] + 1).ravel()
        self.c_order = np.argsort(comp_c, kind="stable").astype(np.int32)
        self.c_runs = _runs(comp_c[self.c_order], gid[self.c_order])

    def _sums(self, w: np.ndarray, order: np.ndarray, runs: tuple) -> tuple[np.ndarray, np.ndarray]:
        """Weight below / equal to each entry within its group, in original entry order."""
        lo, hi, start = runs
        cw = np.concatenate(([0.0], np.cumsum(w.ravel()[order])))
        less, eq = np.empty(len(order)), np.empty(len(order))
        less[order], eq[order] = cw[lo] - cw[start], cw[hi] - cw[lo]
        return less.reshape(w.shape), eq.reshape(w.shape)

    def versus(self, opp: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(numerator, denominator) per hero combo summed over runouts: opponent weight beaten
        (+ half tied) and total compatible opponent weight."""
        w = np.where(self.blocked, 0.0, opp[None, :])
        less, eq = self._sums(w, self.g_order, self.g_runs)
        wc = w[:, self.c_members]
        c_less, c_eq = self._sums(wc, self.c_order, self.c_runs)
        gtot = wc.reshape(len(w), 52, 51).sum(2)  # weight holding each card, per runout
        a, b = self.pos_a, self.pos_b
        win = less - c_less[:, a] - c_less[:, b]
        tie = eq - c_eq[:, a] - c_eq[:, b] + w
        tot = w.sum(1)[:, None] - gtot[:, a // 51] - gtot[:, b // 51] + w
        live = ~self.blocked
        return (np.where(live, win + 0.5 * tie, 0.0).sum(0), np.where(live, tot, 0.0).sum(0))


_lock = threading.Lock()  # guards the two caches
_build_lock = threading.Lock()  # one plan build at a time (a flop build peaks at ~300 MB)
_computes = threading.BoundedSemaphore(MAX_COMPUTES)
_plans: OrderedDict[tuple[int, ...], _Plan] = OrderedDict()
_results: OrderedDict[tuple, dict] = OrderedDict()


def _cached_plan(board: tuple[int, ...]) -> _Plan | None:
    with _lock:
        if board in _plans:
            _plans.move_to_end(board)
            return _plans[board]
    return None


def _plan(board: tuple[int, ...]) -> _Plan:
    """The board's plan; concurrent cold requests wait for one build instead of each building."""
    plan = _cached_plan(board)
    if plan is not None:
        return plan
    with _build_lock:
        plan = _cached_plan(board)  # built while this thread waited
        if plan is not None:
            return plan
        plan = _Plan(board)
        with _lock:
            _plans[board] = plan
            while len(_plans) > PLAN_CACHE:
                _plans.popitem(last=False)
    return plan


# ------------------------------------------------------------------ public
def range_equity(board: Sequence[str], reach: Sequence[Sequence[float] | None],
                 positions: Sequence[str | None]) -> dict:  # fmt: skip
    """The equity payload for a heads-up node: `reach` is each player's 1326 combo weights
    (None = unknown). ValueError for a bad board or a non-heads-up node."""
    if len(reach) != 2:
        raise ValueError("range equity is heads-up only")
    if not 3 <= len(board) <= 5:
        raise ValueError(f"range equity needs a flop, turn or river board, not {list(board)}")
    ints = tuple(ranges.card_to_int(c) for c in board)
    if len(set(ints)) != len(ints):
        raise ValueError(f"duplicate board card in {list(board)}")
    arrs = [None if r is None else np.asarray(r, dtype=np.float64) for r in reach]
    if any(a is not None and a.shape != (N,) for a in arrs):
        raise ValueError(f"reach must have {N} weights")
    digest = hashlib.blake2b(b"".join(b"-" if a is None else a.tobytes() for a in arrs)).hexdigest()
    key = (ints, digest, tuple(positions))
    with _lock:
        if key in _results:
            _results.move_to_end(key)
            return _results[key]
    with _computes:
        out = _compute(ints, arrs, positions)
    with _lock:
        _results[key] = out
        while len(_results) > RESULT_CACHE:
            _results.popitem(last=False)
    return out


def _compute(board: tuple[int, ...], arrs: list[np.ndarray | None],
             positions: Sequence[str | None]) -> dict:  # fmt: skip
    plan = _plan(board)
    cards = _combo_cards()
    dead = np.isin(cards, board).any(axis=1)
    players = []
    for p, me in enumerate(arrs):
        opp = arrs[1 - p]
        if me is None:
            players.append({"position": positions[p], "equity": None, "combos": 0.0,
                            "per_combo": [None] * N})  # fmt: skip
            continue
        mine = np.where(dead, 0.0, me)
        no_opp = (np.zeros(N), np.zeros(N))
        num, den = no_opp if opp is None else plan.versus(np.where(dead, 0.0, opp))
        ok = (mine > 0) & (den > 0)
        eq = np.divide(num, den, out=np.zeros(N), where=ok)
        tot = float((mine * den).sum())
        players.append({
            "position": positions[p],
            "equity": round(float((mine * num).sum()) / tot, ROUND) if tot > 0 else None,
            "combos": round(float(mine.sum()), ROUND),
            "per_combo": [round(float(x), ROUND) if k else None for x, k in zip(eq, ok, strict=True)],
        })  # fmt: skip
    street = {3: "flop", 4: "turn", 5: "river"}[len(board)]
    return {"board": [ranges.int_to_card(c) for c in board], "street": street,
            "method": "exact" if street == "river" else "enumerated", "runouts": len(plan.boards),
            "players": players}  # fmt: skip
