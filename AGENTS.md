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

The current scope is the project foundation only. Do not add THSR browser
automation until a later vertical slice explicitly requires it.
