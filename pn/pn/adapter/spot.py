"""Build a solver spot (contracts/tree_config.md TOML) from a real hand in the DB.

`build_spot(db, hand_id, street, hero_id)` -> Spot(config dict, toml text, per-player meta).
Postflop: players still in the pot at the street start, in that street's action order, with
stacks behind / pot / board at the street start. Preflop: every player who had not folded
before hero's first decision (folded players' blinds become dead money).
Ranges come from pn.solve.sources (Plan 7: chart ranges of each player's preflop line, no
profile ranges and no locks); `overrides` (player id -> range text) replace a player's range.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pn.adapter.state import (
    AGGRESSIVE,
    POSTFLOP_ORDER,
    POSTS,
    STREETS,
    Line,
    first_voluntary_seq,
    order_key,
    preflop_lines,
    replay,
    street_index,
)
from pn.adapter.validate import to_toml
from pn.profile import charts
from pn.profile.priors import load_priors
from pn.solve.build import DEFAULT_SIZES, SpotError, build
from pn.solve.request import (
    MAX_CALLERS_UNLIMITED,
    ROOT_KNOBS,
    Budget,
    Seat,
    SpotRequest,
    preflop_knobs,
)
from pn.solve.sources import ProfileSource

MAX_PREFLOP_PCT = 2000


@dataclass(frozen=True)
class SpotPlayer:
    player_id: str
    name: str
    position: str
    stack: int
    range_text: str
    hero: bool
    line: str
    range_source: str
    lock_source: str | None  # folded | None (free)
    profile_hands: int
    tip: str | None = None


@dataclass(frozen=True)
class Spot:
    config: dict[str, Any]
    toml: str
    players: tuple[SpotPlayer, ...]
    hand_id: str
    street: str
    hero_id: str
    hero_cards: tuple[str, ...] | None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def hero_index(self) -> int:
        return next(i for i, p in enumerate(self.players) if p.hero)


# ------------------------------------------------------------------ profiles


class ProfileStore:
    """Lazily builds contracts/profile.md profiles from the ingest DB, cached per player."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._loaded: tuple | None = None
        self._cache: dict[str, dict | None] = {}

    def _load(self) -> tuple:
        if self._loaded is None:
            from pn.profile import source
            from pn.profile.stats import aggregate, walk_all

            conn = source.open_db(self.db_path)
            try:
                hands = source.load_hands(conn)
                amap = source.alias_map(conn)
                names = {**source.names_from_hands(hands, amap), **source.display_names(conn)}
            finally:
                conn.close()
            walked = walk_all(hands)
            self._loaded = (walked, amap, names, aggregate(walked, amap), load_priors())
        return self._loaded

    def get(self, player_id: str, default: Any = None) -> dict | None:
        if player_id not in self._cache:
            from pn.profile.build import build_from_walked

            walked, amap, names, raw_all, priors = self._load()
            cid = amap.get(player_id, player_id)
            if cid in raw_all:
                self._cache[player_id] = build_from_walked(
                    cid, walked, alias_map=amap, names=names, priors=priors, raw_all=raw_all
                )
            else:
                self._cache[player_id] = None
        return self._cache[player_id] or default


# ------------------------------------------------------------------ helpers


def load_hand(db: str | Path | sqlite3.Connection, hand_id: str) -> dict[str, Any]:
    from pn.ingest import db as ingest_db

    if isinstance(db, sqlite3.Connection):
        hand = ingest_db.get_hand(db, hand_id)
    else:
        if not Path(db).exists():
            raise SpotError(f"database not found: {db}")
        with ingest_db.opened(db) as conn:
            hand = ingest_db.get_hand(conn, hand_id)
    if hand is None:
        raise SpotError(f"hand {hand_id!r} not found")
    return hand


def default_hero(hand: Mapping[str, Any]) -> str | None:
    for p in hand["players"]:
        if p.get("cards_source") == "hero_export":
            return p["player_id"]
    return None


def _player(hand: Mapping[str, Any], pid: str) -> dict[str, Any]:
    return next(p for p in hand["players"] if p["player_id"] == pid)


