"""Rich renderables for CLI output (hand replay, summaries)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text

_SUIT_STYLE = {"s": "white", "h": "red", "d": "blue", "c": "green"}
_SUIT_GLYPH = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
_VERB = {
    "post_sb": "posts SB", "post_bb": "posts BB", "post_ante": "posts ante", "post_straddle": "straddles",
    "post_missing_sb": "posts dead SB", "post_missed_bb": "posts missed BB", "fold": "folds",
    "check": "checks", "call": "calls", "bet": "bets", "raise": "raises to",
}


def money(cents: int | None) -> str:
    return "" if cents is None else f"{cents / 100:,.2f}"


def cards_text(cards: list[str] | None) -> Text:
    out = Text()
    for c in cards or []:
        out.append(c[0] + _SUIT_GLYPH.get(c[1], c[1]) + " ", style=_SUIT_STYLE.get(c[1], ""))
    return out


def _board_line(board: dict[str, Any], street: str) -> list[str]:
    cards = list(board["flop"])
    if street in ("turn", "river") and board["turn"]:
        cards.append(board["turn"])
    if street == "river" and board["river"]:
        cards.append(board["river"])
    return cards


def hand_replay(hand: dict[str, Any]) -> Group:
    names = {p["player_id"]: p["name"] for p in hand["players"]}
    head = Text(
        f"Hand #{hand['hand_no']}  {hand['hand_id']}  {hand['started_at']}  {hand['variant']} "
        f"{money(hand['blinds']['sb'])}/{money(hand['blinds']['bb'])}",
        style="bold",
    )
    if hand["flags"]:
        head.append("  [" + ", ".join(hand["flags"]) + "]", style="yellow")
    seats = Table(box=None, pad_edge=False)
    for col in ("seat", "pos", "player", "stack", "cards", "net"):
        seats.add_column(col, justify="right" if col in ("stack", "net", "seat") else "left")
    for p in hand["players"]:
        net = hand["net"].get(p["player_id"], 0)
        seats.add_row(
            str(p["seat"]), p["position"] or "", f"{p['name']} ({p['player_id']})", money(p["stack_start"]),
            cards_text(p["cards"]), Text(money(net), style="green" if net > 0 else "red" if net < 0 else ""),
        )
    lines: list[Any] = [head, seats]
    by_street: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in hand["actions"]:
        by_street[a["street"]].append(a)
    for street in ("preflop", "flop", "turn", "river"):
        board = _board_line(hand["board"], street) if street != "preflop" else []
        if street != "preflop" and not board and not by_street[street]:
            continue
        title = Text(f"\n{street.upper()} ", style="bold cyan")
        title.append_text(cards_text(board))
        lines.append(title)
        for a in by_street[street]:
            amt = f" {money(a['to_amount'] if a['kind'] in ('call', 'bet', 'raise') else a['amount'])}"
            amt = "" if a["kind"] in ("fold", "check") else amt
            lines.append(Text(f"  {names.get(a['player_id'], a['player_id'])} {_VERB.get(a['kind'], a['kind'])}{amt}"
                              + (" (all-in)" if a["allin"] else "")))
    for i, run in enumerate(hand["runs"][1:], start=2):
        t = Text(f"\nRUN {i} ", style="bold cyan")
        t.append_text(cards_text(_board_line(run["board"], "river")))
        lines.append(t)
    for u in hand["uncalled_returned"]:
        lines.append(Text(f"  uncalled {money(u['amount'])} returned to {names.get(u['player_id'])}", style="dim"))
    for s in hand["showdowns"]:
        t = Text(f"  {names.get(s['player_id'])} {'shows' if s['shown'] else 'has'} ")
        t.append_text(cards_text(s["cards"]))
        lines.append(t)
    for i, pot in enumerate(hand["pots"]):
        label = "pot" if len(hand["pots"]) == 1 else ("main pot" if i == 0 else f"side pot {i}")
        for w in pot["winners"]:
            desc = f" with {w['hand_desc']}" if w.get("hand_desc") else ""
            run = f" (run {w['run']})" if w.get("run") else ""
            lines.append(Text(f"  {names.get(w['player_id'])} wins {money(w['amount'])} from {label}{desc}{run}",
                              style="green"))
    for issue in hand.get("issues", []):
        lines.append(Text(f"  ! {issue}", style="red"))
    return Group(*lines)
