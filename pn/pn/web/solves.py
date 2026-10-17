"""Solver-output files (contracts/strategy_output.md) for the strategy viewer.

Sources: `<solves_dir>/*.json`, uploads (kept in memory for the server's lifetime), the
SolveCache and the built-in heuristic `sample`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import tomllib
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from pn.profile.paths import repo_root
from pn.solve import Seat, SolveCache, SpotRequest, build_spot, docstore, library, spot_key
from pn.solve.cache import is_stale
from pn.solve.lazydoc import LazyDoc, LazyDocError
from pn.solve.request import (
    POSTFLOP_SEAT_ORDER,
    TABLE_SEATS,
    apply_preset,
    new_request,
    parse_board,
    request_from_json,
    request_to_json,
)
from pn.solve.sources import ProfileSource
from pn.solve.subgame import path_to, subgame_request
from pn.web.jobs import SolveHooks, cache_solve
from pn.web.sample import generate_sample

SAMPLE = "sample"
CACHE_SIZE = 4
MAX_GENERATED = 16  # solved documents kept in memory (each can be tens of MB)
NODE_ARRAYS = ("strategy", "ev", "reach", "equity")
log = logging.getLogger(__name__)


class SolveError(ValueError):
    """Malformed solver output."""


class SolveNotFound(LookupError):
    """Unknown solve name or node."""


def validate(doc: object) -> dict:
    if not isinstance(doc, dict):
        raise SolveError("solver output must be a JSON object")
    for k in ("combos", "root", "nodes"):
        if k not in doc:
            raise SolveError(f"missing top-level key {k!r}")
    if not isinstance(doc["nodes"], dict) or doc["root"] not in doc["nodes"]:
        raise SolveError("root node not found in nodes")
    if len(doc["combos"]) != 1326:
        raise SolveError(f"expected 1326 combos, got {len(doc['combos'])}")
    return doc


LAZY_BYTES = 150_000_000  # files above this are opened as LazyDoc (offset index, nodes on demand)


def validate_lazy(doc: LazyDoc) -> LazyDoc:
    """validate() from the index alone: no node is parsed."""
    if len(doc.combos) != 1326:
        raise SolveError(f"expected 1326 combos, got {len(doc.combos)}")
    if not doc.has(doc.root):
        raise SolveError("root node not found in nodes")
    dangling = doc.dangling_children()
    if dangling:
        raise SolveError(f"{len(dangling)} child ids missing from nodes, e.g. {dangling[:3]}")
    return doc


def open_doc(path: Path) -> dict | LazyDoc:
    """Validated document: json.load for small files, LazyDoc above LAZY_BYTES."""
    if path.stat().st_size > LAZY_BYTES:
        try:
            return validate_lazy(LazyDoc.open(path))
        except LazyDocError as e:
            raise SolveError(str(e)) from e
    try:
        with path.open() as f:
            return validate(json.load(f))
    except json.JSONDecodeError as e:
        raise SolveError(f"{path}: invalid JSON ({e})") from e


def board_of(doc: dict) -> list[str]:
    """Board cards: meta.board / top-level board (extension), else [game].board of the TOML
    named by meta.config_path when it is readable."""
    meta = doc.get("meta") or {}
    for b in (meta.get("board"), doc.get("board")):
        if isinstance(b, list):
            return [str(c) for c in b]
    cfg = meta.get("config_path")
    if isinstance(cfg, str):
        for p in (Path(cfg), repo_root() / cfg):
            try:
                return list(tomllib.loads(p.read_text()).get("game", {}).get("board", []))
            except (OSError, tomllib.TOMLDecodeError, ValueError):
                continue
    return []


def stale_doc(doc: dict) -> bool:
    """SolveCache's rule (pn.solve.cache.is_stale) applied to a document alone: a 2-player postflop
    document not solved by texas while TexasSolver is available."""
    root = doc["nodes"][doc["root"]]
    shape = {"game": {"street": root.get("street", "flop")}, "players": doc.get("players") or []}
    return is_stale(shape, None, doc)


INDEX_FILE = "registry_names.json"  # in the SolveCache dir: generated name -> key/path + info
CACHE_PREFIX = "cache-"  # `cache-<key prefix>` opens any SolveCache document (saved solves library)


class Registry:
    """Solve documents by name. Generated solves (new spots, subgames) keep the last
    MAX_GENERATED in memory; registered with a SolveCache `key` they are also indexed on disk,
    so an evicted or pre-restart name reloads from the cache. Library roots (200-500 MB of
    JSON each) are registered by path, loaded lazily, and at most ONE is held in memory."""

    def __init__(self, solves_dir: Path, cache: SolveCache | None = None) -> None:
        self.solves_dir = solves_dir
        self._store = cache
        self._uploads: dict[str, dict] = {}
        # solved in this process (new spots, subgames, trainer streets); oldest evicted
        self._generated: OrderedDict[str, dict] = OrderedDict()
        # name -> {"source", "parent", "req", "elapsed_sec", "spot"}; spot = the New-spot body
        self._info: dict[str, dict] = {}
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._libraries: dict[str, Path] = {}  # name -> library file (not loaded)
        self._library: tuple[str, dict] | None = None  # the one loaded library root
        self._lock = threading.Lock()

    def _files(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        if self.solves_dir.is_dir():
            out |= {p.stem: p for p in sorted(self.solves_dir.glob("*.json"))}
        return out

    # ------------------------------------------------------------ persisted names
    def use_cache(self, cache: SolveCache) -> None:
        """Index generated names next to the documents of the cache the app solves with."""
        with self._lock:
            self._store = cache

    def _cache_store(self) -> SolveCache:
        if self._store is None:
            self._store = SolveCache()
        return self._store

    def _index_path(self) -> Path:
        return self._cache_store().root / INDEX_FILE

    def _index(self) -> dict[str, dict]:
        try:
            return json.loads(self._index_path().read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _persist(self, name: str, entry: dict) -> None:
        path = self._index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        index = {**self._index(), name: entry}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index))
        tmp.replace(path)

    def _load_library(self, name: str) -> dict:
        """The library root `name`, replacing the loaded one (caller holds the lock)."""
        if self._library is not None and self._library[0] == name:
            return self._library[1]
        self._library = None  # free the previous root before parsing the next one
        path = self._libraries[name]
        try:
            doc = open_doc(path)
        except OSError as e:
            raise SolveNotFound(f"library file {path} is gone ({e})") from e
        self._library = (name, doc)
        return doc

    def _reload(self, name: str) -> dict | None:
        """Generated solve `name` from the on-disk index (caller holds the lock)."""
        entry = self._index().get(name)
        if entry is None and name.startswith(CACHE_PREFIX):
            entry = self._cache_entry(name)
        if entry is None:
            return None
        if not entry.get("key"):
            if not entry.get("path") or not Path(entry["path"]).exists():
                return None
            self._libraries[name] = Path(entry["path"])
            self._info[name] = {"source": entry.get("source", "library"), "parent": None,
                                "req": None, "elapsed_sec": None,
                                "spot": entry.get("spot")}  # fmt: skip
            return self._load_library(name)
        doc = self._cache_store().get(entry["key"])
        if doc is None:
            return None
        doc = with_board(doc, tuple(entry.get("board") or ()))
        req = request_from_json(entry["req"]) if entry.get("req") else None
        self._keep(name, doc, {"source": entry.get("source", "cached"), "parent": entry.get("parent"),
                               "req": req, "elapsed_sec": None, "spot": entry.get("spot"),
                               "key": entry["key"]})  # fmt: skip
        return doc

    def _cache_entry(self, name: str) -> dict | None:
        """Index-shaped entry for `cache-<key prefix>`: any SolveCache document opens by its key,
        with the board from its spot TOML (caller holds the lock)."""
        key = self._cache_key(name[len(CACHE_PREFIX):])
        if key is None:
            return None
        try:
            game = tomllib.loads((self._cache_store().root / f"{key}.toml").read_text())["game"]
        except (OSError, tomllib.TOMLDecodeError, KeyError):
            game = {}
        return {"key": key, "board": list(game.get("board") or []), "source": "cache",
                "parent": None, "req": None, "spot": None}  # fmt: skip

    def _keep(self, name: str, doc: dict, info: dict) -> None:
        self._generated[name] = doc
        self._generated.move_to_end(name)
        self._info[name] = info
        while len(self._generated) > MAX_GENERATED:
            old, _ = self._generated.popitem(last=False)
            self._info.pop(old, None)

    def _fresh(self, name: str) -> dict:
        """Generated document `name`; a stale one (stale_doc) is swapped for the cache entry of its
        key once that entry has been re-solved with dcfr. The cache file is re-read only when its
        mtime changes, so walking a stale tree costs one stat per request (caller holds the lock)."""
        doc = self._generated[name]
        info = self._info.get(name) or {}
        key = info.get("key")
        if not key or not stale_doc(doc):
            return doc
        path = self._cache_store().path(key)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return doc
        if info.get("seen_mtime") == mtime:
            return doc
        try:
            new = docstore.read(path.parent, key)  # retries a `pn cache compress` swap
        except (OSError, ValueError, EOFError) as e:
            log.warning("re-reading %s for %s failed: %s", path, name, e)
            info["seen_mtime"] = mtime  # unreadable as it stands: retry when it changes
            return doc
        if new is None:
            return doc  # gone meanwhile: not seen, so the next request looks again
        info["seen_mtime"] = mtime
        if stale_doc(new):
            return doc
        new = with_board(new, tuple((doc.get("meta") or {}).get("board") or ()))
        self._generated[name] = new
        log.info("%s: replaced a %s document with the re-solved %s one", name,
                 (doc.get("meta") or {}).get("backend"), (new.get("meta") or {}).get("backend"))  # fmt: skip
        return new

    def list(self) -> list[dict]:
        rows = [{"name": SAMPLE, "source": "built-in heuristic sample (mock)"}]
        rows += [{"name": n, "source": str(p)} for n, p in self._files().items()]
        rows += [{"name": n, "source": "upload"} for n in self._uploads]
        rows += [{"name": n, "source": self._info[n]["source"]} for n in self._generated]
        rows += [{"name": n, "source": self._info.get(n, {}).get("source", str(p))}
                 for n, p in self._libraries.items()]  # fmt: skip
        seen = {r["name"] for r in rows}
        rows += [{"name": n, "source": e.get("source", "cached")}
                 for n, e in self._index().items() if n not in seen]  # fmt: skip
        return rows

    def get(self, name: str) -> dict:
        with self._lock:
            if name in self._uploads:
                return self._uploads[name]
            if name in self._generated:
                return self._fresh(name)
            if name in self._libraries:
                return self._load_library(name)
            if name in self._cache:
                self._cache.move_to_end(name)
                return self._cache[name]
            if name == SAMPLE:
                doc = generate_sample()
            else:
                path = self._files().get(name)
                if path is None:
                    reloaded = self._reload(name)
                    if reloaded is not None:
                        return reloaded
                    raise SolveNotFound(f"no solve {name!r}")
                doc = open_doc(path)
            self._cache[name] = doc
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return doc

    def add_upload(self, filename: str, raw: bytes) -> str:
        try:
            doc = validate(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SolveError(f"invalid JSON: {e}") from e
        stem = Path(filename or "upload").stem or "upload"
        name = f"upload-{stem}-{hashlib.sha1(raw).hexdigest()[:8]}"
        with self._lock:
            self._uploads[name] = doc
        return name

    def register(self, name: str, doc: dict, source: str, *, parent: dict | None = None,
                 req: SpotRequest | None = None, elapsed_sec: float | None = None,
                 key: str | None = None, path: Path | None = None,
                 spot: dict | None = None) -> str:  # fmt: skip
        """Keep a solved document under `name`. With the SolveCache `key` of its spot (or the
        library file `path`) the name is also indexed on disk and survives eviction/restart.
        `spot` is the New-spot body that produced it (re-requested by the vs-opponent view)."""
        validate(doc)
        with self._lock:
            self._keep(name, doc, {"source": source, "parent": parent, "req": req,
                                   "elapsed_sec": elapsed_sec, "spot": spot, "key": key})  # fmt: skip
            if key is not None or path is not None:
                self._persist(name, {"key": key, "path": str(path) if path else None,
                                     "board": (doc.get("meta") or {}).get("board"),
                                     "source": source, "parent": parent,
                                     "req": request_to_json(req) if req else None,
                                     "spot": spot})  # fmt: skip
        return name

    def register_library(self, name: str, path: Path, source: str,
                         spot: dict | None = None) -> str:  # fmt: skip
        """Register a library root file by path (indexed on disk); loaded on first get()."""
        with self._lock:
            self._libraries[name] = path
            self._info[name] = {"source": source, "parent": None, "req": None, "elapsed_sec": None,
                                "spot": spot}  # fmt: skip
            self._persist(name, {"key": None, "path": str(path), "board": [], "source": source,
                                 "parent": None, "req": None, "spot": spot})  # fmt: skip
        return name

    def has(self, name: str) -> bool:
        """Known name; a library root is checked by its file, without loading it."""
        with self._lock:
            if name in self._generated or name in self._libraries:
                return True
            entry = self._index().get(name)
            if entry is None:
                return False
            if not entry.get("key"):
                return bool(entry.get("path")) and Path(entry["path"]).exists()
            return self._reload(name) is not None

    def info(self, name: str) -> dict:
        with self._lock:
            return dict(self._info.get(name) or {})

    def index(self) -> dict[str, dict]:
        """The on-disk name index (generated name -> key / path / spot / parent)."""
        with self._lock:
            return self._index()

    def cache_root(self) -> Path:
        with self._lock:
            return self._cache_store().root

    def delete(self, name: str) -> dict:
        """Forget `name` and delete its solved files: the SolveCache entry (`.json` + `.toml`) of a
        generated or `cache-<key>` solve, or the file of a `<solves_dir>` solve. Every indexed name
        sharing the cache key goes too. Library roots are only unregistered (their file stays).
        SolveNotFound for an unknown name, ValueError for the built-in sample."""
        if name == SAMPLE:
            raise ValueError("the built-in sample cannot be deleted")
        with self._lock:
            index = self._index()
            entry = index.get(name) or {}
            key = entry.get("key") or (self._info.get(name) or {}).get("key")
            if key is None and name.startswith(CACHE_PREFIX):
                key = self._cache_key(name[len(CACHE_PREFIX):])
            file = self._files().get(name) if name not in index else None
            known = (name in index or name in self._generated or name in self._uploads
                     or name in self._libraries or key is not None or file is not None)  # fmt: skip
            if not known:
                raise SolveNotFound(f"no solve {name!r}")
            names = {name} | ({n for n, e in index.items() if e.get("key") == key} if key else set())
            for n in names:
                for store in (self._generated, self._info, self._uploads, self._cache, self._libraries):
                    store.pop(n, None)  # type: ignore[attr-defined]
            if self._library is not None and self._library[0] in names:
                self._library = None
            root = self._cache_store().root
            removed = [str(p) for p in docstore.remove(root, key)] if key else []
            paths = [root / f"{key}.toml"] if key else []
            if file is not None and file.parent == self.solves_dir:
                paths.append(file)
            for p in paths:
                try:
                    p.unlink()
                    removed.append(str(p))
                except FileNotFoundError:
                    pass
            if key:  # drop SolveCache's in-memory copy too (it has no public eviction call)
                store = self._cache_store()
                mem, mem_lock = getattr(store, "_mem", None), getattr(store, "_mem_lock", None)
                if mem is not None and mem_lock is not None:
                    with mem_lock:
                        mem.pop(key, None)
            left = {n: e for n, e in index.items() if n not in names}
            if len(left) != len(index):
                tmp = self._index_path().with_suffix(".tmp")
                tmp.write_text(json.dumps(left))
                tmp.replace(self._index_path())
        return {"deleted": sorted(names), "files": removed}

    def _cache_key(self, prefix: str) -> str | None:
        """Full SolveCache key for a `cache-<prefix>` name (caller holds the lock)."""
        if not re.fullmatch(r"[0-9a-f]{8,64}", prefix):
            return None
        root = self._cache_store().root
        hits = {k for p in root.glob(f"{prefix}*.json*") if (k := docstore.key_of(p.name))}
        return hits.pop() if len(hits) == 1 else None

    # ------------------------------------------------------------ views
    def meta(self, name: str) -> dict:
        doc = self.get(name)
        info = self.info(name)
        return {"name": name, "meta": doc.get("meta", {}), "players": doc.get("players", []),
                "combos": doc["combos"], "root": doc["root"], "board": board_of(doc),
                "node_count": len(doc["nodes"]), "parent": info.get("parent"),
                "elapsed_sec": info.get("elapsed_sec"), "spot": info.get("spot")}  # fmt: skip

    def node(self, name: str, node_id: str) -> dict:
        doc = self.get(name)
        node = doc["nodes"].get(node_id)
        if node is None:
            raise SolveNotFound(f"no node {node_id!r} in {name}")
        return {"id": node_id, **node}

    def walk(self, name: str, tokens: list[str]) -> dict:
        """Node reached from the root by action tokens / chance cards (for compare mode)."""
        doc = self.get(name)
        nid = doc["root"]
        for i, tok in enumerate(tokens):
            node = doc["nodes"][nid]
            nxt = None
            if node.get("type") == "action":
                nxt = next((a["child"] for a in node["actions"] if a["token"] == tok), None)
            elif node.get("type") == "chance":
                nxt = node.get("children", {}).get(tok)
            if nxt is None:
                raise SolveNotFound(f"path {tokens[: i + 1]} does not exist in {name}")
            nid = nxt
        return self.node(name, nid)


# ------------------------------------------------------------------ solving from the UI
# "New spot" (preflop root or heads-up postflop root) and lazy next-street subgames for the
# Strategy viewer. A spot is prepared (and validated) synchronously, then solved in a job through
# the SolveCache; a preflop root is served only from an existing library file (no preflop solver).

NEW_SPOT_PRESET = "review"
DEFAULT_SB, DEFAULT_BB = 50, 100
POSTFLOP_STREETS = ("flop", "turn", "river")
CARD_RE = re.compile(r"^[2-9TJQKA][shdc]$")
# Plan 7 custom sizing (contracts/tree_config.md [sizes]); anything else in `sizes` is refused.
SIZE_KEYS = frozenset({"flop_bets", "turn_bets", "river_bets", "raise", "allin_threshold", "donk",
                       "max_bets_per_street"})  # fmt: skip

@dataclass(frozen=True)
class SeatSpec:
    position: str
    player_id: str | None = None
    range: str | None = None


def with_board(doc: dict, board: tuple[str, ...]) -> dict:
    """Shallow copy with meta.board set (the cached document stays untouched)."""
    return {**doc, "meta": {**(doc.get("meta") or {}), "board": list(board)}}


def spot_echo(*, table: int, depth: int, source: str, street: str, board: str,
              line: str | None, hero: str | None, seats: tuple[SeatSpec, ...],
              preset: str, sizes: dict | None = None, pot: int | None = None,
              stack: int | None = None) -> dict:  # fmt: skip
    """The New-spot body that reproduces a root solve (`GET /api/solves/{name}` -> `spot`)."""
    cards = list(parse_board(board)) if board else []
    out = {"table": table, "depth": depth, "source": source, "street": street, "board": cards,
           "line": line, "hero": hero, "preset": preset,
           "seats": [{"position": s.position, "player_id": s.player_id, "range": s.range}
                     for s in seats]}  # fmt: skip
    for k, v in (("sizes", sizes), ("pot", pot), ("stack", stack)):
        if v is not None:
            out[k] = v
    return out


def check_cards(cards: tuple[str, ...]) -> None:
    bad = [c for c in cards if not CARD_RE.match(c)]
    if bad or len(set(cards)) != len(cards):
        raise ValueError(f"bad or repeated cards: {list(cards)}")


def check_sizes(sizes: dict) -> dict:
    """Custom sizing: only SIZE_KEYS; bet/raise lists of positive pot percentages (or "auto"
    for flop_bets). Engine-level limits are checked by build_spot."""
    unknown = set(sizes) - SIZE_KEYS
    if unknown:
        raise ValueError(f"unknown sizes keys {sorted(unknown)}; allowed {sorted(SIZE_KEYS)}")
    for k in ("flop_bets", "turn_bets", "river_bets", "raise"):
        v = sizes.get(k)
        if v is None or (k == "flop_bets" and v == "auto"):
            continue
        if not isinstance(v, list) or not v or not all(isinstance(x, int | float) and 0 < x <= 1000
                                                       for x in v):  # fmt: skip
            raise ValueError(f"sizes.{k} must be a non-empty list of pot percentages (1-1000)")
    t = sizes.get("allin_threshold")
    if t is not None and not (isinstance(t, int | float) and 0 < t <= 1):
        raise ValueError("sizes.allin_threshold must be in (0, 1]")
    return dict(sizes)


def oop_ip(positions: list[str]) -> tuple[str, str]:
    """(out of position, in position) for a heads-up postflop pot."""
    a, b = sorted(positions, key=POSTFLOP_SEAT_ORDER.index)
    return a, b


def seat_ranges(seats: tuple[SeatSpec, ...], ranges: dict[str, str] | None) -> tuple[SeatSpec, ...]:
    """`ranges` keyed "oop" / "ip" (heads-up postflop) or by position replace seat ranges."""
    if not ranges:
        return seats
    pos = [s.position for s in seats]
    alias = dict(zip(("oop", "ip"), oop_ip(pos), strict=True)) if len(pos) == 2 else {}
    by_pos: dict[str, str] = {}
    for k, text in ranges.items():
        p = alias.get(k.lower(), k)
        if p not in pos:
            raise ValueError(f"ranges key {k!r} is not a seat ({pos})")
        if (text or "").strip():
            by_pos[p] = text.strip()
    return tuple(replace(s, range=by_pos.get(s.position, s.range)) for s in seats)


@dataclass(frozen=True)
class Prepared:
    """A validated New-spot request: `name` is set when nothing needs solving (library root)."""

    echo: dict
    name: str | None = None
    spot: dict | None = None
    backend: str | None = None
    req: SpotRequest | None = None


def prepare_spot(registry: Registry, profiles: ProfileSource | None, *,
                 table: int = 6, depth: int = 100, source: str = "gto", street: str = "preflop",
                 board: str = "", line: str | None = None, hero: str | None = None,
                 seats: tuple[SeatSpec, ...] = (), preset: str = NEW_SPOT_PRESET,
                 ranges: dict[str, str] | None = None, sizes: dict | None = None,
                 pot: int | None = None, stack: int | None = None) -> Prepared:  # fmt: skip
    """Build and validate the spot (ValueError on anything wrong). Preflop: the whole table (seat
    specs add players / ranges), a library root when one exists and nothing is custom. Postflop:
    heads-up only, `seats` are the two players, `line` srp|3bp sets pot and stacks unless `pot` /
    `stack` (cents) override them, `ranges` and `sizes` replace the defaults."""
    if street in POSTFLOP_STREETS and len(seats) != 2:
        raise ValueError("postflop spots are heads-up only: give exactly two seats")
    seats = seat_ranges(seats, ranges)
    specs = {s.position: s for s in seats}
    custom = any(s.player_id or s.range for s in seats)
    # Plan 7: the preset is the user's explicit choice (the builder shows its budget); the dcfr-era
    # review -> library swap for heads-up flops is gone.
    if sizes is not None:
        sizes = check_sizes(sizes)
    echo = spot_echo(table=table, depth=depth, source=source, street=street, board=board,
                     line=line, hero=hero, seats=seats, preset=preset, sizes=sizes, pot=pot,
                     stack=stack)  # fmt: skip
    if street == "preflop":
        lib = library.library_path(table, depth, source)
        if lib.exists() and not custom:
            name = f"library-{table}max-{depth}bb-{source}"
            registry.register_library(name, lib, f"library {lib.name}", spot=echo)
            return Prepared(echo, name=name)
        positions = list(TABLE_SEATS.get(table, ()))
        cards: tuple[str, ...] = ()
        hero = hero or "BTN"
    else:
        if street not in POSTFLOP_STREETS:
            raise ValueError(f"street must be preflop|flop|turn|river, got {street!r}")
        positions = [s.position for s in seats]
        if len(positions) != 2:
            raise ValueError("postflop spots are heads-up only: give exactly two seats")
        cards = parse_board(board)
        check_cards(cards)
    req = new_request(positions, depth, table=table, source=source, board=cards, hero=hero,  # type: ignore[arg-type]
                      line=line if street != "preflop" else None,
                      seat_ranges={p: s.range for p, s in specs.items() if s.range})  # fmt: skip
    if req.street != street:
        raise ValueError(f"board {list(cards)} does not start the {street}")
    req = replace(req, seats=tuple(replace(s, player_id=specs[s.position].player_id)
                                   if s.position in specs else s for s in req.seats))  # fmt: skip
    if street != "preflop" and (pot is not None or stack is not None):
        if (pot is not None and pot <= 0) or (stack is not None and stack <= 0):
            raise ValueError("pot and stack must be positive (cents)")
        req = replace(req, pot=pot if pot is not None else req.pot,
                      seats=tuple(replace(s, stack=stack) if stack is not None else s
                                  for s in req.seats))  # fmt: skip
    if street != "preflop" and sizes:
        req = replace(req, sizes={**(req.sizes or {}), **sizes})
    spot, backend = apply_preset(build_spot(req, profiles), preset)  # preflop: SolveError
    return Prepared(echo, spot=spot, backend=backend, req=req)


def solve_prepared(registry: Registry, cache: SolveCache, p: Prepared,
                   hooks: SolveHooks | None = None) -> str:  # fmt: skip
    """Solve a Prepared spot and register it; the name."""
    if p.name is not None:
        return p.name
    assert p.spot is not None and p.req is not None and p.backend is not None
    registry.use_cache(cache)
    t0 = time.monotonic()
    doc = cache_solve(cache, p.spot, p.backend, hooks)
    key = spot_key(p.spot)
    e = p.echo
    name = f"new-{e['table']}max-{e['depth']}bb-{e['source']}-{e['street']}{''.join(p.req.board)}-{key[:8]}"
    return registry.register(name, with_board(doc, p.req.board), f"solved {e['street']} root",
                             req=p.req, elapsed_sec=round(time.monotonic() - t0, 2),
                             key=key, spot=e)  # fmt: skip


def new_spot(registry: Registry, cache: SolveCache, profiles: ProfileSource | None, *,
             hooks: SolveHooks | None = None, **kw: object) -> str:  # fmt: skip
    """prepare_spot + solve_prepared in one call; returns the solve name."""
    registry.use_cache(cache)
    return solve_prepared(registry, cache, prepare_spot(registry, profiles, **kw), hooks)  # type: ignore[arg-type]


def hint_from_doc(doc: dict) -> SpotRequest:
    """SpotRequest stand-in for documents solved elsewhere (files, uploads): positions and hero
    from `players`, board from the document, anonymous seats, source gto."""
    root = doc["nodes"][doc["root"]]
    seats = tuple(Seat(None, p["position"], 0, hero=bool(p.get("hero")), name=p.get("name"))
                  for p in doc.get("players", []))  # fmt: skip
    return SpotRequest(seats=seats, street=root.get("street", "flop"), board=tuple(board_of(doc)),
                       pot=int(root.get("pot", 0)), sb=DEFAULT_SB, bb=DEFAULT_BB, source="gto")  # fmt: skip


def solve_subgame(registry: Registry, cache: SolveCache, profiles: ProfileSource | None,
                  name: str, node_id: str, cards: tuple[str, ...],
                  preset: str = NEW_SPOT_PRESET, hooks: SolveHooks | None = None) -> str:  # fmt: skip
    """Solve the next street at a leaf / chance node of solve `name` and register it as
    `<name>__<node>__<cards>` with its parent (name, node, path tokens, cards) for breadcrumbs."""
    registry.use_cache(cache)
    child = f"{name}__{node_id}__{''.join(cards)}"
    if registry.has(child):
        return child
    doc = registry.get(name)
    hint = registry.info(name).get("req") or hint_from_doc(doc)
    req = subgame_request(doc, node_id, cards, hint)
    spot, backend = apply_preset(build_spot(req, profiles), preset)
    t0 = time.monotonic()
    solved = cache_solve(cache, spot, backend, hooks)
    parent = {"name": name, "node": node_id, "cards": list(cards),
              "path": [tok for _, tok in path_to(doc, node_id)]}  # fmt: skip
    return registry.register(child, with_board(solved, req.board), f"subgame of {name}",
                             parent=parent, req=req, elapsed_sec=round(time.monotonic() - t0, 2),
                             key=spot_key(spot))  # fmt: skip


