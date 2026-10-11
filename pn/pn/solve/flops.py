"""Canonical (suit-isomorphic) flops and texture buckets.

Texture rules were ported line for line from the removed Rust tree crate (texture.rs).
Ranks are 0..12 = 2..A. A canonical flop is the lexicographically smallest image of the
three cards under the 24 suit permutations; its weight is the number of raw flops (out of
C(52,3) = 22100) that map to it. There are 1755 canonical flops.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, permutations

RANKS = "23456789TJQKA"
SUITS = "cdhs"
N_RAW_FLOPS = 22100
N_CANONICAL_FLOPS = 1755

RANK_T = 8
RANK_5 = 3
RANK_A = 12
N_STRAIGHTS = 10
CONNECTED_MIN_STRAIGHTS = 2
SUIT_PERMS = tuple(permutations(range(4)))

CLASS_KEYS = ("trips", "paired_high", "paired_low") + tuple(
    f"{s}_{st}"
    for s in ("rainbow", "twotone", "mono")
    for st in ("ahigh", "bway", "connected", "high_dry", "low_dry")
)


def parse_card(card: str) -> tuple[int, int]:
    """'Ah' -> (rank, suit)."""
    if len(card) != 2 or card[0].upper() not in RANKS or card[1].lower() not in SUITS:
        raise ValueError(f"bad card {card!r}")
    return RANKS.index(card[0].upper()), SUITS.index(card[1].lower())


def parse_flop(text: str) -> tuple[tuple[int, int], ...]:
    """'AhKd5c' or 'Ah Kd 5c' -> three (rank, suit) cards."""
    s = text.replace(" ", "").replace(",", "")
    if len(s) != 6:
        raise ValueError(f"flop needs 3 cards: {text!r}")
    cards = tuple(parse_card(s[i : i + 2]) for i in (0, 2, 4))
    if len(set(cards)) != 3:
        raise ValueError(f"duplicate card in {text!r}")
    return cards


def card_str(card: tuple[int, int]) -> str:
    return RANKS[card[0]] + SUITS[card[1]]


def flop_str(cards) -> str:
    """Cards sorted by rank desc then suit, e.g. 'AhKd5c'."""
    return "".join(card_str(c) for c in sorted(cards, key=lambda c: (-c[0], c[1])))


def flop_cards(flop: str) -> list[str]:
    """'AhKd5c' -> ['Ah', 'Kd', '5c'] (the [game] board format)."""
    return [card_str(c) for c in parse_flop(flop)]


# ---------------------------------------------------------------- texture (port of texture.rs)


@dataclass(frozen=True)
class Texture:
    paired: bool
    trips: bool
    suits: str  # mono | twotone | rainbow
    connectivity: int
    high_card: int
    ace_high: bool
    broadway_count: int
    wheel_cards: int


def _straight_ranks(k: int) -> tuple[int, ...]:
    if k == 0:
        return (RANK_A, 0, 1, 2, 3)
    return tuple(range(k - 1, k + 4))


def classify(cards) -> Texture:
    ranks = [c[0] for c in cards]
    distinct = sorted(set(ranks))
    n_suits = len({c[1] for c in cards})
    connectivity = sum(
        1
        for k in range(N_STRAIGHTS)
        if sum(1 for r in distinct if r in _straight_ranks(k)) >= 3
    )
    high = max(ranks)
    return Texture(
        paired=len(distinct) < 3,
        trips=len(distinct) == 1,
        suits={1: "mono", 2: "twotone"}.get(n_suits, "rainbow"),
        connectivity=connectivity,
        high_card=high,
        ace_high=high == RANK_A,
        broadway_count=sum(1 for r in ranks if r >= RANK_T),
        wheel_cards=sum(1 for r in ranks if r <= RANK_5 or r == RANK_A),
    )


def class_key(t: Texture) -> str:
    if t.trips:
        return "trips"
    if t.paired:
        return "paired_high" if t.high_card >= RANK_T else "paired_low"
    if t.broadway_count >= 2:
        structure = "bway"
    elif t.ace_high:
        structure = "ahigh"
    elif t.connectivity >= CONNECTED_MIN_STRAIGHTS:
        structure = "connected"
    elif t.high_card >= RANK_T:
        structure = "high_dry"
    else:
        structure = "low_dry"
    return f"{t.suits}_{structure}"


def bucket_of(flop: str) -> str:
    return class_key(classify(parse_flop(flop)))


# ---------------------------------------------------------------- canonical flops


@dataclass(frozen=True)
class Flop:
    flop: str  # canonical representative, e.g. 'AdKc5c'
    weight: int  # raw flops mapping here (sum over all canonical flops = 22100)
    bucket: str


def canonical_key(cards) -> tuple:
    best = None
    for perm in SUIT_PERMS:
        key = tuple(sorted(((r, perm[s]) for r, s in cards), reverse=True))
        if best is None or key < best:
            best = key
    return best


@lru_cache(maxsize=1)
def canonical_flops() -> tuple[Flop, ...]:
    """All 1755 canonical flops, sorted by rank pattern (high to low), with weights."""
    deck = [(r, s) for r in range(13) for s in range(4)]
    counts: dict[tuple, int] = {}
    for cards in combinations(deck, 3):
        k = canonical_key(cards)
        counts[k] = counts.get(k, 0) + 1
    out = [
        Flop(flop=flop_str(k), weight=w, bucket=class_key(classify(k)))
        for k, w in counts.items()
    ]
    return tuple(sorted(out, key=lambda f: [-c[0] for c in parse_flop(f.flop)] + [f.flop]))


def flops_by_bucket() -> dict[str, list[Flop]]:
    groups: dict[str, list[Flop]] = {k: [] for k in CLASS_KEYS}
    for f in canonical_flops():
        groups[f.bucket].append(f)
    return groups


def _chunks_by_weight(flops: list[Flop], n: int) -> list[list[Flop]]:
    """Split an ordered list into n contiguous chunks of roughly equal total weight."""
    total = sum(f.weight for f in flops)
    chunks: list[list[Flop]] = [[] for _ in range(n)]
    acc = 0
    for f in flops:
        idx = min(n - 1, int(acc * n / total))
        chunks[idx].append(f)
        acc += f.weight
    return [c for c in chunks if c]


def select_flops(n_per_bucket: int | None) -> list[Flop]:
    """Representative subset: per bucket, cut the rank-ordered flop list into n chunks of
    equal raw-flop weight and take each chunk's heaviest flop, carrying the chunk's total
    weight (so weights still sum to 22100). `None` = all 1755 canonical flops."""
    if n_per_bucket is None:
        return list(canonical_flops())
    if n_per_bucket < 1:
        raise ValueError("n_per_bucket must be >= 1")
    out = []
    for flops in flops_by_bucket().values():
        for chunk in _chunks_by_weight(flops, min(n_per_bucket, len(flops))):
            rep = max(chunk, key=lambda f: f.weight)
            out.append(Flop(rep.flop, sum(f.weight for f in chunk), rep.bucket))
    return out


def parse_flop_selection(spec: str) -> list[Flop]:
    """'all' | '<n>-per-bucket' | comma list of flops (weight = raw multiplicity)."""
    s = spec.strip().lower()
    if s == "all":
        return select_flops(None)
    if s.endswith("-per-bucket"):
        return select_flops(int(s.removesuffix("-per-bucket")))
    lookup = {canonical_key(parse_flop(f.flop)): f for f in canonical_flops()}
    return [lookup[canonical_key(parse_flop(x))] for x in spec.split(",") if x.strip()]
