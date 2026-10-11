"""Fill the decision_ev table: enumerate gradable decisions per hand, grade the missing ones
(idempotent per (hand_id, seq, source) unless force), commit per hand. Plan 7: frequency grades
(pn.ev.grade), source "gto" only."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pn.ev.decisions import enumerate_decisions
from pn.ev.grade import REVIEW_SOURCES, Grade, Grader
from pn.ev.report import player_ids
from pn.ingest import db as ingest_db
from pn.solve import SolveCache

COLUMNS = ("hand_id", "seq", "source", "street", "player_id", "chosen", "best", "ev_chosen",
           "ev_best", "loss", "gto_freq_chosen", "backend", "solve_key", "exploitability",
           "computed_at", "note", "spot", "grade", "freq_chosen", "best_freq")  # fmt: skip
ADDED_COLUMNS = {"exploitability": "REAL", "grade": "TEXT", "freq_chosen": "REAL",
                 "best_freq": "REAL"}  # columns added after decision_ev first shipped (Plan 7: grade)


def _hands(conn: sqlite3.Connection, ids: list[str] | None, limit: int | None,
           since: str | None) -> list[dict]:  # fmt: skip
    """Hands with any known hole cards (of `ids` when given), newest first."""
    where = [("(hp.cards IS NOT NULL OR "
              "COALESCE(json_array_length(json_extract(h.json,'$.showdowns')), 0) > 0)")]
    args: list[Any] = []
    if ids:
        where.append(f"hp.player_id IN ({','.join('?' * len(ids))})")
        args += ids
    if since:
        where.append("h.started_at >= ?")
        args.append(since)
    sql = (f"SELECT DISTINCT h.hand_id, h.json, h.started_at, h.hand_no FROM hands h "
           f"JOIN hand_players hp ON hp.hand_id = h.hand_id WHERE {' AND '.join(where)} "
           "ORDER BY h.started_at DESC, h.hand_no DESC")  # fmt: skip
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [json.loads(r["json"]) for r in conn.execute(sql, args)]


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(decision_ev)")}
    for col, kind in ADDED_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE decision_ev ADD COLUMN {col} {kind}")
    conn.commit()


def _write(conn: sqlite3.Connection, grades: list[Grade]) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows = [{**asdict(g), "computed_at": now} for g in grades]
    conn.executemany(
        f"INSERT OR REPLACE INTO decision_ev ({','.join(COLUMNS)}) "
        f"VALUES ({','.join(':' + c for c in COLUMNS)})", rows)  # fmt: skip
    conn.commit()


def run(db: str | Path, player: str | None = None, source: str = "gto",
        budget: Mapping[str, Any] | None = None, limit: int | None = None,
        since: str | None = None, *, force: bool = False, cache: SolveCache | None = None,
        freqs: Mapping | None = None, profiles: Any = None,
        emit: Callable[[str], None] = print) -> dict[str, Any]:  # fmt: skip
    """Grade decisions of `player` (all players with known cards when None) in the newest
    `limit` hands. Returns counts: hands, graded, null, skipped, notes."""
    if source not in REVIEW_SOURCES:
        raise ValueError(f"source must be one of {REVIEW_SOURCES}, got {source!r}")
    if not Path(db).exists():
        raise FileNotFoundError(f"database not found: {db}")
    if freqs is None:
        from pn.profile import gtowizard

        try:
            freqs = gtowizard.load_freqs()
        except FileNotFoundError:
            freqs = None
    grader = Grader(cache if cache is not None else SolveCache(), source=source,
                    profiles=profiles, game=budget, freqs=freqs)  # fmt: skip
    counts: dict[str, Any] = {"hands": 0, "graded": 0, "null": 0, "skipped": 0}
    notes: Counter = Counter()
    conn = ingest_db.connect(db)  # ensures the decision_ev table on old DBs
    try:
        _migrate(conn)
        ids = player_ids(conn, player) if player else None
        for hand in _hands(conn, ids, limit, since):
            decs = enumerate_decisions(hand, set(ids) if ids else None)
            if not force:
                # rows graded before Plan 7 (EV only, grade NULL) are graded again
                done = {r[0] for r in conn.execute(
                    "SELECT seq FROM decision_ev WHERE hand_id = ? AND source = ? AND "
                    "(grade IS NOT NULL OR (loss IS NULL AND note IS NOT NULL))",
                    (hand["hand_id"], source))}  # fmt: skip
                counts["skipped"] += sum(d.seq in done for d in decs)
                decs = [d for d in decs if d.seq not in done]
            counts["hands"] += 1
            if not decs:
                continue
            grades = grader.grade_hand(hand, decs)
            _write(conn, grades)
            graded = sum(g.grade is not None for g in grades)
            counts["graded"] += graded
            counts["null"] += len(grades) - graded
            notes.update((g.note or "").split(":")[0] for g in grades if g.grade is None)
            emit(f"hand #{hand.get('hand_no')} {hand['hand_id']}: {graded} graded, "
                 f"{len(grades) - graded} null")  # fmt: skip
    finally:
        conn.close()
    counts["notes"] = dict(notes)
    return counts
