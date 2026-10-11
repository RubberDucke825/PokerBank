"""CLI: pn fetch | import-csv | validate | alias merge/list | hands list | hand show."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from pn.ingest import db
from pn.ingest.parse import session_events
from pn.ingest.pipeline import IngestResult, find_ledger, ingest
from pn.ingest.render import hand_replay, money
from pn.ingest.validate import read_ledger, validate_game

app = typer.Typer()
alias_app = typer.Typer(help="Player identity: group PokerNow ids belonging to one person.")
hands_app = typer.Typer(help="Browse stored hands.")
hand_app = typer.Typer(help="Inspect one hand.")
app.add_typer(alias_app, name="alias")
app.add_typer(hands_app, name="hands")
app.add_typer(hand_app, name="hand")
console = Console()

DbOpt = Annotated[str, typer.Option("--db", help="SQLite database path.")]
NameOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--name",
        help='Attach a player to a person: --name "Full Name=<player id or username>" '
        "(repeatable; usernames are matched within the ingested game).",
    ),
]


def _parse_names(values: list[str] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for v in values or []:
        name, sep, pid = v.rpartition("=")
        if not sep or not name.strip() or not pid.strip():
            raise typer.BadParameter(f'expected "Full Name=<player id or username>", got {v!r}')
        out[name.strip().strip('"')].append(pid.strip())
    return dict(out)


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.0f}%" if n else "-"


def _summary(result: IngestResult, db_path: str) -> None:
    rep = result.report
    with db.opened(db_path) as conn:
        ids = [h["hand_id"] for h in result.hands]
        marks = ",".join("?" * len(ids))
        rows = (
            conn.execute(
                f"""SELECT hp.player_id, COALESCE(c.display_name, p.nickname) AS name, COUNT(*) n, SUM(vpip) vpip,
                       SUM(pfr) pfr, SUM(saw_flop) sf, SUM(went_to_sd) sd, SUM(net) net
                FROM hand_players hp JOIN players p USING(player_id)
                LEFT JOIN players c ON c.player_id = p.canonical_id
                WHERE hand_id IN ({marks}) GROUP BY hp.player_id ORDER BY net DESC""",
                ids,
            ).fetchall()
            if ids
            else []
        )
    ledger = {r["player_id"]: r for r in rep["ledger"] or []}
    table = Table(title=f"Game {result.game_id}: {len(result.hands)} hands")
    for col in ("player", "id", "hands", "VPIP", "PFR", "WTSD", "net", "ledger net", "delta"):
        table.add_column(col, justify="left" if col in ("player", "id") else "right")
    for r in rows:
        led = ledger.get(r["player_id"], {})
        wtsd = _pct(r["sd"], r["sf"])
        delta = led.get("delta")
        table.add_row(
            r["name"],
            r["player_id"],
            str(r["n"]),
            _pct(r["vpip"], r["n"]),
            _pct(r["pfr"], r["n"]),
            wtsd,
            money(r["net"]),
            money(led.get("ledger_net")),
            "" if delta is None else f"[{'green' if delta == 0 else 'red'}]{money(delta)}[/]",
        )
    stats_line = result.load_stats
    console.print(table)
    console.print(
        f"loaded: {stats_line['inserted']} new, {stats_line['skipped']} already stored, "
        f"{stats_line['replaced']} replaced, {stats_line['enriched']} enriched with new hole cards | "
        f"flagged: {len(rep['flagged'])} | "
        f"pot-math problems: {len(rep['hand_problems'])} | missing hand numbers: {rep['missing_hand_numbers'] or 'none'}"
    )
    _print_problems(rep)
    if result.ledger_path is None:
        console.print("[yellow]no ledger CSV found; pass --ledger to cross-check nets[/]")


def _print_problems(rep: dict[str, Any]) -> None:
    for hid, info in rep["flagged"].items():
        console.print(f"[yellow]#{info['hand_no']} {hid} {info['flags']}: {info['issues']}[/]")
    for hid, probs in rep["hand_problems"].items():
        console.print(f"[red]{hid}: {probs}[/]")
    unexplained = [b for b in rep["stack_breaks"] if not b["explained_by"]]
    console.print(
        f"stack continuity: {len(rep['stack_breaks'])} breaks between hands, "
        f"{len(unexplained)} not explained by rebuy/top-up/rejoin events"
    )
    for b in unexplained:
        console.print(f"[red]  {b}[/]")


@app.command()
def fetch(
    source: Annotated[
        str, typer.Argument(help="PokerNow game URL or id, or a local log .json file.")
    ],
    name: NameOpt = None,
    db_path: DbOpt = db.DEFAULT_DB,
    cache_dir: Annotated[str, typer.Option(help="Raw page cache directory.")] = "data/raw",
    ledger: Annotated[str | None, typer.Option(help="Ledger CSV for the net cross-check.")] = None,
    no_ledger: Annotated[
        bool, typer.Option("--no-ledger", help="Do not download the ledger CSV.")
    ] = False,
    game_id: Annotated[str | None, typer.Option(help="Game id when SOURCE is a file.")] = None,
    verbose: Annotated[bool, typer.Option("-v")] = False,
) -> None:
    """Fetch (or refresh) a game log, parse, load into SQLite and validate."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    result = ingest(
        source,
        db_path,
        cache_dir=cache_dir,
        names=_parse_names(name),
        ledger=ledger,
        fetch_ledger_csv=not no_ledger,
        game_id=game_id,
    )
    _summary(result, db_path)


