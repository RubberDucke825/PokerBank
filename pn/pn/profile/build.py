"""Assemble the player profile JSON (contracts/profile.md)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from functools import lru_cache

from pn.profile import archetype, gtowizard, leaks, lines, pairwise, preflop_range, tips
from pn.profile.baselines import annotate, ci_dev, gto_value, load_baselines
from pn.profile.bayes import af_posterior, beta_binomial, shift_prior
from pn.profile.priors import load_priors, position_mean, stat_prior
from pn.profile.stats import (
    AF_STATS,
    ALL_STATS,
    DEPTH_NAMES,
    DEPTH_STATS,
    HIST_NAMES,
    PIP_STATS,
    PREFLOP_STATS,
    SIZE_BUCKETS,
    RawStats,
    aggregate,
    walk_all,
)
from pn.profile.walk import (
    POSTFLOP,
    THREE_BET_SIZE_BUCKETS,
    THREE_BET_SIZE_TOP,
    HandFacts,
    three_bet_size_bucket,
)

SIZE_HIST_NAMES = tuple(name for _, name in THREE_BET_SIZE_BUCKETS) + (THREE_BET_SIZE_TOP,)


def _stat(stat: str, n: int, s: int, mean: float, k: float) -> dict:
    if stat in AF_STATS:
        return af_posterior(s, n, mean, k)
    return beta_binomial(s, n, mean, k)


def _af_p(af: float) -> float:
    return af / (1.0 + af)


MIN_SITU_COVERAGE = 0.5  # share of a cell's situations with a GTO Wizard spot


@lru_cache(maxsize=1)
def _gtow_table() -> dict:
    path = gtowizard.freqs_path()
    return gtowizard.spot_table(gtowizard.load_freqs(path)) if path.exists() else {}


def gto_for(raw: RawStats, stat: str, cell: str, bl: Mapping) -> float | None:
    """GTO baseline for a profile cell (`all`, a position, IP/OOP, or `<pos> vs <group>`).

    Preflop stats: mean GTO Wizard frequency over the player's own situations in that cell
    (situation-weighted, so short-handed / heads-up seats compare with the right spot) when at
    least MIN_SITU_COVERAGE of them map to a solved spot. Otherwise gto_baselines.json: the
    cell's value, and for postflop stats the default seed when the cell has none."""
    if stat in PREFLOP_STATS:
        total = covered = 0
        acc = 0.0
        table = _gtow_table()
        for (st, c, situ), n in raw.situ.items():
            if st != stat or c != cell:
                continue
            total += n
            v = gtowizard.situation_freq(table, stat, situ)
            if v is not None:
                covered += n
                acc += n * v
        if covered and covered / total >= MIN_SITU_COVERAGE:
            return acc / covered
    if " vs " in cell:
        return None
    if cell == "all":
        return gto_value(bl, stat)
    v = gto_value(bl, stat, cell)
    if v is None and stat not in PREFLOP_STATS:
        v = gto_value(bl, stat)  # postflop seeds are position-agnostic for now
    return v


def stats_block(raw: RawStats, priors: Mapping, bl: Mapping) -> dict[str, dict]:
    out = {}
    for stat in ALL_STATS:
        n, s = raw.count(stat)
        mean, k = stat_prior(priors, stat)
        out[stat] = annotate(_stat(stat, n, s, mean, k), gto_for(raw, stat, "all", bl))
    return out


def preflop_vs_gto_block(raw: RawStats, overall: Mapping, bl: Mapping) -> dict:
    """Headline preflop stats vs the situation-weighted GTO baseline, plus the table-wide
    (6-max default) baseline as a secondary reference."""
    out = {}
    for stat in ("vpip", "pfr", "rfi", "three_bet", "cold_call", "fold_to_three_bet"):
        s = overall[stat]
        wide = gto_value(bl, stat)
        out[stat] = {
            "player": s["post_mean"], "n": s["n"], "ci90": s["ci90"],
            "gto_weighted": None if s["gto"] is None else round(s["gto"], 4),
            "dev_gto_weighted": s["dev_gto"],
            "gto_table_wide": wide, "dev_gto_table_wide": ci_dev(s, wide),
        }  # fmt: skip
    return out


def _shifted(stat: str, base: float, player: Mapping, pop: float) -> float:
    """Shift base by the player's overall deviation (AF stats shifted on the p scale)."""
    if stat in AF_STATS:
        p = shift_prior(_af_p(base), _af_p(player["post_mean"]), _af_p(pop))
        return p / (1 - p)
    return shift_prior(base, player["post_mean"], pop)


def by_position_block(raw: RawStats, overall: Mapping, priors: Mapping, bl: Mapping) -> dict:
    out: dict[str, dict] = {}
    for pos in sorted(raw.by_position):
        out[pos] = {}
        for stat, (n, s) in sorted(raw.by_position[pos].items()):
            pop, k = stat_prior(priors, stat)
            base = position_mean(priors, pos, stat) or pop
            post = _stat(stat, n, s, _shifted(stat, base, overall[stat], pop), k)
            out[pos][stat] = annotate(post, gto_for(raw, stat, pos, bl))
    return out


