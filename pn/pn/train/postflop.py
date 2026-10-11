"""Postflop for the trainer: street spots from the actual line, a solve runner with a hard
timeout (a failure raises SolveError; no other backend is tried), walking the solved tree,
villain sampling and hero frequency grading (pn.train.grading; Plan 7: TexasSolver documents
carry no EVs). Without a usable tree, a fixed fallback policy plays on."""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from pn import ranges
from pn.adapter.state import Line, preflop_lines
from pn.solve import Budget, EvUnavailable, SpotRequest
from pn.solve import backend as be
from pn.solve.ev import combo_position
from pn.train import grading
from pn.train import suitmap as sm
from pn.train.engine import State, raise_cap

TRAINER_BUDGET = Budget.preset("trainer")
HARD_GRACE_SEC = 5  # kill a backend this long after its time limit
MIN_WEIGHT = 0.02  # every live player's actual combo stays in its range at least this much
FALLBACK_BETS = (33, 75)
FALLBACK_RAISE = 100
FALLBACK_CALL_POT = 0.5
OUTSIDE_RANGE = "combo_outside_range"  # fallback villain calls bets up to this share of the pot


# ------------------------------------------------------------------ runner


def runner(spot: Mapping[str, Any], backend: str | None = None, **hooks: Any) -> tuple[dict, str]:
    """SolveCache runner with a hard timeout: the backend is killed at time_limit + 5 s, so a
    trainer hand never hangs. A missing binary, a failure or a kill raises SolveError (the
    street then plays the fallback policy, ungraded); no other backend is tried. `hooks`
    (progress=, should_stop=) go to pn.solve.backend.run_backend."""
    limit = float(spot["game"].get("time_limit_sec", TRAINER_BUDGET.time_limit_sec))
    return be.run_backend(spot, backend, timeout=limit + HARD_GRACE_SEC, **hooks)


# ------------------------------------------------------------------ spots


@dataclass(frozen=True)
class Player:
    position: str
    player_id: str | None
    name: str
    hero: bool
    cards: tuple[str, str]

    @property
    def combo(self) -> str:
        return ranges.canonical_combo("".join(self.cards))


def lines(st: State) -> dict[str, Line]:
    """Preflop line per position (pn.adapter.state.preflop_lines over the engine history)."""
    hand = {"players": [{"player_id": p, "position": p} for p in st.positions],
            "actions": [{"seq": k, "street": a.street, "player_id": st.positions[a.seat],
                         "kind": a.kind, "amount": a.added, "allin": a.allin}
                        for k, a in enumerate(st.history) if a.street == "preflop"]}  # fmt: skip
    return preflop_lines(hand)


def ensure_combos(config: Mapping[str, Any], combos: Mapping[str, str]) -> dict[str, Any]:
    """Copy of `config` where each player's actual combo (by position) has range weight at
    least MIN_WEIGHT, so the solve has a strategy and action EVs for every real hand."""
    players = []
    for p in config["players"]:
        q = dict(p)
        combo = combos.get(p["position"])
        if combo:
            rng = ranges.parse(p["range"])
            rng[combo] = max(rng.get(combo, 0.0), MIN_WEIGHT)
            q["range"] = ranges.to_text(rng)
        players.append(q)
    return {**config, "players": players}


# ------------------------------------------------------------------ tree walking


@dataclass(frozen=True)
class Tree:
    doc: dict
    node: str
    req: SpotRequest  # what was solved, in actual cards (hint for the next street's subgame)
    perm: sm.Perm = sm.IDENTITY  # document suit -> actual suit (a library flop dealt permuted)

    @property
    def here(self) -> dict:
        return self.doc["nodes"][self.node]

    def doc_combo(self, combo: str) -> str:
        """An actual combo in the document's cards."""
        return sm.permute_combo(combo, sm.inverse(self.perm))

    def at(self, node: str) -> Tree:
        return replace(self, node=node)


def _kind(token: str) -> str:
    if token in ("fold", "check", "call", "allin"):
        return token
    return "raise"  # bet<pct> / raise<pct>


def tree_actions(tree: Tree, st: State) -> list[dict[str, Any]] | None:
    """The node's actions as [{token, kind, to, child}] when the node is the engine's decision
    (same actor); None when the tree and the hand disagree."""
    node = tree.here
    if node.get("type") != "action" or st.to_act is None:
        return None
    actor = tree.doc["players"][node["player"]]["position"]
    if actor != st.positions[st.to_act]:
        return None
    i = st.to_act
    out = []
    for a in node["actions"]:
        kind = _kind(a["token"])
        to = int(a["amount"]) if a.get("amount") is not None else (
            st.committed[i] + st.to_call(i) if kind == "call" else st.committed[i])  # fmt: skip
        out.append({"token": a["token"], "kind": kind, "to": to, "child": a["child"]})
    return out


