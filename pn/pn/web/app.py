"""FastAPI app: JSON API under /api/* and the single-page UI for every other page route."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from pn.profile.paths import repo_root
from pn.profile.source import default_db
from pn.web import preflop
from pn.web.ev_routes import router as ev_router
from pn.web.solve_routes import router as solve_router
from pn.web.solves import Registry, SolveError, SolveNotFound
from pn.web.store import NotFound, Store
from pn.web.study_preflop import router as study_preflop_router
from pn.web.train_routes import router as train_router

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
PAGES = ("/", "/players", "/players/{pid}", "/review", "/review/{hero}", "/hands",
         "/hands/{hand_id}", "/charts", "/strategy", "/strategy/new", "/strategy/{name}",
         "/train", "/train/performance")  # fmt: skip


def _404(e: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


def create_app(db_path: str | Path | None = None, solves_dir: str | Path | None = None) -> FastAPI:
    store = Store(db_path or default_db())
    registry = Registry(Path(solves_dir) if solves_dir else repo_root() / "data" / "solves")
    app = FastAPI(title="pn study UI", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store, app.state.registry = store, registry
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.include_router(solve_router)  # before the /api/solves/{name} routes below
    app.include_router(ev_router)
    app.include_router(train_router)
    app.include_router(study_preflop_router)

    def db_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotFound as e:
            raise _404(e) from e
        except FileNotFoundError as e:
            raise HTTPException(503, f"{e} (run `pn fetch` or pass --db)") from e
        except sqlite3.DatabaseError as e:
            raise HTTPException(500, f"database error: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ------------------------------------------------------------ players / profiles
    @app.get("/api/players")
    def players() -> list[dict]:
        return db_call(store.players)

    @app.get("/api/players/{pid}/profile")
    def profile(pid: str) -> dict:
        return db_call(store.profile, pid)

    @app.get("/api/review/{hero}")
    def review(hero: str) -> dict:
        return db_call(store.review, hero)

    # ------------------------------------------------------------ hands
    @app.get("/api/hands")
    def hands(
        player: str | None = None,
        street: str | None = None,
        showdown: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        return db_call(store.hands, player or None, street or None, showdown, limit, offset)

    @app.get("/api/hands/{hand_id}")
    def hand(hand_id: str) -> dict:
        return db_call(store.hand, hand_id)

    # ------------------------------------------------------------ ranges / charts
    @app.get("/api/range/grid")
    def range_grid(text: str = "") -> dict:
        try:
            return preflop.range_grid(text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/charts")
    def chart_files() -> dict:
        return {"files": preflop.chart_files()}

    @app.get("/api/charts/grid")
    def chart_grid(file: str, key: str) -> dict:
        try:
            return preflop.chart(file, key)
        except preflop.ChartNotFound as e:
            raise _404(e) from e
        except ValueError as e:
            raise HTTPException(400, f"chart does not parse: {e}") from e

    @app.get("/api/charts/pc")
    def pc_spot(table: str, situation: str, position: str, other: str | None = None) -> dict:
        try:
            return preflop.pc_spot(table, situation, position, other)
        except preflop.ChartNotFound as e:
            raise _404(e) from e

    @app.get("/api/gtow/spots")
    def gtow_spots() -> list[dict]:
        return preflop.gtow_spots()

    @app.get("/api/gtow/spot")
    def gtow_spot(key: str) -> dict:
        try:
            return preflop.gtow_spot(key)
        except preflop.ChartNotFound as e:
            raise _404(e) from e

    # ------------------------------------------------------------ solves
    def solve_call(fn, *args):
        try:
            return fn(*args)
        except SolveNotFound as e:
            raise _404(e) from e
        except SolveError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/solves")
    def solves() -> list[dict]:
        return registry.list()

    @app.post("/api/solves/upload")
    async def upload(file: UploadFile) -> dict:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        return {"name": solve_call(registry.add_upload, file.filename or "upload", raw)}

    @app.get("/api/solves/{name}")
    def solve_meta(name: str) -> dict:
        return solve_call(registry.meta, name)

    def raw_json(obj: dict) -> Response:
        """Serialize directly: FastAPI's jsonable_encoder would deep-copy every per-combo
        array of the node (1326 x actions floats per array)."""
        return Response(json.dumps(obj, separators=(",", ":")), media_type="application/json")

    @app.get("/api/solves/{name}/nodes/{node_id}")
    def solve_node(name: str, node_id: str) -> Response:
        return raw_json(solve_call(registry.node, name, node_id))

    @app.get("/api/solves/{name}/walk")
    def solve_walk(name: str, path: str = "") -> Response:
        tokens = [t for t in path.split(",") if t]
        return raw_json(solve_call(registry.walk, name, tokens))

    # ------------------------------------------------------------ pages (SPA shell)
    index = STATIC / "index.html"

    def page() -> FileResponse:
        return FileResponse(index, media_type="text/html")

    for route in PAGES:
        app.add_api_route(route, page, methods=["GET"], include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
