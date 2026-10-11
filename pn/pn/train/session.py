"""Trainer sessions (contracts/trainer.md): deal, villain policy, background street solves,
hero grading and running totals. Hand state is immutable; a Session swaps it under a lock.

Flow: `advance()` lets villains act until hero is due, a solve is in flight, or the hand ends.
Heads-up flops come from the solved set (pn.train.flop_library, 2026-10-07): the board is drawn
from the boards whose texas flop document is cached for the hand's matchup, and that document is
the flop tree; with no solved board the hand ends at the flop (solve note "no_library_solve").
Turn and river are lifted from the flop tree's leaf (pn.solve.subgame) and solved live with the
trainer preset; a street with no leaf to lift from is not solved (note "off_tree"). Villains and
hero wait while a solve runs; a failed solve switches the street to the fallback policy.
Once a postflop street starts with 3+ players, that street and every later street of the hand
are not solved (2026-10-07: 2-player only): solve status "none" with note "multiway", villains
play the fallback policy, hero decisions are recorded ungraded with note "multiway_unsolvable".
Plan 7: hero decisions are graded by frequency (pn.train.grading: best / mixed / blunder), every
villain plays the GTO / chart strategy (source "gto" only, no profile blend, no locked solves).
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pn import ranges
from pn.adapter.state import BOARD_LEN, STREETS
from pn.ingest import db as ingest_db
from pn.solve import SolveCache, SpotError, SpotRequest, SubgameError, build_spot
from pn.solve import flops as fl
from pn.solve.backend import MULTIWAY_UNSOLVABLE, SolveError
from pn.solve.request import TABLE_SEATS, apply_preset
from pn.solve.sources import ProfileSource
from pn.solve.subgame import subgame_request
from pn.train import drills, grading, spots, stats
from pn.train import flop_library as flib
from pn.train import postflop as post
from pn.train import preflop as pre
from pn.train import rng as dice
from pn.train import suitmap as sm
from pn.train.drills import Drill
from pn.train.engine import (
    IllegalAction,
    State,
    apply,
    hand_decided,
    next_street,
    round_closed,
    start,
)
from pn.train.leaks import Leaks, Line
from pn.train.postflop import Player, Tree
from pn.train.showdown import settle

BB = 100  # cents
SB = BB // 2
DEPTHS = (100, 200)
TRAIN_SOURCES = ("gto",)  # Plan 7: no opponent-specific (locked) solves
MAX_SESSIONS = 32
TRAINER_PRESET = "trainer"
RANKS, SUITS = "23456789TJQKA", "shdc"
DECK = tuple(r + s for r in RANKS for s in SUITS)
SOLVE_ERRORS = (SolveError, SpotError, SubgameError, ValueError, KeyError, OSError)
SOLVER_UNAVAILABLE = "solver_unavailable"
MULTIWAY = "multiway"  # solve.note of a street that starts with 3+ players
NO_LIBRARY = "no_library_solve"  # heads-up flop without a solved board: the hand ends there
OFF_TREE = "off_tree"  # no flop / turn leaf to lift the street from: not solved
LEAKS_TOP = 3
log = logging.getLogger(__name__)

class TrainError(ValueError):
    """Bad request for the session's current state."""


class SessionNotFound(LookupError):
    """Unknown (or evicted) session id."""


@dataclass(frozen=True)
class Config:
    table: int = 6
    depth: int = 100
    hero_seat: str | None = None  # starting position; None = random
    opponents: tuple[str | None, ...] = ()  # player id per villain seat (None = GTO bot)
    source: str = "gto"
    seed: int | None = None
    rng_enabled: bool = True  # roll 1-100 for each hero decision
    window: int = 1000  # hands in state.rolling
    drill_share: float = 0.8  # share of hands dealt as leak drills (needs a Leaks model)

    def __post_init__(self) -> None:
        if self.table not in TABLE_SEATS:
            raise TrainError(f"table must be one of {sorted(TABLE_SEATS)}")
        if self.depth not in DEPTHS:
            raise TrainError(f"depth must be one of {DEPTHS}")
        if self.hero_seat is not None and self.hero_seat not in TABLE_SEATS[self.table]:
            raise TrainError(f"hero_seat must be one of {TABLE_SEATS[self.table]}")
        if len(self.opponents) > self.table - 1:
            raise TrainError(f"at most {self.table - 1} opponents")
        if self.source not in TRAIN_SOURCES:
            raise TrainError(f"source must be one of {TRAIN_SOURCES}")
        if self.window < 1:
            raise TrainError("window must be >= 1")
        if not 0.0 <= self.drill_share <= 1.0:
            raise TrainError("drill_share must be in [0, 1]")

    @property
    def table_kind(self) -> str:
        return f"{self.table}max"


