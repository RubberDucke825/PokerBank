"""Leak-weighted spot priorities (contracts/trainer.md "Plan 6 additions").

freq(spot) = expected occurrences per hand of hero deciding in the spot, hero's seat uniform:
- preflop: reach of every hero node in the library root of the table/depth (path probability =
  product of the actors' reach-weighted action frequencies, `summary`), or GTO Wizard frequency
  chains (6-max) when there is no root;
- postflop: P(hero sees the flop heads-up in <pot> <ip|oop>) from the root's preflop leaves,
  else from the GTO Wizard chains (heads-up flop lines, 2026-10-07), else trainer observations,
  x P(spot | that flop) from solved heads-up street documents in the SolveCache (both seats;
  flop docs grouped by pot and side, turn / river docs by side only), then trainer observations
  of natural hands, then FIRST_FALLBACK for flop `first`. Only pots that reach the flop heads-up count
  (2026-10-07: 2-player only): multiway preflop leaves, 3+ player street solves and trainer
  hands with a "multiway_unsolvable" decision contribute nothing, so drills target HU flops.
Mistakes come from train_decisions in the window (Plan 7, no EVs): the blunder rate per occurrence
(pn.train.grading `blunder`, preflop and postflop alike), shrunk toward the global rate with
prior weight PRIOR_N. The global rate is itself shrunk toward PRIOR_BLUNDER_RATE with the same
weight (so one clean decision cannot price every spot at 0), and a spot never reached still has
expected blunders = 100 x freq x global rate and is drilled in proportion to its frequency.
weight = normalized(expected_blunders_per_100 + C_EXPLORE * sqrt(ln N / (n + 1))).
The solver part is cached in memory per (table, depth); `Line`s say how to deal a drill.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pn import ranges
from pn.adapter.state import STREETS
from pn.profile import gtowizard
from pn.solve.backend import MULTIWAY_UNSOLVABLE
from pn.solve.lazydoc import LazyDoc
from pn.solve.library import library_path
from pn.solve.request import TABLE_SEATS
from pn.train import grading, spots
from pn.train import preflop as pre
from pn.train.stats import decision_grade, ensure

PRIOR_N = 5.0  # prior strength: observations' worth of the global blunder rate
PRIOR_BLUNDER_RATE = 0.2  # the global blunder rate before any graded decision exists
C_EXPLORE = 0.5  # exploration bonus, in expected blunders per 100 hands
MIN_FREQ = 0.0  # spots at or below this frequency per hand are not listed or drilled
MIN_REACH = 1e-6  # prune tree walks below this path probability
FIRST_FALLBACK = 1.0  # P(flop first | heads-up flop) without a solved flop or an observation
POSTFLOP = STREETS[1:]


# ------------------------------------------------------------------ drill lines


@dataclass(frozen=True)
class Step:
    """One forced (villain) or required (hero) preflop action on the way into a spot. `src`
    says how to sample the actor's hand: ("lib", node_id, action_index) | ("gtow", key, codes)
    | ()."""

    position: str
    kind: str  # fold | check | call | raise | allin
    src: tuple = ()


@dataclass(frozen=True)
class Line:
    """How to deal a drill: hero's seat, the preflop actions into the spot, and where hero's
    hand comes from: ("lib_reach", node) | ("lib_leaf", node, player_index) | ("gtow", ()) ."""

    hero: str
    steps: tuple[Step, ...]
    hero_src: tuple
    doc: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class FreqMap:
    freq: dict[str, float]
    lines: dict[str, Line]  # preflop spot -> line; "post|<pot>|<ip|oop>" -> line to that flop
    source: str  # library | gtow | none
    matchups: dict[str, float] = field(default_factory=dict)  # spots.line_key -> per hand
    # "post|<pot>|<ip|oop>" -> every heads-up flop line into it, highest reach first (gtow)
    post_lines: dict[str, tuple[Line, ...]] = field(default_factory=dict)


def token_kind(token: str) -> str:
    if token in ("fold", "check", "call", "allin"):
        return token
    return "raise"  # raise<amount> / bet<pct> / raise<pct>


# ------------------------------------------------------------------ node access

_TYPE = re.compile(rb'"type":\s*"(\w+)"')
_KIND = re.compile(rb'"kind":\s*"(\w+)"')
_PLAYER = re.compile(rb'"player":\s*(\d+)')
_ACTIONS = re.compile(rb'"actions":\s*(\[[^\[\]]*\])')
_SUMMARY = re.compile(rb'"summary":\s*(\{[^{}]*\})')
_CHILDREN = re.compile(rb'"children":\s*(\{[^{}]*\})')


def _brief(doc: Mapping, nid: str) -> Mapping:
    """type / kind / player / actions / summary / children of a node. Library roots are read
    partially (the arrays in between are skipped); anything unexpected loads the whole node."""
    if not isinstance(doc, LazyDoc):
        return doc["nodes"][nid]
    off, length = doc._nodes._spans[nid]
    with doc.path.open("rb") as f:
        f.seek(off)
        head = f.read(min(length, 8192))
        f.seek(off + max(0, length - 4096))
        tail = f.read(min(length, 4096))
    t = _TYPE.search(head)
    if t is None:
        return doc.node(nid)
    out: dict[str, Any] = {"type": t.group(1).decode()}
    if out["type"] == "terminal":
        k = _KIND.search(head)
        out["kind"] = k.group(1).decode() if k else None
        return out
    if out["type"] == "chance":
        c = _CHILDREN.search(head)
        return {**out, "children": json.loads(c.group(1))} if c else doc.node(nid)
    p, a, s = _PLAYER.search(head), _ACTIONS.search(head), _SUMMARY.search(tail)
    if p is None or a is None or s is None:
        return doc.node(nid)
    return {**out, "player": int(p.group(1)), "actions": json.loads(a.group(1)),
            "summary": json.loads(s.group(1))}  # fmt: skip


def _action_freqs(node: Mapping) -> list[float]:
    acts = node["actions"]
    summ = node.get("summary") or {}
    fr = [max(0.0, float(summ.get(a["token"], 0.0))) for a in acts]
    tot = sum(fr)
    return [f / tot for f in fr] if tot > 0 else [1.0 / len(acts)] * len(acts)


# ------------------------------------------------------------------ preflop: library root


def library_walk(doc: Mapping, min_reach: float = MIN_REACH) -> FreqMap:
    """Spot frequencies and best lines from a preflop root (every seat; hero seat uniform)."""
    positions = [p["position"] for p in doc["players"]]
    n = len(positions)
    freq: dict[str, float] = {}
    pairs: dict[str, float] = {}
    best: dict[str, tuple[float, Line]] = {}

    def keep(key: str, reach: float, line: Line) -> None:
        if reach > best.get(key, (0.0, None))[0]:
            best[key] = (reach, line)

    # (node id, reach, steps so far, raisers, limpers, folded)
    stack: list[tuple[str, float, tuple[Step, ...], tuple[str, ...], int, frozenset]] = [
        (doc["root"], 1.0, (), (), 0, frozenset())]
    while stack:
        nid, reach, steps, raisers, limpers, folded = stack.pop()
        node = _brief(doc, nid)
        if node["type"] == "terminal":
            live = [p for p in positions if p not in folded]
            if node.get("kind") == "leaf" and len(live) == 2:  # heads-up flops only
                mk = spots.line_key((x.position, x.kind) for x in steps)
                pairs[mk] = pairs.get(mk, 0.0) + reach
                for p in live:
                    pk = f"post|{spots.pot_type(len(raisers))}|{'ip' if spots.is_ip(p, live) else 'oop'}"
                    freq[pk] = freq.get(pk, 0.0) + reach / n
                    keep(pk, reach, Line(p, steps, ("lib_leaf", nid, positions.index(p)), doc))
            continue
        if node["type"] != "action":
            continue
        me = positions[node["player"]]
        key = spots.spot_key({"street": "preflop", "position": me,
                              **spots.preflop_fields(me, raisers, limpers)})  # fmt: skip
        freq[key] = freq.get(key, 0.0) + reach / n
        keep(key, reach, Line(me, steps, ("lib_reach", nid), doc))
        for k, (a, f) in enumerate(zip(node["actions"], _action_freqs(node), strict=True)):
            r = reach * f
            if r < min_reach:
                continue
            kind = token_kind(a["token"])
            stack.append((a["child"], r, (*steps, Step(me, kind, ("lib", nid, k))),
                          (*raisers, me) if kind in ("raise", "allin") else raisers,
                          limpers + (kind == "call" and not raisers),
                          folded | {me} if kind == "fold" else folded))  # fmt: skip
    return FreqMap(freq, {k: line for k, (_, line) in best.items()}, "library", pairs)


# ------------------------------------------------------------------ preflop: GTO Wizard chains

_OURS = gtowizard.GTOW_TO_OURS
_THEIRS = {v: k for k, v in _OURS.items()}


def _shares(cells: Mapping[str, Mapping[str, float]],
            reach: Mapping[str, float] | None = None) -> dict[str, float]:  # fmt: skip
    """{raise, call, fold, check} shares over the reaching combos (as gtowizard.spot_freqs)."""
    acc = dict.fromkeys(("raise", "call", "fold", "check"), 0.0)
    total = 0.0
    for cls, d in cells.items():
        w = ranges.class_n_combos(cls) * (1.0 if reach is None else reach.get(cls, 0.0))
        if w <= 0:
            continue
        total += w
        acc["raise"] += w * (d.get("R", 0.0) + d.get("A", 0.0))
        acc["call"] += w * d.get("C", 0.0)
        acc["fold"] += w * d.get("F", 0.0)
        acc["check"] += w * d.get("X", 0.0)
    return {k: v / total for k, v in acc.items()} if total else acc


def pc_spot_table() -> dict[str, dict[str, float]]:
    """GTO Wizard 6-max spot keys (without depth) that the PokerCoaching charts cover: rfi
    (SB call = limp), vs_open and vs_3bet (over the opener's PokerCoaching open range). Pure
    charts: each class plays the charted action at frequency 1."""
    order = list(TABLE_SEATS[6])
    out: dict[str, dict[str, float]] = {}
    opens: dict[str, dict[str, float]] = {}
    for o in order[:-1]:
        found = pre.pc_class_dists("6max", "rfi", o, None)
        if found is not None:
            out[f"rfi|{_THEIRS[o]}"] = _shares(found[0])
            opens[o] = {cls: d.get("R", 0.0) for cls, d in found[0].items()}
    for oi, o in enumerate(order[:-1]):
        for h in order[oi + 1 :]:
            found = pre.pc_class_dists("6max", "vs_open", h, o)
            if found is not None:
                out[f"vs_open|{_THEIRS[o]}|{_THEIRS[h]}"] = _shares(found[0])
            found = pre.pc_class_dists("6max", "vs_3bet", o, h)
            if found is not None and o in opens:
                out[f"vs_3bet|{_THEIRS[o]}|{_THEIRS[h]}"] = _shares(found[0], opens[o])
    return out


def gtow_walk(freqs: Mapping, table: int, depth: int, *, pokercoaching: bool = True) -> FreqMap:
    """Frequency chains over the GTO Wizard 6-max tree (hero seat uniform): rfi, vs_limp (SB-BB),
    vs_open, cold, vs_3bet .. vs_6bet, players between the actors fold; and heads-up flops
    (2026-10-07): SB limp + BB check, open + call, 3-bet + call, 3-bet cold-called + opener
    folds, 4-bet + call. Each key is read at the nearest depth that has it. The data has no
    open + call (squeeze) or 3-bet + cold-call nodes: players behind a caller fold with their
    vs_open / cold fold share, the opener facing 3-bet + cold call with its vs_3bet fold share.
    Limp-isolation and 5-bet+ pots (all-in at these depths) are not counted. Empty for other
    tables. Action shares are PokerCoaching first (`pc_spot_table`, as the solver's seat
    ranges), GTO Wizard where PokerCoaching has no chart (vs_limp, cold, vs_4bet and later);
    `pokercoaching=False` reads GTO Wizard only."""
    if table != 6:
        return FreqMap({}, {}, "none")
    depths = sorted({int(k.split("|")[0]) for k in freqs}, key=lambda d: (abs(d - depth), d))
    if not depths:
        return FreqMap({}, {}, "none")
    tab: dict[str, dict[str, float]] = {}
    for d in reversed(depths):  # the nearest depth last: it wins per key
        tab.update(gtowizard.spot_table(freqs, d))
    if pokercoaching:
        tab.update(pc_spot_table())
    order = list(TABLE_SEATS[6])
    freq: dict[str, float] = {}
    lines: dict[str, Line] = {}
    best: dict[str, float] = {}
    flops: dict[str, list[tuple[float, Line]]] = {}

    def share(key: str, action: str) -> float:
        return tab.get(key, {}).get(action, 0.0)

    def step(pos: str, kind: str, key: str, code: str) -> Step:
        return Step(pos, kind, ("gtow", key, ("R", "A") if code == "R" else (code,)) if key in tab else ())

    def folds(prefix: str, between: list[str]) -> tuple[float, list[Step]]:
        """Everyone in `between` folds at `<prefix><their seat>`."""
        p, steps = 1.0, []
        for x in between:
            k = prefix + _THEIRS[x]
            p *= share(k, "fold") if k in tab else 1.0
            steps.append(step(x, "fold", k, "F"))
        return p, steps

    def add(key: str, me: str, reach: float, steps: list[Step]) -> None:
        if reach <= 0:
            return
        freq[key] = freq.get(key, 0.0) + reach / 6
        if reach > best.get(key, 0.0):
            best[key] = reach
            lines[key] = Line(me, tuple(steps), ("gtow", ()))

    def flop(pot: str, reach: float, steps: list[Step]) -> None:
        """A heads-up flop between the two players of `steps` who never fold."""
        if reach <= 0:
            return
        folded = {s.position for s in steps if s.kind == "fold"}
        live = [p for p in dict.fromkeys(s.position for s in steps) if p not in folded]
        for p in live:
            pk = f"post|{pot}|{'ip' if spots.is_ip(p, live) else 'oop'}"
            freq[pk] = freq.get(pk, 0.0) + reach / 6
            flops.setdefault(pk, []).append((reach, Line(p, tuple(steps), ("gtow", ()))))

    def spot(me: str, raisers: list[str], limpers: int = 0) -> str:
        return spots.spot_key({"street": "preflop", "position": me,
                               **spots.preflop_fields(me, raisers, limpers)})  # fmt: skip

    for i, me in enumerate(order[:-1]):  # rfi (BB has no rfi decision)
        p, steps = folds("rfi|", order[:i])
        add(spot(me, []), me, p, steps)
    p, steps = folds("rfi|", order[:-2])  # BB vs SB limp
    limp = [*steps, step("SB", "call", "rfi|SB", "C")]
    add(spot("BB", [], 1), "BB", p * share("rfi|SB", "call"), limp)
    flop("limped", p * share("rfi|SB", "call") * share("vs_limp|SB|BB", "check"),
         [*limp, step("BB", "check", "vs_limp|SB|BB", "X")])  # fmt: skip
    for oi, o in enumerate(order[:-1]):
        O = _THEIRS[o]
        p_o, st_o = folds("rfi|", order[:oi])
        st_o = [*st_o, step(o, "raise", f"rfi|{O}", "R")]
        p_open = p_o * share(f"rfi|{O}", "raise")
        for hi in range(oi + 1, len(order)):  # vs_open / then the 3-bet wars
            h, H = order[hi], _THEIRS[order[hi]]
            p_b, st_b = folds(f"vs_open|{O}|", order[oi + 1 : hi])
            reach = p_open * p_b
            add(spot(h, [o]), h, reach, [*st_o, *st_b])
            tk = f"vs_open|{O}|{H}"
            p_af, st_af = folds(f"vs_open|{O}|", order[hi + 1 :])
            flop("srp", reach * share(tk, "call") * p_af,
                 [*st_o, *st_b, step(h, "call", tk, "C"), *st_af])  # fmt: skip
            p3 = reach * share(tk, "raise")
            st3 = [*st_o, *st_b, step(h, "raise", tk, "R")]
            k3 = f"vs_3bet|{O}|{H}"
            for ci in range(hi + 1, len(order)):  # cold callers of the 3-bet
                c = order[ci]
                p_c, st_c = folds(f"cold|{O}|{H}|", order[hi + 1 : ci])
                add(spot(c, [o, h]), c, p3 * p_c, [*st3, *st_c])
                ck = f"cold|{O}|{H}|{_THEIRS[c]}"
                p_cf, st_cf = folds(f"cold|{O}|{H}|", order[ci + 1 :])
                flop("3bp", p3 * p_c * share(ck, "call") * p_cf * share(k3, "fold"),
                     [*st3, *st_c, step(c, "call", ck, "C"), *st_cf, step(o, "fold", k3, "F")])  # fmt: skip
            p_rest, st_rest = folds(f"cold|{O}|{H}|", order[hi + 1 :])
            war = [*st3, *st_rest]  # everyone left folds to the 3-bet
            reach_w = p3 * p_rest
            raisers = [o, h]
            for kind in ("vs_3bet", "vs_4bet", "vs_5bet", "vs_6bet"):
                actor = o if kind in ("vs_3bet", "vs_5bet") else h
                k = f"{kind}|{O}|{H}"
                if k not in tab:
                    break
                add(spot(actor, raisers), actor, reach_w, war)
                if kind in ("vs_3bet", "vs_4bet"):  # called: a 3-bet / 4-bet pot
                    flop("3bp" if kind == "vs_3bet" else "4bp+", reach_w * share(k, "call"),
                         [*war, step(actor, "call", k, "C")])  # fmt: skip
                reach_w *= share(k, "raise")
                war = [*war, step(actor, "raise", k, "R")]
                raisers = [*raisers, actor]
    post = {pk: tuple(line for _, line in sorted(c, key=lambda x: -x[0])) for pk, c in flops.items()}
    return FreqMap(freq, {**lines, **{pk: ls[0] for pk, ls in post.items()}}, "gtow",
                   post_lines=post)


# ------------------------------------------------------------------ postflop conditionals


def _street_walk(doc: Mapping) -> tuple[list[dict[str, float]], float]:
    """Within the root street: expected decisions per situation for each player, and the
    probability the hand goes on to the next street with two players still in (heads-up)."""
    n = len(doc["players"])
    occ: list[dict[str, float]] = [{} for _ in range(n)]
    nxt = 0.0
    stack = [(doc["root"], 1.0, 0, frozenset())]
    while stack:
        nid, reach, bets, folded = stack.pop()
        node = _brief(doc, nid)
        if node["type"] in ("chance", "terminal"):
            leaf = node["type"] == "chance" or node.get("kind") == "leaf"
            if leaf and n - len(folded) >= 2:
                nxt += reach
            continue
        sit = spots.SITUATIONS[min(bets, 2)]
        mine = occ[node["player"]]
        mine[sit] = mine.get(sit, 0.0) + reach
        for a, f in zip(node["actions"], _action_freqs(node), strict=True):
            r = reach * f
            if r < MIN_REACH:
                continue
            kind = token_kind(a["token"])
            stack.append((a["child"], r, bets + (kind in ("raise", "allin")),
                          folded | {node["player"]} if kind == "fold" else folded))  # fmt: skip
    return occ, nxt


@dataclass(frozen=True)
class DocStats:
    street: str
    pot: str | None  # flop docs only
    ip: bool
    occ: dict[str, float]
    p_next: float


def doc_stats(toml_path: Path) -> tuple[DocStats, ...] | None:
    """DocStats of one heads-up SolveCache entry (<key>.toml + <key>.json), one per seat (both
    players' situations count, so a library flop document solved with the aggressor flagged
    also prices the caller's side); None for preflop spots, 3+ players, or unreadable files.
    The pot type of a flop spot comes from the pot per player in big blinds (<= 1.5 limped,
    <= 4.5 srp, <= 13 3bp, else 4bp+)."""
    js = toml_path.with_suffix(".json")
    try:
        game = tomllib.loads(toml_path.read_text())["game"]
        if game.get("street") not in POSTFLOP or not js.exists():
            return None
        doc = json.loads(js.read_text())
    except (OSError, ValueError, KeyError):
        return None
    positions = [p["position"] for p in doc.get("players", [])]
    if len(positions) != 2:  # heads-up street solves only
        return None
    occ, p_next = _street_walk(doc)
    pot = None
    if game["street"] == "flop":
        per_player_bb = float(game["pot"]) / len(positions) / float(game.get("bb") or 1)
        pot = ("limped" if per_player_bb <= 1.5 else "srp" if per_player_bb <= 4.5
               else "3bp" if per_player_bb <= 13 else "4bp+")  # fmt: skip
    return tuple(DocStats(game["street"], pot, spots.is_ip(p, positions), occ[k], p_next)
                 for k, p in enumerate(positions))  # fmt: skip


def conditionals(stats: list[DocStats]) -> dict[str, float]:
    """P(spot | hero sees the flop in <pot> <side>) keyed by the postflop spot key."""

    def mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    out: dict[str, float] = {}
    for pot in spots.POTS:
        for ip in (True, False):
            flop = [d for d in stats if d.street == "flop" and d.pot == pot and d.ip == ip]
            if not flop:
                continue
            reach = {"flop": 1.0}
            reach["turn"] = mean([d.p_next for d in flop]) or 0.0
            turn = [d for d in stats if d.street == "turn" and d.ip == ip]
            reach["river"] = reach["turn"] * (mean([d.p_next for d in turn]) or 0.0)
            for street in POSTFLOP:
                docs = flop if street == "flop" else [d for d in stats if d.street == street and d.ip == ip]
                for sit in spots.SITUATIONS:
                    m = mean([d.occ.get(sit, 0.0) for d in docs])
                    if m:
                        key = spots.spot_key({"street": street, "pot": pot, "ip": ip,
                                              "situation": sit})  # fmt: skip
                        out[key] = reach[street] * m
    return out


# ------------------------------------------------------------------ observations


def _window_rows(conn: sqlite3.Connection, window: int, table: int, depth: int) -> list[dict]:
    ensure(conn)
    hands = [r[0] for r in conn.execute(
        "SELECT hand_uid FROM train_hands WHERE table_size = ? AND depth = ? "
        "ORDER BY played_at DESC, rowid DESC LIMIT ?", (table, depth, window))]  # fmt: skip
    rows: list[dict] = []
    for k in range(0, len(hands), 500):
        chunk = hands[k : k + 500]
        cur = conn.execute(
            "SELECT d.hand_uid, d.spot, d.chosen, d.options_json, d.grade, d.note, "
            "h.drill_spot FROM train_decisions d JOIN train_hands h ON h.hand_uid = d.hand_uid "
            f"WHERE d.hand_uid IN ({','.join('?' * len(chunk))})", chunk)  # fmt: skip
        cols = [c[0] for c in cur.description]
        rows += [dict(zip(cols, r, strict=True)) for r in cur]
    return rows


def _blunder(row: Mapping[str, Any]) -> float | None:
    """1.0 for a blunder, 0.0 for best / mixed, None when the decision was not graded."""
    g = decision_grade(row)
    return None if g is None else float(g == grading.BLUNDER)


def observed_postflop(rows: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """From natural hands: P(hero sees the flop heads-up in pot/side) per hand and P(spot | that
    flop). Hands with a multiway postflop street count as hands but not as flops."""
    natural = [r for r in rows if r["drill_spot"] is None and r["spot"]]
    hands = {r["hand_uid"] for r in natural}
    multiway = {r["hand_uid"] for r in natural if r.get("note") == MULTIWAY_UNSOLVABLE}
    natural = [r for r in natural
               if not (r["hand_uid"] in multiway and r["spot"].startswith("post|"))]  # fmt: skip
    flop_of: dict[str, str] = {}
    for r in natural:
        if r["spot"].startswith("post|"):
            flop_of.setdefault(r["hand_uid"], "|".join(r["spot"].split("|")[:3]))
    pre: dict[str, float] = {}
    for pk in flop_of.values():
        pre[pk] = pre.get(pk, 0.0) + 1.0 / len(hands)
    seen: dict[str, int] = {}
    for pk in flop_of.values():
        seen[pk] = seen.get(pk, 0) + 1
    cond: dict[str, float] = {}
    for r in natural:
        if r["spot"].startswith("post|"):
            pk = "|".join(r["spot"].split("|")[:3])
            cond[r["spot"]] = cond.get(r["spot"], 0.0) + 1.0 / seen[pk]
    return pre, cond


# ------------------------------------------------------------------ the model


def rank(freq: Mapping[str, float], rows: list[dict]) -> list[dict[str, Any]]:
    """Leak rows over the spots with freq > MIN_FREQ, sorted by expected blunders per 100."""
    n: dict[str, int] = {}
    total: dict[str, float] = {}
    for r in rows:
        b = _blunder(r)
        if b is None or not r["spot"]:
            continue
        n[r["spot"]] = n.get(r["spot"], 0) + 1
        total[r["spot"]] = total.get(r["spot"], 0.0) + b
    all_n = sum(n.values())
    mean = (sum(total.values()) + PRIOR_N * PRIOR_BLUNDER_RATE) / (all_n + PRIOR_N)
    universe = [s for s, f in freq.items() if f > MIN_FREQ]
    big_n = max(sum(n.get(s, 0) for s in universe), 2)
    out = []
    for s in universe:
        k = n.get(s, 0)
        rate = (total.get(s, 0.0) + PRIOR_N * mean) / (k + PRIOR_N)
        expected = 100.0 * freq[s] * rate
        out.append({"spot": s, "label": spots.label(s), "freq": round(freq[s], 5), "n": k,
                    "blunders": int(total.get(s, 0.0)), "blunder_rate": round(rate, 4),
                    "expected_blunders_per_100": round(expected, 4),
                    "_w": expected + C_EXPLORE * math.sqrt(math.log(big_n) / (k + 1))})  # fmt: skip
    tot_w = sum(r["_w"] for r in out) or 1.0
    for r in out:
        r["weight"] = round(r.pop("_w") / tot_w, 5)
    out.sort(key=lambda r: (-r["expected_blunders_per_100"], -r["weight"], -r["freq"], r["spot"]))
    return out


class Leaks:
    """Leak table and drill picks for the trainer. `library(table, depth)` returns the root
    document or None; `cache_root` is the SolveCache directory scanned for street solves."""

    def __init__(self, db: str | Path | None, *,
                 library: Callable[[int, int], Mapping | None] | None = None,
                 cache_root: Path | None = None, freqs: Mapping | None = None) -> None:  # fmt: skip
        self.db = db
        self.library = library if library is not None else open_library
        self.cache_root = cache_root
        self.gtow = freqs
        self._maps: dict[tuple[int, int], FreqMap] = {}
        self._docs: dict[Path, tuple[DocStats, ...] | None] = {}  # SolveCache entries read so far
        self._lock = threading.Lock()

    def solver_map(self, table: int, depth: int) -> tuple[FreqMap, dict[str, float]]:
        """(preflop map with lines, cached per (table, depth); postflop conditionals over the
        SolveCache, reading only entries not seen before)."""
        with self._lock:
            fm = self._maps.get((table, depth))
            if fm is None:
                doc = self.library(table, depth)
                if doc is not None:
                    fm = library_walk(doc)
                else:
                    gt = self.gtow if self.gtow is not None else _gtow_freqs()
                    fm = gtow_walk(gt, table, depth)
                self._maps[(table, depth)] = fm
            if self.cache_root is not None and self.cache_root.exists():
                for path in sorted(self.cache_root.glob("*.toml")):
                    if path not in self._docs:
                        self._docs[path] = doc_stats(path)
            return fm, conditionals([d for ds in self._docs.values() if ds for d in ds])

    def warm(self) -> None:
        """Read the SolveCache entries now (the first scan takes seconds on a full cache)."""
        self.solver_map(6, 100)

    def rows(self, conn: sqlite3.Connection | None, table: int, depth: int,
             window: int) -> list[dict[str, Any]]:  # fmt: skip
        """The leak table (contracts/trainer.md GET /api/train/leaks) over the newest `window`
        trainer hands at this table size and depth."""
        if window < 1:
            raise ValueError("window must be >= 1")
        return rank(self.frequencies(conn, table, depth, window)[0],
                    _window_rows(conn, window, table, depth) if conn is not None else [])  # fmt: skip

    def frequencies(self, conn: sqlite3.Connection | None, table: int, depth: int,
                    window: int) -> tuple[dict[str, float], FreqMap]:  # fmt: skip
        """Per-hand frequency of every spot (preflop and postflop) and the solver map."""
        fm, cond = self.solver_map(table, depth)
        rows = _window_rows(conn, window, table, depth) if conn is not None else []
        obs_pre, obs_cond = observed_postflop(rows)
        freq = {k: v for k, v in fm.freq.items() if k.startswith("pf|")}
        pre = {k: v for k, v in fm.freq.items() if k.startswith("post|")} or obs_pre
        for pk, p in pre.items():
            for street in POSTFLOP:
                for sit in spots.SITUATIONS:
                    key = f"{pk}|{street}|{sit}"
                    c = cond.get(key, obs_cond.get(key))
                    if c is None and street == "flop" and sit == "first":
                        c = FIRST_FALLBACK  # no solved flop and no observation for the pot
                    if c:
                        freq[key] = p * c
        return freq, fm

    def line(self, table: int, depth: int, spot: str) -> Line | None:
        ls = self.lines(table, depth, spot)
        return ls[0] if ls else None

    def lines(self, table: int, depth: int, spot: str) -> tuple[Line, ...]:
        """Every known line into the spot, highest reach first (postflop spots: the heads-up
        flop lines of its pot and side; one line elsewhere)."""
        fm, _ = self.solver_map(table, depth)
        key = "|".join(spot.split("|")[:3]) if spot.startswith("post|") else spot
        if key in fm.post_lines:
            return fm.post_lines[key]
        return (fm.lines[key],) if key in fm.lines else ()


def _gtow_freqs() -> Mapping:
    try:
        return gtowizard.load_freqs()
    except FileNotFoundError:
        return {}


_LIBS: dict[Path, LazyDoc] = {}


def open_library(table: int, depth: int) -> LazyDoc | None:
    """The gto library root for table/depth as a LazyDoc (index built on first use)."""
    path = library_path(table, depth, "gto")
    if not path.exists():
        return None
    if path not in _LIBS:
        _LIBS[path] = LazyDoc.open(path)
    return _LIBS[path]
