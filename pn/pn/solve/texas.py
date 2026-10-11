"""TexasSolver backend (contracts/texas_backend.md): spot dict -> console_solver command file,
a subprocess run with streamed progress, a watchdog and preemption, and the dump converted to a
contracts/strategy_output.md document by pn.solve.texas_doc.

Heads-up postflop only; `players[0]` = OOP, `players[1]` = IP. TexasSolver splits `set_pot`
equally into both players' commitments and plays the effective (smaller) stack.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pn import ranges
from pn.profile.paths import repo_root
from pn.solve.texas_doc import STREETS, Row, Sizes, to_document

log = logging.getLogger(__name__)

ENV_BINARY = "PN_TEXAS"
ENV_RESOURCES = "PN_TEXAS_RESOURCES"
TIMEOUT_FACTOR = 1.5
TIMEOUT_GRACE_SEC = 30
POLL_SEC = 0.2
KILL_GRACE_SEC = 2.0
MIN_WEIGHT = 0.005  # TexasSolver drops range entries with weight <= this
MAX_PRINT_INTERVAL = 10
DEFAULT_SIZES: dict[str, Any] = {"flop_bets": "auto", "turn_bets": [75], "river_bets": [150],
                                 "raise": [100], "max_bets_per_street": 3,
                                 "allin_threshold": 0.67, "donk": True}  # fmt: skip
SEATS = ("oop", "ip")
LOG_TAIL_LINES = 40
ISO_WARNING = "disabling suit isomorphism"

Progress = Callable[[dict], None]


class TexasRefused(ValueError):
    """The spot is outside TexasSolver's scope (code = the refusal reason)."""


def texas_cmd() -> list[str] | None:
    """$PN_TEXAS (may include args), then <repo>/third_party/texassolver/build/console_solver;
    None when neither exists."""
    env = os.environ.get(ENV_BINARY)
    if env:
        return env.split()
    p = repo_root() / "third_party" / "texassolver" / "build" / "console_solver"
    return [str(p)] if p.exists() else None


def resources_dir(cmd: list[str]) -> Path:
    """$PN_TEXAS_RESOURCES, else <binary dir>/../resources, else the vendored resources."""
    env = os.environ.get(ENV_RESOURCES)
    if env:
        return Path(env)
    beside = Path(cmd[0]).resolve().parent.parent / "resources"
    if beside.exists():
        return beside
    return repo_root() / "third_party" / "texassolver" / "resources"


# ------------------------------------------------------------------ command file


def check_spot(spot: Mapping[str, Any]) -> None:
    """TexasRefused unless 2 players, a flop/turn/river start and no locks."""
    street, players = spot["game"]["street"], spot["players"]
    if street == "preflop":
        raise TexasRefused("preflop_unsolvable")
    if len(players) != 2:
        raise TexasRefused("multiway_unsolvable")
    if any(p.get("lock") for p in players):
        raise TexasRefused("locks_unsupported")
    if street not in STREETS:
        raise TexasRefused(f"bad street {street!r}")


def _fmt_weight(w: float) -> str:
    return "" if w >= 1 - 1e-9 else f":{w:.6g}"


def texas_range(text: str, board: list[str]) -> tuple[str, Row]:
    """(TexasSolver range text, dense per-combo weights it will solve with). Weights are
    scaled to max 1 (the solve is invariant to scaling one range); entries at or below
    MIN_WEIGHT after scaling are dropped, as TexasSolver would. A class whose live combos share
    one weight is written as the class (keeps suit isomorphism); otherwise exact combos."""
    dead = {ranges.card_to_int(c) for c in board}
    raw = {c: w for c, w in ranges.parse(text).items()
           if ranges.card_to_int(c[:2]) not in dead and ranges.card_to_int(c[2:]) not in dead}  # fmt: skip
    top = max(raw.values(), default=0.0)
    if top <= 0:
        raise TexasRefused("empty range after board removal")
    used = [0.0] * ranges.N_COMBOS
    parts: list[str] = []
    for cls in ranges.all_classes():
        live = [c for c in ranges.class_combos(cls)
                if ranges.card_to_int(c[:2]) not in dead and ranges.card_to_int(c[2:]) not in dead]  # fmt: skip
        ws = {c: raw.get(c, 0.0) / top for c in live}
        kept = {c: w for c, w in ws.items() if w > MIN_WEIGHT}
        if not kept:
            continue
        for c in kept:
            used[ranges.combo_str_index(c)] = raw[c]
        if len(kept) == len(live) and max(kept.values()) - min(kept.values()) < 1e-6:
            parts.append(cls + _fmt_weight(next(iter(kept.values()))))
        else:
            parts.extend(c + _fmt_weight(w) for c, w in kept.items())
    return ",".join(parts), used


