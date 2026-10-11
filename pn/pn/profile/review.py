"""Hero review: is each GTO deviation a justified exploit or a leak? Rules in REVIEW.md."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from pn.profile import archetype
from pn.profile.stats import RawStats
from pn.profile.walk import HandFacts

MIN_OPP_N = 10  # opponent counter-stat opportunities needed to trust its CI
MIN_ARCH_CONFIDENCE = 0.3  # archetype fallback when the counter-stat sample is small
MIN_COVERAGE = 0.5  # share of hero's opportunities with a known opponent tendency
MAX_NEUTRAL_WIDTH = 0.25  # CI containing GTO counts as "GTO-like" only if this tight
MAX_REASON = 90  # chars in the short reason shown in tables; full text in `detail`

# hero stat -> (opponent counter-stat, relation). relation +1: hero should move in the SAME
# direction as the opponent's deviation (e.g. 3-bet more vs players who fold to 3-bets too
# much); -1: OPPOSITE (e.g. fold more to c-bets vs players who c-bet less than GTO).
COUNTER: dict[str, tuple[str, int]] = {
    "fold_to_cbet_flop": ("cbet_flop", -1),
    "fold_to_cbet_turn": ("cbet_turn", -1),
    "fold_to_cbet_river": ("cbet_river", -1),
    "fold_to_three_bet": ("three_bet", -1),
    "fold_to_squeeze": ("squeeze", -1),
    "call_vs_squeeze": ("squeeze", 1),
    "three_bet": ("fold_to_three_bet", 1),
    "four_bet": ("three_bet", 1),
    "squeeze": ("fold_to_squeeze", 1),
    "cold_call": ("rfi", 1),
    "rfi": ("three_bet", -1),
    "steal": ("three_bet", -1),
    "pfr": ("three_bet", -1),
    "limp": ("pfr", -1),
    "vpip": ("fold_to_cbet_flop", 1),
    "cbet_flop": ("fold_to_cbet_flop", 1),
    "cbet_turn": ("fold_to_cbet_turn", 1),
    "cbet_river": ("fold_to_cbet_river", 1),
    "check_raise_flop": ("cbet_flop", 1),
    "donk_flop": ("cbet_flop", -1),
    "probe_turn": ("fold_to_cbet_turn", 1),
    "af_flop": ("fold_to_cbet_flop", 1),
    "af_turn": ("fold_to_cbet_turn", 1),
    "af_river": ("fold_to_cbet_river", 1),
    "wtsd": ("af_river", 1),
}
OUTCOME_STATS = frozenset({"wsd", "wwsf"})  # results, not decisions: always `unknown`
FVS_COUNTER = {"flop": "af_flop", "turn": "af_turn", "river": "af_river"}  # opposite relation

# Archetype fallback: sign of the opponent's deviation implied by its label, per counter family.
_AGGR = {"aggressive": 1, "lag": 1, "nit": -1, "calling_station": -1, "punching_bag": -1}
_FOLD = {"punching_bag": 1, "nit": 1, "calling_station": -1, "aggressive": -1, "lag": -1}
_LOOSE = {"lag": 1, "calling_station": 1, "aggressive": 1, "nit": -1, "tag": -1}


def _family(stat: str) -> dict[str, int]:
    if stat.startswith("fold_to"):
        return _FOLD
    if stat in ("rfi", "pfr", "vpip", "steal"):
        return _LOOSE
    return _AGGR


@dataclass
class OppView:
    """An opponent's counter-stat posterior and archetype."""

    cid: str
    stats: Mapping[str, Mapping]
    arch: Mapping
    hands: int


@dataclass
class Tally:
    total: float = 0.0
    support: float = 0.0
    against: float = 0.0
    unknown: float = 0.0
    by_opp: dict[str, float] = field(default_factory=dict)


