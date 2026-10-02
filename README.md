# THSR Watcher

A personal-use THSR tool developed in small vertical slices. VS-01 searches the
**official public timetable** for a route, date, and departure-time window.
VS-02 adds a separate official reservation search with an external human CAPTCHA
handoff. It stops at currently bookable train results. Polling, notifications,
persistence, reservation creation, and payment are not implemented.

## Development

Requires Python 3.13+ and uv:

```sh
uv sync
uv run playwright install chromium
uv run thsr-watcher --help
uv run thsr-watcher search --help
uv run pytest
```

## Search

```sh
uv run thsr-watcher search \
  --from 台北 \
  --to 台中 \
  --date 2026-09-15 \
  --after 17:00 \
  --before 20:00
```

Supported station names: 南港、台北、板橋、桃園、新竹、苗栗、台中、彰化、雲林、
嘉義、台南、左營. Origin and destination must differ. Dates use `YYYY-MM-DD`
and cannot precede today in Asia/Taipei. Times use `HH:MM` with no seconds or
zone. Both endpoints are **inclusive departure times on the travel date**;
`--after` must not exceed `--before`. Equal endpoints search an exact minute.
Arrival may be later than `--before`.

Chromium runs headlessly by default; add `--headed` to show the browser for
debugging. No browser installation or network is required for the tests.
Exit codes: 0 for a successful search (including no matches), 2 for invalid
input, and 1 for retrieval/browser/response-format failures.

## Design and website boundary

- `models.py`: Pydantic `SearchRequest` and `Train`, station/input validation,
  and pure departure-window filtering and chronological ordering.
- `timetable.py`: Playwright lifecycle, public form submission, response parsing,
  and retrieval errors. Every invocation uses a fresh context and closes it and
  its browser in `finally` blocks. There are no sleeps or retries.
- `cli.py`: Typer options, useful validation/error messages, and train display.

The inspected page is [THSR Timetable and Fare Search](https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c).
Playwright selects stations by their displayed names, selects 單程, fills the
visible date/time inputs, and clicks 查詢. It observes the page's own POST
response to `https://www.thsrc.com.tw/TimeTable/Search`; it does not call that
endpoint directly or use reservation APIs. The page renders only five trains
at a time, while this response contains the full timetable, so one submission
suffices and filtering is local. Fares, discounts, and car information are ignored.

The parser reads `DepartureTable.TrainItem` fields `TrainNumber`,
`DepartureTime`, and `DestinationTime`. The latter is the destination arrival;
`StationInfo` can instead show that station's later departure. Route/date headers
and matching rows' `RunDate` are checked before results are returned. Leading
zeros in train numbers are preserved. Malformed responses fail explicitly.

Selectors/contracts to review if THSR changes the page: titles 出發站/到達站/票種,
button names 查詢/不同意 (cookie refusal), `#Departdate03`, `#outWardTime`, the
public response URL, and its `Title`/`TrainItem` fields. CAPTCHA/access challenges,
HTTP errors (including 403/429/5xx), and unexpected content stop the search with a
manual-handoff/error message. The tool never attempts to solve or bypass a challenge.

Special cross-night departure date labels were not present in the inspected
responses. If a matching row contains one, the parser fails with a manual-check
message rather than guessing its departure date. This is a deliberate limitation
until a real example can establish the date-label semantics. Ordinary same-day
windows are supported; overnight departure windows are rejected.

## Verification record — 2026-09-08

The exact search command above succeeded against the official site in headless
Chromium: **15 trains**, first `0845 17:11 → 18:15`, last
`0681 19:46 → 20:46`. A separate public-form inspection of 台北 → 南港 on the
same travel date after 23:00 returned 0690, 0862, and 0294; none had cross-night
labels. Timetables can change, so these are verification observations.

`tests/fixtures/timetable.json` is a trimmed capture of the first query's public
response (72 rows), retaining only timetable/date fields used by the parser.
The offline suite covers validation, all supported stations, filtering/ordering,
response parsing, CLI success/empty/error cases, challenges/HTTP errors, and
browser cleanup. The original package smoke test remains unchanged. Live checks
are manual and are not part of `uv run pytest`.

## VS-02: booking search with human CAPTCHA handoff

Run locally on your Mac:

```sh
uv run thsr-watcher booking-search \
  --from 台北 \
  --to 台中 \
  --date 2026-09-15 \
  --after 17:00 \
  --adults 1
```

Chromium is headed by default (`--headless` is optional). Open the printed PNG
path externally, read the CAPTCHA yourself, and enter its text at the terminal
prompt. Keep that terminal process running. One answer makes one submission;
the command prints the currently presented bookable trains and closes. It does
not click a train or continue to Pickup Information. Ctrl-C/EOF cancels and
cleans up. Failed searches exit 1; invalid inputs exit 2. No separate
`booking-start`/`booking-captcha` shell commands exist because a new process
cannot recover these in-memory browser sessions.

An opt-in compatibility experiment is available for the observed case where a
fully manual submission succeeds in ordinary Chrome but stalls in
Playwright-launched Chrome:

```sh
uv run thsr-watcher booking-search \
  --from 台北 \
  --to 台中 \
  --date 2026-09-15 \
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

The transport-independent API is:

```python
from thsr_watcher.booking import BookingSessionManager
from thsr_watcher.booking_models import BookingSearchRequest

