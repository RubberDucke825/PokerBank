"""CLI: the SolveCache on disk (Plan 17).

pn cache compress [--dry-run] [--limit N] [--min-age MIN]   plain <key>.json -> verified <key>.json.gz
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer()
cache_app = typer.Typer(no_args_is_help=True, help="The solve cache on disk.")
app.add_typer(cache_app, name="cache")
console = Console()


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


@cache_app.command("compress")
def compress(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="count, convert nothing")] = False,
    limit: Annotated[int | None, typer.Option(help="convert at most N documents")] = None,
    min_age: Annotated[float, typer.Option(
        help="skip plain files modified in the last MIN minutes (a pull may be writing them)")] = 10,
) -> None:
    """Convert the plain documents of the solve cache (PN_SOLVE_CACHE) to gzip in place: each is
    written to a temp file, fsynced, verified against the original, renamed, and only then is the
    plain file deleted, so a kill at any point leaves complete files. Idempotent and resumable;
    the flop index, registry index, .toml requests and lazydoc roots are never touched. Each file
    is converted under the cache's solve lock, so a running solve or fill only delays it."""
    from pn.solve import docstore
    from pn.solve.cache import cache_dir

    if (limit is not None and limit < 1) or min_age < 0:
        console.print("[red]bad --limit or --min-age[/red]")
        raise typer.Exit(1)
    root = cache_dir()
    rep = docstore.compress(root, dry_run=dry_run, limit=limit, min_age_sec=min_age * 60)
    if dry_run:
        console.print(f"{root}: {rep.converted} plain documents to convert ({_gb(rep.before_bytes)}), "
                      f"{rep.duplicates} leftover plain copies, {rep.recent} too recent")  # fmt: skip
        return
    ratio = f" ({rep.after_bytes / rep.before_bytes:.3f})" if rep.before_bytes else ""
    console.print(f"{root}: {rep.converted} converted, {_gb(rep.before_bytes)} -> {_gb(rep.after_bytes)}"
                  f"{ratio}; {rep.duplicates} leftover plain copies removed, {rep.recent} too recent "
                  f"or rewritten meanwhile, {rep.vanished} gone meanwhile, {rep.failed} failed (left plain)")  # fmt: skip
    if rep.stopped:
        console.print("[red]stopped early: disk full (free space, then rerun)[/red]")
    if rep.failed:
        raise typer.Exit(1)
