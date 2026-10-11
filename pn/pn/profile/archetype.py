"""Rule-based archetype scores on posterior means. Thresholds documented in ARCHETYPES.md."""

from __future__ import annotations

import math
from collections.abc import Mapping

MIN_HANDS = 30  # below this the label is always `unknown`
LABEL_THRESHOLD = 0.5  # best score must reach this, else `unknown`
CONFIDENCE_HANDS = 75.0  # data factor = hands / (hands + CONFIDENCE_HANDS)
LABELS = ("aggressive", "calling_station", "punching_bag", "nit", "tag", "lag")


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _up(value: float, center: float, scale: float) -> float:
    """~0.5 at center, -> 1 as value grows (scale = distance for ~0.73)."""
    return _sig((value - center) / scale)


def _down(value: float, center: float, scale: float) -> float:
    return _sig((center - value) / scale)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _gmean(xs: list[float]) -> float:
    return math.exp(sum(math.log(max(x, 1e-9)) for x in xs) / len(xs))


def _m(stats: Mapping[str, Mapping], name: str) -> float:
    return float(stats[name]["post_mean"])


def scores(stats: Mapping[str, Mapping]) -> dict[str, float]:
    vpip, pfr = _m(stats, "vpip"), _m(stats, "pfr")
    ratio = pfr / vpip if vpip > 0 else 0.0
    af = _mean([_m(stats, f"af_{s}") for s in ("flop", "turn", "river")])
    ftc = _m(stats, "fold_to_cbet_flop")
    wtsd = _m(stats, "wtsd")
    out = {
        "aggressive": _mean([
            _up(af, 2.6, 0.5),
            _up(_m(stats, "cbet_flop"), 0.72, 0.06),
            _up(_m(stats, "cbet_turn"), 0.62, 0.07),
            _up(_m(stats, "three_bet"), 0.09, 0.02),
        ]),
        "calling_station": _mean([
            _up(vpip, 0.35, 0.05),
            _down(ratio, 0.45, 0.08),
            _down(ftc, 0.38, 0.06),
            _up(wtsd, 0.34, 0.03),
        ]),
        "punching_bag": _mean([
            _up(ftc, 0.58, 0.06),
            _down(wtsd, 0.26, 0.03),
            _down(af, 1.4, 0.4),
        ]),
        "nit": _down(vpip, 0.17, 0.025),
        "tag": _gmean([_up(vpip, 0.17, 0.02), _down(vpip, 0.27, 0.02), _up(ratio, 0.70, 0.07)]),
        "lag": _gmean([_up(vpip, 0.28, 0.03), _up(ratio, 0.65, 0.07), _up(pfr, 0.21, 0.03)]),
    }  # fmt: skip
    return {k: round(v, 4) for k, v in out.items()}


def classify(stats: Mapping[str, Mapping], hands: int) -> dict:
    sc = scores(stats)
    best = max(sc, key=lambda k: sc[k])
    data_factor = hands / (hands + CONFIDENCE_HANDS)
    label = best if hands >= MIN_HANDS and sc[best] >= LABEL_THRESHOLD else "unknown"
    return {"label": label, "scores": sc, "confidence": round(sc[best] * data_factor, 4)}
