"""Grade decisions of past hands by frequency (Plan 7, contracts/trainer.md "Plan 7 additions"):
the same best / mixed / blunder rule as the trainer (pn.train.grading).

Postflop: one GTO solve (source "gto", no locks) per (hand, street, player) at the street start
with that player as hero, heads-up only, with the `review` preset; every decision of the player
on the street is read off it by walking the real actions and graded from the solver's frequency
of the chosen action for the actual combo. EV columns (ev_chosen / ev_best / loss) are filled
only from documents with `meta.ev_available` true; TexasSolver documents leave them NULL.
Preflop: chart-based, never solved: the PokerCoaching chart action (pure: frequency 1 or 0)
where those charts cover the situation, else the GTO Wizard frequencies of the combo's hand
class; the row's `backend` names the source (`pokercoaching` | `gtowizard`).
Postflop in a hand that saw the flop with 3+ players is not solved on any street (2026-10-07:
2-player only): those rows carry note "multiway_unsolvable".
A row that cannot be graded keeps NULL grade fields and a `note` saying why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from pn import ranges
from pn.adapter.spot import ProfileSource, Spot, SpotError, build_spot_from_hand
from pn.adapter.state import replay
from pn.ev.decisions import GradableDecision
from pn.live.assist import LIVE_GAME, LiveError, _match_token, walk_to_hero
from pn.profile import charts, gtowizard
from pn.solve import EvUnavailable, SolveCache, SolveError, action_ev_loss, spot_key
from pn.solve.backend import MULTIWAY_UNSOLVABLE
from pn.solve.ev import combo_position
from pn.solve.request import apply_preset
from pn.train import grading
from pn.train import preflop as train_preflop

REVIEW_SOURCES = ("gto",)  # Plan 7: no opponent-specific (locked) solves
HU_PRESET = "review"  # heads-up postflop
GTOW_DEPTHS = (100, 200)
ADDED_COMBO_WEIGHT = 0.001
DEFAULT_GAME: Mapping[str, Any] = dict(LIVE_GAME)
NO_PREFLOP_FREQ = "no_preflop_frequencies"  # no PokerCoaching chart / GTO Wizard cell
PC_SOURCE, GTOW_SOURCE = train_preflop.PC, train_preflop.GTOW  # preflop rows' `backend`
# GTO Wizard cell codes -> the action kind they grade (bets and raises are one kind)
GTOW_KINDS = {"R": "raise", "A": "raise", "C": "call", "F": "fold", "X": "check"}
KIND_OF = {"bet": "raise"}  # hand action kind -> graded kind


@dataclass(frozen=True)
class Grade:
    hand_id: str
    seq: int
    source: str
    street: str
    player_id: str
    chosen: str
    best: str
    ev_chosen: int | None
    ev_best: int | None
    loss: int | None
    gto_freq_chosen: float | None
    backend: str | None
    solve_key: str | None
    exploitability: float | None  # meta.exploitability_pct_pot
    note: str | None
    spot: str
    grade: str | None = None  # best | mixed | blunder (pn.train.grading); None = not graded
    freq_chosen: float | None = None  # solver / chart frequency of the chosen action
    best_freq: float | None = None  # frequency of the most frequent action


class NoGrade(Exception):
    """A decision (or a whole street) cannot be solved; the message is the row's note."""


def spot_note(e: Exception) -> str:
    msg = str(e)
    if "at most 8" in msg:
        return "too_many_players"
    if "all-in before" in msg:
        return "allin_before_street"
    if "did not reach" in msg:
        return "incomplete_hand"
    return f"spot_error: {msg}"[:200]


def players_at(hand: Mapping[str, Any], street: str) -> int:
    """Players still in (not folded) at the start of `street`."""
    return len(hand["players"]) - len(replay(hand, street).folded)


def effective_bb(hand: Mapping[str, Any], pid: str) -> float:
    bb = int((hand.get("blinds") or {}).get("bb") or 0)
    stacks = {p["player_id"]: int(p.get("stack_start") or 0) for p in hand["players"]}
    others = [v for k, v in stacks.items() if k != pid]
    if bb <= 0 or not others:
        return 0.0
    return min(stacks[pid], max(others)) / bb


