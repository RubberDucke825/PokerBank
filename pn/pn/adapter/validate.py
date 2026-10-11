"""Spot config validation (contracts/tree_config.md) and a small TOML writer for spot
configs."""

from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pn import ranges
from pn.adapter.state import BOARD_LEN, POSTFLOP_ORDER, PREFLOP_ORDER, STREETS

TREE_POSITIONS = ("UTG", "UTG1", "UTG2", "LJ", "HJ", "CO", "BTN", "SB", "BB", "STR")
FLOP_SIZES = frozenset({25, 33, 50, 100})
LATER_SIZES = frozenset({75, 150})
PREFLOP_LISTS = ("open", "three_bet_ip", "three_bet_oop", "four_bet")
PLAYER_KEYS = frozenset({"name", "position", "stack", "range", "hero"})
# Plan 7: no node locks. The one lock left keeps a folded preflop seat (a listed BB) folding.
FOLD_LOCK = {"preflop:*:*": {"fold": 1.0, "order": "uniform"}}  # checks when fold is illegal
LIMP_MODES = ("all", "sb_only", "none")
HAND_ABSTRACTIONS = ("combo", "class")
LEAF_KINDS = ("equity_realization",)
PREFLOP_PRESETS = ("gtowizard",)
REALIZATION_KEYS = {"base": ("ip", "oop", "sb", "bb", "multiway_penalty"),
                    "hand": ("pair", "suited", "connected", "high_card", "broadway2")}  # fmt: skip


class SpotValidationError(ValueError):
    """Spot config violates the tree_config contract."""


def _fail(msg: str) -> None:
    raise SpotValidationError(msg)


def _check_game(game: Mapping[str, Any], n_players: int) -> None:
    street = game.get("street")
    if street not in STREETS:
        _fail(f"game.street must be one of {STREETS}, got {street!r}")
    board = list(game.get("board", []))
    if len(board) != BOARD_LEN[street]:
        _fail(f"{street} needs {BOARD_LEN[street]} board cards, got {len(board)}")
    try:
        ints = [ranges.card_to_int(c) for c in board]
    except ranges.RangeParseError as e:
        raise SpotValidationError(str(e)) from e
    if len(set(ints)) != len(ints):
        _fail(f"duplicate board cards {board}")
    if int(game.get("pot", 0)) <= 0:
        _fail("game.pot must be > 0")
    mode = game.get("mode", "depth_limited")
    if mode not in ("full", "depth_limited"):
        _fail(f"bad mode {mode!r}")
    if mode == "full" and n_players > 3:
        _fail("mode=full allowed only with <= 3 players")
    if street == "preflop" and mode != "depth_limited":
        _fail("preflop spots require mode=depth_limited")
    if game.get("hand_abstraction", "combo") not in HAND_ABSTRACTIONS:
        _fail(f"game.hand_abstraction must be one of {HAND_ABSTRACTIONS}")
    if game.get("hand_abstraction") == "class" and street != "preflop":
        _fail('hand_abstraction = "class" requires street = "preflop"')


