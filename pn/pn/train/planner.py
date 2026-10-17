"""Drill planning for the trainer (contracts/trainer.md "Plan 6 additions", "Plan 13 additions",
"Plan 14 additions"): which spot a hand drills, its line, its flop texture and the solved board's
ranges. `DrillPlanner` is a mixin of pn.train.session.Trainer (it reads the trainer's leaks, db,
background, flops, cache, freqs and _drill_mids).

Plan 14: spots are drawn by importance weight (pn.train.importance, the session's p_form / pot_mode);
a postflop drill draws its flop texture by texture weight (p_tex) among the textures its matchup
has solved boards of, or takes the pinned texture (`Config.drill_texture`). A pinned texture
without a pinned spot draws, by weight, a dealable postflop spot with a solved board of that
texture. A pinned formation (`Config.drill_formation`) deals its highest-reach line, aimed at
hero's first flop decision. No solved board of the texture: the drill falls back to any texture
(logged).
"""

from __future__ import annotations

import logging
import random
import sqlite3
from collections.abc import Callable
from typing import Any

from pn.ingest import db as ingest_db
from pn.solve import flops as fl
from pn.solve.request import TABLE_SEATS
from pn.train import drills, spots
from pn.train import flop_library as flib
from pn.train import importance as imp
from pn.train import textures as tx
from pn.train.drills import Drill
from pn.train.leaks import Line
from pn.train.model import Config, TrainError

SB, BB = 50, 100  # cents (pn.train.session)
PLAN_ERRORS = (sqlite3.Error, OSError, ValueError, KeyError, IndexError)
log = logging.getLogger("pn.train.session")


class TextureIndex:
    """A FlopIndex view keeping only the boards of one texture (flop_library.pick_board reads
    `boards(mid)` only)."""

    def __init__(self, index: flib.FlopIndex, texture: str) -> None:
        self.index, self.texture = index, texture

    def boards(self, mid: str) -> dict[str, dict[str, Any]]:
        return tx.boards_of(self.index.boards(mid), self.texture)


