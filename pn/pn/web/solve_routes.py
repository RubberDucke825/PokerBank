"""Strategy-tab solve API (contracts/trainer.md "Root solves and subgames", Plan 7 study UI):
new spots, jobs with live progress and cancel, subgames, the saved-solves library, delete,
ranges and range equity at a node, the preflop line catalog and the solve presets."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from pn import ranges
from pn.solve import SolveCache, SolveError
from pn.solve.library import library_path
from pn.solve.request import parse_board, solve_preset
from pn.solve.subgame import SubgameError, path_to
from pn.web import study
from pn.web.equity import range_equity
from pn.web.jobs import JobFinished, JobNotFound, Jobs
from pn.web.solves import (
    NEW_SPOT_PRESET,
    Registry,
    SeatSpec,
    SolveNotFound,
    board_of,
    check_cards,
    prepare_spot,
    solve_prepared,
    solve_subgame,
)
from pn.web.train_routes import StoreProfiles

SOLVE_SOURCES = ("gto",)  # Plan 7: no profile-based, custom or population (locked) solves
router = APIRouter()


class SeatBody(BaseModel):
    position: str
    player_id: str | None = None
    range: str | None = None


class NewSpotBody(BaseModel):
    table: int = 6
    depth: int = 100
    source: str = "gto"
    street: str = "preflop"  # preflop | flop | turn | river (board of 0 / 3 / 4 / 5 cards)
    board: list[str] | str = ""
    line: str | None = None
    hero: str | None = None
    seats: list[SeatBody] = []
    preset: str = NEW_SPOT_PRESET
    ranges: dict[str, str] | None = None  # "oop" / "ip" (or positions) -> range text
    sizes: dict | None = None  # custom [sizes]: flop_bets, turn_bets, river_bets, raise, ...
    pot: int | None = None  # cents at the start of the street (default: from `line`)
    stack: int | None = None  # effective cents behind (default: depth minus the line's bets)


class SubgameBody(BaseModel):
    node_id: str
    cards: list[str] | str
    preset: str = NEW_SPOT_PRESET


def _solve_ctx(request: Request) -> tuple[Registry, SolveCache, StoreProfiles, Jobs]:
    state = request.app.state
    if getattr(state, "solve_cache", None) is None:
        state.solve_cache = SolveCache()
    if getattr(state, "solve_jobs", None) is None:
        state.solve_jobs = Jobs()
    state.registry.use_cache(state.solve_cache)
    return state.registry, state.solve_cache, StoreProfiles(state.store), state.solve_jobs


def _cards(v: list[str] | str) -> str:
    return "".join(v) if isinstance(v, list) else v


def _check_preset(name: str) -> None:
    try:
        solve_preset(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _refused(err: SolveError) -> str:
    raise err


def _raw(obj: object) -> Response:
    return Response(json.dumps(obj, separators=(",", ":")), media_type="application/json")


@router.post("/api/solves/new", status_code=202)
def solves_new(body: NewSpotBody, request: Request) -> dict:
    """Start a root solve; the spot is validated first (422), a library preflop root is done at
    once."""
    reg, cache, profiles, jobs = _solve_ctx(request)
    _check_preset(body.preset)
    if body.source not in SOLVE_SOURCES or any(s.player_id for s in body.seats):
        raise HTTPException(400, f"source must be one of {SOLVE_SOURCES} and seats carry no "
                                 "player_id (Plan 7: no opponent-specific solves)")  # fmt: skip
    seats = tuple(SeatSpec(s.position, s.player_id or None, (s.range or "").strip() or None)
                  for s in body.seats)  # fmt: skip
    custom = any(s.player_id or s.range for s in seats)
    if body.street == "preflop" and not custom and library_path(body.table, body.depth, body.source).exists():
        body = body.model_copy(update={"seats": [], "preset": NEW_SPOT_PRESET})
        seats = ()
    try:
        prepared = prepare_spot(reg, profiles, table=body.table, depth=body.depth,
                                source=body.source, street=body.street, board=_cards(body.board),
                                line=body.line or None, hero=body.hero or None, seats=seats,
                                preset=body.preset, ranges=body.ranges, sizes=body.sizes,
                                pot=body.pot, stack=body.stack)  # fmt: skip
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except SolveError as e:  # refused spot (preflop_unsolvable, multiway, locks): a failed job
        err = e
        return {"job_id": jobs.submit(lambda hooks: _refused(err))}
    if prepared.name is not None:  # library root: loading is quick, no job thread needed
        return {"job_id": jobs.done(prepared.name)}
    return {"job_id": jobs.submit(lambda hooks: solve_prepared(reg, cache, prepared, hooks))}


@router.get("/api/solves/jobs/{job_id}")
def solves_job(job_id: str, request: Request) -> dict:
    try:
        return _solve_ctx(request)[3].get(job_id)
    except JobNotFound as e:
        raise HTTPException(404, str(e)) from e


@router.post("/api/solves/jobs/{job_id}/cancel")
def solves_job_cancel(job_id: str, request: Request) -> dict:
    try:
        return _solve_ctx(request)[3].cancel(job_id)
    except JobNotFound as e:
        raise HTTPException(404, str(e)) from e
    except JobFinished as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/solves/{name}/subgame", status_code=202)
def solves_subgame(name: str, body: SubgameBody, request: Request) -> dict:
    reg, cache, profiles, jobs = _solve_ctx(request)
    _check_preset(body.preset)
    try:
        reg.get(name)
        cards = parse_board(_cards(body.cards))
        check_cards(cards)
    except SolveNotFound as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"job_id": jobs.submit(lambda hooks: solve_subgame(
        reg, cache, profiles, name, body.node_id, cards, body.preset, hooks))}  # fmt: skip


# ------------------------------------------------------------------ study UI data
@router.get("/api/solves/library")
def solves_library(request: Request) -> list[dict]:
    return study.library_rows(_solve_ctx(request)[0])


@router.get("/api/solves/presets")
def solves_presets() -> dict:
    return study.presets()


@router.get("/api/solves/lines")
def solves_lines(table: int = 6) -> list[dict]:
    try:
        return study.line_catalog(table)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/solves/line")
def solves_line(line: str, positions: str, table: int = 6, depth: int = 100) -> dict:
    """Pot, stacks and OOP / IP chart ranges for a heads-up preflop line."""
    try:
        return study.line_ranges(table, depth, line, [p for p in positions.split(",") if p])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/api/solves/{name}/ranges")
def solves_ranges(name: str, request: Request, path: str = "", node: str = "") -> Response:
    """Both players' reach at the node at `path`, or at node id `node` (trainer grid_ref)."""
    reg = _solve_ctx(request)[0]
    try:
        doc = reg.get(name)
        return _raw(study.node_ranges(doc, _tokens(doc, path, node)))
    except SolveNotFound as e:
        raise HTTPException(404, str(e)) from e


