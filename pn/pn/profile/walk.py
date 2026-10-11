"""Walk one normalized hand (contracts/hand_history.md) and derive per-player facts.

Every stat in pn.profile is computed from the `HandFacts` produced here, so all definitions of
opportunities/successes live in this module. See STATS.md-style docstrings on each block.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

STREETS = ("preflop", "flop", "turn", "river")
POSTFLOP = ("flop", "turn", "river")
VOLUNTARY = frozenset({"fold", "check", "call", "bet", "raise"})
BLIND_POSTS = frozenset({"post_sb", "post_bb", "post_straddle", "post_missed_bb"})  # live money
STEAL_POSITIONS = frozenset({"CO", "BTN", "SB"})
SKIP_FLAGS = frozenset({"incomplete", "bomb_pot"})

# fold_vs_size buckets: nearest size in log space (geometric midpoints between solver sizes).
FLOP_SIZE_BUCKETS = ((28.7, "25"), (40.6, "33"), (70.7, "50"), (float("inf"), "100"))
LATER_SIZE_BUCKETS = ((106.1, "75"), (float("inf"), "150"))
# bet_size_hist buckets (half-open intervals, percent of pot).
HIST_BUCKETS = ((30.0, "0-30"), (45.0, "30-45"), (70.0, "45-70"), (120.0, "70-120"))
HIST_TOP = "120+"
# 3-bet size buckets: raise-to as a multiple of the open (half-open intervals).
THREE_BET_SIZE_BUCKETS = ((2.5, "<2.5x"), (3.5, "2.5-3.5x"), (4.5, "3.5-4.5x"))
THREE_BET_SIZE_TOP = ">4.5x"
# Effective stack (bb) buckets at hand start: (upper bound inclusive, name).
DEPTH_BUCKETS = ((60.0, "<=60bb"), (150.0, "60-150bb"), (250.0, "150-250bb"))
DEPTH_TOP = ">250bb"
# Postflop acting order by position label (first to act first); BTN acts last (also HU).
POSTFLOP_ORDER = ("SB", "BB", "STR", "UTG", "UTG1", "UTG2", "UTG3", "LJ", "HJ", "CO", "BTN")


def size_bucket(street: str, pct: float) -> str:
    table = FLOP_SIZE_BUCKETS if street == "flop" else LATER_SIZE_BUCKETS
    for edge, name in table:
        if pct < edge:
            return name
    return table[-1][1]


def three_bet_size_bucket(multiple: float) -> str:
    for edge, name in THREE_BET_SIZE_BUCKETS:
        if multiple < edge:
            return name
    return THREE_BET_SIZE_TOP


def depth_bucket(bb: float) -> str:
    for edge, name in DEPTH_BUCKETS:
        if bb <= edge:
            return name
    return DEPTH_TOP


def postflop_rank(position: str) -> int:
    return POSTFLOP_ORDER.index(position) if position in POSTFLOP_ORDER else len(POSTFLOP_ORDER)


def hist_bucket(pct: float) -> str:
    for edge, name in HIST_BUCKETS:
        if pct < edge:
            return name
    return HIST_TOP


@dataclass(frozen=True)
class Decision:
    """One voluntary action with the situation the actor faced."""

    street: str
    player: str
    kind: str  # fold|check|call|bet|raise (preflop bets normalized to raise)
    allin: bool
    to_amount: int
    pot_before: int
    facing: int  # chips needed to call
    level_before: int  # highest commitment on this street before the action
    n_aggr_before: int  # preflop: raises (posts excluded); postflop: bets+raises this street
    last_aggressor: str | None
    facing_bet_pct: float | None  # size (% pot) of the bet faced when n_aggr_before == 1
    bet_pct: float | None  # own bet size (% pot) for postflop bets
    n_in_hand: int  # players not folded, incl. actor
    n_street_start: int
    first_on_street: bool
    callers_since_raise: int  # preflop: voluntary calls since the last raise (limpers if none)
    prior_voluntary: bool  # actor already called/raised earlier this hand (preflop)

    @property
    def situation(self) -> str:
        if self.n_aggr_before == 0:
            return "first"
        return "facing_bet" if self.n_aggr_before == 1 else "facing_raise"

    @property
    def aggressive(self) -> bool:
        return self.kind in ("bet", "raise")


@dataclass
class PlayerFacts:
    player_id: str
    position: str
    stats: dict[str, tuple[bool, bool]] = field(default_factory=dict)  # stat -> (opp, success)
    pip: dict[str, str] = field(default_factory=dict)  # postflop stat -> "hu" | "multiway"
    actions: Counter = field(default_factory=Counter)  # (street, situation, action) -> n
    fold_vs_size: list[tuple[str, str, bool, str | None]] = field(default_factory=list)
    # stat -> opponents the opportunity was against (aggressor, else others involved)
    vs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    bet_sizes: list[tuple[str, float]] = field(default_factory=list)
    preflop_action: str | None = None  # open|call|3bet (first meaningful decision)
    three_bet_sizes: list[tuple[float, bool, int]] = field(default_factory=list)  # (x, ip, callers)
    depth: str | None = None  # effective-stack bucket at hand start
    # positional facts (pn.profile.spots)
    pos_key: str = ""
    situ: dict[str, tuple | None] = field(default_factory=dict)
    group: dict[str, str] = field(default_factory=dict)
    street_ip: dict[str, bool] = field(default_factory=dict)
    ipos: dict[str, str] = field(default_factory=dict)
    line: str | None = None
    cards: list[str] | None = None
    saw_flop: bool = False
    went_to_sd: bool = False
    won: bool = False
    net: int = 0

    def mark(self, stat: str, success: bool, pip: str | None = None, vs: str | None = None) -> None:
        """Record an opportunity. `vs` = the opponent whose action created it (aggressor)."""
        if stat in self.stats:  # first opportunity per hand wins
            return
        self.stats[stat] = (True, success)
        if pip is not None:
            self.pip[stat] = pip
        if vs is not None:
            self.vs[stat] = (vs,)


@dataclass
class HandFacts:
    hand_id: str
    n_players: int
    players: dict[str, PlayerFacts]
    decisions: list[Decision]
    pfa: str | None  # last preflop raiser
    opener: str | None  # first preflop raiser
    board: list[str]
    flags: frozenset[str]

    def dealt(self) -> set[str]:
        return set(self.players)


def _board_cards(hand: dict) -> dict[str, list[str]]:
    b = hand.get("board") or {}
    flop = list(b.get("flop") or [])
    turn = [b["turn"]] if b.get("turn") else []
    river = [b["river"]] if b.get("river") else []
    return {"flop": flop, "turn": flop + turn, "river": flop + turn + river}


def _known_cards(hand: dict) -> dict[str, list[str]]:
    """Both hole cards when known (PokerNow may show a single card: ignored)."""
    out: dict[str, list[str]] = {}
    for p in hand.get("players", []):
        if p.get("cards") and len(p["cards"]) == 2:
            out[p["player_id"]] = list(p["cards"])
    for sd in hand.get("showdowns", []) or []:
        if sd.get("cards") and len(sd["cards"]) == 2 and sd.get("shown", True):
            out.setdefault(sd["player_id"], list(sd["cards"]))
    return out


def _walk_decisions(hand: dict) -> list[Decision]:
    players = [p["player_id"] for p in hand["players"]]
    folded: set[str] = set()
    pot = 0
    decisions: list[Decision] = []
    actions = sorted(hand.get("actions", []), key=lambda a: a["seq"])
    street = None
    committed: dict[str, int] = {}
    level = 0
    n_aggr = 0
    last_aggr: str | None = None
    aggr_pct: float | None = None
    acted: set[str] = set()
    street_start_n = len(players)
    callers = 0
    voluntary: set[str] = set()
    for a in actions:
        if a["street"] != street:
            street = a["street"]
            committed = {p: 0 for p in players}
            level, n_aggr, last_aggr, aggr_pct, acted = 0, 0, None, None, set()
            street_start_n = len(players) - len(folded)
            callers = 0
        kind, pid = a["kind"], a["player_id"]
        amount = int(a.get("amount") or 0)
        to_amount = int(a.get("to_amount") or 0)
        if kind not in VOLUNTARY:
            if kind in BLIND_POSTS:
                committed[pid] = committed.get(pid, 0) + amount
                level = max(level, committed[pid])
            pot += amount
            continue
        if street == "preflop" and kind == "bet":
            kind = "raise"
        if street != "preflop" and kind == "raise" and level == 0:
            kind = "bet"
        facing = max(0, level - committed.get(pid, 0))
        bet_pct = None
        if kind == "bet" and street != "preflop" and pot > 0:
            bet_pct = 100.0 * to_amount / pot
        decisions.append(
            Decision(
                street=street,
                player=pid,
                kind=kind,
                allin=bool(a.get("allin")),
                to_amount=to_amount,
                pot_before=pot,
                facing=facing,
                level_before=level,
                n_aggr_before=n_aggr,
                last_aggressor=last_aggr,
                facing_bet_pct=aggr_pct if n_aggr == 1 else None,
                bet_pct=bet_pct,
                n_in_hand=len(players) - len(folded),
                n_street_start=street_start_n,
                first_on_street=pid not in acted,
                callers_since_raise=callers,
                prior_voluntary=pid in voluntary,
            )
        )
        acted.add(pid)
        pot += amount
        if kind == "fold":
            folded.add(pid)
        elif kind == "call":
            committed[pid] = committed.get(pid, 0) + amount
            callers += 1
            voluntary.add(pid)
        elif kind in ("bet", "raise"):
            committed[pid] = max(to_amount, committed.get(pid, 0) + amount)
            level = committed[pid]
            n_aggr += 1
            last_aggr = pid
            aggr_pct = bet_pct if kind == "bet" else None
            callers = 0
            voluntary.add(pid)
    return decisions


def _preflop_facts(facts: dict[str, PlayerFacts], decs: list[Decision], opener: str | None) -> None:
    by_player: dict[str, list[Decision]] = {}
    for d in decs:
        by_player.setdefault(d.player, []).append(d)
    for pid, pf in facts.items():
        mine = by_player.get(pid, [])
        if not mine:  # walk (BB) or sat all-in from posting: no preflop decision
            continue
        vpip = any(d.kind in ("call", "raise") for d in mine)
        pfr = any(d.kind == "raise" for d in mine)
        pf.mark("vpip", vpip)
        pf.mark("pfr", pfr)
        d0 = mine[0]
        if d0.n_aggr_before == 0 and d0.facing > 0:
            pf.mark("limp", d0.kind == "call")
        if d0.n_aggr_before == 0 and d0.callers_since_raise == 0:
            pf.mark("rfi", d0.kind == "raise")
            if pf.position in STEAL_POSITIONS:
                pf.mark("steal", d0.kind == "raise")
            if d0.kind == "raise":
                pf.preflop_action = "open"
        if d0.n_aggr_before == 1:
            pf.mark("cold_call", d0.kind == "call", vs=d0.last_aggressor)
            if d0.callers_since_raise >= 1:
                pf.mark("squeeze", d0.kind == "raise", vs=d0.last_aggressor)
            if d0.kind == "call":
                pf.preflop_action = "call"
        for d in mine:
            if d.n_aggr_before == 1:
                pf.mark("three_bet", d.kind == "raise", vs=d.last_aggressor)
                if d.kind == "raise" and pf.preflop_action is None:
                    pf.preflop_action = "3bet"
            if d.n_aggr_before == 2:
                pf.mark("four_bet", d.kind == "raise", vs=d.last_aggressor)
                if pid == opener:
                    pf.mark("fold_to_three_bet", d.kind == "fold", vs=d.last_aggressor)
        for d in mine:
            pf.actions[("preflop", d.situation, d.kind)] += 1


def _three_bet_sizes(
    facts: dict[str, PlayerFacts], decs: list[Decision], opener: str | None
) -> None:
    """Record every 3-bet's raise-to as a multiple of the open, IP/OOP vs the opener."""
    if opener is None:
        return
    for d in decs:
        if d.kind == "raise" and d.n_aggr_before == 1 and d.level_before > 0:
            ip = postflop_rank(facts[d.player].position) > postflop_rank(facts[opener].position)
            facts[d.player].three_bet_sizes.append(
                (d.to_amount / d.level_before, ip, d.callers_since_raise)
            )


