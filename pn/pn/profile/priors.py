"""Population priors: load config/population_priors.json and rebuild it from the DB."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from pn.profile.bayes import fit_beta_binomial
from pn.profile.paths import config_dir
from pn.profile.stats import AF_STATS, ALL_STATS, RawStats

MIN_PLAYERS_EB = 8  # players needed before trusting an empirical-Bayes fit of k
MIN_POOLED_N = 30  # pooled opportunities needed before replacing a seed mean


def canonical_action(act: str, keys: tuple[str, ...] | set[str]) -> str:
    """Map an observed action onto a prior's action set: preflop BB check -> call (passive
    continue), free fold -> check, postflop `raise` with no bet in front -> bet."""
    if act == "check" and "check" not in keys:
        return "call"
    if act == "fold" and "fold" not in keys:
        return "check"
    if act == "raise" and "raise" not in keys:
        return "bet"
    return act


def priors_path() -> Path:
    return config_dir() / "population_priors.json"


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> dict:
    del mtime  # cache key only
    return json.loads(Path(path).read_text())


def load_priors(path: Path | None = None) -> dict:
    p = path or priors_path()
    return copy.deepcopy(_load(str(p), p.stat().st_mtime))


def stat_prior(priors: Mapping, stat: str) -> tuple[float, float]:
    """(mean, k) for a stat. AF means are on the AF scale."""
    s = priors["stats"][stat]
    return float(s["mean"]), float(s["k"])


def position_mean(priors: Mapping, position: str, stat: str) -> float | None:
    v = priors.get("by_position", {}).get(position, {}).get(stat)
    return None if v is None else float(v)


def _af_to_p(af: float) -> float:
    return af / (1.0 + af)


def _p_to_af(p: float) -> float:
    p = min(p, 0.999)
    return p / (1.0 - p)


def rebuild(raw_by_player: Mapping[str, RawStats], base: Mapping, min_hands: int = 100) -> dict:
    """Recompute population means (and k by empirical Bayes) from players with >= min_hands.

    - stat mean & k: beta-binomial MLE over players when >= MIN_PLAYERS_EB players have data,
      else pooled mean (if pooled n >= MIN_POOLED_N) with the seed k kept.
    - by_position, actions, fold_vs_size: pooled frequencies (seed kept where no data).
    """
    out = copy.deepcopy(dict(base))
    pool = {cid: r for cid, r in raw_by_player.items() if r.hands >= min_hands}
    report: dict[str, str] = {}
    for stat in ALL_STATS:
        pairs = [(r.count(stat)[1], r.count(stat)[0]) for r in pool.values() if r.count(stat)[0]]
        seed = out["stats"].setdefault(stat, {"mean": 0.5, "k": 10})
        total_n = sum(n for _, n in pairs)
        fit = fit_beta_binomial(pairs) if len(pairs) >= MIN_PLAYERS_EB else None
        if fit is not None:
            mean, k = fit
            seed["k"] = round(k, 2)
            report[stat] = f"eb({len(pairs)} players)"
        elif total_n >= MIN_POOLED_N:
            mean = sum(s for s, _ in pairs) / total_n
            report[stat] = f"pooled(n={total_n}), seed k"
        else:
            report[stat] = "seed"
            continue
        seed["mean"] = round(_p_to_af(mean) if stat in AF_STATS else mean, 4)
        seed.pop("sd", None)

    pos_counts: dict[str, dict[str, list[int]]] = {}
    act_counts: dict[str, dict[str, int]] = {}
    size_counts: dict[str, dict[str, list[int]]] = {}
    for r in pool.values():
        for pos, d in r.by_position.items():
            for stat, (n, s) in d.items():
                c = pos_counts.setdefault(pos, {}).setdefault(stat, [0, 0])
                c[0] += n
                c[1] += s
        for (street, situation, action), n in r.actions.items():
            act_counts.setdefault(f"{street}:{situation}", {}).setdefault(action, 0)
            act_counts[f"{street}:{situation}"][action] += n
        for street, d in r.fold_vs_size.items():
            for bucket, (n, s) in d.items():
                c = size_counts.setdefault(street, {}).setdefault(bucket, [0, 0])
                c[0] += n
                c[1] += s
    for pos, d in pos_counts.items():
        for stat, (n, s) in d.items():
            if n >= MIN_POOLED_N:
                out.setdefault("by_position", {}).setdefault(pos, {})[stat] = round(s / n, 4)
    for key, d in act_counts.items():
        if key not in out.get("actions", {}):
            continue
        entry = out["actions"][key]
        keys = tuple(a for a in entry if a != "k")
        mapped: dict[str, int] = dict.fromkeys(keys, 0)
        for action, n in d.items():  # same mapping as the locks so the shares sum to 1
            mapped[canonical_action(action, keys)] = (
                mapped.get(canonical_action(action, keys), 0) + n
            )
        tot = sum(mapped[a] for a in keys)
        if tot >= MIN_POOLED_N:
            for action in keys:
                entry[action] = round(mapped[action] / tot, 4)
    for street, d in size_counts.items():
        for bucket, (n, s) in d.items():
            if n >= MIN_POOLED_N:
                out["fold_vs_size"].setdefault(street, {})[bucket] = round(s / n, 4)
    out["_meta"] = {
        "source": "rebuilt",
        "description": "Rebuilt from DB by `pn priors rebuild`; entries without enough data keep "
        "seed values (see fit_report).",
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "min_hands": min_hands,
        "n_players": len(pool),
        "fit_report": report,
    }
    return out


def save_priors(priors: Mapping, path: Path | None = None) -> Path:
    p = path or priors_path()
    p.write_text(json.dumps(priors, indent=2) + "\n")
    return p
