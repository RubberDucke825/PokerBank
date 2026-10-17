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
("finer"), so passes run coarse to fine (1 -> 0.5 -> 0.3) without ever downgrading a board,
unless its recorded key is no longer the board's key (`indexed`: the spot changed): then it is
solved again (or, --cached-only, its new document recorded) and the entry replaced.
"""

from __future__ import annotations

import heapq
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pn.ingest import db as ingest_db
from pn.solve import SolveCache
from pn.solve import flops as fl
from pn.solve.backend import Preempted, SolveError, run_backend
from pn.solve.lock import POLL_SEC, priority_waiting
from pn.solve.request import SOLVE_PRESETS, TABLE_SEATS
from pn.train import spots
from pn.train import textures as tx
from pn.train.engine import apply, next_street, start
from pn.train.flop_library import (
    LIBRARY_BACKEND,
    LIBRARY_PRESET,
    FlopIndex,
    Matchup,
    flop_spot,
    matchup_of,
)
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


def subset_flops(n: int) -> list[fl.Flop]:
    """`n` canonical flops (Plan 16: 184 per new library) stratified by texture (pn.train.textures)
    x high card. Slots go to the strata by highest averages on raw weight (at least one each,
    never more than a stratum's flops); within a stratum the flops, ordered by rank pattern
    (unpaired / pair / trips), low card, suit pattern and connectedness, are cut into that many
    contiguous chunks of near-equal weight, each represented by the weighted median flop of the
    chunk's heaviest rank pattern (keys review: a light trips flop must not carry a pair chunk).
    The chunk weights are then raked (`_rake`) to the full rank-pattern, low-card, suit-pattern
    and connected shares and rounded per stratum, so the weights sum to 22,100 and reproduce the
    texture and high-card distributions exactly. Ordered round-robin over the strata (heaviest
    first). Deterministic: no randomness, ties broken by order."""
    strata: dict[tuple[str, str], list[fl.Flop]] = {}
    for f in fl.canonical_flops():  # rank pattern order, high to low
        strata.setdefault(_stratum(f), []).append(f)
    if not len(strata) <= n <= fl.N_CANONICAL_FLOPS:
        raise ValueError(f"n must be in {len(strata)}..{fl.N_CANONICAL_FLOPS}")
    weight = {k: sum(f.weight for f in v) for k, v in strata.items()}
    keys = sorted(strata, key=lambda k: (-weight[k], k))
    alloc = dict.fromkeys(keys, 1)
    heap = [(-weight[k] / 2, i, k) for i, k in enumerate(keys) if len(strata[k]) > 1]
    heapq.heapify(heap)
    for _ in range(n - len(keys)):
        _, i, k = heapq.heappop(heap)
        alloc[k] += 1
        if alloc[k] < len(strata[k]):
            heapq.heappush(heap, (-weight[k] / (alloc[k] + 1), i, k))
    per = [[_representative(c) for c in _even_chunks(sorted(strata[k], key=_subset_order), alloc[k])]
           for k in keys]  # fmt: skip
    raked = iter(_rake([f for b in per for f in b]))
    per = [[replace(f, weight=next(raked)) for f in b] for b in per]
    return [b[r] for r in range(max(len(b) for b in per)) for b in per if r < len(b)]


def _stratum(f: fl.Flop) -> tuple[str, str]:
    return tx.texture(f.flop), f.flop[0]


def _pattern(f: fl.Flop) -> int:
    """0 unpaired, 1 paired, 2 trips."""
    return 3 - len({c[0] for c in fl.parse_flop(f.flop)})


def _low(f: fl.Flop) -> int:
    return min(c[0] for c in fl.parse_flop(f.flop))


def _suits(f: fl.Flop) -> int:
    return len({c[1] for c in fl.parse_flop(f.flop)})


def _connected(f: fl.Flop) -> bool:
    return fl.classify(fl.parse_flop(f.flop)).connectivity >= 2


def _subset_order(f: fl.Flop) -> tuple[int, int, int, int]:
    return _pattern(f), _low(f), _suits(f), -fl.classify(fl.parse_flop(f.flop)).connectivity


def _representative(chunk: list[fl.Flop]) -> fl.Flop:
    """The weighted median flop of the chunk's heaviest rank pattern, carrying the chunk's weight."""
    by: dict[int, int] = {}
    for f in chunk:
        by[_pattern(f)] = by.get(_pattern(f), 0) + f.weight
    top = max(by, key=lambda p: (by[p], -p))
    rep = _median([f for f in chunk if _pattern(f) == top])
    return fl.Flop(rep.flop, sum(f.weight for f in chunk), rep.bucket)


RAKE_MARGINS = (_pattern, _low, _suits, _connected, _stratum)  # the stratum last: kept exact
RAKE_ROUNDS = 200


def _rake(flops: list[fl.Flop]) -> list[int]:
    """Integer weights for `flops` (iterative proportional fitting from their chunk weights): each
    round scales them to the full share of every RAKE_MARGINS class in turn, then each stratum's
    weights are rounded (largest remainder) to sum to the stratum's raw weight exactly."""
    full = fl.canonical_flops()
    w = [float(f.weight) for f in flops]
    margins = []  # (class of each flop, full weight per class), computed once
    for key in RAKE_MARGINS:
        want: dict[Any, float] = {}
        for f in full:
            want[key(f)] = want.get(key(f), 0) + f.weight
        margins.append(([key(f) for f in flops], want))
    for _ in range(RAKE_ROUNDS):
        for cls, want in margins:
            have: dict[Any, float] = {}
            for c, x in zip(cls, w, strict=True):
                have[c] = have.get(c, 0) + x
            w = [x * want[c] / have[c] for c, x in zip(cls, w, strict=True)]
    out = [int(x) for x in w]
    groups: dict[tuple[str, str], list[int]] = {}
    for i, f in enumerate(flops):
        groups.setdefault(_stratum(f), []).append(i)
    raw = margins[-1][1]  # the stratum is the last margin
    for k, idx in groups.items():
        for i in sorted(idx, key=lambda i: (out[i] - w[i], i))[: int(raw[k]) - sum(out[i] for i in idx)]:
            out[i] += 1
    return out


def _median(flops: list[fl.Flop]) -> fl.Flop:
    """The flop holding the middle of the chunk's cumulative weight."""
    half, acc = sum(f.weight for f in flops) / 2, 0
    for f in flops:
        acc += f.weight
        if acc >= half:
            return f
    return flops[-1]


def _even_chunks(flops: list[fl.Flop], n: int) -> list[list[fl.Flop]]:
    """Exactly `n` non-empty contiguous chunks (1 <= n <= len) of near-equal total weight."""
    total = sum(f.weight for f in flops)
    out: list[list[fl.Flop]] = []
    start = acc = 0
    for k in range(1, n):
        end, acc = start + 1, acc + flops[start].weight
        while end < len(flops) - (n - k) and acc + flops[end].weight / 2 <= total * k / n:
            acc += flops[end].weight
            end += 1
        out.append(flops[start:end])
        start = end
    return [*out, flops[start:]]


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


# ------------------------------------------------------------------ libraries (Plan 16)

PROBE_BOARD = ("Ks", "7d", "2c")  # any board: a flop key hashes ranges, stacks, pot and sizes too
SB, BB = 50, 100  # cents, as pn.train.session deals
LIB_RE = re.compile(r"^(\d+)x(\d+)$")


def parse_depth_accuracy(items: Sequence[str]) -> dict[int, float]:
    """`["200=2"]` -> {200: 2.0}: accuracy in % of pot per depth; ValueError on a bad item."""
    from pn.train.model import DEPTHS

    out: dict[int, float] = {}
    for x in items:
        d, sep, a = x.partition("=")
        try:
            depth, acc = int(d), float(a)
        except ValueError:
            raise ValueError(f"bad depth accuracy {x!r}: want DEPTH=PCT, e.g. 200=2") from None
        if not sep or depth not in DEPTHS or acc <= 0:
            raise ValueError(f"bad depth accuracy {x!r}: depth in {list(DEPTHS)}, accuracy > 0")
        out[depth] = acc
    return out


def parse_lib(text: str) -> tuple[int, int]:
    """`8x200` -> (8, 200): table size x depth in bb; ValueError unless the trainer deals it."""
    from pn.train.model import DEPTHS

    m = LIB_RE.match(text.strip())
    if m is None or int(m[1]) not in TABLE_SEATS or int(m[2]) not in DEPTHS:
        raise ValueError(f"bad library {text!r}: want TABLExDEPTH with table in "
                         f"{sorted(TABLE_SEATS)} and depth in {list(DEPTHS)}, e.g. 8x200")  # fmt: skip
    return int(m[1]), int(m[2])


def lib_matchups(table: int, depth: int) -> list[Matchup]:
    from pn.train.flop_library import matchups

    return matchups(TABLE_SEATS[table], depth, f"{table}max", sb=SB, bb=BB)


def aliased(ms: Sequence[Matchup], ref: Sequence[Matchup]) -> set[str]:
    """Matchup ids of `ms` whose flop spots equal one of `ref`'s (same key on a probe board,
    hence the same document on every board): an 8-max line where UTG/UTG1 folded is the 6-max
    line of that depth (Plan 16 P0), so its boards come from the 6-max library, not a solve."""
    ref_keys = {flop_spot(m, PROBE_BOARD)[2] for m in ref}
    return {m.mid for m in ms if flop_spot(m, PROBE_BOARD)[2] in ref_keys}


def new_lines(table: int, depth: int, ms: Sequence[Matchup]) -> tuple[list[Matchup], int, int]:
    """(matchups to solve, aliased count, limped count) under the Plan 16 budget decision: no
    limped pots, no 8-max line that aliases the 6-max one of the same depth."""
    alias = aliased(ms, lib_matchups(6, depth)) if table != 6 else set()
    keep = [m for m in ms if m.mid not in alias and m.pot != "limped"]
    return keep, len(alias), sum(1 for m in ms if m.mid not in alias and m.pot == "limped")


def missing_inputs(libs: Sequence[tuple[int, int]]) -> list[str]:
    """Inputs every flop key of these libraries hashes (Plan 11 lesson: a machine without one
    plans other keys and solves boards nobody looks up): GTO Wizard frequencies at each depth,
    the preflop chart files, the library preset. Empty when all are present."""
    from pn.profile import charts
    from pn.train import preflop as pre

    out: list[str] = []
    freqs = pre._freqs()  # the loader the preflop engine sizes raises from
    if not freqs:
        out.append("GTO Wizard frequencies (data/gtowizard)")
    out += [f"GTO Wizard {d}bb frequencies" for d in sorted({d for _, d in libs})
            if freqs and not any(k.startswith(f"{d}|") for k in freqs)]  # fmt: skip
    files = {charts.PC_FILE, "common.txt", *(f"gtowizard_{d}bb.txt" for _, d in libs),
             *(f"{t}max.txt" for t, _ in libs)}
    out += [f"chart file config/preflop_charts/{f}" for f in sorted(files)
            if not (charts.charts_dir() / f).exists()]  # fmt: skip
    if LIBRARY_PRESET not in SOLVE_PRESETS:
        out.append(f"solve preset {LIBRARY_PRESET!r}")
    return out


def trainer_key(m: Matchup, board: tuple[str, ...], accuracy: float | None = None) -> tuple[str, str]:
    """(matchup id, flop key) the trainer computes for a hand that plays `m`'s preflop line:
    a fresh hand at the trainer's blinds and table config, the line's actions applied by the
    engine, the flop dealt, then pn.train.session's lookup (matchup_of with Config.table_kind)."""
    from pn.train.model import Config

    cfg = Config(table=len(m.state.positions), depth=m.depth)
    st = start(TABLE_SEATS[cfg.table], cfg.depth * BB, SB, BB)
    for a in m.state.history:
        if a.street == "preflop":
            st = apply(st, a.kind, a.to)
    tm = matchup_of(next_street(st, board), cfg.table_kind, cfg.depth)
    if tm is None:
        raise ValueError(f"{m.label}: the trainer's hand is not heads-up at the flop")
    return tm.mid, flop_spot(tm, board, accuracy)[2]


# ------------------------------------------------------------------ running


@dataclass(frozen=True)
class FillResult:
    label: str
    flop: str
    key: str
    status: str  # solved | cached | finer | failed | missing | aliased (an alias entry: never solved)
    detail: str = ""


DONE_STATES = ("solved", "cached", "finer")


def preemptible_runner(root: Path) -> Callable[[Mapping[str, Any], str | None], tuple[dict, str]]:
    """SolveCache runner whose subprocess is killed (Preempted) when a priority solve waits."""

    def run(spot: Mapping[str, Any], backend: str | None) -> tuple[dict, str]:
        return run_backend(spot, backend, should_stop=lambda: priority_waiting(root))

    return run


def running_fill(root: Path) -> int | None:
    """The pid of the live fill (or --cached-only walk) on this cache root, None when none runs.
    Another index writer must wait for it: the fill rewrites the whole index when it ends."""
    try:
        pid = int((Path(root) / PID_NAME).read_text().strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    except PermissionError:  # alive, another user's
        return pid
    return pid


@contextmanager
def single_fill(root: Path) -> Iterator[None]:
    """At most one fill per cache root (pid file); RuntimeError when another one runs."""
    root.mkdir(parents=True, exist_ok=True)
    pid_file = root / PID_NAME
    if (other := running_fill(root)) is not None:
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
         cached_only: bool = False, subset: int | None = None,
         depth_accuracy: Mapping[int, float] | None = None, full: bool = False) -> list[FillResult]:  # fmt: skip
    """Solve the items in order to `accuracy` % of pot (None = the preset's). A Preempted solve
    waits for the priority solves (`wait`, default: until none is waiting) and is retried; other
    failures are reported and skipped; a board already indexed at a finer or equal accuracy under
    its current key (`indexed`) is reported "finer" and left as it is. `cached_only` records the
    documents already in the cache (pulled from other machines) and reports the rest "missing"
    without solving, writing the index once at the end. An alias entry's matchup
    (pn.train.library_alias) is reported "aliased" and never solved or recorded. `subset` is recorded on each matchup entry the run writes;
    `full` (a `--flops all` run, Plan 17) marks the entries it records a board on `flops: "all"`
    instead (FlopIndex.record; a weight reset alone does not).
    `depth_accuracy` ({depth bb: % of pot}) replaces `accuracy` for the matchups of that depth
    (Plan 16: 200bb solves to 2% pot, 100bb to 1%)."""
    da = dict(depth_accuracy or {})
    if cached_only:
        with index.batch():
            return _fill(items, cache, index, backend, accuracy, wait, on_result, True, subset, da, full)
    return _fill(items, cache, index, backend, accuracy, wait, on_result, False, subset, da, full)


def _fill(items: Sequence[FillItem], cache: SolveCache, index: FlopIndex, backend: str,
          accuracy: float | None, wait: Callable[[], None] | None,
          on_result: Callable[[FillResult], None] | None, cached_only: bool,
          subset: int | None = None,
          depth_accuracy: Mapping[int, float] | None = None, full: bool = False) -> list[FillResult]:  # fmt: skip
    wait = wait or (lambda: _wait_idle(cache.root))
    out: list[FillResult] = []
    for it in items:
        if (to := index.alias_of(it.matchup.mid)) is not None:  # its boards are the target's
            res = FillResult(it.matchup.label, it.flop.flop, "", "aliased", f"boards of {to}")
            out.append(res)
            if on_result:
                on_result(res)
            continue
        board = tuple(fl.flop_cards(it.flop.flop))
        target = (depth_accuracy or {}).get(it.matchup.depth, accuracy)
        have = indexed(index, it.matchup, it.flop.flop, target)
        if have is not None:
            if cached_only:  # a stratified fill recorded the chunk's weight; the wide plan carries the raw one
                entry = index.boards(it.matchup.mid).get(it.flop.flop) or {}
                if entry.get("weight") != it.flop.weight and entry.get("key"):
                    # a weight reset is not an extension: never marks the entry full (review)
                    index.record(it.matchup, it.flop.flop, entry["key"], it.flop.weight, have, subset)
            res = FillResult(it.matchup.label, it.flop.flop, "", "finer", f"at {have:g}% pot")
            out.append(res)
            if on_result:
                on_result(res)
            continue
        _, spot, key = flop_spot(it.matchup, board, target)
        acc = float(spot["game"]["target_exploitability"])  # recorded even on the preset's pass
        cached = cache.get(key)
        res = None
        if cached is not None and (cached.get("meta") or {}).get("backend") == backend:
            index.record(it.matchup, it.flop.flop, key, it.flop.weight, acc, subset, full)
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
                index.record(it.matchup, it.flop.flop, key, it.flop.weight, acc, subset, full)
                res = FillResult(it.matchup.label, it.flop.flop, key, "solved")
        out.append(res)
        if on_result:
            on_result(res)
    return out


def indexed(index: FlopIndex, m: Matchup, flop: str, target: float | None) -> float | None:
    """The board's indexed accuracy when it is at or finer than `target` and its recorded key is
    still the board's key at that accuracy, else None. A stale key (the spot changed, e.g. new
    seat ranges) is re-solved like a missing board; its entry stays until the new key is recorded."""
    entry = index.boards(m.mid).get(flop) or {}
    have = None if entry.get("accuracy") is None else float(entry["accuracy"])
    if target is None or have is None or have > target:
        return None
    return have if entry.get("key") == flop_spot(m, tuple(fl.flop_cards(flop)), have)[2] else None


def _wait_idle(root: Path) -> None:
    while priority_waiting(root):
        time.sleep(POLL_SEC)


def accuracy_counts(index: FlopIndex) -> dict[str, int]:
    """Indexed boards per recorded accuracy ("?" when the entry has none)."""
    out: dict[str, int] = {}
    for m in index.load()["matchups"].values():
        for e in m.get("boards", {}).values():  # alias entries hold none
            k = "?" if e.get("accuracy") is None else f"{float(e['accuracy']):g}"
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def coverage(ms: Sequence[Matchup], index: FlopIndex, burn: Mapping[str, float],
             planned: int) -> list[dict[str, Any]]:  # fmt: skip
    """Per matchup: label, solved boards, planned boards, burn; sorted by burn."""
    rows = [{"label": m.label, "mid": m.mid, "solved": len(index.boards(m.mid)),
             "planned": planned, "burn": round(burn.get(m.mid, 0.0), 4)} for m in ms]  # fmt: skip
    return sorted(rows, key=lambda r: (-r["burn"], r["label"]))