def _squeeze_facts(facts: dict[str, PlayerFacts], decs: list[Decision]) -> None:
    """fold_to_squeeze / call_vs_squeeze: the opener and the callers of the open, at their first
    decision facing the squeeze (a 3-bet made after >= 1 player called the open)."""
    idx = next(
        (i for i, d in enumerate(decs)
         if d.kind == "raise" and d.n_aggr_before == 1 and d.callers_since_raise >= 1),
        None,
    )  # fmt: skip
    if idx is None:
        return
    squeezer = decs[idx].player
    involved = {d.player for d in decs[:idx] if d.n_aggr_before == 0 and d.kind == "raise"}
    involved |= {d.player for d in decs[:idx] if d.n_aggr_before == 1 and d.kind == "call"}
    involved.discard(squeezer)
    seen: set[str] = set()
    for d in decs[idx + 1 :]:
        if d.player in involved and d.player not in seen and d.n_aggr_before == 2:
            seen.add(d.player)
            facts[d.player].mark("fold_to_squeeze", d.kind == "fold", vs=squeezer)
            facts[d.player].mark("call_vs_squeeze", d.kind == "call", vs=squeezer)


def _depths(hand: dict, facts: dict[str, PlayerFacts]) -> None:
    bb = int((hand.get("blinds") or {}).get("bb") or 0)
    stacks = {p["player_id"]: int(p.get("stack_start") or 0) for p in hand["players"]}
    if bb <= 0 or len(stacks) < 2:
        return
    for pid, pf in facts.items():
        others = max(v for k, v in stacks.items() if k != pid)
        pf.depth = depth_bucket(min(stacks[pid], others) / bb)


