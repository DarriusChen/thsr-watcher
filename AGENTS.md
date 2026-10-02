# Project engineering rules

- Work in vertical slices.
- Prefer the smallest change that completes the current spec.
- Do not implement future features or architecture prematurely.
- Keep browser automation isolated from domain logic.
- Prefer typed domain models, using Pydantic for models and configuration.
- Use uv for Python dependencies and commands, Typer for the CLI, Playwright
  for browser automation, and pytest for tests.
- Within VS-02, explicit opt-in browser compatibility experiments may adjust
  Playwright automation indicators (such as `navigator.webdriver`) or use a
  stealth plugin to reduce automation fingerprint exposure. Keep these changes
  isolated in the browser layer, disabled by default, and covered by offline
  tests when implemented. This permission does not establish that automation
  detection caused a failure or guarantee that these adjustments will help.
- Preserve the external human CAPTCHA handoff.
  Stop automation on additional access challenges, explicit blocks, or HTTP 403/429
  responses; do not automatically retry with different fingerprints, proxies, or
  identities, or use stealth to continue past a block.
- Use conservative polling and respect server errors and rate limits. Do not
  implement aggressive polling or mechanisms intended to evade limits.

VS-02 may open the official reservation search form, fill search criteria,
capture the CAPTCHA element, accept one human answer in the same live session,
submit once, and parse currently bookable trains. Stop at train results: never
select a train, enter Pickup Information, handle personal data, create a
reservation, or pay.

Polling/watchers, notifications, messaging integrations, persistence, and
reservation completion remain out of scope. Regular tests must be offline.
