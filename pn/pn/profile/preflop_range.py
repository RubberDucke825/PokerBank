"""Preflop range-width inference from chart prior + action frequency + showdown hands.

Model (per position x action in {open, call, 3bet}):
  * Ordering: chart classes first (in hand-ranking order), then all other classes by ranking.
    R(w) = the first w*1326 combos of that ordering, so the chart core is always kept and the
    range narrows by dropping its weakest-ranked hands / widens by adding the next-best hands.
  * Prior: w ~ Beta(w0*K_CHART, (1-w0)*K_CHART), w0 = chart width.
  * Frequency evidence: the action frequency at that position *is* the range width
    (e.g. RFI% at BTN), so (successes s, opportunities n) contribute w^s (1-w)^(n-s).
  * Showdown evidence: each shown hand h played that way contributes
    P(h | w) = (1-ETA) * 1[h in R(w)] / (w*1326) + ETA / 1326   (ETA = mis-click/misread noise).
  * Posterior over a 400-point grid on (0, 1]; report mean and 90% interval; range_text = R(mean)
    with the boundary class fractionally weighted, plus every shown class forced in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
from scipy import stats as sps

from pn import ranges
from pn.profile import charts
from pn.profile.stats import RawStats

K_CHART = 20.0
ETA = 0.05
GRID = np.linspace(0.0025, 1.0, 400)
ACTION_STAT = {"open": "rfi", "call": "cold_call", "3bet": "three_bet"}
MIN_W0 = 0.005


def _ordering(chart: Mapping[str, float]) -> list[str]:
    in_chart = {ranges.combo_class(c) for c in chart}
    ranking = charts.hand_ranking()
    return [c for c in ranking if c in in_chart] + [c for c in ranking if c not in in_chart]


def _class_end_pct(order: list[str]) -> dict[str, tuple[float, float]]:
    """class -> (start, end) cumulative combo fraction in the ordering."""
    out: dict[str, tuple[float, float]] = {}
    acc = 0
    for cls in order:
        n = ranges.class_n_combos(cls)
        out[cls] = (acc / ranges.N_COMBOS, (acc + n) / ranges.N_COMBOS)
        acc += n
    return out


def width_posterior(
    w0: float, freq: tuple[int, int], shown_classes: Iterable[str], order: list[str]
) -> np.ndarray:
    """Normalized posterior weights on GRID."""
    w0 = min(0.95, max(MIN_W0, w0))
    logp = sps.beta.logpdf(GRID, w0 * K_CHART, (1 - w0) * K_CHART)
    s, n = freq
    if n > 0:
        logp += s * np.log(GRID) + (n - s) * np.log1p(-np.minimum(GRID, 1 - 1e-9))
    spans = _class_end_pct(order)
    for cls in shown_classes:
        start, end = spans[cls]
        # fraction of the class's combos inside R(w)
        frac = np.clip((GRID - start) / (end - start), 0.0, 1.0)
        like = (1 - ETA) * frac / GRID + ETA
        logp += np.log(like)
    logp -= logp.max()
    p = np.exp(logp)
    return p / p.sum()


def range_at_width(order: list[str], w: float, forced: Iterable[str] = ()) -> dict[str, float]:
    target = w * ranges.N_COMBOS
    out: dict[str, float] = {}
    acc = 0.0
    for cls in order:
        if acc >= target - 1e-9:
            break
        n = ranges.class_n_combos(cls)
        weight = min(1.0, (target - acc) / n)
        for combo in ranges.class_combos(cls):
            out[combo] = round(weight, 2)
        acc += n
    for cls in forced:
        for combo in ranges.class_combos(cls):
            out[combo] = 1.0
    return {c: wt for c, wt in out.items() if wt > 0}


def estimate(
    table: str, position: str, action: str, freq: tuple[int, int], shown: list[str]
) -> dict:
    chart = charts.prior_range(table, position, action)
    w0 = ranges.width(chart)
    order = _ordering(chart)
    post = width_posterior(w0, freq, shown, order)
    mean = float((GRID * post).sum())
    cdf = np.cumsum(post)
    lo = float(GRID[np.searchsorted(cdf, 0.05)])
    hi = float(GRID[min(len(GRID) - 1, np.searchsorted(cdf, 0.95))])
    rng = range_at_width(order, mean, shown)
    return {
        "width_pct": round(mean, 4),
        "ci90": [round(lo, 4), round(hi, 4)],
        "prior_width_pct": round(w0, 4),
        "freq": {"n": freq[1], "successes": freq[0]},
        "range_text": ranges.to_text(rng),
        "evidence_hands": len(shown),
    }


def _table_for(raw: RawStats) -> str:
    if not raw.table_sizes:
        return "6max"
    n = raw.table_sizes.most_common(1)[0][0]
    return charts.table_kind(n)


def preflop_range_est(raw: RawStats) -> dict[str, dict[str, dict]]:
    table = _table_for(raw)
    shown_by: dict[tuple[str, str], list[str]] = {}
    for sh in raw.shown:
        if sh.preflop_action and charts.table_kind(sh.n_players) == table:
            pos = charts.normalize_position(sh.position, table)
            cls = ranges.combo_class("".join(sh.cards))
            shown_by.setdefault((pos, sh.preflop_action), []).append(cls)
    stat_action = {stat: action for action, stat in ACTION_STAT.items()}
    freq_by = {
        (pos, stat_action[stat]): (c[1], c[0])
        for (tbl, pos, stat), c in raw.range_freq.items()
        if tbl == table
    }
    out: dict[str, dict[str, dict]] = {}
    for key in sorted(set(shown_by) | set(freq_by)):
        pos, action = key
        try:
            est = estimate(table, pos, action, freq_by.get(key, (0, 0)), shown_by.get(key, []))
        except charts.ChartError:
            continue
        out.setdefault(pos, {})[action] = est
    return out
