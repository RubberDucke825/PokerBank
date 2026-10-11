"""CLI: trainer performance (contracts/trainer.md "Plan 4 additions").

pn train stats [--window N] [--source S] [--db PATH]
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from pn.profile import source as psource

app = typer.Typer()
train_app = typer.Typer(no_args_is_help=True, help="Trainer history and rolling performance.")
app.add_typer(train_app, name="train")
console = Console()


def _fmt(x: float | None, pct: bool = False) -> str:
    if x is None:
        return "-"
    return f"{100 * x:.0f}%" if pct else f"{x:.2f}"


@train_app.command("stats")
def stats_cmd(
    window: Annotated[int, typer.Option(help="Newest N trainer hands")] = 1000,
    source: Annotated[str | None, typer.Option(help="only hands played with this source (gto)")] = None,
    db: Annotated[str | None, typer.Option("--db", help="SQLite path (env PN_DB).")] = None,
) -> None:
    """Rolling best / mixed / blunder rates and RNG match rate, by street / position / situation."""
    from pn.ingest import db as ingest_db
    from pn.train.stats import rolling

    path = Path(db or psource.default_db())
    if not path.exists():
        console.print(f"[red]database not found: {path}[/red]")
        raise typer.Exit(1)
    try:
        with ingest_db.opened(path) as conn:
            s = rolling(conn, window, source)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"{s['hands']} hands (window {s['window']}), {s['decisions']} decisions, "
                  f"{s['graded']} graded: best {_fmt(s['best_rate'], True)}, mixed "
                  f"{_fmt(s['mixed_rate'], True)}, blunders {s['blunders']} "
                  f"({_fmt(s['blunder_rate'], True)}); RNG match "
                  f"{_fmt(s['rng_match_rate'], True)} of {s['rng_decisions']}")  # fmt: skip
    table = Table("group", "hands", "graded", "best", "mixed", "blunder", "rng match")
    for title in ("by_street", "by_position", "by_situation"):
        for key, g in s[title].items():
            table.add_row(f"{title[3:]}: {key}", str(g["hands"]), str(g["graded"]),
                          _fmt(g["best_rate"], True), _fmt(g["mixed_rate"], True),
                          _fmt(g["blunder_rate"], True), _fmt(g["rng_match_rate"], True))  # fmt: skip
    for sl in s["slices"]:
        table.add_row(f"hands {sl['from_hand']}-{sl['to_hand']}", str(sl["hands"]),
                      str(sl["graded"]), "", "", _fmt(sl["blunder_rate"], True),
                      _fmt(sl["rng_match_rate"], True))  # fmt: skip
    console.print(table)


@train_app.command("leaks")
def leaks_cmd(
    window: Annotated[int, typer.Option(help="Newest N trainer hands")] = 1000,
    table: Annotated[int, typer.Option(help="6 | 8")] = 6,
    depth: Annotated[int, typer.Option(help="100 | 200")] = 100,
    top: Annotated[int, typer.Option(help="Rows to show")] = 20,
    db: Annotated[str | None, typer.Option("--db", help="SQLite path (env PN_DB).")] = None,
) -> None:
    """Spots ranked by expected blunders per 100 hands, with their drill weights."""
    from pn.ingest import db as ingest_db
    from pn.solve.cache import cache_dir
    from pn.train.leaks import Leaks

    path = Path(db or psource.default_db())
    if not path.exists():
        console.print(f"[red]database not found: {path}[/red]")
        raise typer.Exit(1)
    try:
        with ingest_db.opened(path) as conn:
            rows = Leaks(path, cache_root=cache_dir()).rows(conn, table, depth, window)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    t = Table("spot", "freq", "n", "blunder rate", "blunders/100", "weight")
    for r in rows[:top]:
        t.add_row(r["label"], f"{100 * r['freq']:.2f}%", str(r["n"]), f"{r['blunder_rate']:.1%}",
                  f"{r['expected_blunders_per_100']:.3f}", f"{100 * r['weight']:.1f}%")  # fmt: skip
    console.print(t)
