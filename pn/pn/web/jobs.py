"""Solve jobs started from the Strategy UI (contracts/trainer.md "Root solves and subgames",
contracts/solve_service.md "Plan 7"): one at a time in a background thread, with live progress
(iterations, exploitability curve) and cancel through the backend's stop hook."""

from __future__ import annotations

import inspect
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from pn.solve import SolveCache

log = logging.getLogger(__name__)


class JobNotFound(LookupError):
    """Unknown job id."""


class JobFinished(Exception):
    """cancel() on a job that is already done or failed."""


# ---- progress / cancel hooks (contracts/solve_service.md "Plan 7")
ProgressFn = Callable[[dict], None]


@dataclass(frozen=True)
class SolveHooks:
    """Passed to SolveCache.solve (and from there to the backend runner) when it accepts them:
    `progress(dict)` gets {"iterations": int, "exploitability_pct_pot": float | None,
    "elapsed_sec": float} per printed solver iteration; `should_stop()` True kills the solver
    (backend.Preempted) and nothing is cached."""

    progress: ProgressFn | None = None
    should_stop: Callable[[], bool] | None = None


def _solve_kwargs(cache: SolveCache, hooks: SolveHooks | None) -> dict:
    """The hooks SolveCache.solve accepts: until the backend wires them (Plan 7, stream T1) a
    cancelled job simply discards its result when the solve returns."""
    if hooks is None:
        return {}
    try:
        params = inspect.signature(cache.solve).parameters
    except (TypeError, ValueError):
        return {}
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return {k: getattr(hooks, k) for k in ("progress", "should_stop")
            if getattr(hooks, k) is not None and (var_kw or k in params)}  # fmt: skip


def cache_solve(cache: SolveCache, spot: dict, backend: str, hooks: SolveHooks | None) -> dict:
    """Interactive solve: priority over the library fill, hooks forwarded when supported."""
    return cache.solve(spot, backend, priority=True, **_solve_kwargs(cache, hooks))


# ------------------------------------------------------------------ async solve jobs

CURVE_POINTS = 240  # progress samples kept per job (the UI sparkline)


@dataclass(frozen=True)
class Job:
    job_id: str
    status: str = "queued"  # queued | running | done | error | cancelled
    name: str | None = None
    error: str | None = None  # "too_large" or the failure message
    estimate_bytes: int | None = None
    limit_bytes: int | None = None
    started: float | None = None
    finished: float | None = None
    iterations: int | None = None
    exploitability_pct_pot: float | None = None
    curve: tuple[tuple[float, int, float | None], ...] = ()  # (solver elapsed, iterations, expl)

    def view(self) -> dict:
        end = self.finished or time.monotonic()
        out = {"job_id": self.job_id, "status": self.status,
               "elapsed_sec": round(end - self.started, 2) if self.started else 0.0,
               "iterations": self.iterations, "exploitability_pct_pot": self.exploitability_pct_pot,
               "curve": [list(p) for p in self.curve]}  # fmt: skip
        for k in ("name", "error", "estimate_bytes", "limit_bytes"):
            if getattr(self, k) is not None:
                out[k] = getattr(self, k)
        return out


def _progress_point(d: dict) -> tuple[float, int, float | None]:
    """Validated progress callback payload -> curve point (ValueError on garbage)."""
    it = int(d["iterations"])
    expl = d.get("exploitability_pct_pot")
    return (round(float(d.get("elapsed_sec") or 0.0), 2), it,
            None if expl is None else round(float(expl), 4))  # fmt: skip


class Jobs:
    """Solves started from the UI, run one at a time in a background thread. Each job gets a
    SolveHooks: progress updates its iterations / exploitability / curve; cancel() sets its stop
    flag, and a cancelled job's result (if the solver still returns one) is discarded."""

    MAX_JOBS = 64

    def __init__(self) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._stop: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ui-solve")

    def _set(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            while len(self._jobs) > self.MAX_JOBS:
                old, _ = self._jobs.popitem(last=False)
                self._stop.pop(old, None)

    def _update(self, job_id: str, **changes: object) -> None:
        """Apply `changes` unless the job was cancelled (its final state is kept)."""
        with self._lock:
            cur = self._jobs.get(job_id)
            if cur is None or cur.status == "cancelled":
                return
            self._jobs[job_id] = replace(cur, **changes)  # type: ignore[arg-type]

    def done(self, name: str) -> str:
        """A job that finished immediately (e.g. a library root)."""
        now = time.monotonic()
        job = Job(uuid.uuid4().hex[:12], "done", name=name, started=now, finished=now)
        self._set(job)
        return job.job_id

    def _progress(self, job_id: str) -> ProgressFn:
        def report(d: dict) -> None:
            try:
                pt = _progress_point(d)
            except (KeyError, TypeError, ValueError) as e:
                log.warning("job %s: bad progress payload %r (%s)", job_id, d, e)
                return
            with self._lock:
                cur = self._jobs.get(job_id)
                if cur is None or cur.status != "running":
                    return
                curve = (*cur.curve, pt)[-CURVE_POINTS:]
                self._jobs[job_id] = replace(cur, iterations=pt[1], exploitability_pct_pot=(
                    pt[2] if pt[2] is not None else cur.exploitability_pct_pot), curve=curve)  # fmt: skip

        return report

    def submit(self, fn: Callable[[SolveHooks], str]) -> str:
        job = Job(uuid.uuid4().hex[:12])
        stop = threading.Event()
        with self._lock:
            self._stop[job.job_id] = stop
        self._set(job)
        jid = job.job_id

        def run() -> None:
            if stop.is_set():
                return
            self._update(jid, status="running", started=time.monotonic())
            hooks = SolveHooks(progress=self._progress(jid), should_stop=stop.is_set)
            try:
                self._update(jid, status="done", name=fn(hooks), finished=time.monotonic())
            except Exception as e:  # a worker thread must always end its job
                fields = e.job_fields() if hasattr(e, "job_fields") else {"error": str(e) or type(e).__name__}
                if not isinstance(e, ValueError | LookupError | RuntimeError | OSError):
                    log.exception("solve job %s failed", jid)
                self._update(jid, status="error", finished=time.monotonic(), **fields)

        self._pool.submit(run)
        return jid

    def cancel(self, job_id: str) -> dict:
        """Stop a queued or running job (status `cancelled` at once). JobFinished for a job that
        already ended; JobNotFound for an unknown id."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"no solve job {job_id!r}")
            if job.status in ("done", "error"):
                raise JobFinished(f"job {job_id} already {job.status}")
            stop = self._stop.get(job_id)
            if stop is not None:
                stop.set()
            if job.status != "cancelled":
                now = time.monotonic()
                self._jobs[job_id] = replace(job, status="cancelled", finished=now,
                                             started=job.started or now)  # fmt: skip
            return self._jobs[job_id].view()

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"no solve job {job_id!r}")
        return job.view()
