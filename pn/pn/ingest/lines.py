"""Classify single PokerNow log messages into typed events.

Pure functions, no hand state. Unknown messages classify as ``None`` so the caller
can log them at debug level and move on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SUIT_MAP = {"♠": "s", "♥": "h", "♦": "d", "♣": "c", "s": "s", "h": "h", "d": "d", "c": "c"}
_CARD_RE = re.compile(r"(10|[2-9TJQKA])\s*([♠♥♦♣])|(?<![A-Za-z])(10|[2-9TJQKA])([shdc])(?![a-z])")

_P = r'"(?P<p>.+)"'
_AMT = r"(?P<amt>\d+(?:\.\d+)?)"
_ALLIN = r"(?P<allin> and go all in)?"

POST_KINDS = {
    "small blind": "post_sb",
    "big blind": "post_bb",
    "missing small blind": "post_missing_sb",
    "missed small blind": "post_missing_sb",
    "missed big blind": "post_missed_bb",
    "missing big blind": "post_missed_bb",
    "straddle": "post_straddle",
    "an ante": "post_ante",
}

# Kinds of player actions that go into the contract `actions` list.
ACTION_KINDS = frozenset({"fold", "check", "call", "bet", "raise", *POST_KINDS.values()})


@dataclass(frozen=True)
class Player:
    name: str
    player_id: str


@dataclass(frozen=True)
class Event:
    kind: str
    player: Player | None = None
    amount: int = 0
    allin: bool = False
    cards: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)


def to_cents(text: str) -> int:
    """'0.25' -> 25, '426.06' -> 42606, '20' -> 2000. Exact decimal arithmetic."""
    try:
        return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError(f"bad amount {text!r}") from exc


def norm_card(rank: str, suit: str) -> str:
    return ("T" if rank == "10" else rank) + SUIT_MAP[suit]


def parse_cards(text: str) -> tuple[str, ...]:
    """Extract every card in order: 'K♥, 10♣ [A♠]' -> ('Kh', 'Tc', 'As')."""
    out = []
    for m in _CARD_RE.finditer(text):
        rank, suit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out.append(norm_card(rank, suit))
    return tuple(out)


def parse_player(text: str) -> Player:
    """'harris @ MzdlOZiJWw' -> Player('harris', 'MzdlOZiJWw'). Ids never contain ' @ '."""
    name, sep, pid = text.rpartition(" @ ")
    if not sep:
        return Player(text, text)
    return Player(name, pid)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.DOTALL)


_START = _rx(r"^-- starting hand #(?P<no>\d+) \(id: (?P<hid>[^)]+)\)\s*(?P<rest>.*?)\s*--$")
_DEALER = re.compile(r'\(dealer: "(?P<p>.+)"\)')
_END = _rx(r"^-- ending hand #(?P<no>\d+) --$")
_STACKS = _rx(r"^Player stacks: (?P<rest>.*)$")
_STACK_SEG = re.compile(r'^#(?P<seat>\d+) "(?P<p>.+)" \((?P<amt>\d+(?:\.\d+)?)\)$')
_POST = _rx(
    _P + r" posts (?P<what>a small blind|a big blind|a missing small blind|a missed small blind"
    r"|a missed big blind|a missing big blind|a straddle|an ante) of " + _AMT + _ALLIN + r"\s*$"
)
_BOMB = _rx(_P + r" (?:posts a bet of|calls) " + _AMT + _ALLIN + r" \(bomb pot bet\)$")
_FOLD_CHECK = _rx(_P + r" (?P<verb>folds|checks)$")
_BET = _rx(_P + r" (?P<verb>calls|bets|raises to) " + _AMT + _ALLIN + r"$")
_BOARD = _rx(
    r"^(?P<street>Flop|Turn|River)(?: \((?P<run>second run|second board)\))?:\s*(?P<cards>.*)$"
)
_SHOWS = _rx(_P + r" shows a (?P<cards>.+?)\.?$")
_COLLECTED = _rx(
    _P + r" collected " + _AMT + r" from pot(?: with (?P<desc>.+?))?(?P<run2>\s+on the second (?:run|board))?"
    r"(?:\s*\(combination: (?P<combo>[^)]*)\))?\s*$"
)
_UNCALLED = _rx(r"^Uncalled bet of " + _AMT + r' returned to "(?P<p>.+)"$')
_YOUR_HAND = _rx(r"^Your hand is (?P<cards>.+)$")
_CONFIG = _rx(
    r"^The game's (?P<what>small blind|big blind|ante) was changed from "
    r"(?P<old>\d+(?:\.\d+)?) to " + _AMT + r"\.?$"
)
_ID_CHANGE = _rx(
    r'^The player "(?P<p>.+)" changed the ID from (?P<old>\S+) to (?P<new>\S+)'
    r" because authenticated login\.?$"
)
_BOUNTY = _rx(_P + r" paid " + _AMT + r' for the .+? bounty to "(?P<p2>.+)"\.?$')
_ADMIN_STACK = _rx(
    r'^The admin updated the player "(?P<p>.+)" stack from (?P<old>\d+(?:\.\d+)?) to ' + _AMT
)
_SESSION = _rx(
    r'^The player "(?P<p>.+)" (?P<what>joined the game|quits the game|stand up|sit back)'
    r" with (?:a|the) stack of " + _AMT
)
_ADMIN_APPROVE = _rx(
    r'^The admin approved the player "(?P<p>.+)" participation with a stack of ' + _AMT
)

# Recognised but irrelevant to hand reconstruction: classify as 'info' (not 'unknown').
_INFO_PREFIXES = (
    "The player ",
    "The admin ",
    "WARNING:",
    "Game Config Changes",
    "Remaining players decide whether to run it twice",
    "The game's ",
)


def _player(m: re.Match[str], group: str = "p") -> Player:
    return parse_player(m.group(group))


def _classify_hand_structure(msg: str) -> Event | None:
    if m := _START.match(msg):
        rest = m.group("rest")
        dealer = _DEALER.search(rest)
        variant = re.sub(r"\s*\((?:dealer: \".+\"|dead button)\)", "", rest).strip()
        return Event(
            "hand_start",
            player=parse_player(dealer.group("p")) if dealer else None,
            data={
                "hand_no": int(m.group("no")),
                "hand_id": m.group("hid"),
                "variant": variant,
                "dead_button": "dead button" in rest,
                "bomb_pot": "bomb pot" in rest.lower(),
            },
        )
    if m := _END.match(msg):
        return Event("hand_end", data={"hand_no": int(m.group("no"))})
    if m := _STACKS.match(msg):
        seats = []
        for seg in m.group("rest").split(" | "):
            sm = _STACK_SEG.match(seg.strip())
            if sm:
                seats.append((int(sm.group("seat")), parse_player(sm.group("p")), to_cents(sm.group("amt"))))
        return Event("stacks", data={"seats": tuple(seats)})
    if msg == "Dead Small Blind":
        return Event("dead_sb")
    return None


def _classify_action(msg: str) -> Event | None:
    if m := _BOMB.match(msg):
        return Event(
            "post_ante", _player(m), to_cents(m.group("amt")), bool(m.group("allin")),
            data={"bomb_pot": True},
        )
    if m := _POST.match(msg):
        what = m.group("what").removeprefix("a ")
        return Event(POST_KINDS[what], _player(m), to_cents(m.group("amt")), bool(m.group("allin")))
    if m := _FOLD_CHECK.match(msg):
        return Event("fold" if m.group("verb") == "folds" else "check", _player(m))
    if m := _BET.match(msg):
        kind = {"calls": "call", "bets": "bet", "raises to": "raise"}[m.group("verb")]
        return Event(kind, _player(m), to_cents(m.group("amt")), bool(m.group("allin")))
    return None


def _classify_showdown(msg: str) -> Event | None:
    if m := _BOARD.match(msg):
        run = 2 if m.group("run") else 1
        return Event(
            "board", cards=parse_cards(m.group("cards")),
            data={"street": m.group("street").lower(), "run": run,
                  "double_board": m.group("run") == "second board"},
        )
    if m := _COLLECTED.match(msg):
        desc = m.group("desc")
        run = 2 if m.group("run2") else 1
        combo = parse_cards(m.group("combo")) if m.group("combo") else ()
        return Event(
            "collected", _player(m), to_cents(m.group("amt")), cards=combo,
            data={"hand_desc": desc.strip() if desc else None, "run": run},
        )
    if m := _UNCALLED.match(msg):
        return Event("uncalled", _player(m), to_cents(m.group("amt")))
    if m := _SHOWS.match(msg):
        return Event("shows", _player(m), cards=parse_cards(m.group("cards")))
    if m := _YOUR_HAND.match(msg):
        return Event("your_hand", cards=parse_cards(m.group("cards")))
    if "choose to run it twice" in msg or "chooses to  run it twice" in msg:
        return Event("rit", data={"agreed": msg.startswith("All players")})
    if "choose to not run it twice" in msg or "chooses to not run it twice" in msg:
        return Event("rit", data={"agreed": False})
    return None


def _classify_meta(msg: str) -> Event | None:
    if m := _CONFIG.match(msg):
        what = {"small blind": "sb", "big blind": "bb", "ante": "ante"}[m.group("what")]
        return Event("config", amount=to_cents(m.group("amt")), data={"what": what})
    if m := _ID_CHANGE.match(msg):
        return Event("id_change", _player(m), data={"old": m.group("old"), "new": m.group("new")})
    if m := _BOUNTY.match(msg):
        return Event("bounty", _player(m), to_cents(m.group("amt")), data={"to": parse_player(m.group("p2"))})
    if m := _ADMIN_STACK.match(msg):
        return Event("session", _player(m), to_cents(m.group("amt")), data={"what": "admin_stack"})
    if m := _ADMIN_APPROVE.match(msg):
        return Event("session", _player(m), to_cents(m.group("amt")), data={"what": "approved"})
    if m := _SESSION.match(msg):
        what = m.group("what").replace(" the game", "").replace(" ", "_")
        return Event("session", _player(m), to_cents(m.group("amt")), data={"what": what})
    if msg.startswith(_INFO_PREFIXES):
        return Event("info")
    return None


def classify(msg: str) -> Event | None:
    """Return the typed event for one log message, or None if unrecognised."""
    msg = msg.strip()
    for fn in (_classify_hand_structure, _classify_action, _classify_showdown, _classify_meta):
        ev = fn(msg)
        if ev is not None:
            return ev
    return None
