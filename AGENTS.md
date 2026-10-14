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

VS-02 `booking-search` may open the official reservation search form, fill search
criteria, capture the CAPTCHA element, accept one human answer in the same live
session, submit once per answer, and parse currently bookable trains. Only after
THSR explicitly rejects the CAPTCHA may the same live session recapture a new
CAPTCHA and accept another human answer, up to 3 answers per session in total;
any other outcome ends the session. After the first result page, it may read
the remaining result pages in that same session and list them together.
`booking-search` and diagnostics stop at
train results: never select a train, enter pickup information, handle personal
data, create a reservation, or pay.

VS-03 `book` may continue one retained search session: show one result page,
move one page earlier or later when that control is present, and select one
train from the page currently shown. It does not select a train from another
page. It fills pickup details from the local booking
preset, checks the same-ID membership box only when the preset says so, submits
the reservation once, and reports a confirmed code, an explicit rejection, or an
uncertain outcome. It does not log in, pay, or retry a reservation submission.
Personal data may be stored only in the owner-only preset file, never in logs,
diagnostics, or the CAPTCHA directory.

Polling, watchers, notifications, and messaging integrations remain out of
scope. Regular tests must be offline.
