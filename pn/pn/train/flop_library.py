"""Library-first heads-up flops (contracts/trainer.md 2026-10-07 "boards come from the solved
set"). A flop solve takes minutes, so the trainer never solves a flop live: it deals heads-up
flops only from boards whose texas (TexasSolver) flop document is cached.

- `Matchup`: a heads-up pot at the start of the flop, reached by the trainer's own preflop
  engine and sizes (pn.train.preflop raise_to / code_action), so the library and the trainer
  build the same spot. `matchups()` enumerates limped / srp / 3bp / 4bp+ pots for every
  position pair; `matchup_of(state)` identifies the pot of a live hand.
- `library_request`: the canonical flop SpotRequest (GTO ranges from the preflop line, anonymous
  seats, no dead hero cards, hero flag on the preflop aggressor, else the IP seat); the cached
  document's key is spot_key of it with the `library` solve preset.
- `FlopIndex`: `<cache root>/hu_flop_library.json`, matchup id -> solved boards (board ->
  {key, weight}); written by the library fill, read by the trainer. Plan 16: an entry may be an
  alias `{alias_of: <mid>}` (an 8-max line without UTG/UTG1 action -> the 6-max matchup of the
  same line, whose flop keys are identical; pn.train.library_alias): boards() / accuracy() /
  pick_board read the target's boards.
- `pick_board`: one solved board serves its whole suit-isomorphism class: it is dealt under a
  suit permutation that avoids the live hole cards (`Pick.perm`, pn.train.suitmap); the hand
  reads the document through the inverse permutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pn.adapter.state import order_key
from pn.solve import SolveCache, SpotRequest, build_spot, spot_key
from pn.solve import flops as fl
from pn.solve.request import Budget, Seat, apply_preset, request_to_json
from pn.train import postflop as post
from pn.train import preflop as pre
from pn.train import spots
from pn.train import suitmap as sm
from pn.train.engine import IllegalAction, State, apply, hand_decided, start

INDEX_NAME = "hu_flop_library.json"
LIBRARY_PRESET = "library"
LIBRARY_BACKEND = "texas"  # Plan 7: cached documents of any other backend count as missing
CODE_OF = {"raise": "R", "allin": "A", "call": "C", "check": "X", "fold": "F"}


# ------------------------------------------------------------------ preflop lines


def replay(order: Sequence[str], depth_bb: int, steps: Iterable[tuple[str, str]], *,
           sb: int, bb: int, freqs: Mapping | None = None,
           fold_others: bool = False) -> State | None:  # fmt: skip
    """Engine state at the end of preflop after `steps` ((position, kind) with kind fold | check
    | call | raise | allin, sized like trainer villains). Seats not on the line fold when
    `fold_others`, else the line is broken. None when the line does not end in a heads-up flop
    with betting left."""
    st = start(tuple(order), depth_bb * bb, sb, bb)
    todo = list(steps)
    try:
        while st.to_act is not None:
            i = st.to_act
            pos = st.positions[i]
            if todo and todo[0][0] == pos:
                code = CODE_OF[todo.pop(0)[1]]
            elif fold_others:
                code = "F"
            else:
                return None
            sit = pre.situation(st, i)
            found = pre.gtow_spot(sit.key, depth_bb, freqs)
            kind, to = pre.code_action(st, i, code, pre.raise_to(st, i, sit, found[1] if found else None))
            st = apply(st, kind, to)
    except IllegalAction:
        return None
    if todo or len(st.in_hand()) != 2 or hand_decided(st):
        return None
    return st


def _raisers(st: State) -> list[str]:
    return [st.positions[a.seat] for a in st.history if a.street == "preflop" and a.kind == "raise"]


def library_request(st: State, table_kind: str, board: tuple[str, ...] = ()) -> SpotRequest:
    """Canonical flop request for the heads-up pot of `st` (any street; preflop history only)."""
    pf = post.lines(st)
    live = [st.positions[i] for i in st.in_hand()]
    raisers = _raisers(st)
    aggressor = raisers[-1] if raisers and raisers[-1] in live else None
    hero = aggressor or max(live, key=lambda p: order_key(p, "flop"))
    seats = tuple(Seat(None, p, st.stacks[st.seat_of(p)], hero=p == hero, name=p.lower(),
                       line=pf[p].action if p in pf else None, vs=pf[p].vs if p in pf else None)
                  for p in live)  # fmt: skip
    return SpotRequest(seats=seats, street="flop", board=tuple(board), pot=st.pot, sb=st.sb,
                       bb=st.bb, source="gto", last_aggressor=aggressor, budget=Budget(),
                       table=table_kind)  # fmt: skip


def matchup_id(req: SpotRequest) -> str:
    blob = json.dumps(request_to_json(replace(req, board=())), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Matchup:
    mid: str
    pot: str  # limped | srp | 3bp | 4bp+
    positions: tuple[str, str]  # postflop order: OOP, IP
    depth: int
    table_kind: str
    state: State = field(compare=False, repr=False)

    @property
    def label(self) -> str:
        return f"{self.pot} {self.positions[0]}-{self.positions[1]} {self.depth}bb: {self.line}"

    @property
    def line(self) -> str:
        """The preflop line (pn.train.spots.line_key; FreqMap.matchups key)."""
        st = self.state
        return spots.line_key((st.positions[a.seat], a.kind) for a in st.history
                              if a.street == "preflop")  # fmt: skip

    def request(self, board: tuple[str, ...]) -> SpotRequest:
        return library_request(self.state, self.table_kind, board)


def matchup_of(st: State, table_kind: str, depth: int) -> Matchup | None:
    """The matchup of a hand at the end of preflop / start of the flop (None unless heads-up)."""
    if len(st.in_hand()) != 2:
        return None
    req = library_request(st, table_kind)
    live = tuple(sorted((s.position for s in req.seats), key=lambda p: order_key(p, "flop")))
    return Matchup(matchup_id(req), spots.pot_type(len(_raisers(st))), live, depth, table_kind, st)  # type: ignore[arg-type]


def _plans(order: Sequence[str]) -> list[list[tuple[str, str]]]:
    """Heads-up preflop lines (everyone else folds): a limp checked by the BB; open + call,
    3-bet + call, 4-bet + call; a limp isolated by a raise and called; a 3-bet cold-called after
    the opener folds."""
    out: list[list[tuple[str, str]]] = []
    for oi, o in enumerate(order[:-1]):
        out.append([(o, "call"), ("BB", "check")])
        for ci, c in enumerate(order[oi + 1 :], oi + 1):
            out += [[(o, "raise"), (c, "call")],
                    [(o, "raise"), (c, "raise"), (o, "call")],
                    [(o, "raise"), (c, "raise"), (o, "raise"), (c, "call")],
                    [(o, "call"), (c, "raise"), (o, "call")]]  # fmt: skip
            out += [[(o, "raise"), (c, "raise"), (x, "call")] for x in order[ci + 1 :]]
    return out


def matchups(order: Sequence[str], depth: int, table_kind: str, *, sb: int, bb: int,
             freqs: Mapping | None = None) -> list[Matchup]:  # fmt: skip
    """Every heads-up flop the trainer's preflop engine reaches with one caller (others fold)."""
    out: dict[str, Matchup] = {}
    for plan in _plans(order):
        st = replay(order, depth, plan, sb=sb, bb=bb, freqs=freqs, fold_others=True)
        m = matchup_of(st, table_kind, depth) if st is not None else None
        if m is not None:
            out.setdefault(m.mid, m)
    return list(out.values())


