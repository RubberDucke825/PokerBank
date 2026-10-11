"""Pairwise tendencies of player A against each opponent B.

Definitions (A = profiled player, B = opponent, both canonical ids):
  hands_together     hands where both were dealt in (skipped hands excluded)
  vpip_when_in       A's VPIP in those hands (per hand)
  three_bet_vs       A's decisions facing exactly one raise made by B -> A re-raised (per hand)
  fold_to_their_bet  A's postflop decisions facing a bet/raise whose last aggressor is B -> fold
  call_vs_their_bet  same opportunities -> call                       (both per decision)
  net_vs             sum of A's net (cents) over hands where B was involved in the pot, i.e. B
                     put chips in voluntarily preflop or saw the flop; hands_b_involved counts them
Priors are A's own overall posteriors (hierarchy: population -> player -> player-vs-B).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pn.profile.bayes import beta_binomial
from pn.profile.walk import HandFacts

PAIR_STATS = ("vpip_when_in", "three_bet_vs", "fold_to_their_bet", "call_vs_their_bet")


def _bump(d: dict[str, list[int]], key: str, ok: bool) -> None:
    c = d.setdefault(key, [0, 0])
    c[0] += 1
    c[1] += int(ok)


def pairwise_counts(
    walked: Iterable[HandFacts], cid: str, alias_map: Mapping[str, str] | None = None
) -> dict[str, dict]:
    alias_map = alias_map or {}
    out: dict[str, dict] = {}
    for hf in walked:
        canon = {pid: alias_map.get(pid, pid) for pid in hf.players}
        mine = [pid for pid, c in canon.items() if c == cid]
        if not mine:
            continue
        a = mine[0]
        pa = hf.players[a]
        seen: set[str] = set()
        for b, cb in canon.items():
            if cb == cid or cb in seen:  # an alias seated twice counts once
                continue
            seen.add(cb)
            rec = out.setdefault(cb, {"hands_together": 0, "net_vs": 0, "hands_b_involved": 0,
                                      "counts": {}})  # fmt: skip
            rec["hands_together"] += 1
            counts = rec["counts"]
            if "vpip" in pa.stats:
                _bump(counts, "vpip_when_in", pa.stats["vpip"][1])
            seen_3b = False
            for d in hf.decisions:
                if d.player != a or d.last_aggressor != b:
                    continue
                if d.street == "preflop" and d.n_aggr_before == 1 and not seen_3b:
                    _bump(counts, "three_bet_vs", d.kind == "raise")
                    seen_3b = True
                if d.street != "preflop" and d.n_aggr_before >= 1:
                    _bump(counts, "fold_to_their_bet", d.kind == "fold")
                    _bump(counts, "call_vs_their_bet", d.kind == "call")
            pb = hf.players[b]
            b_involved = pb.saw_flop or ("vpip" in pb.stats and pb.stats["vpip"][1])
            if b_involved:
                rec["net_vs"] += pa.net
                rec["hands_b_involved"] += 1
    return out


def pairwise(
    walked: Iterable[HandFacts],
    cid: str,
    prior_by_stat: Mapping[str, tuple[float, float]],
    alias_map: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """Profile `vs` block. prior_by_stat maps each PAIR_STATS name to (mean, k)."""
    out: dict[str, dict] = {}
    for other, rec in pairwise_counts(walked, cid, alias_map).items():
        entry: dict = {"hands_together": rec["hands_together"]}
        for stat in PAIR_STATS:
            n, s = rec["counts"].get(stat, [0, 0])
            mean, k = prior_by_stat[stat]
            entry[stat] = beta_binomial(s, n, mean, k)
        entry["net_vs"] = rec["net_vs"]
        entry["hands_b_involved"] = rec["hands_b_involved"]
        out[other] = entry
    return out
