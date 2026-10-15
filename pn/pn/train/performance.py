"""Trainer performance per spot: GTO closeness heatmap data (contracts/trainer.md "Plan 15
additions").

Rows group `train_decisions` by (spot, hero position) over hands filtered by table size, depth,
`since` (played_at >= it) and then the newest `window` hands. Only Plan 7 grades (best / mixed /
blunder) are scored; legacy grades ("ev" / "freq") count as `legacy` and other rows (grade
"none", NULL: multiway, off tree, no solve) as `ungraded`, both excluded from every score.
Closeness of a graded decision = freq(chosen) / max option freq from options_json.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pn.adapter.state import PREFLOP_ORDER, STREETS, order_key
from pn.solve.request import TABLE_SEATS
from pn.train import grading, spots
from pn.train.stats import LEGACY_GRADES, ensure

RECENT = 20  # newest graded decisions per cell (detail panel, cell_recent)
MAX_INT = 2**31 - 1  # window / depth bound (SQLite binds int64; anything near is nonsense)
TREND_BUCKETS = 20  # at most this many trend buckets
TREND_MIN = 5  # graded decisions per trend bucket at least
OTHER = "other"
_FACED_LABEL = {"rfi": "RFI", "vs_limp": "vs limp", "vs_open": "vs open", "vs_3bet": "vs 3-bet",
                "vs_4bet+": "vs 4-bet+", "cold": "3-bet cold"}  # fmt: skip
_POT_LABEL = {"limped": "Limped", "srp": "SRP", "3bp": "3BP", "4bp+": "4BP+"}
_SIT_LABEL = {"first": "first", "facing_bet": "vs bet", "facing_raise": "vs raise"}


def closeness(options_json: str | None, chosen: str | None) -> float | None:
    """freq(chosen) / max option freq (0 when chosen is not an option); None without usable
    frequencies."""
    freqs = _freqs(options_json)
    top = max(freqs.values(), default=0.0) if freqs else 0.0
    if top <= 0:
        return None
    return round(min(1.0, freqs.get(chosen, 0.0) / top), 4)


def _freqs(options_json: str | None) -> dict[str, float] | None:
    try:
        opts = json.loads(options_json or "")
        freqs = {o["token"]: o["freq"] for o in opts}
    except (TypeError, ValueError, KeyError):
        return None
    if any(not isinstance(f, int | float) for f in freqs.values()):
        return None
    return {t: max(0.0, float(f)) for t, f in freqs.items()}


def family(spot: str | None) -> dict[str, Any]:
    """{id, label, street, pot_type} of the heatmap column of `spot` (IP / OOP collapsed: the
    row is hero's position); unknown keys -> "other"."""
    other = {"id": OTHER, "label": "Other", "street": None, "pot_type": None}
    try:
        d = spots.parse(spot or "")
    except ValueError:
        return other
    if d["street"] == "preflop":
        if d.get("faced") not in spots.FACED:  # "post|..|preflop|.." parses without one
            return other
        return {
            "id": d["faced"],
            "label": _FACED_LABEL[d["faced"]],
            "street": "preflop",
            "pot_type": None,
        }
    if (d["pot"] not in spots.POTS or d["situation"] not in spots.SITUATIONS
            or d["street"] not in STREETS[1:]):  # fmt: skip
        return other
    return {"id": f"{d['pot']}|{d['street']}|{d['situation']}",
            "label": f"{_POT_LABEL[d['pot']]} {d['street']} {_SIT_LABEL[d['situation']]}",
            "street": d["street"], "pot_type": d["pot"]}  # fmt: skip


def _family_order(f: dict[str, Any]) -> tuple:
    if f["id"] == OTHER:
        return (len(STREETS),)
    if f["street"] == "preflop":
        return (0, spots.FACED.index(f["id"]))
    _, _, sit = f["id"].split("|")
    return (
        STREETS.index(f["street"]),
        spots.POTS.index(f["pot_type"]),
        spots.SITUATIONS.index(sit),
    )


def _since(since: str | None) -> str | None:
    """`since` as a played_at-comparable string (UTC ISO, naive = UTC); ValueError when
    unparseable."""
    if not since:
        return None
    t = datetime.fromisoformat(since)
    return (t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)).isoformat()


