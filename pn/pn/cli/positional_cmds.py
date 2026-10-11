"""CLI: pn pos <id> [--street ...] [--split ...] [--json] | pn pos leaks <id>."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from pn.profile import source
from pn.profile.positional import positional_matrix
from pn.profile.positional_render import STREETS, print_leaks, print_matrix

app = typer.Typer()
console = Console()


def _hands_from_log(log: Path) -> list[dict]:
    from pn.ingest.parse import parse_messages, sort_messages

    msgs = json.loads(log.read_text())
    return parse_messages(sort_messages(msgs), game_id=log.stem.removeprefix("pokernow_"))


def _load(db: str, log: Path | None) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    if log is not None:
        hands = _hands_from_log(log)
        return hands, {}, source.names_from_hands(hands)
    try:
        conn = source.open_db(db)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red] (run `pn fetch` first, pass --db or --log)")
        raise typer.Exit(1) from e
    with conn:
        hands = source.load_hands(conn)
        amap = source.alias_map(conn)
        names = {**source.names_from_hands(hands, amap), **source.display_names(conn)}
    conn.close()
    return hands, amap, names


@app.command("pos")
def pos_cmd(
    target: Annotated[str, typer.Argument(help="player id/name, or `leaks`")],
    player: Annotated[str | None, typer.Argument(help="player for `pos leaks <id>`")] = None,
    db: Annotated[str, typer.Option("--db", help="SQLite path (env PN_DB).")] = source.default_db(),
    log: Annotated[
        Path | None, typer.Option("--log", help="PokerNow JSON log instead of DB.")
    ] = None,
    street: Annotated[str, typer.Option(help="preflop|flop|turn|river")] = "preflop",
    split: Annotated[str, typer.Option(help="postflop: all|ip|oop|hu|multiway")] = "all",
    json_out: Annotated[bool, typer.Option("--json", help="Print the matrix as JSON.")] = False,
) -> None:
    """PT4-style positional stats (rows = stats, columns = positions, colored vs GTO)."""
    leaks_mode = target == "leaks" and player is not None
    query = player if leaks_mode else target
    if street not in STREETS or split not in ("all", "ip", "oop", "hu", "multiway"):
        console.print(f"[red]bad --street {street!r} or --split {split!r}[/red]")
        raise typer.Exit(2)
    hands, amap, names = _load(db, log)
    seen = {amap.get(p["player_id"], p["player_id"]) for h in hands for p in h["players"]}
    try:
        cid = source.resolve_target(query, amap, {**{c: c for c in seen}, **names})
    except source.PlayerNotFound as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    m = positional_matrix(cid, hands, alias_map=amap)
    if json_out:
        typer.echo(json.dumps(m["leaks"] if leaks_mode else m, indent=2))
        return
    console.rule(f"[bold]{names.get(cid, cid)}[/bold] ({cid})  hands {m['hands']}")
    if leaks_mode:
        print_leaks(m["leaks"], console)
    else:
        print_matrix(m, console, street, split)
