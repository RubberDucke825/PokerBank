"""Drill construction (contracts/trainer.md "Plan 6 additions"): hands sampled from the solver's
ranges along a line into a spot, villains forced along the line preflop and steered toward the
target situation postflop. Hero's decisions are always hero's own: a deviation on the line ends
the drill and the hand plays on naturally."""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pn import ranges
from pn.adapter.state import STREETS
from pn.train import spots
from pn.train.leaks import Line, token_kind


@dataclass(frozen=True)
class Drill:
    spot: str
    line: Line
    landed: bool = False
    texture: str | None = None  # postflop drills: the flop texture to deal (None: any solved board)
    formation: str | None = None  # a formation pin's drill: the formation it is dealt in

    @property
    def target(self) -> dict[str, Any]:
        return spots.parse(self.spot)

    def view(self) -> dict[str, str]:
        return {"spot": self.spot, "label": spots.label(self.spot)}


# ------------------------------------------------------------------ hands


def _cards(combo: str) -> tuple[str, str]:
    return combo[:2], combo[2:]


def _lib_weights(doc: Mapping, nid: str, row: str, k: int | None = None) -> dict[str, float]:
    """Combo weights at a library node: `reach` (or reach_by_player[k] at leaves), times the
    strategy share of action k when given."""
    node = doc["nodes"][nid]
    combos = doc["combos"]
    if row == "reach_by_player":
        base = node["reach_by_player"][k]
        return {c: float(w) for c, w in zip(combos, base, strict=True)}
    base = node.get("reach") or [1.0] * len(combos)
    strat = node.get("strategy")
    if k is None or not strat:
        return {c: float(w) for c, w in zip(combos, base, strict=True)}
    return {c: float(w) * float(s[k]) for c, w, s in zip(combos, base, strat, strict=True)}


def _gtow_weights(freqs: Mapping | None, depth: int, srcs: Sequence[tuple]) -> dict[str, float]:
    """Product over (key, codes) of the class's share of those codes (uniform without data;
    `freqs` None = the installed GTO Wizard frequencies, as pn.train.preflop.gtow_spot).
    A ("pc", (table, kind, position, other), codes) source (8-max UTG / UTG1 keys, Plan 16)
    reads the PokerCoaching chart's class shares."""
    from pn.train import preflop as pre

    w = {c: 1.0 for c in ranges.all_combos()}
    for kind, key, codes in srcs:
        if kind == "pc":
            pc = pre.pc_class_dists(*key)
            dist_of = (lambda cls, d=pc[0]: d.get(cls, {})) if pc else None
        else:
            found = pre.gtow_spot(key, depth, freqs)
            dist_of = (lambda cls, s=found[1]: pre.cell_dist(s, cls)) if found else None
        if dist_of is None:
            continue
        for c in w:
            dist = dist_of(ranges.combo_class(c))
            w[c] *= sum(dist.get(code, 0.0) for code in codes)
    return w


def _weights(line: Line, position: str, freqs: Mapping | None, depth: int) -> dict[str, float] | None:
    if position == line.hero:
        src = line.hero_src
        if src[0] == "lib_reach":
            return _lib_weights(line.doc, src[1], "reach")
        if src[0] == "lib_leaf":
            return _lib_weights(line.doc, src[1], "reach_by_player", src[2])
        own = [s.src for s in line.steps if s.position == position and s.src]
        return _gtow_weights(freqs, depth, own) if own else None
    own = [s.src for s in line.steps if s.position == position and s.src]
    lib = [s for s in own if s[0] == "lib"]
    if lib:  # the last node's reach already carries the earlier own actions
        return _lib_weights(line.doc, lib[-1][1], "reach", lib[-1][2])
    return _gtow_weights(freqs, depth, own) if own else None


def flop_ranges(doc: Mapping) -> dict[str, dict[str, float]]:
    """Each player's range in a flop document, {position: {combo: weight > 0}}: the reach at the
    player's first decision (the root, then the node after the root's first action)."""
    out: dict[str, dict[str, float]] = {}
    nid = doc["root"]
    while True:
        node = doc["nodes"][nid]
        if node.get("type") != "action":
            return out
        pos = doc["players"][node["player"]]["position"]
        if pos in out:
            return out
        out[pos] = {c: float(w) for c, w in zip(doc["combos"], node["reach"], strict=True) if w > 0}
        nid = node["actions"][0]["child"]


