# THSR Watcher

A personal-use THSR ticket availability watcher, developed incrementally in
small vertical slices. Only the project foundation exists today; the CLI prints
a status message and does not contact THSR.

## Development

Requires Python 3.13+ and uv. From the repository root:

```sh
uv sync
uv run thsr-watcher
uv run thsr-watcher --help
uv run pytest
```

The package lives in `src/thsr_watcher/`, with tests in `tests/`. Dependencies
are managed with uv and its committed lockfile. The stack is Typer for the CLI,
Pydantic for models/configuration, Playwright for future browser automation,
and pytest for tests. Browser installation is not needed for this foundation.
Project engineering rules are in `AGENTS.md`.

## Development phases

1. **Foundation (current):** package, CLI entry point, and import smoke test.
2. **Search:** query trains for a route, date, and time range.
3. **Monitor:** check availability at a conservative polling interval, respecting
   server errors and rate limits.
4. **Notify:** alert the user when a matching train becomes available.
5. **Reservation assistance:** help with reservation, with no automatic payment.

CAPTCHA and anti-bot challenges must always trigger a manual handoff; they must
never be bypassed. Aggressive polling and rate-limit evasion are out of scope.
Each future phase will be implemented through focused vertical slices, without
adding infrastructure before it is needed.
