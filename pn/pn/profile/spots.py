"""Positional (PokerTracker-4-style) facts derived after the main walk; see POSITIONS.md.

Per player and hand this adds:
  pos_key        position label; heads-up seats are `BTN_HU` / `BB_HU` (kept apart from 6-max)
  situ[stat]     preflop situation of the opportunity, used to look up GTO frequencies:
                 ("rfi", P) | ("vs_limp", P) | ("vs_open", O, P) | ("cold", O, T, P) |
                 ("vs_3bet", P, T) | ("vs_4bet" | "vs_5bet" | "vs_6bet", O, T)
                 (O opener, T 3-bettor, P player)
  group[stat]    opener group (EP/MP/CO/BTN/SB/BTN_HU) for vs-open stats
  street_ip      street -> True when last to act among the street's actors
  ipos[stat]     "IP" / "OOP" for postflop stats
New stats: fold_to_four_bet, five_bet, fold_to_five_bet, six_bet, fold_to_six_bet,
fold_to_steal, raise_cbet_<street>, bet_vs_missed_cbet_flop/turn,
float_turn; per-street counts for agg_freq (aggr vs calls+folds).
"""

from __future__ import annotations

from pn.profile.walk import POSTFLOP, Decision, HandFacts, PlayerFacts, postflop_rank

OPENER_GROUP = {"UTG": "EP", "UTG1": "EP", "UTG2": "EP", "UTG3": "EP", "LJ": "EP", "HJ": "MP",
                "CO": "CO", "BTN": "BTN", "SB": "SB", "BTN_HU": "BTN_HU", "STR": "EP"}  # fmt: skip
STEAL_KEYS = frozenset({"CO", "BTN", "SB", "BTN_HU"})
BLIND_KEYS = frozenset({"SB", "BB", "BB_HU"})
POSTFLOP_STREET = {
    "check_raise_flop": "flop", "donk_flop": "flop", "probe_turn": "turn",
    "bet_vs_missed_cbet_flop": "flop", "bet_vs_missed_cbet_turn": "turn", "float_turn": "turn",
}  # fmt: skip


def pos_key(position: str, n_players: int) -> str:
    return f"{position}_HU" if n_players == 2 and position in ("BTN", "BB", "SB") else position


def opener_group(key: str) -> str:
    return OPENER_GROUP.get(key, "EP")


def stat_street(stat: str) -> str | None:
    if stat in POSTFLOP_STREET:
        return POSTFLOP_STREET[stat]
    for street in POSTFLOP:
        if stat.endswith(f"_{street}"):
            return street
    return None


def _set(pf: PlayerFacts, stat: str, situ: tuple | None, group: str | None = None) -> None:
    if stat in pf.stats and stat not in pf.situ:
        pf.situ[stat] = situ
        if group is not None:
            pf.group[stat] = group


def _preflop(hf: HandFacts) -> None:
    pre = [d for d in hf.decisions if d.street == "preflop"]
    key = {pid: pf.pos_key for pid, pf in hf.players.items()}
    three_bettors: set[str] = set()
    for i, d in enumerate(pre):
        pf = hf.players[d.player]
        P = key[d.player]
        first = not any(x.player == d.player for x in pre[:i])
        # facing raises, a GTO Wizard spot exists only if nobody limped or called before
        clean = not any(x.kind == "call" for x in pre[:i])
        situ: tuple | None = None
        if d.n_aggr_before == 0 and first:
            limpers = [key[x.player] for x in pre[:i] if x.kind == "call"]
            if not limpers:
                situ = ("rfi", P)
            elif P in ("BB", "BB_HU") and limpers in (["SB"], ["BTN_HU"]):
                situ = ("vs_limp", P)
            for stat in ("vpip", "pfr", "rfi", "limp", "steal"):
                _set(pf, stat, situ if (situ and (situ[0] == "rfi" or stat in ("vpip", "pfr")))
                     else None)  # fmt: skip
        elif d.n_aggr_before == 1:
            O = key[d.last_aggressor] if d.last_aggressor else None
            if O and clean and first:
                situ = ("vs_open", O, P)
            g = opener_group(O) if O else None
            for stat in ("vpip", "pfr") if first else ():
                _set(pf, stat, situ)
            for stat in ("three_bet", "cold_call"):
                _set(pf, stat, situ, g if situ else None)
            if (first and O in STEAL_KEYS and P in BLIND_KEYS and clean
                    and "fold_to_steal" not in pf.stats):  # fmt: skip
                pf.mark("fold_to_steal", d.kind == "fold", vs=d.last_aggressor)
                _set(pf, "fold_to_steal", situ, g)
            if d.kind == "raise":
                three_bettors.add(d.player)
        elif d.n_aggr_before == 2:
            raisers = [x for x in pre[:i] if x.kind == "raise"]
            O, T = key[raisers[0].player], key[raisers[1].player]
            if first:
                situ = ("cold", O, T, P) if clean else None
                for stat in ("vpip", "pfr", "four_bet"):
                    _set(pf, stat, situ)
            else:
                opener_ok = d.player == hf.opener and clean
                situ = ("vs_3bet", P, T) if opener_ok else None
                for stat in ("fold_to_three_bet", "four_bet"):
                    _set(pf, stat, situ)
        elif d.n_aggr_before >= 3:
            _nbet(pf, d, pre[:i], hf.opener, three_bettors, key, clean)


