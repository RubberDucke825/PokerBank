"""CLI: solver glue.

pn spot <hand_id> --street flop [--hero ID] [--range PID=TEXT] [--out f.toml]   GTO ranges
pn solve new --seats BTN,BB --depth 100 [--board Kd2cKh --line srp] [--out f.toml]   anonymous spot
pn solve run <spot.toml> [--preset review]   solve through the cache (TexasSolver), print key + path
pn library fill [--flops 50] [--configs 6] [--dry-run]   trainer heads-up flop library -> cache
pn library status   flop library coverage per matchup
pn live now <game> --hero ID [--cards AhKd]   one recommendation for the hand in progress
pn live watch <game> --hero ID                poll and print range-level recommendations
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from pn.profile import source

app = typer.Typer()
live_app = typer.Typer(no_args_is_help=True, help="Live assist from a PokerNow game log.")
solve_app = typer.Typer(no_args_is_help=True, help="Build and solve spots through the cache.")
library_app = typer.Typer(no_args_is_help=True, help="The trainer's heads-up flop library.")
app.add_typer(live_app, name="live")
app.add_typer(solve_app, name="solve")
app.add_typer(library_app, name="library")
console = Console()

DbOpt = Annotated[str, typer.Option("--db", help="SQLite path (env PN_DB).")]
HERO_DEFAULT = "HERO"
DEFAULT_DB = source.default_db()


def _fail(msg: str) -> typer.Exit:
    console.print(f"[red]{msg}[/red]")
    return typer.Exit(1)


RangeOpt = Annotated[list[str] | None, typer.Option("--range", help="KEY=range text (repeatable)")]


def _pairs(items: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, text = item.partition("=")
        if not sep or not key.strip() or not text.strip():
            raise _fail(f"{what} must look like KEY=text: {item!r}")
        out[key.strip()] = text.strip()
    return out


@app.command("spot")
def spot_cmd(
    hand_id: Annotated[str, typer.Argument(help="hand id from the DB")],
    street: Annotated[str, typer.Option(help="preflop|flop|turn|river")] = "flop",
    hero: Annotated[str | None, typer.Option(help="hero player id (default: hand's hero)")] = None,
    range_: RangeOpt = None,
    cards: Annotated[str | None, typer.Option(help="hero cards, e.g. AhKd")] = None,
    out: Annotated[Path | None, typer.Option(help="write the TOML here")] = None,
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Build a solver spot (contracts/tree_config.md) from a real hand with GTO chart ranges.
    --range PID=TEXT replaces that player's range."""
    from pn.adapter.spot import build_spot
    from pn.live.assist import parse_cards

    try:
        spot = build_spot(db, hand_id, street, hero, overrides=_pairs(range_, "--range"),
                          hero_cards=parse_cards(cards))  # fmt: skip
    except ValueError as e:
        raise _fail(str(e)) from e
    for w in spot.warnings:
        console.print(f"[yellow]{w}[/yellow]")
    if out:
        out.write_text(spot.toml)
        console.print(f"wrote {out}")
    else:
        print(spot.toml)


# ------------------------------------------------------------------ solve / library


@solve_app.command("new")
def solve_new(
    seats: Annotated[str, typer.Option(help="comma list of positions, e.g. BTN,BB")],
    depth: Annotated[int, typer.Option(help="stack depth in big blinds")] = 100,
    table: Annotated[int, typer.Option(help="6 or 8")] = 6,
    board: Annotated[str | None, typer.Option(help="e.g. Kd2cKh (none = preflop root)")] = None,
    hero: Annotated[str | None, typer.Option(help="hero position")] = None,
    line: Annotated[str | None, typer.Option(help="postflop: srp | 3bp")] = None,
    range_: RangeOpt = None,
    out: Annotated[Path | None, typer.Option(help="write the TOML here")] = None,
) -> None:
    """Spot with anonymous seats: any-two at the preflop root; postflop chart ranges for --line
    (--range POS=TEXT replaces a seat's range)."""
    from pn.adapter.validate import to_toml
    from pn.solve import build_spot
    from pn.solve.request import new_request, parse_board

    try:
        req = new_request([p.strip().upper() for p in seats.split(",") if p.strip()], depth,
                          table=table, board=parse_board(board),
                          hero=hero.upper() if hero else None, line=line,
                          seat_ranges={k.upper(): v for k, v in _pairs(range_, "--range").items()})  # fmt: skip
        text = to_toml(build_spot(req), header=f"pn solve new: seats={seats} depth={depth}bb "
                                               f"table={table} GTO")  # fmt: skip
    except ValueError as e:
        raise _fail(str(e)) from e
    if out:
        out.write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@solve_app.command("run")
