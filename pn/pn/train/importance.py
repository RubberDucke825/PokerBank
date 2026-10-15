"""Spot importance (contracts/trainer.md "Plan 14 additions"; the "Spot importance - V3" sheet).

priority = freq x pot x (1 - score)^p, normalised so the top row is 1 (all 0 when every raw value is).
- score: trainer accuracy (best 1, mixed 0.5, blunder 0 per graded decision) shrunk toward the global
  score with prior weight PRIOR_N; the global score is itself shrunk toward PRIOR_SCORE (the sheet's
  default GTO Wizard score), so a spot never played is ranked by freq x pot alone.
- pot: "start" = the pot in bb when hero first decides there (preflop spots: before hero acts;
  postflop spots, formations, textures: at the start of the flop) replayed from the line with the
  trainer's sizes; "avg" = mean pot at hero's recorded decisions (train_decisions.pot_bb), falling
  back to the start pot without any.
- p: 1..5; formations and spots use p_form (default 3), textures p_tex (default 1).
Drill weights (spots, and the texture a postflop drill's flop is drawn from) are
normalised(priority + C_EXPLORE x sqrt(ln N / (n + 1))), so never-played rows still get dealt.
Formation label "<first to act preflop> vs <second> - <SRP|3BP|4BP|Limped>" (the sheet's names).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pn.adapter.state import order_key
from pn.solve.request import TABLE_SEATS
from pn.train import grading, spots
from pn.train import preflop as pre
from pn.train import textures as tx
from pn.train.engine import IllegalAction, State, apply, start
from pn.train.stats import decision_grade

if TYPE_CHECKING:
    from pn.train.leaks import Line

P_FORM, P_TEX, P_RANGE = 3, 1, (1, 5)
POT_MODES = ("start", "avg")
PRIOR_N = 5.0  # prior strength: observations' worth of the global rate / score
PRIOR_BLUNDER_RATE = 0.2  # the global blunder rate before any graded decision exists
PRIOR_SCORE = 0.7  # the global score before any graded decision (the sheet's default score)
C_EXPLORE = 0.05  # exploration bonus in priority units (the top row is 1)
PREFLOP_POT_BB = 1.5  # the blinds: start pot of a preflop spot whose line cannot be replayed
POT_PRIOR_BB = {"limped": 2.0, "srp": 5.5, "3bp": 22.0, "4bp+": 45.0}  # flop pots without a line
POT_TEXT = {"limped": "Limped", "srp": "SRP", "3bp": "3BP", "4bp+": "4BP"}
CALLED_POT = {"vs_open": "srp", "vs_3bet": "3bp", "cold": "3bp", "vs_4bet+": "4bp+"}
CODE_OF = {"raise": "R", "allin": "A", "call": "C", "check": "X", "fold": "F"}
SB_C, BB_C = 50, 100  # cents (pots come back in bb)
ACCURACY = {grading.BEST: 1.0, grading.MIXED: 0.5, grading.BLUNDER: 0.0}


def check_params(p_form: Any, p_tex: Any, pot_mode: Any) -> None:
    """ValueError unless p_form / p_tex are integers in P_RANGE and pot_mode is in POT_MODES."""
    lo, hi = P_RANGE
    for name, p in (("p_form", p_form), ("p_tex", p_tex)):
        if (
            isinstance(p, bool)
            or not isinstance(p, int | float)
            or p != int(p)
            or not lo <= p <= hi
        ):
            raise ValueError(f"{name} must be an integer in {lo}..{hi}")
    if pot_mode not in POT_MODES:
        raise ValueError(f"pot must be one of {POT_MODES}")


# ------------------------------------------------------------------ scores and priorities


def accuracy(row: Mapping[str, Any]) -> float | None:
    """1 / 0.5 / 0 for a best / mixed / blunder decision, None when it was not graded."""
    return ACCURACY.get(decision_grade(row))


def global_score(rows: Iterable[Mapping[str, Any]]) -> float:
    accs = [a for a in map(accuracy, rows) if a is not None]
    return (sum(accs) + PRIOR_N * PRIOR_SCORE) / (len(accs) + PRIOR_N)


def shrunk(total: float, n: int, prior: float) -> float:
    return (total + PRIOR_N * prior) / (n + PRIOR_N)


def prioritise(items: Sequence[Mapping[str, Any]], p: int) -> list[dict[str, Any]]:
    """Copies of `items` ({freq, pot_bb, score}) with `priority` (the top one = 1)."""
    raw = [it["freq"] * it["pot_bb"] * (1.0 - it["score"]) ** p for it in items]
    top = max(raw, default=0.0)
    return [
        {**it, "priority": round(r / top, 5) if top > 0 else 0.0}
        for it, r in zip(items, raw, strict=True)
    ]


def with_weights(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copies with `weight` = normalised(priority + exploration bonus) over `items` ({priority, n})."""
    big_n = max(sum(it["n"] for it in items), 2)
    raw = [it["priority"] + C_EXPLORE * math.sqrt(math.log(big_n) / (it["n"] + 1)) for it in items]
    tot = sum(raw) or 1.0
    return [{**it, "weight": round(r / tot, 5)} for it, r in zip(items, raw, strict=True)]


