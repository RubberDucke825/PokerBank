"""Contract-shaped sample solver output (contracts/strategy_output.md) for the strategy viewer.

MOCK DATA: strategies come from a simple equity heuristic, not a solver. Equity is exact
showdown equity of the made hand on the flop vs the opponent's current reach (no draws), so
numbers are plausible but not GTO. Used until `mwsolver solve` produces real files.
"""

from __future__ import annotations

import numpy as np

from pn import ranges
from pn.profile.handeval import evaluate

BOARD = ["Ks", "7d", "2c"]
BB_CENTS = 100
POT = 550
STACKS = [9725, 9725]
RANGES = {
    0: "22-99,A2s-AJs,K2s-KJs,Q6s+,J7s+,T7s+,96s+,85s+,74s+,63s+,53s+,43s,A8o-AJo,K9o-KJo,"
       "Q9o+,J9o+,T9o,98o",  # BB defend vs BTN (approx.)
    1: "22+,A2s+,K2s+,Q4s+,J6s+,T6s+,96s+,85s+,75s+,64s+,54s,A5o+,K9o+,Q9o+,J9o+,T9o",  # BTN open
}  # fmt: skip
PLAYERS = [
    {"index": 0, "name": "hero", "position": "BB", "hero": True, "locked": False},
    {"index": 1, "name": "villain", "position": "BTN", "hero": False, "locked": False},
]


def _board_ints() -> list[int]:
    return [ranges.card_to_int(c) for c in BOARD]


def _combo_cards() -> np.ndarray:
    out = np.zeros((ranges.N_COMBOS, 2), dtype=np.int16)
    for i, c in enumerate(ranges.all_combos()):
        out[i] = (ranges.card_to_int(c[:2]), ranges.card_to_int(c[2:]))
    return out


def _initial_reach(text: str, dead: list[int], cards: np.ndarray) -> np.ndarray:
    w = np.array(ranges.to_dense(ranges.parse(text)), dtype=np.float64)
    blocked = np.isin(cards, dead).any(axis=1)
    w[blocked] = 0.0
    return w


class _Ctx:
    def __init__(self) -> None:
        self.cards = _combo_cards()
        board = _board_ints()
        self.values = np.array([evaluate([int(a), int(b), *board]) for a, b in self.cards])
        # conflict[i, j] = combos i and j share a card
        c = self.cards
        self.conflict = ((c[:, None, 0] == c[None, :, 0]) | (c[:, None, 0] == c[None, :, 1])
                         | (c[:, None, 1] == c[None, :, 0]) | (c[:, None, 1] == c[None, :, 1]))  # fmt: skip
        self.nodes: dict[str, dict] = {}
        self.next_id = 0

    def equity(self, opp_reach: np.ndarray) -> np.ndarray:
        w = np.where(self.conflict, 0.0, opp_reach[None, :])
        win = (self.values[:, None] > self.values[None, :]) * w
        tie = (self.values[:, None] == self.values[None, :]) * w
        tot = w.sum(axis=1)
        return np.divide(win.sum(axis=1) + 0.5 * tie.sum(axis=1), tot,
                         out=np.zeros_like(tot), where=tot > 0)  # fmt: skip

    def new_id(self) -> str:
        nid = f"n{self.next_id}"
        self.next_id += 1
        return nid


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _first_strategy(eq: np.ndarray, n_bets: int) -> np.ndarray:
    """Columns: check, bet sizes small..large."""
    value = _sigmoid((eq - 0.72) * 14)
    bluff = 0.45 * _sigmoid((0.22 - eq) * 20)
    bet = np.clip(value + bluff, 0, 0.95)
    cols = [1 - bet]
    if n_bets == 1:
        cols.append(bet)
    else:
        big = np.clip(_sigmoid((eq - 0.85) * 18) + 0.5 * _sigmoid((0.15 - eq) * 25), 0, 1)
        cols += [bet * (1 - big), bet * big]
    return np.stack(cols, axis=1)


