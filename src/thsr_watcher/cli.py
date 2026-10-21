"""CLI validation and presentation for timetable search and local booking."""

from datetime import timedelta
from typing import Annotated

import typer
from pydantic import ValidationError

from thsr_watcher.booking import BookingError, BookingSessionManager
from thsr_watcher.booking_models import (
    BookingSearchRequest, BookingSearchStatus, DocumentType, PickupDetails, ReservationResult,
    ReservationStatus,
)
from thsr_watcher.booking_preset import PresetError, clear_preset, load_preset, masked_summary, save_preset
from thsr_watcher.models import SearchRequest, Train
from thsr_watcher.timetable import TimetableError, search_trains

app = typer.Typer(help="THSR timetable search and human-assisted local booking.")
BOOKING_TTL = timedelta(minutes=10)
_DOCUMENT_TYPE_CODES = {
    "1": DocumentType.NATIONAL_ID,
    "2": DocumentType.PASSPORT,
    "3": DocumentType.RESIDENCE_PERMIT,
}
_DOCUMENT_TYPE_LABELS = {"1": "身分證", "2": "護照", "3": "居留證"}


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
    """Capture CAPTCHA, accept a human answer, show bookable trains, and stop."""
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
            result = _captcha_handoff(manager, session, retain=True)
            if result.status != BookingSearchStatus.SUCCESS:
                typer.echo(f"{result.status.value}: {result.message}")
                raise typer.Exit(1)
            result = _maybe_list_later_trains(manager, session.session_id, result)
    except BookingError as exc:
        typer.echo(f"Booking search failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{len(result.trains)} currently bookable trains from {request.after:%H:%M} onward. "
        "No train selected."
    )


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


def _captcha_handoff(manager: BookingSessionManager, session, *, retain: bool = False):
    typer.echo(f"Booking session created: {session.session_id}")
    typer.echo(f"CAPTCHA image: {session.captcha_path}")
    typer.echo(f"Open this PNG externally and read it yourself. Local deadline: {session.expires_at.isoformat()}")
    typer.echo("Keep this process running; Ctrl-C cancels and removes the image.")
    answer = typer.prompt("Enter CAPTCHA", hide_input=True)
    kwargs = {"retain_on_success": True} if retain else {}
    result = manager.submit_captcha(session.session_id, answer, **kwargs)
    while result.captcha_attempts_remaining:
        typer.echo(f"{result.status.value}: {result.message}")
        typer.echo(
            f"New CAPTCHA image: {session.captcha_path} "
            f"({result.captcha_attempts_remaining} attempts remaining)"
        )
        answer = typer.prompt("Enter CAPTCHA", hide_input=True)
        result = manager.submit_captcha(session.session_id, answer, **kwargs)
    return result


def _print_trains(trains) -> None:
    for train in trains:
        typer.echo(f"Train {train.number}   {train.departure:%H:%M} → {train.arrival:%H:%M}")


def _print_numbered_trains(trains) -> None:
    for index, train in enumerate(trains, start=1):
        typer.echo(f"{index}  Train {train.number}   {train.departure:%H:%M} → {train.arrival:%H:%M}")


PAGE_NOTE = (
    "Only trains on this page can be selected. "
    "Return to the page that lists a train before choosing it."
)


def _page_prompt(earlier: bool, later: bool) -> str:
    options = []
    if earlier:
        options.append("e earlier")
    if later:
        options.append("l later")
    if not options:
        return "Enter a list number"
    return "Enter " + ", ".join(options) + ", or a list number"


def _train_number_for_choice(trains, choice: str) -> str:
    """Map a 1-based list number on the current page to that train's number."""
    if choice.isdigit():
        try:
            index = int(choice)
        except ValueError:
            index = 0
        if 1 <= index <= len(trains):
            return trains[index - 1].number
    raise ValueError("Enter a list number.")


def _choose_current_page(manager: BookingSessionManager, session_id: str, result):
    """Show one result page at a time. e or l replaces that page."""
    while True:
        typer.echo(f"{result.status.value}: {result.message}")
        _print_numbered_trains(result.trains)
        earlier = manager.has_earlier_trains(session_id)
        later = manager.can_step_later(session_id)
        if not result.trains and not earlier and not later:
            return None
        typer.echo(PAGE_NOTE)
        choice = typer.prompt(_page_prompt(earlier, later)).strip()
        if choice.lower() in {"e", "l"}:
            if (choice.lower() == "e" and not earlier) or (choice.lower() == "l" and not later):
                typer.echo("That result page is not available.", err=True)
                continue
            result = manager.step_trains(session_id, "earlier" if choice.lower() == "e" else "later")
            if result.status != BookingSearchStatus.SUCCESS:
                typer.echo(f"{result.status.value}: {result.message}")
                raise typer.Exit(1)
            continue
        try:
            return _train_number_for_choice(result.trains, choice)
        except ValueError as exc:
            typer.echo(str(exc), err=True)


