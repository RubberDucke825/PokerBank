"""Lazy strategy documents for files too large to json.load (library roots: 1-2 GB).

One streaming pass (8 MB chunks, regex over the structural bytes `{ } " \\` only; number
arrays are skipped at C speed) records each node's byte span inside top-level "nodes", every
child edge (parent, token or dealt card), and the small top-level members (meta, players,
combos, root). The result is cached next to the file as `<file>.index.json` (reused while the
file's size and mtime match). A LazyDoc is a read-only Mapping with the same top-level keys
as a dict document; `doc["nodes"][id]` seeks and json-loads that one node (small LRU).
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

CHUNK = 8 << 20
NODE_CACHE = 32
_EVENTS = re.compile(rb'[{}"\\]')


class LazyDocError(ValueError):
    """The file is not a well-formed strategy document."""


def index_path(path: Path) -> Path:
    return path.with_name(path.name + ".index.json")


def _stamp(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def build_index(path: Path) -> dict[str, Any]:
    """Single streaming pass; see module doc. Raises LazyDocError on malformed structure."""
    spans: dict[str, list[int]] = {}
    parent: dict[str, list[str]] = {}
    depth, in_str, skip = 0, False, -1
    str_start, str_buf = 0, b""
    nodes_open = nodes_key_at = nodes_end = None
    node_start, cur_id = 0, None
    last_d3 = last_d4 = token = b""
    children_obj, parity = False, 0
    with path.open("rb") as f:
        base = 0
        while chunk := f.read(CHUNK):
            for m in _EVENTS.finditer(chunk):
                pos = base + m.start()
                if pos == skip:
                    continue
                ch = m.group()
                if in_str:
                    if ch == b"\\":
                        skip = pos + 1
                    elif ch == b'"':
                        in_str = False
                        s = str_buf + chunk[max(0, str_start - base):m.start()]
                        str_buf = b""
                        if depth == 1 and s == b"nodes" and nodes_key_at is None:
                            nodes_key_at = str_start - 1
                        elif nodes_open and depth == 2:
                            cur_id = s
                        elif nodes_open and depth == 3:
                            last_d3 = s
                        elif nodes_open and depth == 4:
                            if children_obj:
                                if parity % 2:
                                    parent[s.decode()] = [cur_id.decode(), last_d4.decode()]
                                parity += 1
                            elif last_d4 == b"token":
                                token = s
                            elif last_d4 == b"child":
                                parent[s.decode()] = [cur_id.decode(), token.decode()]
                            last_d4 = s
                    continue
                if ch == b'"':
                    in_str, str_start = True, pos + 1
                elif ch == b"{":
                    depth += 1
                    if depth == 2 and nodes_key_at is not None and nodes_open is None:
                        nodes_open = True
                    elif nodes_open and depth == 3:
                        node_start = pos
                    elif nodes_open and depth == 4:
                        children_obj, parity, last_d4 = last_d3 == b"children", 0, b""
                elif ch == b"}":
                    if nodes_open and depth == 3:
                        spans[cur_id.decode()] = [node_start, pos + 1 - node_start]
                    elif nodes_open and depth == 4:
                        children_obj = False
                    elif nodes_open and depth == 2:
                        nodes_open, nodes_end = False, pos + 1
                    depth -= 1
            if in_str:
                str_buf += chunk[max(0, str_start - base):]
                if len(str_buf) > 1 << 20:
                    str_buf = b""  # a long string (config_path is short; node ids tiny)
            base += len(chunk)
    if nodes_key_at is None or nodes_end is None or depth != 0:
        raise LazyDocError(f"{path}: no complete top-level \"nodes\" object")
    return {"stamp": _stamp(path), "head": _small_members(path, nodes_key_at, nodes_end),
            "spans": spans, "parent": parent}  # fmt: skip


def _small_members(path: Path, nodes_key_at: int, nodes_end: int) -> dict[str, Any]:
    """Top-level members other than "nodes" (before and after it)."""
    with path.open("rb") as f:
        head = f.read(nodes_key_at).rstrip().rstrip(b",")
        f.seek(nodes_end)
        tail = f.read().strip().lstrip(b",").rstrip(b"}").strip()
    text = head + (b"," + tail if tail else b"") + b"}"
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LazyDocError(f"{path}: cannot parse the top-level members ({e})") from e


def load_index(path: Path) -> dict[str, Any]:
    """Cached index when it matches the file, else a fresh pass (saved next to the file)."""
    ip = index_path(path)
    try:
        idx = json.loads(ip.read_text())
        if idx.get("stamp") == _stamp(path):
            return idx
    except (OSError, json.JSONDecodeError):
        pass
    idx = build_index(path)
    tmp = ip.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx))
    tmp.replace(ip)
    return idx


class LazyNodes(Mapping[str, dict]):
    def __init__(self, path: Path, spans: Mapping[str, list[int]]) -> None:
        self._path, self._spans = path, spans
        self._lru: OrderedDict[str, dict] = OrderedDict()

    def __getitem__(self, nid: str) -> dict:
        if nid in self._lru:
            self._lru.move_to_end(nid)
            return self._lru[nid]
        off, length = self._spans[nid]  # KeyError for unknown ids, as a dict would
        with self._path.open("rb") as f:
            f.seek(off)
            node = json.loads(f.read(length))
        self._lru[nid] = node
        while len(self._lru) > NODE_CACHE:
            self._lru.popitem(last=False)
        return node

    def __contains__(self, nid: object) -> bool:
        return nid in self._spans

    def __iter__(self) -> Iterator[str]:
        return iter(self._spans)

    def __len__(self) -> int:
        return len(self._spans)


class LazyDoc(Mapping[str, Any]):
    """Read-only document view: meta / players / combos / root are in memory, nodes on demand."""

    def __init__(self, path: Path, idx: Mapping[str, Any]) -> None:
        self.path = path
        self._head = dict(idx["head"])
        self._nodes = LazyNodes(path, idx["spans"])
        self._parent = idx["parent"]

    @classmethod
    def open(cls, path: str | Path) -> LazyDoc:
        path = Path(path)
        return cls(path, load_index(path))

    # Mapping protocol: the same top-level keys as the dict document
    def __getitem__(self, key: str) -> Any:
        return self._nodes if key == "nodes" else self._head[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._head
        yield "nodes"

    def __len__(self) -> int:
        return len(self._head) + 1

    # convenience accessors
    @property
    def meta(self) -> dict:
        return self._head.get("meta") or {}

    @property
    def players(self) -> list:
        return self._head.get("players") or []

    @property
    def combos(self) -> list:
        return self._head["combos"]

    @property
    def root(self) -> str:
        return self._head["root"]

    def node(self, nid: str) -> dict:
        return self._nodes[nid]

    def has(self, nid: str) -> bool:
        return nid in self._nodes

    def parent_of(self, nid: str) -> tuple[str, str] | None:
        """(parent id, action token or dealt card) from the index, None for the root."""
        p = self._parent.get(nid)
        return (p[0], p[1]) if p else None

    def dangling_children(self) -> list[str]:
        return [c for c in self._parent if c not in self._nodes]
