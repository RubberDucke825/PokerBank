"""Fetch a PokerNow game log with pagination, 429 backoff and an on-disk cache.

Endpoint behaviour (verified against pokernow.com on 2026-10-01):
- GET /games/{id}/log -> {"logs": [...], "infos": {...}}, newest first, 50 per page.
- ?before_at=X returns messages with created_at < X (exclusive); an empty page = start of game.
- ?after_at=X filters to created_at > X (exclusive) but still returns the *newest* page,
  so incremental refresh pages backwards with before_at+after_at until a short page.
- The default python-requests User-Agent gets 403; 'curl/8.4.0' works.
- ~5 rapid requests trigger 429; we pace pages and back off exponentially.
The cache is rewritten after every page, so an interrupted fetch resumes without loss.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.pokernow.com/games/{game_id}"
HEADERS = {"User-Agent": "curl/8.4.0", "Accept": "application/json"}
PAGE_SIZE = 50
PAGE_DELAY_S = 1.5
MAX_RETRIES = 8
MAX_BACKOFF_S = 60.0
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


class FetchError(RuntimeError):
    pass


def parse_game_id(url_or_id: str) -> str:
    m = re.search(r"/games/([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    if _ID_RE.match(url_or_id.strip()):
        return url_or_id.strip()
    raise ValueError(f"not a PokerNow game url or id: {url_or_id!r}")


def _get_json(
    session: requests.Session, url: str, params: dict[str, str], sleep: Callable[[float], None]
) -> dict[str, Any]:
    delay = PAGE_DELAY_S
    for attempt in range(MAX_RETRIES + 1):
        resp = session.get(url, params=params, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else delay
            log.info("HTTP %s from %s; retrying in %.1fs", resp.status_code, url, wait)
            sleep(min(wait, MAX_BACKOFF_S))
            delay = min(delay * 2, MAX_BACKOFF_S)
            continue
        hint = " (User-Agent rejected?)" if resp.status_code == 403 else ""
        raise FetchError(f"GET {url} {params} -> HTTP {resp.status_code}{hint}")
    raise FetchError(f"GET {url} {params}: retries exhausted")


class _Cache:
    def __init__(self, path: Path, game_id: str) -> None:
        self.path = path
        data = json.loads(path.read_text()) if path.exists() else {}
        self.game_id = game_id
        self.complete: bool = bool(data.get("complete", False))
        self.logs: dict[str, dict[str, Any]] = {str(m["created_at"]): m for m in data.get("logs", [])}

    def add(self, page: list[dict[str, Any]]) -> int:
        before = len(self.logs)
        for m in page:
            self.logs[str(m["created_at"])] = m
        return len(self.logs) - before

    def bounds(self) -> tuple[int | None, int | None]:
        keys = [int(k) for k in self.logs]
        return (min(keys), max(keys)) if keys else (None, None)

    def ordered(self) -> list[dict[str, Any]]:
        return [self.logs[k] for k in sorted(self.logs, key=int, reverse=True)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "game_id": self.game_id, "complete": self.complete,
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "logs": self.ordered(),
        }))
        tmp.replace(self.path)


def _page_back(
    session: requests.Session, url: str, cache: _Cache, sleep: Callable[[float], None],
    before: int | None, after: int | None,
) -> bool:
    """Page backwards from `before` (None = newest) until the start of the game (after=None)
    or until reaching `after`. Saves after every page. Returns True if the log start was reached."""
    first = True
    while True:
        if not first:
            sleep(PAGE_DELAY_S)
        first = False
        params = {}
        if before is not None:
            params["before_at"] = str(before)
        if after is not None:
            params["after_at"] = str(after)
        page = _get_json(session, url, params, sleep).get("logs") or []
        cache.add(page)
        cache.save()
        log.info("fetched %d messages (cache now %d)", len(page), len(cache.logs))
        if not page:
            return after is None
        before = min(int(m["created_at"]) for m in page)
        if len(page) < PAGE_SIZE:
            return after is None


def fetch_game_log(
    url_or_id: str,
    cache_dir: str | Path = "data/raw",
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    refresh: bool = True,
) -> list[dict[str, Any]]:
    """Return every log message of the game (newest first), using/refreshing the cache.

    1. If the cache has messages, fetch only newer ones (after_at = cached max).
    2. If the cache never reached the start of the game, keep paging backwards from the
       cached minimum (resume after interruption / 429 exhaustion).
    """
    game_id = parse_game_id(url_or_id)
    cache = _Cache(Path(cache_dir) / f"{game_id}.json", game_id)
    session = session or requests.Session()
    url = BASE_URL.format(game_id=game_id) + "/log"
    lo, hi = cache.bounds()
    if hi is not None and refresh:
        _page_back(session, url, cache, sleep, before=None, after=hi)
    if not cache.complete:
        if hi is not None:
            sleep(PAGE_DELAY_S)
        cache.complete = _page_back(session, url, cache, sleep, before=lo, after=None)
        cache.save()
    return cache.ordered()


def fetch_ledger(
    url_or_id: str,
    cache_dir: str | Path = "data/raw",
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Download the ledger CSV (cents) to <cache_dir>/ledger_<id>.csv and return its path."""
    game_id = parse_game_id(url_or_id)
    session = session or requests.Session()
    url = BASE_URL.format(game_id=game_id) + f"/ledger_{game_id}.csv"
    for attempt in range(MAX_RETRIES + 1):
        resp = session.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 429 and attempt < MAX_RETRIES:
            sleep(min(PAGE_DELAY_S * 2**attempt, MAX_BACKOFF_S))
            continue
        if resp.status_code != 200:
            raise FetchError(f"GET {url} -> HTTP {resp.status_code}")
        path = Path(cache_dir) / f"ledger_{game_id}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text, encoding="utf-8")
        return path
    raise FetchError(f"GET {url}: retries exhausted")
