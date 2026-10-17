"""Study-UI data (Plan 7, Strategy tab): the saved-solves library, per-player ranges at a node,
the heads-up preflop line catalog with its ranges, and the solve presets."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pn import ranges
from pn.adapter.state import Line
from pn.solve import docstore
from pn.solve.build import DEFAULT_SIZES
from pn.solve.request import LINES, POSTFLOP_SEAT_ORDER, SOLVE_PRESETS, TABLE_SEATS, new_request
from pn.solve.sources import seat_range
from pn.web.solves import CACHE_PREFIX, Registry, SolveNotFound, board_of

META_HEAD_BYTES = 8192  # the document's `meta` object is read from the start of the file
# Pot type from the pot per player in big blinds (pn.train.leaks thresholds)
POT_TYPES = ((1.5, "limped"), (4.5, "srp"), (13.0, "3bp"))
ROUND = 5  # significant digits of per-combo weights (contracts/strategy_output.md)


# ------------------------------------------------------------------ saved solves library
def pot_type(pot: int, bb: int, n_players: int) -> str:
    per = pot / max(1, bb) / max(1, n_players)
    return next((name for limit, name in POT_TYPES if per <= limit), "4bp+")


def read_meta(path: Path) -> dict:
    """`meta` of a strategy document from its first bytes ({} when it is not at the start)."""
    try:
        with docstore.open_binary(path) as f:
            head = f.read(META_HEAD_BYTES).decode("utf-8", "ignore")
    except (OSError, EOFError):
        return {}
    at = head.find('"meta"')
    if at < 0:
        return {}
    start = head.find("{", at)
    try:
        meta, _ = json.JSONDecoder().raw_decode(head, start)
    except (json.JSONDecodeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _row_from_toml(key: str, spot: Mapping[str, Any]) -> dict:
    game, players = spot.get("game") or {}, spot.get("players") or []
    bb = int(game.get("bb") or 100)
    pot = int(game.get("pot") or 0)
    stacks = [int(p.get("stack") or 0) for p in players]
    eff = min(stacks) if stacks else 0
    hero = next((p.get("position") for p in players if p.get("hero")), None)
    return {
        "key": key, "street": game.get("street"), "board": list(game.get("board") or []),
        "positions": [p.get("position") for p in players], "hero": hero, "pot": pot, "bb": bb,
        "pot_bb": round(pot / bb, 2), "stack_bb": round(eff / bb, 1),
        "depth_bb": round((eff + pot / max(1, len(players))) / bb),
        "pot_type": pot_type(pot, bb, len(players)), "sizes": spot.get("sizes") or {},
        "max_iterations": game.get("max_iterations"),
        "target_exploitability": game.get("target_exploitability"),
    }  # fmt: skip


def library_rows(registry: Registry) -> list[dict]:
    """Every SolveCache document with its spot TOML, newest first. `name` opens it in the
    browser (the indexed name when one maps to the key, else `cache-<key prefix>`); `parent`
    is set for subgames solved from the UI."""
    root = registry.cache_root()
    index = registry.index()
    by_key: dict[str, tuple[str, dict]] = {}
    for n, e in index.items():
        if e.get("key") and (e["key"] not in by_key or e.get("parent") is None):
            by_key[e["key"]] = (n, e)
    rows = []
    for key, path in docstore.doc_files(root).items():
        try:
            spot = tomllib.loads((root / f"{key}.toml").read_text())
            st = path.stat()
        except (OSError, tomllib.TOMLDecodeError):
            continue
        name, entry = by_key.get(key, (f"{CACHE_PREFIX}{key[:16]}", {}))
        meta = read_meta(path)
        rows.append({
            "name": name, **_row_from_toml(key, spot),
            "source": entry.get("source", "cache"), "parent": entry.get("parent"),
            "line": (entry.get("spot") or {}).get("line"),
            "backend": meta.get("backend"), "iterations": meta.get("iterations"),
            "exploitability_pct_pot": meta.get("exploitability_pct_pot"),
            "stop": meta.get("stop"), "wall_sec": meta.get("wall_sec"),
            "size_bytes": st.st_size,
            "date": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
        })  # fmt: skip
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


# ------------------------------------------------------------------ ranges at a node
def _round(xs: list[float]) -> list[float]:
    return [float(f"{x:.{ROUND}g}") if x else 0 for x in xs]


def _first_reach(nodes: Mapping[str, Any], root: str, player: int) -> list[float] | None:
    """`player`'s range: its reach at the first node down the first-child line where it acts
    (it has not acted before that node), or reach_by_player on the way."""
    nid: str | None = root
    for _ in range(64):
        node = nodes[nid]
        rbp = node.get("reach_by_player")
        if rbp and player < len(rbp) and rbp[player] is not None:
            return list(rbp[player])
        if node.get("type") == "action" and node.get("player") == player and node.get("reach"):
            return list(node["reach"])
        nxt = (node.get("actions") or [{}])[0].get("child") if node.get("type") == "action" \
            else next(iter((node.get("children") or {}).values()), None)  # fmt: skip
        if nxt is None:
            return None
        nid = nxt
    return None


def _walk(doc: Mapping[str, Any], tokens: list[str],
          visit: Callable[[Mapping[str, Any], int, list, list[str]], None] | None = None,
          ) -> tuple[str, list[list[float] | None], list[str]]:  # fmt: skip
    """(node id, every player's reach, chance cards dealt) at `tokens`; `visit(node, k, reach,
    dealt)` sees each action node on the way before action `k`'s strategy applies."""
    nodes, root = doc["nodes"], doc["root"]
    combos = doc["combos"]
    n = len(doc.get("players") or []) or 2
    reach = [_first_reach(nodes, root, p) for p in range(n)]
    dealt: list[str] = []
    nid = root
    for i, tok in enumerate(tokens):
        node = nodes[nid]
        _adopt(node, reach)
        if node.get("type") == "action":
            k = next((j for j, a in enumerate(node["actions"]) if a["token"] == tok), None)
            if k is None:
                raise SolveNotFound(f"path {tokens[: i + 1]} does not exist")
            if visit is not None:
                visit(node, k, reach, dealt)
            p = node["player"]
            strat = node.get("strategy")
            if reach[p] is not None and strat is not None:
                reach[p] = [r * row[k] for r, row in zip(reach[p], strat, strict=True)]
            nid = node["actions"][k]["child"]
        elif node.get("type") == "chance" and tok in (node.get("children") or {}):
            reach = [None if r is None else [0.0 if tok in c else w for c, w in zip(combos, r, strict=True)]
                     for r in reach]  # fmt: skip
            dealt.append(tok)
            nid = node["children"][tok]
        else:
            raise SolveNotFound(f"path {tokens[: i + 1]} does not exist")
    _adopt(nodes[nid], reach)
    return nid, reach, dealt


def node_ranges(doc: Mapping[str, Any], tokens: list[str]) -> dict:
    """Every player's combo weights reaching the node at `tokens` (range weight x own strategy
    product along the line; dealt cards zero the combos they block). A node's own
    reach_by_player / reach win when present. SolveNotFound for a path that does not exist."""
    nid, reach, _ = _walk(doc, tokens)
    node = doc["nodes"][nid]
    n = len(reach)
    players = doc.get("players") or [{"position": f"P{p}"} for p in range(n)]
    return {"id": nid, "type": node.get("type"), "player": node.get("player"),
            "players": [{"index": p, "position": players[p].get("position"),
                         "reach": None if reach[p] is None else _round(reach[p])}
                        for p in range(n)]}  # fmt: skip


def line_breakdown(doc: Mapping[str, Any], tokens: list[str], parent: dict | None = None) -> dict:
    """How the actor's range split over its actions at every action node on the line to
    `tokens` (the final node excluded), per hand class: `reach` entering (the node_ranges walk,
    board and dealt cards zeroed), `n` live combos and reach-weighted `freq` per action."""
    combos = doc["combos"]
    cls = [ranges.combo_class(c) for c in combos]
    board = board_of(doc)
    positions = [p.get("position") for p in doc.get("players") or []]
    steps: list[dict] = []

    def visit(node: Mapping[str, Any], k: int, reach: list, dealt: list[str]) -> None:
        p, acts = node["player"], node["actions"]
        dead = {*board, *dealt}
        r, strat = reach[p], node.get("strategy")
        out = {c: [0.0, 0, [0.0] * len(acts)] for c in ranges.all_classes()}
        for i, combo in enumerate(combos):
            if combo[:2] in dead or combo[2:] in dead:
                continue
            row = out[cls[i]]
            row[1] += 1
            w = r[i] if r is not None else 0.0
            row[0] += w
            if w and strat is not None:
                row[2] = [f + w * x for f, x in zip(row[2], strat[i], strict=True)]
        steps.append({"position": positions[p] if p < len(positions) else f"P{p}",
                      "actions": [a["token"] for a in acts], "taken": acts[k]["token"],
                      "classes": {c: {"reach": _round([w])[0], "n": m,
                                      "freq": _round([f / w for f in fs]) if w else [0] * len(fs)}
                                  for c, (w, m, fs) in out.items()}})  # fmt: skip

    dealt = _walk(doc, tokens, visit)[2]
    return {"board": [*board, *dealt], "parent": parent, "steps": steps}


def _adopt(node: Mapping[str, Any], reach: list[list[float] | None]) -> None:
    """Authoritative weights stored on the node replace the walked ones."""
    rbp = node.get("reach_by_player")
    if rbp:
        for p, r in enumerate(rbp[: len(reach)]):
            reach[p] = list(r) if r is not None else None
    elif node.get("type") == "action" and node.get("reach") and node.get("player") is not None:
        reach[node["player"]] = list(node["reach"])


# ------------------------------------------------------------------ preflop lines (builder)
def _oop_ip(a: str, b: str) -> tuple[str, str]:
    return (a, b) if POSTFLOP_SEAT_ORDER.index(a) < POSTFLOP_SEAT_ORDER.index(b) else (b, a)


def line_catalog(table: int) -> list[dict]:
    """Heads-up preflop lines at `table`: `a` opens and `b` calls (srp), or `b` 3-bets and `a`
    calls (3bp), for every seat pair with `a` before `b` in preflop order."""
    if table not in TABLE_SEATS:
        raise ValueError(f"table must be one of {sorted(TABLE_SEATS)}")
    seats = TABLE_SEATS[table]
    out = []
    for line in LINES:
        for i, a in enumerate(seats):
            for b in seats[i + 1:]:
                if a == "BB":
                    continue
                oop, ip = _oop_ip(a, b)
                label = f"{a} opens, {b} calls" if line == "srp" else f"{a} opens, {b} 3-bets, {a} calls"
                out.append({"id": f"{line}:{a}:{b}", "line": line, "positions": [a, b],
                            "oop": oop, "ip": ip, "label": label})  # fmt: skip
    return out


def line_ranges(table: int, depth: int, line: str, positions: list[str]) -> dict:
    """Pot, stacks and both chart ranges (before board-card removal) for a heads-up line; the
    same ranges pn.solve.build uses for source `gto`. ValueError on a bad line or seats."""
    if line not in LINES:
        raise ValueError(f"line must be one of {LINES}")
    if len(positions) != 2:
        raise ValueError("a line needs exactly two positions")
    req = new_request(list(positions), depth, table=table, source="gto",
                      board=("2c", "3d", "4h"), line=line)  # fmt: skip (board: postflop street only)
    behind = sorted(s.stack for s in req.seats)
    depth_bb = max(1.0, behind[-2] / req.bb)
    three = next((s.position for s in req.seats if s.line == "3bet"), None)
    oop, ip = _oop_ip(*positions)
    out: dict[str, Any] = {"table": table, "depth": depth, "line": line, "pot": req.pot,
                           "bb": req.bb, "stack": min(behind)}  # fmt: skip
    for side, pos in (("oop", oop), ("ip", ip)):
        seat = next(s for s in req.seats if s.position == pos)
        rng, origin = seat_range(position=pos, line=Line(seat.line, seat.vs) if seat.line else None,
                                 table=req.table, depth_bb=depth_bb, three_bettor=three)  # fmt: skip
        out[side] = {"position": pos, "stack": seat.stack, "range": ranges.to_text(rng),
                     "origin": origin, "combos": round(sum(rng.values()), 1)}  # fmt: skip
    return out


# ------------------------------------------------------------------ presets
def presets() -> dict:
    """Solve presets (budget per name) and the default postflop sizing for the custom editor."""
    rows = []
    for name, p in SOLVE_PRESETS.items():
        b = p.budget
        rows.append({"name": name, "max_iterations": b.max_iterations,
                     "accuracy_pct_pot": b.target_exploitability,
                     "time_limit_sec": b.time_limit_sec, "threads": b.threads,
                     "max_bets_per_street": getattr(p, "hu_max_bets", None)})  # fmt: skip
    return {"presets": rows, "default_sizes": dict(DEFAULT_SIZES)}