def sample_hands(line: Line, rng: random.Random, freqs: Mapping | None = None,
                 depth: int = 100, ranges: Mapping[str, Mapping[str, float]] | None = None,
                 dead: Iterable[str] = ()) -> dict[str, tuple[str, str]]:  # fmt: skip
    """Hero's and the line's villains' hole cards, sampled without card clashes and without
    the `dead` cards. For positions in `ranges` (postflop drills: the library flop document's
    ranges) the line's weights are multiplied by that range, or replaced by it when the two do
    not overlap, so hero's combo can be graded on the flop tree."""
    order = [line.hero] + list(dict.fromkeys(s.position for s in line.steps if s.position != line.hero))
    used: set[str] = set(dead)
    out: dict[str, tuple[str, str]] = {}
    for pos in order:
        w = _weights(line, pos, freqs, depth)
        if ranges and pos in ranges:
            both = {c: x * w.get(c, 0.0) for c, x in ranges[pos].items()} if w else {}
            w = both if any(x > 0 for x in both.values()) else dict(ranges[pos])
        if w is None:
            continue
        cand = [(c, x) for c, x in w.items() if x > 0 and not used & set(_cards(c))]
        if not cand:
            continue
        combo = rng.choices([c for c, _ in cand], weights=[x for _, x in cand])[0]
        out[pos] = _cards(combo)
        used |= set(out[pos])
    return out


# ------------------------------------------------------------------ the line


def _norm(kind: str, owe: int) -> str:
    if kind in ("raise", "allin"):
        return "aggressive"
    return "fold" if kind == "fold" and owe > 0 else "passive"


def preflop_step(drill: Drill, st: Any, i: int) -> str | None:
    """The line's kind for the actor at seat i, None when the line is done. Raises
    LookupError when the hand left the line (another actor is due)."""
    k = sum(1 for a in st.history if a.street == "preflop")
    steps = drill.line.steps
    if k >= len(steps):
        return None
    if steps[k].position != st.positions[i]:
        raise LookupError(f"line expects {steps[k].position}, {st.positions[i]} is due")
    return steps[k].kind


def on_line(drill: Drill, st: Any, i: int, token: str) -> bool:
    """Hero's preflop action keeps the drill on its line."""
    try:
        kind = preflop_step(drill, st, i)
    except LookupError:
        return False
    return kind is None or _norm(token_kind(token), st.to_call(i)) == _norm(kind, st.to_call(i))


def postflop_token(drill: Drill, st: Any, i: int, options: Sequence[Mapping[str, Any]],
                   summary: Mapping[str, float] | None = None) -> str | None:  # fmt: skip
    """Villain token steering toward the target situation: passive before the target street;
    on it, check for `first`, bet then call for `facing_bet`, raise hero's bet for
    `facing_raise`. None when the target is preflop or already behind."""
    tgt = drill.target
    if tgt["street"] == "preflop":
        return None
    cur, goal = STREETS.index(st.street), STREETS.index(tgt["street"])
    if cur > goal:
        return None
    toks = [o["token"] for o in options]
    passive = "check" if "check" in toks else ("call" if "call" in toks else None)
    sized = [t for t in toks if token_kind(t) == "raise"] or [t for t in toks if t == "allin"]
    biggest = max(sized, key=lambda t: (summary or {}).get(t, 0.0)) if sized else None
    want = tgt["situation"]
    if cur < goal or want == "first":
        return passive or toks[0]
    last = next((a for a in reversed(st.history) if a.street == st.street and a.kind == "raise"),
                None)  # fmt: skip
    if want == "facing_bet":
        return (biggest or passive or toks[0]) if st.bets == 0 else (passive or toks[0])
    hero_bet = last is not None and st.bets == 1 and drill.line.hero == st.positions[last.seat]
    if hero_bet and biggest:
        return biggest
    return passive or toks[0]


def missed(drill: Drill, street: str) -> bool:
    """The hand moved past the target street without hero landing in the spot."""
    return not drill.landed and STREETS.index(street) > STREETS.index(drill.target["street"])