def by_vs_opener_block(raw: RawStats, by_pos: Mapping, priors: Mapping, bl: Mapping) -> dict:
    """position -> opener group -> 3bet / call-open / fold-to-steal; prior = that position's
    posterior (hierarchy population -> player -> position -> vs opener)."""
    out: dict[str, dict] = {}
    for pos in sorted(raw.by_vs_opener):
        out[pos] = {}
        for group in sorted(raw.by_vs_opener[pos]):
            out[pos][group] = {}
            for stat, (n, s) in sorted(raw.by_vs_opener[pos][group].items()):
                _, k = stat_prior(priors, stat)
                prior = by_pos[pos][stat]["post_mean"]
                post = _stat(stat, n, s, prior, k)
                out[pos][group][stat] = annotate(post, gto_for(raw, stat, f"{pos} vs {group}", bl))
    return out


def bet_size_hist_by_position_block(raw: RawStats) -> dict:
    return {
        cell: {st: {b: d.get(st, {}).get(b, 0) for b in HIST_NAMES} for st in POSTFLOP}
        for cell, d in sorted(raw.bet_size_hist_by_pos.items())
    }


def by_pip_block(raw: RawStats, overall: Mapping, priors: Mapping, bl: Mapping) -> dict:
    out: dict[str, dict] = {}
    for group in ("hu", "multiway"):
        d = raw.by_pip.get(group, {})
        out[group] = {}
        for stat in PIP_STATS:
            if stat not in d:
                continue
            n, s = d[stat]
            _, k = stat_prior(priors, stat)
            post = _stat(stat, n, s, overall[stat]["post_mean"], k)
            out[group][stat] = annotate(post, gto_value(bl, stat, group))
    return out


def by_depth_block(raw: RawStats, overall: Mapping, priors: Mapping, bl: Mapping) -> dict:
    """Effective-stack buckets; prior = the player's overall posterior; GTO = 100bb default."""
    out: dict[str, dict] = {}
    for bucket in DEPTH_NAMES:
        d = raw.by_depth.get(bucket, {})
        if not d:
            continue
        out[bucket] = {}
        for stat in DEPTH_STATS:
            if stat not in d:
                continue
            n, s = d[stat]
            _, k = stat_prior(priors, stat)
            post = _stat(stat, n, s, overall[stat]["post_mean"], k)
            out[bucket][stat] = annotate(post, gto_value(bl, stat))
    return out


def preflop_size_hist_block(raw: RawStats) -> dict:
    """3-bet raise-to / open-size histograms: all, ip/oop vs the opener, callers 0 / 1+."""
    groups = ("all", "ip", "oop", "callers_0", "callers_1plus")
    hist = {g: dict.fromkeys(SIZE_HIST_NAMES, 0) for g in groups}
    total = {g: [0, 0.0] for g in groups}
    for mult, ip, callers in raw.three_bet_sizes:
        b = three_bet_size_bucket(mult)
        for g in ("all", "ip" if ip else "oop", "callers_0" if callers == 0 else "callers_1plus"):
            hist[g][b] += 1
            total[g][0] += 1
            total[g][1] += mult
    return {
        "three_bet": {
            g: {**hist[g], "n": total[g][0],
                "mean_multiple": round(total[g][1] / total[g][0], 3) if total[g][0] else None}
            for g in groups
        }
    }  # fmt: skip


def fold_vs_size_block(raw: RawStats, overall: Mapping, priors: Mapping, bl: Mapping) -> dict:
    fvs = priors["fold_vs_size"]
    k = float(fvs["k"])
    out: dict[str, dict] = {}
    for street, buckets in SIZE_BUCKETS.items():
        anchor = f"fold_to_cbet_{street}"
        pop_anchor, _ = stat_prior(priors, anchor)
        out[street] = {}
        for b in buckets:
            n, s = raw.fold_vs_size.get(street, {}).get(b, [0, 0])
            base = float(fvs[street][b])
            post = beta_binomial(s, n, _shifted(anchor, base, overall[anchor], pop_anchor), k)
            out[street][b] = annotate(post, gto_value(bl, "fold_vs_size", f"{street}_{b}"))
    return out


def bet_size_hist_block(raw: RawStats) -> dict:
    return {st: {b: raw.bet_size_hist.get(st, {}).get(b, 0) for b in HIST_NAMES} for st in POSTFLOP}


def _facing_rates(raw: RawStats, priors: Mapping) -> tuple[dict, dict]:
    """Player's overall postflop fold/call rate when facing aggression (pairwise priors)."""
    n = fold = call = 0
    for (street, situation, act), c in raw.actions.items():
        if street in POSTFLOP and situation in ("facing_bet", "facing_raise"):
            n += c
            fold += c * (act == "fold")
            call += c * (act == "call")
    pf = priors["actions"]["flop:facing_bet"]
    return (
        beta_binomial(fold, n, pf["fold"], pf["k"]),
        beta_binomial(call, n, pf["call"], pf["k"]),
    )


