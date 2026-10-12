# Verification record

[Back to README](../README.md)

These are dated observations, not guarantees of current site behavior. Regular
[tests](development.md#setup-and-offline-tests) are offline; live searches are
manual and stop at train results without selecting a train or making a reservation.

## Latest recorded booking status

The complete CLI CAPTCHA handoff, submission, and parsing flow was verified with
`--compatibility` on 2026-10-02. On that computer the default launch displayed
the CAPTCHA and stalled after the answer was submitted; the browser itself
opened. CAPTCHA rejection and recapture remain unverified live; offline tests
cover that path. Browser compatibility experiments do not establish the cause
of a failure.

## Timetable — 2026-09-08

The timetable query 台北 → 台中 on 2026-09-15, 17:00–20:00 succeeded against the official site in headless
Chromium: **15 trains**, first `0845 17:11 → 18:15`, last
`0681 19:46 → 20:46`. A separate public-form inspection of 台北 → 南港 on the
same travel date after 23:00 returned 0690, 0862, and 0294; none had cross-night
labels. Timetables can change, so these are verification observations.

`tests/fixtures/timetable.json` is a trimmed capture of the first query's public
response (72 rows), retaining only timetable/date fields used by the parser.
The offline suite covers validation, all supported stations, filtering/ordering,
response parsing, CLI success/empty/error cases, challenges/HTTP errors, and
browser cleanup. Live checks are manual and are not part of `uv run pytest`.

## Booking search — 2026-09-10 and 2026-10-02

During these checks, default Playwright launches stalled when the reservation
form POST was submitted, including with fully manual browser interaction. A
controlled comparison ruled out the active VPN, campus network, and effective
WPAD/PAC use as necessary causes: ordinary Chrome succeeded while
Playwright-launched Chrome stalled under the same hotspot conditions.

On 2026-09-10, `--compatibility` successfully reached selectable train results
after the user entered the CAPTCHA and submitted directly in the browser. This
verifies the compatibility launch environment, automated criteria preparation,
and the site's result-page transition.

On 2026-10-02, the complete terminal handoff path was verified live with
`--compatibility`: human answer at the CLI prompt, one Playwright submission,
and result parsing printed the bookable trains (台北→台中, 2026-10-23, after
17:00). No train was selected.

For the earlier environment comparisons, see the
[historical browser investigation](local-browser-investigation.md).

## Booking fixture and remaining gaps

`tests/fixtures/booking_results.json` is a synthetic parser contract fixture,
not a capture of current live markup. The live page observed on 2026-10-01 used
radio inputs rather than the older result table; reading train numbers from
`querycode` and parsing those results was verified on 2026-10-02.

The live CAPTCHA rejection page, its wording, and its new-image behavior have
not been verified. Failure to refill or recapture ends the session with
`UNKNOWN_ERROR`. See [development](development.md#booking-session-api) for the
retry contract and [troubleshooting](troubleshooting.md#reservation-selectors)
for selectors and recognized feedback assumptions.