def _tally(rows: Iterable[Mapping[str, Any]], key) -> tuple[dict, dict, dict]:
    """Per key (key(row), None skips): accuracy sum, graded count, recorded pots (graded rows
    only: an ungraded decision may be multiway or off the solved tree)."""
    acc: dict[str, float] = {}
    n: dict[str, int] = {}
    pots: dict[str, list[float]] = {}
    for r in rows:
        k = key(r)
        if k is None:
            continue
        a = accuracy(r)
        if a is None:
            continue
        acc[k] = acc.get(k, 0.0) + a
        n[k] = n.get(k, 0) + 1
        if r.get("pot_bb") is not None:
            pots.setdefault(k, []).append(float(r["pot_bb"]))
    return acc, n, pots


def _pot(start_pot: float, recorded: list[float] | None, pot_mode: str) -> float:
    if pot_mode == "avg" and recorded:
        return round(sum(recorded) / len(recorded), 2)
    return round(start_pot, 2)


def _ranked(items: list[dict[str, Any]], p: int, name: str) -> list[dict[str, Any]]:
    # freq is rounded before the priority so the returned fields reproduce it
    out = with_weights(prioritise([{**r, "freq": round(r["freq"], 5)} for r in items], p))
    out.sort(key=lambda r: (-r["priority"], -r["weight"], -r["freq"], r[name]))
    return out


# ------------------------------------------------------------------ formations


def formation_label(pot: str, positions: Iterable[str]) -> str:
    a, b = sorted(positions, key=lambda x: order_key(x, "preflop"))
    return f"{a} vs {b} - {POT_TEXT[pot]}"


def pot_key(formation: str) -> str:
    """The pot key (pn.train.spots.POTS) of a formation label: "BTN vs BB - SRP" -> "srp"."""
    text = formation.rsplit(" - ", 1)[1]
    return next(k for k, v in POT_TEXT.items() if v == text)


def parse_formation(label: str) -> tuple[tuple[str, str], str]:
    """((first, second), pot key) of a formation label; ValueError when malformed."""
    positions, sep, text = label.partition(" - ")
    pair = tuple(positions.split(" vs "))
    if not sep or len(pair) != 2 or text not in POT_TEXT.values() or pair[0] == pair[1]:
        raise ValueError(f"not a formation label: {label!r}")
    return (pair[0], pair[1]), pot_key(label)


def spot_formation(spot: str) -> str | None:
    """The heads-up flop a preflop spot leads to when hero calls (None without a known
    opponent: rfi, vs_limp); postflop spots carry no positions (see `line_formation`)."""
    d = spots.parse(spot)
    if d["street"] != "preflop" or d["faced"] not in CALLED_POT or d["vs"] in ("-", d["position"]):
        return None
    return formation_label(CALLED_POT[d["faced"]], (d["position"], d["vs"]))


def line_formation(line: Line) -> str | None:
    """Formation of a line that ends in a heads-up flop (None otherwise)."""
    folded = {s.position for s in line.steps if s.kind == "fold"}
    live = [
        p for p in dict.fromkeys([*(s.position for s in line.steps), line.hero]) if p not in folded
    ]
    if len(live) != 2:
        return None
    raises = sum(1 for s in line.steps if s.kind in ("raise", "allin"))
    return formation_label(spots.pot_type(raises), live)


def state_formation(st: State) -> str | None:
    """Formation of a heads-up postflop engine state (None preflop or multiway)."""
    live = st.in_hand()
    if st.street == "preflop" or len(live) != 2:
        return None
    raises = sum(1 for a in st.history if a.street == "preflop" and a.kind == "raise")
    return formation_label(spots.pot_type(raises), (st.positions[i] for i in live))


def line_pot_bb(line: Line, table: int, depth: int, freqs: Mapping | None) -> float | None:
    """Pot in bb after the line's preflop steps (sized like trainer villains), None when the
    steps do not replay in the table's action order."""
    st = start(TABLE_SEATS[table], depth * BB_C, SB_C, BB_C)
    try:
        for step in line.steps:
            i = st.to_act
            if i is None or st.positions[i] != step.position:
                return None
            sit = pre.situation(st, i)
            found = pre.gtow_spot(sit.key, depth, freqs)
            kind, to = pre.code_action(
                st, i, CODE_OF[step.kind], pre.raise_to(st, i, sit, found[1] if found else None)
            )
            st = apply(st, kind, to)
    except (IllegalAction, KeyError, ValueError):
        return None
    return st.pot / BB_C


# ------------------------------------------------------------------ tables


def _blunder_fields(k: int, blunders: float, freq: float, mean_rate: float) -> dict[str, Any]:
    rate = (blunders + PRIOR_N * mean_rate) / (k + PRIOR_N)
    return {"blunders": int(blunders), "blunder_rate": round(rate, 4),
            "expected_blunders_per_100": round(100.0 * freq * rate, 4)}  # fmt: skip


