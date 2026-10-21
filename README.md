# THSR Watcher

A personal-use command-line tool for searching Taiwan High Speed Rail trains:

- **Timetable search:** list scheduled trains within a departure-time window.
- **Bookable train search:** query the official reservation form with a CAPTCHA
  answered by a human, display currently presented bookable trains, and stop.
- **Local booking:** after a bookable-train search, select one listed train and
  submit one unpaid reservation using a preset saved on this computer.

Timetable results do not indicate seat availability. Despite the project name,
polling, notifications, and payment are not implemented.

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
3. The tool submits once per human answer and prints the results. It never selects a train.

Only an explicit CAPTCHA rejection allows another human answer in the same
session, up to three answers total. Reopen the PNG at the same path to see the
new image. Other outcomes end the search session. Ctrl-C or EOF cancels and
cleans up. `booking-search` has a local deadline of five minutes from the first
CAPTCHA capture.

Reservation searches (`booking-search` and `book`) assume one-way,
standard-class travel with 1–10 adults. One search is submitted. The first
result page is shown and filtered by the requested earliest departure. If a
later-train control is present, the tool asks before reading the remaining
pages in that same session. Missing trains must not be interpreted as sold out.
If a later page cannot be read after you ask for it, the search fails instead
of treating the first page as the full list.

## Local booking

Save pickup details once. The file is `~/.config/thsr-watcher/booking-preset.json`
and is readable only by you. `show` masks the document and mobile numbers.
`clear` deletes the file.

```sh
uv run thsr-watcher book-preset
uv run thsr-watcher book-preset show
uv run thsr-watcher book-preset clear
```

The prompts ask for a document type number (`1` 身分證, `2` 護照, or `3` 居留證),
document number, mobile, optional email, and whether to check 同取票人身分證字號.
Document, mobile, and email input is hidden in the terminal. On the live
form, 護照 and 居留證 share one document option. Membership checks 高鐵會員 TGo
and then that checkbox. The site then asks to confirm the TGo account; the
tool clicks 確定 once. It does not log in. Enter `y` to save. Checking
membership requires a national ID.

`book` reads that preset before opening a browser. It uses the same search
options as `booking-search`, including `--compatibility`. After you answer the
CAPTCHA, it shows one page of bookable trains, numbered from 1. Enter that list
number to select a train. Enter `e` or `l` to move one page earlier or later
when that control is present; the list is replaced by that page and numbered
again from 1. Only trains on the current page can be selected. Return to the
page that lists a train before choosing its list number. Entering the train
number does not select it. The command
fills the preset and submits once. It does not ask for the document, mobile,
membership choice, or a second confirmation. It never clicks payment.

```sh
uv run thsr-watcher book \
  --from 台北 \
  --to 台中 \
  --date 2026-10-23 \
  --after 17:00 \
  --adults 1 \
  --compatibility
```

A confirmed result prints the reservation code. An explicit rejection prints
`REJECTED`. If the outcome cannot be read after the click, the result is
`UNCERTAIN`: the reservation may already exist, and the tool will not submit
again. `NOT_SUBMITTED` means the tool stopped before submission. The local
deadline for `book` is ten minutes from the first CAPTCHA capture, covering
CAPTCHA answers, train selection, and the start of reservation submission.
The same three-answer CAPTCHA limit applies. Ctrl-C or EOF cleans up the
session; interruption after submission does not undo a reservation.

## Inputs and boundaries

Supported stations: 南港、台北、板橋、桃園、新竹、苗栗、台中、彰化、雲林、嘉義、台南、左營.
Origin and destination must differ. Dates use `YYYY-MM-DD` and cannot precede
today in Asia/Taipei. Times use `HH:MM` without seconds or a timezone.

CAPTCHAs are always answered externally by a human; no OCR or automated solving
is implemented. Additional access challenges, explicit blocks, HTTP errors
(including 403/429), and unexpected responses stop the search or the booking.
`booking-search` never selects a train. `book` stops before payment and does
not retry a reservation submission. Pickup details are stored only in the
preset file.

Search exit codes: `0` for success (including no matches), `2` for invalid input,
and `1` for retrieval, browser, or response failures.
For `book`, `0` means a confirmed reservation or no bookable trains, `2` means
invalid input or a missing/invalid preset, and `1` means a failed search,
`NOT_SUBMITTED`, `REJECTED`, or `UNCERTAIN`.

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
- [Plan](docs/plan.md): vertical-slice status and future ideas outside the current scope.
