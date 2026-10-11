"""Solver backend selection (contracts/solve_service.md "Backend selection", Plan 7).

TexasSolver (`texas`, contracts/texas_backend.md) is the only engine: heads-up flop/turn/river
spots. Preflop spots raise SolveError(PREFLOP_UNSOLVABLE), 3+ players
SolveError(MULTIWAY_UNSOLVABLE); node locks are rejected earlier by pn.adapter.validate. There
is no fallback: a missing or failing binary raises SolveError.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

BACKENDS = ("texas",)
MULTIWAY_UNSOLVABLE = "multiway_unsolvable"
PREFLOP_UNSOLVABLE = "preflop_unsolvable"
EMITTED_STREETS = 1  # later streets become `leaf` terminals that pn.solve.subgame lifts


class SolveError(RuntimeError):
    """A backend failed or refused the spot. `code` is the process exit code (None: no run)."""

    def __init__(self, msg: str, code: int | None = None) -> None:
        super().__init__(msg)
        self.code = code


class Preempted(SolveError):
    """A preemptible run was stopped because `should_stop()` turned true (a priority solve is
    waiting, pn.solve.lock, or the caller cancelled it)."""


def texas_cmd() -> list[str] | None:
    from pn.solve.texas import texas_cmd as cmd

    return cmd()


def texas_available() -> bool:
    return texas_cmd() is not None


def is_multiway_postflop(spot: Mapping[str, Any]) -> bool:
    return spot["game"]["street"] != "preflop" and len(spot["players"]) > 2


def choose_backend(spot: Mapping[str, Any]) -> str:
    """"texas" for heads-up flop/turn/river spots; SolveError otherwise."""
    if spot["game"]["street"] == "preflop":
        raise SolveError(PREFLOP_UNSOLVABLE)
    if len(spot["players"]) != 2:
        raise SolveError(MULTIWAY_UNSOLVABLE)
    return "texas"


def run_backend(spot: Mapping[str, Any], backend: str | None = None, *,
                should_stop: Callable[[], bool] | None = None,
                progress: Callable[[dict], None] | None = None,
                timeout: float | None = None) -> tuple[dict, str]:  # fmt: skip
    """Solve `spot`; (document, "texas"). `should_stop` is polled while the solver runs (true
    -> the process is killed, Preempted); `progress` gets {"iterations",
    "exploitability_pct_pot", "elapsed_sec"} per measured iteration; `timeout` overrides the
    watchdog (default time_limit_sec * 1.5 + 30 s)."""
    chosen = choose_backend(spot)
    if backend not in (None, chosen):
        raise ValueError(f"unknown backend {backend!r} (expected one of {BACKENDS})")
    from pn.solve.texas import run_texas

    doc = run_texas(spot, streets=EMITTED_STREETS, should_stop=should_stop, progress=progress,
                    timeout=timeout)  # fmt: skip
    return doc, chosen
