"""CLI: solver glue.

pn spot <hand_id> --street flop [--hero ID] [--range PID=TEXT] [--out f.toml]   GTO ranges
pn solve new --seats BTN,BB --depth 100 [--board Kd2cKh --line srp] [--out f.toml]   anonymous spot
pn solve run <spot.toml> [--preset review]   solve through the cache (TexasSolver), print key + path
pn library fill [--flops 50 | --subset 184] [--lib 8x100 ...] [--new-only] [--configs 6] [--dry-run]
    trainer heads-up flop library -> cache
pn library status   flop library coverage per matchup (+ solved / aliased / missing per table/depth)
pn library alias --table 8 --depth 100   8-max lines without UTG/UTG1 action read the 6-max boards
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
    flops: Annotated[str, typer.Option(help="planned flops per matchup (a count or `all`)")] = "50",
    subset: Annotated[int | None, typer.Option(help="planned: the weighted N-flop subset")] = None,
    db: DbOpt = DEFAULT_DB,
) -> None:
    """The trainer's heads-up flop library: solved / planned boards per matchup, by expected
    burn."""
    from pn.solve.cache import cache_dir
    from pn.solve.request import TABLE_SEATS
    from pn.train import flop_library as flib
    from pn.train import library_alias as la
    from pn.train import library_fill as lf

    if table not in TABLE_SEATS:
        raise _fail(f"--table must be one of {sorted(TABLE_SEATS)}")
    ms = flib.matchups(TABLE_SEATS[table], depth, f"{table}max", sb=50, bb=100)
    burn = lf.load_burns(ms, table, depth, db)
    index = flib.FlopIndex(cache_dir())
    rows = lf.coverage(ms, index, burn, subset if subset is not None else len(_flops(flops)))
    covered = sum(r["solved"] > 0 for r in rows)
    console.print(f"heads-up flop library {cache_dir()}: {covered}/{len(rows)} matchups with a "
                  f"solved board, {sum(r['solved'] for r in rows)} boards")  # fmt: skip
    acc = ", ".join(f"{k}% pot: {n}" for k, n in lf.accuracy_counts(index).items())
    console.print(f"  boards by accuracy: {acc or 'none'}")
    for t, d in la.LIBS:  # Plan 16: every table / depth the trainer deals
        console.print(f"  {la.summary_line(la.coverage(index, t, d))}")
    state = {r["mid"]: r for r in la.coverage(index, table, depth)["rows"]}
    for r in rows:
        s = state[r["mid"]]
        tag = "skipped" if s["skipped"] else s["state"]
        console.print(f"  {r['solved']:>4}/{r['planned']:<4} burn {r['burn']:>8.3f}  {tag:<7}  {r['label']}")


@library_app.command("alias")
def library_alias(
    table: Annotated[int, typer.Option(help="table size of the aliased matchups")] = 8,
    depth: Annotated[int, typer.Option(help="stack depth in bb")] = 100,
    sample: Annotated[int, typer.Option(
        help="indexed boards per target whose recorded key is re-derived (0 = all)")] = 25,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="check only, write nothing")] = False,
) -> None:
    """Alias every 8-max matchup without UTG / UTG1 action to the 6-max matchup of the same line
    and depth (Plan 16 P2: identical flop keys, so the trainer deals the 6-max boards). Checks
    the keys first (probe boards + `--sample` indexed boards); one refusal writes nothing.
    Idempotent."""
    from pn.solve.cache import cache_dir
    from pn.solve.request import TABLE_SEATS
    from pn.train import flop_library as flib
    from pn.train import library_alias as la
    from pn.train.model import DEPTHS

    if table not in TABLE_SEATS or table == 6 or depth not in DEPTHS:
        raise _fail(f"--table must be one of {sorted(set(TABLE_SEATS) - {6})}, --depth one of {DEPTHS}")
    if not dry_run:
        _no_running_fill(cache_dir())
    index = flib.FlopIndex(cache_dir())
    rows = la.run(index, table, depth, sample=sample, dry_run=dry_run)
    for r in rows:
        if r.status in ("refused", "solved"):
            console.print(f"  {r.status:7} {r.matchup.label}: {r.detail}")
    n = {s: sum(r.status == s for r in rows) for s in ("new", "present", "solved", "refused")}
    verb = "would write" if dry_run else "wrote"
    if n["refused"]:
        raise _fail(f"{n['refused']} refused: no alias written ({len(rows)} candidates)")
    console.print(f"{table}max/{depth}bb aliases in {index.path}: {verb} {n['new']}, "
                  f"{n['present']} present, {n['solved']} solved on their own")  # fmt: skip


def _no_running_fill(root) -> None:
    """typer.Exit(1) while a fill or --cached-only walk runs on the root: its final index write
    (from its own snapshot) would erase this command's entries."""
    from pn.train import library_fill as lf

    if (pid := lf.running_fill(root)) is not None:
        raise _fail(f"a library fill is running on {root} (pid {pid}): its final index write would "
                    "undo this one; rerun when it ends")  # fmt: skip