def gto_kinds(freqs: Mapping | None, hand: Mapping[str, Any],
              dec: GradableDecision) -> dict[str, float] | None:  # fmt: skip
    """GTO Wizard frequencies of the combo's hand class at the decision, by action kind
    (fold | check | call | raise), normalised; None without a cell."""
    if freqs is None or dec.situ is None:
        return None
    key = gtowizard.situation_key(dec.situ)
    depth = min(GTOW_DEPTHS, key=lambda d: abs(d - effective_bb(hand, dec.player_id)))
    spot = freqs.get(f"{depth}|{key}") if key else None
    cell = (spot or {}).get("cells", {}).get(ranges.combo_class(dec.combo))
    if not cell:
        return None
    out: dict[str, float] = {}
    for code, v in cell.items():
        if code in GTOW_KINDS:
            out[GTOW_KINDS[code]] = out.get(GTOW_KINDS[code], 0.0) + float(v)
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total > 0 else None


def pc_kinds(hand: Mapping[str, Any], dec: GradableDecision) -> dict[str, float] | None:
    """PokerCoaching chart action at the decision by action kind (pure: one kind at 1), or
    None when the charts do not cover the situation."""
    if dec.situ is None:
        return None
    kind, *seats = dec.situ
    n = len(hand["players"])
    actor = seats[0] if kind in ("vs_3bet", "limp_vs_raise") else seats[-1]
    other = train_preflop.pc_other(kind, tuple(seats))
    found = train_preflop.pc_dist(charts.table_kind(n), kind, actor, other, dec.combo, n)
    if found is None:
        return None
    out: dict[str, float] = {}
    for code, v in found[0].items():
        out[GTOW_KINDS[code]] = out.get(GTOW_KINDS[code], 0.0) + v
    return out


def gto_freq(freqs: Mapping | None, hand: Mapping[str, Any], dec: GradableDecision) -> float | None:
    """GTO Wizard frequency of the chosen action for the combo's hand class (preflop)."""
    kinds = gto_kinds(freqs, hand, dec)
    if kinds is None:
        return None
    kind = KIND_OF.get(dec.action["kind"], dec.action["kind"])
    return round(kinds.get(kind, 0.0), 4)


def with_combo(config: Mapping[str, Any], hero: int, combo: str) -> tuple[dict, bool]:
    """Config whose hero range contains `combo` (added at ADDED_COMBO_WEIGHT when the modeled
    range excludes it, so the solve has a strategy row for it). Returns (config, added)."""
    rng = ranges.parse(config["players"][hero]["range"])
    if rng.get(combo, 0.0) > 0:
        return dict(config), False
    players = [dict(p) for p in config["players"]]
    players[hero]["range"] = ranges.to_text({**rng, combo: ADDED_COMBO_WEIGHT})
    return {**config, "players": players}, True


def _ev_fields(doc: Mapping[str, Any], nid: str, combo: str, chosen: str) -> dict[str, int]:
    """ev_chosen / ev_best / loss in cents, only for documents with `meta.ev_available`."""
    if not (doc.get("meta") or {}).get("ev_available"):
        return {}
    try:
        loss, best = action_ev_loss(doc, nid, combo, chosen)
    except EvUnavailable:
        return {}
    node = doc["nodes"][nid]
    evs = node["action_ev"][combo_position(doc, combo)]
    ev_best = float(evs[[a["token"] for a in node["actions"]].index(best)])
    return {"ev_chosen": round(ev_best - loss), "ev_best": round(ev_best), "loss": round(loss)}


