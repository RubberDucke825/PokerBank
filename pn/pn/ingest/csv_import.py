"""PokerNow 'Download full log' CSV (columns entry,at,order) -> raw message dicts.

The logged-in export uses the same message strings as the JSON log, plus
'Your hand is C, C' lines for the downloading player. Multi-line entries
(e.g. 'Game Config Changes') are quoted and handled by the csv module.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

_GAME_ID_RE = re.compile(r"(?:poker_now_log_|pokernow_)([A-Za-z0-9_-]+)\.csv$")


def game_id_from_filename(path: str | Path) -> str | None:
    m = _GAME_ID_RE.search(Path(path).name)
    return m.group(1) if m else None


def read_csv_log(path: str | Path) -> list[dict[str, Any]]:
    """Return [{at, created_at, msg}] newest first (same shape as fetch_game_log)."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"entry", "at", "order"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: not a PokerNow log CSV (missing columns {sorted(missing)})")
        for row in reader:
            out.append({"at": row["at"], "created_at": str(row["order"]).strip(), "msg": row["entry"]})
    out.sort(key=lambda m: int(m["created_at"]) if m["created_at"].isdigit() else 0, reverse=True)
    return out