def flop_spot(m: Matchup, board: tuple[str, ...],
              accuracy: float | None = None) -> tuple[SpotRequest, dict[str, Any], str]:  # fmt: skip
    """(request, spot with the library preset, cache key) of a matchup's board. `accuracy`
    (% of pot) replaces the preset's target, so each accuracy pass has its own key."""
    req = m.request(board)
    spot, _ = apply_preset(build_spot(req), LIBRARY_PRESET)
    if accuracy is not None:
        spot = {**spot, "game": {**spot["game"], "target_exploitability": accuracy}}
    return req, spot, spot_key(spot)


# ------------------------------------------------------------------ index


class FlopIndex:
    """`<root>/hu_flop_library.json`: {"matchups": {mid: {label, pot, positions, depth,
    boards: {board: {key, weight, accuracy?}}} | {label, ..., alias_of: <mid>}}}; re-read when
    the file changes. `accuracy`
    (% of pot) is the solve's target when the fill recorded it (Plan 10 accuracy passes)."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / INDEX_NAME
        self._data: dict[str, Any] = {"matchups": {}}
        self._mtime: float | None = None
        self._lock = threading.Lock()
        self._batch: dict[str, Any] | None = None  # the copy record() edits inside batch()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._batch is not None:
                return self._batch
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                self._data, self._mtime = {"matchups": {}}, None
                return self._data
            if mtime != self._mtime:
                try:
                    self._data = json.loads(self.path.read_text())
                except ValueError:
                    return self._data  # half-written by another process: keep the last read
                self._mtime = mtime
            return self._data

    def alias_of(self, mid: str) -> str | None:
        """The matchup id an alias entry reads its boards from (None: not an alias)."""
        return self.load()["matchups"].get(mid, {}).get("alias_of")

    def boards(self, mid: str) -> dict[str, dict[str, Any]]:
        """Solved boards of a matchup; an alias returns its target's (one level)."""
        ms = self.load()["matchups"]
        entry = ms.get(mid, {})
        if "alias_of" in entry:
            entry = ms.get(entry["alias_of"], {})
        return dict(entry.get("boards", {}))

    def record(self, m: Matchup, board: str, key: str, weight: int,
               accuracy: float | None = None, subset: int | None = None) -> None:  # fmt: skip
        """Record a solved board. A new entry carries the table kind (labels do not: 6-max and
        8-max lines share them); `subset` (Plan 16: the flop subset size the fill plans) is
        written on the entry when given."""
        data = self._batch if self._batch is not None else json.loads(json.dumps(self.load()))
        if "alias_of" in data["matchups"].get(m.mid, {}):
            raise ValueError(f"{m.label} is an alias: its boards are the target's")
        entry = data["matchups"].setdefault(m.mid, {"label": m.label, "pot": m.pot,
                                                    "positions": list(m.positions),
                                                    "depth": m.depth, "table": m.table_kind,
                                                    "boards": {}})  # fmt: skip
        if subset is not None:
            entry["subset"] = subset
        entry["boards"][board] = {"key": key, "weight": weight}
        if accuracy is not None:
            entry["boards"][board]["accuracy"] = accuracy
        if self._batch is None:
            self._write(data)

    def alias(self, m: Matchup, target: str) -> bool:
        """Record `m` as an alias of matchup id `target` (the caller checks the flop keys first:
        pn.train.library_alias). False when it already is one; ValueError when `m` has boards of
        its own, is `target`, or `target` is an alias itself."""
        data = self._batch if self._batch is not None else json.loads(json.dumps(self.load()))
        ms = data["matchups"]
        cur = ms.get(m.mid, {})
        if cur.get("alias_of") == target:
            return False
        if target == m.mid or "alias_of" in ms.get(target, {}) or cur.get("boards"):
            raise ValueError(f"cannot alias {m.label} to {target}")
        ms[m.mid] = {"label": m.label, "pot": m.pot, "positions": list(m.positions),
                     "depth": m.depth, "table": m.table_kind, "alias_of": target}  # fmt: skip
        if self._batch is None:
            self._write(data)
        return True

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Defer writes: record() edits one copy of the index until the block ends, then one
        write (a walk over tens of thousands of boards would otherwise rewrite the file each)."""
        data = json.loads(json.dumps(self.load()))
        with self._lock:
            self._batch = data
        try:
            yield
        finally:
            with self._lock:
                self._batch = None
            self._write(data)

    def accuracy(self, mid: str, board: str) -> float | None:
        """The recorded accuracy of a board's solve (None when unknown or unsolved)."""
        acc = self.boards(mid).get(board, {}).get("accuracy")
        return None if acc is None else float(acc)

    def rekey(self, ms: Sequence[Matchup]) -> int:
        """Move entries recorded under an old matchup id to the current id of the same label
        (the id hashes the SpotRequest, so a request-shape change strands the index). Boards
        already under the current id win; returns the number of entries moved. Only entries of
        the same table kind move (an entry without one is a 6-max entry, written before Plan 16);
        alias entries pointing at a moved id are repointed to its new id."""
        by_label = {(m.table_kind, m.label): m.mid for m in ms}
        data = json.loads(json.dumps(self.load()))
        moved = 0
        renamed: dict[str, str] = {}  # old id -> new id, for the alias_of pointers
        for mid, entry in list(data["matchups"].items()):
            new = by_label.get((entry.get("table", "6max"), entry.get("label")))
            if new is None or new == mid:
                continue
            renamed[mid] = new
            if "alias_of" in entry:  # an alias moves as is (an entry under the new id wins)
                data["matchups"].setdefault(new, entry)
                del data["matchups"][mid]
                moved += 1
                continue
            target = data["matchups"].setdefault(new, {**entry, "boards": {}})
            target["boards"] = {**entry["boards"], **target["boards"]}
            del data["matchups"][mid]
            moved += 1
        for entry in data["matchups"].values():  # an alias follows its moved target
            if entry.get("alias_of") in renamed:
                entry["alias_of"] = renamed[entry["alias_of"]]
        if moved:
            self._write(data)
        return moved

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")  # two writers never share one
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(self.path)


