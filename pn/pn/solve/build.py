"""SpotRequest -> validated tree_config dict (contracts/tree_config.md)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pn import ranges
from pn.adapter import spot_ranges as sr
from pn.adapter.state import Line, order_key
from pn.adapter.validate import FOLD_LOCK, validate_config
from pn.solve.request import Seat, SpotRequest, preflop_knobs
from pn.solve.sources import ProfileSource, profile_hands, seat_range

DEFAULT_SIZES: dict[str, Any] = {
    "flop_bets": "auto", "turn_bets": [75], "river_bets": [150], "raise": [100],
    "max_bets_per_street": 3, "allin_threshold": 0.67, "donk": True,
}  # fmt: skip


class SpotError(ValueError):
    """The request cannot be turned into a spot."""


@dataclass(frozen=True)
class SeatResult:
    seat: Seat
    range_text: str
    range_origin: str
    lock_origin: str | None  # folded | None (free)
    profile_hands: int
    tip: str | None


@dataclass(frozen=True)
class BuiltSpot:
    config: dict[str, Any]
    seats: tuple[SeatResult, ...]  # same order as config["players"]


def _first_tip(profile: Mapping | None) -> str | None:
    tips = (profile or {}).get("tips") or []
    return tips[0] if tips else None


def _depth_bb(req: SpotRequest) -> float:
    behind = sorted(s.stack for s in req.seats)
    return max(1.0, behind[-2] / req.bb) if req.bb and len(behind) > 1 else 100.0


def _game(req: SpotRequest, positions: list[str]) -> dict[str, Any]:
    g: dict[str, Any] = {
        "street": req.street, "board": list(req.board), "pot": req.pot, "sb": req.sb,
        "bb": req.bb, "ante": req.ante, "mode": req.mode,
    }  # fmt: skip
    if "STR" in positions:
        g["straddle"] = req.straddle
    if req.mode == "depth_limited":
        g["leaf_model"] = "equity_realization"
    b = req.budget
    g.update(max_iterations=b.max_iterations, target_exploitability=b.target_exploitability,
             time_limit_sec=b.time_limit_sec, threads=b.threads)  # fmt: skip
    if req.street != "preflop" and req.last_aggressor in positions:
        g["last_aggressor"] = req.last_aggressor
    return g


def build(req: SpotRequest, profiles: ProfileSource | None = None) -> BuiltSpot:
    """Chart ranges per seat (pn.solve.sources); explicit Seat.range and `req.overrides`
    replace a seat's range; dead cards removed; folded seats locked to fold (the only lock);
    preflop spots with 4+ players get pn.solve.request.preflop_knobs defaults under any
    explicit [sizes.preflop]; validated. Profiles only supply hand counts and tips."""
    seats = sorted(req.seats, key=lambda s: order_key(s.position, req.street))
    if not 2 <= len(seats) <= 8:
        raise SpotError(f"need 2..8 seats, got {len(seats)}")
    depth_bb = _depth_bb(req)
    three_bettor = next((s.position for s in seats if s.line == "3bet"), None)
    limpers = [s.position for s in seats if s.line == "limp"]
    limper = limpers[0] if len(limpers) == 1 else None
    players: list[dict[str, Any]] = []
    results: list[SeatResult] = []
    for seat in seats:
        profile = profiles.get(seat.player_id) if profiles and seat.player_id and not seat.hero else None
        if seat.range is not None:
            rng, origin = ranges.parse(seat.range), "explicit"
        else:
            line = Line(seat.line, seat.vs) if seat.line else None
            rng, origin = seat_range(position=seat.position, line=line, table=req.table,
                                     depth_bb=depth_bb, three_bettor=three_bettor,
                                     limper=limper)  # fmt: skip
        if seat.key in req.overrides:
            rng, origin = ranges.parse(req.overrides[seat.key]), "override"
        dead = [*req.board, *([] if seat.hero else req.hero_cards)]
        rng = sr.remove_dead(rng, dead)
        if not rng:
            raise SpotError(f"empty range for {seat.key} after dead-card removal")
        lock, lock_origin = (dict(FOLD_LOCK), "folded") if seat.folded else (None, None)
        entry: dict[str, Any] = {
            "name": seat.name or seat.player_id or seat.position.lower(),
            "position": seat.position, "stack": max(0, seat.stack), "range": ranges.to_text(rng),
        }  # fmt: skip
        if seat.hero:
            entry["hero"] = True
        if lock:
            entry["lock"] = lock
        players.append(entry)
        results.append(SeatResult(seat, entry["range"], origin, lock_origin,
                                  profile_hands(profile), _first_tip(profile)))  # fmt: skip
    sizes = {**DEFAULT_SIZES, **(req.sizes or {})}
    knobs = preflop_knobs(len(seats)) if req.street == "preflop" else {}
    if knobs:  # explicit [sizes.preflop] entries win over the multiway defaults
        sizes["preflop"] = {**knobs, **(sizes.get("preflop") or {})}
    cfg = {"game": _game(req, [s.position for s in seats]), "players": players, "sizes": sizes}
    validate_config(cfg)
    return BuiltSpot(cfg, tuple(results))


def build_spot(req: SpotRequest, profiles: ProfileSource | None = None) -> dict[str, Any]:
    """Validated tree_config dict for `req` (contracts/solve_service.md)."""
    return build(req, profiles).config
