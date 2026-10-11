"""RNG buckets for mixed spots (contracts/trainer.md "Plan 4 additions"): the strategy for hero's
hand laid over 1-100 in aggression order (fold < check < call < bet/raise by size < all-in),
integer widths by largest-remainder rounding, zero-width buckets omitted."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PURE_FREQ = 0.99  # one action at least this frequent: the spot is pure
SLOTS = 100
_RANK = {"fold": 0, "check": 1, "call": 2, "allin": 4}  # bets and raises rank 3, by amount


def _order(opt: Mapping[str, Any]) -> tuple[int, int]:
    rank = _RANK.get(opt["token"], 3)
    return rank, int(opt.get("amount") or 0) if rank == 3 else 0


def buckets(options: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """[{token, from, to}] (1-based inclusive) over 1-100; [] when frequencies are missing."""
    if not options or any(o.get("freq") is None for o in options):
        return []
    opts = sorted(options, key=_order)
    total = sum(float(o["freq"]) for o in opts)
    if total <= 0:
        return []
    exact = [float(o["freq"]) / total * SLOTS for o in opts]
    width = [int(x) for x in exact]
    # largest remainder; ties go to the less aggressive action (stable sort keeps order)
    for k in sorted(range(len(opts)), key=lambda k: width[k] - exact[k])[: SLOTS - sum(width)]:
        width[k] += 1
    out, lo = [], 1
    for o, w in zip(opts, width, strict=True):
        if w > 0:
            out.append({"token": o["token"], "from": lo, "to": lo + w - 1})
            lo += w
    return out


def dominant(options: Sequence[Mapping[str, Any]]) -> str | None:
    """The action played at least PURE_FREQ of the time, if any."""
    freqs = [(o["token"], float(o["freq"])) for o in options if o.get("freq") is not None]
    total = sum(f for _, f in freqs)
    if not freqs or total <= 0:
        return None
    token, f = max(freqs, key=lambda tf: tf[1])
    return token if f / total >= PURE_FREQ else None


def judge(options: Sequence[Mapping[str, Any]], chosen: str, rng: int | None) -> dict[str, Any]:
    """Feedback fields: rng, buckets, rng_action, matched_rng, pure. In a pure spot rng_action is
    the dominant action whatever the roll; without a roll or buckets rng_action/matched_rng are
    None."""
    bks = buckets(options)
    pure_token = dominant(options)
    action = None
    if rng is not None and bks:
        action = pure_token or next(b["token"] for b in bks if b["from"] <= rng <= b["to"])
    return {"rng": rng, "buckets": bks, "rng_action": action,
            "matched_rng": None if action is None else chosen == action,
            "pure": pure_token is not None}  # fmt: skip
