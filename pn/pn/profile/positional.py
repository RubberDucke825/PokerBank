"""PokerTracker-4-style positional matrix: every stat by position (postflop also IP/OOP, HU/MW).

Opportunities are derived from `pn.profile.walk` output (HandFacts / Decision), never from a
second state machine. Stats taken straight from the walker: rfi, limp, steal, cold_call,
squeeze, cbet_*, fold_to_cbet_*, donk_flop, probe_turn. Stats derived here from the walker's
Decision list (first opportunity per hand, like the walker):

  call_open / three_bet     first decision facing exactly one raise (the open) -> call / raise;
                            also split by opener group: `<stat>_vs_<EP|MP|CO|BTN|SB>`
  fold_to_steal /           SB or BB, first decision, facing a raise-first-in from CO/BTN/SB
  three_bet_vs_steal        -> fold / raise
  fold_to_3bet / call_3bet  opener facing exactly two raises -> fold / call / raise
  / four_bet                (four_bet also counts cold 4-bets: any player facing two raises)
  fold_to_4bet / five_bet   the 3-bettor facing exactly three raises -> fold / raise
  raise_cbet_<st>           facing the street's c-bet (no raise between) -> raise
  bet_vs_missed_cbet_<st>   the c-bet owner checked first; you act after with no bet -> bet
  check_raise_<st>          your first decision on the street was a check; you face a bet -> raise
  float_turn                called the flop c-bet in position, c-bettor checks turn -> bet
  agg_freq_<st>                  every bet/raise/call/fold on the street -> bet or raise (PT4 AFq; = stats.py agg_freq_X)

Shrinkage per cell (hierarchical population -> position -> player, as in build.py):
  base   = population position prior (config/population_priors.json `by_position`), else the
           table-wide stat prior with k * REDUCED_K
  prior  = base shifted on the logit scale by the player's pooled deviation for that stat
  post   = Beta-Binomial(prior, k); postflop IP/OOP/HU/MW splits use the position cell's prior
           (not its posterior, which already contains the split's hands).
GTO per cell: preflop = mean of GTO Wizard spot frequencies over the cell's opportunities
(pn.profile.positional_gto); postflop = gto_baselines.json default (splits: hu/multiway entry
when present). `None` when there is no baseline.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pn.profile.baselines import annotate, gto_value, load_baselines
from pn.profile.bayes import beta_binomial, shift_prior
from pn.profile.positional_gto import GtoTable, Spot, load_table
from pn.profile.priors import load_priors, position_mean
from pn.profile.tips import exploit_for
from pn.profile.walk import (
    POSTFLOP,
    STEAL_POSITIONS,
    HandFacts,
    hist_bucket,
    postflop_rank,
    walk_hand,
)

POSITION_ORDER = ("UTG", "UTG1", "UTG2", "UTG3", "LJ", "HJ", "CO", "BTN", "SB", "BB", "STR",
                  "BTN_HU", "BB_HU")  # fmt: skip
STEAL_OPENERS = STEAL_POSITIONS | {"BTN_HU"}
BLINDS = ("SB", "BB", "BB_HU")
OPENER_GROUPS = {"UTG": "EP", "UTG1": "EP", "UTG2": "EP", "UTG3": "EP", "LJ": "EP",
                 "HJ": "MP", "CO": "CO", "BTN": "BTN", "SB": "SB"}  # fmt: skip
GROUPS = ("EP", "MP", "CO", "BTN", "SB")
PREFLOP_ROWS = (
    "rfi", "limp", "steal", "call_open", "three_bet",
    *(f"call_open_vs_{g}" for g in GROUPS), *(f"three_bet_vs_{g}" for g in GROUPS),
    "fold_to_3bet", "call_3bet", "four_bet", "fold_to_4bet", "five_bet",
    "squeeze", "cold_call", "fold_to_steal", "three_bet_vs_steal",
)  # fmt: skip
STREET_ROWS = {
    "flop": ("cbet_flop", "fold_to_cbet_flop", "raise_cbet_flop", "bet_vs_missed_cbet_flop",
             "check_raise_flop", "donk_flop", "agg_freq_flop"),
    "turn": ("cbet_turn", "fold_to_cbet_turn", "raise_cbet_turn", "bet_vs_missed_cbet_turn",
             "check_raise_turn", "probe_turn", "float_turn", "agg_freq_turn"),
    "river": ("cbet_river", "fold_to_cbet_river", "raise_cbet_river",
              "bet_vs_missed_cbet_river", "check_raise_river", "agg_freq_river"),
}  # fmt: skip
POSTFLOP_ROWS = tuple(s for rows in STREET_ROWS.values() for s in rows)
WALK_POSTFLOP = ("cbet_flop", "cbet_turn", "cbet_river", "fold_to_cbet_flop", "fold_to_cbet_turn",
                 "fold_to_cbet_river", "donk_flop", "probe_turn")  # fmt: skip
SPLITS = ("ip", "oop", "hu", "multiway")
HIST_NAMES = ("0-30", "30-45", "45-70", "70-120", "120+")
REDUCED_K = 0.5  # k multiplier when no position-level prior exists
MIN_LEAK_N = 10
MAX_LEAKS = 20
WEIGHTED_STATS = ("rfi", "limp", "call_open", "three_bet", "fold_to_3bet", "four_bet",
                  "cold_call", "fold_to_steal")  # fmt: skip

# Our stat -> population prior / GTO baseline stat with the same meaning.
PRIOR_ALIAS = {"call_open": "cold_call", "fold_to_3bet": "fold_to_three_bet",
               "fold_to_4bet": "fold_to_four_bet", "three_bet_vs_steal": "three_bet"}  # fmt: skip
PRIOR_ALIAS.update({f"three_bet_vs_{g}": "three_bet" for g in GROUPS})
PRIOR_ALIAS.update({f"call_open_vs_{g}": "cold_call" for g in GROUPS})
# Seed priors (mean, k) for stats config/population_priors.json does not have yet.
FALLBACK_PRIORS = {
    "call_3bet": (0.35, 12), "fold_to_4bet": (0.5, 10), "five_bet": (0.15, 15),
    "fold_to_steal": (0.65, 16), "float_turn": (0.4, 10), "check_raise_turn": (0.07, 25),
    "check_raise_river": (0.06, 25), "raise_cbet_flop": (0.08, 25),
    "raise_cbet_turn": (0.06, 25), "raise_cbet_river": (0.05, 25),
    "bet_vs_missed_cbet_flop": (0.4, 12), "bet_vs_missed_cbet_turn": (0.4, 12),
    "bet_vs_missed_cbet_river": (0.35, 12), "agg_freq_flop": (0.4, 20), "agg_freq_turn": (0.38, 20),
    "agg_freq_river": (0.36, 20),
}  # fmt: skip
LABELS = {
    "rfi": "RFI", "limp": "limp", "steal": "steal", "call_open": "call vs open",
    "three_bet": "3bet", "fold_to_3bet": "fold to 3bet", "call_3bet": "call 3bet",
    "four_bet": "4bet", "fold_to_4bet": "fold to 4bet", "five_bet": "5bet",
    "squeeze": "squeeze", "cold_call": "cold call", "fold_to_steal": "fold to steal",
    "three_bet_vs_steal": "3bet vs steal", "donk_flop": "flop donk", "probe_turn": "turn probe",
    "float_turn": "turn float",
}  # fmt: skip
for _st in POSTFLOP:
    LABELS.update({f"cbet_{_st}": f"{_st} cbet", f"fold_to_cbet_{_st}": f"fold to {_st} cbet",
                   f"raise_cbet_{_st}": f"raise {_st} cbet", f"agg_freq_{_st}": f"{_st} AFq",
                   f"bet_vs_missed_cbet_{_st}": f"{_st} bet vs missed cbet",
                   f"check_raise_{_st}": f"{_st} check-raise"})  # fmt: skip


@dataclass(frozen=True)
class Opp:
    stat: str
    success: bool
    spot: Spot | None = None


@dataclass
class Cell:
    n: int = 0
    s: int = 0
    gto_sum: float = 0.0
    gto_n: int = 0

    def add(self, ok: bool, gto: float | None) -> None:
        self.n += 1
        self.s += int(ok)
        if gto is not None:
            self.gto_sum += gto
            self.gto_n += 1


@dataclass
class PosRaw:
    """Per-player accumulator: position -> stat -> Cell (postflop also per split)."""

    hands: Counter = field(default_factory=Counter)
    pre: dict[str, dict[str, Cell]] = field(default_factory=dict)
    post: dict[str, dict[str, Cell]] = field(default_factory=dict)
    split: dict[str, dict[str, dict[str, Cell]]] = field(default_factory=dict)
    hist: dict[str, dict[str, dict[str, Counter]]] = field(default_factory=dict)


# ---------------------------------------------------------------- opportunity extraction


def _eff_depths(hand: dict) -> dict[str, float | None]:
    bb = int((hand.get("blinds") or {}).get("bb") or 0)
    stacks = {p["player_id"]: int(p.get("stack_start") or 0) for p in hand["players"]}
    if bb <= 0 or len(stacks) < 2:
        return dict.fromkeys(stacks)
    return {
        pid: min(s, max(v for k, v in stacks.items() if k != pid)) / bb for pid, s in stacks.items()
    }


def preflop_opps(hf: HandFacts, pid: str, depth: float | None = None) -> list[Opp]:
    pf = hf.players[pid]
    pos, st = pf.pos_key, pf.stats
    pre = [d for d in hf.decisions if d.street == "preflop"]
    mine = [d for d in pre if d.player == pid]
    # facing raises, a GTO Wizard spot exists only if nobody limped or called before
    clean = {id(d) for i, d in enumerate(pre) if not any(x.kind == "call" for x in pre[:i])}
    raisers = [d.player for d in pre if d.kind == "raise"]
    opener = hf.opener
    three_bettor = raisers[1] if len(raisers) > 1 else None
    out: list[Opp] = []

    def sp(kind: str, seats: tuple[str, ...], action: str, d=None) -> Spot | None:
        return None if d is not None and id(d) not in clean else Spot(kind, seats, action, depth)

    def pos_of(p: str) -> str:
        return hf.players[p].pos_key

    for stat, action in (("rfi", "raise"), ("limp", "call"), ("steal", "raise")):
        if stat in st:
            out.append(Opp(stat, st[stat][1], sp("rfi", (pos,), action)))
    face1 = next((d for d in mine if d.n_aggr_before == 1), None)
    if face1 is not None and opener is not None and opener != pid:
        opos = pos_of(opener)
        group = OPENER_GROUPS.get(opos)
        for action, stat in (("raise", "three_bet"), ("call", "call_open")):
            spot = sp("vs_open", (opos, pos), action, face1)
            out.append(Opp(stat, face1.kind == action, spot))
            if group:
                out.append(Opp(f"{stat}_vs_{group}", face1.kind == action, spot))
        opened_clean = hf.players[opener].stats.get("rfi") == (True, True)
        steal = face1 is mine[0] and pos in BLINDS and opos in STEAL_OPENERS and opened_clean
        if steal and id(face1) in clean:
            out.append(Opp("fold_to_steal", face1.kind == "fold",
                           sp("vs_open", (opos, pos), "fold")))  # fmt: skip
            out.append(Opp("three_bet_vs_steal", face1.kind == "raise",
                           sp("vs_open", (opos, pos), "raise")))  # fmt: skip
        if "cold_call" in st:
            out.append(Opp("cold_call", st["cold_call"][1],
                           sp("vs_open", (opos, pos), "call", face1)))  # fmt: skip
    if "squeeze" in st:
        out.append(Opp("squeeze", st["squeeze"][1]))
    face2 = next((d for d in mine if d.n_aggr_before == 2), None)
    if face2 is not None and opener is not None and three_bettor is not None:
        tpos = pos_of(three_bettor)
        if pid == opener:
            for action, stat in (("fold", "fold_to_3bet"), ("call", "call_3bet"),
                                 ("raise", "four_bet")):  # fmt: skip
                spot = sp("vs_3bet", (pos, tpos), action, face2)
                out.append(Opp(stat, face2.kind == action, spot))
        else:
            spot = sp("cold", (pos_of(opener), tpos, pos), "raise", face2)
            out.append(Opp("four_bet", face2.kind == "raise", spot))
    face3 = next((d for d in mine if d.n_aggr_before == 3), None)
    if face3 is not None and pid == three_bettor and opener is not None:
        opener_4bet = raisers[2] == opener
        for action, stat in (("fold", "fold_to_4bet"), ("raise", "five_bet")):
            spot = sp("vs_4bet", (pos_of(opener), pos), action, face3) if opener_4bet else None
            out.append(Opp(stat, face3.kind == action, spot))
    return out


def _chain(hf: HandFacts) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """(owner, cbettor) per street: owner = PFA on the flop, then the previous c-bettor."""
    owners: dict[str, str | None] = {}
    cbettors: dict[str, str | None] = {}
    owner = hf.pfa
    for street in POSTFLOP:
        owners[street] = owner
        ok = owner is not None and hf.players[owner].stats.get(f"cbet_{street}") == (True, True)
        cbettors[street] = owner if ok else None
        owner = cbettors[street]
    return owners, cbettors


def _flop_callers_ip(hf: HandFacts, cbettor: str | None) -> set[str]:
    """Players who called the flop c-bet (never raised the flop) and are IP of the c-bettor."""
    if cbettor is None:
        return set()
    flop = [d for d in hf.decisions if d.street == "flop"]
    called = {d.player for d in flop if d.kind == "call" and d.last_aggressor == cbettor}
    raised = {d.player for d in flop if d.kind == "raise"}
    crank = postflop_rank(hf.players[cbettor].position)
    return {p for p in called - raised if postflop_rank(hf.players[p].position) > crank}


def postflop_opps(hf: HandFacts) -> dict[str, list[Opp]]:
    out: dict[str, list[Opp]] = {pid: [] for pid in hf.players}
    seen: dict[str, set[str]] = {pid: set() for pid in hf.players}

    def mark(pid: str, stat: str, ok: bool) -> None:
        if stat not in seen[pid]:  # first opportunity per hand wins (walker convention)
            seen[pid].add(stat)
            out[pid].append(Opp(stat, ok))

    for pid, pf in hf.players.items():
        for stat in WALK_POSTFLOP:
            if stat in pf.stats:
                mark(pid, stat, pf.stats[stat][1])
    owners, cbettors = _chain(hf)
    floaters = _flop_callers_ip(hf, cbettors["flop"])
    for street in POSTFLOP:
        decs = [d for d in hf.decisions if d.street == street]
        owner, cb = owners[street], cbettors[street]
        owner_check = next(
            (i for i, d in enumerate(decs)
             if d.player == owner and d.first_on_street and d.n_aggr_before == 0
             and d.kind == "check"),
            None,
        )  # fmt: skip
        checked: set[str] = set()
        for i, d in enumerate(decs):
            p = d.player
            if d.kind in ("bet", "raise", "call", "fold"):
                out[p].append(Opp(f"agg_freq_{street}", d.aggressive))
            if d.n_aggr_before == 1 and cb and d.last_aggressor == cb and p != cb:
                mark(p, f"raise_cbet_{street}", d.kind == "raise")
            missed = owner_check is not None and i > owner_check and p != owner
            if missed and d.first_on_street and d.n_aggr_before == 0:
                mark(p, f"bet_vs_missed_cbet_{street}", d.kind == "bet")
                if street == "turn" and p in floaters:
                    mark(p, "float_turn", d.kind == "bet")
            if not d.first_on_street and d.n_aggr_before >= 1 and p in checked:
                mark(p, f"check_raise_{street}", d.kind == "raise")
            if d.first_on_street and d.kind == "check":
                checked.add(p)
    return out


def _splits(hf: HandFacts, pid: str) -> tuple[str, str]:
    seers = [p for p, f in hf.players.items() if f.saw_flop]
    rank = postflop_rank(hf.players[pid].position)
    ip = all(rank > postflop_rank(hf.players[p].position) for p in seers if p != pid)
    return ("ip" if ip else "oop", "hu" if len(seers) <= 2 else "multiway")


# ---------------------------------------------------------------- aggregation


def _cell(d: dict, *keys: str) -> Cell:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    return d.setdefault(keys[-1], Cell())


def _add_hand(
    out: dict[str, PosRaw], hand: dict, hf: HandFacts, gto: GtoTable | None,
    alias_map: Mapping[str, str],
) -> None:  # fmt: skip
    depths = _eff_depths(hand)
    post = postflop_opps(hf)
    for pid, pf in hf.players.items():
        raw = out.setdefault(alias_map.get(pid, pid), PosRaw())
        pos = pf.pos_key
        raw.hands[pos] += 1
        for o in preflop_opps(hf, pid, depths.get(pid)):
            g = gto.lookup(o.spot) if gto is not None and o.spot is not None else None
            _cell(raw.pre, pos, o.stat).add(o.success, g)
        if not pf.saw_flop:
            continue
        splits = _splits(hf, pid)
        for o in post[pid]:
            _cell(raw.post, pos, o.stat).add(o.success, None)
            for sp in splits:
                _cell(raw.split, pos, sp, o.stat).add(o.success, None)
        for street, pct in pf.bet_sizes:
            for grp in ("all", *splits):
                h = raw.hist.setdefault(pos, {}).setdefault(grp, {})
                h.setdefault(street, Counter())[hist_bucket(pct)] += 1


def aggregate_positional(
    hands: Iterable[dict], alias_map: Mapping[str, str] | None = None,
    gto: GtoTable | None = None,
) -> dict[str, PosRaw]:  # fmt: skip
    """Raw positional counts for every canonical player in `hands` (skipped hands ignored)."""
    alias_map = alias_map or {}
    out: dict[str, PosRaw] = {}
    for hand in hands:
        hf = walk_hand(hand)
        if hf is not None:
            _add_hand(out, hand, hf, gto, alias_map)
    return out


# ---------------------------------------------------------------- shrinkage + GTO


def prior_for(priors: Mapping, stat: str) -> tuple[float, float]:
    """(population mean, k) for a positional stat."""
    base = PRIOR_ALIAS.get(stat, stat)
    s = priors["stats"].get(base)
    if s is not None and not base.startswith("af_"):
        return float(s["mean"]), float(s["k"])
    mean, k = FALLBACK_PRIORS.get(stat, (0.5, 10))
    return float(mean), float(k)


def _pooled(cells: Mapping[str, Mapping[str, Cell]], stat: str) -> tuple[int, int]:
    n = sum(c[stat].n for c in cells.values() if stat in c)
    s = sum(c[stat].s for c in cells.values() if stat in c)
    return n, s


def _cell_obj(
    cell: Cell, stat: str, pos: str, pooled: tuple[int, int], priors: Mapping,
    gto: float | None,
) -> tuple[dict, float]:  # fmt: skip
    """(annotated stat object, population base) for one position cell."""
    pop, k = prior_for(priors, stat)
    pos_base = position_mean(priors, pos, PRIOR_ALIAS.get(stat, stat))
    base, k_cell = (pos_base, k) if pos_base is not None else (pop, k * REDUCED_K)
    player = beta_binomial(pooled[1], pooled[0], pop, k)["post_mean"]
    post = beta_binomial(cell.s, cell.n, shift_prior(base, player, pop), k_cell)
    return annotate(post, gto), base


def _preflop_gto(cell: Cell, stat: str, pos: str, bl: Mapping) -> float | None:
    if cell.gto_n:
        return cell.gto_sum / cell.gto_n
    base = PRIOR_ALIAS.get(stat, stat)
    if base != stat and stat not in ("call_open", "fold_to_3bet"):
        return None  # split stats compare with their own spot only
    return gto_value(bl, base, pos)


def _postflop_gto(stat: str, bl: Mapping, split: str | None = None) -> float | None:
    if split is not None:
        v = gto_value(bl, stat, split.upper() if split in ("ip", "oop") else split)
        if v is not None:
            return v
    return gto_value(bl, stat)


def _positions(raw: PosRaw) -> list[str]:
    known = [p for p in POSITION_ORDER if p in raw.hands]
    return known + sorted(p for p in raw.hands if p not in POSITION_ORDER)


def _pos_block(raw: PosRaw, pos: str, priors: Mapping, bl: Mapping) -> tuple[dict, dict]:
    pre, post, population = {}, {}, {}
    for stat, cell in raw.pre.get(pos, {}).items():
        obj, base = _cell_obj(cell, stat, pos, _pooled(raw.pre, stat), priors,
                              _preflop_gto(cell, stat, pos, bl))  # fmt: skip
        pre[stat], population[stat] = obj, base
    for stat, cell in raw.post.get(pos, {}).items():
        obj, base = _cell_obj(cell, stat, pos, _pooled(raw.post, stat), priors,
                              _postflop_gto(stat, bl))  # fmt: skip
        post[stat], population[stat] = obj, base
    split = {}
    for sp, cells in raw.split.get(pos, {}).items():
        split[sp] = {}
        for stat, cell in cells.items():
            k = post[stat]["k"]
            # same prior as the position cell: its posterior already contains these hands
            obj = beta_binomial(cell.s, cell.n, post[stat]["prior_mean"], k)
            split[sp][stat] = annotate(obj, _postflop_gto(stat, bl, sp))
    hist = {
        grp: {st: {b: h.get(st, Counter()).get(b, 0) for b in HIST_NAMES} for st in POSTFLOP}
        for grp, h in raw.hist.get(pos, {}).items()
    }
    block = {
        "hands": raw.hands[pos],
        "preflop": {s: pre[s] for s in PREFLOP_ROWS if s in pre},
        "postflop": {s: post[s] for s in POSTFLOP_ROWS if s in post},
        "postflop_split": {sp: split[sp] for sp in SPLITS if sp in split},
        "bet_size_hist": hist,
    }
    return block, population


def preflop_weighted(by_position: Mapping[str, Mapping]) -> dict:
    """Hand-weighted (weights = hands per position) preflop frequency vs GTO, per stat."""
    out: dict[str, dict] = {}
    for stat in WEIGHTED_STATS:
        rows = [
            (b["hands"], b["preflop"][stat]) for b in by_position.values()
            if stat in b["preflop"] and b["preflop"][stat]["gto"] is not None
        ]  # fmt: skip
        w = sum(h for h, _ in rows)
        if not w:
            continue
        player = sum(h * o["post_mean"] for h, o in rows) / w
        gto = sum(h * o["gto"] for h, o in rows) / w
        out[stat] = {
            "player": round(player, 4),
            "gto": round(gto, 4),
            "diff": round(player - gto, 4),
            "dev_gto": round(sum(h * o["dev_gto"] for h, o in rows) / w, 4),
            "n": sum(o["n"] for _, o in rows),
            "weight_hands": w,
        }
    if out:
        devs = [abs(v["dev_gto"]) for v in out.values()]
        out["_mean_abs_dev_gto"] = round(sum(devs) / len(devs), 4)
    return out


def build_matrix(
    raw: PosRaw, *, priors: Mapping | None = None, baselines: Mapping | None = None,
    canonical_id: str | None = None,
) -> dict:  # fmt: skip
    priors = priors or load_priors()
    bl = baselines or load_baselines()
    by_position, population = {}, {}
    for pos in _positions(raw):
        by_position[pos], population[pos] = _pos_block(raw, pos, priors, bl)
    return {
        "canonical_id": canonical_id,
        "hands": sum(raw.hands.values()),
        "positions": list(by_position),
        "hands_by_position": {p: b["hands"] for p, b in by_position.items()},
        "by_position": by_position,
        "population": population,
        "summary": {"preflop_weighted": preflop_weighted(by_position)},
    }


def positional_matrix(
    canonical_id: str, hands: Iterable[dict], *, alias_map: Mapping[str, str] | None = None,
    priors: Mapping | None = None, baselines: Mapping | None = None,
    gto: GtoTable | None = None,
) -> dict:  # fmt: skip
    """Positional matrix for one canonical player from normalized hand dicts."""
    table = gto if gto is not None else load_table()
    raw = aggregate_positional(hands, alias_map, table).get(canonical_id, PosRaw())
    m = build_matrix(raw, priors=priors, baselines=baselines, canonical_id=canonical_id)
    return {**m, "leaks": positional_leaks(m)}


def attach_positional(profile: dict, hands: Iterable[dict]) -> dict:
    """New profile dict with `positional` = positional_matrix(...) for its canonical_id.

    The contract's existing `by_position` block is left untouched.
    """
    aliases = {a: profile["canonical_id"] for a in profile.get("aliases", [])}
    m = positional_matrix(profile["canonical_id"], hands, alias_map=aliases)
    return {**profile, "positional": m}


# ---------------------------------------------------------------- leaks


def _label(stat: str, spot: str) -> str:
    pos, _, split = spot.partition(":")
    for g in GROUPS:
        for base in ("three_bet", "call_open"):
            if stat == f"{base}_vs_{g}":
                return f"{LABELS[base]} from {pos} vs {g} open"
    suffix = f" {split.upper() if split in ('ip', 'oop', 'hu') else split}" if split else ""
    return f"{LABELS.get(stat, stat)} from {pos}{suffix}"


def _candidates(matrix: Mapping) -> Iterable[tuple[str, str, Mapping]]:
    for pos, b in matrix["by_position"].items():
        for stat, obj in {**b["preflop"], **b["postflop"]}.items():
            yield stat, pos, obj
        for sp, cells in b["postflop_split"].items():
            for stat, obj in cells.items():
                if obj["n"] < b["postflop"][stat]["n"]:  # else it repeats the position cell
                    yield stat, f"{pos}:{sp}", obj


def positional_leaks(matrix: Mapping, max_leaks: int = MAX_LEAKS) -> list[dict]:
    """Leaks shaped like profile `leaks` entries plus `label`, ranked by |dev_gto| * n."""
    out = []
    for stat, spot, obj in _candidates(matrix):
        dev = obj.get("dev_gto")
        if obj["n"] < MIN_LEAK_N or not dev:
            continue
        pos = spot.partition(":")[0]
        sign = 1 if dev > 0 else -1
        label = _label(stat, spot)
        out.append({
            "stat": stat,
            "spot": spot,
            "player_freq": obj["post_mean"],
            "population": matrix.get("population", {}).get(pos, {}).get(stat),
            "gto": obj["gto"],
            "n": obj["n"],
            "ci90": obj["ci90"],
            "dev_gto": dev,
            "direction": "over" if sign > 0 else "under",
            "exploit": exploit_for(PRIOR_ALIAS.get(stat, stat), sign, pos),
            "label": f"{label} {100 * obj['post_mean']:.0f}% (n={obj['n']}) "
                     f"vs GTO {100 * obj['gto']:.0f}%",
            "_rank": abs(dev) * obj["n"],
        })  # fmt: skip
    out.sort(key=lambda lk: (-lk["_rank"], -lk["n"]))
    return [{k: v for k, v in lk.items() if k != "_rank"} for lk in out[:max_leaks]]