def opponent_sign(view: OppView, counter: str) -> tuple[int | None, str]:
    """(+1 above GTO / -1 below / 0 GTO-like / None unknown, basis)."""
    s = view.stats.get(counter)
    if s is not None and s["n"] >= MIN_OPP_N and s.get("dev_gto") is not None:
        d = s["dev_gto"]
        if d != 0:
            return (1 if d > 0 else -1), "measured"
        lo, hi = s["ci90"]
        if counter.startswith("af_"):
            lo, hi = lo / (1 + lo), hi / (1 + hi)
        if hi - lo <= MAX_NEUTRAL_WIDTH:
            return 0, "measured"
        # wide CI around GTO: inconclusive, fall through to the archetype
    label = view.arch.get("label", "unknown")
    if label != "unknown" and view.arch.get("confidence", 0.0) >= MIN_ARCH_CONFIDENCE:
        return _family(counter).get(label, 0), f"archetype:{label}"
    return None, "unknown"


def _spot_ok(pf, stat: str, spot: str) -> bool:
    if spot == "all":
        return True
    if spot in ("hu", "multiway"):
        return pf.pip.get(stat) == spot
    if spot in ("IP", "OOP"):
        return pf.ipos.get(stat) == spot
    if " vs " in spot:
        pos, group = spot.split(" vs ")
        return pf.pos_key == pos and pf.group.get(stat) == group
    return pf.pos_key == spot


def opportunities(
    walked: list[HandFacts], hero: str, stat: str, spot: str, alias_map: Mapping[str, str]
) -> dict[str, float]:
    """Hero's opportunities for (stat, spot), weighted per opponent canonical id."""
    out: dict[str, float] = {}

    def add(pids: tuple[str, ...] | list[str], w: float) -> None:
        pids = [p for p in pids if p and alias_map.get(p, p) != hero]
        for p in pids:
            cid = alias_map.get(p, p)
            out[cid] = out.get(cid, 0.0) + w / len(pids)

    for hf in walked:
        mine = [p for p in hf.players if alias_map.get(p, p) == hero]
        if not mine:
            continue
        pf = hf.players[mine[0]]
        if stat == "fold_vs_size":
            street, _, bucket = spot.partition("_")
            for st, b, _folded, aggr in pf.fold_vs_size:
                if st == street and b == bucket and aggr:
                    add((aggr,), 1.0)
        elif stat.startswith("af_"):
            street = stat[3:]
            n = pf.actions.get(("af", street, "aggr"), 0) + pf.actions.get(
                ("af", street, "call"), 0
            )
            if n:
                add([p for p, f in hf.players.items() if f.saw_flop and p != pf.player_id], n)
        elif stat in pf.stats and _spot_ok(pf, stat, spot):
            add(pf.vs.get(stat, ()), 1.0)
    return out


def _counter_for(stat: str, spot: str) -> tuple[str, int] | None:
    if stat == "fold_vs_size":
        return FVS_COUNTER[spot.partition("_")[0]], -1
    return COUNTER.get(stat)


