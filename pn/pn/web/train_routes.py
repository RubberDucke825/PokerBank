"""Trainer API (contracts/trainer.md): sessions live in memory on `app.state.trainer`, created
on first use with DB profiles (tests may set their own Trainer before the first request)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from pn.ingest import db as ingest_db
from pn.solve import SpotRequest
from pn.solve.cache import cache_dir
from pn.train import Config, SessionNotFound, Trainer, TrainError, performance
from pn.train import importance as imp
from pn.train import stats as train_stats
from pn.train.leaks import Leaks
from pn.train.preflop_line import preflop_line
from pn.web.solves import with_board
from pn.web.store import NotFound, Store

router = APIRouter()


class StoreProfiles:
    """ProfileSource over the web Store (profiles built from the DB, cached per DB version)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, player_id: str, default: Any = None) -> Mapping[str, Any] | None:
        try:
            return self.store.profile(player_id)
        except (NotFound, FileNotFoundError):
            return default


def _names(store: Store) -> dict[str, str]:
    try:
        return store.names()
    except FileNotFoundError:
        return {}


def trainer(request: Request) -> Trainer:
    state = request.app.state
    if getattr(state, "trainer", None) is None:
        store, registry = state.store, state.registry

        def register(doc: dict, req: SpotRequest, parent: dict | None) -> str:
            name = f"train-{req.street}-{''.join(req.board)}-{uuid.uuid4().hex[:6]}"
            return registry.register(name, with_board(doc, req.board), "trainer street solve",
                                     req=req, parent=parent)  # fmt: skip

        leaks = Leaks(store.db_path, cache_root=cache_dir())
        threading.Thread(target=leaks.warm, name="leaks-warm", daemon=True).start()
        state.trainer = Trainer(profiles=StoreProfiles(store), names=lambda: _names(store),
                                register=register, db=store.db_path, leaks=leaks)  # fmt: skip
    return state.trainer


class NewSession(BaseModel):
    table: int = 6
    depth: int = 100
    hero_seat: str | None = None
    opponents: list[str | None] = []
    source: str = "gto"
    seed: int | None = None
    rng_enabled: bool = True
    window: int = 1000
    drill_share: float = 0.8
    drill_spot: str | None = None
    drill_texture: str | None = None
    drill_formation: str | None = None
    p_form: int = imp.P_FORM
    p_tex: int = imp.P_TEX
    pot_mode: str = "start"


class Act(BaseModel):
    token: str


class Pin(BaseModel):
    spot: str | None = None
    texture: str | None = None  # omitted = null: each call sets the whole pin
    formation: str | None = None


class Params(BaseModel):
    p_form: int | None = None
    p_tex: int | None = None
    pot_mode: str | None = None


def _call(fn, *args):
    try:
        return fn(*args)
    except SessionNotFound as e:
        raise HTTPException(404, str(e)) from e
    except TrainError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/train/players")
def train_players(request: Request) -> list[dict]:
    """DB players for the opponent picker (empty without a DB: GTO bots still work)."""
    try:
        rows = request.app.state.store.players()
    except FileNotFoundError:
        return []
    return [{"player_id": r["canonical_id"], "name": r["name"], "hands": r["hands"],
             "archetype": r["archetype"]} for r in rows]  # fmt: skip


@router.post("/api/train/new")
def train_new(body: NewSession, request: Request) -> dict:
    t = trainer(request)
    return _call(lambda: t.new(Config(table=body.table, depth=body.depth, hero_seat=body.hero_seat,
                                      opponents=tuple(p or None for p in body.opponents),
                                      source=body.source, seed=body.seed,
                                      rng_enabled=body.rng_enabled, window=body.window,
                                      drill_share=body.drill_share,
                                      drill_spot=body.drill_spot, drill_texture=body.drill_texture,
                                      drill_formation=body.drill_formation,
                                      p_form=body.p_form, p_tex=body.p_tex,
                                      pot_mode=body.pot_mode)))  # fmt: skip


