"""Library fill worker for the trainer's heads-up flops (`pn library fill`, contracts/trainer.md
2026-10-07). Solves pn.train.flop_library matchups x texture-stratified flops with TexasSolver
(backend "texas") into the SolveCache and records each solved board in the flop index; a cached
document of another backend counts as missing (Plan 7).

Order: matchups by expected burn = 100 x P(the heads-up flop per hand) x mean blunder rate per
decision of its pot type (pn.train.leaks; expected blunders, not EV), then greedily burn / (1 +
boards already planned), so the most common, most error-prone spots get flops first and every
listed matchup gets some.
Resumable: boards already cached are recorded and skipped. One solve at a time through the
SolveCache lock; the run is preempted (and retried) whenever a trainer solve is waiting.
Accuracy passes (Plan 10): `fill(..., accuracy=x)` solves to x % of pot under its own cache key
and records it; a board the index already holds at a finer or equal accuracy is left alone
("finer"), so passes run coarse to fine (1 -> 0.5 -> 0.3) without ever downgrading a board.
"""

from __future__ import annotations

import heapq
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pn.ingest import db as ingest_db
from pn.solve import SolveCache
from pn.solve import flops as fl
from pn.solve.backend import Preempted, SolveError, run_backend
from pn.solve.lock import POLL_SEC, priority_waiting
from pn.train import spots
from pn.train.flop_library import LIBRARY_BACKEND, FlopIndex, Matchup, flop_spot
from pn.train.leaks import Leaks

# Matchup frequency without a library root (relative): pot type x blind / button involvement.
POT_PRIOR = {"srp": 1.0, "3bp": 0.3, "limped": 0.2, "4bp+": 0.05}
LINE_PRIOR = 0.2  # limp-isolation / cold-called 3-bet lines, relative to the standard line
PRIOR_BLUNDER_RATE = 0.2  # blunders per decision without trainer history (pn.train.leaks)
PID_NAME = ".library_fill.pid"
WINDOW = 1000  # trainer hands the leak blunder rates come from
log = logging.getLogger(__name__)


# ------------------------------------------------------------------ flops


def stratified_flops(n: int) -> list[fl.Flop]:
    """`n` canonical flops spread over the texture buckets in proportion to their raw weight
    (largest remainder, at least one per bucket while n allows), each the heaviest flop of an
    equal-weight chunk carrying the chunk's weight; ordered round-robin over the buckets
    (heaviest bucket first) so any prefix stays texture-stratified."""
    if n < 1:
        raise ValueError("n must be >= 1")
    groups = {k: v for k, v in fl.flops_by_bucket().items() if v}
    base = 1 if n >= len(groups) else 0
    rest = n - base * len(groups)
    total = sum(f.weight for v in groups.values() for f in v)
    share = {k: rest * sum(f.weight for f in v) / total for k, v in groups.items()}
    alloc = {k: base + int(s) for k, s in share.items()}
    for k in sorted(share, key=lambda k: share[k] - int(share[k]), reverse=True)[: n - sum(alloc.values())]:
        alloc[k] += 1
    per_bucket: list[list[fl.Flop]] = []
    for k, v in sorted(groups.items(), key=lambda kv: -sum(f.weight for f in kv[1])):
        m = min(alloc[k], len(v))
        if m <= 0:
            continue
        chunks = _chunks(v, m)
        per_bucket.append([fl.Flop(max(c, key=lambda f: f.weight).flop, sum(f.weight for f in c), k)
                           for c in chunks])  # fmt: skip
    out: list[fl.Flop] = []
    for r in range(max((len(b) for b in per_bucket), default=0)):
        out += [b[r] for b in per_bucket if r < len(b)]
    return out


def all_flops() -> list[fl.Flop]:
    """Every canonical flop (1,755), heaviest first (most raw flops map to it), with its raw
    weight, so a prefix covers the most-dealt flops and the stratified picks are a subset."""
    return sorted(fl.canonical_flops(), key=lambda f: -f.weight)


def _chunks(flops: list[fl.Flop], n: int) -> list[list[fl.Flop]]:
    total = sum(f.weight for f in flops)
    chunks: list[list[fl.Flop]] = [[] for _ in range(n)]
    acc = 0
    for f in flops:
        chunks[min(n - 1, int(acc * n / total))].append(f)
        acc += f.weight
    return [c for c in chunks if c]


# ------------------------------------------------------------------ ordering


