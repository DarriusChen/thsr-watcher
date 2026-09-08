"""Command-line entry point for the project foundation."""

import typer

app = typer.Typer(help="Personal-use THSR ticket availability watcher.")


@app.command()
def main() -> None:
    """Display the current project status."""
    typer.echo("THSR watcher foundation ready. Ticket monitoring is not implemented yet.")