@dataclass(frozen=True)
class Totals:
    hands: int = 0
    decisions: int = 0  # graded decisions (best / mixed / blunder)
    preflop_decisions: int = 0
    freq_error_sum: float = 0.0
    best: int = 0
    mixed: int = 0
    blunders: int = 0
    hero_net: int = 0  # cents, informational

    def add(self, grade: Mapping[str, Any]) -> Totals:
        """Count one graded decision (grade best | mixed | blunder)."""
        g = grade["grade"]
        t = replace(self, decisions=self.decisions + 1, best=self.best + (g == grading.BEST),
                    mixed=self.mixed + (g == grading.MIXED),
                    blunders=self.blunders + (g == grading.BLUNDER))  # fmt: skip
        if grade.get("street") == "preflop":
            t = replace(t, preflop_decisions=t.preflop_decisions + 1,
                        freq_error_sum=t.freq_error_sum + float(grade["freq_error"]))  # fmt: skip
        return t

    def view(self) -> dict[str, Any]:
        pf, n = self.preflop_decisions, self.decisions
        return {"decisions": n, "best": self.best, "mixed": self.mixed, "blunders": self.blunders,
                "blunder_rate": round(self.blunders / n, 4) if n else None,
                "hands": self.hands, "preflop_decisions": pf,
                "avg_freq_error": round(self.freq_error_sum / pf, 4) if pf else None,
                "hero_net": self.hero_net}  # fmt: skip


@dataclass(frozen=True)
class SolveInfo:
    status: str = "none"  # none | solving | ready | failed | fallback
    backend: str | None = None
    note: str | None = None
    name: str | None = None  # Registry name of the street's solve (grid_ref)


@dataclass(frozen=True)
class Hand:
    no: int
    rotation: int
    players: tuple[Player, ...]  # engine seat order (preflop order)
    state: State
    deck: tuple[str, ...]
    tree: Tree | None = None
    solve: SolveInfo = SolveInfo()
    feedback: dict | None = None
    result: dict | None = None
    origins: tuple[str | None, ...] = ()  # per history entry: how a villain chose it
    tokens: tuple[str, ...] = ()  # per history entry: the action token taken
    rng: int | None = None  # roll for hero's pending decision
    decisions: tuple[dict, ...] = ()  # train_decisions rows, written at hand end
    drill: Drill | None = None  # active drill (None: natural hand or the drill was left)
    drill_spot: str | None = None  # the drill attempted this hand (kept after a deviation)
    drill_landed: bool = False
    multiway: bool = False  # a postflop street started with 3+ players: no more solves this hand
    perm: sm.Perm = sm.IDENTITY  # library flop document suit -> dealt suit (Pick.perm)


@dataclass
class Seating:
    """Physical seats: 0 is hero, 1.. the opponents (player id or None) with display names."""

    ids: list[str | None]
    names: list[str]


@dataclass
class Session:
    sid: str
    cfg: Config
    seating: Seating
    rng: random.Random
    hand: Hand
    dice: random.Random  # RNG rolls only, so the deals do not depend on rng_enabled
    drill_rng: random.Random  # drill picks and drill hands
    totals: Totals = Totals()
    rolling: dict | None = None  # stats.totals over the session's window (None without a DB)
    future: Future | None = None
    pending_req: SpotRequest | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


def _grid_ref(grade: Mapping[str, Any], h: Hand) -> dict[str, str] | None:
    """Where the UI finds the strategy grid of a graded decision."""
    if str(grade.get("origin", "")).startswith("gtow:"):
        return {"kind": "gtow", "key": grade["origin"][len("gtow:") :]}
    if grade.get("origin") == "solve" and h.solve.name and h.tree is not None:
        return {"kind": "solve", "name": h.solve.name, "node_id": h.tree.node}
    return None


