# THSR Watcher

A personal-use command-line tool for searching Taiwan High Speed Rail trains:

- **Timetable search:** list scheduled trains within a departure-time window.
- **Bookable train search:** query the official reservation form with a CAPTCHA
  answered by a human, display currently presented bookable trains, and stop.

Timetable results do not indicate seat availability. Despite the project name,
polling, notifications, persistence, reservation creation, and payment are not
implemented.

## Installation

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/). From the repository root:

```sh
uv sync
uv run playwright install chromium
uv run thsr-watcher --help
```

## Timetable search

Replace `2026-10-23` in these examples with a valid travel date supported by THSR.

```sh
uv run thsr-watcher search \
  --from 台北 \
  --to 台中 \
  --date 2026-10-23 \
  --after 17:00 \
  --before 20:00
```

Both endpoints are inclusive departure times on the travel date. `--after` must
not exceed `--before`; equal endpoints search an exact minute. Arrival may be
later than `--before`. Overnight departure windows are not supported.
Chromium runs headlessly by default; add `--headed` to show the browser.

## Bookable train search

```sh
uv run thsr-watcher booking-search \
  --from 台北 \
  --to 台中 \
  --date 2026-10-23 \
  --after 17:00 \
  --adults 1
```

The browser is visible by default; `--headless` hides it.

On the computer used for live checks, this default launch can display the
CAPTCHA and then stalls after the answer is submitted. The complete terminal
handoff, submission, and result parsing completed on 2026-10-02 with
`--compatibility`. That mode requires installed Google Chrome and changes an
automation indicator. This is a dated observation from that computer. The check does not establish
why the default launch stalls. See
[troubleshooting](docs/troubleshooting.md) for the flag's boundaries and the
[verification record](docs/verification.md).

```sh
uv run thsr-watcher booking-search \
  --from 台北 \
  --to 台中 \
  --date 2026-10-23 \
  --after 17:00 \
  --adults 1 \
  --compatibility
```

1. Keep the terminal process running and open the printed CAPTCHA PNG path.
2. Read the image yourself and enter the answer at the terminal prompt.
3. The tool submits once and prints the results. It never selects a train.

Only an explicit CAPTCHA rejection allows another human answer in the same
session, up to three answers total. Reopen the PNG at the same path to see the
new image. Other outcomes end the session. Ctrl-C or EOF cancels and cleans up.
The local session deadline is five minutes from the first CAPTCHA capture.

Searches assume one-way, standard-class travel with 1–10 adults. Only the
displayed result set is returned, filtered by the requested earliest departure;
there is no pagination. Missing trains must not be interpreted as sold out.

## Inputs and boundaries

Supported stations: 南港、台北、板橋、桃園、新竹、苗栗、台中、彰化、雲林、嘉義、台南、左營.
Origin and destination must differ. Dates use `YYYY-MM-DD` and cannot precede
today in Asia/Taipei. Times use `HH:MM` without seconds or a timezone.

CAPTCHAs are always answered externally by a human; no OCR or automated solving
is implemented. Additional access challenges, explicit blocks, HTTP errors
(including 403/429), and unexpected responses stop the search. The tool never
continues to Pickup Information, handles personal data, creates a reservation,
or pays.

Search exit codes: `0` for success (including no matches), `2` for invalid input,
and `1` for retrieval, browser, or response failures.

## Development and documentation

Run the offline test suite:

```sh
uv run pytest
```

Tests require no browser installation or network access.

- [Development](docs/development.md): architecture, Python API, session lifecycle,
  testing, and documentation conventions.
- [Troubleshooting](docs/troubleshooting.md): browser options, diagnostics, and
  selectors to review when the official site changes.
- [Verification record](docs/verification.md): dated live observations, fixture
  provenance, and remaining verification gaps.
