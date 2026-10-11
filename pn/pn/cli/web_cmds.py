"""CLI: `pn web` — local study UI (FastAPI + static SPA)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pn.profile.source import default_db

app = typer.Typer()


@app.command("web")
def web_cmd(
    db: Annotated[str, typer.Option("--db", help="SQLite database path (env PN_DB).")] = default_db(),
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    host: Annotated[str, typer.Option("--host", help="Bind address (local only by default).")] = "127.0.0.1",
    solves: Annotated[Path | None, typer.Option("--solves", help="Solver JSON dir (default data/solves).")] = None,
) -> None:  # fmt: skip
    """Serve the study UI: players, profiles, review, hands, preflop charts, strategy viewer."""
    try:
        import uvicorn

        from pn.web.app import create_app
    except ImportError as e:
        typer.echo(f"web extras missing ({e}); install with: uv pip install -e 'pn[web]'", err=True)
        raise typer.Exit(1) from e
    if not Path(db).exists():
        typer.echo(f"warning: database {db} not found; DB pages will show an error", err=True)
    typer.echo(f"pn web: http://{host}:{port}/  (db={db})")
    uvicorn.run(create_app(db, solves), host=host, port=port, log_level="warning")
