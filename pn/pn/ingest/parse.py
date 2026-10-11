"""PokerNow raw log messages -> normalized hands (contracts/hand_history.md)."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pn.ingest.hand_builder import Config, HandBuilder
from pn.ingest.lines import ACTION_KINDS, Event, classify

log = logging.getLogger(__name__)

# Events that belong to a hand even when logged after its '-- ending hand' line.
_POST_HAND_KINDS = frozenset({"shows", "bounty"})
_HAND_KINDS = ACTION_KINDS | {
    "stacks", "dead_sb", "board", "shows", "collected", "uncalled", "your_hand", "rit",
    "bounty", "hand_end",
}


def _order_key(msg: dict[str, Any]) -> tuple[int, str]:
    raw = str(msg.get("created_at") or "")
    return (int(raw) if raw.isdigit() else 0, str(msg.get("at") or ""))


def sort_messages(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological order, de-duplicated by (created_at, msg)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for m in sorted(messages, key=_order_key):
        key = (str(m.get("created_at")), str(m.get("msg")))
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def _infer_hero(builders: list[HandBuilder]) -> str | None:
    """The player whose shown cards match 'Your hand is' in the same hand."""
    votes: Counter[str] = Counter()
    for b in builders:
        if not b.your_hand:
            continue
        for pid, cards in b.shown.items():
            if set(cards) == set(b.your_hand):
                votes[pid] += 1
    if len(votes) == 1:
        return next(iter(votes))
    if votes:
        (top, n1), (_, n2) = votes.most_common(2)
        return top if n1 > n2 else None
    return None


def _segment(messages: list[dict[str, Any]]) -> list[HandBuilder]:
    config = Config()
    builders: list[HandBuilder] = []
    current: HandBuilder | None = None
    unknown: Counter[str] = Counter()
    for m in messages:
        msg = str(m.get("msg", ""))
        at = str(m.get("at", ""))
        ev: Event | None = classify(msg)
        if ev is None:
            unknown[msg[:60]] += 1
            log.debug("unrecognised log line: %r", msg)
            continue
        if ev.kind == "config":
            setattr(config, ev.data["what"], ev.amount)
            continue
        if ev.kind == "hand_start":
            current = HandBuilder(
                hand_no=ev.data["hand_no"], hand_id=ev.data["hand_id"], started_at=at,
                variant=ev.data["variant"], dealer=ev.player, dead_button=ev.data["dead_button"],
                config=Config(config.sb, config.bb, config.ante),
            )
            if ev.data["bomb_pot"]:
                current.flags.add("bomb_pot")
            builders.append(current)
            continue
        if ev.kind not in _HAND_KINDS:
            continue
        if current is None or (current.ended and ev.kind not in _POST_HAND_KINDS):
            log.debug("hand event outside a hand: %r", msg)
            continue
        if ev.kind == "hand_end" and ev.data["hand_no"] != current.hand_no:
            current.issues.append(f"ending hand #{ev.data['hand_no']} closes hand #{current.hand_no}")
        current.apply(ev, at)
    if unknown:
        log.debug("unrecognised lines: %s", dict(unknown))
    return builders


def parse_messages(
    messages: Iterable[dict[str, Any]],
    *,
    game_id: str,
    source_kind: str = "pokernow_log",
    fetched_at: str | None = None,
    hero_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse raw PokerNow messages (any order) into normalized hand dicts, by hand_no."""
    builders = _segment(sort_messages(messages))
    hero = hero_id or _infer_hero(builders)
    source = {
        "kind": source_kind,
        "game_id": game_id,
        "fetched_at": fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return [b.finalize(source, hero) for b in builders]


def session_events(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Joins/quits/stand-ups/admin stack changes/id changes, chronologically (for validation)."""
    out = []
    for m in sort_messages(messages):
        ev = classify(str(m.get("msg", "")))
        if ev is not None and ev.kind in ("session", "id_change") and ev.player is not None:
            out.append({
                "at": m.get("at"), "created_at": m.get("created_at"), "kind": ev.data.get("what", ev.kind),
                "player_id": ev.player.player_id, "amount": ev.amount,
                **({"old": ev.data["old"], "new": ev.data["new"]} if ev.kind == "id_change" else {}),
            })
    return out
