# Troubleshooting

[Back to README](../README.md)

Use a valid travel date supported by THSR in all commands below; the example
`2026-10-23` is illustrative. Stop at results, an error, or an access challenge.
Never select a train or continue to Pickup Information. Do not automatically
retry with different browser settings, proxies, or identities after a block or
HTTP 403/429 response.

## Known booking compatibility limitation

On the computer used for live checks, the default Playwright launch can display
the CAPTCHA and then stalls after the answer is submitted. The full CLI flow
was verified with `--compatibility` on 2026-10-02; see the
[verification record](verification.md). This is a dated observation, not a
guarantee of current behavior or evidence of a specific blocking mechanism.

An opt-in compatibility experiment is available for the observed case where a
fully manual submission succeeds in ordinary Chrome but stalls in
Playwright-launched Chrome:

```sh
uv run thsr-watcher booking-search \
  --from 台北 \
  --to 台中 \
  --date 2026-10-23 \
  --after 17:00 \
  --adults 1 \
  --compatibility
```

This mode uses the installed Chrome channel and disables Blink's
`AutomationControlled` feature so the observed `navigator.webdriver` indicator
is not exposed. It is disabled by default and does not import a browser profile,
reuse cookies, change the User-Agent, rotate proxies or identities, retry a
failed request, or alter the human CAPTCHA handoff. Stop on any additional
challenge, explicit block, HTTP 403, or HTTP 429 response.

## Booking network diagnostics

A failed search reports its stage (CAPTCHA fill, search click, result wait, or
parsing), the HTTP status or Chromium network error code, and a limited
structural page snapshot. An older generic request-failed message does not
identify the endpoint. Obtain that diagnostic from a later search you start
explicitly. After an additional access challenge, an explicit block, or an HTTP
403/429 response, stop; do not start another search only to collect the
diagnostic. The session closes after an error. Browser closure alone does not
establish whether the CAPTCHA was accepted or the search completed.

What those reports include and omit is described in
[development](development.md#booking-failure-reports).

## Compare installed Chrome with bundled Chromium

If manual search in your regular Chrome succeeds while bundled Chromium returns
`net::ERR_EMPTY_RESPONSE`, use the official Playwright Chrome channel for one
controlled comparison:

```sh
uv run thsr-watcher booking-search \
  --from 台北 --to 台中 \
  --date 2026-10-23 --after 17:00 --adults 1 --chrome
```

This requires Google Chrome to be installed. It uses a fresh isolated context,
not your existing Chrome window, profile, cookies, or extensions. The CAPTCHA
handoff and single-submission boundary remain identical. The default command
still uses bundled Chromium. There is no automatic fallback or retry, custom
User-Agent, stealth setting, or imported browser state. This comparison tests
browser-distribution compatibility; it does not prove the cause of the empty
response or guarantee success. Stop on an explicit access challenge.

The `DetailsGA.js` exception is narrowly based on the resource reported in local
Chrome diagnostics; its contents were not independently retrieved here. It does
not ignore the `/IMINT/` form POST or other THSR scripts/data, alter requests, or
turn a timeout into success. A recognized result or explicit no-trains response
is still required. If no outcome appears, the command reports the outcome-wait
failure with page-state diagnostics instead of treating the GA load as the cause.

## Isolate form automation from the launched browser environment

When normal Chrome and Incognito succeed but `booking-search --chrome` fails:

```sh
uv run thsr-watcher booking-manual-check
```

This diagnostic launches headed Chrome with the same fresh context, locale and
Taipei timezone as `booking-search --chrome`. It opens the official form once
and leaves every field and action to you. Manually enter the same search criteria
and CAPTCHA in the browser and click search once. Stop at results, an error, or
an access challenge; do not select a train or continue. Close the Chrome window
to finish (a five-minute limit also closes it).

The terminal reports document HTTP statuses and relevant network errors without
query strings, session path parameters, request bodies or CAPTCHA values. No
form content or screenshots are captured. A 200 response is not proof of search
success; report whether the browser actually showed train results. Manual
success points toward the automated form interaction as the next investigation;
manual failure focuses attention on the launched browser environment. Neither
outcome alone proves anti-bot blocking. This is a separate diagnostic; the normal
booking-session API still uses external human CAPTCHA handoff.

## Reservation selectors

Review these selectors and assumptions when maintaining the browser integration:

- Consent: `#cookieAccpetBtn`, or the exact button name `我同意`.
- Station select names: `selectStartStation`, `selectDestinationStation`;
  options must match the station names. Optional time mode: `#bookingMethod_0`.
- Date/time names: `toTimeInputField`, `toTimeTable`; date format `YYYY/MM/DD`,
  time option text contains `HH:MM`. The user-observed date input is hidden by
  Flatpickr; date setting uses its `setDate` API with change hooks and verifies
  the submitted input value. A rejected date or missing picker fails explicitly.
- Adult-count select: `ticketPanel:rows:0:ticketAmount`, numeric option labels.
- CAPTCHA image: `#BookingS1Form_homeCaptcha_passCode`; answer input name:
  `homeCaptcha:securityCode`; initial search button: `#SubmitButton`.
- Results: the live page (observed 2026-10-01) has no result table. Each
  bookable train is an `input.uk-radio` named
  `TrainQueryDataViewPanel:TrainGroup` inside `div.result-listing > span > label`;
  the label text shows only departure and arrival. The train number is read
  from the radio's `querycode` attribute (times from `querydeparture`/
  `queryarrival`, else the label), verified live on 2026-10-02. A radio
  without them fails with its attribute names listed. Radios are never
  clicked. `table.result_table` remains as an older-markup fallback.
- Error containers: `.feedbackPanel`, `.feedbackPanelERROR`, `[role=alert]`,
  `.alert-danger`; recognized Chinese/English rejection, expiry, and empty-result
  phrases are defined in `booking_browser.py`. Unknown variants fail explicitly.