def _facing_strategy(eq: np.ndarray, size_frac: float, can_raise: bool) -> np.ndarray:
    """Columns: fold, call[, raise]."""
    need = size_frac / (1 + 2 * size_frac)
    cont = _sigmoid((eq - need * 1.1) * 16)
    if not can_raise:
        return np.stack([1 - cont, cont], axis=1)
    rz = np.clip(_sigmoid((eq - 0.9) * 20) + 0.25 * _sigmoid((0.12 - eq) * 30), 0, 0.9)
    rz = np.minimum(rz, cont + 0.25 * (eq < 0.12))
    call = np.clip(cont - rz, 0, 1)
    fold = np.clip(1 - call - rz, 0, 1)
    return np.stack([fold, call, rz], axis=1)


def _emit(ctx: _Ctx, nid: str, *, street: str, player: int, pot: int, stacks: list[int],
          history: list[str], reach: list[np.ndarray], tokens: list[tuple[str, int | None]],
          strat: np.ndarray) -> list[tuple[str, np.ndarray]]:  # fmt: skip
    """Write one action node; returns (child_id, actor reach after action) per action."""
    me, opp = reach[player], reach[1 - player]
    eq = ctx.equity(opp)
    in_range = me > 0
    strat = strat / strat.sum(axis=1, keepdims=True)
    strat[~in_range] = 0.0
    eqr = 0.75 + 0.45 * eq
    ev = np.where(in_range, eq * pot * eqr, 0.0)
    eq = np.where(in_range, eq, 0.0)
    tot = me.sum()
    summary: dict[str, float] = {}
    for k, (tok, _) in enumerate(tokens):
        summary[tok] = round(float((me * strat[:, k]).sum() / tot), 4) if tot else 0.0
    ev_avg = float((me * ev).sum() / tot) if tot else 0.0
    eq_avg = float((me * eq).sum() / tot) if tot else 0.0
    summary |= {"ev_bb": round(ev_avg / BB_CENTS, 3), "equity": round(eq_avg, 4),
                "eqr": round(ev_avg / (eq_avg * pot), 3) if eq_avg else 0.0}  # fmt: skip
    children = [ctx.new_id() for _ in tokens]
    actions = []
    for (tok, amt), child in zip(tokens, children, strict=True):
        a = {"token": tok, "child": child}
        if amt is not None:
            a["amount"] = amt
        actions.append(a)
    ctx.nodes[nid] = {
        "type": "action", "street": street, "player": player, "pot": pot, "stacks": stacks,
        "history": history, "actions": actions,
        "strategy": np.round(strat, 4).tolist(), "ev": np.round(ev, 1).tolist(),
        "reach": np.round(me, 4).tolist(), "equity": np.round(eq, 4).tolist(),
        "summary": summary,
    }  # fmt: skip
    return [(c, me * strat[:, k]) for k, c in enumerate(children)]


def _terminal(ctx: _Ctx, nid: str, kind: str, pot: int) -> None:
    ctx.nodes[nid] = {"type": "terminal", "kind": kind, "pot": pot, "payoffs_by_player": None}


def _facing(ctx: _Ctx, nid: str, *, player: int, pot: int, bet: int, stacks: list[int],
            history: list[str], reach: list[np.ndarray], street: str = "flop",
            raise_to: int | None = None) -> None:  # fmt: skip
    """Node facing a bet of `bet` into `pot` (pot already includes the bet)."""
    frac = bet / (pot - bet)
    can_raise = raise_to is not None
    tokens: list[tuple[str, int | None]] = [("fold", None), ("call", bet)]
    if can_raise:
        allin = raise_to >= stacks[player]
        tokens.append(("allin" if allin else f"raise{raise_to}", min(raise_to, stacks[player])))
    eq = ctx.equity(reach[1 - player])
    kids = _emit(ctx, nid, street=street, player=player, pot=pot, stacks=stacks, history=history,
                 reach=reach, tokens=tokens, strat=_facing_strategy(eq, frac, can_raise))  # fmt: skip
    _terminal(ctx, kids[0][0], "fold", pot)
    _terminal(ctx, kids[1][0], "leaf", pot + bet)
    if can_raise:
        cid, r = kids[2]
        amt = tokens[2][1]
        new_stacks = list(stacks)
        new_stacks[player] -= amt
        reach2 = list(reach)
        reach2[player] = r
        _facing(ctx, cid, player=1 - player, pot=pot + amt, bet=amt - bet, stacks=new_stacks,
                history=[*history, tokens[2][0]], reach=reach2, street=street)  # fmt: skip