class Grader:
    def __init__(self, cache: SolveCache, *, source: str = "gto",
                 profiles: ProfileSource | None = None, game: Mapping[str, Any] | None = None,
                 freqs: Mapping | None = None) -> None:  # fmt: skip
        if source not in REVIEW_SOURCES:
            raise ValueError(f"source must be one of {REVIEW_SOURCES}, got {source!r}")
        self.cache, self.source, self.profiles = cache, source, profiles
        self.game = {**DEFAULT_GAME, **(game or {})}
        self.freqs = freqs

    # ------------------------------------------------------------ rows
    def _row(self, dec: GradableDecision, **kw: Any) -> Grade:
        empty = Grade(dec.hand_id, dec.seq, self.source, dec.street, dec.player_id, dec.token,
                      "", None, None, None, None, None, None, None, None, dec.spot)  # fmt: skip
        return replace(empty, **kw)

    # ------------------------------------------------------------ solving
    def _solve(self, hand: Mapping[str, Any], dec: GradableDecision) -> tuple[Spot, dict, str, bool]:
        try:
            spot = build_spot_from_hand(hand, dec.street, dec.player_id, source="gto",
                                        profiles=self.profiles, game=self.game)  # fmt: skip
        except SpotError as e:
            raise NoGrade(spot_note(e)) from e
        except ValueError as e:
            raise NoGrade(f"spot_error: {e}"[:200]) from e
        config, added = with_combo(spot.config, spot.hero_index(), dec.combo)
        config, backend = apply_preset(config, HU_PRESET)
        try:
            doc = self.cache.solve(config, backend)
        except (SolveError, OSError) as e:
            raise NoGrade(f"solve_failed: {e}"[:200]) from e
        return spot, doc, spot_key(config), added

    def _read(self, doc: Mapping[str, Any], spot: Spot, hand: Mapping[str, Any],
              dec: GradableDecision) -> tuple[str, str, dict[str, float]]:  # fmt: skip
        """(node id, chosen token, the combo's frequency per token) at the decision node."""
        before = {**hand, "actions": [a for a in hand["actions"] if a["seq"] < dec.seq]}
        try:
            nid, _ = walk_to_hero(doc, spot, before)
        except (LiveError, KeyError) as e:  # KeyError: document without the walked node
            raise NoGrade(f"off_tree: {e}"[:200]) from e
        node = doc["nodes"][nid]
        choice = _match_token(node["actions"], dec.action)
        if choice is None:
            raise NoGrade(f"off_tree: no tree action for {dec.action['kind']}")
        try:
            row = node["strategy"][combo_position(doc, dec.combo)]
        except (EvUnavailable, KeyError, IndexError, TypeError) as e:
            raise NoGrade(f"no_strategy: {e}"[:200]) from e
        freqs = {a["token"]: float(f) for a, f in zip(node["actions"], row, strict=True)}
        if sum(freqs.values()) <= 0:
            raise NoGrade("combo_outside_range")
        return nid, choice["token"], freqs

    def _grade_one(self, hand: Mapping[str, Any], dec: GradableDecision, solved: tuple) -> Grade:
        spot, doc, key, added = solved
        meta = doc.get("meta") or {}
        ctx = {"backend": meta.get("backend"), "solve_key": key,
               "exploitability": meta.get("exploitability_pct_pot")}  # fmt: skip
        try:
            nid, chosen, freqs = self._read(doc, spot, hand, dec)
        except NoGrade as e:
            return self._row(dec, note=str(e), **ctx)
        g = grading.grade(freqs, chosen)
        return self._row(dec, chosen=chosen, best=g.best_token, grade=g.grade,
                         freq_chosen=g.freq, best_freq=g.best_freq,
                         **_ev_fields(doc, nid, dec.combo, chosen), **ctx,
                         note="combo_added_to_range" if added else None)  # fmt: skip

    # ------------------------------------------------------------ entry point
    def grade_hand(self, hand: Mapping[str, Any], decs: list[GradableDecision]) -> list[Grade]:
        flags = set(hand.get("flags") or [])
        for flag, note in (("incomplete", "incomplete_hand"), ("bomb_pot", "bomb_pot")):
            if flag in flags:
                return [self._row(d, note=note) for d in decs]
        out: list[Grade] = []
        groups: dict[tuple[str, str], list[GradableDecision]] = {}
        for d in decs:
            groups.setdefault((d.street, d.player_id), []).append(d)
        for (street, _pid), group in groups.items():
            if street == "preflop":
                out += [self._preflop(hand, d) for d in group]
                continue
            if players_at(hand, "flop") > 2:  # multiway flop: no postflop street is solved
                out += [self._row(d, note=MULTIWAY_UNSOLVABLE) for d in group]
                continue
            try:
                solved = self._solve(hand, group[0])
            except NoGrade as e:
                out += [self._row(d, note=str(e)) for d in group]
                continue
            out += [self._grade_one(hand, d, solved) for d in group]
        return sorted(out, key=lambda g: g.seq)

    def _preflop(self, hand: Mapping[str, Any], dec: GradableDecision) -> Grade:
        """Chart-based, never solved: PokerCoaching chart action, else GTO Wizard class
        frequencies, by action kind. `backend` = the source used."""
        kinds, source = pc_kinds(hand, dec), PC_SOURCE
        if kinds is None:
            kinds, source = gto_kinds(self.freqs, hand, dec), GTOW_SOURCE
        chosen = KIND_OF.get(dec.action["kind"], dec.action["kind"])
        g = grading.try_grade(kinds, chosen)
        if g is None:
            return self._row(dec, note=NO_PREFLOP_FREQ)
        return self._row(dec, best=g.best_token, grade=g.grade, freq_chosen=g.freq,
                         best_freq=g.best_freq, gto_freq_chosen=g.freq,
                         backend=source)  # fmt: skip
