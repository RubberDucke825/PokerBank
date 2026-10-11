"""Cross-process solve lock (8 GB machine: one solver subprocess at a time, across the web app
and a background `pn library fill`). An flock on `<cache root>/.solve.lock` serialises solves;
priority holders (trainer street solves) leave a marker in `<cache root>/.solve_priority/`
while they wait and run, so ordinary holders let them go first and a preemptible run (the
library fill) stops its subprocess when one appears (`priority_waiting`). Ordinary holders
(web new-spot / subgame solves) leave a marker in `<cache root>/.solve_waiting/` while they
wait, and a `background` holder (the fill) lets them go first between its own solves instead of
starving them (2026-10-07: a river solve waited 460 s behind the fill)."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

LOCK_NAME = ".solve.lock"
PRIORITY_DIR = ".solve_priority"
WAITING_DIR = ".solve_waiting"
POLL_SEC = 0.2


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_markers(d: Path) -> bool:
    if not d.exists():
        return False
    found = False
    for marker in d.iterdir():
        try:
            pid = int(marker.name.split("-")[0])
        except ValueError:
            continue
        if _alive(pid):
            found = True
        else:
            with suppress(FileNotFoundError):
                marker.unlink()
    return found


def priority_waiting(root: Path) -> bool:
    """A live process waits for, or runs, a priority solve under `root` (stale markers of dead
    processes are removed)."""
    return _live_markers(root / PRIORITY_DIR)


def ordinary_waiting(root: Path) -> bool:
    """A live process waits for an ordinary (non-background) solve under `root`."""
    return _live_markers(root / WAITING_DIR)


def _marker(root: Path, sub: str) -> Path:
    (root / sub).mkdir(exist_ok=True)
    m = root / sub / f"{os.getpid()}-{threading.get_ident()}"
    m.touch()
    return m


@contextmanager
def file_lock(root: Path, priority: bool = False, background: bool = False) -> Iterator[None]:
    """Hold the cross-process solve lock under `root`. Ordinary holders wait while a priority
    holder is waiting or running; a `background` holder also waits while an ordinary holder is
    waiting, so interactive solves slip in between background solves."""
    root.mkdir(parents=True, exist_ok=True)
    marker = _marker(root, PRIORITY_DIR) if priority else None
    waiting = _marker(root, WAITING_DIR) if not priority and not background else None

    def must_wait() -> bool:
        if priority:
            return False
        return priority_waiting(root) or (background and ordinary_waiting(root))

    try:
        with (root / LOCK_NAME).open("a+") as f:
            while True:
                while must_wait():
                    time.sleep(POLL_SEC)
                fcntl.flock(f, fcntl.LOCK_EX)
                if not must_wait():
                    break
                fcntl.flock(f, fcntl.LOCK_UN)
            if waiting is not None:
                with suppress(FileNotFoundError):
                    waiting.unlink()
                waiting = None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        for m in (marker, waiting):
            if m is not None:
                with suppress(FileNotFoundError):
                    m.unlink()