def _pip(n: int) -> str:
    return "hu" if n <= 2 else "multiway"


def _postflop_street(
    street: str,
    facts: dict[str, PlayerFacts],
    decs: list[Decision],
    pfa: str | None,
    cbettor_prev: str | None,
    flop_unbet: bool,
) -> str | None:
    """Mark street-level postflop stats. Returns this street's c-bettor (for barrel chains)."""
    if not decs:
        return None
    first_idx: dict[str, int] = {}
    for i, d in enumerate(decs):
        first_idx.setdefault(d.player, i)
    cbettor: str | None = None
    chain_owner = pfa if street == "flop" else cbettor_prev
    for i, d in enumerate(decs):
        pf = facts[d.player]
        pip = _pip(d.n_street_start)
        pf.actions[(street, d.situation, d.kind)] += 1
        if d.first_on_street and d.n_aggr_before == 0:
            if d.player == chain_owner:
                pf.mark(f"cbet_{street}", d.kind == "bet", pip)
                if d.kind == "bet":
                    cbettor = d.player
            owner_later = chain_owner in first_idx and first_idx[chain_owner] > i
            if street == "flop" and pfa and d.player != pfa and owner_later:
                pf.mark("donk_flop", d.kind == "bet", pip, vs=pfa)
            pfa_later = pfa in first_idx and first_idx[pfa] > i  # all-in PFA: no opportunity
            if street == "turn" and pfa and d.player != pfa and flop_unbet and pfa_later:
                pf.mark("probe_turn", d.kind == "bet", pip, vs=pfa)
        if d.n_aggr_before == 1 and cbettor and d.last_aggressor == cbettor and d.player != cbettor:
            pf.mark(f"fold_to_cbet_{street}", d.kind == "fold", pip, vs=cbettor)
        if street == "flop" and not d.first_on_street and d.n_aggr_before >= 1:
            prev = [x for x in decs[:i] if x.player == d.player]
            if prev and prev[0].kind == "check":
                pf.mark("check_raise_flop", d.kind == "raise", pip, vs=d.last_aggressor)
        if d.n_aggr_before == 1 and d.facing_bet_pct is not None:
            pf.fold_vs_size.append(
                (street, size_bucket(street, d.facing_bet_pct), d.kind == "fold", d.last_aggressor)
            )
        if d.kind == "bet" and d.bet_pct is not None:
            pf.bet_sizes.append((street, d.bet_pct))
    return cbettor


