"""Solve cache: data/solves/cache/<spot_key>.json.gz or plain .json (pn.solve.docstore) + .toml,
a 4-document in-memory LRU, and a process-wide lock so only one solver subprocess runs at a
time (8 GB machine), extended across processes by pn.solve.lock (an flock under the cache root).
Waiting `priority` solves (the trainer's) go before waiting ordinary ones (library / review
jobs) in this and other processes. A cached 2-player postflop document solved by any backend
other than `texas` (mwsolver, hu, dcfr) is stale once TexasSolver is available (Plan 7): it is
re-solved under the same key. `progress` / `should_stop` are forwarded to the runner
(pn.solve.backend.run_backend)."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pn import ranges
from pn.adapter.validate import to_toml
from pn.profile.paths import repo_root
from pn.solve import docstore
from pn.solve.backend import run_backend, texas_available
from pn.solve.lock import file_lock

MEMORY_DOCS = 4
CURRENT_BACKEND = "texas"
log = logging.getLogger(__name__)


def is_stale(spot: Mapping[str, Any], backend: str | None, doc: Mapping[str, Any]) -> bool:
    """A cached 2-player postflop document whose meta.backend is not `texas` (mwsolver, hu,
    dcfr, old fallback documents) while TexasSolver is available. Preflop and multiway
    documents are never stale: no backend solves them any more, so a cached one is served.
    `backend` is kept for callers; `texas` is the only one."""
    del backend
    if spot["game"]["street"] == "preflop" or len(spot["players"]) != 2:
        return False
    meta = doc.get("meta") or {}
    return meta.get("backend") != CURRENT_BACKEND and texas_available()


class PriorityLock:
    """Mutex where waiting priority holders are served before waiting ordinary ones (a running
    solve is never interrupted)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held = False
        self._priority_waiting = 0

    @contextmanager
    def hold(self, priority: bool = False) -> Iterator[None]:
        with self._cond:
            self._priority_waiting += priority
            try:
                while self._held or (not priority and self._priority_waiting):
                    self._cond.wait()
            finally:
                self._priority_waiting -= priority
            self._held = True
        try:
            yield
        finally:
            with self._cond:
                self._held = False
                self._cond.notify_all()


SOLVE_LOCK = PriorityLock()  # one solver subprocess per process

Runner = Callable[..., tuple[dict, str]]  # (spot, backend, *, progress=, should_stop=)


def cache_dir() -> Path:
    return Path(os.environ.get("PN_SOLVE_CACHE", repo_root() / "data" / "solves" / "cache"))


def _sorted(d: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (_sorted(v) if isinstance(v, Mapping) else v) for k, v in sorted(d.items())}


def canonical(spot: Mapping[str, Any]) -> dict[str, Any]:
    """Spot with sorted keys and normalized range text. Lock entries keep their order (the
    engine applies the first matching pattern) and players keep action order."""
    players = []
    for p in spot["players"]:
        q = {k: v for k, v in sorted(p.items()) if k != "lock"}
        q["range"] = ranges.to_text(ranges.parse(p["range"]))
        if p.get("lock"):
            q["lock"] = {k: _sorted(v) for k, v in p["lock"].items()}
        players.append(q)
    return {"game": _sorted(spot["game"]), "players": players,
            "sizes": _sorted(spot.get("sizes") or {})}  # fmt: skip


def spot_key(spot: Mapping[str, Any]) -> str:
    """sha256 of the canonical TOML (ranges, board, stacks, sizes, locks, budget)."""
    return hashlib.sha256(to_toml(canonical(spot)).encode()).hexdigest()


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _accepted_hooks(runner: Callable, **hooks: Callable | None) -> dict[str, Callable]:
    """The given (non-None) hooks the runner's signature accepts (by name or **kwargs); a plain
    runner such as the test fakes gets none."""
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return {}
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return {k: v for k, v in hooks.items() if v is not None and (var_kw or k in params)}


class SolveCache:
    """Disk + memory cache of strategy documents keyed by `spot_key`. `runner(spot, backend)`
    returns (document, backend that ran); the default runs the solver binaries."""

    def __init__(self, root: str | Path | None = None, runner: Runner = run_backend,
                 memory_docs: int = MEMORY_DOCS) -> None:  # fmt: skip
        self.root = Path(root) if root is not None else cache_dir()
        self.runner = runner
        self.memory_docs = memory_docs
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._mem_lock = threading.Lock()

    def path(self, key: str) -> Path:
        """The document file of `key`: the existing one (gz first), else where a solve writes it."""
        return docstore.doc_path(self.root, key) or docstore.write_path(self.root, key)

    def _remember(self, key: str, doc: dict) -> None:
        with self._mem_lock:
            self._mem[key] = doc
            self._mem.move_to_end(key)
            while len(self._mem) > self.memory_docs:
                self._mem.popitem(last=False)

    def get(self, key: str) -> dict | None:
        with self._mem_lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        doc = docstore.read(self.root, key)  # survives a compress swap; torn gz -> plain copy
        if doc is None:
            return None
        self._remember(key, doc)
        return doc

    def solve(self, spot: Mapping[str, Any], backend: str | None = None, *,
              priority: bool = False, background: bool = False,
              progress: Callable[[dict], None] | None = None,
              should_stop: Callable[[], bool] | None = None) -> dict:  # fmt: skip
        """Cached document for `spot`, solving it (one solve at a time, `priority` callers
        first, `background` callers yield to waiting ordinary ones) on a miss. The cache key
        ignores `backend`; `meta.backend` records which backend
        produced the document. A stale document (is_stale) counts as a miss and is replaced.
        `progress` and `should_stop` go to the runner as keyword arguments when given (a
        Preempted run writes nothing)."""
        key = spot_key(spot)
        doc = self.get(key)
        if doc is not None and not is_stale(spot, backend, doc):
            return doc
        with SOLVE_LOCK.hold(priority), file_lock(self.root, priority, background):
            doc = self.get(key)  # another thread may have solved it while we waited
            if doc is not None and not is_stale(spot, backend, doc):
                return doc
            if doc is not None:
                log.warning("stale backend %s for %s, resolving with texas",
                            (doc.get("meta") or {}).get("backend"), key[:12])  # fmt: skip
            hooks = _accepted_hooks(self.runner, progress=progress, should_stop=should_stop)
            doc, ran = self.runner(spot, backend, **hooks)
            doc.setdefault("meta", {}).setdefault("backend", ran)
            _write(self.root / f"{key}.toml", to_toml(spot, header=f"pn solve cache {key}"))
            docstore.dump(self.root, key, doc)
        self._remember(key, doc)
        return doc
