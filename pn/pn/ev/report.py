"""Read side of decision_ev: per-hand rows and a player's summary of best / mixed / blunder
counts (Plan 7: frequency grades, no EV burned). Opens the DB read-only in spirit (no schema
writes) so the web store's mtime cache holds."""

from __future__ import annotations

import sqlite3
from typing import Any

from pn.train.grading import BEST, BLUNDER, GRADES, MIXED

STREETS = ("preflop", "flop", "turn", "river")
TOP_HANDS = 10
UNMODELED = "combo_added_to_range"  # combo outside the modeled range: grade unreliable
COUNT_KEY = {BEST: "best", MIXED: "mixed", BLUNDER: "blunders"}
_BB = "CAST(json_extract(h.json,'$.blinds.bb') AS REAL)"


def player_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    """Every player_id of the canonical player named by an id or a display name/nickname
    (pn.profile.source.PlayerNotFound when nothing matches)."""
    from pn.profile import source

    amap = source.alias_map(conn)
    seen = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM hand_players")}
    names = {**{amap.get(p, p): amap.get(p, p) for p in seen}, **source.display_names(conn)}
    cid = source.resolve_target(query, amap, names)
    return sorted({p for p in seen | set(amap) if amap.get(p, p) == cid} | {cid})


def has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_ev'").fetchone() is not None


def hand_rows(conn: sqlite3.Connection, hand_id: str, source: str) -> list[dict[str, Any]]:
    """Rows for one hand, in action order; `loss_bb` etc. added (None when not graded)."""
    if not has_table(conn):
        return []
    rows = conn.execute(
        f"SELECT d.*, {_BB} AS bb FROM decision_ev d JOIN hands h ON h.hand_id = d.hand_id "
        "WHERE d.hand_id = ? AND d.source = ? ORDER BY d.seq", (hand_id, source)).fetchall()  # fmt: skip
    out = []
    for r in rows:
        row = dict(r)
        bb = row.pop("bb") or 0
        for k in ("ev_chosen", "ev_best", "loss"):
            row[f"{k}_bb"] = round(row[k] / bb, 3) if bb and row[k] is not None else None
        out.append({"grade": None, "freq_chosen": None, "best_freq": None, **row})
    return out


def _group(rows: list[dict], key: str, order: tuple[str, ...] = ()) -> list[dict]:
    acc: dict[str, dict] = {}
    for r in rows:
        g = acc.setdefault(r[key], {key: r[key], "n": 0, "graded": 0, "best": 0, "mixed": 0,
                                    "blunders": 0})  # fmt: skip
        g["n"] += 1
        if r["grade"] in GRADES:
            g["graded"] += 1
            g[COUNT_KEY[r["grade"]]] += 1
    rank = {k: i for i, k in enumerate(order)}
    return sorted(acc.values(), key=lambda g: (rank.get(g[key], len(rank)), -g["blunders"]))


def _totals(rows: list[dict]) -> dict[str, Any]:
    graded = [r for r in rows if r["grade"] in GRADES]
    hands = {r["hand_id"] for r in graded}
    counts = {g: sum(r["grade"] == g for r in graded) for g in GRADES}
    blunders = counts[BLUNDER]
    return {"decisions": len(rows), "graded": len(graded), "null": len(rows) - len(graded),
            "hands": len(hands), "best": counts[BEST], "mixed": counts[MIXED],
            "blunders": blunders,
            "blunder_rate": round(blunders / len(graded), 4) if graded else None,
            "blunders_per_100": round(100 * blunders / len(hands), 2) if hands else None}  # fmt: skip


def _rows(conn: sqlite3.Connection, ids: list[str], source: str | None) -> list[dict]:
    """decision_ev rows of `ids` as dicts; `grade` is None on databases from before Plan 7."""
    marks = ",".join("?" * len(ids))
    where = f"d.player_id IN ({marks})" + (" AND d.source = ?" if source else "")
    cur = conn.execute(
        f"SELECT d.*, h.hand_no FROM decision_ev d JOIN hands h ON h.hand_id = d.hand_id "
        f"WHERE {where}", (*ids, *([source] if source else [])))  # fmt: skip
    cols = [c[0] for c in cur.description]
    return [{"grade": None, **dict(zip(cols, r, strict=True))} for r in cur]


def _unmodeled(rows: list[dict]) -> dict[str, Any]:
    off = [r for r in rows if r["note"] == UNMODELED]
    return {"decisions": len(off), "blunders": sum(r["grade"] == BLUNDER for r in off)}


def summary(conn: sqlite3.Connection, ids: list[str], source: str,
            include_unmodeled: bool = False) -> dict[str, Any]:  # fmt: skip
    """Totals, by street, by spot, top hands by blunders, notes; `compare` = totals for every
    source. Rows graded off-model (UNMODELED) are left out of all of these unless
    include_unmodeled; `unmodeled` always reports them separately."""
    empty = {"source": source, **_totals([]), "by_street": [], "by_spot": [], "top_hands": [],
             "notes": {}, "compare": {}, "unmodeled": _unmodeled([]),
             "include_unmodeled": include_unmodeled}  # fmt: skip
    if not ids or not has_table(conn):
        return empty
    every = _rows(conn, ids, None)
    unmodeled = _unmodeled([r for r in every if r["source"] == source])
    if not include_unmodeled:
        every = [r for r in every if r["note"] != UNMODELED]
    rows = [r for r in every if r["source"] == source]
    hands: dict[str, dict] = {}
    for r in rows:
        if r["grade"] not in GRADES:
            continue
        h = hands.setdefault(r["hand_id"], {"hand_id": r["hand_id"], "hand_no": r["hand_no"],
                                            "blunders": 0, "decisions": 0})  # fmt: skip
        h["blunders"] += r["grade"] == BLUNDER
        h["decisions"] += 1
    top = sorted((h for h in hands.values() if h["blunders"]),
                 key=lambda h: (-h["blunders"], h["hand_id"]))[:TOP_HANDS]  # fmt: skip
    notes: dict[str, int] = {}
    for r in rows:
        if r["grade"] not in GRADES:
            k = (r["note"] or "unknown").split(":")[0]
            notes[k] = notes.get(k, 0) + 1
    by_source: dict[str, list] = {}
    for r in every:
        by_source.setdefault(r["source"], []).append(r)
    return {"source": source, **_totals(rows), "by_street": _group(rows, "street", STREETS),
            "by_spot": _group(rows, "spot"), "top_hands": top, "notes": notes,
            "compare": {s: _totals(rs) for s, rs in sorted(by_source.items())},
            "unmodeled": unmodeled, "include_unmodeled": include_unmodeled}  # fmt: skip