class DrillPlanner:
    """Drill picks (mixin of Trainer)."""

    def _plan(self, cfg: Config, drill_rng: random.Random) -> tuple[Drill, dict] | None:
        if self.leaks is None:
            return None
        pins = (cfg.drill_spot, cfg.drill_texture, cfg.drill_formation)
        pinned = any(x is not None for x in pins)
        if not pinned and (cfg.drill_share <= 0 or drill_rng.random() >= cfg.drill_share):
            return None
        memo: dict[str, Any] = {}

        def tables() -> dict[str, Any]:
            if "t" not in memo:
                memo["t"] = self._importance(cfg)
            return memo["t"]

        try:
            if cfg.drill_formation is not None:
                spot, line = self._formation_drill(cfg) or (None, None)
            else:
                if cfg.drill_spot is not None:
                    spot = cfg.drill_spot
                elif cfg.drill_texture is not None:
                    spot = self._texture_spot(cfg, tables, drill_rng)
                else:
                    spot = self._pick_spot(cfg, tables, drill_rng)
                line = self._drill_line(cfg, spot) if spot is not None else None
            if line is None or line.hero not in TABLE_SEATS[cfg.table]:
                return None
            texture, flop_rng, dead = None, None, ()
            if spot.startswith("post|"):
                texture = self._drill_texture(cfg, line, tables, drill_rng)
                flop_rng, dead = self._flop_ranges(cfg, line, drill_rng, texture)
            fixed = drills.sample_hands(line, drill_rng, self.freqs, cfg.depth, flop_rng, dead)
        except PLAN_ERRORS as e:
            log.warning("drill skipped: %s", e)
            return None
        return Drill(spot, line, texture=texture, formation=cfg.drill_formation), fixed

    def _importance(self, cfg: Config) -> dict[str, Any]:
        """The leak model's importance tables with the session's parameters. In the background
        (web) mode they never wait for the SolveCache scan."""
        kw = {
            "wait": not self.background,
            "p_form": cfg.p_form,
            "p_tex": cfg.p_tex,
            "pot": cfg.pot_mode,
        }
        if self.db is not None:
            with ingest_db.opened(self.db) as conn:
                return self.leaks.importance(conn, cfg.table, cfg.depth, cfg.window, **kw)
        return self.leaks.importance(None, cfg.table, cfg.depth, cfg.window, **kw)

    def _pick_spot(self, cfg: Config, tables: Callable[[], dict],
                   drill_rng: random.Random) -> str | None:  # fmt: skip
        """A dealable spot drawn by weight. In the background (web) mode the deal never waits
        for the SolveCache scan: before the first one finishes the weights come from the
        fallback frequencies (as the importance tab shows them)."""
        rows = [
            r for r in tables()["spots"] if r["weight"] > 0 and self._drill_line(cfg, r["spot"])
        ]
        if not rows:
            return None
        return drill_rng.choices([r["spot"] for r in rows], [r["weight"] for r in rows])[0]

    def _texture_spot(self, cfg: Config, tables: Callable[[], dict],
                      drill_rng: random.Random) -> str | None:  # fmt: skip
        """A dealable postflop spot drawn by weight among those whose matchup has a solved board
        of the pinned texture; without one the highest-priority dealable postflop spot (logged)."""
        first, cand = None, []
        for r in tables()["spots"]:
            if not r["spot"].startswith("post|"):
                continue
            line = self._drill_line(cfg, r["spot"])
            if line is None:
                continue
            if self._boards(cfg, line, cfg.drill_texture):
                cand.append(r)
            first = first or r["spot"]
        if cand:
            return drill_rng.choices([r["spot"] for r in cand],
                                     [max(r["weight"], 1e-9) for r in cand])[0]  # fmt: skip
        if first is not None:
            log.warning("no solved %s flop for a dealable postflop spot: drilling %s on any flop",
                        cfg.drill_texture, first)  # fmt: skip
        return first

    def _drill_texture(self, cfg: Config, line: Line, tables: Callable[[], dict],
                       drill_rng: random.Random) -> str | None:  # fmt: skip
        """The pinned texture, else one drawn by texture weight among the textures of the line's
        solved boards; None (any board) when the pinned one has no solved board (logged)."""
        have = {tx.texture(b) for b in self._boards(cfg, line, None)}
        if cfg.drill_texture is not None:
            if cfg.drill_texture in have:
                return cfg.drill_texture
            log.warning("no solved %s flop for %s: dealing any solved flop", cfg.drill_texture,
                        " ".join(f"{s.position} {s.kind}" for s in line.steps if s.kind != "fold"))  # fmt: skip
            return None
        cand = [t for t in tx.TEXTURES if t in have]
        if not cand:
            return None
        weight = {t["texture"]: t["weight"] for t in tables()["textures"]}
        return drill_rng.choices(cand, [max(weight.get(t, 0.0), 1e-9) for t in cand])[0]

    def _boards(self, cfg: Config, line: Line, texture: str | None) -> dict[str, dict[str, Any]]:
        """Solved boards of the line's heads-up flop (of `texture` when given)."""
        k = (cfg.table, cfg.depth, line.steps)
        if k not in self._drill_mids:
            st = flib.replay(TABLE_SEATS[cfg.table], cfg.depth,
                             [(x.position, x.kind) for x in line.steps], sb=SB, bb=BB,
                             freqs=self.freqs)  # fmt: skip
            m = flib.matchup_of(st, cfg.table_kind, cfg.depth) if st is not None else None
            self._drill_mids[k] = m.mid if m is not None else None
        mid = self._drill_mids[k]
        return tx.boards_of(self.flops.boards(mid), texture) if mid else {}

    def _formation_drill(self, cfg: Config) -> tuple[str, Line] | None:
        """(target spot, line) of the pinned formation: its highest-reach line, aimed at hero's
        first flop decision; None when not reached or without a solved board."""
        line = self.leaks.formation_line(cfg.table, cfg.depth, cfg.drill_formation)
        if line is None or not heads_up_line(line) or not self._boards(cfg, line, None):
            return None
        positions, pot = imp.parse_formation(cfg.drill_formation)
        side = "ip" if spots.is_ip(line.hero, positions) else "oop"
        return f"post|{pot}|{side}|flop|first", line

    def _check_pin(self, cfg: Config) -> None:
        """TrainError unless the pinned spot / formation / texture can be dealt at this table
        and depth."""
        if self.leaks is None:
            raise TrainError("drills need the leak model")
        if cfg.drill_formation is not None:
            drill = self._formation_drill(cfg)
            if drill is None or drill[1].hero not in TABLE_SEATS[cfg.table]:
                raise TrainError(f"no dealable line into {cfg.drill_formation!r}")
            return
        spot = cfg.drill_spot
        if spot is None:
            return
        if cfg.drill_texture is not None and not spot.startswith("post|"):
            raise TrainError("a texture pin needs a postflop spot (or no spot)")
        try:
            line = self._drill_line(cfg, spot)
        except (ValueError, KeyError, IndexError):
            line = None
        if line is None or line.hero not in TABLE_SEATS[cfg.table]:
            raise TrainError(f"no dealable line into {spot!r}")

    def dealable(self, table: int, depth: int, spot_ids: list[str]) -> dict[str, bool]:
        """Per spot id: would a drill pin of it be dealt at this table and depth (the check of
        `/api/train/new` with `drill_spot`)? TrainError on a bad table or depth."""
        Config(table=table, depth=depth)
        out: dict[str, bool] = {}
        for spot in spot_ids:
            try:
                self._check_pin(Config(table=table, depth=depth, drill_spot=spot))
                out[spot] = True
            except TrainError:
                out[spot] = False
        return out

    def _drill_line(self, cfg: Config, spot: str) -> Line | None:
        """The line to deal a drill of `spot`: preflop spots the leak model's line; a postflop
        spot the highest-reach line of its pot and side whose heads-up flop has at least one
        solved board (the drill would otherwise end at the flop), None without one."""
        if self.leaks is None:
            return None
        if not spot.startswith("post|"):
            line = self.leaks.line(cfg.table, cfg.depth, spot)
            return line if line is not None and heads_up_line(line) else None
        for line in self.leaks.lines(cfg.table, cfg.depth, spot):
            if heads_up_line(line) and self._boards(cfg, line, None):
                return line
        return None

    def _flop_ranges(self, cfg: Config, line: Line, drill_rng: random.Random,
                     texture: str | None = None) -> tuple[dict | None, tuple[str, ...]]:  # fmt: skip
        """(ranges, board cards) of one solved board of the line's matchup (of `texture` when
        given), drawn by weight: the flop players' hands come from the library ranges and avoid
        the board, so the drill lands on a graded combo with that board still in the deck."""
        boards = sorted(self._boards(cfg, line, texture).items())
        if not boards:
            return None, ()
        board, entry = drill_rng.choices(
            boards, [max(1, int(e.get("weight", 1))) for _, e in boards]
        )[0]
        doc = self.cache.get(entry["key"])
        if doc is None:
            return None, ()
        return drills.flop_ranges(doc), tuple(fl.flop_cards(board))

    def _pick_flop(self, h: Any, matchup: flib.Matchup, rng: random.Random) -> flib.Pick | None:
        """The hand's library flop: of the live drill's texture when it has one (falling back to
        any solved board, logged), else any solved board."""
        tex = h.drill.texture if h.drill is not None else None
        if tex is not None:
            pick = flib.pick_board(
                TextureIndex(self.flops, tex), self.cache, matchup.mid, h.deck, rng
            )
            if pick is not None:
                return pick
            log.warning("no dealable %s flop for %s: dealing any solved flop", tex, matchup.label)
        return flib.pick_board(self.flops, self.cache, matchup.mid, h.deck, rng)


def heads_up_line(line: Line) -> bool:
    """A drill line the trainer's bots could play (user 2026-10-16): no limp, and no call or
    raise once two other players are in (no cold call into 3+, no squeeze)."""
    vol: set[str] = set()
    raised = False
    for step in line.steps:
        if step.kind in ("call", "raise", "allin"):
            if step.kind == "call" and not raised:
                return False
            if len(vol - {step.position}) >= 2:
                return False
            vol.add(step.position)
            raised = raised or step.kind != "call"
    return True
