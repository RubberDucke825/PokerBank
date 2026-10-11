"""Review API: rows of decision_ev for a hand and a player's best / mixed / blunder summary.
Reads the SQLite DB directly (no cache; `pn ev run` fills the table, source "gto" only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from pn.ev import report
from pn.profile import source as psource
from pn.solve import SOURCES  # reads only: historic rows of other sources stay viewable

router = APIRouter()


def _with_conn(request: Request, src: str, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    if src not in SOURCES:
        raise HTTPException(400, f"source must be one of {SOURCES}")
    try:
        conn = psource.open_db(request.app.state.store.db_path)
    except FileNotFoundError as e:
        raise HTTPException(503, f"{e} (run `pn fetch` or pass --db)") from e
    try:
        return fn(conn)
    except psource.PlayerNotFound as e:
        raise HTTPException(404, str(e)) from e
    except sqlite3.DatabaseError as e:
        raise HTTPException(500, f"database error: {e}") from e
    finally:
        conn.close()


@router.get("/api/hands/{hand_id}/ev")
def hand_ev(hand_id: str, request: Request, source: str = "gto") -> dict:
    rows = _with_conn(request, source, lambda c: report.hand_rows(c, hand_id, source))
    return {"hand_id": hand_id, "source": source, "rows": rows}


@router.get("/api/review/{hero}/ev")
def review_ev(hero: str, request: Request, source: str = "gto",
              include_unmodeled: bool = False) -> dict:  # fmt: skip
    def fn(conn: sqlite3.Connection) -> dict:
        ids = report.player_ids(conn, hero)
        return {"player_ids": ids, **report.summary(conn, ids, source, include_unmodeled)}

    return _with_conn(request, source, fn)
