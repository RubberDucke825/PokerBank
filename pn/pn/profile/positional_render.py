"""Rich rendering of the positional matrix: PT4-like stat x position grid."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from pn.profile.positional import LABELS, PREFLOP_ROWS, STREET_ROWS

STREETS = ("preflop", "flop", "turn", "river")
STRONG_DEV = 0.10  # |dev_gto| at or above this is bold


def rows_for(street: str) -> tuple[str, ...]:
    return PREFLOP_ROWS if street == "preflop" else STREET_ROWS[street]


def row_label(stat: str) -> str:
    for base in ("three_bet", "call_open"):
        if stat.startswith(f"{base}_vs_"):
            return f"  {LABELS[base]} vs {stat.rsplit('_', 1)[1]}"
    return LABELS.get(stat, stat)


def cell_style(obj: Mapping) -> str:
    """red = above GTO, blue = below, dim = CI contains GTO or no GTO; bold when large."""
    dev = obj.get("dev_gto")
    if not dev:
        return "dim" if dev is None else ""
    color = "red" if dev > 0 else "cyan"
    return f"bold {color}" if abs(dev) >= STRONG_DEV else color


def cell_text(obj: Mapping | None) -> Text:
    if obj is None:
        return Text("-", style="dim")
    t = Text(f"{100 * obj['post_mean']:.0f}% ({obj['n']})", style=cell_style(obj))
    if obj.get("gto") is not None:
        t.append(f"\ng {100 * obj['gto']:.0f}%", style="dim")
    return t


def _cells(block: Mapping, street: str, split: str) -> Mapping:
    if street == "preflop":
        return block["preflop"]
    if split == "all":
        return block["postflop"]
    return block["postflop_split"].get(split, {})


def matrix_table(matrix: Mapping, street: str = "preflop", split: str = "all") -> Table:
    positions: Sequence[str] = matrix["positions"]
    title = f"{matrix.get('canonical_id') or ''} {street}" + ("" if split == "all" else f" {split}")
    t = Table(title=title.strip(), show_lines=False)
    t.add_column("stat", no_wrap=True)
    for pos in positions:
        t.add_column(f"{pos}\n{matrix['hands_by_position'][pos]}h", justify="right")
    for stat in rows_for(street):
        cells = [_cells(matrix["by_position"][p], street, split).get(stat) for p in positions]
        if all(c is None for c in cells):
            continue
        t.add_row(row_label(stat), *(cell_text(c) for c in cells))
    return t


def hist_table(matrix: Mapping, street: str, split: str = "all") -> Table | None:
    rows = []
    for pos in matrix["positions"]:
        h = matrix["by_position"][pos]["bet_size_hist"].get(split, {}).get(street)
        if h and sum(h.values()):
            rows.append((pos, h))
    if not rows:
        return None
    names = list(rows[0][1])
    t = Table("pos", *(f"{n}%" for n in names), title=f"{street} bet sizes (% pot)")
    for pos, h in rows:
        t.add_row(pos, *(str(h[n]) for n in names))
    return t


def summary_table(matrix: Mapping) -> Table | None:
    pw = matrix["summary"]["preflop_weighted"]
    stats = [s for s in pw if not s.startswith("_")]
    if not stats:
        return None
    t = Table("stat", "player", "GTO", "diff", "dev_gto", "n",
              title="Position-weighted preflop (weights = hands per position)")  # fmt: skip
    for s in stats:
        v = pw[s]
        t.add_row(LABELS.get(s, s), f"{100 * v['player']:.1f}%", f"{100 * v['gto']:.1f}%",
                  f"{100 * v['diff']:+.1f}", f"{100 * v['dev_gto']:+.1f}", str(v["n"]))  # fmt: skip
    return t


def print_matrix(
    matrix: Mapping, console: Console, street: str = "preflop", split: str = "all"
) -> None:
    console.print(matrix_table(matrix, street, split))
    if street == "preflop":
        st = summary_table(matrix)
        if st is not None:
            console.print(st)
        return
    ht = hist_table(matrix, street, split)
    if ht is not None:
        console.print(ht)


def print_leaks(leaks: Sequence[Mapping], console: Console) -> None:
    if not leaks:
        console.print("[dim]No positional leaks: no cell with n >= 10 whose CI excludes GTO.[/dim]")
        return
    t = Table("#", "leak", "dev", "exploit", title="Positional leaks (|dev_gto| x n)")
    for i, lk in enumerate(leaks, 1):
        style = "red" if lk["dev_gto"] > 0 else "cyan"
        t.add_row(str(i), lk["label"], Text(f"{100 * lk['dev_gto']:+.1f}", style=style),
                  lk["exploit"])  # fmt: skip
    console.print(t)
