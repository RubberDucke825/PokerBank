"""Live assist (brainstorm item 93): poll a PokerNow game log, rebuild the hand in progress,
solve hero's current street (GTO ranges, no node locks: Plan 7), and report hero's
action frequencies at the current decision.

Flow: fetch log -> pn.ingest.parse (the last hand is flagged `incomplete` while it is being
played) -> is hero next to act? -> pn.adapter.spot at the start of the current street ->
pn.solve.SolveCache (heads-up postflop: a cached texas document only, never a new solve;
preflop and 3+ players postflop: refused) -> walk the solved tree
along this street's real actions (bets snapped to the nearest tree size) -> hero's strategy row for `hero_cards` (PokerNow logs do
not reveal hero's cards, so they are an input), else the node's range-level summary.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pn.adapter.spot import ProfileSource, Spot, build_spot_from_hand
from pn.adapter.state import POSTS, board_cards, next_to_act, replay
from pn.solve import SolveCache, SolveError, action_ev_loss, spot_key
from pn.solve.backend import MULTIWAY_UNSOLVABLE, is_multiway_postflop
from pn.solve.request import apply_preset

STREET_BY_BOARD = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
LIVE_GAME = {"time_limit_sec": 30, "max_iterations": 300, "target_exploitability": 1.0}
AGGRESSIVE_TOKENS = ("bet", "raise", "allin")
SOLVE_GRACE_SEC = 30
LIVE_BACKEND = None  # Plan 7: texas only; preflop spots raise SolveError -> LiveError
NO_SOLVE = "no_solve_available"
# heads-up postflop: besides the live spot itself, look for the spot as these presets cache it
CACHED_PRESETS = ("library", "review", "trainer")

Solver = Callable[[str], dict]


class LiveError(RuntimeError):
    """The current state cannot be turned into a recommendation."""


@dataclass(frozen=True)
class Recommendation:
    hand_id: str
    street: str
    history: tuple[str, ...]  # tree tokens of this street's actions so far
    summary: dict[str, float]  # range-level frequencies at hero's node
    hero_freqs: dict[str, float] | None  # hero's combo, when hero_cards are known
    hero_cards: tuple[str, ...] | None
    tips: tuple[str, ...]
    warnings: tuple[str, ...]


def parse_cards(text: str | None) -> tuple[str, str] | None:
    """'AhKd' / 'Ah Kd' -> ('Ah', 'Kd')."""
    if not text:
        return None
    s = text.replace(" ", "").replace(",", "")
    if len(s) != 4:
        raise ValueError(f"hero cards must be two cards like AhKd: {text!r}")
    return s[0].upper() + s[1].lower(), s[2].upper() + s[3].lower()


def current_hand(hands: list[dict]) -> dict | None:
    """The hand being played: the last parsed hand if it has no ending line yet."""
    if hands and "incomplete" in (hands[-1].get("flags") or []):
        return hands[-1]
    return None


def current_street(hand: Mapping[str, Any]) -> str:
    n = len(board_cards(hand))
    if n not in STREET_BY_BOARD:
        raise LiveError(f"unexpected board size {n}")
    return STREET_BY_BOARD[n]


def hero_to_act(hand: Mapping[str, Any], hero_id: str) -> str | None:
    """Current street if hero is the next player to act, else None."""
    street = current_street(hand)
    state = replay(hand, street, upto_seq=_max_seq(hand) + 1)
    return street if next_to_act(hand, state) == hero_id else None


def _max_seq(hand: Mapping[str, Any]) -> int:
    return max((a["seq"] for a in hand["actions"]), default=0)


def cached_dcfr(cache: SolveCache, spot: Mapping[str, Any]) -> dict | None:
    """A cached texas document for `spot`: its own key, else its key under CACHED_PRESETS
    (library fills, review and trainer solves). None when no texas document matches. A spot
    texas refuses (e.g. with locks) has no preset variants."""
    try:
        variants = [apply_preset(spot, name)[0] for name in CACHED_PRESETS]
    except SolveError:
        variants = []
    for candidate in (spot, *variants):
        doc = cache.get(spot_key(candidate))
        if doc is not None and (doc.get("meta") or {}).get("backend") == "texas":
            return doc
    return None


def cache_solver(cache: SolveCache | None = None, backend: str | None = LIVE_BACKEND) -> Solver:
    """Solver callable over a SolveCache (default data/solves/cache); failures -> LiveError.
    Preflop spots go to `backend`, which refuses them (Plan 7: texas is postflop only), so they
    raise LiveError. Heads-up postflop is cache-only (2026-10-07): a matching cached texas
    document is served, else LiveError("no_solve_available"); no solve is ever started here.
    Postflop spots with 3+ players raise LiveError("multiway_unsolvable")."""
    cache = cache if cache is not None else SolveCache()

    def solve(toml_text: str) -> dict:
        spot = tomllib.loads(toml_text)
        if is_multiway_postflop(spot):
            raise LiveError(MULTIWAY_UNSOLVABLE)
        if spot["game"]["street"] != "preflop":
            doc = cached_dcfr(cache, spot)
            if doc is None:
                raise LiveError(f"{NO_SOLVE}: no cached texas solve for this spot")
            return doc
        try:
            return cache.solve(spot, backend)
        except SolveError as e:
            raise LiveError(str(e)) from e

    return solve


def ev_loss_for(doc: Mapping[str, Any], node_id: str, combo: str, token: str) -> tuple[float, str]:
    """(EV loss in cents, best token) of playing `token` with `combo` at `node_id`."""
    return action_ev_loss(doc, node_id, combo, token)


def _match_token(actions: list[dict], act: Mapping[str, Any]) -> dict | None:
    kind = act["kind"]
    if kind in ("fold", "check", "call"):
        return next((a for a in actions if a["token"] == kind), None)
    sized = [a for a in actions if a["token"].startswith(AGGRESSIVE_TOKENS) and "amount" in a]
    if not sized:
        return None
    if act.get("allin"):
        allin = next((a for a in sized if a["token"] == "allin"), None)
        if allin:
            return allin
    return min(sized, key=lambda a: abs(int(a["amount"]) - int(act["to_amount"])))


def walk_to_hero(out: Mapping[str, Any], spot: Spot, hand: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Follow this street's real actions from the root; (hero node id, tokens taken)."""
    index = {p.player_id: i for i, p in enumerate(spot.players)}
    nid, tokens = out["root"], []
    for act in hand["actions"]:
        if act["street"] != spot.street or act["kind"] in POSTS or act["player_id"] not in index:
            continue
        node = out["nodes"][nid]
        if node.get("type") != "action" or node["player"] != index[act["player_id"]]:
            raise LiveError(f"hand left the solved tree after {tokens}")
        choice = _match_token(node["actions"], act)
        if choice is None:
            raise LiveError(f"no tree action for {act['kind']} after {tokens}")
        tokens.append(choice["token"])
        nid = choice["child"]
    node = out["nodes"][nid]
    if node.get("type") != "action" or node["player"] != spot.hero_index():
        raise LiveError(f"hero is not to act in the solved tree after {tokens}")
    return nid, tokens


