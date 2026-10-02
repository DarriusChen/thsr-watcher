"""CLI validation and presentation for timetable and booking searches."""

from typing import Annotated

import typer
from pydantic import ValidationError

from thsr_watcher.booking import BookingError, BookingSessionManager
from thsr_watcher.booking_models import BookingSearchRequest, BookingSearchStatus
from thsr_watcher.models import SearchRequest
from thsr_watcher.timetable import TimetableError, search_trains

app = typer.Typer(help="THSR timetable and human-assisted booking search.")


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


@app.command("booking-search")
def booking_search(
    origin: Annotated[str, typer.Option("--from", help="Origin station, e.g. 台北.")],
    destination: Annotated[str, typer.Option("--to", help="Destination station, e.g. 台中.")],
    travel_date: Annotated[str, typer.Option("--date", help="Travel date YYYY-MM-DD (Taiwan time).")],
    after: Annotated[str, typer.Option("--after", help="Earliest departure HH:MM.")],
    adult_passengers: Annotated[int, typer.Option("--adults", help="Adult passengers (1–10).")] = 1,
    headless: Annotated[bool, typer.Option("--headless", help="Hide the browser; headed by default.")] = False,
    chrome: Annotated[bool, typer.Option("--chrome", help="Use installed Google Chrome in a fresh isolated session.")] = False,
    compatibility: Annotated[
        bool,
        typer.Option(
            "--compatibility",
            help="Opt in to the installed-Chrome automation compatibility experiment.",
        ),
    ] = False,
) -> None:
    """Capture CAPTCHA, accept one human answer, show bookable trains, and stop."""
    try:
        request = BookingSearchRequest(
            origin=origin, destination=destination, travel_date=travel_date,
            after=after, adult_passengers=adult_passengers,
        )
    except ValidationError as exc:
        for error in exc.errors():
            typer.echo(f"Invalid {'.'.join(map(str, error['loc'])) or 'search'}: {error['msg']}", err=True)
        raise typer.Exit(2) from exc
    try:
        with BookingSessionManager(
            headless=headless, chrome=chrome, compatibility=compatibility,
        ) as manager:
            session = manager.start_search(request)
            typer.echo(f"Booking session created: {session.session_id}")
            typer.echo(f"CAPTCHA image: {session.captcha_path}")
            typer.echo(f"Open this PNG externally and read it yourself. Local deadline: {session.expires_at.isoformat()}")
            typer.echo("Keep this process running; Ctrl-C cancels and removes the image.")
            answer = typer.prompt("Enter CAPTCHA", hide_input=True)
            result = manager.submit_captcha(session.session_id, answer)
    except BookingError as exc:
        typer.echo(f"Booking search failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{result.status.value}: {result.message}")
    if result.status != BookingSearchStatus.SUCCESS:
        raise typer.Exit(1)
    for train in result.trains:
        typer.echo(f"Train {train.number}   {train.departure:%H:%M} → {train.arrival:%H:%M}")
    typer.echo(f"{len(result.trains)} currently bookable trains shown for the requested time. No train selected.")


@app.command("booking-manual-check")
def booking_manual_check() -> None:
    """Diagnose Chrome: manually search once, stop at results, then close window."""
    from thsr_watcher.booking_diagnostics import manual_browser_check

    try:
        manual_browser_check(typer.echo)
    except BookingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Diagnostic finished. HTTP status alone does not confirm bookable results.")
