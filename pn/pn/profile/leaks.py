"""GTO leaks: stat objects whose 90% CI excludes the GTO baseline.

Candidates are POSITIONAL cells only (PT4-style): by_position (spot = position such as `BB`,
`BTN_HU`, or `IP`/`OOP`), by_position_vs_opener (spot `<pos> vs <group>`, e.g. `BB vs CO`)
and fold_vs_size (stat `fold_vs_size`, spot `<street>_<bucket>`). Table-wide numbers are not
leak candidates: a "3bet 5%" mixes seats with very different GTO frequencies. A candidate needs n >= MIN_LEAK_N and dev_gto != 0: with fewer
opportunities the posterior is mostly the population prior, so the "leak" would describe the
population, not the player. Ranking: |dev_gto| (AF stats
converted to the aggr/(aggr+calls) scale so they are comparable with frequencies), then n.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pn.profile.tips import exploit_for

MAX_LEAKS = 15
MIN_LEAK_N = 10
PopFn = Callable[[str, str], float | None]


def _p(x: float) -> float:
    return x / (1.0 + x)


def _rank_dev(stat: str, obj: Mapping) -> float:
    if not stat.startswith("af_"):
        return abs(obj["dev_gto"])
    lo, hi = obj["ci90"]
    g = _p(obj["gto"])
    return max(_p(lo) - g, g - _p(hi), 0.0)


def _candidates(
    by_position: Mapping, by_vs_opener: Mapping, fold_vs_size: Mapping
) -> list[tuple[str, str, Mapping]]:
    out = [(n, pos, o) for pos, d in by_position.items() for n, o in d.items()]
    out += [(n, f"{pos} vs {grp}", o) for pos, gd in by_vs_opener.items()
            for grp, d in gd.items() for n, o in d.items()]  # fmt: skip
    out += [
        ("fold_vs_size", f"{st}_{b}", o) for st, d in fold_vs_size.items() for b, o in d.items()
    ]
    return out


def build_leaks(
    by_position: Mapping,
    by_vs_opener: Mapping,
    fold_vs_size: Mapping,
    population: PopFn,
    max_leaks: int = MAX_LEAKS,
) -> list[dict]:
    leaks = []
    for stat, spot, obj in _candidates(by_position, by_vs_opener, fold_vs_size):
        dev = obj.get("dev_gto")
        if obj["n"] < MIN_LEAK_N or not dev:
            continue
        sign = 1 if dev > 0 else -1
        leaks.append({
            "stat": stat,
            "spot": spot,
            "player_freq": obj["post_mean"],
            "population": population(stat, spot),
            "gto": obj["gto"],
            "n": obj["n"],
            "ci90": obj["ci90"],
            "dev_gto": dev,
            "direction": "over" if sign > 0 else "under",
            "exploit": exploit_for(stat, sign, spot),
            "_rank": _rank_dev(stat, obj),
        })  # fmt: skip
    leaks.sort(key=lambda lk: (-lk["_rank"], -lk["n"]))
    return [{k: v for k, v in lk.items() if k != "_rank"} for lk in leaks[:max_leaks]]