@dataclass(frozen=True)
class Pick:
    board: tuple[str, str, str]  # the dealt (actual) cards: the document's board under `perm`
    key: str
    doc: dict  # the canonical library document
    perm: sm.Perm = sm.IDENTITY  # document suit index -> actual suit index (suits "cdhs")

    @property
    def doc_board(self) -> tuple[str, ...]:
        return sm.permute_cards(self.board, sm.inverse(self.perm))


def board_images(cards: Sequence[str], live: set[str]) -> list[tuple[tuple[str, ...], sm.Perm]]:
    """Distinct suit-permuted images of `cards` lying wholly in `live`, each with the first
    permutation (pn.solve.flops.SUIT_PERMS order) that maps the board onto it."""
    out: dict[frozenset[str], tuple[tuple[str, ...], sm.Perm]] = {}
    for perm in fl.SUIT_PERMS:
        image = sm.permute_cards(cards, perm)  # type: ignore[arg-type]
        if set(image) <= live:
            out.setdefault(frozenset(image), (image, perm))  # type: ignore[arg-type]
    return list(out.values())


def pick_board(index: FlopIndex, cache: SolveCache, mid: str, deck: Sequence[str],
               rng: random.Random) -> Pick | None:  # fmt: skip
    """A solved board of the matchup drawn by flop weight, dealt as one of its suit-isomorphic
    images whose cards are all still in `deck`, uniformly over the distinct images (so every raw
    flop of the class is equally likely). Boards with no such image, or whose cached document is
    gone or not a texas solve, are skipped."""
    live = set(deck)
    cand = [(b, e, imgs) for b, e in sorted(index.boards(mid).items())
            if (imgs := board_images(fl.flop_cards(b), live))]  # fmt: skip
    while cand:
        k = rng.choices(range(len(cand)), weights=[max(1, int(e.get("weight", 1))) for _, e, _ in cand])[0]
        _, entry, imgs = cand.pop(k)
        doc = cache.get(entry["key"])
        if doc is not None and (doc.get("meta") or {}).get("backend") == LIBRARY_BACKEND:
            image, perm = rng.choice(imgs)
            return Pick(image, entry["key"], doc, perm)  # type: ignore[arg-type]
    return None
