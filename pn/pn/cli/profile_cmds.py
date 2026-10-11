"""CLI: pn profile <id|name|all> | tips | leaks | compare | vs | priors rebuild."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from pn import ranges
from pn.profile import charts, gtowizard, source
from pn.profile.baselines import baselines_path, load_baselines
from pn.profile.build import build_from_walked, stats_block
from pn.profile.leaks import MIN_LEAK_N
from pn.profile.priors import load_priors, priors_path, rebuild, save_priors
from pn.profile.review import review
from pn.profile.stats import RawStats, aggregate, walk_all
from pn.profile.walk import HandFacts

app = typer.Typer()
priors_app = typer.Typer(help="Population priors (config/population_priors.json).")
app.add_typer(priors_app, name="priors")
charts_app = typer.Typer(help="Preflop charts (config/preflop_charts): show, validate, import.")
app.add_typer(charts_app, name="charts")
baselines_app = typer.Typer(help="GTO baselines (config/gto_baselines.json).")
app.add_typer(baselines_app, name="baselines")
console = Console()

DbOpt = Annotated[str, typer.Option("--db", help="SQLite database path (env PN_DB).")]
DEFAULT_DB = source.default_db()
KEY_STATS = (
    "vpip", "pfr", "three_bet", "fold_to_three_bet", "limp", "steal", "cbet_flop", "cbet_turn",
    "fold_to_cbet_flop", "fold_to_cbet_turn", "check_raise_flop", "af_flop", "af_turn",
    "af_river", "wtsd", "wsd",
)  # fmt: skip
COMPARE_STATS = KEY_STATS[:4] + ("squeeze", "fold_to_squeeze", "call_vs_squeeze") + KEY_STATS[4:]


@dataclass(frozen=True)
class Loaded:
    walked: list[HandFacts]
    alias_map: dict[str, str]
    names: dict[str, str]
    raw_all: dict[str, RawStats]


def _load(db: str) -> Loaded:
    try:
        conn = source.open_db(db)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red] (run `pn fetch` first or pass --db)")
        raise typer.Exit(1) from e
    with conn:
        hands = source.load_hands(conn)
        amap = source.alias_map(conn)
        names = {**source.names_from_hands(hands, amap), **source.display_names(conn)}
    conn.close()
    walked = walk_all(hands)
    return Loaded(walked, amap, names, aggregate(walked, amap))


def _resolve(ld: Loaded, query: str) -> str:
    try:
        return source.resolve_target(
            query, ld.alias_map, {**{c: c for c in ld.raw_all}, **ld.names}
        )
    except source.PlayerNotFound as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e


def _fmt(stat: str, s: dict) -> tuple[str, str, str, str]:
    if stat.startswith("af_"):
        return (f"{s['post_mean']:.2f}", str(s["n"]), f"{s['ci90'][0]:.2f}-{s['ci90'][1]:.2f}",
                f"{s['prior_mean']:.2f}")  # fmt: skip
    return (f"{100 * s['post_mean']:.0f}%", str(s["n"]),
            f"{100 * s['ci90'][0]:.0f}-{100 * s['ci90'][1]:.0f}", f"{100 * s['prior_mean']:.0f}%")  # fmt: skip


COLUMN_ORDER = ("UTG", "UTG1", "UTG2", "UTG3", "LJ", "HJ", "CO", "BTN", "SB", "BB", "STR",
                "BTN_HU", "BB_HU", "IP", "OOP")  # fmt: skip
MATRIX_ROWS = (
    "vpip", "pfr", "rfi", "limp", "cold_call", "three_bet", "fold_to_three_bet", "four_bet",
    "fold_to_four_bet", "five_bet", "fold_to_five_bet", "six_bet", "fold_to_six_bet", "squeeze",
    "steal", "fold_to_steal",
    "cbet_flop", "cbet_turn", "cbet_river", "fold_to_cbet_flop", "fold_to_cbet_turn",
    "fold_to_cbet_river", "raise_cbet_flop", "bet_vs_missed_cbet_flop", "bet_vs_missed_cbet_turn",
    "float_turn", "check_raise_flop", "donk_flop", "probe_turn",
    "agg_freq_flop", "agg_freq_turn", "agg_freq_river", "af_flop", "af_turn", "af_river",
)  # fmt: skip


def _cell(stat: str, s: dict | None) -> str:
    if s is None or s["n"] == 0:
        return ""
    v = f"{s['post_mean']:.1f}" if stat.startswith("af_") else f"{100 * s['post_mean']:.0f}"
    mark = "" if not s.get("dev_gto") or s["n"] < MIN_LEAK_N else ("^" if s["dev_gto"] > 0 else "v")
    return f"{v}{mark} ({s['n']})"


def _print_by_position(p: dict) -> None:
    """PT4-like matrix. Cells: posterior % (opportunities); ^/v = 90% CI above/below GTO."""
    console.rule(f"[bold]{p['display_name']}[/bold] by position ({p['hands']} hands)")
    bp = p["by_position"]
    cols = [c for c in COLUMN_ORDER if c in bp] + sorted(c for c in bp if c not in COLUMN_ORDER)
    t = Table("stat", *cols, title="By position (IP/OOP: postflop)")
    for stat in MATRIX_ROWS:
        cells = [_cell(stat, bp[c].get(stat)) for c in cols]
        if any(cells):
            t.add_row(stat, *cells)
    console.print(t)
    vs = p["by_position_vs_opener"]
    groups = ("EP", "MP", "CO", "BTN", "SB", "BTN_HU")
    pcols = [c for c in COLUMN_ORDER if c in vs]
    t2 = Table("stat vs opener", *pcols, title="Facing an open, by opener group")
    for stat in ("three_bet", "cold_call", "fold_to_steal"):
        for g in groups:
            cells = [_cell(stat, vs[c].get(g, {}).get(stat)) for c in pcols]
            if any(cells):
                t2.add_row(f"{stat} vs {g}", *cells)
    console.print(t2)
    console.print(f"[dim]cell = posterior % (n); ^/v = 90% CI entirely above/below GTO "
                  f"(shown when n >= {MIN_LEAK_N})[/dim]")  # fmt: skip


def _print_preflop_vs_gto(p: dict) -> None:
    t = Table("stat", "player", "n", "GTO (your seats)", "dev", "GTO 6-max table-wide", "dev",
              title="Preflop vs GTO (weighted by the situations you actually faced)")  # fmt: skip
    for stat, e in p["preflop_vs_gto"].items():
        t.add_row(stat, _pct_or(e["player"], stat), str(e["n"]), _pct_or(e["gto_weighted"], stat),
                  _dev(e["dev_gto_weighted"]), _pct_or(e["gto_table_wide"], stat),
                  _dev(e["dev_gto_table_wide"]))  # fmt: skip
    console.print(t)


def _dev(d: float | None) -> str:
    return "-" if d is None else ("0" if d == 0 else f"{100 * d:+.0f}")


def _print_profile(p: dict) -> None:
    arch = p["archetype"]
    console.rule(f"[bold]{p['display_name']}[/bold] ({p['canonical_id']})")
    console.print(f"hands {p['hands']}  showdowns {p['showdowns_seen']}  archetype "
                  f"[bold]{arch['label']}[/bold] (confidence {arch['confidence']:.2f})  "
                  f"priors: {p['priors_source']}")  # fmt: skip
    _print_preflop_vs_gto(p)
    t = Table("stat", "posterior", "n", "90% CI", "pop prior", title="Key stats")
    for stat in KEY_STATS:
        t.add_row(stat, *_fmt(stat, p["stats"][stat]))
    console.print(t)
    rt = Table(
        "pos", "action", "width", "90% CI", "chart", "shown", "range", title="Preflop ranges"
    )
    for pos, acts in p["preflop_range_est"].items():
        for action, e in acts.items():
            rt.add_row(pos, action, f"{100 * e['width_pct']:.0f}%",
                       f"{100 * e['ci90'][0]:.0f}-{100 * e['ci90'][1]:.0f}",
                       f"{100 * e['prior_width_pct']:.0f}%", str(e["evidence_hands"]),
                       e["range_text"][:60])  # fmt: skip
    console.print(rt)
    _print_tips(p)


def _print_tips(p: dict) -> None:
    if not p["tips"]:
        console.print("[dim]No tips: no stat deviates confidently from the population yet.[/dim]")
        return
    for tip in p["tips"]:
        console.print(f"  - {tip}")


@app.command("profile")
def profile_cmd(
    target: Annotated[str, typer.Argument(help="player id, canonical id, name, or `all`")],
    db: DbOpt = DEFAULT_DB,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write JSON (dir for `all`).")
    ] = None,
    min_hands: Annotated[int, typer.Option(help="`all`: skip players with fewer hands.")] = 1,
    by_position: Annotated[
        bool, typer.Option("--by-position", help="PT4-style matrix: stats x positions.")
    ] = False,
) -> None:
    """Show (or write) a player profile; `all` prints a summary of every player."""
    ld = _load(db)
    priors = load_priors()
    if target != "all":
        cid = _resolve(ld, target)
        p = build_from_walked(cid, ld.walked, alias_map=ld.alias_map, names=ld.names,
                              priors=priors, raw_all=ld.raw_all)  # fmt: skip
        if json_out:
            json_out.write_text(json.dumps(p, indent=2))
            console.print(f"wrote {json_out}")
        if by_position:
            _print_by_position(p)
        else:
            _print_profile(p)
        return
    t = Table("player", "id", "hands", "vpip", "pfr", "3bet", "f2cb", "wtsd", "archetype")
    if json_out:
        json_out.mkdir(parents=True, exist_ok=True)
    for cid, raw in sorted(ld.raw_all.items(), key=lambda kv: -kv[1].hands):
        if raw.hands < min_hands:
            continue
        p = build_from_walked(cid, ld.walked, alias_map=ld.alias_map, names=ld.names,
                              priors=priors, raw_all=ld.raw_all)  # fmt: skip
        if json_out:
            (json_out / f"{cid}.json").write_text(json.dumps(p, indent=2))
        s = p["stats"]
        t.add_row(p["display_name"], cid, str(p["hands"]),
                  *(f"{100 * s[k]['post_mean']:.0f}%" for k in
                    ("vpip", "pfr", "three_bet", "fold_to_cbet_flop", "wtsd")),
                  f"{p['archetype']['label']} ({p['archetype']['confidence']:.2f})")  # fmt: skip
    console.print(t)


@app.command("tips")
def tips_cmd(target: str, db: DbOpt = DEFAULT_DB) -> None:
    """Plain-English exploit tips for one player."""
    ld = _load(db)
    cid = _resolve(ld, target)
    p = build_from_walked(cid, ld.walked, alias_map=ld.alias_map, names=ld.names,
                          raw_all=ld.raw_all)  # fmt: skip
    console.rule(f"Tips vs {p['display_name']} ({p['hands']} hands, {p['archetype']['label']})")
    _print_tips(p)


@app.command("vs")
def vs_cmd(a: str, b: str, db: DbOpt = DEFAULT_DB) -> None:
    """Pairwise tendencies between two players (both directions)."""
    ld = _load(db)
    ca, cb = _resolve(ld, a), _resolve(ld, b)
    priors = load_priors()
    t = Table("stat", f"{ld.names.get(ca, ca)} vs {ld.names.get(cb, cb)}",
              f"{ld.names.get(cb, cb)} vs {ld.names.get(ca, ca)}")  # fmt: skip
    ea = build_from_walked(ca, ld.walked, alias_map=ld.alias_map, names=ld.names, priors=priors,
                           raw_all=ld.raw_all)["vs"].get(cb)  # fmt: skip
    eb = build_from_walked(cb, ld.walked, alias_map=ld.alias_map, names=ld.names, priors=priors,
                           raw_all=ld.raw_all)["vs"].get(ca)  # fmt: skip
    if ea is None or eb is None:
        console.print("[yellow]These players were never dealt in together.[/yellow]")
        raise typer.Exit(0)
    t.add_row("hands_together", str(ea["hands_together"]), str(eb["hands_together"]))
    for stat in ("vpip_when_in", "three_bet_vs", "fold_to_their_bet", "call_vs_their_bet"):
        cells = []
        for e in (ea, eb):
            s = e[stat]
            cells.append(f"{100 * s['post_mean']:.0f}% (n={s['n']}, "
                         f"CI {100 * s['ci90'][0]:.0f}-{100 * s['ci90'][1]:.0f})")  # fmt: skip
        t.add_row(stat, *cells)
    t.add_row("net_vs (cents)", str(ea["net_vs"]), str(eb["net_vs"]))
    console.print(t)


def _pct_or(v: float | None, stat: str) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if stat.startswith("af_") else f"{100 * v:.0f}%"


@app.command("leaks")
def leaks_cmd(target: str, db: DbOpt = DEFAULT_DB) -> None:
    """Ranked deviations from the GTO baseline (90% CI excludes GTO)."""
    ld = _load(db)
    cid = _resolve(ld, target)
    p = build_from_walked(cid, ld.walked, alias_map=ld.alias_map, names=ld.names,
                          raw_all=ld.raw_all)  # fmt: skip
    console.rule(f"Leaks: {p['display_name']} ({p['hands']} hands, gto: {p['gto_source']})")
    if not p["leaks"]:
        console.print("[dim]No confident GTO deviations yet.[/dim]")
        return
    t = Table("#", "stat", "spot", "player", "90% CI", "pop", "GTO", "n", "dir", "exploit")
    for i, lk in enumerate(p["leaks"], 1):
        st = lk["stat"]
        lo, hi = lk["ci90"]
        t.add_row(str(i), st, lk["spot"], _pct_or(lk["player_freq"], st),
                  f"{_pct_or(lo, st)}-{_pct_or(hi, st)}", _pct_or(lk["population"], st),
                  _pct_or(lk["gto"], st), str(lk["n"]), lk["direction"], lk["exploit"])  # fmt: skip
    console.print(t)


@app.command("compare")
def compare_cmd(
    targets: Annotated[list[str], typer.Argument(help="Two or more players (id or name).")],
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Side-by-side: each player's posterior vs population vs GTO."""
    ld = _load(db)
    cids = [_resolve(ld, t) for t in targets]
    profiles = [build_from_walked(c, ld.walked, alias_map=ld.alias_map, names=ld.names,
                                  raw_all=ld.raw_all) for c in cids]  # fmt: skip
    t = Table("stat", *(f"{p['display_name']} (n)" for p in profiles), "population", "GTO")
    for stat in COMPARE_STATS:
        first = profiles[0]["stats"][stat]
        cells = []
        for p in profiles:
            s = p["stats"][stat]
            mark = "" if not s["dev_gto"] else (" ^" if s["dev_gto"] > 0 else " v")
            cells.append(f"{_pct_or(s['post_mean'], stat)} ({s['n']}){mark}")
        pop = load_priors()["stats"][stat]["mean"]
        t.add_row(stat, *cells, _pct_or(pop, stat), _pct_or(first["gto"], stat))
    console.print(t)
    console.print("[dim]^ / v: 90% CI entirely above / below GTO.[/dim]")


