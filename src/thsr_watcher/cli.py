"""CLI validation and presentation for public timetable search."""

from typing import Annotated

import typer
from pydantic import ValidationError

from thsr_watcher.models import SearchRequest
from thsr_watcher.timetable import TimetableError, search_trains

app = typer.Typer(help="Search the official THSR public timetable.")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """THSR Watcher: public timetable search."""
    if ctx.invoked_subcommand is None:
        typer.echo("THSR watcher ready. Use 'search --help' for timetable search.")


@app.command()
def search(
    origin: Annotated[str, typer.Option("--from", help="Origin station, e.g. 台北.")],
    destination: Annotated[str, typer.Option("--to", help="Destination station, e.g. 台中.")],
    travel_date: Annotated[str, typer.Option("--date", help="Travel date in YYYY-MM-DD (Taiwan time).")],
    after: Annotated[str, typer.Option("--after", help="Earliest departure HH:MM, inclusive.")],
    before: Annotated[str, typer.Option("--before", help="Latest departure HH:MM, inclusive, same day.")],
    headed: Annotated[bool, typer.Option("--headed", help="Show Chromium for debugging.")] = False,
) -> None:
    """List trains departing within the requested time window."""
    try:
        request = SearchRequest(
            origin=origin, destination=destination, travel_date=travel_date,
            after=after, before=before,
        )
    except ValidationError as exc:
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "search"
            typer.echo(f"Invalid {field}: {error['msg']}", err=True)
        raise typer.Exit(2) from exc
    try:
        trains = search_trains(request, headless=not headed)
    except TimetableError as exc:
        typer.echo(f"Timetable search failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{request.origin} → {request.destination}")
    typer.echo(f"{request.travel_date} {request.after:%H:%M}–{request.before:%H:%M}\n")
    for train in trains:
        next_day = " (+1 day)" if train.arrival < train.departure else ""
        typer.echo(f"Train {train.number}   {train.departure:%H:%M} → {train.arrival:%H:%M}{next_day}")
    typer.echo(f"\n{len(trains)} trains found" if trains else "No trains match this departure window.")