def _auto_flop_bets(board: list[str]) -> list[int]:
    from pn.solve.flops import bucket_of

    table = tomllib.loads((repo_root() / "config" / "sizing_table.toml").read_text())
    bucket = table.get("buckets", {}).get(bucket_of("".join(board[:3])))
    return list((bucket or table["default"])["flop_bets"])


def resolve_sizes(spot: Mapping[str, Any]) -> tuple[dict[str, Any], Sizes]:
    """([sizes] with defaults and flop "auto" resolved, the pct lists for token mapping)."""
    s = {**DEFAULT_SIZES, **(spot.get("sizes") or {})}
    if s["flop_bets"] == "auto":
        s["flop_bets"] = _auto_flop_bets(list(spot["game"]["board"]))
    bets = {st: tuple(int(x) for x in s[f"{st}_bets"]) for st in STREETS}
    return s, Sizes(bets=bets, raises=tuple(int(x) for x in s["raise"]))


def _ints(xs: Any) -> str:
    return ",".join(str(int(x)) for x in xs)


def _size_lines(spot: Mapping[str, Any], s: Mapping[str, Any]) -> list[str]:
    """set_bet_sizes for both seats from the starting street on. donk = false: OOP gets no
    turn/river donk sizes, and no flop bet sizes when IP was the preflop aggressor (all-in
    stays available: TexasSolver's allin flag is per street, not per bet type)."""
    game, players = spot["game"], spot["players"]
    ip_aggressor = game.get("last_aggressor") == players[1]["position"]
    out: list[str] = []
    for street in STREETS[STREETS.index(game["street"]):]:
        bets = s[f"{street}_bets"]
        for seat in SEATS:
            lead_blocked = seat == "oop" and street == "flop" and not s["donk"] and ip_aggressor
            if not lead_blocked:
                out.append(f"set_bet_sizes {seat},{street},bet,{_ints(bets)}")
            if s["raise"]:
                out.append(f"set_bet_sizes {seat},{street},raise,{_ints(s['raise'])}")
            if seat == "oop" and street != "flop" and s["donk"]:
                out.append(f"set_bet_sizes oop,{street},donk,{_ints(bets)}")
            out.append(f"set_bet_sizes {seat},{street},allin")
    return out


