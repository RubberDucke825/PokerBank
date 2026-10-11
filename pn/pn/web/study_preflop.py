"""Preflop study navigator (contracts/study_preflop.md): replay a preflop line with trainer
sizing and return every seat's options, the actor's class strategy by the trainer's source order
(PokerCoaching -> GTO Wizard -> formula chart) and, once preflop is over, the heads-up matchup
with its solved flops."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from pn import ranges
from pn.solve.cache import cache_dir
from pn.solve.request import TABLE_SEATS
from pn.train import flop_library as flib
from pn.train import preflop as pre
from pn.train.engine import IllegalAction, State, apply, hand_decided, start
from pn.web.solves import CACHE_PREFIX

router = APIRouter()
BB = 100
TOKENS = ("fold", "check", "call", "raise", "allin")
SOURCE_LABEL = {pre.PC: "PokerCoaching", pre.GTOW: "GTO Wizard"}


def _bb(cents: int) -> str:
    return f"{cents / BB:g}"


def _size(st: State, i: int, depth: int) -> int:
    sit = pre.situation(st, i)
    found = pre.gtow_spot(sit.key, depth)
    return pre.raise_to(st, i, sit, found[1] if found else None)


def _options(st: State, i: int, depth: int) -> list[dict[str, Any]]:
    opts = pre.legal_actions(st, i, _size(st, i, depth))
    limp = st.highest == st.bb
    names = {"fold": "Fold", "check": "Check", "call": "Limp" if limp else "Call"}
    for o in opts:
        if o["token"] == "raise":
            o["label"] = f"Raise {_bb(o['to'])}"
        elif o["token"] == "allin":
            o["label"] = f"All-in {_bb(o['to'])}"
        else:
            o["label"] = names[o["token"]]
    return opts


def _class_codes(st: State, i: int, table: str, depth: int) -> tuple[dict, dict]:
    """(class -> code -> frequency, situation) for seat `i`, by the trainer's source order."""
    sit = pre.situation(st, i)
    pos = st.positions[i]
    found = pre.gtow_spot(sit.key, depth)
    base = {"kind": sit.kind, "key": sit.key}
    if pre._pc_line(sit):
        pc = pre.pc_class_dists(table, sit.kind, pos, pre.pc_other(sit.kind, sit.seats))
        if pc is not None:
            return pc[0], {**base, "source": pre.PC, "origin": f"pc:{pc[1]}",
                           "label": f"PokerCoaching 100bb · {pc[1]}"}  # fmt: skip

    def chart(cls: str) -> dict[str, float]:
        return pre.chart_dist(st, i, sit, ranges.class_combos(cls)[0], table, depth)

    if found is not None:
        cells = {c: pre.cell_dist(found[1], c) or chart(c) for c in ranges.all_classes()}
        d = found[0].split("|", 1)
        return cells, {**base, "source": pre.GTOW, "origin": f"gtow:{found[0]}",
                       "label": f"GTO Wizard {d[0]}bb · {d[1]}"}  # fmt: skip
    cells = {c: chart(c) for c in ranges.all_classes()}
    return cells, {**base, "source": pre.CHART, "origin": "chart", "label": "formula chart"}


def _strategy(st: State, i: int, table: str, depth: int) -> tuple[dict, dict, dict]:
    """(cells class -> token -> freq, summary token -> share, situation) for seat `i`."""
    codes, situation = _class_codes(st, i, table, depth)
    size = _size(st, i, depth)
    token = {c: pre.code_action(st, i, c, size)[0] for c in pre.CODES}
    cells: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {}
    for cls, dist in codes.items():
        cell: dict[str, float] = {}
        for code, f in dist.items():
            cell[token[code]] = cell.get(token[code], 0.0) + f
        cells[cls] = {t: round(f, 4) for t, f in cell.items()}
        for t, f in cell.items():
            totals[t] = totals.get(t, 0.0) + f * ranges.class_n_combos(cls)
    summary = {t: round(v / ranges.N_COMBOS, 4) for t, v in totals.items() if v > 0}
    return cells, summary, situation


def _parse(line: str, order: tuple[str, ...]) -> list[tuple[str, str]]:
    steps = []
    for n, raw in enumerate((s.strip() for s in line.split(",") if s.strip()), 1):
        pos, _, tok = raw.partition(":")
        pos, tok = pos.strip().upper(), tok.strip().lower()
        if pos not in order or tok not in TOKENS:
            raise HTTPException(400, f"step {n} {raw!r}: expected <position>:<{'|'.join(TOKENS)}>")
        steps.append((pos, tok))
    return steps