def _prior_freq(m: Matchup) -> float:
    """Relative frequency guess without a library root: pot type x blind / button involvement,
    lowered for limp-isolation (a call before the first raise) and cold-called 3-bets."""
    acts = [(m.state.positions[a.seat], a.kind) for a in m.state.history
            if a.street == "preflop" and a.kind != "fold"]  # fmt: skip
    raisers = [p for p, k in acts if k == "raise"]
    first_raise = next((i for i, (_, k) in enumerate(acts) if k == "raise"), len(acts))
    iso = any(k == "call" for _, k in acts[:first_raise]) and bool(raisers)
    cold = len(raisers) >= 2 and any(k == "call" and p not in raisers for p, k in acts)
    blind = 2.0 if "BB" in m.positions else 1.0
    button = 1.5 if {"BTN", "SB"} & set(m.positions) else 1.0
    line = (LINE_PRIOR if iso else 1.0) * (LINE_PRIOR if cold else 1.0)
    return POT_PRIOR.get(m.pot, 0.05) * blind * button * line


def burns(ms: Sequence[Matchup], freq: Mapping[str, float],
          leak_rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:  # fmt: skip
    """Expected blunders per 100 hands of each matchup ("burn"). `freq` = FreqMap.matchups
    (library root; empty -> pot-type priors); rate = mean blunder_rate of the pot type's postflop
    leak rows (PRIOR_BLUNDER_RATE without any)."""
    rate: dict[str, list[float]] = {}
    for r in leak_rows:
        if str(r["spot"]).startswith("post|"):
            rate.setdefault(spots.parse(r["spot"])["pot"], []).append(float(r["blunder_rate"]))
    out = {}
    for m in ms:
        f = freq.get(m.line, 0.0) if freq else _prior_freq(m)
        rs = rate.get(m.pot)
        out[m.mid] = 100.0 * f * (sum(rs) / len(rs) if rs else PRIOR_BLUNDER_RATE)
    return out


def load_burns(ms: Sequence[Matchup], table: int, depth: int, db: str | Path | None,
               leaks: Leaks | None = None) -> dict[str, float]:  # fmt: skip
    """burns() from the leak model: matchup frequencies from the library root's heads-up
    leaves, blunder rates from the trainer history in `db` (when it exists)."""
    lk = leaks or Leaks(db)
    fm, _ = lk.solver_map(table, depth)
    rows: list[dict[str, Any]] = []
    try:
        if db is not None and Path(db).exists():
            with ingest_db.opened(db) as conn:
                rows = lk.rows(conn, table, depth, WINDOW)
        else:
            rows = lk.rows(None, table, depth, WINDOW)
    except (sqlite3.Error, OSError, ValueError, KeyError) as e:
        log.warning("leak blunder rates unavailable, using the prior: %s", e)
    return burns(ms, fm.matchups, rows)


@dataclass(frozen=True)
class FillItem:
    matchup: Matchup
    flop: fl.Flop
    burn: float


def plan(ms: Sequence[Matchup], burn: Mapping[str, float], flops: Sequence[fl.Flop],
         configs: int | None = None) -> list[FillItem]:  # fmt: skip
    """Solve order: the top `configs` matchups by burn (all with burn > 0 when None), each over
    `flops` in order, interleaved greedily by burn / (1 + boards planned so far)."""
    ranked = [m for m in sorted(ms, key=lambda m: -burn.get(m.mid, 0.0)) if burn.get(m.mid, 0.0) > 0]
    chosen = ranked[:configs] if configs is not None else ranked
    heap = [(-burn[m.mid], k, 0) for k, m in enumerate(chosen)]
    heapq.heapify(heap)
    out: list[FillItem] = []
    while heap:
        _, k, j = heapq.heappop(heap)
        m = chosen[k]
        out.append(FillItem(m, flops[j], burn[m.mid]))
        if j + 1 < len(flops):
            heapq.heappush(heap, (-burn[m.mid] / (j + 2), k, j + 1))
    return out


# ------------------------------------------------------------------ running


@dataclass(frozen=True)
class FillResult:
    label: str
    flop: str
    key: str
    status: str  # solved | cached | finer | failed | missing
    detail: str = ""


DONE_STATES = ("solved", "cached", "finer")


def preemptible_runner(root: Path) -> Callable[[Mapping[str, Any], str | None], tuple[dict, str]]:
    """SolveCache runner whose subprocess is killed (Preempted) when a priority solve waits."""

    def run(spot: Mapping[str, Any], backend: str | None) -> tuple[dict, str]:
        return run_backend(spot, backend, should_stop=lambda: priority_waiting(root))

    return run


@contextmanager
def single_fill(root: Path) -> Iterator[None]:
    """At most one fill per cache root (pid file); RuntimeError when another one runs."""
    root.mkdir(parents=True, exist_ok=True)
    pid_file = root / PID_NAME
    if pid_file.exists():
        try:
            other = int(pid_file.read_text().strip())
            os.kill(other, 0)
        except (ValueError, ProcessLookupError):
            pass
        else:
            raise RuntimeError(f"a library fill is already running (pid {other})")
    pid_file.write_text(str(os.getpid()))
    try:
        yield
    finally:
        with suppress(FileNotFoundError):
            pid_file.unlink()


def fill(items: Sequence[FillItem], cache: SolveCache, index: FlopIndex, *,
         backend: str = LIBRARY_BACKEND, accuracy: float | None = None,
         wait: Callable[[], None] | None = None,
         on_result: Callable[[FillResult], None] | None = None,
         cached_only: bool = False) -> list[FillResult]:  # fmt: skip
    """Solve the items in order to `accuracy` % of pot (None = the preset's). A Preempted solve
    waits for the priority solves (`wait`, default: until none is waiting) and is retried; other
    failures are reported and skipped; a board already indexed at a finer or equal accuracy is
    reported "finer" and left as it is. `cached_only` records the documents already in the cache
    (pulled from other machines) and reports the rest "missing" without solving, writing the
    index once at the end."""
    if cached_only:
        with index.batch():
            return _fill(items, cache, index, backend, accuracy, wait, on_result, True)
    return _fill(items, cache, index, backend, accuracy, wait, on_result, False)


def _fill(items: Sequence[FillItem], cache: SolveCache, index: FlopIndex, backend: str,
          accuracy: float | None, wait: Callable[[], None] | None,
          on_result: Callable[[FillResult], None] | None, cached_only: bool) -> list[FillResult]:  # fmt: skip
    wait = wait or (lambda: _wait_idle(cache.root))
    out: list[FillResult] = []
    for it in items:
        board = tuple(fl.flop_cards(it.flop.flop))
        have = index.accuracy(it.matchup.mid, it.flop.flop)
        if accuracy is not None and have is not None and have <= accuracy:
            res = FillResult(it.matchup.label, it.flop.flop, "", "finer", f"at {have:g}% pot")
            out.append(res)
            if on_result:
                on_result(res)
            continue
        _, spot, key = flop_spot(it.matchup, board, accuracy)
        acc = float(spot["game"]["target_exploitability"])  # recorded even on the preset's pass
        cached = cache.get(key)
        res = None
        if cached is not None and (cached.get("meta") or {}).get("backend") == backend:
            index.record(it.matchup, it.flop.flop, key, it.flop.weight, acc)
            res = FillResult(it.matchup.label, it.flop.flop, key, "cached")
        elif cached_only:
            res = FillResult(it.matchup.label, it.flop.flop, key, "missing")
        while res is None:
            try:
                doc = cache.solve(spot, backend, background=True)
            except Preempted:
                wait()
                continue
            except SolveError as e:
                res = FillResult(it.matchup.label, it.flop.flop, key, "failed", str(e)[:300])
                break
            ran = (doc.get("meta") or {}).get("backend")
            if ran != backend:
                res = FillResult(it.matchup.label, it.flop.flop, key, "failed", f"backend {ran}")
            else:
                index.record(it.matchup, it.flop.flop, key, it.flop.weight, acc)
                res = FillResult(it.matchup.label, it.flop.flop, key, "solved")
        out.append(res)
        if on_result:
            on_result(res)
    return out


def _wait_idle(root: Path) -> None:
    while priority_waiting(root):
        time.sleep(POLL_SEC)


def accuracy_counts(index: FlopIndex) -> dict[str, int]:
    """Indexed boards per recorded accuracy ("?" when the entry has none)."""
    out: dict[str, int] = {}
    for m in index.load()["matchups"].values():
        for e in m["boards"].values():
            k = "?" if e.get("accuracy") is None else f"{float(e['accuracy']):g}"
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def coverage(ms: Sequence[Matchup], index: FlopIndex, burn: Mapping[str, float],
             planned: int) -> list[dict[str, Any]]:  # fmt: skip
    """Per matchup: label, solved boards, planned boards, burn; sorted by burn."""
    rows = [{"label": m.label, "mid": m.mid, "solved": len(index.boards(m.mid)),
             "planned": planned, "burn": round(burn.get(m.mid, 0.0), 4)} for m in ms]  # fmt: skip
    return sorted(rows, key=lambda r: (-r["burn"], r["label"]))