def _maybe_list_later_trains(manager: BookingSessionManager, session_id: str, result):
    """Show the current page, then read later pages only if the user asks."""
    typer.echo(f"{result.status.value}: {result.message}")
    _print_trains(result.trains)
    if not manager.has_later_trains(session_id):
        return result
    choice = typer.prompt("List later bookable trains? [y/n]")
    if choice not in {"y", "n"}:
        typer.echo("Invalid choice: enter y or n", err=True)
        raise typer.Exit(2)
    if choice == "n":
        return result
    later = manager.load_later_trains(session_id)
    typer.echo(f"{later.status.value}: {later.message}")
    if later.status != BookingSearchStatus.SUCCESS:
        raise typer.Exit(1)
    _print_trains(later.trains)
    return later


preset_app = typer.Typer(help="Save, show, or clear the local booking preset.", invoke_without_command=True)
app.add_typer(preset_app, name="book-preset")


@preset_app.callback(invoke_without_command=True)
def book_preset(ctx: typer.Context) -> None:
    """Save pickup details before a booking. show and clear inspect or delete them."""
    if ctx.invoked_subcommand is None:
        _save_preset_from_prompts()


@preset_app.command("show")
def preset_show() -> None:
    """Print the saved preset with document and mobile numbers masked."""
    try:
        typer.echo(masked_summary(load_preset()))
    except PresetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@preset_app.command("clear")
def preset_clear() -> None:
    """Delete the local booking preset."""
    clear_preset()
    typer.echo("Booking preset cleared.")


def _save_preset_from_prompts() -> None:
    typer.echo("Document type:")
    for code, label in _DOCUMENT_TYPE_LABELS.items():
        typer.echo(f"{code} {label}")
    document_type = typer.prompt("Enter a document type number")
    if document_type not in _DOCUMENT_TYPE_CODES:
        typer.echo("Invalid document type: enter 1, 2, or 3", err=True)
        raise typer.Exit(2)
    document_number = typer.prompt("Document number", hide_input=True)
    mobile = typer.prompt("Mobile", hide_input=True)
    email = typer.prompt("Email (optional)", default="", show_default=False, hide_input=True)
    membership = typer.prompt("Check 同取票人身分證字號? [y/n]")
    if membership not in {"y", "n"}:
        typer.echo("Invalid membership choice: enter y or n", err=True)
        raise typer.Exit(2)
    try:
        details = PickupDetails(
            document_type=_DOCUMENT_TYPE_CODES[document_type],
            document_number=document_number,
            mobile=mobile,
            email=email,
            use_same_id_membership=membership == "y",
        )
    except ValidationError as exc:
        _echo_validation(exc)
        raise typer.Exit(2) from exc
    typer.echo(masked_summary(details))
    save = typer.prompt("Save this preset? [y/n]")
    if save not in {"y", "n"}:
        typer.echo("Invalid choice: enter y or n", err=True)
        raise typer.Exit(2)
    if save != "y":
        typer.echo("Preset was not saved.")
        raise typer.Exit(1)
    try:
        save_preset(details)
    except OSError as exc:
        typer.echo("Could not save the booking preset.", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Preset saved.")


def _echo_validation(exc: ValidationError) -> None:
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "preset"
        typer.echo(f"Invalid {field}: {error['msg']}", err=True)


@app.command()
def book(
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
    """Select one bookable train and submit one reservation from the local preset."""
    try:
        preset = load_preset()
    except PresetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        request = BookingSearchRequest(
            origin=origin, destination=destination, travel_date=travel_date,
            after=after, adult_passengers=adult_passengers,
        )
    except ValidationError as exc:
        _echo_validation(exc)
        raise typer.Exit(2) from exc
    typer.echo(masked_summary(preset))
    try:
        with BookingSessionManager(
            headless=headless, chrome=chrome, compatibility=compatibility, ttl=BOOKING_TTL,
        ) as manager:
            session = manager.start_search(request)
            result = _captcha_handoff(manager, session, retain=True)
            if result.status != BookingSearchStatus.SUCCESS:
                _echo_search_failure(result)
            train_number = _choose_current_page(manager, session.session_id, result)
            if train_number is None:
                typer.echo("No currently bookable trains shown. No reservation was submitted.")
                return
            selected = manager.select_train(session.session_id, train_number)
            if isinstance(selected, ReservationResult):
                typer.echo(f"{selected.status.value}: {selected.message}")
                raise typer.Exit(1)
            reservation = manager.submit_reservation(session.session_id, preset)
    except BookingError as exc:
        typer.echo(f"Booking failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_reservation(reservation, selected)


def _echo_search_failure(result) -> None:
    typer.echo(f"{result.status.value}: {result.message}")
    raise typer.Exit(1)


def _echo_reservation(reservation: ReservationResult, train: Train) -> None:
    typer.echo(f"{reservation.status.value}: {reservation.message}")
    if reservation.status == ReservationStatus.CONFIRMED:
        typer.echo(f"Train {train.number}   reservation code {reservation.reservation_code}")
        typer.echo("No payment was made.")
        return
    raise typer.Exit(1)
