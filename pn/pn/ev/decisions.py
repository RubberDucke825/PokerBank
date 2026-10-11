"""Gradable decisions: every voluntary action of a player whose hole cards are known (hero
from the CSV export, villains from showdowns), with its raw tree token and spot label."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pn import ranges
from pn.profile.spots import NBET, pos_key
from pn.profile.walk import VOLUNTARY, Decision, _known_cards, _walk_decisions
from pn.train import spots


@dataclass(frozen=True)
class GradableDecision:
    hand_id: str
    seq: int
    street: str
    player_id: str
    combo: str  # canonical, e.g. AhKd
    token: str  # raw token (fold|check|call|bet<pct>|raise<pct>|allin); grading snaps it
    spot: str  # pn.train.spots.spot_key (pf|... / post|...)
    situ: tuple | None  # preflop situation tuple for GTO Wizard lookups
    action: Mapping[str, Any]  # the hand's action dict


def raw_token(d: Decision) -> str:
    """Approximate tree token of an action (rows that are never solved keep this one)."""
    if d.allin and d.kind in ("bet", "raise"):
        return "allin"
    if d.kind == "bet":
        return f"bet{round(d.bet_pct or 0)}"
    if d.kind == "raise":
        if d.street == "preflop":
            return f"raise{round(100 * d.to_amount / d.level_before) if d.level_before else 0}"
        base = d.pot_before + d.facing
        return f"raise{round(100 * (d.to_amount - d.level_before) / base) if base else 0}"
    return d.kind


def _preflop_situ(pre: list[Decision], i: int, key: Mapping[str, str],
                  opener: str | None, three_bettors: set[str]) -> tuple | None:  # fmt: skip
    """Per-decision version of the situation tuples in pn.profile.spots._preflop."""
    d = pre[i]
    P = key[d.player]
    first = not any(x.player == d.player for x in pre[:i])
    clean = not any(x.kind == "call" for x in pre[:i])
    if d.n_aggr_before == 0:
        if not first:
            return None
        limpers = [key[x.player] for x in pre[:i] if x.kind == "call"]
        if not limpers:
            return ("rfi", P)
        if P in ("BB", "BB_HU") and limpers in (["SB"], ["BTN_HU"]):
            return ("vs_limp", P)
        return None
    raisers = [x.player for x in pre[:i] if x.kind == "raise"]
    if d.n_aggr_before == 1:
        limpers = [key[x.player] for x in pre[:i] if x.kind == "call"]
        if not first and P == "SB" and key[raisers[0]] == "BB" and limpers == ["SB"]:
            return ("limp_vs_raise", "SB", "BB")  # PokerCoaching only (no GTO Wizard key)
        return ("vs_open", key[raisers[0]], P) if first and clean else None
    if d.n_aggr_before == 2:
        if first:
            return ("cold", key[raisers[0]], key[raisers[1]], P) if clean else None
        return ("vs_3bet", P, key[raisers[1]]) if d.player == opener and clean else None
    if d.n_aggr_before not in NBET:
        return None
    who, kind, _ = NBET[d.n_aggr_before]
    actor_ok = d.player == opener if who == "opener" else d.player in three_bettors
    o, t = raisers[0], raisers[1]
    war = all(r == (o if k % 2 == 0 else t) for k, r in enumerate(raisers))
    return (kind, key[o], key[t]) if actor_ok and war and clean and o == opener else None


def _spot_key(d: Decision, position: Mapping[str, str], before: list[Decision],
              folded: set[str]) -> str:  # fmt: skip
    """pn.train.spots key; `before` = the preflop decisions before this one (preflop) or all
    preflop decisions (postflop); `folded` = players folded before this action."""
    raisers = [position[x.player] for x in before if x.kind == "raise"]
    me = position[d.player]
    if d.street == "preflop":
        limpers = 0 if raisers else sum(1 for x in before if x.kind == "call")
        return spots.spot_key({"street": "preflop", "position": me,
                               **spots.preflop_fields(me, raisers, limpers)})  # fmt: skip
    live = [pos for pid, pos in position.items() if pid not in folded]
    return spots.spot_key({"street": d.street, "position": me,
                           **spots.postflop_fields(me, len(raisers), live, d.n_aggr_before)})  # fmt: skip


def enumerate_decisions(hand: Mapping[str, Any],
                        players: set[str] | None = None) -> list[GradableDecision]:  # fmt: skip
    """Voluntary decisions of players with two known hole cards (optionally only `players`)."""
    cards = _known_cards(dict(hand))
    if players is not None:
        cards = {p: c for p, c in cards.items() if p in players}
    if not cards:
        return []
    acts = [a for a in sorted(hand["actions"], key=lambda a: a["seq"]) if a["kind"] in VOLUNTARY]
    decs = _walk_decisions(dict(hand))  # one Decision per voluntary action, same order
    n = len(hand["players"])
    key = {p["player_id"]: pos_key(p.get("position") or "?", n) for p in hand["players"]}
    pre = [d for d in decs if d.street == "preflop"]  # a prefix of decs
    raisers = [d.player for d in pre if d.kind == "raise"]
    opener = raisers[0] if raisers else None
    three_bettors = {d.player for d in pre if d.kind == "raise" and d.n_aggr_before == 1}
    position = {p["player_id"]: p.get("position") or "?" for p in hand["players"]}
    out: list[GradableDecision] = []
    folded: set[str] = set()
    for k, (a, d) in enumerate(zip(acts, decs, strict=True)):
        if d.player not in cards:
            folded |= {d.player} if d.kind == "fold" else set()
            continue
        situ = _preflop_situ(pre, k, key, opener, three_bettors) if d.street == "preflop" else None
        spot = _spot_key(d, position, pre[:k] if d.street == "preflop" else pre, folded)
        folded |= {d.player} if d.kind == "fold" else set()
        combo = ranges.canonical_combo("".join(cards[d.player]))
        out.append(GradableDecision(hand["hand_id"], int(a["seq"]), d.street, d.player, combo,
                                    raw_token(d), spot, situ, a))  # fmt: skip
    return out