def _replay(steps: list[tuple[str, str]], order: tuple[str, ...], depth: int) -> tuple[State, dict]:
    """Final state and, per position, (state at its last turn, applied token)."""
    st = start(order, depth * BB, BB // 2, BB)
    turns: dict[str, tuple[State, str]] = {}
    for n, (pos, tok) in enumerate(steps, 1):
        where = f"step {n} {pos}:{tok}"
        if st.to_act is None:
            raise HTTPException(400, f"{where}: preflop is over")
        if st.positions[st.to_act] != pos:
            raise HTTPException(400, f"{where}: {st.positions[st.to_act]} is to act")
        opts = {
            o["token"]: o for o in pre.legal_actions(st, st.to_act, _size(st, st.to_act, depth))
        }
        if tok == "raise" and "raise" not in opts and "allin" in opts:
            tok = "allin"
        if tok not in opts:
            raise HTTPException(400, f"{where}: illegal (legal: {', '.join(opts)})")
        try:
            nxt = apply(
                st, opts[tok]["kind"], opts[tok]["to"] if tok in ("raise", "allin") else None
            )
        except IllegalAction as e:
            raise HTTPException(400, f"{where}: {e}") from e
        turns[pos] = (st, tok)
        st = nxt
    return st, turns


def _seat(st: State, i: int, turns: dict, table: str, depth: int) -> dict[str, Any]:
    pos = st.positions[i]
    last = turns.get(pos)
    if st.folded[i]:
        status = "folded"
    elif st.to_act == i:
        status = "to_act"
    elif st.stacks[i] == 0:
        status = "allin"
    else:
        status = "acted" if last else "waiting"
    at = st if status == "to_act" else last[0] if last else None
    opts = _options(at, i, depth) if at is not None else []
    summary = _strategy(at, i, table, depth)[1] if at is not None and status != "folded" else None
    for o in opts:
        o["freq"] = summary.get(o["token"], 0.0) if summary is not None else None
    return {"position": pos, "stack": st.stacks[i], "committed": st.committed[i], "status": status,
            "chosen": last[1] if last else None, "options": opts}  # fmt: skip


def _end(st: State, table: str, depth: int, root: Any) -> dict[str, Any]:
    live = st.in_hand()
    decided = hand_decided(st)
    m = flib.matchup_of(st, table, depth) if len(live) == 2 else None
    boards = []
    if m is not None:
        entries = flib.FlopIndex(root).boards(m.mid)
        boards = [{"board": b, "solve": CACHE_PREFIX + e["key"][:16], "weight": e.get("weight", 1)}
                  for b, e in sorted(entries.items(), key=lambda kv: -kv[1].get("weight", 1))]  # fmt: skip
    note = (
        "hand decided preflop"
        if decided
        else None
        if len(live) == 2
        else "multiway: not solved (2-player only)"
    )
    return {"kind": "decided" if decided else "flop", "players": [st.positions[i] for i in live],
            "pot": st.pot, "stacks": {st.positions[i]: st.stacks[i] for i in live},
            "matchup": {"mid": m.mid, "label": m.label, "pot_type": m.pot,
                        "positions": list(m.positions)} if m else None,
            "boards": boards, "solvable": len(live) == 2 and not decided, "note": note}  # fmt: skip


@router.get("/api/study/preflop")
def study_preflop(request: Request, table: int = Query(6), depth: int = Query(100),
                  line: str = "") -> dict[str, Any]:  # fmt: skip
    if table not in TABLE_SEATS or depth not in (100, 200):
        raise HTTPException(400, "table must be 6 or 8 and depth 100 or 200")
    order = TABLE_SEATS[table]
    kind = f"{table}max"
    steps = _parse(line, order)
    st, turns = _replay(steps, order, depth)
    i = st.to_act
    cells = summary = situation = None
    if i is not None:
        cells, summary, situation = _strategy(st, i, kind, depth)
    cache = getattr(request.app.state, "solve_cache", None)
    return {"table": table, "depth": depth, "bb": BB, "line": ",".join(f"{p}:{t}" for p, t in steps),
            "pot": st.pot, "street": "preflop",
            "seats": [_seat(st, k, turns, kind, depth) for k in range(st.n)],
            "actor": st.positions[i] if i is not None else None, "situation": situation,
            "cells": cells, "summary": summary,
            "end": _end(st, kind, depth, cache.root if cache else cache_dir()) if i is None else None}  # fmt: skip