class Trainer:
    """In-memory sessions. Street solves go through `cache` (trainer preset, hard-timeout
    runner); `register(doc, req) -> name` publishes them for grid_ref; `background=False`
    solves inline (tests)."""

    def __init__(self, cache: SolveCache | None = None, profiles: ProfileSource | None = None,
                 names: Callable[[], Mapping[str, str]] | None = None,
                 background: bool = True, freqs: Mapping | None = None,
                 register: Callable[[dict, SpotRequest], str] | None = None,
                 db: str | Path | None = None, leaks: Leaks | None = None) -> None:  # fmt: skip
        self.cache = cache or SolveCache(runner=post.runner)
        self.profiles = profiles
        self.names = names or dict
        self.background = background
        self.freqs = freqs
        self.register = register  # makes street solves viewable (feedback.grid_ref)
        self.db = db  # trainer history (train_hands / train_decisions); None = not persisted
        self.leaks = leaks  # leak table and drill lines; None = no drills
        self.flops = flib.FlopIndex(self.cache.root)  # solved heads-up flop boards
        self._drill_mids: dict[tuple[int, int, tuple], str | None] = {}  # line -> matchup id
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="train-solve")
        self._lock = threading.Lock()

    # ------------------------------------------------------------ sessions
    def new(self, cfg: Config) -> dict[str, Any]:
        rng = random.Random(cfg.seed)
        order = TABLE_SEATS[cfg.table]
        ids = [None, *cfg.opponents, *[None] * (cfg.table - 1 - len(cfg.opponents))]
        known = self.names()
        names = ["Hero"] + [known.get(pid, pid) if pid else f"GTO bot {k}"
                            for k, pid in enumerate(ids[1:], 1)]  # fmt: skip
        start = order.index(cfg.hero_seat) if cfg.hero_seat else rng.randrange(cfg.table)
        sid = uuid.uuid4().hex[:12]
        seed = cfg.seed if cfg.seed is not None else os.urandom(16).hex()
        drill_rng = random.Random(f"drill:{seed}")
        hand = self._deal_hand(cfg, rng, drill_rng, start, 1, ids, names)
        s = Session(sid, cfg, Seating(ids, names), rng, hand, random.Random(f"rng:{seed}"),
                    drill_rng)  # fmt: skip
        s.rolling = self._rolling(cfg)
        with self._lock:
            self.sessions[sid] = s
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
        return self.state(sid)

    def get(self, sid: str) -> Session:
        with self._lock:
            s = self.sessions.get(sid)
        if s is None:
            raise SessionNotFound(f"no trainer session {sid!r}")
        return s

    def state(self, sid: str) -> dict[str, Any]:
        s = self.get(sid)
        with s.lock:
            self._advance(s)
            return self._view(s)

    def act(self, sid: str, token: str) -> dict[str, Any]:
        s = self.get(sid)
        with s.lock:
            self._advance(s)
            self._hero_act(s, token)
            self._advance(s)
            return self._view(s)

    def next(self, sid: str) -> dict[str, Any]:
        s = self.get(sid)
        with s.lock:
            if s.hand.result is None:
                raise TrainError("the hand is not over")
            if s.future is not None and not s.future.done():
                raise TrainError("a solve is still running")
            s.future, s.pending_req = None, None
            rot = (s.hand.rotation - 1) % s.cfg.table  # the button moves one seat
            s.hand = self._deal_hand(s.cfg, s.rng, s.drill_rng, rot, s.hand.no + 1, s.seating.ids,
                                     s.seating.names)  # fmt: skip
            self._advance(s)
            return self._view(s)

    # ------------------------------------------------------------ dealing
    def _deal_hand(self, cfg: Config, rng: random.Random, drill_rng: random.Random, rotation: int,
                   no: int, ids: list[str | None], names: list[str]) -> Hand:  # fmt: skip
        """A natural deal, or with probability drill_share a drill: hero sits in the spot's
        seat and the line's players get hands sampled from the solver's ranges."""
        plan = self._plan(cfg, drill_rng)
        if plan is None:
            return self._deal(cfg, rng, rotation, no, ids, names)
        drill, fixed = plan
        rotation = TABLE_SEATS[cfg.table].index(drill.line.hero)
        hand = self._deal(cfg, rng, rotation, no, ids, names, fixed)
        return replace(hand, drill=drill, drill_spot=drill.spot)

    def _plan(self, cfg: Config, drill_rng: random.Random) -> tuple[Drill, dict] | None:
        if self.leaks is None or cfg.drill_share <= 0 or drill_rng.random() >= cfg.drill_share:
            return None
        try:
            if self.db is not None:
                with ingest_db.opened(self.db) as conn:
                    rows = self.leaks.rows(conn, cfg.table, cfg.depth, cfg.window)
            else:
                rows = self.leaks.rows(None, cfg.table, cfg.depth, cfg.window)
            rows = [r for r in rows if r["weight"] > 0 and self._drill_line(cfg, r["spot"])]
            if not rows:
                return None
            spot = drill_rng.choices([r["spot"] for r in rows], [r["weight"] for r in rows])[0]
            line = self._drill_line(cfg, spot)
            if line is None or line.hero not in TABLE_SEATS[cfg.table]:
                return None
            flop_rng, dead = self._flop_ranges(cfg, line, drill_rng) if spot.startswith("post|") else (None, ())
            fixed = drills.sample_hands(line, drill_rng, self.freqs, cfg.depth, flop_rng, dead)
        except (sqlite3.Error, OSError, ValueError, KeyError, IndexError) as e:
            log.warning("drill skipped: %s", e)
            return None
        return Drill(spot, line), fixed

    def _drill_line(self, cfg: Config, spot: str) -> Line | None:
        """The line to deal a drill of `spot`: preflop spots the leak model's line; a postflop
        spot the highest-reach line of its pot and side whose heads-up flop has at least one
        solved board (the drill would otherwise end at the flop), None without one."""
        if self.leaks is None:
            return None
        if not spot.startswith("post|"):
            return self.leaks.line(cfg.table, cfg.depth, spot)
        for line in self.leaks.lines(cfg.table, cfg.depth, spot):
            k = (cfg.table, cfg.depth, line.steps)
            if k not in self._drill_mids:
                st = flib.replay(TABLE_SEATS[cfg.table], cfg.depth,
                                 [(x.position, x.kind) for x in line.steps], sb=SB, bb=BB,
                                 freqs=self.freqs)  # fmt: skip
                m = flib.matchup_of(st, cfg.table_kind, cfg.depth) if st is not None else None
                self._drill_mids[k] = m.mid if m is not None else None
            mid = self._drill_mids[k]
            if mid is not None and self.flops.boards(mid):
                return line
        return None

    def _flop_ranges(self, cfg: Config, line: Line,
                     drill_rng: random.Random) -> tuple[dict | None, tuple[str, ...]]:  # fmt: skip
        """(ranges, board cards) of one solved board of the line's matchup, drawn by weight:
        the flop players' hands come from the library ranges and avoid the board, so the drill
        lands on a graded combo with that board still in the deck."""
        mid = self._drill_mids.get((cfg.table, cfg.depth, line.steps))
        boards = sorted(self.flops.boards(mid).items()) if mid else []
        if not boards:
            return None, ()
        board, entry = drill_rng.choices(boards, [max(1, int(e.get("weight", 1))) for _, e in boards])[0]
        doc = self.cache.get(entry["key"])
        if doc is None:
            return None, ()
        return drills.flop_ranges(doc), tuple(fl.flop_cards(board))

    @staticmethod
    def _deal(cfg: Config, rng: random.Random, rotation: int, no: int, ids: list[str | None],
              names: list[str], fixed: Mapping[str, tuple[str, str]] | None = None) -> Hand:  # fmt: skip
        """Physical seat s sits at position order[(s + rotation) % n]; hero is seat 0.
        `fixed` hole cards by position (drills); everyone else draws from the shuffled rest."""
        order = TABLE_SEATS[cfg.table]
        n = cfg.table
        fixed = fixed or {}
        taken = {c for cards in fixed.values() for c in cards}
        deck = [c for c in DECK if c not in taken]
        rng.shuffle(deck)
        by_pos = {order[(s + rotation) % n]: s for s in range(n)}
        players = []
        for pos in order:
            s = by_pos[pos]
            cards = fixed.get(pos) or (deck.pop(), deck.pop())
            players.append(Player(pos, ids[s], names[s], s == 0, cards))
        st = start(order, cfg.depth * BB, SB, BB)
        return Hand(no, rotation, tuple(players), st, tuple(deck))

    # ------------------------------------------------------------ the loop
    def _advance(self, s: Session) -> None:
        while s.hand.result is None:
            if s.future is not None:
                if not s.future.done():
                    return
                self._collect(s)
                continue
            st = s.hand.state
            if hand_decided(st) or (round_closed(st) and st.street == "river"):
                self._finish(s)
                return
            if round_closed(st):
                self._next_street(s)
                continue
            i = st.to_act
            if s.hand.players[i].hero:
                if s.cfg.rng_enabled and s.hand.rng is None:
                    s.hand = replace(s.hand, rng=s.dice.randint(1, 100))
                return
            self._villain(s, i)

    def _villain(self, s: Session, i: int) -> None:
        h, st = s.hand, s.hand.state
        p = h.players[i]
        if h.drill is not None and not h.drill.landed and self._forced(s, i):
            return
        if st.street == "preflop":
            kind, to, origin = pre.villain_action(st, i, p.combo, rng=s.rng,
                                                  table=s.cfg.table_kind, depth=s.cfg.depth,
                                                  freqs=self.freqs)  # fmt: skip
            self._apply(s, kind, to, origin, None, kind)
            return
        opts = post.tree_actions(h.tree, st) if h.tree else None
        if opts is None:
            kind = post.fallback_villain(st, i)
            self._apply(s, kind, None, "fallback", None, kind)
            return
        token = post.villain_token(h.tree, p.combo, s.rng)
        opt = next(o for o in opts if o["token"] == token)
        self._apply(s, opt["kind"], opt["to"], f"solve:{token}", opt["child"], token)

    def _forced(self, s: Session, i: int) -> bool:
        """Drill villain action (preflop line, postflop steering); False = act naturally."""
        h, st = s.hand, s.hand.state
        drill = h.drill
        if st.street == "preflop":
            try:
                kind = drills.preflop_step(drill, st, i)
            except LookupError:
                self._leave_drill(s)
                return False
            if kind is None:
                return False
            sit = pre.situation(st, i)
            found = pre.gtow_spot(sit.key, s.cfg.depth, self.freqs)
            size = pre.raise_to(st, i, sit, found[1] if found else None)
            owe = st.to_call(i)
            if kind in ("raise", "allin"):
                k, to = pre.code_action(st, i, "R" if kind == "raise" else "A", size)
            else:
                k, to = ("fold" if kind == "fold" and owe else "call" if owe else "check"), None
            self._apply(s, k, to, "drill", None, k)
            return True
        opts = post.tree_actions(h.tree, st) if h.tree else None
        if opts is None:
            return False
        token = drills.postflop_token(drill, st, i, opts, h.tree.here.get("summary"))
        if token is None:
            return False
        opt = next(o for o in opts if o["token"] == token)
        self._apply(s, opt["kind"], opt["to"], f"drill:{token}", opt["child"], token)
        return True

    def _leave_drill(self, s: Session) -> None:
        """The hand left the line: it plays on naturally (the attempt stays in drill_spot)."""
        s.hand = replace(s.hand, drill=None)

    def _apply(self, s: Session, kind: str, to: int | None, origin: str | None,
               child: str | None, token: str) -> None:  # fmt: skip
        h = s.hand
        try:
            st = apply(h.state, kind, to)
        except IllegalAction:  # the tree and the engine disagree: play on without the tree
            st = apply(h.state, "call" if h.state.to_call(h.state.to_act) else "check")
            child = None
        tree = h.tree.at(child) if (h.tree is not None and child is not None) else None
        solve = h.solve if tree is not None or h.solve.status != "ready" else replace(
            h.solve, status="fallback", note="tree and hand diverged; fallback policy")  # fmt: skip
        s.hand = replace(h, state=st, tree=tree, solve=solve, origins=(*h.origins, origin),
                         tokens=(*h.tokens, token))

    # ------------------------------------------------------------ streets and solves
    def _next_street(self, s: Session) -> None:
        h, st = s.hand, s.hand.state
        nxt = STREETS[STREETS.index(st.street) + 1]
        k = BOARD_LEN[nxt] - len(st.board)
        pick = matchup = None
        if nxt == "flop" and len(st.in_hand()) == 2:
            matchup = flib.matchup_of(st, s.cfg.table_kind, s.cfg.depth)
            if matchup is not None:
                pick = flib.pick_board(self.flops, self.cache, matchup.mid, h.deck, s.rng)
        if pick is not None:
            cards, deck = pick.board, tuple(c for c in h.deck if c not in pick.board)
        else:
            cards, deck = h.deck[:k], h.deck[k:]
        new = next_street(st, cards)
        s.hand = replace(h, state=new, deck=deck, tree=None, perm=pick.perm if pick else h.perm)
        if h.drill is not None and drills.missed(h.drill, nxt):
            self._leave_drill(s)
        if new.to_act is None:  # nobody can bet: run it out
            s.hand = replace(s.hand, solve=SolveInfo())
            return
        if h.multiway or len(new.in_hand()) > 2:  # multiway: no solve for the rest of the hand
            s.hand = replace(s.hand, solve=SolveInfo(note=MULTIWAY), multiway=True)
            if s.hand.drill is not None and not s.hand.drill.landed:
                self._leave_drill(s)  # postflop drills land only in heads-up pots
            return
        if nxt == "flop":
            self._library_flop(s, matchup, pick)
            return
        node = h.tree.here if h.tree is not None else None
        try:
            card = sm.permute_card(cards[0], sm.inverse(h.tree.perm)) if node is not None else None
            if node is not None and node.get("type") == "chance" and card in node["children"]:
                tree = h.tree.at(node["children"][card])
                s.hand = replace(s.hand, tree=tree, solve=replace(h.solve, status="ready"))
                return
            if node is None or node.get("kind") != "leaf":  # never re-solved from the preflop line
                s.hand = replace(s.hand, solve=SolveInfo(note=OFF_TREE))
                return
            req = _lift(h, cards)
            spot = post.ensure_combos(build_spot(req, self.profiles),
                                      {p.position: p.combo for i, p in enumerate(h.players)
                                       if not new.folded[i]})  # fmt: skip
        except SOLVE_ERRORS as e:
            s.hand = replace(s.hand, solve=SolveInfo("failed", note=f"spot: {e}"))
            return
        # trainer preset: heads-up turn / river with the preset's backend (texas, leaf
        # terminals after the street). The cache key ignores the backend.
        spot, backend = apply_preset(spot, TRAINER_PRESET)
        s.pending_req = req
        s.hand = replace(s.hand, solve=SolveInfo("solving"))
        if self.background:
            s.future = self._pool.submit(self.cache.solve, spot, backend, priority=True)
        else:
            f: Future = Future()
            try:
                f.set_result(self.cache.solve(spot, backend, priority=True))
            except SOLVE_ERRORS as e:
                f.set_exception(e)
            s.future = f

    def _library_flop(self, s: Session, matchup: flib.Matchup | None,
                      pick: flib.Pick | None) -> None:  # fmt: skip
        """The solved flop document becomes the street's tree; without one the hand ends."""
        if matchup is None or pick is None:
            s.hand = replace(s.hand, solve=SolveInfo(note=NO_LIBRARY))
            if s.hand.drill is not None and not s.hand.drill.landed:
                self._leave_drill(s)
            self._finish(s)
            return
        f: Future = Future()
        f.set_result(pick.doc)
        s.future, s.pending_req = f, matchup.request(pick.board)
        s.hand = replace(s.hand, solve=SolveInfo("solving"))

    def _collect(self, s: Session) -> None:
        fut, req = s.future, s.pending_req
        s.future, s.pending_req = None, None
        try:
            doc = fut.result()
            perm = s.hand.perm if req.street == "flop" else sm.IDENTITY  # flops: library only
            tree = Tree(doc, doc["root"], req, perm)
            backend = (doc.get("meta") or {}).get("backend")
            if post.tree_actions(tree, s.hand.state) is None:
                raise ValueError("solved root does not match the hand")
            canon = sm.permute_request(req, sm.inverse(perm))  # registered on the document's board
            name = self.register(doc, canon) if self.register else None
            s.hand = replace(s.hand, tree=tree, solve=SolveInfo("ready", backend, name=name))
        except SOLVE_ERRORS as e:
            s.hand = replace(s.hand, tree=None,
                             solve=SolveInfo("failed", note=f"{e}; fallback policy"))  # fmt: skip

    # ------------------------------------------------------------ hero
    def _options(self, s: Session) -> list[dict[str, Any]]:
        h, st = s.hand, s.hand.state
        i = st.to_act
        if h.result is not None or s.future is not None or i is None or not h.players[i].hero:
            return []
        if st.street == "preflop":
            sit = pre.situation(st, i)
            found = pre.gtow_spot(sit.key, s.cfg.depth, self.freqs)
            size = pre.raise_to(st, i, sit, found[1] if found else None)
            return pre.legal_actions(st, i, size)
        opts = post.tree_actions(h.tree, st) if h.tree else None
        return opts if opts is not None else post.fallback_actions(st, i)

    def _hero_act(self, s: Session, token: str) -> None:
        h, st = s.hand, s.hand.state
        opts = self._options(s)
        if not opts:
            raise TrainError("hero is not due to act" if s.future is None else "solve in progress")
        opt = next((o for o in opts if o["token"] == token), None)
        if opt is None:
            raise TrainError(f"{token!r} is not legal here: {[o['token'] for o in opts]}")
        i = st.to_act
        combo = h.players[i].combo
        spot = spots.from_state(st, i)
        if h.drill is not None and not h.drill.landed:
            if spot == h.drill.spot:
                s.hand = h = replace(h, drill=replace(h.drill, landed=True), drill_landed=True)
            elif st.street == "preflop" and not drills.on_line(h.drill, st, i, token):
                self._leave_drill(s)
                h = s.hand
        if st.street == "preflop":
            grade = pre.hero_grade(st, i, combo, token, s.cfg.table_kind, s.cfg.depth, self.freqs)
        elif h.tree is not None and "child" in opt:
            grade = post.hero_grade(h.tree, combo, token, BB)
        else:
            grade = {"street": st.street, "chosen": token, "origin": "none",
                     **grading.feedback_fields(None, token),
                     "actions": [{"token": o["token"], "to": o["to"], "freq": None, "ev_bb": None}
                                 for o in opts], "loss_bb": None,
                     "note": MULTIWAY_UNSOLVABLE if h.multiway
                     else OFF_TREE if h.solve.note == OFF_TREE else SOLVER_UNAVAILABLE}  # fmt: skip
        options = [{"token": a["token"], "amount": a["to"], "freq": a["freq"], "ev_bb": a["ev_bb"]}
                   for a in grade.pop("actions")]  # fmt: skip
        grade = {"note": None, "preflop_source": None, **grade, "options": options,
                 "node_id": h.tree.node if h.tree and grade["origin"] == "solve" else None,
                 "hand": h.no, "position": h.players[i].position, "board": list(st.board),
                 "pot": st.pot}  # fmt: skip
        grade = {**grade, **dice.judge(options, token, h.rng)}
        if grade["grade"] in grading.GRADES:
            s.totals = s.totals.add(grade)
        row = stats.decision_row(_hand_uid(s), len(h.decisions), grade,
                                 stats.situation_kind(st, i), ranges.combo_class(combo), spot)  # fmt: skip
        ref = (pre.pc_ref(st, i, s.cfg.table_kind) if str(grade["origin"]).startswith("pc:")
               else _grid_ref(grade, h))  # fmt: skip
        s.hand = replace(s.hand, feedback={**grade, "grid_ref": ref}, rng=None,
                         decisions=(*h.decisions, row))  # fmt: skip
        self._apply(s, opt["kind"], opt["to"], None, opt.get("child"), token)
        if token == "fold":
            self._finish(s)

    # ------------------------------------------------------------ hand end
    def _finish(self, s: Session) -> None:
        h, st = s.hand, s.hand.state
        hero = next(i for i, p in enumerate(h.players) if p.hero)
        board, deck = st.board, h.deck
        alive = st.in_hand()
        if st.folded[hero]:
            won = [0] * st.n
        else:
            if len(alive) > 1 and len(board) < 5:
                board, deck = (*board, *deck[: 5 - len(board)]), deck[5 - len(board) :]
            won = settle(st.contributed, st.folded, [p.cards for p in h.players], board)
        net = [w - c for w, c in zip(won, st.contributed, strict=True)]
        showdown = len(alive) > 1 and not st.folded[hero]
        winner = max(range(st.n), key=won.__getitem__) if any(won) else None
        result = {
            "winner": h.players[winner].position if winner is not None else None,
            "amount": sum(st.contributed), "board": list(board), "hero_folded": st.folded[hero],
            "showdown": [{"position": h.players[i].position, "cards": list(h.players[i].cards)}
                         for i in alive] if showdown else [],
            "won": {h.players[i].position: won[i] for i in range(st.n) if won[i]},
            "net": {h.players[i].position: net[i] for i in range(st.n)},
        }  # fmt: skip
        s.totals = replace(s.totals, hands=s.totals.hands + 1,
                           hero_net=s.totals.hero_net + net[hero])  # fmt: skip
        s.hand = replace(h, deck=deck, result=result)
        self._record(s, h.players[hero].position, net[hero])

    def _record(self, s: Session, hero_pos: str, net: int) -> None:
        """Persist the finished hand and refresh state.rolling (a DB error never breaks play)."""
        if self.db is None:
            return
        cfg = s.cfg
        hand = {"hand_uid": _hand_uid(s), "session_id": s.sid,
                "played_at": datetime.now(UTC).isoformat(timespec="microseconds"),
                "table_size": cfg.table, "depth": cfg.depth, "source": cfg.source,
                "hero_pos": hero_pos, "net_bb": net / BB,
                "opponents_json": json.dumps(s.seating.ids[1:]), "drill_spot": s.hand.drill_spot,
                "drill_landed": int(s.hand.drill_landed) if s.hand.drill_spot else None}  # fmt: skip
        try:
            with ingest_db.opened(self.db) as conn:
                stats.record_hand(conn, hand, s.hand.decisions)
        except (sqlite3.Error, OSError) as e:
            log.warning("trainer hand %s not saved: %s", hand["hand_uid"], e)
        s.rolling = self._rolling(cfg)

    def _rolling(self, cfg: Config) -> dict | None:
        if self.db is None:
            return None
        try:
            with ingest_db.opened(self.db) as conn:
                out = stats.totals(stats.rolling(conn, cfg.window))
                top = (self.leaks.rows(conn, cfg.table, cfg.depth, cfg.window)[:LEAKS_TOP]
                       if self.leaks is not None else [])  # fmt: skip
        except (sqlite3.Error, OSError, ValueError, KeyError) as e:
            log.warning("trainer rolling stats unavailable: %s", e)
            return None
        return {**out, "leaks_top": [r["label"] for r in top]}

    # ------------------------------------------------------------ hero view
    def _view(self, s: Session) -> dict[str, Any]:
        """Hero view (contracts/trainer.md "Trainer"); amounts in cents."""
        h, st, cfg = s.hand, s.hand.state, s.cfg
        order = TABLE_SEATS[cfg.table]
        res = h.result
        last: dict[int, str] = {}
        for a, tok in zip(st.history, h.tokens, strict=True):
            last[a.seat] = tok
        seats = []
        for phys in range(cfg.table):  # hero first, then clockwise
            pos = order[(phys + h.rotation) % cfg.table]
            i = st.seat_of(pos)
            p = h.players[i]
            seats.append({"position": pos, "name": p.name, "player_id": p.player_id,
                          "stack": st.stacks[i], "in_hand": not st.folded[i], "is_hero": p.hero,
                          "committed": st.committed[i], "last_action": last.get(i),
                          "allin": st.stacks[i] == 0 and not st.folded[i]})  # fmt: skip
        history = [{"street": a.street, "position": st.positions[a.seat], "token": tok,
                    "amount": a.to if a.kind in ("raise", "call") else None, "added": a.added,
                    "allin": a.allin, "origin": o}
                   for a, tok, o in zip(st.history, h.tokens, h.origins, strict=True)]  # fmt: skip
        legal = [{"token": o["token"], "kind": o["kind"], "amount": o["to"] if o["kind"] != "fold" and o["kind"] != "check"
                  else None, "label": label(o, st)} for o in self._options(s)]  # fmt: skip
        hero_i = next(i for i, p in enumerate(h.players) if p.hero)
        hero = h.players[hero_i]
        return {
            "sid": s.sid, "hand_no": h.no, "table": cfg.table, "depth": cfg.depth, "bb": BB,
            "source": cfg.source, "opponents": list(s.seating.ids[1:]),
            "board": list(st.board), "pot": st.pot, "street": st.street, "button": "BTN",
            "hero": {"position": hero.position, "cards": list(hero.cards),
                     "stack": st.stacks[hero_i]},
            "seats": seats, "history": history,
            "to_act": st.positions[st.to_act] if st.to_act is not None and res is None else None,
            "legal_actions": legal, "solving": s.future is not None,
            "solve": {"status": "solving" if s.future is not None else h.solve.status,
                      "backend": h.solve.backend, "note": h.solve.note, "name": h.solve.name},
            "feedback": h.feedback, "totals": s.totals.view(), "hand_over": res is not None,
            "result": res, "rng": h.rng if res is None else None, "rng_enabled": cfg.rng_enabled,
            "rolling": s.rolling, "drill": h.drill.view() if h.drill is not None else None,
            "drill_share": cfg.drill_share if self.leaks is not None else 0.0,
            "solve_error": h.solve.note if h.solve.status == "failed" else None,
        }  # fmt: skip


