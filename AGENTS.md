# Project engineering rules

- Work in vertical slices.
- Prefer the smallest change that completes the current spec.
- Do not implement future features or architecture prematurely.
- Keep browser automation isolated from domain logic.
- Prefer typed domain models, using Pydantic for models and configuration.
- Use uv for Python dependencies and commands, Typer for the CLI, Playwright
  for browser automation, and pytest for tests.
- Never bypass CAPTCHA or anti-bot controls. Stop automation and hand off to
  the user when a challenge occurs.
- Use conservative polling and respect server errors and rate limits. Do not
  implement aggressive polling or mechanisms intended to evade limits.
- Never implement automatic payment.
- Every implementation task must finish by running relevant tests.
- Summarize modified files and verification results when done.

The current scope is VS-01: search the official public THSR timetable by
origin, destination, travel date, and an inclusive same-day departure-time
window. Browser automation is permitted only for this public timetable search.
Keep input validation and timetable filtering independent of browser automation.

Seat availability checks, polling, notifications, persistence, and reservation
assistance are out of scope until a later vertical slice explicitly requires
them. Do not access the reservation system in this slice. Automatic payment
remains prohibited.