def _af_counts(facts: dict[str, PlayerFacts], decs: list[Decision]) -> None:
    for street in POSTFLOP:
        agg: Counter = Counter()
        calls: Counter = Counter()
        for d in decs:
            if d.street != street:
                continue
            if d.aggressive:
                agg[d.player] += 1
            elif d.kind == "call":
                calls[d.player] += 1
        for pid in set(agg) | set(calls):
            facts[pid].actions[("af", street, "aggr")] += agg[pid]
            facts[pid].actions[("af", street, "call")] += calls[pid]


def _line(pf: PlayerFacts, decs: list[Decision], pfa: str | None) -> str | None:
    mine = [d for d in decs if d.player == pf.player_id]
    if not any(d.street != "preflop" for d in mine):
        return None
    pre = [d for d in mine if d.street == "preflop"]
    if pf.player_id == pfa:
        tok = "raise"
    elif pre and any(d.kind in ("call", "raise") for d in pre):
        tok = "call"
    else:
        tok = "check"
    parts = [f"pre:{tok}"]
    for street in POSTFLOP:
        acts = [d.kind for d in mine if d.street == street]
        if acts:
            parts.append(f"{street}:{'-'.join(acts)}")
    return "|".join(parts)


def _default_vs(facts: dict[str, PlayerFacts]) -> None:
    """Opportunities without a single aggressor are against the other players involved:
    everyone else dealt in (preflop stats) or everyone else who saw the flop (postflop)."""
    saw = [p for p, f in facts.items() if f.saw_flop]
    for pid, pf in facts.items():
        others_dealt = tuple(p for p in facts if p != pid)
        others_flop = tuple(p for p in saw if p != pid) or others_dealt
        for stat in pf.stats:
            if stat not in pf.vs:
                pf.vs[stat] = others_flop if stat in pf.pip else others_dealt