def judge(
    leak: Mapping,
    opps: Mapping[str, float],
    views: Mapping[str, OppView],
    names: Mapping[str, str],
) -> dict:
    """Verdict for one hero leak given opportunity weights per opponent."""
    stat, spot = leak["stat"], leak["spot"]
    sign = 1 if leak["dev_gto"] > 0 else -1
    base = {"opponents": []}
    if stat in OUTCOME_STATS:
        return _verdict(base, "unknown", "result stat, not a decision",
                        f"{stat} is a result, not a decision; review the lines behind it.")  # fmt: skip
    counter = _counter_for(stat, spot)
    if counter is None:
        return _verdict(base, "unknown", f"no exploit rule for {stat}", "")
    cstat, relation = counter
    need = sign * relation  # opponent deviation sign that would justify hero's deviation
    t = Tally()
    rows = []
    for cid, w in sorted(opps.items(), key=lambda kv: -kv[1]):
        view = views.get(cid)
        osign, basis = opponent_sign(view, cstat) if view else (None, "unknown")
        t.total += w
        if osign is None:
            t.unknown += w
        elif osign == need:
            t.support += w
        else:
            t.against += w  # opposite deviation, or GTO-like: hero's deviation is not warranted
        s = view.stats.get(cstat) if view else None
        rows.append({"id": cid, "name": names.get(cid, cid), "share": round(w, 2),
                     "counter_stat": cstat,
                     "counter_post": None if s is None else s["post_mean"],
                     "counter_n": 0 if s is None else s["n"],
                     "sign": osign, "basis": basis,
                     "archetype": view.arch.get("label") if view else "unknown"})  # fmt: skip
    base["opponents"] = rows[:5]
    if t.total == 0:
        return _verdict(base, "unknown", "no opponent attributed", "")
    known = t.total - t.unknown
    if known / t.total < MIN_COVERAGE:
        short = f"opp reads too thin: {known:.0f}/{t.total:.0f} opps with {cstat} n>={MIN_OPP_N}"
        detail = (f"only {known / t.total:.0%} of the {t.total:.0f} opportunities are vs "
                  f"opponents with a reliable {cstat} read (n>={MIN_OPP_N}).")  # fmt: skip
        return _verdict(base, "unknown", short, detail)
    direction = "above" if need > 0 else "below"
    share = t.support / known
    top = ", ".join(f"{r['name']} ({r['archetype']})" for r in rows[:3])
    verdict = "justified" if t.support > t.against else "leak"
    hero_dir = "more" if sign > 0 else "less"
    short_top = ", ".join(r["name"] for r in rows[:3])
    short = f"{t.support:.0f}/{known:.0f} opps vs {cstat} {direction} GTO; main: {short_top}"
    detail = (f"{share:.0%} of {known:.0f} attributable opportunities were vs opponents whose "
              f"{cstat} is {direction} GTO, which is what {hero_dir} {stat} exploits "
              f"(GTO-like or opposite: {t.against / known:.0%}); main opponents: {top}.")  # fmt: skip
    return _verdict(base, verdict, short, detail)


def _verdict(base: dict, verdict: str, short: str, detail: str) -> dict:
    """`reason` is capped at MAX_REASON chars for table cells; `detail` is the full text."""
    if len(short) > MAX_REASON:
        short = short[: MAX_REASON - 1].rstrip(" ,;") + "…"
    return {**base, "verdict": verdict, "reason": short, "detail": detail or short}


def _p(x: float) -> float:
    return x / (1.0 + x)


def cost(leak: Mapping) -> float:
    """EV-ish magnitude proxy: CI-aware deviation (frequency scale) x opportunities."""
    dev = abs(leak["dev_gto"])
    if leak["stat"].startswith("af_"):
        lo, hi = leak["ci90"]
        g = _p(leak["gto"])
        dev = max(_p(lo) - g, g - _p(hi), 0.0)
    return round(dev * leak["n"], 3)


def review(
    hero: str,
    leaks: list[Mapping],
    walked: list[HandFacts],
    raw_all: Mapping[str, RawStats],
    stats_for: Callable[[str], Mapping[str, Mapping]],
    alias_map: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
) -> list[dict]:
    """Annotate each hero leak with verdict, reason and cost; sorted by cost (desc).

    stats_for(cid) -> that player's annotated `stats` block (posteriors with dev_gto)."""
    alias_map = alias_map or {}
    names = names or {}
    cache: dict[str, OppView] = {}

    def view(cid: str) -> OppView:
        if cid not in cache:
            st = stats_for(cid)
            hands = raw_all[cid].hands if cid in raw_all else 0
            cache[cid] = OppView(cid, st, archetype.classify(st, hands), hands)
        return cache[cid]

    out = []
    for lk in leaks:
        opps = opportunities(walked, hero, lk["stat"], lk["spot"], alias_map)
        views = {cid: view(cid) for cid in opps}
        out.append({**lk, **judge(lk, opps, views, names), "cost": cost(lk)})
    out.sort(key=lambda r: -r["cost"])
    return out