def _flops(text: str):
    """`--flops`: `all` = every canonical flop heaviest first, else n texture-stratified ones."""
    from pn.train import library_fill as lf

    if text == "all":
        return lf.all_flops()
    try:
        n = int(text)
    except ValueError:
        raise _fail("bad --flops: want a count or `all`") from None
    if n < 1:
        raise _fail("bad --flops: want a count >= 1 or `all`")
    return lf.stratified_flops(n)


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


def _libs(lib: list[str] | None, table: int, depth: int) -> list[tuple[int, int]]:
    """`--lib 8x200` (repeatable), else the single `--table` x `--depth` library."""
    from pn.train import library_fill as lf

    try:
        return [lf.parse_lib(x) for x in lib] if lib else [lf.parse_lib(f"{table}x{depth}")]
    except ValueError as e:
        raise _fail(str(e)) from None


@library_app.command("fill")
def library_fill(
    flops: Annotated[str | None, typer.Option(
        help="texture-stratified canonical flops per matchup, or `all` (1,755, heaviest first; "
             "the index marks those matchups full); default 50")] = None,
    subset: Annotated[int | None, typer.Option(
        help="the weighted N-flop subset (texture x high card; Plan 16: 184) instead of --flops; "
             "recorded on each matchup entry")] = None,
    configs: Annotated[int, typer.Option(help="top matchups by expected burn (0 = all)")] = 6,
    table: Annotated[int, typer.Option(help="table size")] = 6,
    depth: Annotated[int, typer.Option(help="stack depth in bb")] = 100,
    lib: Annotated[list[str] | None, typer.Option(
        help="libraries TABLExDEPTH, repeatable (e.g. --lib 8x100 --lib 6x200): one order over "
             "all of them; replaces --table/--depth")] = None,
    new_only: Annotated[bool, typer.Option(
        "--new-only", help="skip limped pots and 8-max lines that alias the 6-max line of the "
                           "same depth (Plan 16 budget decision)")] = False,
    preset: Annotated[str, typer.Option(help="solve preset (the trainer reads `library`)")] = "library",
    backend: Annotated[str, typer.Option(help="solver backend (the trainer reads `texas`)")] = "texas",
    accuracy: Annotated[list[float] | None, typer.Option(
        help="accuracy passes in % of pot, run in order (e.g. 1 0.5 0.3); default the preset's")] = None,
    depth_accuracy: Annotated[list[str] | None, typer.Option(
        help="DEPTH=PCT, repeatable: the accuracy for that depth's matchups instead of "
             "--accuracy (Plan 16: 200=2)")] = None,
    pots: Annotated[list[str] | None, typer.Option(
        help="pot types to fill (limped, srp, 3bp, 4bp+), repeatable; default all")] = None,
    db: DbOpt = DEFAULT_DB,
    shard: Annotated[str | None, typer.Option(
        help="solve only every n-th item of the order starting at i, as `i/n` (0 <= i < n); "
             "shards on other machines each need their own PN_SOLVE_CACHE, merged by copying")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print the order, solve nothing")] = False,
    cached_only: Annotated[bool, typer.Option(
        "--cached-only", help="record documents already in the cache (pulled from elsewhere) into "
                              "the index, solve nothing")] = False,
    reverse: Annotated[bool, typer.Option(
        "--reverse", help="walk the order from its light end (so a second set of shards meets the "
                          "first in the middle without overlap)")] = False,
) -> None:
    """Solve the trainer's heads-up flop library (pn.train.library_fill): matchups by expected
    burn x texture-stratified flops, most valuable first. Resumable (cached boards are
    skipped), one solve at a time, preempted while trainer solves wait. `--accuracy` runs the
    whole order once per pass, coarse to fine; a board already finer is left alone. Run it in
    the background, e.g. `nohup pn library fill > fill.log 2>&1 &`. `--shard i/n` takes every
    n-th item of the same order, so n fills on separate cache roots split the work
    (tools/cloud_fill.sh). `--dry-run` with `--accuracy` also counts the boards still missing at
    the finest pass, and prints per library the matchup and board counts and one sample flop key
    checked against the trainer's lookup; `--cached-only` indexes pulled documents (one index
    write); `--reverse` walks the order backwards, so a forward and a reverse shard set share one
    pool without overlap. Fails before planning when an input the keys hash is missing."""
    from pn.solve import SolveCache
    from pn.solve.cache import cache_dir
    from pn.train import flop_library as flib
    from pn.train import library_fill as lf
    from pn.train import preflop as pre
    from pn.train.spots import POTS

    libs = _libs(lib, table, depth)
    if not pre.has_freqs():
        raise _fail("GTO Wizard frequencies missing (data/gtowizard): the plan would hash default "
                    "preflop sizes and solve boards no machine with the file can use")
    if missing := lf.missing_inputs(libs):
        raise _fail(f"missing inputs the flop keys hash: {', '.join(missing)}")
    if preset != flib.LIBRARY_PRESET or backend != flib.LIBRARY_BACKEND:
        raise _fail(f"the trainer deals from --preset {flib.LIBRARY_PRESET} --backend "
                    f"{flib.LIBRARY_BACKEND} documents only")  # fmt: skip
    if configs < 0 or any(a <= 0 for a in accuracy or []) or any(p not in POTS for p in pots or []):
        raise _fail("bad --configs, --accuracy or --pots")
    if subset is not None and flops is not None:
        raise _fail("--subset replaces --flops: pass one")
    try:
        per_depth = lf.parse_depth_accuracy(depth_accuracy or [])
    except ValueError as e:
        raise _fail(str(e)) from None
    try:
        flop_list = lf.subset_flops(subset) if subset is not None else _flops(flops or "50")
    except ValueError as e:
        raise _fail(f"bad --subset: {e}") from None
    ms: list[flib.Matchup] = []
    burn: dict[str, float] = {}
    notes: list[str] = []
    for t, d in libs:
        part = lf.lib_matchups(t, d)
        n_all, skipped = len(part), ""
        if new_only:
            part, n_alias, n_limp = lf.new_lines(t, d, part)
            skipped = f" ({n_alias} aliased, {n_limp} limped skipped)"
        if pots:
            part = [m for m in part if m.pot in pots]
        burn.update(lf.load_burns(part, t, d, db))
        ms += part
        notes.append(f"{t}max/{d}: {len(part)} of {n_all} matchups{skipped} x {len(flop_list)} boards")
    items = lf.plan(ms, burn, flop_list, configs or None)
    if reverse:
        items = items[::-1]
    if shard is not None:
        i, n = _shard(shard)
        items = items[i::n]
    passes: list[float | None] = list(accuracy) if accuracy else [None]
    if dry_run:
        _dry_run(items, notes, libs, shard, accuracy, per_depth)
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
                                   cached_only=cached_only, subset=subset, depth_accuracy=per_depth,
                                   full=flops == "all",
                                   on_result=lambda r: r.status in ("missing", "aliased") or console.print(
                                       f"{r.status:7} {r.flop} {r.label} {r.detail}"))  # fmt: skip
    except RuntimeError as e:
        raise _fail(str(e)) from e
    done = sum(r.status in lf.DONE_STATES for r in results)
    n_alias = sum(r.status == "aliased" for r in results)
    console.print(f"{done}/{len(results)} boards in the library"
                  + (f" ({n_alias} of aliased matchups skipped)" if n_alias else ""))  # fmt: skip


