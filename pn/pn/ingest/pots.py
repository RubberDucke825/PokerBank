"""Side-pot layering and assignment of PokerNow 'collected' lines to pots.

PokerNow prints one 'collected' line per pot per winner (per run) with no pot label,
main pot first. We rebuild the pot layers from contributions and greedily match
consecutive collected lines to each layer. If the lines do not tile the layers
exactly (dead money quirks, odd-chip splits) we fall back to a single pot that
lists every collected line, so totals are always preserved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pn.ingest.lines import Event


def pot_layers(
    contributed: Mapping[str, int], returned: Mapping[str, int], folded: Iterable[str]
) -> list[dict[str, Any]]:
    """Return [{amount, eligible}] from main pot upwards."""
    folded = set(folded)
    net_in = {p: contributed.get(p, 0) - returned.get(p, 0) for p in contributed}
    live_levels = sorted({v for p, v in net_in.items() if p not in folded and v > 0})
    layers: list[dict[str, Any]] = []
    prev = 0
    for level in live_levels:
        amount = sum(min(v, level) - min(v, prev) for v in net_in.values())
        eligible = sorted(p for p, v in net_in.items() if p not in folded and v >= level)
        if layers and layers[-1]["eligible"] == eligible:
            layers[-1]["amount"] += amount
        else:
            layers.append({"amount": amount, "eligible": eligible})
        prev = level
    # Money above the highest live level (folded over-contributions) joins the top layer.
    leftover = sum(max(v - prev, 0) for v in net_in.values())
    if layers:
        layers[-1]["amount"] += leftover
    elif leftover:
        layers.append({"amount": leftover, "eligible": []})
    return layers


def _winner(ev: Event) -> dict[str, Any]:
    assert ev.player is not None
    entry = {"player_id": ev.player.player_id, "amount": ev.amount, "hand_desc": ev.data.get("hand_desc")}
    if ev.data.get("run", 1) != 1:
        entry["run"] = ev.data["run"]
    return entry


def assign_pots(
    contributed: Mapping[str, int],
    returned: Mapping[str, int],
    folded: Iterable[str],
    collected: list[Event],
) -> list[dict[str, Any]]:
    if not collected:
        return []
    layers = pot_layers(contributed, returned, folded)
    pots: list[dict[str, Any]] = []
    idx = 0
    for layer in layers:
        winners: list[dict[str, Any]] = []
        total = 0
        while idx < len(collected) and total < layer["amount"]:
            winners.append(_winner(collected[idx]))
            total += collected[idx].amount
            idx += 1
        if total != layer["amount"]:
            break
        pots.append({"amount": total, "winners": winners})
    else:
        if idx == len(collected):
            return pots
    return [{"amount": sum(ev.amount for ev in collected), "winners": [_winner(ev) for ev in collected]}]
