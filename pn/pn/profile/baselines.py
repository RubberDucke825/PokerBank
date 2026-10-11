"""GTO baselines (config/gto_baselines.json) and CI-aware deviations (contracts/profile.md)."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from pn.profile.paths import config_dir

HU_MW = ("hu", "multiway", "IP", "OOP")  # top-level spot keys in a baselines entry


def baselines_path() -> Path:
    return config_dir() / "gto_baselines.json"


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    del mtime  # cache key only
    return json.loads(Path(path).read_text())


def load_baselines(path: Path | None = None) -> dict:
    p = path or baselines_path()
    return copy.deepcopy(_load(str(p), p.stat().st_mtime))


def gto_value(baselines: Mapping, stat: str, spot: str | None = None) -> float | None:
    """Baseline for a stat. spot: None (overall), a position, `hu`/`multiway`, or
    `<street>_<bucket>` for fold_vs_size. Returns None when the spot has no baseline
    (a spot never falls back to the overall value: that would compare unlike things)."""
    if stat == "fold_vs_size":
        if spot is None:
            return None
        street, _, bucket = spot.partition("_")
        v = baselines.get("fold_vs_size", {}).get(street, {}).get(bucket)
        return None if v is None else float(v)
    entry = baselines.get("stats", {}).get(stat)
    if entry is None:
        return None
    if spot is None:
        v = entry.get("default")
    elif spot in HU_MW:
        v = entry.get(spot)
    else:
        v = entry.get("by_position", {}).get(spot)
    return None if v is None else float(v)


def ci_dev(stat: Mapping, base: float | None) -> float | None:
    """0 if the 90% CI contains `base`; else signed distance from the nearest CI edge
    (positive = player above the baseline). None when there is no baseline."""
    if base is None:
        return None
    lo, hi = stat["ci90"]
    if lo <= base <= hi:
        return 0.0
    return round(lo - base, 4) if base < lo else round(hi - base, 4)


def annotate(stat: Mapping, gto: float | None) -> dict:
    """Stat object with `gto`, `dev_pop` (vs prior_mean) and `dev_gto` added."""
    return {
        **stat,
        "gto": None if gto is None else round(gto, 4),
        "dev_pop": ci_dev(stat, stat["prior_mean"]),
        "dev_gto": ci_dev(stat, gto),
    }