@app.command("import-csv")
def import_csv(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="PokerNow full-log CSV export.")
    ],
    name: NameOpt = None,
    db_path: DbOpt = db.DEFAULT_DB,
    game_id: Annotated[str | None, typer.Option(help="Game id if not in the filename.")] = None,
    hero: Annotated[
        str | None, typer.Option(help="Player id of the exporting player ('Your hand is').")
    ] = None,
    ledger: Annotated[str | None, typer.Option(help="Ledger CSV for the net cross-check.")] = None,
) -> None:
    """Import a logged-in PokerNow CSV export (includes your hole cards)."""
    try:
        result = ingest(
            str(file),
            db_path,
            names=_parse_names(name),
            ledger=ledger,
            game_id=game_id,
            hero_id=hero,
            fetch_ledger_csv=False,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _summary(result, db_path)


@app.command()
def validate(
    game_id: str,
    db_path: DbOpt = db.DEFAULT_DB,
    ledger: Annotated[
        str | None, typer.Option(help="Ledger CSV (default: data/raw or data/fixtures).")
    ] = None,
    cache_dir: Annotated[
        str, typer.Option(help="Raw cache, used to explain stack breaks.")
    ] = "data/raw",
) -> None:
    """Re-check stored hands: pot math, stack continuity, ledger nets."""
    with db.opened(db_path) as conn:
        hands = db.hands_for_game(conn, game_id)
    if not hands:
        raise typer.BadParameter(f"no hands stored for game {game_id}")
    raw = Path(cache_dir) / f"{game_id}.json"
    events = session_events(json.loads(raw.read_text())["logs"]) if raw.exists() else []
    ledger_path = Path(ledger) if ledger else find_ledger(game_id)
    rep = validate_game(hands, read_ledger(ledger_path) if ledger_path else None, events)
    console.print(
        f"{rep['n_hands']} hands, missing numbers: {rep['missing_hand_numbers'] or 'none'}, "
        f"flagged: {len(rep['flagged'])}, pot-math problems: {len(rep['hand_problems'])}"
    )
    _print_problems(rep)
    if rep["ledger"] is None:
        console.print("[yellow]no ledger CSV found[/]")
        return
    t = Table(title=f"Ledger cross-check (tolerance 0 cents) — {ledger_path}")
    for col in ("player", "id", "ledger net", "hands net", "delta"):
        t.add_column(col)
    for r in rep["ledger"]:
        t.add_row(
            r["name"],
            r["player_id"],
            money(r["ledger_net"]),
            money(r["hands_net"]),
            f"[{'green' if r['ok'] else 'red'}]{money(r['delta'])}[/]",
        )
    console.print(t)
    if not all(r["ok"] for r in rep["ledger"]):
        raise typer.Exit(1)


@alias_app.command("merge")
def alias_merge(
    id1: str,
    id2: str,
    name: Annotated[str, typer.Option("--name", help="Full name for the merged player.")],
    db_path: DbOpt = db.DEFAULT_DB,
) -> None:
    """Treat two PokerNow player ids as the same person."""
    with db.opened(db_path) as conn:
        canon = db.merge(conn, [id1, id2], name=name)
        members = db.group_ids(conn, canon)
    console.print(f"{name}: canonical {canon} <- {', '.join(members)}")


@alias_app.command("assign")
def alias_assign(
    name: Annotated[str, typer.Argument(help="The person's full name.")],
    refs: Annotated[list[str], typer.Argument(help="Player ids or usernames to attach.")],
    db_path: DbOpt = db.DEFAULT_DB,
) -> None:
    """Attach player ids / usernames to a person (merging with that name's existing ids)."""
    with db.opened(db_path) as conn:
        try:
            ids = db.resolve_player_refs(conn, refs)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        canon = db.assign_names(conn, {name: ids})[name]
        members = db.group_ids(conn, canon)
    console.print(f"{name}: canonical {canon} <- {', '.join(members)}")


@alias_app.command("unnamed")
def alias_unnamed(
    db_path: DbOpt = db.DEFAULT_DB,
    game_id: Annotated[
        str | None, typer.Option("--game", help="Only players seen in this game.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Players without a full name yet (id, PokerNow username, hands)."""
    with db.opened(db_path) as conn:
        rows = db.unnamed_players(conn, game_id)
    if as_json:
        console.print_json(json.dumps(rows))
        return
    if not rows:
        console.print("every player has a full name")
        return
    table = Table("player_id", "username", "hands")
    for r in rows:
        table.add_row(r["player_id"], r["nickname"] or "", str(r["hands"]))
    console.print(table)


@alias_app.command("list")
def alias_list(db_path: DbOpt = db.DEFAULT_DB) -> None:
    """Show canonical players and their PokerNow ids."""
    with db.opened(db_path) as conn:
        groups = db.alias_groups(conn)
    t = Table()
    for col in ("canonical_id", "display name", "ids (id:nickname)"):
        t.add_column(col)
    for g in groups:
        t.add_row(g["canonical_id"], g["display_name"] or "", g["members"])
    console.print(t)


@hands_app.command("list")
def hands_list(
    player: Annotated[str | None, typer.Option(help="Player id (any alias of the person).")] = None,
    game: Annotated[str | None, typer.Option(help="Game id.")] = None,
    limit: Annotated[int, typer.Option(help="Max rows (newest first).")] = 20,
    db_path: DbOpt = db.DEFAULT_DB,
) -> None:
    """List hands, newest first."""
    where, args = [], []
    with db.opened(db_path) as conn:
        if player:
            ids = db.group_ids(conn, player)
            where.append(f"hp.player_id IN ({','.join('?' * len(ids))})")
            args += ids
        if game:
            where.append("h.game_id = ?")
            args.append(game)
        sql = (
            "SELECT h.hand_id, h.game_id, h.hand_no, h.started_at, h.n_players, h.flags, "
            + (
                "hp.position, hp.cards, hp.net "
                if player
                else "NULL position, NULL cards, NULL net "
            )
            + "FROM hands h "
            + ("JOIN hand_players hp ON hp.hand_id = h.hand_id " if player else "")
            + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY h.started_at DESC LIMIT ?"
        )
        rows = conn.execute(sql, [*args, limit]).fetchall()
    t = Table()
    for col in (
        "hand_id",
        "#",
        "started",
        "players",
        "flags",
        *(("pos", "cards", "net") if player else ()),
    ):
        t.add_column(col)
    for r in rows:
        extra = (r["position"] or "", r["cards"] or "", money(r["net"])) if player else ()
        t.add_row(
            r["hand_id"],
            str(r["hand_no"]),
            r["started_at"],
            str(r["n_players"]),
            r["flags"],
            *extra,
        )
    console.print(t)


@hand_app.command("show")
def hand_show(
    hand_id: str,
    db_path: DbOpt = db.DEFAULT_DB,
    as_json: Annotated[bool, typer.Option("--json", help="Print normalized JSON.")] = False,
) -> None:
    """Pretty replay of one hand."""
    with db.opened(db_path) as conn:
        hand = db.get_hand(conn, hand_id)
    if hand is None:
        raise typer.BadParameter(f"hand {hand_id} not found")
    if as_json:
        console.print_json(json.dumps(hand))
    else:
        console.print(hand_replay(hand))
