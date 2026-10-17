"""Plan 16 P2: flop library aliases and coverage per table / depth.

An 8-max heads-up flop line on which UTG and UTG1 only fold is the 6-max line of the same depth:
the trainer builds the same flop SpotRequest except its table kind, and spot_key ignores the
table, so every board has the same document (P0: 85 of the 175 8-max matchups per depth). Such a
matchup gets an alias entry in the flop index (`FlopIndex.alias`): the trainer deals and grades
it from the 6-max boards. `run` writes them only after checking the keys end to end (the 8-max
key of the target's indexed boards must equal the recorded document key; probe boards while the
target has none), all or nothing. `coverage` reports solved / aliased / missing matchups per table and depth; limped pots
outside 6-max 100bb are missing on purpose (Plan 16 budget decision: they end at the flop).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pn.solve import flops as fl
from pn.solve.request import TABLE_SEATS
from pn.train.flop_library import FlopIndex, Matchup, flop_spot, matchups

EP = frozenset({"UTG", "UTG1"})  # 8-max seats in front of the 6-max table
PROBES = ("Ks7d2c", "AhQh5h", "7h7c2s")  # any boards: a key hashes ranges, stacks, pot, sizes
LIBS = ((6, 100), (8, 100), (6, 200), (8, 200))
SKIPPED_POTS = ("limped",)  # never solved outside 6-max 100bb (budget decision 2026-10-15)
SB, BB = 50, 100  # cents, as pn.train.session deals
N_FLOPS = 1755  # canonical flops (library_fill.all_flops): a matchup with all of them is full


@dataclass(frozen=True)
class AliasRow:
    matchup: Matchup
    target: str | None  # the 6-max matchup id of the same line
    status: str  # new | present | solved (own boards: left alone) | refused
    detail: str = ""


def lib_matchups(table: int, depth: int, freqs: Mapping | None = None) -> list[Matchup]:
    return matchups(TABLE_SEATS[table], depth, f"{table}max", sb=SB, bb=BB, freqs=freqs)


def ep_acts(m: Matchup) -> bool:
    """UTG or UTG1 does more than fold preflop."""
    st = m.state
    return any(st.positions[a.seat] in EP for a in st.history
               if a.street == "preflop" and a.kind != "fold")  # fmt: skip


def candidates(table: int, depth: int,
               freqs: Mapping | None = None) -> list[tuple[Matchup, Matchup | None]]:  # fmt: skip
    """(matchup, the 6-max matchup of its line or None) for every matchup of the table without
    UTG / UTG1 action."""
    if table == 6:
        raise ValueError("6-max matchups are the alias targets")
    ref = {r.line: r for r in lib_matchups(6, depth, freqs)}
    return [(m, ref.get(m.line)) for m in lib_matchups(table, depth, freqs) if not ep_acts(m)]


def _sampled(boards: Mapping[str, Mapping[str, Any]], sample: int) -> list[tuple[str, Mapping]]:
    """Every k-th indexed board (deterministic), at most `sample` of them; 0 = all."""
    items = sorted(boards.items())
    if sample <= 0 or len(items) <= sample:
        return items
    step = len(items) / sample
    return [items[int(i * step)] for i in range(sample)]


def key_mismatch(m: Matchup, target: Matchup, index: FlopIndex, sample: int = 25) -> str | None:
    """Why `m` may not read `target`'s boards (None: every key checked is identical): `sample`
    indexed boards of the target at their recorded accuracy, where `m`'s key must equal the
    recorded document key; a target without boards yet (a library still to fill) is compared on
    the probe boards instead."""
    boards = _sampled(index.boards(target.mid), sample)
    for b, entry in boards:
        if flop_spot(m, tuple(fl.flop_cards(b)), entry.get("accuracy"))[2] != entry["key"]:
            return f"indexed {b}: key differs from the recorded document"
    for b in () if boards else PROBES:
        cards = tuple(fl.flop_cards(b))
        if flop_spot(m, cards)[2] != flop_spot(target, cards)[2]:
            return f"probe {b}: flop keys differ"
    return None


def run(index: FlopIndex, table: int, depth: int, *, sample: int = 25, dry_run: bool = False,
        freqs: Mapping | None = None) -> list[AliasRow]:  # fmt: skip
    """Check every candidate, then write the new aliases in one index write, unless one is
    refused (no 6-max line, a key differs, or it is aliased elsewhere): then nothing is written.
    A matchup with boards of its own is left alone. Idempotent."""
    rows: list[AliasRow] = []
    entries = index.load()["matchups"]
    for m, t in candidates(table, depth, freqs):
        own = entries.get(m.mid, {})
        if t is None:
            rows.append(AliasRow(m, None, "refused", "no 6-max matchup of this line"))
        elif own.get("boards"):
            rows.append(AliasRow(m, t.mid, "solved", f"{len(own['boards'])} boards of its own"))
        elif (why := key_mismatch(m, t, index, sample)) is not None:
            rows.append(AliasRow(m, t.mid, "refused", why))
        elif own.get("alias_of") not in (None, t.mid):
            rows.append(AliasRow(m, t.mid, "refused", f"aliased to {own['alias_of']}"))
        else:
            rows.append(AliasRow(m, t.mid, "present" if own.get("alias_of") else "new"))
    new = [r for r in rows if r.status == "new"]
    if dry_run or not new or any(r.status == "refused" for r in rows):
        return rows
    with index.batch():
        for r in new:
            index.alias(r.matchup, r.target)  # type: ignore[arg-type]
    return rows


def _fill_state(entry: Mapping[str, Any], boards: int) -> str:
    """full (every canonical flop indexed, or a pre-Plan-16 entry: the wide fill recorded no
    subset), extending (a `--flops all` fill has started: `flops: "all"`, boards missing) or
    subset (filled from the `subset`-flop plan)."""
    if boards >= N_FLOPS:
        return "full"
    if entry.get("flops") == "all":
        return "extending"
    return "subset" if entry.get("subset") is not None else "full"


def coverage(index: FlopIndex, table: int, depth: int,
             freqs: Mapping | None = None) -> dict[str, Any]:  # fmt: skip
    """{table, depth, matchups, solved, subset, extending, full, aliased, aliased_with_boards,
    missing, skipped, rows}: a matchup is solved (boards of its own; its `fill` is subset,
    extending or full: _fill_state), aliased, or missing; a limped pot outside 6-max
    100bb that is missing, or aliased to a 6-max limped line of another depth without boards, is
    `skipped` (intentionally never solved)."""
    entries = index.load()["matchups"]
    rows = []
    for m in lib_matchups(table, depth, freqs):
        e = entries.get(m.mid, {})
        state = "aliased" if "alias_of" in e else "solved" if e.get("boards") else "missing"
        boards = len(index.boards(m.mid))
        # a limped line never gets boards outside 6-max 100bb: missing here, or aliased to a
        # 6-max limped line of another depth that is never solved either
        never = m.pot in SKIPPED_POTS and (table, depth) != (6, 100)
        skipped = never and (
            state == "missing" or (state == "aliased" and not boards and depth != 100)
        )
        rows.append({"mid": m.mid, "label": m.label, "pot": m.pot, "state": state,
                     "boards": boards, "subset": e.get("subset"), "skipped": skipped,
                     "fill": _fill_state(e, boards) if state == "solved" else None})  # fmt: skip
    solved = [r for r in rows if r["state"] == "solved"]
    aliased = [r for r in rows if r["state"] == "aliased"]
    missing = [r for r in rows if r["state"] == "missing"]
    return {"table": table, "depth": depth, "matchups": len(rows), "solved": len(solved),
            "subset": sum(r["fill"] == "subset" for r in solved),
            "extending": sum(r["fill"] == "extending" for r in solved),
            "full": sum(r["fill"] == "full" for r in solved), "aliased": len(aliased),
            "aliased_with_boards": sum(r["boards"] > 0 for r in aliased),
            "missing": len(missing), "skipped": sum(r["skipped"] for r in rows),
            "rows": rows}  # fmt: skip


def summary_line(c: Mapping[str, Any]) -> str:
    return (f"{c['table']}max/{c['depth']}bb: {c['matchups']} matchups, {c['solved']} solved "
            f"({c['subset']} subset, {c['extending']} extending, {c['full']} full), {c['aliased']} aliased "
            f"({c['aliased_with_boards']} with boards), {c['missing']} missing; "
            f"{c['skipped']} limped skipped on purpose")  # fmt: skip
