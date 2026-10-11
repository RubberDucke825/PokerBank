"""fetch/import -> parse -> load -> validate, shared by the CLI and tests."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pn.ingest import db
from pn.ingest.csv_import import game_id_from_filename, read_csv_log
from pn.ingest.fetch import FetchError, fetch_game_log, fetch_ledger, parse_game_id
from pn.ingest.parse import parse_messages, session_events
from pn.ingest.validate import read_ledger, validate_game

log = logging.getLogger(__name__)
LEDGER_DIRS = ("data/raw", "data/fixtures")


@dataclass(frozen=True)
class IngestResult:
    game_id: str
    hands: list[dict[str, Any]]
    load_stats: dict[str, int]
    report: dict[str, Any]
    ledger_path: Path | None


def find_ledger(game_id: str, dirs: tuple[str, ...] = LEDGER_DIRS) -> Path | None:
    for d in dirs:
        p = Path(d) / f"ledger_{game_id}.csv"
        if p.exists():
            return p
    return None


def load_messages(
    source: str, cache_dir: str, game_id: str | None = None
) -> tuple[str, list[dict[str, Any]], str]:
    """Source = url / game id (fetched) or a local .json log / .csv export. Returns (id, msgs, kind)."""
    path = Path(source)
    if path.is_file():
        if path.suffix.lower() == ".csv":
            gid = game_id or game_id_from_filename(path)
            msgs, kind = read_csv_log(path), "pokernow_csv"
        else:
            data = json.loads(path.read_text())
            msgs = data["logs"] if isinstance(data, dict) else data
            gid = game_id or (data.get("game_id") if isinstance(data, dict) else None)
            gid = gid or path.stem.rsplit("_", 1)[-1]
            kind = "pokernow_log"
        if not gid:
            raise ValueError(f"cannot infer game id from {path.name}; pass --game-id")
        return gid, msgs, kind
    gid = parse_game_id(source)
    return gid, fetch_game_log(source, cache_dir), "pokernow_log"


def ingest(
    source: str,
    db_path: str = db.DEFAULT_DB,
    *,
    cache_dir: str = "data/raw",
    names: dict[str, list[str]] | None = None,
    ledger: str | None = None,
    fetch_ledger_csv: bool = True,
    game_id: str | None = None,
    hero_id: str | None = None,
) -> IngestResult:
    gid, msgs, kind = load_messages(source, cache_dir, game_id)
    hands = parse_messages(msgs, game_id=gid, source_kind=kind, hero_id=hero_id)
    url = source if source.startswith("http") else None
    stats = db.load(hands, db_path, url=url)
    events = session_events(msgs)
    with db.opened(db_path) as conn:
        for e in (e for e in events if e["kind"] == "id_change"):
            db.merge(conn, [e["new"], e["old"]], note="PokerNow authenticated-login id change")
        if names:  # values may be player ids or the usernames shown in this game's log
            db.assign_names(
                conn, {n: db.resolve_player_refs(conn, refs, gid) for n, refs in names.items()}
            )
    ledger_path = Path(ledger) if ledger else find_ledger(gid)
    if ledger_path is None and fetch_ledger_csv and not Path(source).is_file():
        try:
            ledger_path = fetch_ledger(gid, cache_dir)
        except (FetchError, OSError) as exc:
            log.warning("ledger download failed: %s", exc)
    report = validate_game(hands, read_ledger(ledger_path) if ledger_path else None, events)
    return IngestResult(gid, hands, stats, report, ledger_path)
