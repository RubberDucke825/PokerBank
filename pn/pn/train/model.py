"""Trainer session state (contracts/trainer.md): the immutable Config, Totals, SolveInfo and Hand
records and the mutable Session that pn.train.session swaps them in under its lock."""

from __future__ import annotations

import random
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from typing import Any

from pn.solve import SpotRequest
from pn.solve.request import TABLE_SEATS
from pn.train import grading, spots
from pn.train import importance as imp
from pn.train import suitmap as sm
from pn.train import textures as tx
from pn.train.drills import Drill
from pn.train.engine import State
from pn.train.postflop import Player, Tree

DEPTHS = (100, 200)
TRAIN_SOURCES = ("gto",)  # Plan 7: no opponent-specific (locked) solves


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
    drill_spot: str | None = None  # pinned drill: every hand deals this spot (ignores drill_share)
    drill_texture: str | None = None  # pinned flop texture of postflop drills (pn.train.textures)
    drill_formation: str | None = None  # pinned heads-up flop formation (replaces a spot pin)
    p_form: int = imp.P_FORM  # spot importance practice parameter, formations and spots (1..5)
    p_tex: int = imp.P_TEX  # the same for textures (1..5)
    pot_mode: str = "start"  # importance pot: start | avg

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
        if self.drill_spot is not None:
            try:
                spots.parse(self.drill_spot)
            except ValueError as e:
                raise TrainError(str(e)) from e
        if self.drill_formation is not None:
            if self.drill_spot is not None:
                raise TrainError("pin a spot or a formation, not both")
            try:
                imp.parse_formation(self.drill_formation)
            except ValueError as e:
                raise TrainError(str(e)) from e
        if self.drill_texture is not None and self.drill_texture not in tx.TEXTURES:
            raise TrainError(f"texture must be one of {list(tx.TEXTURES)}")
        try:
            imp.check_params(self.p_form, self.p_tex, self.pot_mode)
        except ValueError as e:
            raise TrainError(str(e)) from e

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
    detail: str | None = None  # human text for the note (no_library_solve: why there is no board)
    parent: dict | None = None  # {name, node} of the previous street's solve (while solving)


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
    drill_texture: str | None = None  # the flop texture that drill aimed for (None: any)
    drill_formation: str | None = None  # the formation a formation pin's drill was dealt in
    drill_landed: bool = False
    multiway: bool = False  # a postflop street started with 3+ players: no more solves this hand
    perm: sm.Perm = sm.IDENTITY  # library flop document suit -> dealt suit (Pick.perm)
    hand_feedback: tuple[dict, ...] = ()  # every feedback of the hand, retries included
    replay: bool = False  # rewound by retry: later decisions are graded but never counted
    recorded: bool = False  # finished once: totals and train_hands already have it


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
    snaps: list[Hand] = field(default_factory=list)  # the hand before each hero decision (retry)
    lock: threading.RLock = field(default_factory=threading.RLock)