@router.get("/api/solves/{name}/line_breakdown")
def solves_line_breakdown(name: str, request: Request, node: str = "", path: str = "") -> Response:
    """Per action node on the line to node id `node` (or `path`): the actor's per-class range
    and its split over the actions (study.line_breakdown), with the registry's parent street."""
    reg = _solve_ctx(request)[0]
    try:
        doc = reg.get(name)
        parent = reg.info(name).get("parent")
        return _raw(study.line_breakdown(doc, _tokens(doc, path, node), parent))
    except SolveNotFound as e:
        raise HTTPException(404, str(e)) from e


def _tokens(doc: dict, path: str, node: str) -> list[str]:
    """Path tokens from `path`, or walked up from node id `node` (SolveNotFound if unknown)."""
    if not node:
        return [t for t in path.split(",") if t]
    try:
        return [tok for _, tok in path_to(doc, node)]
    except SubgameError as e:
        raise SolveNotFound(str(e)) from e


@router.get("/api/solves/{name}/equity")
def solves_equity(name: str, request: Request, path: str = "", node: str = "") -> Response:
    """Hand-vs-range equity of both players at the node (contracts/strategy_output.md); reach
    comes from the /ranges walk (`node` as there), the board is the solve's plus the chance cards on the path."""
    reg = _solve_ctx(request)[0]
    try:
        doc = reg.get(name)
        tokens = _tokens(doc, path, node)
        rng = study.node_ranges(doc, tokens)
    except SolveNotFound as e:
        raise HTTPException(404, str(e)) from e
    board = [*board_of(doc), *(t for t in tokens if _is_card(t))]
    try:
        return _raw(range_equity(board, [p["reach"] for p in rng["players"]],
                                 [p["position"] for p in rng["players"]]))  # fmt: skip
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _is_card(tok: str) -> bool:
    """Chance tokens are card strings; action tokens (check, bet33, allin, ...) never are."""
    return len(tok) == 2 and tok[0] in ranges.RANKS and tok[1] in ranges.SUITS


@router.delete("/api/solves/{name}")
def solves_delete(name: str, request: Request) -> dict:
    reg = _solve_ctx(request)[0]
    try:
        return reg.delete(name)
    except SolveNotFound as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