@app.command("review")
def review_cmd(
    hero: str,
    db: DbOpt = DEFAULT_DB,
    json_out: Annotated[Path | None, typer.Option("--json", help="Write the review JSON.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Full reasons.")] = False,
) -> None:
    """Hero's GTO deviations judged against the actual opponents: justified / leak / unknown."""
    ld = _load(db)
    cid = _resolve(ld, hero)
    priors, bl = load_priors(), load_baselines()
    p = build_from_walked(cid, ld.walked, alias_map=ld.alias_map, names=ld.names,
                          priors=priors, raw_all=ld.raw_all, baselines=bl)  # fmt: skip

    def stats_for(other: str) -> dict:
        return stats_block(ld.raw_all.get(other, RawStats()), priors, bl)

    rows = review(cid, p["leaks"], ld.walked, ld.raw_all, stats_for, ld.alias_map, ld.names)
    if json_out:
        json_out.write_text(json.dumps(rows, indent=2))
        console.print(f"wrote {json_out}")
    console.rule(f"Review: {p['display_name']} ({p['hands']} hands)")
    if not rows:
        console.print("[dim]No confident GTO deviations to review yet.[/dim]")
        return
    t = Table("stat", "spot", "dir", "hero", "GTO", "n", "cost", "verdict", "reason")
    colors = {"justified": "green", "leak": "red", "unknown": "yellow"}
    for r in rows:
        st = r["stat"]
        t.add_row(st, r["spot"], r["direction"], _pct_or(r["player_freq"], st),
                  _pct_or(r["gto"], st), str(r["n"]), f"{r['cost']:.2f}",
                  f"[{colors[r['verdict']]}]{r['verdict']}[/]",
                  r["detail"] if verbose else r["reason"])  # fmt: skip
    console.print(t)
    console.print("[dim]cost = CI-aware deviation from GTO x opportunities (EV proxy).[/dim]")