def print_interval(max_iterations: int) -> int:
    return max(1, min(MAX_PRINT_INTERVAL, max_iterations // 4))


def command_text(spot: Mapping[str, Any], *, streets: int = 1,
                 dump: str = "dump.json") -> tuple[str, tuple[Row, Row], Sizes]:  # fmt: skip
    """(command file text, per-player solved range weights, size lists). `dump` must not
    contain spaces (TexasSolver splits commands on ' ')."""
    check_spot(spot)
    game, players = spot["game"], spot["players"]
    board = list(game["board"])
    s, sizes = resolve_sizes(spot)
    oop_text, oop_w = texas_range(players[0]["range"], board)
    ip_text, ip_w = texas_range(players[1]["range"], board)
    eff = min(int(p["stack"]) for p in players)
    max_it = int(game.get("max_iterations", 1000))
    lines = [
        f"set_pot {int(game['pot'])}", f"set_effective_stack {eff}",
        f"set_board {','.join(board)}", f"set_range_oop {oop_text}", f"set_range_ip {ip_text}",
        *_size_lines(spot, s),
        f"set_allin_threshold {float(s['allin_threshold'])}",
        f"set_raise_limit {int(s['max_bets_per_street'])}",
        "build_tree",
        f"set_thread_num {int(game.get('threads', 8))}",
        f"set_accuracy {float(game.get('target_exploitability', 0.5))}",
        f"set_max_iteration {max_it}", f"set_print_interval {print_interval(max_it)}",
        "set_use_isomorphism 1", "start_solve",
        f"set_dump_rounds {max(1, int(streets))}", f"dump_result {dump}",
    ]  # fmt: skip
    return "\n".join(lines) + "\n", (oop_w, ip_w), sizes


# ------------------------------------------------------------------ run

ITER_RE = re.compile(r"^Iter:\s*(\d+)")
EXPL_RE = re.compile(r"^Total exploitability\s+(-?[\d.eE+-]+)\s+precent")
TIME_RE = re.compile(r"^time used:\s*([\d.eE+-]+)")


class LogState:
    """Progress parsed from console_solver stdout."""

    def __init__(self, progress: Progress | None, t0: float) -> None:
        self.progress = progress
        self.t0 = t0
        self.iterations = 0
        self.exploitability: float | None = None
        self.solver_sec: float | None = None
        self.isomorphism = True
        self.tail: list[str] = []

    def feed(self, line: str) -> None:
        line = line.strip().split("\r")[-1].strip()
        if not line:
            return
        self.tail = [*self.tail[-(LOG_TAIL_LINES - 1):], line]
        if ISO_WARNING in line:
            self.isomorphism = False
        if m := ITER_RE.match(line):
            self.iterations = int(m.group(1))
        elif m := EXPL_RE.match(line):
            self.exploitability = float(m.group(1))
            self._report()
        elif m := TIME_RE.match(line):
            self.solver_sec = float(m.group(1))

    def _report(self) -> None:
        if self.progress is None:
            return
        try:
            self.progress({"iterations": self.iterations,
                           "exploitability_pct_pot": self.exploitability,
                           "elapsed_sec": round(time.monotonic() - self.t0, 3)})  # fmt: skip
        except Exception:  # a UI callback must never break the solve
            log.exception("texas progress callback failed")


def _kill(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_process(argv: list[str], cwd: Path, timeout: float, state: LogState,
                should_stop: Callable[[], bool] | None) -> int:  # fmt: skip
    """Run console_solver, feeding stdout+stderr lines to `state`. Returns the exit code;
    raises Preempted when `should_stop()` turns true and SolveError on the watchdog."""
    from pn.solve.backend import Preempted, SolveError

    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")  # fmt: skip
    assert proc.stdout is not None
    reader = threading.Thread(target=lambda: [state.feed(x) for x in proc.stdout],  # type: ignore[union-attr]
                              daemon=True)  # fmt: skip
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if should_stop is not None and should_stop():
                _kill(proc)
                raise Preempted("texas preempted by a priority solve")
            if time.monotonic() > deadline:
                _kill(proc)
                raise SolveError(f"texas timed out after {timeout:.0f}s "
                                 f"(iteration {state.iterations})")  # fmt: skip
            time.sleep(POLL_SEC)
    finally:
        if proc.poll() is None:
            _kill(proc)
        reader.join(KILL_GRACE_SEC)
    return proc.returncode


def default_timeout(spot: Mapping[str, Any]) -> float:
    return float(spot["game"].get("time_limit_sec", 300)) * TIMEOUT_FACTOR + TIMEOUT_GRACE_SEC


def run_texas(spot: Mapping[str, Any], *, streets: int = 1,
              should_stop: Callable[[], bool] | None = None, progress: Progress | None = None,
              timeout: float | None = None, cmd: list[str] | None = None) -> dict:  # fmt: skip
    """Solve `spot` with console_solver; the strategy document. SolveError when the binary is
    missing, refuses the spot, fails or is killed by the watchdog (`timeout`, default
    time_limit_sec * 1.5 + 30 s); Preempted when `should_stop()` turns true."""
    from pn.solve.backend import SolveError

    try:
        text, reach, sizes = command_text(spot, streets=streets)
    except TexasRefused as e:
        raise SolveError(str(e)) from e
    cmd = cmd or texas_cmd()
    if cmd is None:
        raise SolveError("console_solver not found: run tools/build_texassolver.sh or set "
                         f"{ENV_BINARY}")  # fmt: skip
    timeout = default_timeout(spot) if timeout is None else timeout
    game = spot["game"]
    with tempfile.TemporaryDirectory(prefix="pn-texas-") as d:
        cwd = Path(d)
        (cwd / "spot.txt").write_text(text)
        t0 = time.monotonic()
        state = LogState(progress, t0)
        argv = [*cmd, "-i", "spot.txt", "-r", str(resources_dir(cmd))]
        rc = run_process(argv, cwd, timeout, state, should_stop)
        wall = time.monotonic() - t0
        dump_path = cwd / "dump.json"
        if rc != 0 or not dump_path.exists():
            tail = " | ".join(state.tail[-5:])
            raise SolveError(f"texas exit {rc}: {tail[-400:]}", rc)
        dump = json.loads(dump_path.read_text())
    max_it = int(game.get("max_iterations", 1000))
    accuracy = float(game.get("target_exploitability", 0.5))
    converged = state.exploitability is not None and state.exploitability <= accuracy
    meta = {"iterations": state.iterations if converged else max_it,
            "exploitability_pct_pot": state.exploitability, "wall_sec": round(wall, 3),
            "stop": "converged" if converged else "max_iterations",
            "max_iterations": max_it, "accuracy_pct_pot": accuracy,
            "threads": int(game.get("threads", 8)), "isomorphism": state.isomorphism}  # fmt: skip
    try:
        return to_document(dump, spot, sizes, reach, streets, meta)
    except (KeyError, ValueError, TypeError) as e:
        raise SolveError(f"texas dump not understood: {e}") from e