def solve_run(
    spot_path: Annotated[Path, typer.Argument(help="spot TOML (contracts/tree_config.md)")],
    backend: Annotated[str | None, typer.Option(help="texas (the only backend)")] = None,
    preset: Annotated[str | None, typer.Option(
        help="trainer | review | library: budget, backend and heads-up cap (default: the TOML's)")] = None,
) -> None:
    """Solve a spot through the cache (data/solves/cache); prints the key and document path."""
    import tomllib

    from pn.adapter.validate import validate_config
    from pn.solve import SolveCache, SolveError, spot_key
    from pn.solve.request import apply_preset

    try:
        spot = tomllib.loads(spot_path.read_text())
        if preset is not None:
            spot, forced = apply_preset(spot, preset)
            backend = backend or forced
        validate_config(spot)
    except (OSError, ValueError) as e:
        raise _fail(str(e)) from e
    cache = SolveCache()
    key = spot_key(spot)
    try:
        doc = cache.solve(spot, backend)
    except (SolveError, ValueError) as e:
        raise _fail(str(e)) from e
    console.print(f"key {key} backend {doc.get('meta', {}).get('backend')}")
    print(cache.path(key))


@library_app.command("status")
def library_status(
    table: Annotated[int, typer.Option(help="table size")] = 6,
    depth: Annotated[int, typer.Option(help="stack depth in bb")] = 100,
    flops: Annotated[int, typer.Option(help="planned flops per matchup")] = 50,
    db: DbOpt = DEFAULT_DB,
) -> None:
    """The trainer's heads-up flop library: solved / planned boards per matchup, by expected
    burn."""
    from pn.solve.cache import cache_dir
    from pn.solve.request import TABLE_SEATS
    from pn.train import flop_library as flib
    from pn.train import library_fill as lf

    if table not in TABLE_SEATS:
        raise _fail(f"--table must be one of {sorted(TABLE_SEATS)}")
    ms = flib.matchups(TABLE_SEATS[table], depth, f"{table}max", sb=50, bb=100)
    burn = lf.load_burns(ms, table, depth, db)
    index = flib.FlopIndex(cache_dir())
    rows = lf.coverage(ms, index, burn, flops)
    covered = sum(r["solved"] > 0 for r in rows)
    console.print(f"heads-up flop library {cache_dir()}: {covered}/{len(rows)} matchups with a "
                  f"solved board, {sum(r['solved'] for r in rows)} boards")  # fmt: skip
    acc = ", ".join(f"{k}% pot: {n}" for k, n in lf.accuracy_counts(index).items())
    console.print(f"  boards by accuracy: {acc or 'none'}")
    for r in rows:
        console.print(f"  {r['solved']:>4}/{r['planned']:<4} burn {r['burn']:>8.3f}  {r['label']}")


def _shard(text: str) -> tuple[int, int]:
    """`i/n` -> (i, n) with 0 <= i < n; typer.Exit(1) otherwise."""
    try:
        i_s, n_s = text.split("/")
        i, n = int(i_s), int(n_s)
    except ValueError:
        raise _fail("bad --shard: want i/n, e.g. 0/4") from None
    if n < 1 or not 0 <= i < n:
        raise _fail("bad --shard: want 0 <= i < n")
    return i, n


