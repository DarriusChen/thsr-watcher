"""One public timetable form submission; no reservation or availability requests."""

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Dialog, Page, sync_playwright
from pydantic import ValidationError

from thsr_watcher.models import SearchRequest, Train, matching_trains

TIMETABLE_URL = "https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c"
SEARCH_URL = "https://www.thsrc.com.tw/TimeTable/Search"
TIMEOUT_MS = 20_000


class TimetableError(Exception):
    """A search failed; callers must not present it as an empty timetable."""


def parse_timetable(payload: object, request: SearchRequest) -> list[Train]:
    """Parse only timetable fields from the response used by the public page."""
    try:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise TimetableError("THSR did not return a successful timetable search.")
        table = payload["data"]["DepartureTable"]
        title = table["Title"]
        day = request.travel_date.strftime("%Y/%m/%d")
        if (
            title["StartStationName"] != request.origin
            or title["EndStationName"] != request.destination
            or title["TitleSplit1"].split("(")[0] != day
        ):
            raise TimetableError("THSR returned a different route or travel date.")
        rows = table["TrainItem"]
        if not isinstance(rows, list):
            raise TypeError("TrainItem is not a list")
        trains = []
        for row in rows:
            train = Train(
                number=row["TrainNumber"],
                departure=row["DepartureTime"],
                arrival=row["DestinationTime"],
            )
            # The public page can include cross-night services with date labels.
            # Do not silently assign an unverified date-label format to this day.
            if request.after <= train.departure <= request.before:
                if row["IsCrossNight"] is not False or row["DepartureDate"] not in (None, ""):
                    raise TimetableError(
                        "THSR returned a cross-night departure with a date label that "
                        "cannot yet be interpreted reliably. Check the public timetable manually."
                    )
                if row["RunDate"] != day:
                    raise TimetableError("THSR returned a train for a different travel date.")
            trains.append(train)
        return matching_trains(request, trains)
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise TimetableError("THSR timetable response has an unexpected format; the parser needs review.") from exc


def _check_challenge(page: Page) -> None:
    text = page.locator("body").inner_text().lower()
    markers = ("captcha", "verify you are human", "驗證您是人類", "人機驗證", "access denied", "request rejected")
    challenge_frame = page.locator(
        'iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible, '
        'iframe[src*="challenges.cloudflare.com"]:visible'
    )
    if any(marker in text for marker in markers) or challenge_frame.count():
        raise TimetableError(
            "THSR presented a CAPTCHA or access challenge. Automation stopped; "
            "open the public timetable manually. No retry was attempted."
        )


def _check_status(status: int) -> None:
    if status >= 400:
        raise TimetableError(
            f"THSR returned HTTP {status}. Search stopped without retry; "
            "check the public timetable manually or try later."
        )


def _search_page(page: Page, request: SearchRequest) -> list[Train]:
    page.set_default_timeout(TIMEOUT_MS)
    dialogs: list[str] = []

    def dismiss_dialog(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", dismiss_dialog)
    try:
        response = page.goto(TIMETABLE_URL, wait_until="load")
        if response is None:
            raise TimetableError("THSR timetable page did not respond.")
        _check_status(response.status)
        _check_challenge(page)
        decline = page.get_by_role("button", name="不同意", exact=True)
        if decline.is_visible():
            decline.click()
        page.get_by_title("出發站", exact=True).select_option(label=request.origin)
        page.get_by_title("到達站", exact=True).select_option(label=request.destination)
        page.get_by_title("票種", exact=True).select_option(label="單程")
        page.locator("#Departdate03").fill(request.travel_date.strftime("%Y/%m/%d"))
        page.locator("#outWardTime").fill(request.after.strftime("%H:%M"))
        _check_challenge(page)
        # Observe the form's own public request, rather than call an API directly.
        with page.expect_response(
            lambda response: response.url == SEARCH_URL and response.request.method == "POST"
        ) as pending:
            page.get_by_role("button", name="查詢", exact=True).click()
        response = pending.value
        _check_status(response.status)
        _check_challenge(page)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TimetableError(
                "THSR returned non-JSON timetable content, possibly an access challenge. "
                "Search stopped; check the public timetable manually."
            ) from exc
        return parse_timetable(payload, request)
    except PlaywrightError as exc:
        if dialogs:
            raise TimetableError("THSR rejected the search: " + "; ".join(dialogs)) from exc
        # Detect an interstitial that replaced the form while an explicit wait ran.
        if not page.is_closed():
            _check_challenge(page)
        raise TimetableError(
            "Could not retrieve the THSR timetable: the page timed out, the network "
            "failed, or the form changed. Check the public timetable manually."
        ) from exc


def search_trains(request: SearchRequest, *, headless: bool = True) -> list[Train]:
    """Close all resources on success, challenge, timeout, and parsing failure."""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            try:
                context = browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
                try:
                    return _search_page(context.new_page(), request)
                finally:
                    context.close()
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise TimetableError(
            "Could not run the timetable browser. Install Chromium with "
            "'uv run playwright install chromium' and check browser/network access."
        ) from exc
