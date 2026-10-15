"""Trainer persistence and rolling performance (contracts/trainer.md "Plan 4 additions", "Plan 7
additions").

Hands are written once at hand end into train_hands / train_decisions (pn/pn/ingest/schema.sql).
`rolling()` reads the newest `window_hands` hands: best / mixed / blunder counts and rates over
graded decisions (pn.train.grading; Plan 7, no EV), rng_match_rate over decisions with a roll in
non-pure spots, and 100-hand slices oldest first.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from pn.adapter.state import STREETS
from pn.train import grading
from pn.train.engine import State

LEGACY_GRADES = ("ev", "freq")  # train_decisions.grade before Plan 7
ROLLING_KEYS = ("window", "hands", "rng_match_rate", "blunder_rate", "mixed_rate", "best_rate")
SLICE_HANDS = 100
HAND_COLS = ("hand_uid", "session_id", "played_at", "table_size", "depth", "source", "hero_pos",
             "net_bb", "opponents_json", "drill_spot", "drill_landed")  # fmt: skip
DECISION_COLS = ("hand_uid", "seq", "street", "position", "situation", "hand_class", "rng",
                 "options_json", "chosen", "rng_action", "best", "loss_bb", "grade",
                 "matched_rng", "pure", "spot", "note", "board", "pot_bb", "formation")  # fmt: skip
ADDED_COLUMNS = {  # columns added after the tables first shipped (Plan 6, Plan 14)
    "train_hands": {"drill_spot": "TEXT", "drill_landed": "INTEGER"},
    "train_decisions": {"spot": "TEXT", "note": "TEXT", "board": "TEXT", "pot_bb": "REAL",
                        "formation": "TEXT"},
}
BB_CENTS = 100  # trainer big blind (feedback amounts are in cents)


def ensure(conn: sqlite3.Connection) -> None:
    """ALTER the Plan 6 / Plan 14 columns into databases created before them (older rows keep
    NULL). Safe when another connection migrates at the same time."""
    for table, cols in ADDED_COLUMNS.items():
        have = _columns(conn, table)
        for col, kind in cols.items():
            if col in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError:
                if col not in _columns(conn, table):  # not a concurrent ALTER: a real failure
                    raise
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def situation_kind(st: State, i: int) -> str:
    """rfi | vs_open | vs_3bet | vs_4bet+ | cold preflop (by raises faced; limps count as
    unopened); first | facing_bet | facing_raise postflop."""
    if st.street != "preflop":
        return ("first", "facing_bet", "facing_raise")[min(st.bets, 2)]
    raises = st.bets - 1  # the big blind counts as one
    if raises <= 1:
        return ("rfi", "vs_open")[raises]
    if raises == 2:
        raised = any(a.street == "preflop" and a.kind == "raise" and a.seat == i for a in st.history)
        return "vs_3bet" if raised else "cold"
    return "vs_4bet+"


def record_hand(conn: sqlite3.Connection, hand: Mapping[str, Any],
                decisions: Iterable[Mapping[str, Any]]) -> None:  # fmt: skip
    """Insert one finished hand and its hero decisions (keys as HAND_COLS / DECISION_COLS;
    missing drill / spot / note keys are NULL)."""
    ensure(conn)
    with conn:
        conn.execute(f"INSERT INTO train_hands ({','.join(HAND_COLS)}) "
                     f"VALUES ({','.join('?' * len(HAND_COLS))})",
                     [hand.get(c) for c in HAND_COLS])  # fmt: skip
        conn.executemany(f"INSERT INTO train_decisions ({','.join(DECISION_COLS)}) "
                         f"VALUES ({','.join('?' * len(DECISION_COLS))})",
                         [[d.get(c) for c in DECISION_COLS] for d in decisions])  # fmt: skip


def decision_grade(row: Mapping[str, Any]) -> str | None:
    """best | mixed | blunder for a stored decision, None when it was not graded. Rows written
    before Plan 7 (grade "ev" / "freq") are re-graded from their options' frequencies."""
    g = row.get("grade")
    if g in grading.GRADES:
        return g
    if g not in LEGACY_GRADES:
        return None
    try:
        freqs = {o["token"]: o.get("freq") for o in json.loads(row["options_json"])}
    except (TypeError, ValueError, KeyError):
        return None
    if any(f is None for f in freqs.values()):
        return None
    res = grading.try_grade(freqs, row["chosen"])
    return res.grade if res is not None else None


def _rate(num: float, den: int, nd: int = 4) -> float | None:
    return round(num / den, nd) if den else None


