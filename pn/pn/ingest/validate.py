"""Hand-level pot math, per-player sanity, stack continuity and ledger cross-checks.

Ledger tolerance: 0 cents. Buy-ins/top-ups/cash-outs are not P&L, so the sum of hand
nets per player must equal the ledger's net (= buy_out + stack - buy_in) *exactly*
whenever the hands cover the whole session. A non-zero delta therefore means hands are
missing from the log (fetched mid-session, log truncated) or chips moved outside hands
(e.g. nit escrow); both are reported rather than absorbed by a fudge tolerance.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

LEDGER_TOLERANCE_CENTS = 0


def check_hand(hand: dict[str, Any]) -> list[str]:
    """Independent re-check of a parsed hand's money flow."""
    problems = []
    put_in: dict[str, int] = defaultdict(int)
    for a in hand["actions"]:
        put_in[a["player_id"]] += a["amount"]
    returned = sum(u["amount"] for u in hand["uncalled_returned"])
    collected = sum(pot["amount"] for pot in hand["pots"])
    if sum(put_in.values()) - returned != collected:
        problems.append(f"pot: in={sum(put_in.values())} returned={returned} collected={collected}")
    if sum(hand["net"].values()) != 0:
        problems.append(f"net sums to {sum(hand['net'].values())}")
    for p in hand["players"]:
        spent = put_in.get(p["player_id"], 0)
        if spent > p["stack_start"]:
            problems.append(f"{p['player_id']} put in {spent} > stack {p['stack_start']}")
        went_allin = any(a["allin"] and a["player_id"] == p["player_id"] for a in hand["actions"])
        if went_allin and spent != p["stack_start"]:
            problems.append(f"{p['player_id']} all-in but put in {spent} of stack {p['stack_start']}")
    return problems


def stack_continuity(hands: list[dict[str, Any]], events: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """stack_start(next hand) should equal stack_start + net of the player's previous hand.
    Breaks are annotated with session events (rebuy, admin stack change, rejoin) between them."""
    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_player[e["player_id"]].append(e)
    last: dict[str, tuple[dict[str, Any], int]] = {}
    breaks = []
    for hand in sorted(hands, key=lambda h: h["hand_no"]):
        for p in hand["players"]:
            pid = p["player_id"]
            if pid in last:
                prev, expected = last[pid]
                if expected != p["stack_start"]:
                    between = [
                        e["kind"] for e in by_player[pid]
                        if prev["started_at"] < str(e["at"]) <= hand["started_at"]
                    ]
                    breaks.append({
                        "player_id": pid, "from_hand": prev["hand_no"], "to_hand": hand["hand_no"],
                        "expected": expected, "actual": p["stack_start"], "explained_by": between,
                    })
            last[pid] = (hand, p["stack_start"] + hand["net"].get(pid, 0))
    return breaks


def read_ledger(path: str | Path) -> dict[str, dict[str, Any]]:
    """PokerNow ledger CSV (cents) -> {player_id: {name, net, buy_in, rows}}."""
    out: dict[str, dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = row["player_id"]
            entry = out.setdefault(pid, {"name": row["player_nickname"], "net": 0, "buy_in": 0, "rows": 0})
            entry["net"] += int(row["net"] or 0)
            entry["buy_in"] += int(row["buy_in"] or 0)
            entry["rows"] += 1
    return out


def ledger_crosscheck(hands: list[dict[str, Any]], ledger: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    hand_net: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    for h in hands:
        for pid, v in h["net"].items():
            hand_net[pid] += v
        for p in h["players"]:
            names[p["player_id"]] = p["name"]
    rows = []
    for pid in sorted(set(hand_net) | set(ledger)):
        led = ledger.get(pid, {}).get("net")
        delta = None if led is None else led - hand_net.get(pid, 0)
        rows.append({
            "player_id": pid, "name": ledger.get(pid, {}).get("name") or names.get(pid, ""),
            "ledger_net": led, "hands_net": hand_net.get(pid, 0), "delta": delta,
            "ok": delta is not None and abs(delta) <= LEDGER_TOLERANCE_CENTS,
        })
    return rows


def validate_game(
    hands: list[dict[str, Any]],
    ledger: dict[str, dict[str, Any]] | None = None,
    events: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    nos = sorted(h["hand_no"] for h in hands)
    missing = sorted(set(range(nos[0], nos[-1] + 1)) - set(nos)) if nos else []
    hand_problems = {h["hand_id"]: probs for h in hands if (probs := check_hand(h))}
    flagged = {h["hand_id"]: {"hand_no": h["hand_no"], "flags": h["flags"], "issues": h.get("issues", [])}
               for h in hands if {"pot_mismatch", "incomplete"} & set(h["flags"])}
    return {
        "n_hands": len(hands),
        "missing_hand_numbers": missing,
        "flagged": flagged,
        "hand_problems": hand_problems,
        "stack_breaks": stack_continuity(hands, events),
        "ledger": ledger_crosscheck(hands, ledger) if ledger is not None else None,
    }