def own_row(tree: Tree, combo: str) -> list[float] | None:
    """The combo's own strategy row at the node (None when the combo is not in the range)."""
    try:
        i = combo_position(tree.doc, tree.doc_combo(combo))
        row = [float(x) for x in tree.here["strategy"][i]]
    except (EvUnavailable, KeyError, IndexError, TypeError):
        return None
    return row if sum(row) > 0 else None


def strategy_row(tree: Tree, combo: str) -> list[float] | None:
    """The combo's row, else the node's range summary (villains outside the solved range)."""
    node = tree.here
    row = own_row(tree, combo)
    if row is not None:
        return row
    summary = node.get("summary") or {}
    row = [float(summary.get(a["token"], 0.0)) for a in node["actions"]]
    return row if sum(row) > 0 else None


def villain_token(tree: Tree, combo: str, rng: random.Random) -> str:
    tokens = [a["token"] for a in tree.here["actions"]]
    row = strategy_row(tree, combo)
    if row is None:
        return "check" if "check" in tokens else ("call" if "call" in tokens else tokens[0])
    return rng.choices(tokens, weights=row)[0]


def _ev_row(tree: Tree, combo: str, bb: int) -> list[float] | None:
    """Per-action EVs (bb) for the combo, only from documents with `meta.ev_available` true
    (TexasSolver documents have none); None otherwise."""
    if not (tree.doc.get("meta") or {}).get("ev_available"):
        return None
    try:
        i = combo_position(tree.doc, tree.doc_combo(combo))
        return [float(x) / bb for x in tree.here["action_ev"][i]]
    except (EvUnavailable, KeyError, IndexError, TypeError):
        return None


def hero_grade(tree: Tree, combo: str, chosen: str, bb: int) -> dict[str, Any]:
    """Frequency of every action for hero's exact combo and the grade of the chosen one
    (pn.train.grading); `loss_bb` only when the document carries EVs (`meta.ev_available`).
    A combo outside the solved range (library flops are not widened to the actual hands) has no
    own frequencies: ungraded with note "combo_outside_range", never graded against the range
    summary (whose frequencies `actions` still shows)."""
    node = tree.here
    tokens = [a["token"] for a in node["actions"]]
    own = own_row(tree, combo)
    row = own or strategy_row(tree, combo) or [0.0] * len(tokens)
    evs = _ev_row(tree, combo, bb) if own is not None else None
    loss = None
    if evs is not None and chosen in tokens:
        loss = round(max(evs) - evs[tokens.index(chosen)], 3)
    fields = grading.feedback_fields(dict(zip(tokens, own, strict=True)) if own else None, chosen)
    if fields["best"] is None and any(row):
        fields = {**fields, "best": tokens[max(range(len(tokens)), key=row.__getitem__)]}
    amounts = {a["token"]: a.get("amount") for a in node["actions"]}
    return {
        "street": node.get("street"), "chosen": chosen, "origin": "solve", **fields,
        "actions": [{"token": t, "to": amounts[t], "freq": round(f, 4),
                     "ev_bb": round(evs[k], 3) if evs is not None else None}
                    for k, (t, f) in enumerate(zip(tokens, row, strict=True))],
        "loss_bb": loss,
        "note": None if own is not None else OUTSIDE_RANGE,
    }  # fmt: skip


# ------------------------------------------------------------------ fallback (no tree)


def fallback_actions(st: State, i: int) -> list[dict[str, Any]]:
    """Tree-vocabulary options when no solve is usable: fold/check/call, bet 33/75% pot or
    raise 100%, all-in."""
    owe = st.to_call(i)
    out: list[dict[str, Any]] = []
    if owe > 0:
        out += [{"token": "fold", "kind": "fold", "to": st.committed[i]},
                {"token": "call", "kind": "call", "to": st.committed[i] + owe}]  # fmt: skip
    else:
        out.append({"token": "check", "kind": "check", "to": st.committed[i]})
    if not st.can_raise(i):
        return out
    sized = ([(f"bet{p}", st.committed[i] + st.pot * p // 100) for p in FALLBACK_BETS]
             if st.highest == 0 else
             [(f"raise{FALLBACK_RAISE}", st.highest + (st.pot + owe) * FALLBACK_RAISE // 100)])  # fmt: skip
    seen = set()
    for token, to in sized:
        kind, to = raise_cap(st, i, to)
        if kind == "raise" and to not in seen:
            seen.add(to)
            out.append({"token": token, "kind": "raise", "to": to})
    out.append({"token": "allin", "kind": "allin", "to": st.max_to(i)})
    return out


def fallback_villain(st: State, i: int) -> str:
    owe = st.to_call(i)
    if owe == 0:
        return "check"
    return "call" if owe <= FALLBACK_CALL_POT * st.pot else "fold"