def _bet_branch(ctx: _Ctx, cid: str, r: np.ndarray, *, actor: int, tok: str, amt: int,
                pot: int, history: list[str], reach: list[np.ndarray]) -> None:  # fmt: skip
    stacks = list(STACKS)
    stacks[actor] -= amt
    reach2 = list(reach)
    reach2[actor] = r
    _facing(ctx, cid, player=1 - actor, pot=pot + amt, bet=amt, stacks=stacks,
            history=[*history, tok], reach=reach2, raise_to=3 * amt)  # fmt: skip


def _turn_chance(ctx: _Ctx, nid: str, reach: list[np.ndarray], history: list[str]) -> None:
    """Check-check -> turn chance node; two cards lead to a turn decision, the rest to leaves."""
    dead = set(_board_ints())
    children: dict[str, str] = {}
    for c in range(52):
        if c in dead:
            continue
        card = ranges.int_to_card(c)
        cid = ctx.new_id()
        children[card] = cid
        if card in ("As", "3h"):
            r = [np.where(np.isin(ctx.cards, [c]).any(axis=1), 0.0, x) for x in reach]
            kids = _emit(ctx, cid, street="turn", player=0, pot=POT, stacks=list(STACKS),
                         history=[*history, card], reach=r,
                         tokens=[("check", None), ("bet75", 412)],
                         strat=_first_strategy(ctx.equity(r[1]), 1))  # fmt: skip
            for k, _ in kids:
                _terminal(ctx, k, "leaf", POT)
        else:
            _terminal(ctx, cid, "leaf", POT)
    ctx.nodes[nid] = {"type": "chance", "street": "turn", "children": children}


def generate_sample() -> dict:
    ctx = _Ctx()
    dead = _board_ints()
    reach = [_initial_reach(RANGES[i], dead, ctx.cards) for i in (0, 1)]
    root = ctx.new_id()
    oop = [("check", None), ("bet33", 182)]
    kids = _emit(ctx, root, street="flop", player=0, pot=POT, stacks=list(STACKS), history=[],
                 reach=reach, tokens=oop, strat=_first_strategy(ctx.equity(reach[1]), 1))  # fmt: skip
    (chk_id, chk_r), (bet_id, bet_r) = kids
    _bet_branch(ctx, bet_id, bet_r, actor=0, tok="bet33", amt=182, pot=POT, history=[],
                reach=reach)  # fmt: skip
    r1 = [chk_r, reach[1]]
    ip = [("check", None), ("bet50", 275), ("bet125", 688)]
    k2 = _emit(ctx, chk_id, street="flop", player=1, pot=POT, stacks=list(STACKS),
               history=["check"], reach=r1, tokens=ip,
               strat=_first_strategy(ctx.equity(r1[0]), 2))  # fmt: skip
    (cc_id, cc_r), *bets = k2
    _turn_chance(ctx, cc_id, [chk_r, cc_r], ["check", "check"])
    for (cid, r), (tok, amt) in zip(bets, ip[1:], strict=True):
        _bet_branch(ctx, cid, r, actor=1, tok=tok, amt=amt, pot=POT, history=["check"],
                    reach=r1)  # fmt: skip
    return {
        "meta": {"config_path": "(sample: generated by pn.web.sample, not a solver)",
                 "iterations": 0, "exploitability_pct_pot": None, "wall_sec": 0.0,
                 "node_count": len(ctx.nodes), "memory_bytes": 0, "mode": "full",
                 "board": BOARD, "bb": BB_CENTS, "sample": True},
        "players": PLAYERS,
        "combos": list(ranges.all_combos()),
        "root": root,
        "nodes": dict(sorted(ctx.nodes.items(), key=lambda kv: int(kv[0][1:]))),
    }  # fmt: skip
