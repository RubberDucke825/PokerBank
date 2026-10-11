"""Precomputed preflop root documents per table/depth/source (read-only since Plan 7: no engine
solves preflop, so roots are only read when an old file exists). The heads-up flop library the
trainer deals from is filled by pn.train.library_fill."""

from __future__ import annotations

import os
from pathlib import Path

from pn.profile.paths import repo_root


def library_dir() -> Path:
    return Path(os.environ.get("PN_LIBRARY_DIR", repo_root() / "data" / "solves" / "library"))


def library_path(table: int, depth_bb: int, source: str) -> Path:
    """The root document `<library dir>/<table>max_<depth>bb_<source>.json`."""
    return library_dir() / f"{table}max_{depth_bb}bb_{source}.json"
