"""Preflop GTO baselines from scraped GTO Wizard frequencies (data/gtowizard/*_freqs.json).

Spot keys: `<depth>|rfi|<POS>`, `<depth>|vs_open|<OPENER>|<HERO>`,
`<depth>|vs_3bet|<OPENER>|<3BETTOR>` (actor = opener), `<depth>|vs_4bet|<OPENER>|<3BETTOR>`
(actor = 3-bettor), `<depth>|vs_5bet|<OPENER>|<3BETTOR>` (actor = opener facing the
5-bet), `<depth>|vs_6bet|<OPENER>|<3BETTOR>` (actor = 3-bettor facing the 6-bet),
`<depth>|cold|<OPENER>|<3BETTOR>|<HERO>`, `<depth>|vs_limp|SB|BB`.
Cells: hand class -> {R raise, A all-in, C call, F fold, X check: percent}; `out` marks hands
outside the actor's range, `?` is an unreadable cell (ignored).

Action frequencies are combo-weighted over the hands that REACH the node: RFI, vs-open, cold
and vs-limp spots are reached by every hand; vs-3bet by the opener's raise frequency (from the
RFI spot); vs-4bet by the 3-bettor's raise frequency (from the vs-open spot); vs-5bet / vs-6bet
by the product of the actor's raise frequencies along its earlier decisions (rfi x vs-3bet,
vs-open x vs-4bet).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from pn import ranges
from pn.profile.paths import repo_root

ACTION_CODES = ("R", "A", "C", "F", "X")
# GTO Wizard 6-max seat names -> our 6-max position labels (their UTG is our 6-max LJ).
GTOW_TO_OURS = {"UTG": "LJ", "HJ": "HJ", "CO": "CO", "BTN": "BTN", "SB": "SB", "BB": "BB"}


def freqs_path() -> Path:
    return repo_root() / "data" / "gtowizard" / "cash6m_cev_2.5x_freqs.json"


def load_freqs(path: Path | None = None) -> dict:
    return json.loads((path or freqs_path()).read_text())


def _cell_shares(cell: Mapping) -> dict[str, float]:
    known = {k: float(v) for k, v in cell.items() if k in ACTION_CODES}
    tot = sum(known.values())
    return {k: v / tot for k, v in known.items()} if tot > 0 else {}


def raise_share(cell: Mapping) -> float:
    s = _cell_shares(cell)
    return s.get("R", 0.0) + s.get("A", 0.0)


def spot_freqs(spot: Mapping, reach: Mapping[str, float] | None = None) -> dict[str, float]:
    """{raise, call, fold, check} frequencies (raise includes all-in) over reaching combos."""
    acc = {"raise": 0.0, "call": 0.0, "fold": 0.0, "check": 0.0}
    total = 0.0
    for cls, cell in spot["cells"].items():
        w = ranges.class_n_combos(cls) * (1.0 if reach is None else reach.get(cls, 0.0))
        shares = _cell_shares(cell)
        if w <= 0 or not shares:
            continue
        total += w
        acc["raise"] += w * (shares.get("R", 0.0) + shares.get("A", 0.0))
        acc["call"] += w * shares.get("C", 0.0)
        acc["fold"] += w * shares.get("F", 0.0)
        acc["check"] += w * shares.get("X", 0.0)
    return {k: v / total for k, v in acc.items()} if total else acc


def _reach(spot: Mapping) -> dict[str, float]:
    return {cls: raise_share(cell) for cls, cell in spot["cells"].items()}


# The actor's previous decision node for each facing-a-raise kind (same actor one raise earlier).
_PREV_NODE = {
    "vs_3bet": lambda p: f"rfi|{p[1]}",
    "vs_4bet": lambda p: f"vs_open|{p[1]}|{p[2]}",
    "vs_5bet": lambda p: f"vs_3bet|{p[1]}|{p[2]}",
    "vs_6bet": lambda p: f"vs_4bet|{p[1]}|{p[2]}",
}


def _reach_chain(spots: Mapping, key: str) -> dict[str, float] | None:
    """Reach weight per hand class: product of the actor's raise shares at its earlier nodes.
    None when the kind is reached by every hand; KeyError when an earlier node is missing."""
    parts = key.split("|")
    if parts[0] not in _PREV_NODE:
        return None
    prev = _PREV_NODE[parts[0]](parts)
    if prev not in spots:
        raise KeyError(prev)
    r = _reach(spots[prev])
    earlier = _reach_chain(spots, prev)
    if earlier is None:
        return r
    return {cls: v * earlier.get(cls, 0.0) for cls, v in r.items()}


def spot_table(freqs: Mapping, depth: int = 100) -> dict[str, dict[str, float]]:
    """Frequencies per spot key (without the depth prefix), reach-weighted as documented."""
    pre = f"{depth}|"
    spots = {k[len(pre) :]: v for k, v in freqs.items() if k.startswith(pre)}
    out: dict[str, dict[str, float]] = {}
    for key, spot in spots.items():
        try:
            reach = _reach_chain(spots, key)
        except KeyError:
            continue  # cannot condition on the actor's range: skip rather than guess
        out[key] = spot_freqs(spot, reach)
    return out


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def preflop_baselines(freqs: Mapping, depth: int = 100) -> dict[str, dict]:
    """Baselines for our preflop stats, by our position labels.

    rfi / steal(CO,BTN,SB)   RFI raise%                       by opener position
    limp                     RFI call% (SB completes)          by position
    three_bet / cold_call    vs-open raise% / call%           by hero, mean over openers
    fold_to_three_bet        vs-3bet fold%                    by opener, mean over 3-bettors
    four_bet                 raise% facing exactly two raises: vs-3bet (opener) and cold spots
    fold_to_four_bet         vs-4bet fold%                    by 3-bettor
    five_bet                 vs-4bet raise%                   by 3-bettor
    fold_to_five_bet/six_bet vs-5bet fold% / raise%           by opener
    fold_to_six_bet          vs-6bet fold%                    by 3-bettor
    fold_to_steal            vs-open fold% of SB/BB vs CO/BTN/SB opens
    Defaults are unweighted means over the spots (not frequency-of-occurrence weighted).
    """
    table = spot_table(freqs, depth)
    by: dict[str, dict[str, list[float]]] = {}

    def add(stat: str, gtow_pos: str, v: float) -> None:
        by.setdefault(stat, {}).setdefault(GTOW_TO_OURS[gtow_pos], []).append(v)

    for key, f in table.items():
        p = key.split("|")
        if p[0] == "rfi":
            add("rfi", p[1], f["raise"])
            add("limp", p[1], f["call"])  # only the SB completes
            if p[1] in ("CO", "BTN", "SB"):
                add("steal", p[1], f["raise"])
        elif p[0] == "vs_open":
            add("three_bet", p[2], f["raise"])
            add("cold_call", p[2], f["call"])
            if p[2] in ("SB", "BB") and p[1] in ("CO", "BTN", "SB"):
                add("fold_to_steal", p[2], f["fold"])
        elif p[0] == "vs_3bet":
            add("fold_to_three_bet", p[1], f["fold"])
            add("four_bet", p[1], f["raise"])
        elif p[0] == "vs_4bet":
            add("fold_to_four_bet", p[2], f["fold"])
            add("five_bet", p[2], f["raise"])
        elif p[0] == "vs_5bet":
            add("fold_to_five_bet", p[1], f["fold"])
            add("six_bet", p[1], f["raise"])
        elif p[0] == "vs_6bet":
            add("fold_to_six_bet", p[2], f["fold"])
        elif p[0] == "cold":
            add("four_bet", p[3], f["raise"])
    out: dict[str, dict] = {}
    for stat, positions in by.items():
        by_pos = {pos: _mean(vs) for pos, vs in positions.items()}
        out[stat] = {
            "default": _mean([v for vs in positions.values() for v in vs]),
            "by_position": by_pos,
        }
    return out


def merge_into(baselines: Mapping, preflop: Mapping[str, Mapping], source: str) -> dict:
    """New baselines dict: preflop stats' default and by_position values replaced (positions
    not covered, e.g. 8-max UTG/UTG1/UTG2, keep their previous values)."""
    out = json.loads(json.dumps(baselines))
    for stat, entry in preflop.items():
        cur = out["stats"].setdefault(stat, {})
        if entry["default"] is not None:
            cur["default"] = entry["default"]
        cur.setdefault("by_position", {}).update(
            {p: v for p, v in entry["by_position"].items() if v is not None}
        )
        cur["source"] = source
    meta = out.setdefault("_meta", {})
    meta["preflop_source"] = source
    meta["preflop_stats"] = sorted(preflop)
    return out


# ---------------------------------------------------------------- situation lookup

# Our seat keys -> GTO Wizard 6-max seats. Short-handed hands are the 6-max solution with the
# early seats folded (ingest names seats from the button backwards, so 4-handed = CO/BTN/SB/BB).
# Heads-up is the SB-vs-BB spot of the same solution (BTN posts the SB).
HERO_SEAT = {"LJ": "UTG", "HJ": "HJ", "CO": "CO", "BTN": "BTN", "SB": "SB", "BB": "BB",
             "BTN_HU": "SB", "BB_HU": "BB"}  # fmt: skip
OPENER_SEAT = {**HERO_SEAT, "UTG": "UTG", "UTG1": "UTG", "UTG2": "UTG", "UTG3": "UTG"}

_STAT_ACTION = {
    "rfi": {"vpip": ("raise", "call"), "pfr": ("raise",), "rfi": ("raise",), "limp": ("call",),
            "steal": ("raise",)},
    "vs_limp": {"vpip": ("raise",), "pfr": ("raise",)},
    "vs_open": {"vpip": ("raise", "call"), "pfr": ("raise",), "three_bet": ("raise",),
                "cold_call": ("call",), "fold_to_steal": ("fold",)},
    "cold": {"vpip": ("raise", "call"), "pfr": ("raise",), "four_bet": ("raise",)},
    "vs_3bet": {"fold_to_three_bet": ("fold",), "four_bet": ("raise",)},
    "vs_4bet": {"fold_to_four_bet": ("fold",), "five_bet": ("raise",)},
    "vs_5bet": {"fold_to_five_bet": ("fold",), "six_bet": ("raise",)},
    "vs_6bet": {"fold_to_six_bet": ("fold",)},
}  # fmt: skip


def situation_key(situ: tuple) -> str | None:
    """GTO Wizard spot key (without depth) for a spots.py situation tuple, or None."""
    kind, *seats = situ
    h, o = HERO_SEAT.get, OPENER_SEAT.get
    if kind == "rfi":
        return f"rfi|{h(seats[0])}" if h(seats[0]) else None
    if kind == "vs_limp":
        return "vs_limp|SB|BB"
    if kind == "vs_open":
        O, P = o(seats[0]), h(seats[1])
        return f"vs_open|{O}|{P}" if O and P else None
    if kind == "cold":
        O, T, P = o(seats[0]), h(seats[1]), h(seats[2])
        return f"cold|{O}|{T}|{P}" if O and T and P else None
    if kind in ("vs_3bet", "vs_5bet"):  # (kind, opener, 3-bettor), actor = opener
        P, T = o(seats[0]), h(seats[1])
        return f"{kind}|{P}|{T}" if P and T else None
    if kind in ("vs_4bet", "vs_6bet"):  # (kind, opener, 3-bettor), actor = 3-bettor
        O, P = o(seats[0]), h(seats[1])
        return f"{kind}|{O}|{P}" if O and P else None
    return None


def situation_freq(table: Mapping[str, Mapping], stat: str, situ: tuple) -> float | None:
    """GTO frequency of `stat` succeeding in this situation, or None if not covered."""
    actions = _STAT_ACTION.get(situ[0], {}).get(stat)
    key = situation_key(situ)
    if actions is None or key is None or key not in table:
        return None
    return sum(table[key][a] for a in actions)