def hero_strategy(out: Mapping[str, Any], node: Mapping[str, Any],
                  cards: tuple[str, ...]) -> dict[str, float] | None:  # fmt: skip
    """Hero's action frequencies for `cards` at `node` (None if the combo is not in range)."""
    want = set(cards)
    i = next((k for k, c in enumerate(out["combos"]) if {c[:2], c[2:]} == want), None)
    strat, reach = node.get("strategy"), node.get("reach")
    if i is None or strat is None or (reach is not None and reach[i] <= 0) or not any(strat[i]):
        return None
    return {a["token"]: float(f) for a, f in zip(node["actions"], strat[i], strict=True)}


def recommend(hand: dict, hero_id: str, solve: Solver, *, hero_cards: tuple[str, ...] | None = None,
              profiles: ProfileSource | None = None, game: Mapping[str, Any] | None = None) -> Recommendation | None:  # fmt: skip
    """Recommendation for hero's pending decision in `hand`, or None if hero is not to act."""
    street = hero_to_act(hand, hero_id)
    if street is None:
        return None
    upto = _max_seq(hand) + 1 if street == "preflop" else None
    spot = build_spot_from_hand(hand, street, hero_id, source="gto", profiles=profiles,
                                hero_cards=hero_cards,
                                upto_seq=upto, game={**LIVE_GAME, **(game or {})})  # fmt: skip
    out = solve(spot.toml)
    nid, tokens = walk_to_hero(out, spot, hand)
    node = out["nodes"][nid]
    raw = node.get("summary") or {}
    summary = {a["token"]: float(raw[a["token"]]) for a in node["actions"] if a["token"] in raw}
    freqs = hero_strategy(out, node, hero_cards) if hero_cards else None
    tips = tuple(f"{p.name}: {p.tip}" for p in spot.players if p.tip)
    return Recommendation(hand["hand_id"], street, tuple(tokens), summary, freqs,
                          tuple(hero_cards) if hero_cards else None, tips, spot.warnings)  # fmt: skip


def render(rec: Recommendation) -> str:
    def fmt(d: Mapping[str, float]) -> str:
        return "  ".join(f"{k} {v:.0%}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))

    lines = [f"hand {rec.hand_id} {rec.street} after [{' '.join(rec.history) or 'start'}]"]
    if rec.hero_freqs is not None:
        lines.append(f"  {''.join(rec.hero_cards or ())}: {fmt(rec.hero_freqs)}")
    elif rec.hero_cards:
        lines.append(f"  {''.join(rec.hero_cards)}: not in hero's modeled range")
    lines.append(f"  range: {fmt(rec.summary)}")
    lines += [f"  tip  {t}" for t in rec.tips]
    lines += [f"  note {w}" for w in rec.warnings]
    return "\n".join(lines)


def watch(fetch: Callable[[], list[dict]], hero_id: str, solve: Solver, *,
          emit: Callable[[str], None] = print, profiles: ProfileSource | None = None,
          interval: float = 5.0,
          max_polls: int | None = None, sleep: Callable[[float], None] = time.sleep) -> int:  # fmt: skip
    """Poll `fetch()` (parsed hands) and emit a range-level recommendation once per new hero
    decision (hero's cards change every hand; use `recommend` with hero_cards for one combo).
    Returns the number of recommendations emitted."""
    seen: set[tuple] = set()
    polls = emitted = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        hand = current_hand(fetch())
        key = (hand["hand_id"], len(hand["actions"])) if hand is not None else None
        if key is not None and key not in seen and hero_to_act(hand, hero_id):
            seen.add(key)  # one solve per decision, whatever its outcome
            try:
                rec = recommend(hand, hero_id, solve, profiles=profiles)
            except (LiveError, ValueError) as e:
                emit(f"hand {hand['hand_id']}: no recommendation ({e})")
            else:
                if rec is not None:
                    emitted += 1
                    emit(render(rec))
        if max_polls is None or polls < max_polls:
            sleep(interval)
    return emitted


__all__ = ["LiveError", "Recommendation", "cache_solver", "current_hand", "ev_loss_for",
           "hero_to_act", "parse_cards", "recommend", "render", "walk_to_hero", "watch"]  # fmt: skip