@router.get("/api/train/stats")
def train_stats_route(request: Request, window: int = 1000, source: str = "") -> dict:
    """Rolling trainer performance over the newest `window` hands (empty without a DB)."""
    db = request.app.state.store.db_path
    try:
        with ingest_db.opened(db if db.exists() else ":memory:") as conn:
            return train_stats.rolling(conn, window, source or None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _importance(request: Request, window: int, table: int, depth: int, p_form: int, p_tex: int,
                pot: str) -> dict:  # fmt: skip
    """Leaks.importance for the query (400 on bad parameters); while the SolveCache warms up
    (web mode) it uses the fallbacks instead of waiting."""
    t = trainer(request)
    leaks = t.leaks if t.leaks is not None else Leaks(None, cache_root=t.cache.root)
    try:
        Config(table=table, depth=depth, window=window, p_form=p_form, p_tex=p_tex, pot_mode=pot)
    except TrainError as e:
        raise HTTPException(400, str(e)) from e
    db = request.app.state.store.db_path
    with ingest_db.opened(db if db.exists() else ":memory:") as conn:
        return leaks.importance(conn, table, depth, window, wait=not t.background, p_form=p_form,
                                p_tex=p_tex, pot=pot)  # fmt: skip


@router.get("/api/train/leaks")
def train_leaks_route(request: Request, window: int = 1000, table: int = 6, depth: int = 100,
                      p_form: int = imp.P_FORM, p_tex: int = imp.P_TEX,
                      pot: str = "start") -> list[dict]:  # fmt: skip
    """Spots by importance priority, with drill weights (contracts/trainer.md "Plan 14 additions")."""
    return _importance(request, window, table, depth, p_form, p_tex, pot)["spots"]


@router.get("/api/train/importance")
def train_importance_route(request: Request, window: int = 1000, table: int = 6, depth: int = 100,
                           p_form: int = imp.P_FORM, p_tex: int = imp.P_TEX,
                           pot: str = "start") -> dict:  # fmt: skip
    """Formation and texture importance tables (contracts/trainer.md "Plan 14 additions")."""
    out = _importance(request, window, table, depth, p_form, p_tex, pot)
    return {"formations": out["formations"], "textures": out["textures"], "params": out["params"]}


def _opt_int(name: str, raw: str) -> int | None:
    try:
        return int(raw) if raw.strip() else None
    except ValueError as e:
        raise HTTPException(400, f"{name} must be an integer") from e


@router.get("/api/train/performance")
def train_performance_route(request: Request, window: str = "", table: str = "", depth: str = "",
                            since: str = "") -> dict:  # fmt: skip
    """GTO closeness per spot (contracts/trainer.md "Plan 15 additions"); empty params (or window=all) = no filter."""
    return _perf_call(request, performance.performance, window, table, depth, since)


@router.get("/api/train/performance/recent")
def train_performance_recent_route(request: Request, position: str = "", family: str = "",
                                   window: str = "", table: str = "", depth: str = "",
                                   since: str = "") -> dict:  # fmt: skip
    """One heatmap cell's newest graded decisions (same filters as /api/train/performance)."""
    if not position or not family:
        raise HTTPException(400, "position and family are required")
    return _perf_call(request, lambda conn, **kw: performance.cell_recent(conn, position, family, **kw),
                      window, table, depth, since)  # fmt: skip


@router.get("/api/train/dealable")
def train_dealable_route(request: Request, table: int = 6, depth: int = 100,
                         spot: Annotated[list[str] | None, Query()] = None) -> dict:  # fmt: skip
    """Which spot ids a drill pin can deal at this table / depth (Plan 16: hides "Drill this
    spot" on /train/performance when it would 400)."""
    return {"table": table, "depth": depth,
            "dealable": _call(lambda: trainer(request).dealable(table, depth, spot or []))}  # fmt: skip


def _perf_call(request: Request, fn, window: str, table: str, depth: str, since: str) -> dict:
    window = "" if window == "all" else window
    args = {k: _opt_int(k, v) for k, v in (("window", window), ("table", table), ("depth", depth))}
    db = request.app.state.store.db_path
    try:
        with ingest_db.opened(db if db.exists() else ":memory:") as conn:
            return fn(conn, since=since or None, **args)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/train/{sid}")
def train_state(sid: str, request: Request) -> dict:
    return _call(trainer(request).state, sid)


@router.get("/api/train/{sid}/preflop_line")
def train_preflop_line(sid: str, request: Request) -> dict:
    """Preflop range split at each decision of the current hand's two flop players
    (pn.train.preflop_line); {"steps": []} unless the hand reached a heads-up flop."""
    t = trainer(request)

    def build() -> dict:
        s = t.get(sid)
        with s.lock:
            h, cfg = s.hand, s.cfg
        hero = next(i for i, p in enumerate(h.players) if p.hero)
        return preflop_line(h.state, h.tokens, hero, cfg.table_kind, cfg.depth, t.freqs)

    return _call(build)


@router.post("/api/train/{sid}/act")
def train_act(sid: str, body: Act, request: Request) -> dict:
    return _call(trainer(request).act, sid, body.token)


@router.post("/api/train/{sid}/next")
def train_next(sid: str, request: Request) -> dict:
    return _call(trainer(request).next, sid)


@router.post("/api/train/{sid}/drill")
def train_drill(sid: str, body: Pin, request: Request) -> dict:
    """Pin (spot or formation, and / or flop texture) or unpin (nulls) the drill dealt from the
    next hand on."""
    return _call(trainer(request).drill, sid, body.spot, body.texture, body.formation)


@router.post("/api/train/{sid}/params")
def train_params(sid: str, body: Params, request: Request) -> dict:
    """Importance parameters (drill weights) from the next deal on; omitted fields stay."""
    return _call(trainer(request).params, sid, body.p_form, body.p_tex, body.pot_mode)


@router.post("/api/train/{sid}/retry")
def train_retry(sid: str, request: Request) -> dict:
    """Rewind to just before hero's latest decision this hand (a replay: not counted)."""
    return _call(trainer(request).retry, sid)
