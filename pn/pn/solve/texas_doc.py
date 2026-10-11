"""TexasSolver dump JSON -> contracts/strategy_output.md document (contracts/texas_backend.md).

Dump shape (console_solver `dump_result`): action nodes `{actions: ["CHECK", "BET 25.000000"],
player, strategy: {actions, strategy: {"AdAc": [p, ...]}}, childrens: {"<action>": node},
node_type: "action_node"}`, chance nodes `{deal_number, dealcards: {"2c": node}, node_type:
"chance_node"}` (no `dealcards` past `set_dump_rounds`); showdown and fold terminals are absent
from `childrens`. TexasSolver player 1 is OOP (acts first) = `players[0]`, player 0 is IP =
`players[1]`. BET / RAISE amounts are the chips the actor ADDS with that action (a raise
includes the call), in the cents passed to `set_pot` / `set_effective_stack`. Combo strings put
the higher card first (suit order c<d<h<s); they are canonicalized with `pn.ranges`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from pn import ranges

STREETS = ("flop", "turn", "river")
SIG_DIGITS = 5
ALLIN_EPS = 0.5  # amounts are whole cents; a bet within this of the stack behind is all-in
Row = list[float]


def tex_player(tp: int) -> int:
    """TexasSolver player index -> our player index (1 = OOP = players[0])."""
    return 1 - int(tp)


def sig(x: float) -> float:
    """Round to SIG_DIGITS significant digits; exact 0 stays 0."""
    if x == 0:
        return 0.0
    return round(x, SIG_DIGITS - 1 - math.floor(math.log10(abs(x))))


@lru_cache(maxsize=1)
def combos_with_card() -> tuple[tuple[int, ...], ...]:
    """For each card int 0..51 (pn.ranges order), the combo indices that hold it."""
    out: list[list[int]] = [[] for _ in range(52)]
    for i, combo in enumerate(ranges.all_combos()):
        out[ranges.card_to_int(combo[:2])].append(i)
        out[ranges.card_to_int(combo[2:])].append(i)
    return tuple(tuple(x) for x in out)


def block(row: Row, card: int) -> Row:
    out = list(row)
    for i in combos_with_card()[card]:
        out[i] = 0.0
    return out


@dataclass(frozen=True)
class Sizes:
    """Configured pot percentages: bets per street and the raise list (all streets)."""

    bets: Mapping[str, tuple[int, ...]]
    raises: tuple[int, ...]


@dataclass(frozen=True)
class State:
    street: str
    pot: int  # pot at the start of the street (cents)
    commits: tuple[int, int]  # chips put in on this street, our player order
    stacks: tuple[int, int]  # real stacks behind at the start of the street
    eff: int  # effective stack behind at the start of the street (what TexasSolver plays)
    board: frozenset[int]
    history: tuple[str, ...]
    reach: tuple[Row, Row]

    @property
    def pot_now(self) -> int:
        return self.pot + sum(self.commits)

    @property
    def stacks_now(self) -> list[int]:
        return [s - c for s, c in zip(self.stacks, self.commits, strict=True)]


def parse_action(text: str) -> tuple[str, float | None]:
    """"BET 25.000000" -> ("BET", 25.0); "CHECK" -> ("CHECK", None)."""
    kind, _, amount = text.partition(" ")
    return kind, (float(amount) if amount else None)


def token_for(kind: str, added: float | None, st: State, actor: int,
              sizes: Sizes) -> tuple[str, int | None, float | None]:  # fmt: skip
    """(token, amount = actor's street commitment after the action, raw pot pct)."""
    if kind in ("CHECK", "CALL", "FOLD"):
        return kind.lower(), None, None
    if kind not in ("BET", "RAISE") or added is None:
        raise ValueError(f"unknown TexasSolver action {kind!r}")
    me, top = st.commits[actor], max(st.commits)
    amount = me + round(added)
    if added >= st.eff - me - ALLIN_EPS:
        return "allin", amount, None
    pct = (added - (top - me)) / (st.pot + 2 * top) * 100
    cands = sizes.raises if kind == "RAISE" else sizes.bets.get(st.street, ())
    snapped = min(cands, key=lambda s: abs(s - pct)) if cands else round(pct)
    return f"{kind.lower()}{snapped}", amount, pct


def summary(tokens: Sequence[str], strategy: list[Row], reach: Row) -> dict[str, float]:
    total = sum(reach)
    freq = [0.0] * len(tokens)
    if total > 0:
        for w, row in zip(reach, strategy, strict=True):
            if w:
                for a, p in enumerate(row):
                    freq[a] += w * p
    return {t: sig(f / total) if total > 0 else 0.0 for t, f in zip(tokens, freq, strict=True)}


def dense_strategy(block_js: Mapping[str, Any] | None, n_actions: int) -> list[Row]:
    rows = [[0.0] * n_actions for _ in range(ranges.N_COMBOS)]
    for combo, probs in ((block_js or {}).get("strategy") or {}).items():
        if len(probs) != n_actions:
            raise ValueError(f"strategy row for {combo} has {len(probs)} entries, "
                             f"expected {n_actions}")  # fmt: skip
        rows[ranges.combo_str_index(ranges.canonical_combo(combo))] = [float(p) for p in probs]
    return rows


class Builder:
    """Walks the dump depth-first and assigns node ids n0, n1, ... in pre-order."""

    def __init__(self, sizes: Sizes, heroes: tuple[bool, bool], last_street: str) -> None:
        self.sizes = sizes
        self.heroes = heroes
        self.last_street = last_street
        self.nodes: dict[str, dict[str, Any]] = {}

    def _new_id(self) -> str:
        nid = f"n{len(self.nodes)}"
        self.nodes[nid] = {}
        return nid

    def _rounded(self, row: Row) -> Row:
        return [sig(x) for x in row]

    def action(self, js: Mapping[str, Any], st: State) -> str:
        nid = self._new_id()
        actor = tex_player(js["player"])
        acts = list(js["actions"])
        strat = dense_strategy(js.get("strategy"), len(acts))
        mine = st.reach[actor]
        tokens: list[str] = []
        out_actions = []
        for i, text in enumerate(acts):
            kind, added = parse_action(text)
            token, amount, pct = token_for(kind, added, st, actor, self.sizes)
            if token in tokens and pct is not None:  # two amounts snapped to one size
                token = f"{kind.lower()}{round(pct)}"
            tokens.append(token)
            reach = list(st.reach)
            reach[actor] = [w * row[i] for w, row in zip(mine, strat, strict=True)]
            commits = list(st.commits)
            if kind == "CALL":
                commits[actor] = max(commits)
            elif added is not None:
                commits[actor] += round(added)
            child_st = replace(st, commits=(commits[0], commits[1]),
                               history=(*st.history, token), reach=(reach[0], reach[1]))  # fmt: skip
            child_js = (js.get("childrens") or {}).get(text)
            child = self.after(kind, child_js, child_st)
            entry: dict[str, Any] = {"token": token, "child": child}
            if amount is not None:
                entry = {"token": token, "amount": amount, "child": child}
            out_actions.append(entry)
        node: dict[str, Any] = {
            "type": "action", "street": st.street, "player": actor, "pot": st.pot_now,
            "stacks": st.stacks_now, "history": list(st.history), "actions": out_actions,
            "strategy": [self._rounded(r) for r in strat], "reach": self._rounded(mine),
        }  # fmt: skip
        if self.heroes[actor]:
            node["reach_by_player"] = [self._rounded(r) for r in st.reach]
        node["summary"] = summary(tokens, strat, mine)
        self.nodes[nid] = node
        return nid

    def _terminal(self, kind: str, st: State, **extra: Any) -> str:
        nid = self._new_id()
        self.nodes[nid] = {"type": "terminal", "kind": kind, "pot": st.pot_now, **extra,
                           "payoffs_by_player": None}  # fmt: skip
        return nid

    def after(self, kind: str, js: Mapping[str, Any] | None, st: State) -> str:
        if js is None:  # TexasSolver omits fold and showdown terminals
            return self._terminal("fold" if kind == "FOLD" else "showdown", st)
        if js.get("node_type") == "action_node":
            return self.action(js, st)
        if js.get("node_type") != "chance_node":
            raise ValueError(f"unknown dump node type {js.get('node_type')!r}")
        return self.chance(js, st)

    def chance(self, js: Mapping[str, Any], st: State) -> str:
        pot, stacks = st.pot_now, st.stacks_now
        eff = st.eff - max(st.commits)
        if eff <= 0:
            return self._terminal("allin_runout", st)
        nxt = STREETS[STREETS.index(st.street) + 1]
        cards = {c: sub for c, sub in (js.get("dealcards") or {}).items()
                 if ranges.card_to_int(c) not in st.board}  # fmt: skip
        reach_rows = [self._rounded(r) for r in st.reach]
        if not cards or STREETS.index(nxt) > STREETS.index(self.last_street):
            return self._terminal("leaf", st, street=st.street, stacks=stacks,
                                  reach_by_player=reach_rows)  # fmt: skip
        nid = self._new_id()
        children: dict[str, str] = {}
        for card, sub in cards.items():
            ci = ranges.card_to_int(card)
            child_st = State(nxt, pot, (0, 0), (stacks[0], stacks[1]), eff, st.board | {ci},
                             st.history, (block(st.reach[0], ci), block(st.reach[1], ci)))  # fmt: skip
            children[card] = self.after("DEAL", sub, child_st)
        self.nodes[nid] = {"type": "chance", "street": nxt, "pot": pot, "stacks": stacks,
                           "reach_by_player": reach_rows, "children": children}  # fmt: skip
        return nid


def to_document(dump: Mapping[str, Any], spot: Mapping[str, Any], sizes: Sizes,
                root_reach: tuple[Row, Row], streets: int,
                meta: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
    """The strategy document for `spot` (2 players, players[0] = OOP) from a TexasSolver dump.
    `root_reach` are the per-combo range weights TexasSolver actually solved with."""
    game, players = spot["game"], spot["players"]
    start = STREETS.index(game["street"])
    last = STREETS[min(start + max(streets, 1) - 1, len(STREETS) - 1)]
    board = frozenset(ranges.card_to_int(c) for c in game["board"])
    stacks = (int(players[0]["stack"]), int(players[1]["stack"]))
    st = State(game["street"], int(game["pot"]), (0, 0), stacks, min(stacks), board, (),
               root_reach)  # fmt: skip
    b = Builder(sizes, (bool(players[0].get("hero")), bool(players[1].get("hero"))), last)
    if dump.get("node_type") != "action_node":
        raise ValueError("dump root is not an action node")
    root = b.action(dump, st)
    return {
        "meta": {**meta, "backend": "texas", "ev_available": False, "node_count": len(b.nodes),
                 "emitted_streets": STREETS.index(last) - start + 1, "mode": "full"},
        "players": [{"index": i, "name": p["name"], "position": p["position"],
                     "hero": bool(p.get("hero")), "locked": False}
                    for i, p in enumerate(players)],
        "combos": list(ranges.all_combos()),
        "root": root,
        "nodes": b.nodes,
    }  # fmt: skip