def leaf_model(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The effective leaf model {kind, realization?}: top-level [leaf_model] table or
    [game] leaf_model (bare string or inline table); setting both is an error."""
    top, inline = cfg.get("leaf_model"), cfg.get("game", {}).get("leaf_model")
    if top is not None and inline is not None:
        _fail("set either [leaf_model] or [game] leaf_model, not both")
    lm = top if top is not None else inline if inline is not None else "equity_realization"
    if isinstance(lm, str):
        lm = {"kind": lm}
    if not isinstance(lm, Mapping):
        _fail(f"leaf_model must be a string or a table: {lm!r}")
    unknown = set(lm) - {"kind", "realization"}
    if unknown:
        _fail(f"leaf_model: unknown keys {sorted(unknown)}")
    if lm.get("kind") not in LEAF_KINDS:
        _fail(f"leaf_model kind must be one of {LEAF_KINDS}: {lm.get('kind')!r}")
    return dict(lm)


def _resolve(path: str) -> Path:
    """Relative paths are relative to the solver's cwd; the repo root is tried as well."""
    from pn.profile.paths import repo_root

    p = Path(path)
    return p if p.is_absolute() or p.exists() else repo_root() / p


def validate_realization(table: Mapping[str, Any]) -> None:
    """config/realization.toml rules."""
    unknown = set(table) - set(REALIZATION_KEYS)
    if unknown:
        _fail(f"realization table: unknown sections {sorted(unknown)}")
    for section, keys in REALIZATION_KEYS.items():
        sec = table.get(section, {})
        bad = set(sec) - set(keys)
        if bad:
            _fail(f"realization table: unknown keys [{section}] {sorted(bad)}")
        for k, v in sec.items():
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                _fail(f"realization table: [{section}] {k} must be a finite number")
    base = table.get("base", {})
    if any(float(base.get(k, 1.0)) <= 0 for k in ("ip", "oop", "sb", "bb")):
        _fail("realization table: [base] ip/oop/sb/bb must be > 0")
    if float(base.get("multiway_penalty", 0.0)) < 0:
        _fail("realization table: multiway_penalty must be >= 0")


def _check_leaf_model(cfg: Mapping[str, Any]) -> None:
    lm = leaf_model(cfg)
    if "realization" not in lm:
        return
    path = _resolve(str(lm["realization"]))
    try:
        validate_realization(tomllib.loads(path.read_text()))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SpotValidationError(f"leaf_model realization {lm['realization']}: {e}") from e


def _check_players(players: list[Mapping[str, Any]], street: str) -> None:
    if not 2 <= len(players) <= 8:
        _fail(f"need 2..=8 players, got {len(players)}")
    order = PREFLOP_ORDER if street == "preflop" else POSTFLOP_ORDER
    pos = [p["position"] for p in players]
    for p in pos:
        if p not in TREE_POSITIONS:
            _fail(f"position {p!r} not supported by the tree")
    if len(set(pos)) != len(pos):
        _fail(f"duplicate positions {pos}")
    if len({p["name"] for p in players}) != len(players):
        _fail("player names must be unique")
    if pos != sorted(pos, key=order.index):
        _fail(f"players not in {street} action order: {pos}")
    heroes = sum(bool(p.get("hero")) for p in players)
    if heroes != 1:
        _fail(f"exactly one player must have hero = true, got {heroes}")
    for p in players:
        if int(p["stack"]) < 0:
            _fail(f"{p['name']}: negative stack")
        try:
            if not ranges.parse(p["range"]):
                _fail(f"{p['name']}: empty range")
        except ranges.RangeParseError as e:
            raise SpotValidationError(f"{p['name']}: {e}") from e
        allowed = PLAYER_KEYS | ({"lock"} if street == "preflop" and p.get("lock") == FOLD_LOCK else set())
        unknown = set(p) - allowed
        if unknown:
            _fail(f"{p['name']}: unknown keys {sorted(unknown)}")


def _check_postflop_sizes(sizes: Mapping[str, Any]) -> None:
    flop = sizes.get("flop_bets", "auto")
    if flop != "auto" and (not 1 <= len(flop) <= 2 or not set(flop) <= FLOP_SIZES):
        _fail(f"flop_bets must be 'auto' or 1-2 sizes from {sorted(FLOP_SIZES)}: {flop}")
    for key in ("turn_bets", "river_bets"):
        v = sizes.get(key, [75 if key == "turn_bets" else 150])
        if len(v) != 1 or v[0] not in LATER_SIZES:
            _fail(f"{key} must be exactly one of {sorted(LATER_SIZES)}: {v}")
    if len(sizes.get("raise", [100])) != 1:
        _fail("raise must contain exactly one size (v1)")
    if int(sizes.get("max_bets_per_street", 3)) < 1:
        _fail("max_bets_per_street must be >= 1")
    t = float(sizes.get("allin_threshold", 0.67))
    if not 0.0 < t <= 1.0:
        _fail("allin_threshold must be in (0, 1]")


def _check_preflop_sizes(pf: Mapping[str, Any]) -> None:
    for key in PREFLOP_LISTS:
        v = pf.get(key)
        if v is None:
            continue
        if not 1 <= len(v) <= 3 or any(int(x) <= 100 for x in v):
            _fail(f"sizes.preflop.{key}: 1-3 entries, each > 100: {v}")
    for x in pf.get("five_bet", ["allin"]):
        if x != "allin" and (not isinstance(x, int) or x <= 100):
            _fail(f"sizes.preflop.five_bet entries must be 'allin' or > 100: {x!r}")
    sq = pf.get("squeeze")
    if sq is not None and int(sq.get("base", 300)) <= 100:
        _fail("sizes.preflop.squeeze.base must be > 100")
    if int(pf.get("max_bets", 5)) < 2:
        _fail("sizes.preflop.max_bets must be >= 2")
    if pf.get("limp", "all") not in LIMP_MODES:
        _fail(f"sizes.preflop.limp must be one of {LIMP_MODES}: {pf.get('limp')!r}")
    if not isinstance(pf.get("cold_call_3bet", True), bool):
        _fail("sizes.preflop.cold_call_3bet must be true or false")
    preset = pf.get("preset")
    if preset is not None and preset not in PREFLOP_PRESETS:
        _fail(f"sizes.preflop.preset must be one of {PREFLOP_PRESETS}: {preset!r}")
    for pos, pct in (pf.get("three_bet_by_pos") or {}).items():
        if pos not in TREE_POSITIONS:
            _fail(f"sizes.preflop.three_bet_by_pos: unknown position {pos!r}")
        if isinstance(pct, bool) or not isinstance(pct, int) or pct <= 100:
            _fail(f"sizes.preflop.three_bet_by_pos.{pos}: raise-to must be > 100 (% of BB)")
    mc = pf.get("max_callers")
    if mc is not None and (isinstance(mc, bool) or not isinstance(mc, int) or mc < 1):
        _fail(f"sizes.preflop.max_callers must be an integer >= 1: {mc!r}")


def validate_config(cfg: Mapping[str, Any]) -> None:
    """Raise SpotValidationError if `cfg` (TOML-shaped dict) breaks the contract rules."""
    game, players = cfg.get("game", {}), cfg.get("players", [])
    _check_game(game, len(players))
    _check_leaf_model(cfg)
    street = game["street"]
    _check_players(players, street)
    pos = [p["position"] for p in players]
    straddle = int(game.get("straddle", 0) or 0)
    if ("STR" in pos) != (straddle > 0):
        _fail("game.straddle > 0 must be set exactly when a STR player is listed")
    if straddle and straddle <= int(game.get("bb", 0)):
        _fail("straddle must be greater than bb")
    if street == "preflop":
        if not game.get("sb") or not game.get("bb"):
            _fail("preflop spots need sb and bb")
        if "BB" not in pos:
            _fail("preflop spots need a listed BB")
        posted = {"SB": int(game["sb"]), "BB": int(game["bb"]), "STR": straddle}
        if int(game["pot"]) < sum(posted.get(p, 0) for p in pos):
            _fail("preflop pot must cover the posted blinds of listed players")
    la = game.get("last_aggressor")
    if la is not None and la not in pos:
        _fail(f"last_aggressor {la!r} is not a listed player")
    sizes = cfg.get("sizes", {})
    _check_postflop_sizes(sizes)
    _check_preflop_sizes(sizes.get("preflop", {}))


# ------------------------------------------------------------------ TOML writer


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k)


