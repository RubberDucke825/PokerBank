"""Read normalized hands and player identity from the ingest SQLite DB (or plain lists)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

DEFAULT_DB = "data/pn.sqlite"
_INGEST_SCHEMA = Path(__file__).resolve().parent.parent / "ingest" / "schema.sql"
_FALLBACK_SCHEMA = Path(__file__).resolve().parent / "_schema_fallback.sql"


class PlayerNotFound(LookupError):
    """No (or ambiguous) player for a query."""


def default_db() -> str:
    return os.environ.get("PN_DB", DEFAULT_DB)


def schema_sql() -> str:
    path = _INGEST_SCHEMA if _INGEST_SCHEMA.exists() else _FALLBACK_SCHEMA
    return path.read_text()


def open_db(path: str | Path, create: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if not create and str(path) != ":memory:" and not p.exists():
        raise FileNotFoundError(f"database not found: {p}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if create:
        conn.executescript(schema_sql())
    return conn


def load_hands(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT json FROM hands ORDER BY started_at, hand_no").fetchall()
    return [json.loads(r["json"]) for r in rows]


def alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in conn.execute("SELECT player_id, canonical_id FROM aliases"):
        out[r["player_id"]] = r["canonical_id"]
    for r in conn.execute("SELECT player_id, canonical_id FROM players"):
        out[r["player_id"]] = r["canonical_id"] or r["player_id"]
    return out


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def display_names(conn: sqlite3.Connection) -> dict[str, str]:
    """canonical_id -> display name (display_name, else latest nickname of any member)."""
    has_nick = "nickname" in _cols(conn, "players")
    nick = "nickname" if has_nick else "NULL"
    rows = conn.execute(
        f"SELECT player_id, canonical_id, display_name, {nick} AS nickname, last_seen "
        "FROM players ORDER BY last_seen"
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        cid = r["canonical_id"] or r["player_id"]
        if r["display_name"]:
            out[cid] = r["display_name"]
        elif r["nickname"] and cid not in out:
            out[cid] = r["nickname"]
    return out


def names_from_hands(hands: Iterable[dict], amap: dict[str, str] | None = None) -> dict[str, str]:
    """canonical_id -> latest in-hand nickname."""
    amap = amap or {}
    out: dict[str, str] = {}
    for h in hands:
        for p in h.get("players", []):
            if p.get("name"):
                out[amap.get(p["player_id"], p["player_id"])] = p["name"]
    return out


def resolve_target(query: str, amap: dict[str, str], names: dict[str, str]) -> str:
    """Canonical id for a player_id, canonical_id or (case-insensitive) display name."""
    if query in amap:
        return amap[query]
    if query in names:
        return query
    hits = {cid for cid, n in names.items() if n.lower() == query.lower()}
    if len(hits) == 1:
        return hits.pop()
    if len(hits) > 1:
        raise PlayerNotFound(f"name {query!r} is ambiguous: {sorted(hits)}")
    raise PlayerNotFound(f"no player matches {query!r}")
