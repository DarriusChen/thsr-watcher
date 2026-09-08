# THSR Watcher

A personal-use THSR tool developed in small vertical slices. VS-01 searches the
**official public timetable** for a route, date, and departure-time window.
It does not check seat availability or enter the reservation system. Polling,
notifications, persistence, reservations, and payment are not implemented.

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