def _val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(round(v, 6))
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, Mapping):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _scalars(d: Mapping[str, Any]) -> list[str]:
    return [f"{_key(k)} = {_val(v)}" for k, v in d.items()
            if v is not None and not (isinstance(v, Mapping) and k in ("lock", "preflop"))]  # fmt: skip


def to_toml(cfg: Mapping[str, Any], comments: Mapping[int, str] | None = None,
            header: str = "") -> str:  # fmt: skip
    """Serialize a spot config. `comments[i]` is a comment line above player i."""
    out = [f"# {line}" for line in header.splitlines()] if header else []
    out += ["[game]", *_scalars(cfg["game"]), ""]
    for i, p in enumerate(cfg["players"]):
        if comments and i in comments:
            out.append(f"# {comments[i]}")
        out += ["[[players]]", *_scalars(p)]
        if p.get("lock"):
            out += ["[players.lock]", *(f"{_key(k)} = {_val(v)}" for k, v in p["lock"].items())]
        out.append("")
    if cfg.get("leaf_model") is not None:
        out += ["[leaf_model]", *_scalars(cfg["leaf_model"]), ""]
    sizes = cfg.get("sizes")
    if sizes:
        out += ["[sizes]", *_scalars(sizes), ""]
        if sizes.get("preflop"):
            out += ["[sizes.preflop]", *_scalars(sizes["preflop"]), ""]
    return "\n".join(out)
