"""Preflop chart data for the web UI: pure chart files (config/preflop_charts) and the
GTO Wizard mixed-frequency JSON (data/gtowizard/cash6m_cev_2.5x_freqs.json)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pn import ranges
from pn.profile import charts, gtowizard
from pn.train import preflop as pre

ACTION_CODES = ("F", "C", "X", "R", "A")  # fold, call, check, raise, all-in
KIND_LABELS = {
    "rfi": "RFI", "vs_open": "vs open", "vs_3bet": "vs 3-bet", "vs_4bet": "vs 4-bet",
    "cold": "cold (vs open + 3-bet)", "vs_limp": "vs limp",
}  # fmt: skip


class ChartNotFound(LookupError):
    """Unknown chart file/key or GTO Wizard spot."""


def class_grid(d: dict[str, float]) -> dict[str, float]:
    """Hand class -> mean combo weight (0..1) for a combo-weight dict."""
    out = {}
    for cls in ranges.all_classes():
        combos = ranges.class_combos(cls)
        w = sum(d.get(c, 0.0) for c in combos) / len(combos)
        if w > 0:
            out[cls] = round(w, 4)
    return out


def range_grid(text: str) -> dict:
    d = ranges.parse(text) if text.strip() else {}
    return {"text": text, "width": ranges.width(d) if d else 0.0, "grid": class_grid(d)}


def chart_files() -> dict[str, list[str]]:
    out = {}
    for name in charts.CHART_FILES:
        if (charts.charts_dir() / name).exists():
            out[name] = list(charts.chart_entries(name))
    return out


def chart(name: str, key: str) -> dict:
    if name not in charts.CHART_FILES or not (charts.charts_dir() / name).exists():
        raise ChartNotFound(f"no chart file {name!r}")
    entries = charts.chart_entries(name)
    key = " ".join(key.split())
    if key not in entries:
        raise ChartNotFound(f"no key {key!r} in {name}")
    return {"file": name, "key": key, **range_grid(entries[key])}


# ------------------------------------------------------------------ PokerCoaching spot
# Chart action names per situation kind (codes as pn.train.preflop.PC_STEPS).
PC_LABELS = {
    "rfi": {"R": "Raise", "C": "Limp", "F": "Fold"},
    "vs_open": {"R": "3-bet", "C": "Call", "F": "Fold"},
    "vs_3bet": {"R": "4-bet", "C": "Call", "F": "Fold"},
    "limp_vs_raise": {"R": "3-bet", "C": "Call", "F": "Fold"},
}  # fmt: skip


def pc_spot(table: str, situation: str, position: str, other: str | None = None) -> dict:
    """A PokerCoaching chart spot in the `gtow_spot` shape (cells: class -> code -> share),
    plus `labels` (code -> chart action name) and `source = "pokercoaching"`."""
    found = pre.pc_class_dists(table, situation, position, other or None)
    if found is None:
        raise ChartNotFound(f"no PokerCoaching chart for {situation} {position} vs {other}")
    cells, key = found
    totals = dict.fromkeys(ACTION_CODES, 0.0)
    for cls, dist in cells.items():
        for a, v in dist.items():
            totals[a] += ranges.class_n_combos(cls) * v
    summary = {a: round(v / ranges.N_COMBOS, 4) for a, v in totals.items() if v > 0}
    labels = PC_LABELS[situation]
    return {"key": key, "depth": 100, "kind": situation,
            "positions": [p for p in (other, position) if p], "actor": position,
            "actions": [labels[c] for c in ("R", "C", "F") if summary.get(c)], "labels": labels,
            "raise_to_bb": None, "source": "pokercoaching",
            "cells": {cls: {a: round(v, 4) for a, v in d.items()} for cls, d in cells.items()},
            "summary": summary}  # fmt: skip


# ------------------------------------------------------------------ GTO Wizard mixed freqs
@lru_cache(maxsize=4)
def _freqs(path: str) -> dict:
    return gtowizard.load_freqs(Path(path))


def freqs_path() -> Path:
    return gtowizard.freqs_path()


def gtow_spots(path: Path | None = None) -> list[dict]:
    p = path or freqs_path()
    if not p.exists():
        return []
    out = []
    for key, spot in _freqs(str(p)).items():
        depth, kind, *positions = key.split("|")
        out.append({"key": key, "depth": int(depth), "kind": kind,
                    "kind_label": KIND_LABELS.get(kind, kind), "positions": positions,
                    "actor": spot.get("actor"), "actions": spot.get("actions", [])})  # fmt: skip
    return out


def _shares(cell: dict) -> dict[str, float]:
    known = {k: float(v) for k, v in cell.items() if k in ACTION_CODES}
    tot = sum(known.values())
    return {k: round(v / tot, 4) for k, v in known.items() if v > 0} if tot > 0 else {}


def gtow_spot(key: str, path: Path | None = None) -> dict:
    p = path or freqs_path()
    if not p.exists():
        raise ChartNotFound(f"missing {p}")
    data = _freqs(str(p))
    if key not in data:
        raise ChartNotFound(f"no GTO Wizard spot {key!r}")
    spot = data[key]
    cells = {cls: _shares(cell) for cls, cell in spot["cells"].items()}
    totals = dict.fromkeys(ACTION_CODES, 0.0)
    n = 0.0
    for cls, sh in cells.items():
        w = ranges.class_n_combos(cls)
        n += w
        for a, v in sh.items():
            totals[a] += w * v
    summary = {a: round(v / n, 4) for a, v in totals.items() if v > 0} if n else {}
    depth, kind, *positions = key.split("|")
    return {"key": key, "depth": int(depth), "kind": kind, "positions": positions,
            "actor": spot.get("actor"), "actions": spot.get("actions", []),
            "raise_to_bb": spot.get("raise_to_bb"), "cells": cells, "summary": summary}  # fmt: skip
