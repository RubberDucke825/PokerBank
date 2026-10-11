"""Lift the end of a street in a solved document into the next street's spot.

The node is where a street ends: a depth-limited `leaf` terminal or a full-mode `chance` node.
Ranges are the players' `reach_by_player` weights there (scaled to max 1, board/dead cards
removed; a null row = folded); pot and stacks come from the node. When the node carries no
`reach_by_player`, they are derived from the nearest ancestor action node that has it (reach x
each later action's strategy; the closing actor's stack reduced by the pot growth). Folded players are found by walking the tree from the root.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pn import ranges
from pn.adapter import spot_ranges as sr
from pn.adapter.state import BOARD_LEN
from pn.solve.build import build_spot
from pn.solve.request import Seat, SpotRequest
from pn.solve.sources import ProfileSource

NEXT_STREET = {"preflop": "flop", "flop": "turn", "turn": "river"}
AGGRESSIVE_TOKENS = ("bet", "raise", "allin")
WEIGHT_DECIMALS = 4


class SubgameError(ValueError):
    """The node cannot be lifted into a next-street spot."""


def path_to(doc: Mapping[str, Any], node_id: str) -> list[tuple[str, str]]:
    """[(node id, action token or dealt card)] from the root down to `node_id`."""
    if hasattr(doc, "parent_of"):  # LazyDoc: parent edges come from its index
        return _lazy_path(doc, node_id)
    parent: dict[str, tuple[str, str]] = {}
    for nid, node in doc["nodes"].items():
        for a in node.get("actions") or []:
            parent[a["child"]] = (nid, a["token"])
        for card, child in (node.get("children") or {}).items():
            parent[child] = (nid, card)
    if node_id not in doc["nodes"]:
        raise SubgameError(f"no node {node_id!r}")
    path: list[tuple[str, str]] = []
    nid = node_id
    while nid != doc["root"]:
        if nid not in parent:
            raise SubgameError(f"node {node_id} is not reachable from the root")
        nid, tok = parent[nid]
        path.append((nid, tok))
    return path[::-1]


Rows = list[list[float] | None]  # reach_by_player; a folded player's row may be null


def _lazy_path(doc: Any, node_id: str) -> list[tuple[str, str]]:
    if not doc.has(node_id):
        raise SubgameError(f"no node {node_id!r}")
    path: list[tuple[str, str]] = []
    nid = node_id
    while nid != doc.root:
        edge = doc.parent_of(nid)
        if edge is None:
            raise SubgameError(f"node {node_id} is not reachable from the root")
        nid = edge[0]
        path.append(edge)
    return path[::-1]


def _node_state(doc: Mapping[str, Any], node_id: str,
                path: list[tuple[str, str]]) -> tuple[Rows, list[int], str]:  # fmt: skip
    """(reach_by_player, stacks, street of the last action) at `node_id`. Uses the node's own
    fields when present (leaf / chance nodes); otherwise starts from the nearest ancestor
    action node that carries reach_by_player (hero nodes in sparse documents) and multiplies
    each later actor's row by its strategy for the action taken."""
    node = doc["nodes"][node_id]
    prev = doc["nodes"][path[-1][0]] if path else None
    street = node.get("street") if node.get("type") != "chance" else None
    if street is None and prev is not None:
        street = prev.get("street")
    if node.get("reach_by_player") is not None and node.get("stacks") is not None:
        return node["reach_by_player"], list(node["stacks"]), street
    if prev is None or prev.get("type") != "action":
        raise SubgameError(f"node {node_id} has no reach_by_player (solve with a newer binary)")
    start = next((k for k in range(len(path) - 1, -1, -1)
                  if doc["nodes"][path[k][0]].get("reach_by_player") is not None), None)  # fmt: skip
    if start is None:
        raise SubgameError(f"no ancestor of {node_id} carries reach_by_player")
    reach: Rows = [None if r is None else list(r)
                   for r in doc["nodes"][path[start][0]]["reach_by_player"]]  # fmt: skip
    for nid, token in path[start:]:
        n = doc["nodes"][nid]
        if n.get("type") != "action":
            raise SubgameError(f"cannot derive reach across {n.get('type')} node {nid}")
        row = reach[n["player"]]
        if row is None:
            continue
        strat = n.get("strategy")
        if strat is None:
            raise SubgameError(f"node {nid} has no strategy (document solved --compact?): "
                               f"cannot derive reach at {node_id}")  # fmt: skip
        idx = [a["token"] for a in n["actions"]].index(token)
        reach[n["player"]] = [w * st[idx] for w, st in zip(row, strat, strict=True)]
    stacks = list(prev["stacks"])
    stacks[prev["player"]] -= int(node.get("pot", prev["pot"])) - int(prev["pot"])
    return reach, stacks, street