@library_app.command("fill")
def library_fill(
    flops: Annotated[int, typer.Option(help="texture-stratified canonical flops per matchup")] = 50,
    configs: Annotated[int, typer.Option(help="top matchups by expected burn (0 = all)")] = 6,
    table: Annotated[int, typer.Option(help="table size")] = 6,
    depth: Annotated[int, typer.Option(help="stack depth in bb")] = 100,
    preset: Annotated[str, typer.Option(help="solve preset (the trainer reads `library`)")] = "library",
    backend: Annotated[str, typer.Option(help="solver backend (the trainer reads `texas`)")] = "texas",
    accuracy: Annotated[list[float] | None, typer.Option(
        help="accuracy passes in % of pot, run in order (e.g. 1 0.5 0.3); default the preset's")] = None,
    pots: Annotated[list[str] | None, typer.Option(
        help="pot types to fill (limped, srp, 3bp, 4bp+), repeatable; default all")] = None,
    db: DbOpt = DEFAULT_DB,
    shard: Annotated[str | None, typer.Option(
        help="solve only every n-th item of the order starting at i, as `i/n` (0 <= i < n); "
             "shards on other machines each need their own PN_SOLVE_CACHE, merged by copying")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print the order, solve nothing")] = False,
) -> None:
    """Solve the trainer's heads-up flop library (pn.train.library_fill): matchups by expected
    burn x texture-stratified flops, most valuable first. Resumable (cached boards are
    skipped), one solve at a time, preempted while trainer solves wait. `--accuracy` runs the
    whole order once per pass, coarse to fine; a board already finer is left alone. Run it in
    the background, e.g. `nohup pn library fill > fill.log 2>&1 &`. `--shard i/n` takes every
    n-th item of the same order, so n fills on separate cache roots split the work
    (tools/cloud_fill.sh)."""
    from pn.solve import SolveCache
    from pn.solve.cache import cache_dir
    from pn.solve.request import TABLE_SEATS
    from pn.train import flop_library as flib
    from pn.train import library_fill as lf
    from pn.train.spots import POTS

    if preset != flib.LIBRARY_PRESET or backend != flib.LIBRARY_BACKEND:
        raise _fail(f"the trainer deals from --preset {flib.LIBRARY_PRESET} --backend "
                    f"{flib.LIBRARY_BACKEND} documents only")  # fmt: skip
    if table not in TABLE_SEATS or flops < 1 or configs < 0 or any(a <= 0 for a in accuracy or []) \
            or any(p not in POTS for p in pots or []):
        raise _fail("bad --table, --flops, --configs, --accuracy or --pots")
    ms = flib.matchups(TABLE_SEATS[table], depth, f"{table}max", sb=50, bb=100)
    if pots:
        ms = [m for m in ms if m.pot in pots]
    burn = lf.load_burns(ms, table, depth, db)
    items = lf.plan(ms, burn, lf.stratified_flops(flops), configs or None)
    if shard is not None:
        i, n = _shard(shard)
        items = items[i::n]
    passes: list[float | None] = list(accuracy) if accuracy else [None]
    if dry_run:
        for it in items:
            console.print(f"{it.burn:8.3f}  {it.flop.flop}  {it.matchup.label}")
        tag = f" (shard {shard})" if shard is not None else ""
        console.print(f"{len(items)} solves planned{tag}")
        return
    root = cache_dir()
    cache = SolveCache(root, runner=lf.preemptible_runner(root))
    results: list[lf.FillResult] = []
    try:
        with lf.single_fill(root):
            for acc in passes:
                if acc is not None:
                    console.print(f"--- pass: accuracy {acc:g}% pot")
                results += lf.fill(items, cache, flib.FlopIndex(root), backend=backend, accuracy=acc,
                                   on_result=lambda r: console.print(
                                       f"{r.status:7} {r.flop} {r.label} {r.detail}"))  # fmt: skip
    except RuntimeError as e:
        raise _fail(str(e)) from e
    done = sum(r.status in lf.DONE_STATES for r in results)
    console.print(f"{done}/{len(results)} boards in the library")


@library_app.command("reindex")
def library_reindex(
    table: Annotated[int, typer.Option(help="table size")] = 6,
    depth: Annotated[int, typer.Option(help="stack depth in bb")] = 100,
) -> None:
    """Re-key the flop index to the current matchup ids (an id hashes the flop SpotRequest,
    so a change to the request shape strands every recorded board)."""
    from pn.solve.cache import cache_dir
    from pn.solve.request import TABLE_SEATS
    from pn.train import flop_library as flib

    if table not in TABLE_SEATS:
        raise _fail(f"--table must be one of {sorted(TABLE_SEATS)}")
    ms = flib.matchups(TABLE_SEATS[table], depth, f"{table}max", sb=50, bb=100)
    moved = flib.FlopIndex(cache_dir()).rekey(ms)
    console.print(f"{moved} matchups re-keyed in {cache_dir() / flib.INDEX_NAME}")


# ------------------------------------------------------------------ live


def _live_parts(game: str, db: str, hero: str):
    from pn.adapter.spot import ProfileStore
    from pn.ingest.fetch import fetch_game_log, parse_game_id
    from pn.ingest.parse import parse_messages
    from pn.live.assist import cache_solver

    game_id = parse_game_id(game)

    def fetch() -> list[dict]:
        return parse_messages(fetch_game_log(game_id, refresh=True), game_id=game_id, hero_id=hero)

    profiles = ProfileStore(db) if Path(db).exists() else None
    return fetch, cache_solver(), profiles


@live_app.command("now")
def live_now(
    game: Annotated[str, typer.Argument(help="PokerNow game url or id")],
    hero: Annotated[str, typer.Option(help="hero player id")] = HERO_DEFAULT,
    cards: Annotated[str | None, typer.Option(help="hero cards, e.g. AhKd")] = None,
    db: DbOpt = DEFAULT_DB,
) -> None:
    """One recommendation for hero's pending decision in the hand in progress."""
    from pn.live.assist import current_hand, parse_cards, recommend, render

    fetch, solve, profiles = _live_parts(game, db, hero)
    hand = current_hand(fetch())
    if hand is None:
        raise _fail("no hand in progress")
    try:
        rec = recommend(hand, hero, solve, hero_cards=parse_cards(cards), profiles=profiles)
    except ValueError as e:
        raise _fail(str(e)) from e
    if rec is None:
        raise _fail(f"hero {hero} is not to act in hand {hand['hand_id']}")
    print(render(rec))


@live_app.command("watch")
def live_watch(
    game: Annotated[str, typer.Argument(help="PokerNow game url or id")],
    hero: Annotated[str, typer.Option(help="hero player id")] = HERO_DEFAULT,
    interval: Annotated[float, typer.Option(help="seconds between polls")] = 5.0,
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Poll the log; print a range-level recommendation at each new hero decision."""
    from pn.live.assist import watch

    fetch, solve, profiles = _live_parts(game, db, hero)
    watch(fetch, hero, solve, profiles=profiles, interval=interval)
