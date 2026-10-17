"""`pn` command-line entry point. Each subsystem registers its own typer app here."""

import typer

from pn.cli.cache_cmds import app as cache_app
from pn.cli.ev_cmds import app as ev_app
from pn.cli.ingest_cmds import app as ingest_app
from pn.cli.positional_cmds import app as positional_app
from pn.cli.profile_cmds import app as profile_app
from pn.cli.solve_cmds import app as solve_app
from pn.cli.train_cmds import app as train_app
from pn.cli.web_cmds import app as web_app

app = typer.Typer(no_args_is_help=True, help="PokerNow ingestion, profiling and solver tools.")
app.add_typer(ingest_app)
app.add_typer(profile_app)
app.add_typer(solve_app)
app.add_typer(positional_app)
app.add_typer(web_app)
app.add_typer(ev_app)
app.add_typer(train_app)
app.add_typer(cache_app)

if __name__ == "__main__":
    app()
