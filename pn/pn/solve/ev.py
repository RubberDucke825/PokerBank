"""EV loss of an action from a strategy document's `action_ev` table."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class EvUnavailable(ValueError):
    """The document or node has no usable action EVs for the combo."""


def combo_position(doc: Mapping[str, Any], combo: str) -> int:
    want = {combo[:2], combo[2:]}
    for i, c in enumerate(doc["combos"]):
        if {c[:2], c[2:]} == want:
            return i
    raise EvUnavailable(f"unknown combo {combo!r}")


def _range_weight(doc: Mapping[str, Any], node: Mapping[str, Any], i: int) -> float:
    """Acting player's range weight for combo i at the root (board cards are zero there).
    action_ev is counterfactual: filled even where the player's own strategy never reaches."""
    root = doc["nodes"][doc["root"]].get("reach_by_player")
    if root is not None and root[node["player"]] is not None:
        return float(root[node["player"]][i])
    reach = node.get("reach")  # documents without (or with sparse) reach_by_player
    return float(reach[i]) if reach is not None else 1.0


def _node_reach(node: Mapping[str, Any], i: int) -> float:
    reach = node.get("reach")
    if reach is None:  # --compact drops `reach` for non-hero nodes
        rows = node.get("reach_by_player")
        reach = rows[node["player"]] if rows is not None else None
    if reach is None:
        raise EvUnavailable("node carries no reach for the acting player (sparse document)")
    return float(reach[i])


def action_ev_loss(doc: Mapping[str, Any], node_id: str, combo: str,
                   chosen_token: str) -> tuple[float, str]:  # fmt: skip
    """(loss in cents, best token): max(action_ev[c]) - action_ev[c][chosen]. Raises
    EvUnavailable without action_ev, for a combo outside the actor's range, and (hu backend
    only) for a combo whose own reach at the node is 0: hu emits zeros there, not
    counterfactual values. mwsolver and dcfr action_ev is counterfactual for every in-range
    combo, so off-path combos still get a loss."""
    node = doc["nodes"].get(node_id)
    if node is None or node.get("type") != "action":
        raise EvUnavailable(f"{node_id} is not an action node")
    table = node.get("action_ev")
    if table is None:
        raise EvUnavailable(f"node {node_id} has no action_ev: the document predates "
                            "contracts/strategy_output.md action_ev (re-solve it)")  # fmt: skip
    tokens = [a["token"] for a in node["actions"]]
    if chosen_token not in tokens:
        raise EvUnavailable(f"{chosen_token!r} is not an action at {node_id}: {tokens}")
    i = combo_position(doc, combo)
    if _range_weight(doc, node, i) <= 0:
        raise EvUnavailable(f"{combo} is not in the acting player's range (or board-blocked)")
    if doc.get("meta", {}).get("backend") == "hu" and _node_reach(node, i) <= 0:
        raise EvUnavailable(f"{combo} does not reach {node_id}: hu has no counterfactual "
                            "action_ev there (crates/hu/DEVIATIONS.md)")  # fmt: skip
    evs = [float(x) for x in table[i]]
    best = max(range(len(evs)), key=evs.__getitem__)
    return evs[best] - evs[tokens.index(chosen_token)], tokens[best]
