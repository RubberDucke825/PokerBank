"""Range text <-> combo weights, per contracts/range_format.md.

Card int = rank*4 + suit (rank 0..12 = 2..A, suit 0..3 = s,h,d,c).
Dense combo index = lexicographic over (a, b) with a < b as ints, identical to the Rust
`cards::combo_index`. Combo string = higher rank first; for pairs the lower suit first.
"""

from __future__ import annotations

from functools import cache, lru_cache

RANKS = "23456789TJQKA"
SUITS = "shdc"
N_COMBOS = 1326


class RangeParseError(ValueError):
    """Raised when range text cannot be parsed."""


def card_to_int(card: str) -> int:
    if len(card) != 2 or card[0] not in RANKS or card[1] not in SUITS:
        raise RangeParseError(f"bad card: {card!r}")
    return RANKS.index(card[0]) * 4 + SUITS.index(card[1])


def int_to_card(c: int) -> str:
    return RANKS[c // 4] + SUITS[c % 4]


def combo_index(c1: int, c2: int) -> int:
    """Dense index 0..1325 of an unordered pair of distinct int cards."""
    a, b = (c1, c2) if c1 < c2 else (c2, c1)
    if a == b or not (0 <= a < 52 and 0 <= b < 52):
        raise ValueError(f"bad combo cards: {c1}, {c2}")
    return a * (103 - a) // 2 + (b - a - 1)


def combo_str_from_ints(c1: int, c2: int) -> str:
    a, b = (c1, c2) if c1 < c2 else (c2, c1)
    if a // 4 == b // 4:  # pair: lower suit first
        return int_to_card(a) + int_to_card(b)
    return int_to_card(b) + int_to_card(a)  # higher rank first


@lru_cache(maxsize=1)
def all_combos() -> tuple[str, ...]:
    """All 1326 combo strings in dense index order."""
    return tuple(combo_str_from_ints(a, b) for a in range(52) for b in range(a + 1, 52))


def canonical_combo(combo: str) -> str:
    """Normalize a 4-char combo string (any order) to its canonical form."""
    if len(combo) != 4:
        raise RangeParseError(f"bad combo: {combo!r}")
    c1, c2 = card_to_int(combo[:2]), card_to_int(combo[2:])
    if c1 == c2:
        raise RangeParseError(f"duplicate card in combo: {combo!r}")
    return combo_str_from_ints(c1, c2)


def combo_str_index(combo: str) -> int:
    return combo_index(card_to_int(combo[:2]), card_to_int(combo[2:]))


# ---------------------------------------------------------------- hand classes (169)


@lru_cache(maxsize=1)
def all_classes() -> tuple[str, ...]:
    """169 hand classes: pairs high->low, then for each high rank suited/offsuit by kicker."""
    out: list[str] = [r + r for r in reversed(RANKS)]
    for hi in range(12, 0, -1):
        for lo in range(hi - 1, -1, -1):
            out.append(RANKS[hi] + RANKS[lo] + "s")
            out.append(RANKS[hi] + RANKS[lo] + "o")
    return tuple(out)


@cache
def class_combos(cls: str) -> tuple[str, ...]:
    """Canonical combo strings for a hand class (`AA`, `AKs`, `AKo`, `AK`)."""
    if len(cls) == 2 and cls[0] == cls[1]:
        r = RANKS.index(cls[0])
        cards = [r * 4 + s for s in range(4)]
        return tuple(
            combo_str_from_ints(cards[i], cards[j]) for i in range(4) for j in range(i + 1, 4)
        )
    if len(cls) == 2:
        return class_combos(cls + "s") + class_combos(cls + "o")
    if len(cls) != 3 or cls[2] not in "so" or cls[0] not in RANKS or cls[1] not in RANKS:
        raise RangeParseError(f"bad hand class: {cls!r}")
    r1, r2 = RANKS.index(cls[0]), RANKS.index(cls[1])
    if r1 == r2:
        raise RangeParseError(f"pair cannot be suited/offsuit: {cls!r}")
    suited = cls[2] == "s"
    out = []
    for s1 in range(4):
        for s2 in range(4):
            if (s1 == s2) == suited:
                out.append(combo_str_from_ints(r1 * 4 + s1, r2 * 4 + s2))
    return tuple(out)


def combo_class(combo: str) -> str:
    """Hand class of a combo, e.g. `AhKh` -> `AKs`, `7c7d` -> `77`."""
    c = canonical_combo(combo)
    r1, s1, r2, s2 = c[0], c[1], c[2], c[3]
    if r1 == r2:
        return r1 + r2
    return r1 + r2 + ("s" if s1 == s2 else "o")


def class_n_combos(cls: str) -> int:
    return 6 if cls[0] == cls[1] else (4 if cls.endswith("s") else 12)


# ---------------------------------------------------------------- parsing


def _norm_class(tok: str) -> str:
    if len(tok) == 2 and tok[0] in RANKS and tok[1] in RANKS:
        hi, lo = sorted(tok, key=RANKS.index, reverse=True)
        return hi + lo
    if len(tok) == 3 and tok[0] in RANKS and tok[1] in RANKS and tok[2] in "so":
        hi, lo = sorted(tok[:2], key=RANKS.index, reverse=True)
        return hi + lo + tok[2]
    raise RangeParseError(f"bad hand class: {tok!r}")


def _expand_plus(cls: str) -> list[str]:
    r1, r2 = RANKS.index(cls[0]), RANKS.index(cls[1])
    suffix = cls[2:]
    if r1 == r2:
        return [RANKS[r] * 2 for r in range(r1, 13)]
    return [cls[0] + RANKS[k] + suffix for k in range(r2, r1)]


def _expand_dash(a: str, b: str) -> list[str]:
    a, b = _norm_class(a), _norm_class(b)
    if a[2:] != b[2:]:
        raise RangeParseError(f"mismatched suitedness in {a}-{b}")
    suffix = a[2:]
    a1, a2, b1, b2 = (RANKS.index(x) for x in (a[0], a[1], b[0], b[1]))
    if a1 == a2 and b1 == b2:
        lo, hi = sorted((a1, b1))
        return [RANKS[r] * 2 for r in range(lo, hi + 1)]
    if a1 == b1:
        lo, hi = sorted((a2, b2))
        return [a[0] + RANKS[k] + suffix for k in range(lo, hi + 1)]
    if a1 - a2 == b1 - b2:
        lo, hi = sorted((a1, b1))
        gap = a1 - a2
        return [RANKS[r] + RANKS[r - gap] + suffix for r in range(lo, hi + 1)]
    raise RangeParseError(f"unsupported range {a}-{b}")


def _expand_token(tok: str) -> list[str]:
    """Expand one token (without weight) to canonical combo strings."""
    if len(tok) == 4 and tok[1] in SUITS and tok[3] in SUITS:
        return [canonical_combo(tok)]
    if "-" in tok:
        a, b = tok.split("-", 1)
        classes = _expand_dash(a, b)
    elif tok.endswith("+"):
        classes = _expand_plus(_norm_class(tok[:-1]))
    else:
        classes = [_norm_class(tok)]
    combos: list[str] = []
    for c in classes:
        combos.extend(class_combos(c))
    return combos


def parse(text: str) -> dict[str, float]:
    """Parse range text to {combo: weight}. Zero-weight combos are omitted."""
    out: dict[str, float] = {}
    cleaned = "".join(text.split())
    if not cleaned:
        return out
    for raw in cleaned.split(","):
        if not raw:
            continue
        tok, _, wtxt = raw.partition(":")
        try:
            w = float(wtxt) if wtxt else 1.0
        except ValueError as e:
            raise RangeParseError(f"bad weight in {raw!r}") from e
        if not 0.0 <= w <= 1.0:
            raise RangeParseError(f"weight out of [0,1] in {raw!r}")
        for combo in _expand_token(tok):
            out[combo] = w
    return {c: w for c, w in out.items() if w > 0.0}


def to_dense(d: dict[str, float]) -> list[float]:
    dense = [0.0] * N_COMBOS
    for combo, w in d.items():
        dense[combo_str_index(combo)] = float(w)
    return dense


def from_dense(weights: list[float]) -> dict[str, float]:
    combos = all_combos()
    return {combos[i]: float(w) for i, w in enumerate(weights) if w > 0.0}


# ---------------------------------------------------------------- to_text


def _fmt_w(w: float) -> str:
    if abs(w - 1.0) < 1e-9:
        return ""
    return ":" + f"{w:.4f}".rstrip("0").rstrip(".")


def _class_weight(d: dict[str, float], cls: str) -> float | None:
    """Uniform weight of a fully-present class, else None."""
    ws = {round(d.get(c, 0.0), 6) for c in class_combos(cls)}
    if len(ws) == 1:
        w = ws.pop()
        return w if w > 0 else None
    return None


def _compress_run(names: list[str], top: str) -> str:
    """names is a contiguous high->low run; emit `X+` when it starts at `top`."""
    if len(names) == 1:
        return names[0]
    if names[0] == top:
        return names[-1] + "+"
    return f"{names[0]}-{names[-1]}"


def to_text(d: dict[str, float]) -> str:
    """Compress {combo: weight} to range text (pairs runs, kicker runs, then single combos)."""
    d = {canonical_combo(c): w for c, w in d.items() if w > 0}
    parts: list[str] = []
    covered: set[str] = set()

    def flush(run: list[str], w: float, top: str) -> None:
        if run:
            parts.append(_compress_run(run, top) + _fmt_w(w))

    # pairs
    run: list[str] = []
    run_w = -1.0
    for r in range(12, -1, -1):
        cls = RANKS[r] * 2
        w = _class_weight(d, cls)
        if w is not None and (not run or abs(w - run_w) < 1e-9):
            run.append(cls)
            run_w = w
            covered.update(class_combos(cls))
            continue
        flush(run, run_w, "AA")
        run, run_w = ([cls], w) if w is not None else ([], -1.0)
        if w is not None:
            covered.update(class_combos(cls))
    flush(run, run_w, "AA")

    # non-pairs, per high card and suitedness, kicker runs
    for hi in range(12, 0, -1):
        for suffix in ("s", "o"):
            run, run_w = [], -1.0
            top = RANKS[hi] + RANKS[hi - 1] + suffix
            for lo in range(hi - 1, -1, -1):
                cls = RANKS[hi] + RANKS[lo] + suffix
                w = _class_weight(d, cls)
                if w is not None and (not run or abs(w - run_w) < 1e-9):
                    run.append(cls)
                    run_w = w
                    covered.update(class_combos(cls))
                    continue
                flush(run, run_w, top)
                run, run_w = ([cls], w) if w is not None else ([], -1.0)
                if w is not None:
                    covered.update(class_combos(cls))
            flush(run, run_w, top)

    for combo in all_combos():
        if combo in d and combo not in covered:
            parts.append(combo + _fmt_w(d[combo]))
    return ",".join(parts)


def width(d: dict[str, float]) -> float:
    """Fraction of all 1326 combos (weighted)."""
    return sum(d.values()) / N_COMBOS
