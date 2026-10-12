# Development

[Back to README](../README.md)

## Setup and offline tests

Requires Python 3.13+ and uv. From the repository root:

```sh
uv sync
uv run pytest
```

Regular tests use fixtures, fake browser sessions, and mocked Playwright. They
require neither a browser installation nor network access. They cover input
validation, parsing and ordering, CLI behavior, CAPTCHA handoff and rejection,
expiry and error mapping, simultaneous session ownership, and cleanup.
Live verification is manual; see the [verification record](verification.md).

## Timetable implementation

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

## Booking session API

The example date must be replaced with a valid travel date supported by THSR.
The transport-independent API is:

```python
from thsr_watcher.booking import BookingSessionManager
from thsr_watcher.booking_models import BookingSearchRequest

request = BookingSearchRequest(
    origin="台北", destination="台中", travel_date="2026-10-23",
    after="17:00", adult_passengers=1,
)
with BookingSessionManager() as manager:
    session = manager.start_search(request)
    # The caller may read/upload session.captcha_path and wait for a human.
    # Keep this manager alive; all calls must use the same process and thread.
    answer = input(f"Read {session.captcha_path} externally; enter CAPTCHA: ")
    result = manager.submit_captcha(session.session_id, answer)
    while result.captcha_attempts_remaining:
        answer = input(f"Rejected; read the new {session.captcha_path}: ")
        result = manager.submit_captcha(session.session_id, answer)
```

`booking_models.py` reuses the timetable search’s station, date/time validation, and `Train`
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
fills the same page, then consumes/closes the session, except after an
explicit CAPTCHA rejection with answers remaining (`max_captcha_attempts`,
an integer from 1 to 3, default 3): then the criteria are refilled on the same page, a new CAPTCHA is
captured to the same path, and the result has a nonzero
`captcha_attempts_remaining`. The browser accepts exactly one submission per
captured CAPTCHA.
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

Successful parsing requires recognized enabled train radio inputs, a supported
result table with identifiable train/departure/arrival columns, or an explicit
recognized no-matching-trains message. The parser preserves leading zeros and orders by
departure time. It never clicks those radio inputs. A supported time slot is
rounded down and the exact requested lower bound is applied locally to the
returned rows. Only the displayed result set is returned; no pagination,
additional searches, or inference that missing trains are sold out is performed.
The page's default one-way, standard-class, adult-only search is assumed.

Explicit CAPTCHA-error text maps to `CAPTCHA_REJECTED`; recognized timeout/session
text maps to `SESSION_EXPIRED`. Unrecognized feedback, malformed tables, HTTP
errors, additional access challenges, and browser/timeouts map to `UNKNOWN_ERROR`
(or `BookingError` during setup), never empty availability. A rejected answer
is never resubmitted automatically; only a new human answer for a newly
captured CAPTCHA is submitted. On such a retry, the rejection still displayed
from the previous attempt is ignored, and outcomes count only after the
document changes (`performance.timeOrigin`). Rejection is recognized for
「驗證碼」 or 「檢測碼」 followed by 錯誤/不正確/有誤. The live rejection
page, its wording, and whether THSR shows a new CAPTCHA after it have not been
verified; if a retry cannot refill the form or capture a CAPTCHA, the session
closes with `UNKNOWN_ERROR`. The local deadline still counts from the first
capture.

## Browser maintenance

Reservation selectors and diagnostic commands are documented in
[troubleshooting](troubleshooting.md). Keep browser automation isolated from
domain logic and preserve the project boundaries in [AGENTS.md](../AGENTS.md).

### Booking failure reports

Failed document/XHR/fetch requests report the method, resource type, host/path,
and Chromium network error code (or HTTP status). Query strings, fragments,
credentials, and path parameters such as `;jsessionid` are omitted. These details
are preserved in `UNKNOWN_ERROR` after submission as well as setup errors.
Only explicitly identified Google Analytics collection hosts and paths, plus
GET loads of the exact THSR `/IMINT/js/GA/DetailsGA.js` resource, are excluded
from fatal checks; unknown external endpoints and reservation requests
still stop the flow, including aborted requests. This reporting does not retry
requests or change browser traffic.

Submission failures also report their stage (CAPTCHA fill, search click, result
wait, or parsing) and a limited structural page snapshot: readiness, whether the
search controls remain visible, result-table counts, and visible tables' classes,
row/radio counts and headings. No form values, CAPTCHA image, full HTML, or raw
Playwright call logs are recorded. The search click is issued once without its
implicit navigation wait; an explicit bounded outcome wait then handles the
transition. Hidden feedback and hidden table templates cannot satisfy that wait.

## Maintaining documentation

- README: current capabilities, setup, usage, and important limitations.
- Development: current architecture, API contracts, failure reports, and offline testing.
- Troubleshooting: actionable diagnostics and browser integration assumptions.
- Verification: dated observations, fixture provenance, and unverified behavior.
- Commit and PR descriptions: what changed in that particular revision and how
  it was checked. Avoid appending those reports to README.

Historical investigation notes in
[local-browser-investigation.md](local-browser-investigation.md) describe the
environment at the time. Mark their status clearly and link to newer conclusions
rather than presenting old findings as current behavior.
