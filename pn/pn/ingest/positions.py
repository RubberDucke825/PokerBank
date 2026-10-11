"""Position assignment from button seat + dealt-in seats (contracts/README.md)."""

from __future__ import annotations

_BACK = ("BTN", "CO", "HJ", "LJ")  # filled from the button backwards
_FRONT = ("UTG", "UTG1", "UTG2", "UTG3")  # extra early seats beyond 4 non-blind players


def _non_blind_names(k: int) -> list[str]:
    """Names for k non-blind players in preflop action order (last = BTN).

    k=4 -> LJ,HJ,CO,BTN (6-max); k=6 -> UTG,UTG1,LJ,HJ,CO,BTN (8-max);
    k=7 -> UTG,UTG1,UTG2,LJ,HJ,CO,BTN (9-max).
    """
    if k <= len(_BACK):
        return list(reversed(_BACK[:k]))
    extra = k - len(_BACK)
    front = list(_FRONT[:extra]) + [f"UTG{i}" for i in range(len(_FRONT), extra)]
    return front + list(reversed(_BACK))


def _cyclic_after(seats: list[int], start: int) -> list[int]:
    """Seats in clockwise order beginning with the first seat strictly after `start`."""
    ordered = sorted(seats)
    after = [s for s in ordered if s > start]
    return after + [s for s in ordered if s <= start]


def assign_positions(
    seats: list[int],
    button_seat: int | None,
    sb_seat: int | None,
    bb_seat: int | None,
    straddle_seat: int | None = None,
) -> dict[int, str]:
    """Map seat -> position name for the players dealt into a hand.

    `sb_seat`/`bb_seat` are the seats that actually posted the small/big blind
    (None for a dead small blind). Heads-up the button is labelled BTN (it also posts
    the SB) and the other player BB. With a dead button the last non-blind player to
    act preflop is labelled BTN.
    """
    if not seats:
        return {}
    if len(seats) == 1:
        return {seats[0]: "BTN"}
    anchor = button_seat
    if anchor is None:
        first_blind = sb_seat if sb_seat is not None else bb_seat
        anchor = first_blind - 1 if first_blind is not None else min(seats) - 1
    order = _cyclic_after(seats, anchor)
    if len(seats) == 2:
        btn = button_seat if button_seat in seats else (sb_seat if sb_seat in seats else order[1])
        other = next(s for s in seats if s != btn)
        return {btn: "BTN", other: "BB"}
    if bb_seat not in seats:
        sb_seat, bb_seat = order[0], order[1]
    blinds = {bb_seat: "BB"}
    if sb_seat in seats and sb_seat != bb_seat:
        blinds[sb_seat] = "SB"
    rest = [s for s in _cyclic_after(seats, bb_seat) if s not in blinds]
    result = dict(blinds)
    if straddle_seat in rest:
        result[straddle_seat] = "STR"
        rest.remove(straddle_seat)
    result.update(zip(rest, _non_blind_names(len(rest)), strict=True))
    return result