def pair_priors(raw: RawStats, overall: Mapping, priors: Mapping) -> dict[str, tuple[float, float]]:
    fold, call = _facing_rates(raw, priors)
    k_fold = float(priors["actions"]["flop:facing_bet"]["k"])
    return {
        "vpip_when_in": (overall["vpip"]["post_mean"], stat_prior(priors, "vpip")[1]),
        "three_bet_vs": (overall["three_bet"]["post_mean"], stat_prior(priors, "three_bet")[1]),
        "fold_to_their_bet": (fold["post_mean"], k_fold),
        "call_vs_their_bet": (call["post_mean"], k_fold),
    }


def _aliases(cid: str, alias_map: Mapping[str, str], raw_ids: Iterable[str]) -> list[str]:
    ids = {pid for pid, c in alias_map.items() if c == cid} | {cid}
    ids |= {pid for pid in raw_ids if alias_map.get(pid, pid) == cid}
    return sorted(ids)


def _annotate_vs(vs: Mapping[str, Mapping], bl: Mapping) -> dict:
    gto = {"vpip_when_in": gto_value(bl, "vpip"), "three_bet_vs": gto_value(bl, "three_bet")}
    return {
        other: {k: annotate(v, gto.get(k)) if isinstance(v, dict) else v for k, v in e.items()}
        for other, e in vs.items()
    }


def population_fn(priors: Mapping) -> leaks.PopFn:
    """Population baseline for a (stat, spot) pair, used in leak entries."""

    def pop(stat: str, spot: str) -> float | None:
        if stat == "fold_vs_size":
            street, _, bucket = spot.partition("_")
            v = priors["fold_vs_size"].get(street, {}).get(bucket)
            return None if v is None else float(v)
        if stat not in priors["stats"]:
            return None
        pos = spot.split(" vs ")[0]
        if pos not in ("all", "hu", "multiway", "IP", "OOP"):
            v = position_mean(priors, pos, stat)
            if v is not None:
                return v
        return float(priors["stats"][stat]["mean"])

    return pop


def build_from_walked(
    canonical_id: str,
    walked: list[HandFacts],
    *,
    alias_map: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
    priors: Mapping | None = None,
    raw_all: Mapping[str, RawStats] | None = None,
    baselines: Mapping | None = None,
) -> dict:
    """Profile from walked hands. Uses every hand the player played against anyone."""
    alias_map = alias_map or {}
    names = names or {}
    priors = priors or load_priors()
    bl = baselines or load_baselines()
    raw_all = raw_all if raw_all is not None else aggregate(walked, alias_map)
    raw = raw_all.get(canonical_id, RawStats())
    overall = stats_block(raw, priors, bl)
    by_pos = by_position_block(raw, overall, priors, bl)
    by_vs = by_vs_opener_block(raw, by_pos, priors, bl)
    by_pip = by_pip_block(raw, overall, priors, bl)
    fvs = fold_vs_size_block(raw, overall, priors, bl)
    vs = pairwise.pairwise(walked, canonical_id, pair_priors(raw, overall, priors), alias_map)
    vs = _annotate_vs(vs, bl)
    leak_list = leaks.build_leaks(by_pos, by_vs, fvs, population_fn(priors))
    seen_ids = {pid for hf in walked for pid in hf.players}
    return {
        "canonical_id": canonical_id,
        "display_name": names.get(canonical_id, canonical_id),
        "aliases": _aliases(canonical_id, alias_map, seen_ids),
        "hands": raw.hands,
        "showdowns_seen": raw.showdowns_seen,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "priors_source": priors.get("_meta", {}).get("source", "unknown"),
        "gto_source": bl.get("_meta", {}).get("source", "unknown"),
        "stats": overall,
        "preflop_vs_gto": preflop_vs_gto_block(raw, overall, bl),
        "by_position": by_pos,
        "by_position_vs_opener": by_vs,
        "by_players_in_pot": by_pip,
        "by_depth": by_depth_block(raw, overall, priors, bl),
        "fold_vs_size": fvs,
        "bet_size_hist": bet_size_hist_block(raw),
        "bet_size_hist_by_position": bet_size_hist_by_position_block(raw),
        "preflop_size_hist": preflop_size_hist_block(raw),
        "preflop_range_est": preflop_range.preflop_range_est(raw),
        "lines": lines.lines_summary(raw),
        "vs": vs,
        "archetype": archetype.classify(overall, raw.hands),
        "leaks": leak_list,
        "tips": tips.generate_tips(overall, fvs, vs, priors, names, leaks=leak_list),
    }


def build_profile(
    canonical_id: str,
    hands: Iterable[dict],
    *,
    alias_map: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
    priors: Mapping | None = None,
    baselines: Mapping | None = None,
) -> dict:
    """Profile for `canonical_id` from normalized hand dicts (contracts/profile.md)."""
    return build_from_walked(
        canonical_id,
        walk_all(hands),
        alias_map=alias_map,
        names=names,
        priors=priors,
        baselines=baselines,
    )
