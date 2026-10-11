"""DB-backed data for the web UI: players, profiles, hero review, hands.

Everything profile-related is computed from the hands in the SQLite DB and cached until the DB
file changes (mtime), so the UI stays fast without a separate profile store.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pn.profile import archetype, source
from pn.profile.baselines import load_baselines
from pn.profile.build import build_from_walked, stats_block
from pn.profile.priors import load_priors
from pn.profile.review import review
from pn.profile.stats import RawStats, aggregate, walk_all
from pn.profile.walk import HandFacts

STREETS = ("preflop", "flop", "turn", "river")
LIST_STATS = ("vpip", "pfr", "three_bet", "af_flop", "wtsd")
MAX_HAND_PAGE = 500


class NotFound(LookupError):
    """Unknown player / hand."""


@dataclass
class Snapshot:
    """Everything derived from one version of the DB file."""

    hands: list[dict]
    walked: list[HandFacts]
    alias_map: dict[str, str]
    names: dict[str, str]
    raw_all: dict[str, RawStats]
    priors: dict
    baselines: dict
    profiles: dict[str, dict] = field(default_factory=dict)
    stats: dict[str, dict] = field(default_factory=dict)


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._snap: Snapshot | None = None
        self._mtime: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ loading
    def _conn(self) -> sqlite3.Connection:
        return source.open_db(self.db_path)

    def snapshot(self) -> Snapshot:
        if not self.db_path.exists():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        mtime = self.db_path.stat().st_mtime
        with self._lock:
            if self._snap is None or mtime != self._mtime:
                self._snap = self._load()
                self._mtime = mtime
            return self._snap

    def _load(self) -> Snapshot:
        conn = self._conn()
        try:
            hands = source.load_hands(conn)
            amap = source.alias_map(conn)
            names = {**source.names_from_hands(hands, amap), **source.display_names(conn)}
        finally:
            conn.close()
        walked = walk_all(hands)
        return Snapshot(hands, walked, amap, names, aggregate(walked, amap), load_priors(),
                        load_baselines())  # fmt: skip

    def resolve(self, query: str) -> str:
        snap = self.snapshot()
        try:
            return source.resolve_target(
                query, snap.alias_map, {**{c: c for c in snap.raw_all}, **snap.names}
            )
        except source.PlayerNotFound as e:
            raise NotFound(str(e)) from e

    # ------------------------------------------------------------ players
    def stats_for(self, cid: str) -> dict:
        snap = self.snapshot()
        if cid not in snap.stats:
            snap.stats[cid] = stats_block(snap.raw_all.get(cid, RawStats()), snap.priors,
                                          snap.baselines)  # fmt: skip
        return snap.stats[cid]

    def players(self) -> list[dict]:
        snap = self.snapshot()
        nets = self._nets()
        out = []
        for cid, raw in snap.raw_all.items():
            st = self.stats_for(cid)
            out.append({
                "canonical_id": cid,
                "name": snap.names.get(cid, cid),
                "hands": raw.hands,
                "showdowns_seen": raw.showdowns_seen,
                "net": nets.get(cid, 0),
                "stats": {k: _brief(st[k]) for k in LIST_STATS if k in st},
                "archetype": archetype.classify(st, raw.hands)["label"],
            })  # fmt: skip
        out.sort(key=lambda p: -p["hands"])
        return out

    def _nets(self) -> dict[str, int]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT canonical_id, SUM(net) AS net FROM hand_players_canon GROUP BY canonical_id"
            ).fetchall()
        finally:
            conn.close()
        return {r["canonical_id"]: r["net"] or 0 for r in rows}

    def profile(self, query: str) -> dict:
        cid = self.resolve(query)
        snap = self.snapshot()
        if cid not in snap.profiles:
            snap.profiles[cid] = build_from_walked(
                cid, snap.walked, alias_map=snap.alias_map, names=snap.names, priors=snap.priors,
                raw_all=snap.raw_all, baselines=snap.baselines,
            )  # fmt: skip
        return snap.profiles[cid]

    def review(self, hero: str) -> dict:
        p = self.profile(hero)
        snap = self.snapshot()
        rows = review(p["canonical_id"], p["leaks"], snap.walked, snap.raw_all, self.stats_for,
                      snap.alias_map, snap.names)  # fmt: skip
        return {"canonical_id": p["canonical_id"], "display_name": p["display_name"],
                "hands": p["hands"], "rows": rows}  # fmt: skip

    def names(self) -> dict[str, str]:
        return self.snapshot().names

    # ------------------------------------------------------------ hands
    def hands(self, player: str | None = None, street: str | None = None,
              showdown: bool | None = None, limit: int = 100, offset: int = 0) -> dict:  # fmt: skip
        if street is not None and street not in STREETS:
            raise ValueError(f"street must be one of {STREETS}")
        limit = max(1, min(limit, MAX_HAND_PAGE))
        cid = self.resolve(player) if player else None
        where, args = [], []
        if cid:
            where.append(
                "h.hand_id IN (SELECT hand_id FROM hand_players_canon WHERE canonical_id = ?)"
            )
            args.append(cid)
        if street:
            where.append(f"{_STREET_SQL} >= ?")
            args.append(STREETS.index(street))
        if showdown is not None:
            where.append(f"{_SD_SQL} {'>' if showdown else '='} 0")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        conn = self._conn()
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM hands h {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT h.json FROM hands h {clause} ORDER BY h.started_at DESC, h.hand_no DESC "
                "LIMIT ? OFFSET ?", (*args, limit, offset),
            ).fetchall()  # fmt: skip
        finally:
            conn.close()
        snap = self.snapshot()
        items = [_hand_row(json.loads(r["json"]), cid, snap.alias_map, snap.names) for r in rows]
        return {"total": total, "limit": limit, "offset": offset, "player": cid, "items": items}

    def hand(self, hand_id: str) -> dict:
        conn = self._conn()
        try:
            row = conn.execute("SELECT json FROM hands WHERE hand_id = ?", (hand_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise NotFound(f"no hand {hand_id!r}")
        hand = json.loads(row["json"])
        snap = self.snapshot()
        names = {p["player_id"]: snap.names.get(snap.alias_map.get(p["player_id"], p["player_id"]),
                                                p["name"])
                 for p in hand["players"]}  # fmt: skip
        canon = {p["player_id"]: snap.alias_map.get(p["player_id"], p["player_id"])
                 for p in hand["players"]}  # fmt: skip
        return {"hand": hand, "names": names, "canonical": canon, "replay": replay(hand)}


_STREET_SQL = (
    "(CASE WHEN json_extract(h.json,'$.board.river') IS NOT NULL THEN 3 "
    "WHEN json_extract(h.json,'$.board.turn') IS NOT NULL THEN 2 "
    "WHEN json_array_length(json_extract(h.json,'$.board.flop')) > 0 THEN 1 ELSE 0 END)"
)
_SD_SQL = "COALESCE(json_array_length(json_extract(h.json,'$.showdowns')), 0)"


def _brief(s: dict) -> dict:
    return {"v": s["post_mean"], "n": s["n"], "ci90": s["ci90"], "gto": s.get("gto")}


def street_reached(hand: dict) -> str:
    b = hand.get("board") or {}
    if b.get("river"):
        return "river"
    if b.get("turn"):
        return "turn"
    return "flop" if b.get("flop") else "preflop"


def _hand_row(hand: dict, cid: str | None, amap: dict[str, str], names: dict[str, str]) -> dict:
    me = None
    if cid:
        me = next((p for p in hand["players"] if amap.get(p["player_id"], p["player_id"]) == cid),
                  None)  # fmt: skip
    winners = []
    for pot in hand.get("pots", []):
        for w in pot.get("winners", []):
            c = amap.get(w["player_id"], w["player_id"])
            winners.append(names.get(c, c))
    return {
        "hand_id": hand["hand_id"],
        "hand_no": hand["hand_no"],
        "started_at": hand.get("started_at"),
        "n_players": len(hand["players"]),
        "board": hand.get("board") or {},
        "street": street_reached(hand),
        "showdown": bool(hand.get("showdowns")),
        "pot": sum(p.get("amount", 0) for p in hand.get("pots", [])),
        "winners": sorted(set(winners)),
        "flags": hand.get("flags", []),
        "bb": (hand.get("blinds") or {}).get("bb", 0),
        "hero": None if me is None else {
            "position": me["position"], "cards": me["cards"],
            "net": hand["net"].get(me["player_id"], 0),
        },
    }  # fmt: skip


def replay(hand: dict) -> list[dict]:
    """Per-street action list with running pot and each player's stack after the action."""
    stacks = {p["player_id"]: p["stack_start"] for p in hand["players"]}
    pot = 0
    board = hand.get("board") or {}
    out: list[dict] = []
    for st in STREETS:
        acts = [a for a in hand["actions"] if a["street"] == st]
        cards = {"preflop": [], "flop": board.get("flop") or [],
                 "turn": [board["turn"]] if board.get("turn") else [],
                 "river": [board["river"]] if board.get("river") else []}[st]  # fmt: skip
        if not acts and not cards:
            continue
        pot_start = pot
        steps = []
        for a in acts:
            pot += a["amount"]
            stacks[a["player_id"]] = stacks.get(a["player_id"], 0) - a["amount"]
            steps.append({**a, "pot_after": pot, "stack_after": stacks[a["player_id"]]})
        out.append({"street": st, "cards": cards, "pot_start": pot_start, "actions": steps})
    return out