def _group(decs: list[dict], hands: int | None = None) -> dict[str, Any]:
    """hands (default: hands with a decision here), decisions, graded, best / mixed / blunder
    counts and rates over graded decisions, rng_match_rate."""
    graded = [d["g"] for d in decs if d["g"] is not None]
    rolled = [d for d in decs if d["matched_rng"] is not None and not d["pure"]]
    n_hands = len({d["hand_uid"] for d in decs}) if hands is None else hands
    counts = {g: graded.count(g) for g in grading.GRADES}
    return {"hands": n_hands, "decisions": len(decs), "graded": len(graded),
            "best": counts[grading.BEST], "mixed": counts[grading.MIXED],
            "blunders": counts[grading.BLUNDER],
            "best_rate": _rate(counts[grading.BEST], len(graded)),
            "mixed_rate": _rate(counts[grading.MIXED], len(graded)),
            "blunder_rate": _rate(counts[grading.BLUNDER], len(graded)),
            "rng_match_rate": _rate(sum(bool(d["matched_rng"]) for d in rolled), len(rolled))}  # fmt: skip


def _by(decs: list[dict], key: str, order: Iterable[str] | None = None) -> dict[str, Any]:
    groups: dict[str, list[dict]] = {}
    for d in decs:
        groups.setdefault(d[key], []).append(d)
    keys = [k for k in order if k in groups] if order is not None else sorted(groups)
    return {k: _group(groups[k]) for k in keys}


def rolling(conn: sqlite3.Connection, window_hands: int = 1000,
            source: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Performance over the newest `window_hands` trainer hands (of `source` when given)."""
    if window_hands < 1:
        raise ValueError("window must be >= 1")
    ensure(conn)
    where, args = ("WHERE source = ?", [source]) if source else ("", [])
    rows = conn.execute(f"SELECT hand_uid, hero_pos, drill_spot, drill_landed FROM train_hands {where} "
                        "ORDER BY played_at DESC, rowid DESC LIMIT ?",
                        [*args, window_hands]).fetchall()  # fmt: skip
    hands = [r[0] for r in reversed(rows)]  # oldest first
    pos = {r[0]: r[1] for r in rows}
    decs: list[dict] = []
    for k in range(0, len(hands), 500):  # stay under SQLite's bound-parameter limit
        chunk = hands[k : k + 500]
        cur = conn.execute(f"SELECT hand_uid, seq, street, situation, grade, options_json, chosen, "
                           f"matched_rng, pure FROM train_decisions "
                           f"WHERE hand_uid IN ({','.join('?' * len(chunk))})", chunk)  # fmt: skip
        cols = [c[0] for c in cur.description]
        for r in cur:
            d = dict(zip(cols, r, strict=True))
            decs.append({**d, "g": decision_grade(d), "position": pos[r[0]]})
    index = {h: k for k, h in enumerate(hands)}
    decs.sort(key=lambda d: (index[d["hand_uid"]], d["seq"]))
    total = _group(decs, len(hands))
    slices = []
    for lo in range(0, len(hands), SLICE_HANDS):
        part = hands[lo : lo + SLICE_HANDS]
        g = _group([d for d in decs if lo <= index[d["hand_uid"]] < lo + len(part)], len(part))
        slices.append({"from_hand": lo + 1, "to_hand": lo + len(part), "hands": len(part),
                       "decisions": g["decisions"], "graded": g["graded"],
                       "blunder_rate": g["blunder_rate"], "rng_match_rate": g["rng_match_rate"]})  # fmt: skip
    return {
        "window": window_hands, "source": source, **total,
        "rng_decisions": sum(1 for d in decs if d["matched_rng"] is not None and not d["pure"]),
        "by_street": _by(decs, "street", STREETS), "by_position": _by(decs, "position"),
        "by_situation": _by(decs, "situation"), "slices": slices,
        "drill_hands": sum(1 for r in rows if r[2]), "natural_hands": sum(1 for r in rows if not r[2]),
        "drills_landed": sum(1 for r in rows if r[2] and r[3]),
    }  # fmt: skip


def totals(stats: Mapping[str, Any]) -> dict[str, Any]:
    """The state's `rolling` summary."""
    return {k: stats[k] for k in ROLLING_KEYS}


def decision_row(hand_uid: str, seq: int, feedback: Mapping[str, Any], situation: str,
                 hand_class: str, spot: str | None = None,
                 formation: str | None = None) -> dict[str, Any]:  # fmt: skip
    """train_decisions row from a hero feedback dict (Plan 14: the board postflop, the pot in bb
    and the heads-up formation postflop)."""
    board = "".join(feedback.get("board") or ()) if feedback["street"] != "preflop" else ""
    pot = feedback.get("pot")
    return {"hand_uid": hand_uid, "seq": seq, "street": feedback["street"],
            "position": feedback["position"], "situation": situation, "hand_class": hand_class,
            "rng": feedback["rng"], "options_json": json.dumps(feedback["options"]),
            "chosen": feedback["chosen"], "rng_action": feedback["rng_action"],
            "best": feedback["best"], "loss_bb": feedback["loss_bb"], "grade": feedback["grade"],
            "matched_rng": None if feedback["matched_rng"] is None else int(feedback["matched_rng"]),
            "pure": int(feedback["pure"]), "spot": spot, "note": feedback.get("note"),
            "board": board or None, "pot_bb": None if pot is None else pot / BB_CENTS,
            "formation": formation if feedback["street"] != "preflop" else None}  # fmt: skip