def preflop_sizes(hand: Mapping[str, Any]) -> dict[str, Any]:
    """[sizes.preflop] lists matching the hand's actual raise-to sizes (contract defaults
    where the hand did not reach that level)."""
    b = hand["blinds"]
    unit = b.get("straddle") or b["bb"]
    pos = {p["player_id"]: p["position"] for p in hand["players"]}
    out: dict[str, Any] = {"open": [300], "three_bet_ip": [300], "three_bet_oop": [350],
                           "four_bet": [220], "five_bet": ["allin"], "max_bets": 5}  # fmt: skip
    tos: list[tuple[str, int, int]] = []  # (pid, raise_to, callers since previous raise)
    callers = 0
    for a in hand["actions"]:
        if a["street"] != "preflop":
            break
        if a["kind"] == "call":
            callers += 1
        if a["kind"] in AGGRESSIVE and not a.get("allin"):
            tos.append((a["player_id"], int(a["to_amount"]), callers))
            callers = 0
        elif a["kind"] in AGGRESSIVE:
            break

    def pct(x: int, y: int) -> int | None:
        p = round(100 * x / y) if y else 0
        return p if 100 < p <= MAX_PREFLOP_PCT else None

    if tos and (p := pct(tos[0][1], unit)):
        out["open"] = [p]
    if len(tos) > 1 and (p := pct(tos[1][1], tos[0][1])):
        n_callers = tos[1][2]
        if n_callers and p - 100 * n_callers > 100:
            out["squeeze"] = {"base": p - 100 * n_callers, "per_caller": 100}
        ip = order_key(pos[tos[1][0]], "flop") > order_key(pos[tos[0][0]], "flop")
        out["three_bet_ip" if ip else "three_bet_oop"] = [p]
    if len(tos) > 2 and (p := pct(tos[2][1], tos[1][1])):
        out["four_bet"] = [p]
    if len(tos) > 3 and (p := pct(tos[3][1], tos[2][1])):
        out["five_bet"] = [p, "allin"]
    return out


def knob_relaxations(hand: Mapping[str, Any]) -> dict[str, Any]:
    """[sizes.preflop] knob values the hand's real preflop line needs: a limp (call with no
    raise in, not the SB completing) -> limp=all; a call of a 3-bet+ by a player with no
    voluntary chips in -> cold_call_3bet=true; a 3rd caller of one raise -> no max_callers."""
    out: dict[str, Any] = {}
    raises = callers = 0
    voluntary: set[str] = set()
    for a in hand["actions"]:
        if a["street"] != "preflop":
            break
        pid, kind = a["player_id"], a["kind"]
        if kind in AGGRESSIVE:
            raises, callers = raises + 1, 0
        elif kind == "call":
            if raises == 0 and _player(hand, pid)["position"] != "SB":
                out["limp"] = "all"
            if raises >= 2 and pid not in voluntary:
                out["cold_call_3bet"] = True
            if raises >= 1:
                callers += 1
                if callers > ROOT_KNOBS[8]["max_callers"]:
                    out["max_callers"] = MAX_CALLERS_UNLIMITED
        else:
            continue
        voluntary.add(pid)
    return out


def _after_posts_seq(hand: Mapping[str, Any]) -> int:
    """Seq of the first preflop action that is not a blind/ante post."""
    for a in hand["actions"]:
        if a["street"] != "preflop" or a["kind"] not in POSTS:
            return a["seq"]
    return len(hand["actions"])


def _table(hand: Mapping[str, Any]) -> str:
    return charts.table_kind(len(hand["players"]))


# ------------------------------------------------------------------ main entry points