def _range_text(weights: list[float], dead: list[str]) -> str:
    top = max(weights, default=0.0)
    if top <= 0:
        return ""
    rng = {c: round(w / top, WEIGHT_DECIMALS) for c, w in ranges.from_dense(weights).items()}
    return ranges.to_text(sr.remove_dead({c: w for c, w in rng.items() if w > 0}, dead))


def subgame_request(parent: Mapping[str, Any], node_id: str, next_cards: tuple[str, ...],
                    req_hint: SpotRequest) -> SpotRequest:  # fmt: skip
    """Next-street SpotRequest at `node_id` of `parent` (solved from `req_hint`)."""
    node = parent["nodes"].get(node_id)
    path = path_to(parent, node_id)
    if node is None or not (node.get("type") == "chance" or node.get("kind") == "leaf"):
        raise SubgameError(f"node {node_id} does not end a street (need a leaf or chance node)")
    reach, stacks, ended = _node_state(parent, node_id, path)
    street = node["street"] if node.get("type") == "chance" else NEXT_STREET.get(ended or "")
    if street is None:
        raise SubgameError(f"no street after {ended!r}")
    board = (*req_hint.board, *next_cards)
    if len(board) != BOARD_LEN[street]:
        raise SubgameError(f"{street} needs {BOARD_LEN[street]} board cards, got {list(board)}")
    folded: set[int] = set()
    aggressor: int | None = None
    for nid, tok in path:
        n = parent["nodes"][nid]
        if n.get("type") != "action":
            continue
        if tok == "fold":
            folded.add(n["player"])
        if n.get("street") == ended and tok.startswith(AGGRESSIVE_TOKENS):
            aggressor = n["player"]
    by_pos = {s.position: s for s in req_hint.seats}
    dead = [*board, *req_hint.hero_cards]
    seats: list[Seat] = []
    for i, p in enumerate(parent["players"]):
        if i in folded or reach[i] is None or sum(reach[i]) <= 0:
            continue
        hint = by_pos.get(p["position"])
        hero = bool(p.get("hero"))
        text = _range_text(reach[i], [*board] if hero else dead)
        if not text:
            raise SubgameError(f"{p['name']}: empty range after dead-card removal")
        if stacks[i] <= 0:
            raise SubgameError(f"{p['name']} is all-in: no {street} decision to solve")
        seats.append(Seat(hint.player_id if hint else None, p["position"], int(stacks[i]),
                          hero=hero, name=p["name"], range=text))  # fmt: skip
    if len(seats) < 2:
        raise SubgameError(f"only {len(seats)} player(s) left at node {node_id}")
    in_pos = {s.position for s in seats}
    la = parent["players"][aggressor]["position"] if aggressor is not None else None
    return replace(req_hint, seats=tuple(seats), street=street, board=board,
                   pot=int(node.get("pot") or parent["nodes"][path[-1][0]]["pot"]),
                   overrides={}, last_aggressor=la if la in in_pos else None,
                   mode="depth_limited")  # fmt: skip


def subgame(parent: Mapping[str, Any], node_id: str, next_cards: tuple[str, ...],
            req_hint: SpotRequest, profiles: ProfileSource | None = None) -> dict[str, Any]:
    """Next-street tree_config at `node_id`; villain locks follow `req_hint.source`."""
    return build_spot(subgame_request(parent, node_id, next_cards, req_hint), profiles)
