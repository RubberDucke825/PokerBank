"""Spot keys for leak tracking (contracts/trainer.md "Plan 6 additions").

Preflop  `pf|<pos>|<faced>|<vs>`: faced = rfi | vs_limp | vs_open | vs_3bet | vs_4bet+ | cold by
the raises before the decision (vs_3bet when the actor raised earlier, else cold), vs = the
last aggressor's position or "-".
Postflop `post|<pot>|<ip|oop>|<street>|<first|facing_bet|facing_raise>`: pot = limped | srp |
3bp | 4bp+ by the number of preflop raises; ip = last to act postflop among the players still
in; the situation counts bets and raises on the street so far.

`spot_key(decision)` takes a mapping with `street`, `position` and either `faced`/`vs`
(preflop) or `pot`/`ip`/`situation` (postflop); `preflop_fields` / `postflop_fields` build them
from action histories, `from_state` from a trainer engine state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pn.adapter.state import order_key

FACED = ("rfi", "vs_limp", "vs_open", "vs_3bet", "vs_4bet+", "cold")
POTS = ("limped", "srp", "3bp", "4bp+")
SITUATIONS = ("first", "facing_bet", "facing_raise")


def preflop_fields(position: str, raisers: Sequence[str], limpers: int) -> dict[str, Any]:
    """faced / vs for `position` given the preflop raisers so far (in order) and limpers."""
    n = len(raisers)
    if n == 0:
        return {"faced": "vs_limp" if limpers else "rfi", "vs": "-"}
    if n == 1:
        faced = "vs_open"
    elif n == 2:
        faced = "vs_3bet" if position in raisers else "cold"
    else:
        faced = "vs_4bet+"
    return {"faced": faced, "vs": raisers[-1]}


def pot_type(preflop_raises: int) -> str:
    return POTS[min(preflop_raises, 3)]


def line_key(actions: Iterable[tuple[str, str]]) -> str:
    """A preflop line as `<pos> <kind>, ...` over its non-fold actions (allin counts as raise):
    the key of heads-up flop frequencies (pn.train.leaks FreqMap.matchups)."""
    return ", ".join(f"{p} {'raise' if k == 'allin' else k}" for p, k in actions if k != "fold")


def is_ip(position: str, live: Iterable[str]) -> bool:
    """Last to act postflop among the live positions (which include `position`)."""
    return all(order_key(position, "flop") >= order_key(p, "flop") for p in live)


def postflop_fields(position: str, preflop_raises: int, live: Iterable[str],
                    street_bets: int) -> dict[str, Any]:  # fmt: skip
    return {"pot": pot_type(preflop_raises), "ip": is_ip(position, live),
            "situation": SITUATIONS[min(street_bets, 2)]}  # fmt: skip


def spot_key(decision: Mapping[str, Any]) -> str:
    d = decision
    if d["street"] == "preflop":
        return f"pf|{d['position']}|{d['faced']}|{d.get('vs') or '-'}"
    side = "ip" if d["ip"] else "oop"
    return f"post|{d['pot']}|{side}|{d['street']}|{d['situation']}"


def parse(spot: str) -> dict[str, Any]:
    """Inverse of spot_key."""
    parts = spot.split("|")
    if parts[0] == "pf" and len(parts) == 4:
        return {"street": "preflop", "position": parts[1], "faced": parts[2], "vs": parts[3]}
    if parts[0] == "post" and len(parts) == 5 and parts[2] in ("ip", "oop"):
        return {"street": parts[3], "pot": parts[1], "ip": parts[2] == "ip",
                "situation": parts[4]}  # fmt: skip
    raise ValueError(f"not a spot key: {spot!r}")


_FACED_TEXT = {"rfi": "first in", "vs_limp": "vs limp", "vs_open": "vs {vs} open",
               "vs_3bet": "vs {vs} 3-bet", "vs_4bet+": "vs {vs} 4-bet+", "cold": "vs {vs} 3-bet cold"}  # fmt: skip
_POT_TEXT = {"limped": "Limped", "srp": "SRP", "3bp": "3BP", "4bp+": "4BP+"}
_SIT_TEXT = {"first": "first to act", "facing_bet": "facing a bet", "facing_raise": "facing a raise"}


def label(spot: str) -> str:
    """Short human text, e.g. "BTN vs CO open" or "SRP IP flop facing a bet"."""
    d = parse(spot)
    if d["street"] == "preflop":
        return f"{d['position']} {_FACED_TEXT[d['faced']].format(vs=d['vs'])}"
    return (f"{_POT_TEXT[d['pot']]} {'IP' if d['ip'] else 'OOP'} {d['street']} "
            f"{_SIT_TEXT[d['situation']]}")  # fmt: skip


def from_state(st: Any, i: int) -> str:
    """Spot key of seat `i` deciding in a pn.train.engine State."""
    pos = st.positions
    pre = [a for a in st.history if a.street == "preflop"]
    raisers = [pos[a.seat] for a in pre if a.kind == "raise"]
    if st.street == "preflop":
        limpers = sum(1 for a in pre if a.kind == "call") if not raisers else 0
        return spot_key({"street": "preflop", "position": pos[i],
                         **preflop_fields(pos[i], raisers, limpers)})  # fmt: skip
    live = [p for k, p in enumerate(pos) if not st.folded[k]]
    return spot_key({"street": st.street, "position": pos[i],
                     **postflop_fields(pos[i], len(raisers), live, st.bets)})  # fmt: skip