# n raises faced -> (who faces them, stat -> success action); the GTO Wizard spot needs a
# heads-up raise war: opener, 3-bettor, opener, 3-bettor, ... with no calls before.
NBET = {
    3: ("three_bettor", "vs_4bet", {"fold_to_four_bet": "fold", "five_bet": "raise"}),
    4: ("opener", "vs_5bet", {"fold_to_five_bet": "fold", "six_bet": "raise"}),
    5: ("three_bettor", "vs_6bet", {"fold_to_six_bet": "fold"}),
}


def _nbet(pf: PlayerFacts, d: Decision, before: list[Decision], opener: str | None,
          three_bettors: set[str], key: dict[str, str], clean: bool) -> None:  # fmt: skip
    """3-bettor vs 4-bet, opener vs 5-bet, 3-bettor vs 6-bet (first opportunity per hand)."""
    if d.n_aggr_before not in NBET:
        return
    who, kind, stats = NBET[d.n_aggr_before]
    raisers = [x.player for x in before if x.kind == "raise"]
    actor_ok = d.player == opener if who == "opener" else d.player in three_bettors
    if not actor_ok or len(raisers) < 2:
        return
    o, t = raisers[0], raisers[1]
    war = all(r == (o if k % 2 == 0 else t) for k, r in enumerate(raisers))
    situ = (kind, key[o], key[t]) if war and clean and o == opener else None
    for stat, action in stats.items():
        if stat not in pf.stats:
            pf.mark(stat, d.kind == action, vs=d.last_aggressor)
            _set(pf, stat, situ)


def _street_ip(hf: HandFacts) -> None:
    for street in POSTFLOP:
        actors = {d.player for d in hf.decisions if d.street == street}
        if len(actors) < 2:
            continue
        last = max(actors, key=lambda p: postflop_rank(hf.players[p].position))
        for p in actors:
            hf.players[p].street_ip[street] = p == last


def _owner_checked(decs: list[Decision], owner: str | None) -> int | None:
    """Index of the owner's first decision on the street if it was an unbet check."""
    for i, d in enumerate(decs):
        if d.player == owner:
            return i if d.kind == "check" and d.n_aggr_before == 0 else None
    return None


def _postflop(hf: HandFacts) -> None:
    cbettor = {st: next((p for p, f in hf.players.items()
                         if f.stats.get(f"cbet_{st}") == (True, True)), None)
               for st in POSTFLOP}  # fmt: skip
    flop_callers = {
        d.player for d in hf.decisions
        if d.street == "flop" and d.kind == "call" and d.last_aggressor == cbettor["flop"]
        and cbettor["flop"] and d.n_aggr_before == 1
    }  # fmt: skip
    for street in POSTFLOP:
        decs = [d for d in hf.decisions if d.street == street]
        cb = cbettor[street]
        seen: set[str] = set()
        for d in decs:
            facing_cbet = cb and d.n_aggr_before == 1 and d.last_aggressor == cb
            if facing_cbet and d.player != cb and d.player not in seen:
                seen.add(d.player)
                hf.players[d.player].mark(f"raise_cbet_{street}", d.kind == "raise", vs=cb)
        owner = hf.pfa if street == "flop" else (cbettor["flop"] if street == "turn" else None)
        idx = _owner_checked(decs, owner) if owner else None
        if idx is None:
            continue
        acted: set[str] = set()
        for d in decs[idx + 1 :]:
            if d.player in acted or d.player == owner:
                continue
            acted.add(d.player)
            if d.n_aggr_before != 0:
                break
            pf = hf.players[d.player]
            pf.mark(f"bet_vs_missed_cbet_{street}", d.kind == "bet", vs=owner)
            if street == "turn" and d.player in flop_callers:
                pf.mark("float_turn", d.kind == "bet", vs=owner)
    for d in hf.decisions:
        if d.street == "preflop":
            continue
        kind = "aggr" if d.aggressive else ("pass" if d.kind in ("call", "fold") else None)
        if kind:
            hf.players[d.player].actions[("afq", d.street, kind)] += 1


def _ip_tags(hf: HandFacts) -> None:
    for pf in hf.players.values():
        for stat in pf.stats:
            st = stat_street(stat)
            if st and st in pf.street_ip:
                pf.ipos[stat] = "IP" if pf.street_ip[st] else "OOP"


def add_positional_facts(hf: HandFacts) -> None:
    for pf in hf.players.values():
        pf.pos_key = pos_key(pf.position, hf.n_players)
    _preflop(hf)
    _street_ip(hf)
    _postflop(hf)
    _ip_tags(hf)