def spot_rows(freq: Mapping[str, float], rows: Sequence[Mapping[str, Any]],
              meta: Mapping[str, tuple[float, str | None]], *, p: int = P_FORM,
              pot_mode: str = "start", min_freq: float = 0.0) -> list[dict[str, Any]]:  # fmt: skip
    """Spot rows over the spots with freq > min_freq, sorted by -priority. `meta[spot]` =
    (start pot in bb, formation label | None). The Plan 7 blunder fields stay."""
    g = global_score(rows)
    acc, n, pots = _tally(rows, lambda r: r.get("spot") or None)
    blunders: dict[str, float] = {}
    for r in rows:
        a = accuracy(r) if r.get("spot") else None
        if a is not None:
            blunders[r["spot"]] = blunders.get(r["spot"], 0.0) + (a == ACCURACY[grading.BLUNDER])
    all_n = sum(n.values())
    mean_rate = (sum(blunders.values()) + PRIOR_N * PRIOR_BLUNDER_RATE) / (all_n + PRIOR_N)
    items = []
    for s, f in freq.items():
        if f <= min_freq:
            continue
        k = n.get(s, 0)
        start_pot, form = meta.get(s, (PREFLOP_POT_BB, None))
        items.append({"spot": s, "label": spots.label(s), "freq": f, "n": k,
                      **_blunder_fields(k, blunders.get(s, 0.0), f, mean_rate),
                      "score": round(shrunk(acc.get(s, 0.0), k, g), 4),
                      "pot_bb": _pot(start_pot, pots.get(s), pot_mode), "formation": form})  # fmt: skip
    return _ranked(items, p, "spot")


def formation_rows(freq: Mapping[str, float], start_pots: Mapping[str, float],
                   rows: Sequence[Mapping[str, Any]], spot_table: Sequence[Mapping[str, Any]], *,
                   p: int = P_FORM, pot_mode: str = "start") -> list[dict[str, Any]]:  # fmt: skip
    """Formation rows (heads-up flops) from `freq` (label -> per hand), scored on the postflop
    decisions recorded with that formation; `spots` = the spot rows labelled with it."""
    g = global_score(rows)
    acc, n, pots = _tally(
        rows, lambda r: r.get("formation") if r.get("street") != "preflop" else None
    )
    members: dict[str, list[str]] = {}
    for r in spot_table:
        if r.get("formation"):
            members.setdefault(r["formation"], []).append(r["spot"])
    items = []
    for label, f in freq.items():
        if f <= 0:
            continue
        positions, pot = label.split(" - ")
        k = n.get(label, 0)
        items.append({"formation": label, "pot_type": pot, "positions": positions.split(" vs "),
                      "freq": f, "n": k, "score": round(shrunk(acc.get(label, 0.0), k, g), 4),
                      "pot_bb": _pot(start_pots.get(label, POT_PRIOR_BB[pot_key(label)]), pots.get(label), pot_mode),
                      "spots": members.get(label, [])})  # fmt: skip
    return [{k: v for k, v in r.items() if k != "weight"} for r in _ranked(items, p, "formation")]


def texture_rows(rows: Sequence[Mapping[str, Any]], g: float, *, start_pot: float, p: int = P_TEX,
                 pot_mode: str = "start") -> list[dict[str, Any]]:  # fmt: skip
    """The eight texture rows: freq = exact share of all flops, score from the postflop decisions
    recorded with a board (older rows have none), pot = `start_pot` or the recorded mean ("avg":
    a texture without one takes the mean over every recorded texture pot, so all rows share one
    scale; `start_pot` without any)."""

    def key(r: Mapping[str, Any]) -> str | None:
        if r.get("street") == "preflop" or not r.get("board"):
            return None
        try:
            return tx.texture(r["board"])
        except ValueError:
            return None

    acc, n, pots = _tally(rows, key)
    every = [x for ps in pots.values() for x in ps]
    fallback = sum(every) / len(every) if pot_mode == "avg" and every else start_pot
    items = [{"texture": t, "details": tx.DETAILS[t], "freq": tx.FLOP_SHARE[t], "n": n.get(t, 0),
              "score": round(shrunk(acc.get(t, 0.0), n.get(t, 0), g), 4),
              "pot_bb": _pot(fallback, pots.get(t), pot_mode)} for t in tx.TEXTURES]  # fmt: skip
    return _ranked(items, p, "texture")


def mean_flop_pot(freq: Mapping[str, float], start_pots: Mapping[str, float]) -> float:
    """Frequency-weighted start pot over the formations (the srp prior without any)."""
    tot = sum(f for k, f in freq.items() if k in start_pots)
    if tot <= 0:
        return POT_PRIOR_BB["srp"]
    return sum(f * start_pots[k] for k, f in freq.items() if k in start_pots) / tot