@priors_app.command("rebuild")
def priors_rebuild(
    db: DbOpt = DEFAULT_DB,
    min_hands: Annotated[
        int, typer.Option(help="Pool players with at least this many hands.")
    ] = 100,
    out: Annotated[Path | None, typer.Option(help="Output path (default: config file).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print, don't write.")] = False,
) -> None:
    """Recompute population priors from the DB (empirical Bayes for k)."""
    ld = _load(db)
    new = rebuild(ld.raw_all, load_priors(), min_hands=min_hands)
    t = Table("stat", "mean", "k", "fit")
    for stat, v in new["stats"].items():
        t.add_row(stat, str(v["mean"]), str(v["k"]), new["_meta"]["fit_report"].get(stat, ""))
    console.print(t)
    console.print(f"pooled players: {new['_meta']['n_players']} (min_hands={min_hands})")
    if dry_run:
        return
    path = save_priors(new, out or priors_path())
    console.print(f"wrote {path}")


@charts_app.command("show")
def charts_show(
    key: Annotated[str, typer.Argument(help='Chart key, e.g. "open BTN" or "3bet BB vs CO".')],
) -> None:
    """Print a chart range with its file and width."""
    files = charts.files_with_key(key)
    if not files:
        console.print(f"[red]no chart key {key!r}[/red]")
        raise typer.Exit(2)
    for name in files:
        text = charts.chart_entries(name)[" ".join(key.split())]
        width = ranges.width(ranges.parse(text))
        console.print(f"[bold]{name}[/bold]  {key}  ({100 * width:.1f}% of hands)")
        console.print(text)