def _dry_run(items, notes: list[str], libs: list[tuple[int, int]], shard: str | None,
             accuracy: list[float] | None, per_depth: dict[int, float]) -> None:  # fmt: skip
    """The planned order, then per library its counts and one sample flop key (first label and
    board, so every machine prints the same one) compared with the trainer's lookup of the same
    line (pn.train.library_fill.trainer_key), then the total."""
    from pn.solve import flops as fl
    from pn.solve.cache import cache_dir
    from pn.train import flop_library as flib
    from pn.train import library_fill as lf

    for it in items:
        console.print(f"{it.burn:8.3f}  {it.flop.flop}  {it.matchup.table_kind}  {it.matchup.label}")
    finest = min(accuracy) if accuracy else None

    def target(depth: int) -> float | None:
        return per_depth.get(depth, finest)

    for (t, d), note in zip(libs, notes, strict=True):
        acc = target(d)
        console.print(f"{note} at {f'{acc:g}% pot' if acc is not None else 'the preset accuracy'}")
        mine = [x for x in items if x.matchup.table_kind == f"{t}max" and x.matchup.depth == d]
        if not mine:
            continue
        it = min(mine, key=lambda x: (x.matchup.label, x.flop.flop))  # not by burn: burns differ by machine
        board = tuple(fl.flop_cards(it.flop.flop))
        key = flib.flop_spot(it.matchup, board, target(d))[2]
        mid, tkey = lf.trainer_key(it.matchup, board, target(d))
        same = "same" if (mid, tkey) == (it.matchup.mid, key) else "DIFFERENT"
        console.print(f"  sample {t}max/{d} {it.matchup.label} {it.flop.flop}: mid {it.matchup.mid} "
                      f"key {key}; trainer lookup {same}")  # fmt: skip
    tag = f" (shard {shard})" if shard is not None else ""
    if accuracy or per_depth:
        index = flib.FlopIndex(cache_dir())
        left = sum(1 for it in items if lf.indexed(index, it.matchup, it.flop.flop, target(it.matchup.depth)) is None)
        tag += f", {left} remaining" + (f" at {finest:g}% pot" if finest is not None and not per_depth else "")
    console.print(f"{len(items)} solves planned{tag}")


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
    _no_running_fill(cache_dir())
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
