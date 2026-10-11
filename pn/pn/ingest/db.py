"""SQLite storage for normalized hands (idempotent load) and player identity."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from pn.ingest.derive import annotate_actions, player_flags

DEFAULT_DB = "data/pn.sqlite"


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    if str(db_path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(files("pn.ingest").joinpath("schema.sql").read_text())
    return conn


@contextmanager
def opened(db_path: str | Path = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _upsert_game(
    conn: sqlite3.Connection, game_id: str, hands: list[dict[str, Any]], url: str | None
) -> None:
    times = [h["started_at"] for h in hands if h.get("started_at")]
    conn.execute(
        """INSERT INTO games(game_id, url, first_hand_at, last_hand_at, fetched_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(game_id) DO UPDATE SET
             url = COALESCE(excluded.url, games.url),
             first_hand_at = MIN(COALESCE(games.first_hand_at, excluded.first_hand_at), excluded.first_hand_at),
             last_hand_at = MAX(COALESCE(games.last_hand_at, excluded.last_hand_at), excluded.last_hand_at),
             fetched_at = excluded.fetched_at""",
        (game_id, url, min(times, default=None), max(times, default=None), _now()),
    )


def _upsert_player(conn: sqlite3.Connection, pid: str, name: str, at: str) -> None:
    conn.execute(
        """INSERT INTO players(player_id, canonical_id, display_name, nickname, first_seen, last_seen)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(player_id) DO UPDATE SET
             nickname = excluded.nickname,
             first_seen = MIN(COALESCE(players.first_seen, excluded.first_seen), excluded.first_seen),
             last_seen = MAX(COALESCE(players.last_seen, excluded.last_seen), excluded.last_seen)""",
        (pid, pid, name, name, at, at),
    )


def _insert_hand(conn: sqlite3.Connection, hand: dict[str, Any]) -> None:
    hid = hand["hand_id"]
    conn.execute(
        "INSERT INTO hands(hand_id, game_id, hand_no, started_at, n_players, flags, json) VALUES (?,?,?,?,?,?,?)",
        (
            hid,
            hand["source"]["game_id"],
            hand["hand_no"],
            hand["started_at"],
            len(hand["players"]),
            ",".join(hand["flags"]),
            json.dumps(hand, separators=(",", ":")),
        ),
    )
    flags = player_flags(hand)
    conn.executemany(
        """INSERT INTO hand_players(hand_id, player_id, seat, position, stack_start, net, cards,
             vpip, pfr, saw_flop, went_to_sd, won_sd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                hid,
                p["player_id"],
                p["seat"],
                p["position"],
                p["stack_start"],
                hand["net"].get(p["player_id"], 0),
                "".join(p["cards"]) if p["cards"] else None,
                *(
                    int(flags[p["player_id"]][k])
                    for k in ("vpip", "pfr", "saw_flop", "went_to_sd", "won_sd")
                ),
            )
            for p in hand["players"]
        ],
    )
    conn.executemany(
        """INSERT INTO actions(hand_id, seq, street, player_id, kind, amount, to_amount, allin,
             pot_before, facing_bet_pct, n_active) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                hid,
                a["seq"],
                a["street"],
                a["player_id"],
                a["kind"],
                a["amount"],
                a["to_amount"],
                int(a["allin"]),
                a["pot_before"],
                a["facing_bet_pct"],
                a["n_active"],
            )
            for a in annotate_actions(hand)
        ],
    )
    for p in hand["players"]:
        _upsert_player(conn, p["player_id"], p["name"], hand["started_at"])


def merge_known_cards(stored: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
    """Fill hole cards the stored hand lacks from a newer version of the same hand
    (e.g. a logged-in CSV export with 'Your hand is' after the public log was loaded).
    hero_export cards also replace showdown-sourced ones: they are not selection-biased.
    Returns the updated hand, or None if nothing new is known."""
    incoming = {p["player_id"]: p for p in new["players"]}
    changed = False
    players = []
    for p in stored["players"]:
        n = incoming.get(p["player_id"])
        better = (
            n is not None
            and n["cards"]
            and (
                p["cards"] is None
                or (n["cards_source"] == "hero_export" and p["cards_source"] != "hero_export")
            )
        )
        if better:
            p = {**p, "cards": n["cards"], "cards_source": n["cards_source"]}
            changed = True
        players.append(p)
    if not changed:
        return None
    merged = {**stored, "players": players}
    if "hero_cards" in new:
        merged["hero_cards"] = new["hero_cards"]
    return merged


def _enrich(conn: sqlite3.Connection, stored: dict[str, Any], hand: dict[str, Any]) -> bool:
    merged = merge_known_cards(stored, hand)
    if merged is None:
        return False
    conn.execute(
        "UPDATE hands SET json = ? WHERE hand_id = ?",
        (json.dumps(merged, separators=(",", ":")), stored["hand_id"]),
    )
    conn.executemany(
        "UPDATE hand_players SET cards = ? WHERE hand_id = ? AND player_id = ?",
        [
            ("".join(p["cards"]), stored["hand_id"], p["player_id"])
            for p in merged["players"]
            if p["cards"]
        ],
    )
    return True


def load(
    hands: Iterable[dict[str, Any]], db_path: str | Path = DEFAULT_DB, url: str | None = None
) -> dict[str, int]:
    """Insert hands; hand_ids already present are skipped, except that
    - a stored 'incomplete' hand is replaced by a complete version (live fetch mid-hand), and
    - hole cards the stored hand lacks are merged in ('enriched'), e.g. hero cards from a CSV export.
    Returns counts {inserted, skipped, replaced, enriched}."""
    hands = list(hands)
    stats = {"inserted": 0, "skipped": 0, "replaced": 0, "enriched": 0}
    with opened(db_path) as conn:
        for game_id in sorted({h["source"]["game_id"] for h in hands}):
            _upsert_game(
                conn, game_id, [h for h in hands if h["source"]["game_id"] == game_id], url
            )
        for hand in hands:
            row = conn.execute(
                "SELECT flags, json FROM hands WHERE hand_id = ?", (hand["hand_id"],)
            ).fetchone()
            if row is not None:
                was_incomplete = "incomplete" in row["flags"].split(",")
                if not (was_incomplete and "incomplete" not in hand["flags"]):
                    stats[
                        "enriched" if _enrich(conn, json.loads(row["json"]), hand) else "skipped"
                    ] += 1
                    continue
                conn.execute("DELETE FROM hands WHERE hand_id = ?", (hand["hand_id"],))
                stats["replaced"] += 1
            else:
                stats["inserted"] += 1
            _insert_hand(conn, hand)
    return stats


# ------------------------------------------------------------------ identity
def resolve(conn: sqlite3.Connection, player_id: str) -> str:
    row = conn.execute(
        "SELECT canonical_id FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if row is not None:
        return row["canonical_id"]
    row = conn.execute(
        "SELECT canonical_id FROM aliases WHERE player_id = ?", (player_id,)
    ).fetchone()
    return row["canonical_id"] if row is not None else player_id


def group_ids(conn: sqlite3.Connection, player_id: str) -> list[str]:
    """All player_ids sharing player_id's canonical id."""
    canon = resolve(conn, player_id)
    rows = conn.execute(
        "SELECT player_id FROM players WHERE canonical_id = ? UNION SELECT player_id FROM aliases WHERE canonical_id = ?",
        (canon, canon),
    ).fetchall()
    return sorted({r["player_id"] for r in rows} | {player_id})


def merge(
    conn: sqlite3.Connection, ids: list[str], name: str | None = None, note: str = "manual merge"
) -> str:
    """Group ids under one canonical id (first id's canonical). Returns the canonical id."""
    canon = resolve(conn, ids[0])
    old = {resolve(conn, i) for i in ids}
    for i in ids:  # ids not seen in any hand yet still get a players row
        conn.execute(
            "INSERT OR IGNORE INTO players(player_id, canonical_id, display_name) VALUES (?,?,?)",
            (i, i, name),
        )
    marks = ",".join("?" * len(old))
    conn.execute(
        f"UPDATE players SET canonical_id = ? WHERE canonical_id IN ({marks})", (canon, *old)
    )
    conn.execute(
        f"UPDATE aliases SET canonical_id = ? WHERE canonical_id IN ({marks})", (canon, *old)
    )
    members = conn.execute(
        "SELECT player_id FROM players WHERE canonical_id = ?", (canon,)
    ).fetchall()
    conn.executemany(
        """INSERT INTO aliases(player_id, canonical_id, note) VALUES (?,?,?)
           ON CONFLICT(player_id) DO UPDATE SET canonical_id = excluded.canonical_id""",
        [(r["player_id"], canon, note) for r in members],
    )
    if name:
        conn.execute("UPDATE players SET display_name = ? WHERE canonical_id = ?", (name, canon))
    return canon


def resolve_player_refs(
    conn: sqlite3.Connection, refs: list[str], game_id: str | None = None
) -> list[str]:
    """Player ids for user-supplied references: a PokerNow player id, or the username
    (nickname) shown in the log. Usernames are matched case-insensitively, first among the
    players of `game_id`, then among every known player; a username that matches several
    ids (or none) raises ValueError, since usernames are not unique or stable."""
    out = []
    for ref in refs:
        if conn.execute("SELECT 1 FROM players WHERE player_id = ?", (ref,)).fetchone():
            out.append(ref)
            continue
        scopes = []
        if game_id:
            scopes.append(
                (
                    """SELECT DISTINCT p.player_id, p.nickname FROM players p
                 JOIN hand_players hp ON hp.player_id = p.player_id
                 JOIN hands h ON h.hand_id = hp.hand_id
                 WHERE h.game_id = ? AND LOWER(p.nickname) = LOWER(?)""",
                    (game_id, ref),
                )
            )
        scopes.append(
            ("SELECT player_id, nickname FROM players WHERE LOWER(nickname) = LOWER(?)", (ref,))
        )
        hits: list[sqlite3.Row] = []
        for sql, params in scopes:
            hits = conn.execute(sql, params).fetchall()
            if hits:
                break
        if len(hits) == 1:
            out.append(hits[0]["player_id"])
        elif not hits:
            raise ValueError(f"no player id or username matches {ref!r}")
        else:
            ids = ", ".join(f"{r['player_id']} ({r['nickname']})" for r in hits)
            raise ValueError(f"username {ref!r} matches several player ids, use the id: {ids}")
    return out


def assign_names(conn: sqlite3.Connection, names: dict[str, list[str]]) -> dict[str, str]:
    """{full name: [player_ids]} -> merge each group, reusing an existing canonical id that
    already carries that display name (same human across games). Returns name -> canonical."""
    out = {}
    for name, ids in names.items():
        row = conn.execute(
            "SELECT canonical_id FROM players WHERE display_name = ? AND player_id = canonical_id LIMIT 1",
            (name,),
        ).fetchone()
        group = ([row["canonical_id"]] if row else []) + [
            i for i in ids if not row or i != row["canonical_id"]
        ]
        out[name] = merge(conn, group, name=name, note=f"--name {name}")
    return out


def unnamed_players(conn: sqlite3.Connection, game_id: str | None = None) -> list[dict[str, Any]]:
    """Players nobody has given a full name yet (display name missing or still the PokerNow
    username), with their hand count; optionally only those seen in `game_id`."""
    sql = """SELECT p.player_id, p.nickname, COUNT(hp.hand_id) AS hands
             FROM players p LEFT JOIN hand_players hp ON hp.player_id = p.player_id
             {join}
             WHERE p.player_id = p.canonical_id
               AND (p.display_name IS NULL OR p.display_name = '' OR p.display_name = p.nickname)
             GROUP BY p.player_id ORDER BY hands DESC"""
    if game_id:
        sql = sql.format(join="JOIN hands h ON h.hand_id = hp.hand_id AND h.game_id = ?")
        rows = conn.execute(sql, (game_id,)).fetchall()
    else:
        rows = conn.execute(sql.format(join="")).fetchall()
    return [dict(r) for r in rows]


def alias_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.canonical_id, c.display_name, GROUP_CONCAT(p.player_id || ':' || COALESCE(p.nickname, ''), ', ')
             AS members, COUNT(*) AS n
           FROM players p LEFT JOIN players c ON c.player_id = p.canonical_id
           GROUP BY p.canonical_id ORDER BY c.display_name""",
    ).fetchall()
    return [dict(r) for r in rows]


def hands_for_game(conn: sqlite3.Connection, game_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT json FROM hands WHERE game_id = ? ORDER BY hand_no", (game_id,)
    ).fetchall()
    return [json.loads(r["json"]) for r in rows]


def get_hand(conn: sqlite3.Connection, hand_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT json FROM hands WHERE hand_id = ?", (hand_id,)).fetchone()
    return json.loads(row["json"]) if row else None