@charts_app.command("validate")
def charts_validate() -> None:
    """Every chart range must parse and be pure (no weights); prints widths."""
    rows = charts.validate_charts()
    t = Table("file", "key", "width", "status")
    for r in rows:
        width = "-" if r["width"] is None else f"{100 * r['width']:.1f}%"
        t.add_row(r["file"], r["key"], width, r["error"] or "ok")
    console.print(t)
    bad = [r for r in rows if r["error"]]
    if bad:
        console.print(f"[red]{len(bad)} invalid chart entries[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{len(rows)} chart entries OK[/green]")


@charts_app.command("import")
def charts_import(
    src: Annotated[Path, typer.Argument(help="File with an exported range (GTO Wizard/Pio).")],
    key: Annotated[str, typer.Option("--key", help='Chart key to replace, e.g. "open CO".')],
    file: Annotated[str | None, typer.Option("--file", help="Chart file (6max.txt, ...).")] = None,
) -> None:
    """Import an exported range as a PURE chart entry (frequency >= 0.5 -> in)."""
    try:
        pure = charts.pure_range(src.read_text())
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot import {src}: {e}[/red]")
        raise typer.Exit(2) from e
    targets = [file] if file else charts.files_with_key(key)
    if len(targets) != 1:
        where = ", ".join(targets) or "no file"
        console.print(f"[red]key {key!r} is in {where}; pass --file to choose one[/red]")
        raise typer.Exit(2)
    if targets[0] not in charts.CHART_FILES:
        console.print(f"[red]unknown chart file {targets[0]!r}[/red]")
        raise typer.Exit(2)
    text = ranges.to_text(pure)
    charts.write_chart_key(targets[0], key, text)
    console.print(f"wrote {targets[0]}: {key} = {text} ({100 * ranges.width(pure):.1f}%)")


@baselines_app.command("import-gtowizard")
def baselines_import_gtowizard(
    freqs: Annotated[Path | None, typer.Option(help="GTO Wizard freqs JSON.")] = None,
    depth: Annotated[int, typer.Option(help="Stack depth (bb) of the spots to use.")] = 100,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print, don't write.")] = False,
) -> None:
    """Replace preflop GTO baselines with values computed from GTO Wizard frequencies."""
    src = freqs or gtowizard.freqs_path()
    pre = gtowizard.preflop_baselines(gtowizard.load_freqs(src), depth)
    t = Table("stat", "default", "by position")
    for stat, e in sorted(pre.items()):
        t.add_row(stat, _pct_or(e["default"], stat),
                  ", ".join(f"{p} {_pct_or(v, stat)}" for p, v in e["by_position"].items()))  # fmt: skip
    console.print(t)
    if dry_run:
        return
    label = f"gtowizard:{src.name}@{depth}bb"
    merged = gtowizard.merge_into(load_baselines(), pre, label)
    baselines_path().write_text(json.dumps(merged, indent=2) + "\n")
    console.print(f"wrote {baselines_path()} (preflop source {label})")
