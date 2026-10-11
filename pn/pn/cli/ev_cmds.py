"""CLI: frequency grade (best / mixed / blunder) per decision in past hands (contracts/trainer.md
"Plan 7 additions"; the decision_ev table).

pn ev run [player] [--limit N] [--since ISO] [--force] [--time SEC]
pn ev show <hand_id> [--source S]          per-decision table
pn ev summary [player] [--source S]         counts, by street, by spot, top 10 hands by blunders
"""

from __future__ import annotations

import time
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from pn.profile import source as psource

app = typer.Typer()
ev_app = typer.Typer(no_args_is_help=True,
                     help="Frequency grades (best / mixed / blunder) of decisions in past hands.")
app.add_typer(ev_app, name="ev")
console = Console()

HERO_DEFAULT = "HERO"
DbOpt = Annotated[str | None, typer.Option("--db", help="SQLite path (env PN_DB).")]
SourceOpt = Annotated[str, typer.Option(help="gto (rows of older sources stay readable)")]


def _fail(msg: str) -> typer.Exit:
    console.print(f"[red]{msg}[/red]")
    return typer.Exit(1)


def _bb(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


@ev_app.command("run")
def run_cmd(
    player: Annotated[str | None, typer.Argument(help="Player id or name (default: all)")] = None,
    source: SourceOpt = "gto",
    limit: Annotated[int | None, typer.Option(help="Newest N hands")] = None,
    since: Annotated[str | None, typer.Option(help="Hands started at or after (ISO)")] = None,
    force: Annotated[bool, typer.Option(help="Re-grade existing rows")] = False,
    time_limit: Annotated[int, typer.Option("--time", help="Solver seconds per spot")] = 30,
    iterations: Annotated[int, typer.Option(help="Solver max iterations per spot")] = 300,
    db: DbOpt = None,
) -> None:
    """Grade decisions of players with known hole cards and store them in decision_ev."""
    from pn.ev.run import run

    t0 = time.monotonic()
    try:
        counts = run(db or psource.default_db(), player, source,
                     {"time_limit_sec": time_limit, "max_iterations": iterations}, limit, since,
                     force=force, emit=console.print)  # fmt: skip
    except (ValueError, FileNotFoundError, psource.PlayerNotFound) as e:
        raise _fail(str(e)) from e
    console.print(f"{counts['hands']} hands: {counts['graded']} graded, {counts['null']} null, "
                  f"{counts['skipped']} already done ({time.monotonic() - t0:.0f}s)")  # fmt: skip
    for note, n in sorted(counts["notes"].items(), key=lambda kv: -kv[1]):
        console.print(f"  null {note}: {n}")


def _conn(db: str | None):
    try:
        return psource.open_db(db or psource.default_db())
    except FileNotFoundError as e:
        raise _fail(str(e)) from e


@ev_app.command("show")
def show_cmd(hand_id: str, source: SourceOpt = "gto", db: DbOpt = None) -> None:
    """Per-decision grade table for one hand."""
    from pn.ev.report import hand_rows

    conn = _conn(db)
    try:
        rows = hand_rows(conn, hand_id, source)
    finally:
        conn.close()
    if not rows:
        raise _fail(f"no decision_ev rows for {hand_id} (source {source}): run `pn ev run`")
    t = Table(title=f"hand {hand_id} · source {source}")
    for c in ("seq", "street", "player", "spot", "chosen", "freq", "best", "best freq", "grade",
              "loss bb", "note"):  # fmt: skip
        t.add_column(c)
    for r in rows:
        t.add_row(str(r["seq"]), r["street"], r["player_id"], r["spot"] or "", r["chosen"],
                  _pct(r["freq_chosen"]), r["best"], _pct(r["best_freq"]), r["grade"] or "-",
                  _bb(r["loss_bb"]), r["note"] or "")  # fmt: skip
    console.print(t)


@ev_app.command("summary")
def summary_cmd(player: Annotated[str, typer.Argument()] = HERO_DEFAULT,
                source: SourceOpt = "gto",
                include_unmodeled: Annotated[bool, typer.Option(
                    help="Count decisions whose hand was outside the modeled range")] = False,
                db: DbOpt = None) -> None:  # fmt: skip
    """Best / mixed / blunder counts, by street, by spot, top 10 hands by blunders."""
    from pn.ev.report import player_ids, summary

    conn = _conn(db)
    try:
        s = summary(conn, player_ids(conn, player), source, include_unmodeled)
    except psource.PlayerNotFound as e:
        raise _fail(str(e)) from e
    finally:
        conn.close()
    console.print(f"[bold]{player}[/bold] source {source}: {s['graded']} graded decisions in "
                  f"{s['hands']} hands: best {s['best']}, mixed {s['mixed']}, blunders "
                  f"{s['blunders']} ({_pct(s['blunder_rate'])}, {_num(s['blunders_per_100'])} "
                  f"per 100 hands); {s['null']} not graded")  # fmt: skip
    off = s["unmodeled"]
    if off["decisions"]:
        verb = "included above" if include_unmodeled else "not counted (--include-unmodeled)"
        console.print(f"  off-model (hand outside the modeled range): {off['decisions']} "
                      f"decisions, {off['blunders']} blunders, {verb}")  # fmt: skip
    for other, tot in s["compare"].items():
        if other != source:
            console.print(f"  {other}: {tot['blunders']} blunders over {tot['graded']} decisions")
    for key, title in (("by_street", "street"), ("by_spot", "spot")):
        t = Table(title=f"by {title}")
        for c in (title, "decisions", "graded", "best", "mixed", "blunders"):
            t.add_column(c)
        for g in s[key]:
            t.add_row(g[title if title == "street" else "spot"], str(g["n"]), str(g["graded"]),
                      str(g["best"]), str(g["mixed"]), str(g["blunders"]))  # fmt: skip
        console.print(t)
    t = Table(title="hands with the most blunders")
    for c in ("hand", "#", "decisions", "blunders"):
        t.add_column(c)
    for h in s["top_hands"]:
        t.add_row(h["hand_id"], str(h["hand_no"]), str(h["decisions"]), str(h["blunders"]))
    console.print(t)
    if s["notes"]:
        console.print("not graded: " + ", ".join(f"{k} {v}" for k, v in s["notes"].items()))
