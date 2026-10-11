"""Accumulate the events of one hand and emit the normalized hand dict.

The builder is a short-lived mutable accumulator; `finalize()` returns plain dicts
(contracts/hand_history.md) and never mutates them afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pn.ingest.lines import Event, Player
from pn.ingest.positions import assign_positions
from pn.ingest.pots import assign_pots

log = logging.getLogger(__name__)

STREETS = ("preflop", "flop", "turn", "river")
_LIVE_POSTS = frozenset({"post_sb", "post_bb", "post_missed_bb", "post_straddle"})
_DEAD_POSTS = frozenset({"post_missing_sb", "post_ante"})
_VARIANTS = {"No Limit Texas Hold'em": "NLHE", "Pot Limit Omaha Hi": "PLO"}


def _empty_board() -> dict[str, Any]:
    return {"flop": [], "turn": None, "river": None}


def _board_from_cards(cards: tuple[str, ...]) -> dict[str, Any]:
    return {
        "flop": list(cards[:3]),
        "turn": cards[3] if len(cards) > 3 else None,
        "river": cards[4] if len(cards) > 4 else None,
    }


def _board_cards(board: dict[str, Any]) -> list[str]:
    return [*board["flop"], *([board["turn"]] if board["turn"] else []),
            *([board["river"]] if board["river"] else [])]


@dataclass
class Config:
    """Game-level blind configuration tracked across hands (cents)."""

    sb: int | None = None
    bb: int | None = None
    ante: int = 0


@dataclass
class HandBuilder:
    hand_no: int
    hand_id: str
    started_at: str
    variant: str
    dealer: Player | None
    dead_button: bool
    config: Config
    seats: dict[str, tuple[int, str, int]] = field(default_factory=dict)  # pid -> seat,name,stack
    street: str = "preflop"
    committed: dict[str, int] = field(default_factory=dict)
    contributed: dict[str, int] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    folded: set[str] = field(default_factory=set)
    boards: dict[int, tuple[str, ...]] = field(default_factory=dict)
    shown: dict[str, list[str]] = field(default_factory=dict)
    collected: list[Event] = field(default_factory=list)
    returned: dict[str, int] = field(default_factory=dict)
    bounties: list[dict[str, Any]] = field(default_factory=list)
    your_hand: tuple[str, ...] = ()
    posters: dict[str, str] = field(default_factory=dict)  # kind -> pid (first poster)
    post_amounts: dict[str, int] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    issues: list[str] = field(default_factory=list)
    ended: bool = False
    has_stacks: bool = False

    # ------------------------------------------------------------------ events
    def apply(self, ev: Event, at: str) -> None:
        handler = getattr(self, f"_on_{ev.kind}", None)
        if ev.kind in _LIVE_POSTS or ev.kind in _DEAD_POSTS:
            handler = self._on_post
        elif ev.kind in ("call", "bet", "raise"):
            handler = self._on_wager
        if handler is None:
            log.debug("hand %s: ignoring %s event", self.hand_no, ev.kind)
            return
        handler(ev, at)

    def _on_stacks(self, ev: Event, at: str) -> None:
        self.has_stacks = True
        for seat, player, stack in ev.data["seats"]:
            self.seats[player.player_id] = (seat, player.name, stack)

    def _check_player(self, player: Player | None) -> str:
        assert player is not None
        pid = player.player_id
        if pid not in self.seats:
            self.issues.append(f"player {player.name} @ {pid} acted but is not in Player stacks")
            self.flags.add("incomplete")
            self.seats[pid] = (0, player.name, 0)
        return pid

    def _record(self, ev: Event, at: str, pid: str, kind: str, amount: int) -> None:
        self.contributed[pid] = self.contributed.get(pid, 0) + amount
        self.actions.append({
            "street": self.street, "seq": len(self.actions), "player_id": pid, "kind": kind,
            "amount": amount, "to_amount": self.committed.get(pid, 0), "allin": ev.allin, "at": at,
        })

    def _on_post(self, ev: Event, at: str) -> None:
        pid = self._check_player(ev.player)
        if ev.kind in _LIVE_POSTS:
            self.committed[pid] = self.committed.get(pid, 0) + ev.amount
        self.posters.setdefault(ev.kind, pid)
        if not ev.allin:
            self.post_amounts.setdefault(ev.kind, ev.amount)
        if ev.kind == "post_straddle":
            self.flags.add("straddle")
        if ev.data.get("bomb_pot"):
            self.flags.add("bomb_pot")
        self._record(ev, at, pid, ev.kind, ev.amount)

    def _on_wager(self, ev: Event, at: str) -> None:
        pid = self._check_player(ev.player)
        before = self.committed.get(pid, 0)
        added = ev.amount - before
        if added < 0:
            self.issues.append(f"{ev.kind} to {ev.amount} below committed {before} by {pid}")
            self.flags.add("incomplete")
            added = 0
        self.committed[pid] = before + added
        self._record(ev, at, pid, ev.kind, added)

    def _on_fold(self, ev: Event, at: str) -> None:
        pid = self._check_player(ev.player)
        self.folded.add(pid)
        self._record(ev, at, pid, "fold", 0)

    def _on_check(self, ev: Event, at: str) -> None:
        pid = self._check_player(ev.player)
        self._record(ev, at, pid, "check", 0)

    def _on_board(self, ev: Event, at: str) -> None:
        run = ev.data["run"]
        if ev.data.get("double_board"):
            self.flags.add("double_board")
        prev = self.boards.get(run, ())
        self.boards[run] = ev.cards if len(ev.cards) >= len(prev) else prev
        if run == 1:
            street = ev.data["street"]
            if STREETS.index(street) > STREETS.index(self.street):
                self.street = street
                self.committed = {}
        else:
            self.flags.add("run_it_twice")

    def _on_shows(self, ev: Event, at: str) -> None:
        pid = self._check_player(ev.player)
        known = self.shown.setdefault(pid, [])
        known.extend(c for c in ev.cards if c not in known)

    def _on_collected(self, ev: Event, at: str) -> None:
        self._check_player(ev.player)
        self.collected.append(ev)

    def _on_uncalled(self, ev: Event, at: str) -> None:
        pid = self._check_player(ev.player)
        self.returned[pid] = self.returned.get(pid, 0) + ev.amount

    def _on_your_hand(self, ev: Event, at: str) -> None:
        self.your_hand = ev.cards

    def _on_bounty(self, ev: Event, at: str) -> None:
        payer = self._check_player(ev.player)
        payee = self._check_player(ev.data["to"])
        self.bounties.append({"from": payer, "to": payee, "amount": ev.amount})

    def _on_dead_sb(self, ev: Event, at: str) -> None:
        self.flags.add("dead_sb")

    def _on_rit(self, ev: Event, at: str) -> None:
        if ev.data.get("agreed"):
            self.flags.add("run_it_twice")

    def _on_hand_end(self, ev: Event, at: str) -> None:
        self.ended = True

    # ---------------------------------------------------------------- finalize
    def _runs(self) -> list[dict[str, Any]]:
        if not self.boards:
            return []
        first = self.boards.get(1, ())
        runs = [{"board": _board_from_cards(first)}]
        if 2 in self.boards:
            second = self.boards[2]
            # A second-run line lists the full board; fill shared streets from run 1 if short.
            merged = second if len(second) >= 5 else first[: 5 - len(second)] + second
            runs.append({"board": _board_from_cards(merged)})
        return runs

    def _hole_cards(self, runs: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, bool]]:
        """Known hole cards per player: shows + cards recovered from winning combinations."""
        known = {pid: list(cards) for pid, cards in self.shown.items()}
        explicit = {pid: True for pid in self.shown}
        for ev in self.collected:
            if not ev.cards or ev.player is None:
                continue
            run_idx = ev.data["run"] - 1
            if run_idx >= len(runs):
                continue
            board = set(_board_cards(runs[run_idx]["board"]))
            if len(board) < 5:
                continue
            pid = ev.player.player_id
            current = known.setdefault(pid, [])
            current.extend(c for c in ev.cards if c not in board and c not in current)
            explicit.setdefault(pid, False)
        for pid, cards in known.items():
            if len(cards) > 2 and self.variant == "NLHE":
                self.issues.append(f"more than two hole cards inferred for {pid}: {cards}")
                known[pid] = list(self.shown.get(pid, cards[:2]))
        return known, explicit

    def _blinds(self) -> dict[str, int]:
        bb = self.post_amounts.get("post_bb") or self.config.bb or 0
        sb = self.post_amounts.get("post_sb") or self.config.sb or bb // 2
        ante = self.post_amounts.get("post_ante") or self.config.ante
        return {"sb": sb, "bb": bb, "ante": ante, "straddle": self.post_amounts.get("post_straddle", 0)}

    def _players(self, hole: dict[str, list[str]], hero_id: str | None) -> tuple[list[dict], int | None]:
        seat_of = {pid: s for pid, (s, _, _) in self.seats.items()}
        button_seat = None
        if self.dealer is not None and self.dealer.player_id in seat_of:
            button_seat = seat_of[self.dealer.player_id]
        positions = assign_positions(
            [s for s in seat_of.values() if s > 0], button_seat,
            seat_of.get(self.posters.get("post_sb", "")), seat_of.get(self.posters.get("post_bb", "")),
            seat_of.get(self.posters.get("post_straddle", "")),
        )
        players = []
        for pid, (seat, name, stack) in sorted(self.seats.items(), key=lambda kv: kv[1][0]):
            cards, source = None, None
            if len(hole.get(pid, [])) >= 2:
                cards, source = hole[pid], "showdown"
            if pid == hero_id and self.your_hand:
                cards, source = list(self.your_hand), "hero_export"
            players.append({
                "seat": seat, "player_id": pid, "name": name, "stack_start": stack,
                "position": positions.get(seat), "cards": cards, "cards_source": source,
            })
        return players, button_seat

    def _net(self) -> dict[str, int]:
        net = {pid: -self.contributed.get(pid, 0) + self.returned.get(pid, 0) for pid in self.seats}
        for ev in self.collected:
            assert ev.player is not None
            net[ev.player.player_id] = net.get(ev.player.player_id, 0) + ev.amount
        for b in self.bounties:
            net[b["from"]] -= b["amount"]
            net[b["to"]] += b["amount"]
        return net

    def _check_pot(self, net: dict[str, int]) -> None:
        put_in = sum(self.contributed.values()) - sum(self.returned.values())
        paid_out = sum(ev.amount for ev in self.collected)
        if put_in != paid_out or sum(net.values()) != 0:
            self.flags.add("pot_mismatch")
            self.issues.append(f"pot_mismatch: contributed-returned={put_in} collected={paid_out}")
        for pid, (_, name, stack) in self.seats.items():
            spent = self.contributed.get(pid, 0)
            if stack and spent > stack:
                self.issues.append(f"{name} @ {pid} put in {spent} > stack_start {stack}")

    def finalize(self, source: dict[str, Any], hero_id: str | None = None) -> dict[str, Any]:
        if not self.ended:
            self.flags.add("incomplete")
            self.issues.append("hand has no '-- ending hand' line")
        if not self.has_stacks:
            self.flags.add("incomplete")
            self.issues.append("hand has no 'Player stacks' line")
        if self.dead_button:
            self.flags.add("dead_button")
        runs = self._runs()
        hole, explicit = self._hole_cards(runs)
        players, button_seat = self._players(hole, hero_id)
        net = self._net()
        self._check_pot(net)
        folded_or_not = {pid for pid in self.seats if pid not in self.folded}
        hand: dict[str, Any] = {
            "hand_id": self.hand_id,
            "hand_no": self.hand_no,
            "source": dict(source),
            "started_at": self.started_at,
            "variant": _VARIANTS.get(self.variant, self.variant),
            "blinds": self._blinds(),
            "button_seat": button_seat,
            "players": players,
            "actions": list(self.actions),
            "board": runs[0]["board"] if runs else _empty_board(),
            "runs": runs,
            "showdowns": [
                {"player_id": pid, "cards": cards, "shown": explicit.get(pid, False),
                 "voluntary": pid in self.folded or len(folded_or_not) < 2}
                for pid, cards in hole.items() if cards
            ],
            "pots": assign_pots(self.contributed, self.returned, self.folded, self.collected),
            "uncalled_returned": [{"player_id": p, "amount": a} for p, a in self.returned.items()],
            "net": net,
            "flags": sorted(self.flags),
            "issues": list(self.issues),
        }
        if self.bounties:
            hand["bounties"] = list(self.bounties)
        if self.your_hand:
            hand["hero_cards"] = list(self.your_hand)
        return hand