def build_spot_from_hand(
    hand: Mapping[str, Any],
    street: str,
    hero_id: str | None = None,
    *,
    mode: str = "depth_limited",
    sizes: Mapping[str, Any] | None = None,
    profiles: ProfileSource | None = None,
    source: str = "gto",
    overrides: Mapping[str, str] | None = None,
    hero_cards: tuple[str, ...] | list[str] | None = None,
    upto_seq: int | None = None,
    game: Mapping[str, Any] | None = None,
) -> Spot:
    """Spot at the start of `street` of `hand` (see module doc). `upto_seq` (preflop only)
    sets the decision point used to drop players who already folded. `overrides` (player id
    -> range text) replace those players' ranges. `source` must be "gto" (SpotRequest)."""
    street_index(street)
    hero_id = hero_id or default_hero(hand)
    if hero_id is None or hero_id not in {p["player_id"] for p in hand["players"]}:
        raise SpotError(f"hero {hero_id!r} is not in hand {hand['hand_id']}")
    # Preflop: the tree starts after the blinds are posted (pot includes them, stacks behind).
    st = replay(hand, street, upto_seq=_after_posts_seq(hand) if street == "preflop" else None)
    if street != "preflop" and len(st.board) < STREETS.index(street) + 2:
        raise SpotError(f"hand {hand['hand_id']} did not reach the {street}")
    folded_bb: frozenset[str] = frozenset()
    if street == "preflop":
        cut = upto_seq if upto_seq is not None else first_voluntary_seq(hand, hero_id, "preflop")
        gone = replay(hand, "preflop", upto_seq=cut).folded if cut is not None else frozenset()
        # The tree needs a listed BB: one that already folded stays, locked to fold.
        folded_bb = frozenset(pid for pid in gone if _player(hand, pid)["position"] == "BB")
        in_pot = [pid for pid in st.in_hand(hand) if pid not in gone or pid in folded_bb]
        lines: dict[str, Line] = {}
    else:
        in_pot = st.in_hand(hand)
        lines = preflop_lines(hand)
    if hero_id not in in_pot:
        raise SpotError(f"hero {hero_id} is not in the pot at the {street}")
    if len(in_pot) < 2:
        raise SpotError(f"only {len(in_pot)} player(s) in the pot at the {street}")
    if len(in_pot) > 8:
        raise SpotError(f"{len(in_pot)} players: the tree supports at most 8")
    broke = [pid for pid in in_pot if st.stacks[pid] <= 0]
    if broke:
        raise SpotError(f"{', '.join(broke)} all-in before the {street}: no decision to solve "
                        "(the tree needs chips behind for every listed player)")  # fmt: skip
    pos_of = {p["player_id"]: p["position"] for p in hand["players"]}
    if street == "preflop" and len(hand["players"]) == 2:
        pos_of = {pid: ("SB" if p == "BTN" else p) for pid, p in pos_of.items()}
    if any(pos_of[pid] is None for pid in in_pot):
        raise SpotError("a player in the pot has no position")
    blinds = hand["blinds"]
    known = hero_cards or _player(hand, hero_id).get("cards")
    hero_cards_t = tuple(known) if known else None
    seats = tuple(
        Seat(pid, pos_of[pid], max(0, st.stacks[pid]), hero=pid == hero_id,
             name=f"{_player(hand, pid)['name']} @ {pid}",
             line=lines[pid].action if pid in lines else None,
             vs=lines[pid].vs if pid in lines else None, folded=pid in folded_bb)
        for pid in in_pot
    )  # fmt: skip
    size_cfg = {**DEFAULT_SIZES, **(sizes or {})}
    relaxed: dict[str, Any] = {}
    if street == "preflop":
        knobs = preflop_knobs(len(in_pot))
        relaxed = {k: v for k, v in knob_relaxations(hand).items() if k in knobs}
        size_cfg["preflop"] = {**preflop_sizes(hand), **knobs, **relaxed,
                               **(size_cfg.get("preflop") or {})}  # fmt: skip
    la = st.last_aggressor
    req = SpotRequest(
        seats=seats, street=street, board=tuple(st.board), pot=st.pot, sb=blinds["sb"],
        bb=blinds["bb"], ante=blinds.get("ante", 0), straddle=blinds.get("straddle", 0) or 0,
        source=source, overrides=dict(overrides or {}),  # type: ignore[arg-type]
        last_aggressor=pos_of[la] if street != "preflop" and la in in_pot else None,
        budget=Budget(**(game or {})), table=_table(hand), mode=mode, sizes=size_cfg,
        hero_cards=hero_cards_t or (),
    )  # fmt: skip
    built = build(req, profiles if profiles is not None else {})
    meta: list[SpotPlayer] = []
    warnings: list[str] = []
    for r in built.seats:
        s = r.seat
        pname = _player(hand, s.player_id)["name"]
        meta.append(SpotPlayer(s.player_id, pname, s.position, max(0, s.stack), r.range_text,
                               s.hero, s.line or "-", r.range_origin, r.lock_origin,
                               r.profile_hands, None if s.hero else r.tip))  # fmt: skip
    cfg = built.config
    comments = {i: f"{m.player_id} line={m.line} range={m.range_source} lock={m.lock_source}"
                for i, m in enumerate(meta)}  # fmt: skip
    header = (f"pn spot: hand {hand['hand_id']} (#{hand.get('hand_no')}) street={street} "
              f"hero={hero_id} GTO")  # fmt: skip
    if relaxed:
        header += ("\npreflop knobs relaxed for actions taken in the hand: "
                   + ", ".join(f"{k}={v}" for k, v in relaxed.items()))  # fmt: skip
    return Spot(cfg, to_toml(cfg, comments, header), tuple(meta), hand["hand_id"], street,
                hero_id, hero_cards_t, tuple(warnings))  # fmt: skip


def build_spot(
    db: str | Path | sqlite3.Connection,
    hand_id: str,
    street: str,
    hero_id: str | None = None,
    *,
    mode: str = "depth_limited",
    sizes: Mapping[str, Any] | None = None,
    profiles: ProfileSource | None = None,
    source: str = "gto",
    overrides: Mapping[str, str] | None = None,
    hero_cards: tuple[str, ...] | list[str] | None = None,
    game: Mapping[str, Any] | None = None,
) -> Spot:
    """Spot for `hand_id` from the ingest DB (`profiles` only add hand counts and tips)."""
    hand = load_hand(db, hand_id)
    return build_spot_from_hand(hand, street, hero_id, mode=mode, sizes=sizes, profiles=profiles,
                                source=source, overrides=overrides, hero_cards=hero_cards,
                                game=game)  # fmt: skip


__all__ = ["POSTFLOP_ORDER", "ProfileSource", "ProfileStore", "Spot", "SpotError", "SpotPlayer",
           "build_spot", "build_spot_from_hand", "knob_relaxations", "preflop_sizes"]  # fmt: skip