def _real_hero(req: SpotRequest, h: Hand) -> SpotRequest:
    """`req` with the session's hero flagged and hero's cards dead for the villains (library flop
    requests carry a canonical hero and no hero cards)."""
    hero = next(p for p in h.players if p.hero)
    seats = tuple(replace(x, hero=x.position == hero.position) for x in req.seats)
    return replace(req, seats=seats, hero_cards=hero.cards)


def _lift(h: Hand, cards: tuple[str, ...]) -> SpotRequest:
    """The next street's request lifted from the tree's leaf (pn.solve.subgame), in actual
    cards: the hint and dealt cards go into the document's suits and the result comes back."""
    inv = sm.inverse(h.tree.perm)
    hint = sm.permute_request(_real_hero(h.tree.req, h), inv)
    req = subgame_request(_hero_doc(h.tree.doc, h), h.tree.node, sm.permute_cards(cards, inv), hint)
    return sm.permute_request(req, h.tree.perm)


def _hero_doc(doc: dict, h: Hand) -> dict:
    """Shallow view of `doc` whose players flag the session's hero (pn.solve.subgame reads the
    hero, whose own cards stay live in its range, from the parent document)."""
    pos = next(p for p in h.players if p.hero).position
    return {**doc, "players": [{**p, "hero": p["position"] == pos} for p in doc["players"]]}


def _hand_uid(s: Session) -> str:
    return f"{s.sid}-{s.hand.no}"


def label(o: Mapping[str, Any], st: State) -> str:
    """Button text for an option (amounts in bb)."""
    tok, i = o["token"], st.to_act
    to_bb = f"{o['to'] / BB:g} bb"
    if tok == "fold":
        return "Fold"
    if tok == "check":
        return "Check"
    if tok == "call":
        return f"Call {(o['to'] - st.committed[i]) / BB:g} bb"
    if tok == "allin":
        return f"All-in {to_bb}"
    if tok.startswith("bet"):
        return f"Bet {tok[3:]}% ({to_bb})"
    return f"Raise to {to_bb}"
