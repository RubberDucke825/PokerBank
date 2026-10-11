"""Spot requests (contracts/solve_service.md): seats, street state, range source, budget."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal, get_args

from pn.solve.backend import choose_backend

RangeSource = Literal["gto"]  # Plan 7: chart ranges only (no profile ranges, no locks)
SOURCES: tuple[str, ...] = get_args(RangeSource)


@dataclass(frozen=True)
class Budget:
    max_iterations: int = 1000
    time_limit_sec: int = 300
    target_exploitability: float = 0.5
    threads: int = 8

    @staticmethod
    def preset(name: str) -> Budget:
        return solve_preset(name).budget


@dataclass(frozen=True)
class SolvePreset:
    """Named solve settings: budget, the max_bets_per_street cap and the number of flop bet
    sizes (the first of the sizing table's) written into heads-up postflop spots (None = keep
    the spot's)."""

    name: str
    budget: Budget
    hu_max_bets: int | None = None
    hu_flop_sizes: int | None = None


# Plan 7 (contracts/texas_backend.md): TexasSolver has no time cap, so a preset stops by
# iterations and accuracy (% of pot); time_limit_sec only sizes the watchdog
# (time_limit_sec * 1.5 + 30 s, pn.solve.texas). Plan 10: the library tree is one flop size
# and two bets per street, the only tree that solves in RAM on the 8 GB machine (measured
# table in contracts/texas_backend.md).
SOLVE_PRESETS: dict[str, SolvePreset] = {
    "trainer": SolvePreset("trainer", Budget(max_iterations=300, time_limit_sec=10,
                                             target_exploitability=0.5)),
    "review": SolvePreset("review", Budget(max_iterations=600, time_limit_sec=120,
                                           target_exploitability=0.3), 2),
    "library": SolvePreset("library", Budget(max_iterations=400, time_limit_sec=3600,
                                             target_exploitability=0.3), 2, 1),
}


def solve_preset(name: str) -> SolvePreset:
    if name not in SOLVE_PRESETS:
        raise ValueError(f"preset must be one of {sorted(SOLVE_PRESETS)}, got {name!r}")
    return SOLVE_PRESETS[name]


def apply_preset(spot: Mapping[str, Any], name: str) -> tuple[dict[str, Any], str]:
    """(spot with the preset's budget and HU cap written into the TOML fields, backend).
    The backend is choose_backend's (SolveError for spots no backend solves). The cap lives
    in [sizes] so spot_key tells presets apart."""
    p = solve_preset(name)
    b = p.budget
    backend = choose_backend(spot)
    game = {**spot["game"], "max_iterations": b.max_iterations, "time_limit_sec": b.time_limit_sec,
            "target_exploitability": b.target_exploitability, "threads": b.threads}  # fmt: skip
    sizes = dict(spot.get("sizes") or {})
    if p.hu_max_bets is not None:
        sizes["max_bets_per_street"] = p.hu_max_bets
    if p.hu_flop_sizes is not None:
        from pn.solve.texas import resolve_sizes

        sizes["flop_bets"] = list(resolve_sizes({**spot, "sizes": sizes})[0]["flop_bets"])[: p.hu_flop_sizes]
    return {**spot, "game": game, "sizes": sizes}, backend


@dataclass(frozen=True)
class Seat:
    player_id: str | None  # None = anonymous GTO seat
    position: str  # contracts/README.md position names
    stack: int  # cents behind at the start of `street`
    hero: bool = False
    name: str | None = None  # config player name (default: player_id, else position)
    line: str | None = None  # preflop line action (pn.adapter.state.Line.action)
    vs: str | None = None  # the seat the line was taken against
    range: str | None = None  # explicit range text: bypasses the source (subgames, --range)
    folded: bool = False  # preflop only: already folded (a BB kept listed for the tree), locked to fold

    @property
    def key(self) -> str:
        """Override lookup key: player id, or position for anonymous seats."""
        return self.player_id or self.position


@dataclass(frozen=True)
class SpotRequest:
    seats: tuple[Seat, ...]  # sorted into `street` action order by build_spot
    street: str
    board: tuple[str, ...]
    pot: int
    sb: int
    bb: int
    ante: int = 0
    straddle: int = 0
    source: RangeSource = "gto"
    overrides: Mapping[str, str] = field(default_factory=dict)  # seat key -> range text
    last_aggressor: str | None = None  # position
    budget: Budget = Budget()
    table: str = "6max"  # chart table kind (pn.profile.charts.table_kind)
    mode: str = "depth_limited"
    sizes: Mapping[str, Any] | None = None  # merged over the default [sizes]
    hero_cards: tuple[str, ...] = ()  # dead for every villain range

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {self.source!r}")


# ------------------------------------------------------------------ `pn solve new`

TABLE_SEATS = {
    6: ("LJ", "HJ", "CO", "BTN", "SB", "BB"),
    8: ("UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"),
}
STREET_BY_BOARD = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
LINES = ("srp", "3bp")
BB_CENTS = 100
OPEN_PCT = 300  # tree default open: 3bb
THREE_BET_IP_PCT, THREE_BET_OOP_PCT = 300, 350
# Preflop tree knobs (contracts/tree_config.md [sizes.preflop]) that keep full-table roots under
# 5 GB: tree-size 0.39 GB for 6-max, 0.82 GB for 8-max at 100bb.
ROOT_KNOBS: dict[int, dict[str, Any]] = {
    6: {"limp": "sb_only", "cold_call_3bet": False},
    8: {"limp": "sb_only", "cold_call_3bet": False, "max_callers": 2},
}
MULTIWAY_KNOB_MIN = 4  # preflop-start spots with more than 3 players get the knobs
MAX_CALLERS_UNLIMITED = 8  # explicit "no cap" (max 8 players)


def preflop_knobs(n_players: int) -> dict[str, Any]:
    """Default [sizes.preflop] knobs for a preflop-start spot with `n_players` listed."""
    if n_players < MULTIWAY_KNOB_MIN:
        return {}
    return dict(ROOT_KNOBS[6 if n_players <= 6 else 8])


POSTFLOP_SEAT_ORDER = ("SB", "BB", "UTG", "UTG1", "UTG2", "LJ", "HJ", "CO", "BTN")


def parse_board(text: str | None) -> tuple[str, ...]:
    """'Kd2cKh' / 'Kd 2c Kh' / 'Kd,2c,Kh' -> ('Kd', '2c', 'Kh')."""
    s = (text or "").replace(" ", "").replace(",", "")
    if len(s) % 2:
        raise ValueError(f"bad board {text!r}")
    return tuple(s[i].upper() + s[i + 1].lower() for i in range(0, len(s), 2))


def new_request(positions: list[str], depth_bb: int, *, table: int = 6,
                source: RangeSource = "gto", board: tuple[str, ...] = (),
                hero: str | None = None, line: str | None = None,
                seat_ranges: Mapping[str, str] | None = None) -> SpotRequest:  # fmt: skip
    """Anonymous-seat spot (hero defaults to the first listed seat). Preflop: ROOT_KNOBS, blinds posted, everyone any-two (seat_ranges replace).
    Postflop needs `line`: srp (first seat in preflop order opens 3bb, the others call) or 3bp
    (heads-up: the opener calls the other seat's 3-bet, 3x IP / 3.5x OOP); pot and stacks
    follow from it, and seats get that line's chart ranges unless seat_ranges name them."""
    if table not in TABLE_SEATS:
        raise ValueError(f"table must be one of {sorted(TABLE_SEATS)}")
    order = TABLE_SEATS[table]
    bad = [p for p in positions if p not in order]
    if bad or len(set(positions)) != len(positions) or len(positions) < 2:
        raise ValueError(f"seats must be 2+ distinct positions of a {table}-max table {order}: "
                         f"{positions}")  # fmt: skip
    hero = hero or (positions[0] if positions else None)
    if hero not in positions:
        raise ValueError(f"hero {hero} is not a seat")
    street = STREET_BY_BOARD.get(len(board))
    if street is None:
        raise ValueError(f"board must have 0, 3, 4 or 5 cards: {list(board)}")
    seats_pf = sorted(positions, key=order.index)
    sb, bb, stack = BB_CENTS // 2, BB_CENTS, depth_bb * BB_CENTS
    explicit = dict(seat_ranges or {})
    unknown = set(explicit) - set(positions)
    if unknown:
        raise ValueError(f"--range for positions that are not seats: {sorted(unknown)}")
    put: dict[str, int]
    lines: dict[str, tuple[str, str | None]] = {}
    aggressor = None
    if street == "preflop":
        if line is not None:
            raise ValueError("--line applies to postflop spots only")
        put = {p: {"SB": sb, "BB": bb}.get(p, 0) for p in positions}
        sizes = {"preflop": dict(ROOT_KNOBS[table])}
    elif line == "srp":
        opener = next((p for p in seats_pf if p != "BB"), seats_pf[0])
        to = bb * OPEN_PCT // 100
        put = dict.fromkeys(positions, to)
        lines = {p: ("open", None) if p == opener else ("call", opener) for p in positions}
        aggressor = opener
    elif line == "3bp":
        if len(positions) != 2:
            raise ValueError("--line 3bp is heads-up only")
        opener, three = (seats_pf if seats_pf[0] != "BB" else seats_pf[::-1])
        ip = POSTFLOP_SEAT_ORDER.index(three) > POSTFLOP_SEAT_ORDER.index(opener)
        to = bb * OPEN_PCT // 100 * (THREE_BET_IP_PCT if ip else THREE_BET_OOP_PCT) // 100
        put = dict.fromkeys(positions, to)
        lines = {opener: ("call_3bet", three), three: ("3bet", opener)}
        aggressor = three
    else:
        raise ValueError(f"postflop spots need --line {'|'.join(LINES)}")
    if street != "preflop":
        sizes = None
    dead = sum(v for p, v in (("SB", sb), ("BB", bb)) if p not in positions)
    seats = tuple(
        Seat(None, p, stack - put[p], hero=p == hero, line=lines.get(p, (None, None))[0],
             vs=lines.get(p, (None, None))[1], range=explicit.get(p))
        for p in positions
    )  # fmt: skip
    return SpotRequest(seats=seats, street=street, board=board, pot=sum(put.values()) + dead,
                       sb=sb, bb=bb, source=source, last_aggressor=aggressor,
                       table=f"{table}max", sizes=sizes)  # fmt: skip


# ------------------------------------------------------------------ JSON round trip


def request_to_json(req: SpotRequest) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(req)))


def request_from_json(d: Mapping[str, Any]) -> SpotRequest:
    """Requests saved before Plan 7 may carry `lock_overrides` or another source: both are
    dropped (a subgame of such a document is solved with GTO ranges)."""
    known = {f.name for f in fields(SpotRequest)}
    d = {k: v for k, v in d.items() if k in known} | {"source": "gto"}
    return SpotRequest(**{**d, "seats": tuple(Seat(**s) for s in d["seats"]),
                          "board": tuple(d["board"]), "budget": Budget(**d["budget"]),
                          "hero_cards": tuple(d.get("hero_cards", ()))})  # fmt: skip
