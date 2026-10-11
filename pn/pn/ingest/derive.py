"""Derived per-hand fields stored in SQLite (not part of the hand JSON).

Definitions (documented in schema.sql too):
- vpip: player voluntarily called/bet/raised preflop (blind/straddle/ante posts excluded).
- pfr: player bet or raised preflop.
- saw_flop: a flop was dealt (any run) and the player had not folded preflop.
- went_to_sd: player had not folded at hand end and >= 2 players had not folded.
- won_sd: went_to_sd and collected chips from a pot.
- pot_before: chips in the middle (all streets, dead money included) before the action.
- n_active: players not folded (all-in players included) before the action.
- facing_bet_pct: size of the last bet/raise on this street that the actor still faces,
  as integer percent of the pot it was made into (bet: amount / pot_before;
  raise: raise increment / (pot_before + the raiser's call amount)).
  NULL when the actor faces no bet/raise (preflop blinds alone do not count).
"""

from __future__ import annotations

from typing import Any

_AGGRESSIVE = frozenset({"bet", "raise"})
_VOLUNTARY = frozenset({"call", "bet", "raise"})


def player_flags(hand: dict[str, Any]) -> dict[str, dict[str, bool]]:
    folded_pre: set[str] = set()
    folded: set[str] = set()
    vpip: set[str] = set()
    pfr: set[str] = set()
    for a in hand["actions"]:
        pid = a["player_id"]
        if a["kind"] == "fold":
            folded.add(pid)
            if a["street"] == "preflop":
                folded_pre.add(pid)
        if a["street"] == "preflop" and a["kind"] in _VOLUNTARY:
            vpip.add(pid)
        if a["street"] == "preflop" and a["kind"] in _AGGRESSIVE:
            pfr.add(pid)
    flop_dealt = any(run["board"]["flop"] for run in hand["runs"])
    pids = [p["player_id"] for p in hand["players"]]
    remaining = [p for p in pids if p not in folded]
    showdown = len(remaining) >= 2
    winners = {w["player_id"] for pot in hand["pots"] for w in pot["winners"] if w["amount"] > 0}
    return {
        pid: {
            "vpip": pid in vpip,
            "pfr": pid in pfr,
            "saw_flop": flop_dealt and pid not in folded_pre,
            "went_to_sd": showdown and pid not in folded,
            "won_sd": showdown and pid not in folded and pid in winners,
        }
        for pid in pids
    }


def annotate_actions(hand: dict[str, Any]) -> list[dict[str, Any]]:
    """Return copies of hand['actions'] with pot_before, n_active, facing_bet_pct."""
    pot = 0
    active = {p["player_id"] for p in hand["players"]}
    street = None
    committed: dict[str, int] = {}
    street_max = 0
    last_pct: int | None = None
    out = []
    for a in hand["actions"]:
        if a["street"] != street:
            street, committed, street_max, last_pct = a["street"], {}, 0, None
        pid = a["player_id"]
        mine = committed.get(pid, 0)
        facing = last_pct if (last_pct is not None and mine < street_max) else None
        out.append({**a, "pot_before": pot, "n_active": len(active), "facing_bet_pct": facing})
        if a["kind"] in _AGGRESSIVE and a["to_amount"] > street_max:
            call_part = street_max - mine
            base = pot + call_part
            raise_part = a["to_amount"] - street_max
            last_pct = round(100 * raise_part / base) if base > 0 else None
        pot += a["amount"]
        committed[pid] = a["to_amount"]
        street_max = max(street_max, a["to_amount"])
        if a["kind"] == "fold":
            active.discard(pid)
    return out
