"""Plain-English exploit tips.

Order: (1) GTO leaks (see leaks.py; already ranked by |dev_gto| then n), each citing the
player's frequency, n, 90% CI, the GTO baseline and the population; (2) population
deviations not already covered by a leak: a stat qualifies when its 90% CI lies entirely on
one side of the population mean AND |posterior mean - population mean| >= MARGIN[stat]
(default 0.05), sorted by how far the CI clears the population mean; (3) pairwise tips.
Every tip cites n and the CI.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_MARGIN = 0.05
MARGIN = {"three_bet": 0.025, "squeeze": 0.02, "check_raise_flop": 0.03, "donk_flop": 0.04,
          "af_flop": 0.6, "af_turn": 0.6, "af_river": 0.6}  # fmt: skip
MAX_TIPS = 12

# stat -> (subject phrase, advice if HIGH, advice if LOW). `{}` is replaced by the street.
RULES: dict[str, tuple[str, str | None, str | None]] = {
    "fold_to_cbet_flop": ("to flop c-bets", "c-bet flops wide, including air",
                          "value bet thinner, don't bluff the flop"),
    "fold_to_cbet_turn": ("to turn barrels", "double-barrel turns with air",
                          "value bet thinner, don't bluff the turn"),
    "fold_to_cbet_river": ("to river barrels", "triple-barrel bluffs work",
                           "value bet thinner, never bluff the river"),
    "three_bet": ("3-bets", "4-bet value hands lighter and open tighter in front of them",
                  "steal more; give their rare 3-bets lots of respect"),
    "fold_to_three_bet": ("folds to 3-bets", "3-bet their opens light",
                          "3-bet them for value only, not as a bluff"),
    "limp": ("limps", "isolation-raise their limps wide", "their limps are traps or strong; raise them tighter"),
    "cbet_flop": ("c-bets flops", "float and raise their flop c-bets",
                  "their flop c-bets are strong; check-fold more and stab when they check"),
    "cbet_turn": ("barrels turns", "call flops lighter, they keep firing; raise their turns",
                  "stab turns when they check back the flop"),
    "check_raise_flop": ("check-raises flops", "c-bet less with weak hands",
                         "c-bet freely; their check-calls are capped, their raises are nutted"),
    "donk_flop": ("donk-bets flops", "raise their leads; they are often weak/medium",
                  "their rare leads are strong; fold marginal hands"),
    "steal": ("steals", "3-bet and defend wider against their steals",
              "respect their late-position opens"),
    "wtsd": ("goes to showdown", "no bluffs; value bet thin", "bluff them more often"),
    "vpip": ("plays", "value bet wider preflop and postflop", "steal their blinds more"),
    "pfr": ("raises preflop", "3-bet and call their opens wider",
            "their raises are strong; fold more to them, attack their limps and calls"),
    "rfi": ("opens", "3-bet their opens wider and defend more", "steal when they fold to you"),
    "four_bet": ("4-bets", "5-bet jam a wider value range vs them",
                 "their 4-bets are nutted; 3-bet bluff them freely"),
    "cold_call": ("cold-calls", "raise bigger for value; squeeze their flats",
                  "their cold-calls are strong; c-bet less"),
    "squeeze": ("squeezes", "flat less in front of them; 4-bet their squeezes lighter",
                "flat open-raises more freely"),
    "fold_to_squeeze": ("folds to squeezes", "squeeze them light", "squeeze them for value only"),
    "call_vs_squeeze": ("calls squeezes", "squeeze for value bigger; c-bet thin post",
                        "squeeze bluffs work against them"),
    "cbet_river": ("triple-barrels", "bluff-catch rivers wider", "fold to their river bets more"),
    "probe_turn": ("probes turns", "check back flops with medium hands and raise their probes",
                   "c-bet your checked-back turns freely"),
    "wsd": ("wins at showdown", "don't bluff-catch them light", "value bet thinner"),
    "wwsf": ("wins when seeing the flop", "fight harder for pots postflop",
             "stab pots they check to you"),
}  # fmt: skip
AF_ADVICE = ("call down lighter, let them bluff",
             "their bets/raises are strong; bet for value and check back less")  # fmt: skip
AF_RULE = (
    (
        "Very aggressive on the {street} (AF {v:.2f}, n={n}, CI {lo:.2f}-{hi:.2f}): call down lighter, "
        "let them bluff."
    ),
    (
        "Passive on the {street} (AF {v:.2f}, n={n}, CI {lo:.2f}-{hi:.2f}): their bets/raises are strong; "
        "bet for value and check back less."
    ),
)
SIZE_HIGH = {
    ("flop", "25"): "c-bet tiny with your whole range",
    ("flop", "33"): "c-bet small with your whole range",
    ("flop", "50"): "bluff the flop with half-pot bets",
    ("flop", "100"): "pot-size flop bluffs work",
    ("turn", "75"): "bluff turns with 75% bets",
    ("turn", "150"): "overbet turns with air",
    ("river", "75"): "bluff rivers with 75% bets",
    ("river", "150"): "overbet-bluff rivers",
}


@dataclass(frozen=True)
class Tip:
    text: str
    strength: float
    key: tuple[str, str] = ("", "")  # (stat, spot) to de-duplicate against leaks


def _pct(x: float) -> str:
    return f"{round(100 * x)}"


def exploit_for(stat: str, sign: int, spot: str | None = None) -> str:
    """Exploit sentence for a stat that is too high (sign > 0) or too low (sign < 0)."""
    if stat == "fold_vs_size" and spot:
        street, _, bucket = spot.partition("_")
        return SIZE_HIGH[(street, bucket)] if sign > 0 else "value bet thinner, don't bluff"
    if stat.startswith("af_"):
        return AF_ADVICE[0] if sign > 0 else AF_ADVICE[1]
    rule = RULES.get(stat)
    advice = None if rule is None else (rule[1] if sign > 0 else rule[2])
    return advice or f"{'over' if sign > 0 else 'under'}-uses {stat}; adjust toward GTO"


def _spot_phrase(spot: str | None) -> str:
    if spot in (None, "all"):
        return ""
    if spot == "hu":
        return " heads-up"
    if spot == "multiway":
        return " in multiway pots"
    if spot == "IP":
        return " in position"
    if spot == "OOP":
        return " out of position"
    if spot.startswith(("<", ">")) or spot.endswith("bb"):
        return f" at {spot} deep"
    if " vs " in spot:
        pos, group = spot.split(" vs ")
        return f" from {_seat(pos)} vs {_seat(group)} open"
    return f" from {_seat(spot)}"


def _seat(key: str) -> str:
    return f"{key[:-3]} (heads-up)" if key.endswith("_HU") else key


def leak_tip(leak: Mapping) -> str:
    """One sentence for a leak entry (leaks.py), citing n, CI, GTO and population."""
    lo, hi = leak["ci90"]
    v, g, p = leak["player_freq"], leak["gto"], leak["population"]
    if leak["stat"].startswith("af_"):
        street = leak["stat"][3:]
        ref = f"GTO {g:.2f}" + ("" if p is None else f", pop {p:.2f}")
        head = f"AF {v:.2f} on the {street} (n={leak['n']}, CI {lo:.2f}-{hi:.2f}; {ref})"
        return f"{head}: {leak['exploit']}."
    ref = f"GTO {_pct(g)}%" + ("" if p is None else f", pop {_pct(p)}%")
    cite = f"(n={leak['n']}, CI {_pct(lo)}-{_pct(hi)}; {ref})"
    if leak["stat"] == "fold_vs_size":
        street, _, bucket = leak["spot"].partition("_")
        return f"Folds {_pct(v)}% {cite} to {bucket}% {street} bets: {leak['exploit']}."
    subject = RULES.get(leak["stat"], (leak["stat"].replace("_", " "),))[0]
    if subject.startswith("to "):
        subject = "folds " + subject
    return (f"{subject[0].upper()}{subject[1:]} {_pct(v)}%{_spot_phrase(leak['spot'])} "
            f"{cite}: {leak['exploit']}.")  # fmt: skip


def _deviation(stat: Mapping, pop: float, margin: float) -> tuple[int, float] | None:
    """(+1 high / -1 low, strength) when CI clears pop and the mean differs by >= margin."""
    lo, hi = stat["ci90"]
    m = stat["post_mean"]
    if stat["n"] == 0 or abs(m - pop) < margin:
        return None
    if lo > pop:
        return 1, (lo - pop) / margin
    if hi < pop:
        return -1, (pop - hi) / margin
    return None


def _cite(stat: Mapping) -> str:
    lo, hi = stat["ci90"]
    return f"(n={stat['n']}, CI {_pct(lo)}-{_pct(hi)})"


def _stat_tips(stats: Mapping[str, Mapping], pop: Mapping[str, float]) -> list[Tip]:
    out: list[Tip] = []
    for name, (subject, hi_advice, lo_advice) in RULES.items():
        if name not in stats or name not in pop:
            continue
        dev = _deviation(stats[name], pop[name], MARGIN.get(name, DEFAULT_MARGIN))
        if dev is None:
            continue
        sign, strength = dev
        advice = hi_advice if sign > 0 else lo_advice
        if advice is None:
            continue
        s = stats[name]
        qual = "" if sign > 0 else "only "
        if name.startswith("fold_to_cbet"):
            text = f"Folds {qual}{_pct(s['post_mean'])}% {_cite(s)} {subject}: {advice}."
        else:
            text = f"{subject[0].upper()}{subject[1:]} {qual}{_pct(s['post_mean'])}% {_cite(s)}: "
            text += f"{advice}."
        out.append(Tip(text, strength, (name, "all")))
    for street in ("flop", "turn", "river"):
        name = f"af_{street}"
        if name not in stats or name not in pop:
            continue
        dev = _deviation(stats[name], pop[name], MARGIN[name])
        if dev is None:
            continue
        s = stats[name]
        tmpl = AF_RULE[0] if dev[0] > 0 else AF_RULE[1]
        out.append(Tip(tmpl.format(street=street, v=s["post_mean"], n=s["n"],
                                   lo=s["ci90"][0], hi=s["ci90"][1]), dev[1],
                       (name, "all")))  # fmt: skip
    return out


def _size_tips(fold_vs_size: Mapping, pop_sizes: Mapping) -> list[Tip]:
    out: list[Tip] = []
    for street, buckets in fold_vs_size.items():
        for bucket, s in buckets.items():
            pop = pop_sizes.get(street, {}).get(bucket)
            if pop is None:
                continue
            dev = _deviation(s, pop, DEFAULT_MARGIN)
            if dev is None:
                continue
            if dev[0] > 0:
                advice = SIZE_HIGH[(street, bucket)]
                text = f"Folds {_pct(s['post_mean'])}% {_cite(s)} to {bucket}% {street} bets: "
            else:
                advice = "value bet thinner, don't bluff"
                text = f"Folds only {_pct(s['post_mean'])}% {_cite(s)} to {bucket}% {street} bets: "
            out.append(Tip(text + advice + ".", dev[1], ("fold_vs_size", f"{street}_{bucket}")))
    return out


def _pair_tips(vs: Mapping[str, Mapping], names: Mapping[str, str]) -> list[Tip]:
    out: list[Tip] = []
    for other, entry in vs.items():
        s = entry.get("fold_to_their_bet")
        if not s or s["n"] == 0:
            continue
        dev = _deviation(s, s["prior_mean"], DEFAULT_MARGIN)
        if dev is None:
            continue
        name = names.get(other, other)
        if dev[0] > 0:
            text = (f"vs {name}, they fold to {name}'s bets {_pct(s['post_mean'])}% {_cite(s)}: "
                    f"{name} should bluff them more.")  # fmt: skip
        else:
            text = (f"vs {name}, they rarely fold to {name}'s bets ({_pct(s['post_mean'])}% "
                    f"{_cite(s)}): {name} should value bet thin and not bluff.")  # fmt: skip
        out.append(Tip(text, dev[1]))
    return out


def generate_tips(
    stats: Mapping[str, Mapping],
    fold_vs_size: Mapping,
    vs: Mapping[str, Mapping],
    priors: Mapping,
    names: Mapping[str, str] | None = None,
    max_tips: int = MAX_TIPS,
    leaks: list[Mapping] | None = None,
) -> list[str]:
    pop = {k: float(v["mean"]) for k, v in priors["stats"].items()}
    pop_sizes = {st: v for st, v in priors.get("fold_vs_size", {}).items() if st != "k"}
    out: list[str] = []
    seen_stats: set[str] = set()
    for lk in leaks or []:  # strongest leak per stat only; the full list stays in `leaks`
        key = lk["stat"] if lk["stat"] != "fold_vs_size" else f"fvs_{lk['spot']}"
        if key not in seen_stats:
            seen_stats.add(key)
            out.append(leak_tip(lk))
    covered = {(lk["stat"], lk["spot"]) for lk in leaks or []}
    covered |= {(lk["stat"], "all") for lk in leaks or []}
    pop_tips = _stat_tips(stats, pop) + _size_tips(fold_vs_size, pop_sizes)
    pop_tips = sorted((t for t in pop_tips if t.key not in covered), key=lambda t: -t.strength)
    out += [t.text for t in pop_tips]
    out += [t.text for t in sorted(_pair_tips(vs, names or {}), key=lambda t: -t.strength)]
    return out[:max_tips]
