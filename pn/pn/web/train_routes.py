"""Trainer API (contracts/trainer.md): sessions live in memory on `app.state.trainer`, created
on first use with DB profiles (tests may set their own Trainer before the first request)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pn.ingest import db as ingest_db
from pn.solve import SpotRequest
from pn.solve.cache import cache_dir
from pn.train import Config, SessionNotFound, Trainer, TrainError
from pn.train import stats as train_stats
from pn.train.leaks import Leaks
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

        def register(doc: dict, req: SpotRequest) -> str:
            name = f"train-{req.street}-{''.join(req.board)}-{uuid.uuid4().hex[:6]}"
            return registry.register(name, with_board(doc, req.board), "trainer street solve",
                                     req=req)  # fmt: skip

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


class Act(BaseModel):
    token: str


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
                                      drill_share=body.drill_share)))  # fmt: skip


@router.get("/api/train/stats")
def train_stats_route(request: Request, window: int = 1000, source: str = "") -> dict:
    """Rolling trainer performance over the newest `window` hands (empty without a DB)."""
    db = request.app.state.store.db_path
    try:
        with ingest_db.opened(db if db.exists() else ":memory:") as conn:
            return train_stats.rolling(conn, window, source or None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/train/leaks")
def train_leaks_route(request: Request, window: int = 1000, table: int = 6,
                      depth: int = 100) -> list[dict]:  # fmt: skip
    """Spots by expected blunders per 100 hands, with drill weights (contracts/trainer.md)."""
    t = trainer(request)
    leaks = t.leaks if t.leaks is not None else Leaks(None, cache_root=t.cache.root)
    try:
        Config(table=table, depth=depth, window=window)
    except TrainError as e:
        raise HTTPException(400, str(e)) from e
    db = request.app.state.store.db_path
    with ingest_db.opened(db if db.exists() else ":memory:") as conn:
        return leaks.rows(conn, table, depth, window)


@router.get("/api/train/{sid}")
def train_state(sid: str, request: Request) -> dict:
    return _call(trainer(request).state, sid)


@router.post("/api/train/{sid}/act")
def train_act(sid: str, body: Act, request: Request) -> dict:
    return _call(trainer(request).act, sid, body.token)


@router.post("/api/train/{sid}/next")
def train_next(sid: str, request: Request) -> dict:
    return _call(trainer(request).next, sid)