request = BookingSearchRequest(
    origin="台北", destination="台中", travel_date="2026-09-15",
    after="17:00", adult_passengers=1,
)
with BookingSessionManager() as manager:
    session = manager.start_search(request)
    # The caller may read/upload session.captcha_path and wait for a human.
    # Keep this manager alive; all calls must use the same process and thread.
    answer = input(f"Read {session.captcha_path} externally; enter CAPTCHA: ")
    result = manager.submit_captcha(session.session_id, answer)
```

`booking_models.py` reuses VS-01's station, date/time validation, and `Train`
representation (number/departure/arrival). `booking.py` stores UUID session IDs
mapped to live browser owners. `booking_browser.py` exclusively owns reservation
selectors and Playwright operations. Simultaneous sessions share a sync
Playwright driver, but each opens its own browser/context/page. Reference-counted
driver cleanup keeps other sessions alive. This synchronous API is for one
owner thread; an async bot must dispatch its calls to a dedicated owner thread,
not invoke them directly inside its event loop.

`start_search()` leaves the driver, browser, context, cookies, and page open.
It screenshots the loaded CAPTCHA **element** using `locator.screenshot()` and
returns a PNG path under the OS temporary directory, in a unique private
`thsr-watcher-<session-id>-...` directory. The PNG is restricted to its owner.
No OCR, image interpretation, CAPTCHA endpoint calls, or automated answer
retrieval is implemented. `submit_captcha()` calls the same browser owner and
fills the same page, then consumes/closes the session regardless of outcome.
The browser, context, driver reference, and image directory are released on
submission, cancellation, failed setup, explicit close, or manager exit.
Abrupt process termination (for example SIGKILL) cannot guarantee cleanup.

The default local deadline is five minutes from challenge capture, configurable
with `ttl`. It is a local resource policy, not THSR's server TTL. Expiry is
checked before submission and swept when starting another search. Long-lived
callers must call `expire_sessions()` on their owner thread to release abandoned
sessions while idle; there is no background timer or network polling. Explicit
`close_session(id)` is idempotent. Unknown, consumed, and expired IDs return
`SESSION_EXPIRED` without opening a browser. Cleanup failures raise `BookingError`.

Successful parsing requires a recognized result table with enabled train radio
rows and identifiable train/departure/arrival columns, or an explicit recognized
no-matching-trains message. The parser preserves leading zeros and orders by
departure time. It never clicks those radio inputs. A supported time slot is
rounded down and the exact requested lower bound is applied locally to the
returned rows. Only the displayed result set is returned; no pagination,
additional searches, or inference that missing trains are sold out is performed.
The page's default one-way, standard-class, adult-only search is assumed.

Explicit CAPTCHA-error text maps to `CAPTCHA_REJECTED`; recognized timeout/session
text maps to `SESSION_EXPIRED`. Unrecognized feedback, malformed tables, HTTP
errors, additional access challenges, and browser/timeouts map to `UNKNOWN_ERROR`
(or `BookingError` during setup), never empty availability. A rejected answer
is not retried; the caller must explicitly start another session.

### Live verification status

The default Playwright launches stall when the reservation form POST is
submitted, including when every browser action is performed manually. A
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

Review these selectors/assumptions if the local command fails:

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

Use the command above for one local capture → human answer → submit → results
flow, then stop. The synthetic `tests/fixtures/booking_results.json` is a parser
contract fixture, not evidence of current live markup. Regular tests use fake
browser sessions and mocked Playwright only, including lifecycle, same-page
routing, simultaneous driver ownership, rejection/expiry/error mapping, result
parsing/ordering, CLI handoff/cancellation, and cleanup. No dependencies were added.

### Booking network diagnostics

Failed document/XHR/fetch requests report the method, resource type, host/path,
and Chromium network error code (or HTTP status). Query strings, fragments,
credentials, and path parameters such as `;jsessionid` are omitted. These details
are preserved in `UNKNOWN_ERROR` after submission as well as setup errors.
Only explicitly identified Google Analytics collection hosts and paths, plus
GET loads of the exact THSR `/IMINT/js/GA/DetailsGA.js` resource, are excluded
from fatal checks; unknown external endpoints and reservation requests
still stop the flow, including aborted requests. This does not retry requests or
change browser traffic. An old generic request-failed message cannot establish
which endpoint failed; rerun once to obtain the diagnostic if it recurs.

Submission failures also report their stage (CAPTCHA fill, search click, result
wait, or parsing) and a limited structural page snapshot: readiness, whether the
search controls remain visible, result-table counts, and visible tables' classes,
row/radio counts and headings. No form values, CAPTCHA image, full HTML, or raw
Playwright call logs are recorded. The search click is issued once without its
implicit navigation wait; an explicit bounded outcome wait then handles the
transition. Hidden feedback and hidden table templates cannot satisfy that wait.
The session still closes after an error; browser closure alone does not establish
whether CAPTCHA was accepted or the search completed.

### Compare installed Chrome with bundled Chromium

If manual search in your regular Chrome succeeds while bundled Chromium returns
`net::ERR_EMPTY_RESPONSE`, use the official Playwright Chrome channel for one
controlled comparison:

```sh
uv run thsr-watcher booking-search \
  --from 台北 --to 台中 \
  --date 2026-09-15 --after 17:00 --adults 1 --chrome
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

### Isolate form automation from the launched browser environment

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
