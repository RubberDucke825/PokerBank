"""Aggregate per-hand facts into raw (opportunity, success) counts per canonical player.

Stat definitions (opportunity -> success), all per hand, first opportunity in the hand counts:
  vpip / pfr        had a preflop decision (walks excluded) -> called or raised / raised
  rfi               first decision with no raise and no limper -> raised
  steal             rfi opportunity from CO/BTN/SB -> raised
  limp              first decision, no raise yet, chips needed to call -> called
  cold_call         first decision facing exactly one raise -> called (blinds included)
  squeeze           first decision facing one raise plus >=1 caller -> raised
  fold_to_squeeze / call_vs_squeeze
                    opener or a caller of the open, first decision facing the squeeze
                    -> folded / called
  three_bet         any decision facing exactly one raise -> raised
  four_bet          any decision facing exactly two raises -> raised
  fold_to_three_bet opener facing a 3-bet -> folded
  cbet_flop         preflop last raiser, first flop decision, unbet -> bet
  cbet_turn/river   c-bet previous street, first decision, unbet -> bet
  fold_to_cbet_X    facing the street's c-bet (no raise in between) -> folded
  check_raise_flop  checked flop then faced a bet -> raised
  donk_flop         not PFA, PFA still to act after you, unbet -> bet
  probe_turn        flop checked through, not PFA, PFA to act after you, unbet turn -> bet
  af_X              (bets+raises) / calls on street X; stored as aggr vs aggr+calls
  agg_freq_X        (bets+raises) / (bets+raises+calls+folds) on street X (per decision)
  fold_to_four_bet  3-bettor facing a 4-bet -> folded
  five_bet          3-bettor facing a 4-bet -> raised
  fold_to_five_bet / six_bet  opener facing a 5-bet -> folded / raised
  fold_to_six_bet   3-bettor facing a 6-bet -> folded
  fold_to_steal     SB/BB first decision facing a lone CO/BTN/SB open -> folded
  raise_cbet_X      facing the street's c-bet (same opportunity as fold_to_cbet_X) -> raised
  bet_vs_missed_cbet_X  the c-bet owner (PFA on the flop, flop c-bettor on the turn) checked,
                    and you act later with no bet yet -> bet
  float_turn        bet_vs_missed_cbet_turn after calling the flop c-bet -> bet
  Positional cells, IP/OOP and situations: see spots.py / POSITIONS.md
  wtsd / wwsf       saw flop -> reached showdown / won money
  wsd               reached showdown -> won money
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pn.profile.charts import normalize_position, table_kind
from pn.profile.walk import POSTFLOP, HandFacts, PlayerFacts, hist_bucket, walk_hand

PREFLOP_STATS = (
    "vpip", "pfr", "three_bet", "fold_to_three_bet", "limp", "cold_call", "steal", "squeeze",
    "rfi", "four_bet", "fold_to_squeeze", "call_vs_squeeze", "fold_to_four_bet", "fold_to_steal",
    "five_bet", "fold_to_five_bet", "six_bet", "fold_to_six_bet",
)  # fmt: skip
POSTFLOP_STATS = (
    "cbet_flop", "cbet_turn", "cbet_river",
    "fold_to_cbet_flop", "fold_to_cbet_turn", "fold_to_cbet_river",
    "check_raise_flop", "donk_flop", "probe_turn",
    "raise_cbet_flop", "raise_cbet_turn", "raise_cbet_river",
    "bet_vs_missed_cbet_flop", "bet_vs_missed_cbet_turn", "float_turn",
)  # fmt: skip
AF_STATS = ("af_flop", "af_turn", "af_river")
AFQ_STATS = ("agg_freq_flop", "agg_freq_turn", "agg_freq_river")
SHOWDOWN_STATS = ("wtsd", "wsd", "wwsf")
ALL_STATS = PREFLOP_STATS + POSTFLOP_STATS + AF_STATS + AFQ_STATS + SHOWDOWN_STATS
POSITION_STATS = ALL_STATS
VS_OPENER_STATS = ("three_bet", "cold_call", "fold_to_steal")
RANGE_STATS = ("rfi", "cold_call", "three_bet")
DEPTH_STATS = ("vpip", "pfr", "three_bet", "fold_to_three_bet", "cbet_flop", "fold_to_cbet_flop")
DEPTH_NAMES = ("<=60bb", "60-150bb", "150-250bb", ">250bb")
PIP_STATS = POSTFLOP_STATS + SHOWDOWN_STATS
SIZE_BUCKETS = {"flop": ("25", "33", "50", "100"), "turn": ("75", "150"), "river": ("75", "150")}
HIST_NAMES = ("0-30", "30-45", "45-70", "70-120", "120+")


@dataclass
class ShownHand:
    hand_id: str
    position: str
    preflop_action: str | None
    cards: list[str]
    board: list[str]
    line: str | None
    n_players: int


@dataclass
class RawStats:
    """Mutable accumulator; counts are [opportunities, successes]."""

    hands: int = 0
    showdowns_seen: int = 0
    overall: dict[str, list[int]] = field(default_factory=dict)
    # PT4-style matrix: position key (BTN_HU etc.) or IP/OOP -> stat -> [opp, succ]
    by_position: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    by_pip: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    fold_vs_size: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    bet_size_hist: dict[str, dict[str, int]] = field(default_factory=dict)
    actions: Counter = field(default_factory=Counter)
    shown: list[ShownHand] = field(default_factory=list)
    lines: Counter = field(default_factory=Counter)
    table_sizes: Counter = field(default_factory=Counter)
    # (table kind, chart position, stat) -> [opp, succ] for rfi / cold_call / three_bet
    range_freq: dict[tuple[str, str, str], list[int]] = field(default_factory=dict)
    by_depth: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    three_bet_sizes: list[tuple[float, bool, int]] = field(default_factory=list)
    # position -> opener group -> stat -> [opp, succ]
    by_vs_opener: dict[str, dict[str, dict[str, list[int]]]] = field(default_factory=dict)
    # (stat, cell, preflop situation) -> opportunities; cell = all | <pos> | <pos> vs <group>
    situ: Counter = field(default_factory=Counter)
    bet_size_hist_by_pos: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)

    def count(self, stat: str) -> tuple[int, int]:
        n, s = self.overall.get(stat, [0, 0])
        return n, s


def _bump(d: dict[str, list[int]], key: str, success: bool) -> None:
    c = d.setdefault(key, [0, 0])
    c[0] += 1
    c[1] += int(success)


def _add_player(raw: RawStats, pf: PlayerFacts, hf: HandFacts) -> None:
    raw.hands += 1
    raw.table_sizes[hf.n_players] += 1
    for stat, (opp, ok) in pf.stats.items():
        if not opp:
            continue
        _bump(raw.overall, stat, ok)
        if stat in POSITION_STATS:
            _bump(raw.by_position.setdefault(pf.pos_key, {}), stat, ok)
        if stat in pf.ipos:
            _bump(raw.by_position.setdefault(pf.ipos[stat], {}), stat, ok)
        group = pf.group.get(stat)
        if stat in VS_OPENER_STATS and group:
            _bump(raw.by_vs_opener.setdefault(pf.pos_key, {}).setdefault(group, {}), stat, ok)
        situ = pf.situ.get(stat)
        if situ is not None:
            raw.situ[(stat, "all", situ)] += 1
            raw.situ[(stat, pf.pos_key, situ)] += 1
            if group:
                raw.situ[(stat, f"{pf.pos_key} vs {group}", situ)] += 1
        if stat in RANGE_STATS:
            table = table_kind(hf.n_players)
            key = (table, normalize_position(pf.position, table), stat)
            c = raw.range_freq.setdefault(key, [0, 0])
            c[0] += 1
            c[1] += int(ok)
        if stat in pf.pip:
            _bump(raw.by_pip.setdefault(pf.pip[stat], {}), stat, ok)
        if stat in DEPTH_STATS and pf.depth is not None:
            _bump(raw.by_depth.setdefault(pf.depth, {}), stat, ok)
    for street in POSTFLOP:
        cells = [raw.overall, raw.by_position.setdefault(pf.pos_key, {})]
        if street in pf.street_ip:
            cells.append(raw.by_position.setdefault("IP" if pf.street_ip[street] else "OOP", {}))
        for stat, tag, other in ((f"af_{street}", "af", "call"), (f"agg_freq_{street}", "afq",
                                                                     "pass")):  # fmt: skip
            a = pf.actions.get((tag, street, "aggr"), 0)
            c = pf.actions.get((tag, street, other), 0)
            if a + c:
                for d in cells:
                    cnt = d.setdefault(stat, [0, 0])
                    cnt[0] += a + c
                    cnt[1] += a
    for key, n in pf.actions.items():
        if key[0] not in ("af", "afq"):
            raw.actions[key] += n
    for street, bucket, folded, _aggressor in pf.fold_vs_size:
        _bump(raw.fold_vs_size.setdefault(street, {}), bucket, folded)
    for street, pct in pf.bet_sizes:
        b = hist_bucket(pct)
        h = raw.bet_size_hist.setdefault(street, {})
        h[b] = h.get(b, 0) + 1
        cells = [pf.pos_key]
        if street in pf.street_ip:
            cells.append("IP" if pf.street_ip[street] else "OOP")
        for cell in cells:
            hp = raw.bet_size_hist_by_pos.setdefault(cell, {}).setdefault(street, {})
            hp[b] = hp.get(b, 0) + 1
    raw.three_bet_sizes.extend(pf.three_bet_sizes)
    if pf.line:
        raw.lines[pf.line] += 1
    if pf.cards and pf.went_to_sd:
        raw.showdowns_seen += 1
    if pf.cards:
        raw.shown.append(
            ShownHand(
                hand_id=hf.hand_id,
                position=pf.position,
                preflop_action=pf.preflop_action,
                cards=pf.cards,
                board=hf.board,
                line=pf.line if pf.went_to_sd else None,
                n_players=hf.n_players,
            )
        )


def walk_all(hands: Iterable[dict]) -> list[HandFacts]:
    return [hf for hf in (walk_hand(h) for h in hands) if hf is not None]


def aggregate(
    walked: Iterable[HandFacts], alias_map: Mapping[str, str] | None = None
) -> dict[str, RawStats]:
    """Raw counts for every canonical player appearing in `walked`."""
    alias_map = alias_map or {}
    out: dict[str, RawStats] = {}
    for hf in walked:
        for pid, pf in hf.players.items():
            cid = alias_map.get(pid, pid)
            _add_player(out.setdefault(cid, RawStats()), pf, hf)
    return out


def compute_raw(
    hands: Iterable[dict], canonical_id: str, alias_map: Mapping[str, str] | None = None
) -> RawStats:
    return aggregate(walk_all(hands), alias_map).get(canonical_id, RawStats())