def walk_hand(hand: dict) -> HandFacts | None:
    """Derive facts for every dealt player. Returns None for skipped hands (SKIP_FLAGS)."""
    flags = frozenset(hand.get("flags") or [])
    if flags & SKIP_FLAGS:
        return None
    decs = _walk_decisions(hand)
    facts = {
        p["player_id"]: PlayerFacts(player_id=p["player_id"], position=p.get("position") or "?")
        for p in hand["players"]
    }
    pre = [d for d in decs if d.street == "preflop"]
    raisers = [d.player for d in pre if d.kind == "raise"]
    opener = raisers[0] if raisers else None
    pfa = raisers[-1] if raisers else None
    _preflop_facts(facts, pre, opener)
    _squeeze_facts(facts, pre)
    _three_bet_sizes(facts, pre, opener)
    _depths(hand, facts)

    folded_pre = {d.player for d in pre if d.kind == "fold"}
    boards = _board_cards(hand)
    has_flop = len(boards["flop"]) == 3
    for pid, pf in facts.items():
        pf.saw_flop = has_flop and pid not in folded_pre

    cbettor: str | None = None
    flop_decs = [d for d in decs if d.street == "flop"]
    flop_unbet = not any(d.aggressive for d in flop_decs)
    for street in POSTFLOP:
        sd = [d for d in decs if d.street == street]
        prev = cbettor
        cbettor = _postflop_street(street, facts, sd, pfa, prev, flop_unbet)
    _af_counts(facts, decs)

    folded = {d.player for d in decs if d.kind == "fold"}
    remaining = [p for p in facts if p not in folded]
    winners = {
        w["player_id"]
        for pot in hand.get("pots", []) or []
        for w in pot.get("winners", []) or []
        if int(w.get("amount") or 0) > 0
    }
    net = hand.get("net") or {}
    cards = _known_cards(hand)
    for pid, pf in facts.items():
        pf.went_to_sd = pf.saw_flop and pid in remaining and len(remaining) >= 2
        pf.won = pid in winners
        pf.net = int(net.get(pid, 0))
        pf.cards = cards.get(pid)
        pip = _pip(len([p for p in facts if facts[p].saw_flop]))
        if pf.saw_flop:
            pf.mark("wtsd", pf.went_to_sd, pip)
            pf.mark("wwsf", pf.won, pip)
            if pf.went_to_sd:
                pf.mark("wsd", pf.won, pip)
        pf.line = _line(pf, decs, pfa)
    hf = HandFacts(
        hand_id=hand.get("hand_id", "?"),
        n_players=len(facts),
        players=facts,
        decisions=decs,
        pfa=pfa,
        opener=opener,
        board=boards["river"],
        flags=flags,
    )
    from pn.profile.spots import add_positional_facts  # local: spots imports this module

    add_positional_facts(hf)
    _default_vs(facts)
    return hf