def _hands_sql(window: int | None, table: int | None, depth: int | None,
               since: str | None) -> tuple[str, list]:  # fmt: skip
    """SELECT of the filtered hands (newest `window` of them) and its parameters."""
    where, args = [], []
    for col, val in (("table_size", table), ("depth", depth)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since is not None:
        where.append("played_at >= ?")
        args.append(since)
    sql = (f"SELECT hand_uid, played_at, table_size, rowid AS hrow FROM train_hands "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY played_at DESC, rowid DESC")  # fmt: skip
    if window is not None:
        sql += " LIMIT ?"
        args.append(window)
    return sql, args


def _load(conn: sqlite3.Connection, hands_sql: str, args: list,
          extra: str = "", extra_args: Iterable = ()) -> tuple[set[int], list[dict]]:  # fmt: skip
    """(table sizes of the filtered hands, their decisions in play order with played_at, kind
    (graded | legacy | ungraded) and closeness); `extra` = more WHERE terms on train_decisions."""
    sizes = {r[0] for r in conn.execute(f"SELECT DISTINCT table_size FROM ({hands_sql})", args)}
    cur = conn.execute(f"SELECT d.hand_uid, d.seq, d.street, d.position, d.hand_class, d.options_json, "
                       f"d.chosen, d.best, d.grade, d.matched_rng, d.pure, d.spot, h.played_at "
                       f"FROM ({hands_sql}) h JOIN train_decisions d ON d.hand_uid = h.hand_uid {extra} "
                       f"ORDER BY h.played_at, h.hrow, d.seq", [*args, *extra_args])  # fmt: skip
    cols = [c[0] for c in cur.description]
    out: list[dict] = []
    for r in cur:
        d = dict(zip(cols, r, strict=True))
        g = d["grade"]
        kind = "graded" if g in grading.GRADES else "legacy" if g in LEGACY_GRADES else "ungraded"
        c = closeness(d["options_json"], d["chosen"]) if kind == "graded" else None
        out.append({**d, "kind": kind, "closeness": c})
    return sizes, out


def _validated(window: int | None, table: int | None, depth: int | None,
               since: str | None) -> tuple[str, list]:  # fmt: skip
    if window is not None and not 1 <= window <= MAX_INT:
        raise ValueError(f"window must be in 1..{MAX_INT}")
    if table is not None and table not in TABLE_SEATS:
        raise ValueError(f"table must be one of {sorted(TABLE_SEATS)}")
    if depth is not None and not 1 <= depth <= MAX_INT:
        raise ValueError(f"depth must be in 1..{MAX_INT}")
    return _hands_sql(window, table, depth, _since(since))


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _summary(decs: list[dict]) -> dict[str, Any]:
    graded = [d for d in decs if d["kind"] == "graded"]
    rolled = [d for d in decs if d["matched_rng"] is not None and not d["pure"]]
    return {"decisions": len(decs), "graded": len(graded),
            "legacy": sum(d["kind"] == "legacy" for d in decs),
            "ungraded": sum(d["kind"] == "ungraded" for d in decs),
            "best": sum(d["grade"] == grading.BEST for d in graded),
            "mixed": sum(d["grade"] == grading.MIXED for d in graded),
            "blunders": sum(d["grade"] == grading.BLUNDER for d in graded),
            "closeness": _mean([d["closeness"] for d in graded if d["closeness"] is not None]),
            "rng_match": _mean([float(bool(d["matched_rng"])) for d in rolled]),
            "rng_n": len(rolled)}  # fmt: skip


def _recent(decs: list[dict]) -> list[dict]:
    out = []
    for d in reversed([d for d in decs if d["kind"] == "graded"][-RECENT:]):
        freqs = _freqs(d["options_json"]) or {}
        out.append({"played_at": d["played_at"], "hand_class": d["hand_class"], "chosen": d["chosen"],
                    "chosen_freq": freqs.get(d["chosen"]), "best": d["best"],
                    "best_freq": freqs.get(d["best"]), "grade": d["grade"],
                    "closeness": d["closeness"], "spot": d["spot"], "label": _label(d["spot"])})  # fmt: skip
    return out


def _label(spot: str | None) -> str:
    return spots.label(spot) if family(spot)["id"] != OTHER else "Other"


def _positions(table: int | None, sizes: Iterable[int], present: Iterable[str]) -> list[str]:
    """Seats of `table` (else of the table sizes in the data, 6-max without any) in table order,
    then any other position present."""
    seats = set(TABLE_SEATS[table]) if table else {p for s in sizes for p in TABLE_SEATS.get(s, ())}
    seats = seats or set(TABLE_SEATS[6])
    order = [p for p in PREFLOP_ORDER if p in seats]
    return order + sorted(set(present) - set(order), key=lambda p: (order_key(p, "preflop"), p))


def _trend(decs: list[dict]) -> list[dict]:
    graded = [d for d in decs if d["kind"] == "graded"]
    size = max(TREND_MIN, math.ceil(len(graded) / TREND_BUCKETS))
    out = []
    for lo in range(0, len(graded), size):
        part = graded[lo : lo + size]
        s = _summary(part)
        out.append({"bucket": f"{lo + 1}-{lo + len(part)}", "from": lo + 1, "to": lo + len(part),
                    "date": part[-1]["played_at"][:10], "n": len(part), "closeness": s["closeness"],
                    "blunder_rate": round(s["blunders"] / len(part), 4)})  # fmt: skip
    return out


def performance(conn: sqlite3.Connection, window: int | None = None, table: int | None = None,
                depth: int | None = None, since: str | None = None) -> dict[str, Any]:  # fmt: skip
    """{totals, rows, axes, trend} over the filtered hands; ValueError on a bad filter."""
    sql, args = _validated(window, table, depth, since)
    ensure(conn)
    sizes, decs = _load(conn, sql, args)
    groups: dict[tuple, list[dict]] = {}
    for d in decs:
        groups.setdefault((d["spot"], d["position"]), []).append(d)
    positions = _positions(table, sizes, {k[1] for k in groups})
    fams = {f["id"]: f for f in (family(k[0]) for k in groups)}
    families = sorted(fams.values(), key=_family_order)
    col = {f["id"]: k for k, f in enumerate(families)}
    rows = []
    for (spot, pos), ds in groups.items():
        f = family(spot)
        s = _summary(ds)
        rows.append({"spot": spot, "label": _label(spot), "street": f["street"] or ds[0]["street"],
                     "position": pos, "family": f["id"], "pot_type": f["pot_type"],
                     "n": s["decisions"], "graded": s["graded"], "legacy": s["legacy"],
                     "best": s["best"], "mixed": s["mixed"], "blunders": s["blunders"],
                     "closeness": s["closeness"], "rng_match": s["rng_match"], "rng_n": s["rng_n"],
                     "last_played": ds[-1]["played_at"]})  # fmt: skip
    rows.sort(key=lambda r: (positions.index(r["position"]), col[r["family"]], r["spot"] or ""))
    return {"totals": _summary(decs), "rows": rows,
            "axes": {"positions": positions, "families": families}, "trend": _trend(decs)}  # fmt: skip


def cell_recent(conn: sqlite3.Connection, position: str, family_id: str, window: int | None = None,
                table: int | None = None, depth: int | None = None,
                since: str | None = None) -> dict[str, Any]:  # fmt: skip
    """{position, family, recent}: the newest RECENT graded decisions of one heatmap cell (hero
    `position` x `family_id`) under the same filters as performance(), newest first."""
    sql, args = _validated(window, table, depth, since)
    ensure(conn)
    _, decs = _load(conn, sql, args, "WHERE d.position = ?", [position])
    mine = [d for d in decs if family(d["spot"])["id"] == family_id]
    return {"position": position, "family": family_id, "recent": _recent(mine)}
