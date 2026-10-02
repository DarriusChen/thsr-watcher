"""One official reservation form submission, ending on the train-results page.

Selectors are provisional until verified locally against THSR. Unknown markup
fails closed: it never means no availability and never triggers another search.
"""

import json
import re
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

from thsr_watcher.booking import BookingError
from thsr_watcher.booking_models import (
    BookingSearchRequest, BookingSearchResult, BookingSearchStatus as Status,
)
from thsr_watcher.models import Train

RESERVATION_URL = "https://irs.thsrc.com.tw/IMINT/"
TIMEOUT_MS = 20_000
RESULT_TABLE = "table.result_table"
# Live-observed result radios (2026-10-01): one per bookable train, inside
# div.result-listing > span > label. The label shows only the two times.
TRAIN_RADIO = 'input[type=radio][name="TrainQueryDataViewPanel:TrainGroup"]'
CLOCK_TIME = r"\b(?:[01]\d|2[0-3]):[0-5]\d\b"
COMPATIBILITY_ARGS = ["--disable-blink-features=AutomationControlled"]
FEEDBACK = ".feedbackPanel, .feedbackPanelERROR, [role=alert], .alert-danger"
# Reads train radios only; never clicks them. Attribute values are returned as
# data, attribute names only so an unexpected markup change can be reported.
_SCAN_JS = """
({table, radio, feedback, patterns}) => {
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const trains = Array.from(document.querySelectorAll(radio))
        .filter(el => !el.disabled && visible(el))
        .map(el => ({
            code: el.getAttribute('querycode'),
            departure: el.getAttribute('querydeparture'),
            arrival: el.getAttribute('queryarrival'),
            label: ((el.closest('label') || el.parentElement || el).innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
            attributeNames: Array.from(el.attributes).map(a => a.name).slice(0, 20)
        }));
    const hasTableRadio = Array.from(document.querySelectorAll(table + ' input[type=radio]')).some(el => visible(el.closest('tr') || el));
    const hasFeedback = Array.from(document.querySelectorAll(feedback)).some(el => visible(el) && (el.innerText || '').trim());
    const hasPattern = new RegExp(patterns, 'i').test(document.body.innerText || '');
    if (!trains.length && !hasTableRadio && !hasFeedback && !hasPattern) return null;
    return {trains, hasTableRadio, hasFeedback, hasPattern};
}
"""
REJECTED = r"驗證碼.{0,16}(錯誤|不正確|有誤)|驗證碼輸入錯誤|invalid captcha|incorrect verification code"
EXPIRED = r"(連線|工作階段|操作|網頁).{0,16}(逾時|過期|超時)|session.{0,16}(expired|timeout|timed out)"
EMPTY = r"查無符合條件之車次|查無符合條件的車次|無符合條件之車次|no trains available"



def _is_telemetry(request) -> bool:
    """Exclude narrowly scoped analytics resources, not reservation data."""
    url = urlsplit(request.url)
    # Reported as an XHR-loaded GA script in the user's Chrome run. Excluding
    # this ancillary resource never supplies evidence of a successful search:
    # the result/error wait and parser still have to establish an outcome.
    if (
        request.method == "GET"
        and request.resource_type in ("xhr", "fetch", "script")
        and url.scheme == "https"
        and url.hostname == "irs.thsrc.com.tw"
        and url.path == "/IMINT/js/GA/DetailsGA.js"
    ):
        return True
    return (
        request.resource_type in ("xhr", "fetch")
        and url.hostname in {
            "www.google-analytics.com", "region1.google-analytics.com",
            "analytics.google.com", "stats.g.doubleclick.net",
        }
        and url.path in {"/collect", "/g/collect", "/j/collect"}
    )


def _request_description(request) -> str:
    """Omit query, fragment, credentials and URL path parameters (session IDs)."""
    url = urlsplit(request.url)
    path = "/".join(part.split(";", 1)[0] for part in url.path.split("/"))
    return f"{request.method} {request.resource_type} {url.hostname}{path}"


def feedback_result(text: str) -> BookingSearchResult | None:
    """Classify explicit errors only; a bare CAPTCHA label is not a rejection."""
    for pattern, status in ((REJECTED, Status.CAPTCHA_REJECTED), (EXPIRED, Status.SESSION_EXPIRED)):
        if re.search(pattern, text, re.I):
            return BookingSearchResult(status=status, message="THSR reported: " + text.strip())
    if re.search(EMPTY, text, re.I):
        return BookingSearchResult(status=Status.SUCCESS, message="THSR explicitly reported no matching trains for this search.")
    return None


def parse_train_radios(radios: list[dict]) -> list[Train]:
    """Every visible train radio must yield a train, or availability is unknown.

    The train number comes only from the radio's `querycode` attribute. Times come
    from `querydeparture`/`queryarrival`, else from the label's two clock times.
    """
    trains = []
    for radio in radios:
        code = (radio.get("code") or "").strip()
        times = re.findall(CLOCK_TIME, radio.get("label") or "")
        departure = (radio.get("departure") or "").strip() or (times[0] if len(times) == 2 else "")
        arrival = (radio.get("arrival") or "").strip() or (times[1] if len(times) == 2 else "")
        try:
            trains.append(Train(number=code, departure=departure, arrival=arrival))
        except ValueError as exc:
            names = ", ".join(radio.get("attributeNames") or []) or "none"
            raise BookingError(
                "Train choice is missing a readable train number or times; "
                f"availability is unknown (radio attributes: {names})"
            ) from exc
    return sorted(trains, key=lambda train: (train.departure, train.number))


def parse_train_rows(headers: list[str], rows: list[list[str]]) -> list[Train]:
    """Parse visible selectable table rows by column headings, preserving zeros."""
    aliases = {
        "number": {"車次", "車次號碼", "TrainNo.", "TrainNo"},
        "departure": {"出發時間", "出發", "DepartureTime", "Departure"},
        "arrival": {"抵達時間", "到達時間", "抵達", "到達", "ArrivalTime", "Arrival"},
    }
    normalized = [re.sub(r"\s+", "", value) for value in headers]
    columns = {}
    for field, names in aliases.items():
        matches = [index for index, value in enumerate(normalized) if value in names]
        if len(matches) != 1:
            raise BookingError(f"Unrecognized reservation table heading: {field}")
        columns[field] = matches[0]
    if not rows:
        raise BookingError("Result table contains no selectable rows or explicit no-trains message")
    trains = []
    try:
        for row in rows:
            trains.append(Train(**{field: row[index].strip() for field, index in columns.items()}))
    except (ValueError, IndexError, TypeError) as exc:
        raise BookingError("Malformed reservation train row; availability is unknown") from exc
    return sorted(trains, key=lambda train: (train.departure, train.number))


class PlaywrightBookingFactory:
    """Share one sync driver across simultaneous sessions on the owner's thread."""

    def __init__(
        self, *, headless: bool = False, chrome: bool = False,
        compatibility: bool = False,
    ) -> None:
        self.headless = headless
        self.chrome = chrome
        self.compatibility = compatibility
        self._driver = None
        self._users = 0

    def __call__(self):
        return PlaywrightBookingBrowser(
            headless=self.headless, chrome=self.chrome,
            compatibility=self.compatibility, driver_owner=self,
        )

    def acquire(self):
        if self._driver is None:
            self._driver = sync_playwright().start()
        self._users += 1
        return self._driver

    def release(self):
        self._users -= 1
        if self._users == 0:
            driver, self._driver = self._driver, None
            driver.stop()


class PlaywrightBookingBrowser:
    """Own the Playwright driver, browser, context, and page until close()."""

    def __init__(
        self, *, headless: bool = False, chrome: bool = False,
        compatibility: bool = False,
        driver_owner: PlaywrightBookingFactory | None = None,
    ) -> None:
        self._driver_owner = driver_owner or PlaywrightBookingFactory(
            headless=headless, chrome=chrome, compatibility=compatibility,
        )
        self._headless = headless
        self._chrome = chrome
        self._compatibility = compatibility
        self._resources = ExitStack()
        self._page: Page | None = None
        self._request: BookingSearchRequest | None = None
        self._submitted = False
        self._request_errors: list[str] = []
        self._dialogs: list[str] = []

    def _check_access(self) -> None:
        assert self._page is not None
        if self._request_errors:
            raise BookingError(self._request_errors[0] + "; stopped without retry")
        if self._page.locator(
            'iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible, '
            'iframe[src*="challenges.cloudflare.com"]:visible'
        ).count() or re.search(
            r"access denied|request rejected|verify you are human|人機驗證|驗證您是人類",
            self._page.locator("body").inner_text(), re.I,
        ):
            raise BookingError("Additional access challenge; stopped for manual handling")

    def start(self, request: BookingSearchRequest, captcha_path: Path) -> None:
        if self._page is not None:
            raise BookingError("Browser session already started")
        self._request = request.model_copy(deep=True)
        playwright = self._driver_owner.acquire()
        self._resources.callback(self._driver_owner.release)
        # Compatibility mode is an explicit, single-configuration experiment.
        # It keeps an isolated context and never imports a profile or cookies.
        launch_options = {"headless": self._headless}
        if self._chrome or self._compatibility:
            launch_options["channel"] = "chrome"
        if self._compatibility:
            launch_options["args"] = COMPATIBILITY_ARGS.copy()
        browser = playwright.chromium.launch(**launch_options)
        self._resources.callback(browser.close)
        context = browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
        self._resources.callback(context.close)
        page = self._page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        page.set_default_navigation_timeout(TIMEOUT_MS)

        def response_received(response):
            if (response.request.resource_type in ("document", "xhr", "fetch")
                    and response.status >= 400 and not _is_telemetry(response.request)):
                self._request_errors.append(
                    f"HTTP {response.status}: {_request_description(response.request)}"
                )

        def request_failed(failed_request):
            if (failed_request.resource_type in ("document", "xhr", "fetch")
                    and not _is_telemetry(failed_request)):
                # Chromium failure codes are useful; avoid logging arbitrary URLs
                # or payloads embedded in a provider's failure description.
                code = re.search(r"net::ERR_[A-Z0-9_]+", failed_request.failure or "")
                reason = code.group() if code else "network request failed"
                self._request_errors.append(
                    f"{reason}: {_request_description(failed_request)}"
                )

        def dialog_received(dialog):
            self._dialogs.append(dialog.message)
            dialog.dismiss()

        page.on("response", response_received)
        page.on("requestfailed", request_failed)
        page.on("dialog", dialog_received)
        response = page.goto(RESERVATION_URL, wait_until="load")
        if response is None:
            raise BookingError("Reservation page did not respond")
        self._check_access()
        consent = page.locator("#cookieAccpetBtn")
        if consent.is_visible():
            consent.click()
        else:
            consent = page.get_by_role("button", name="我同意", exact=True)
            if consent.is_visible():
                consent.click()
        page.locator('[name="selectStartStation"]').select_option(label=request.origin)
        page.locator('[name="selectDestinationStation"]').select_option(label=request.destination)
        time_mode = page.locator("#bookingMethod_0")
        if time_mode.is_visible():
            time_mode.check()
        date_input = page.locator('[name="toTimeInputField"]')
        date_value = request.travel_date.strftime("%Y/%m/%d")
        # Flatpickr hides the named form input when it creates a visible altInput.
        # Use its public API to update both inputs and selectedDates, including
        # change hooks and the configured min/max/disabled-date constraints.
        uses_picker = date_input.evaluate("""(el, value) => {
            const picker = el._flatpickr;
            if (!picker) return false;
            picker.setDate(value, true, 'Y/m/d');
            return true;
        }""", date_value)
        if not uses_picker:
            if date_input.get_attribute("type") == "hidden" or date_input.get_attribute("readonly") is not None:
                raise BookingError("Reservation date picker is unavailable; cannot safely set the travel date")
            date_input.fill(date_value)
        if date_input.input_value() != date_value:
            raise BookingError("Reservation date picker did not accept the requested travel date")
        time_select = page.locator('[name="toTimeTable"]')
        options = time_select.locator("option").evaluate_all(
            "els => els.map(el => ({text: el.textContent.trim(), value: el.value}))"
        )
        target = request.after.strftime("%H:%M")
        choices = [(m.group(), option["value"]) for option in options
                   if (m := re.search(r"\b[0-2][0-9]:[0-5][0-9]\b", option["text"])) and m.group() <= target]
        if not choices:
            raise BookingError("Requested time is outside the reservation form's supported slots")
        # Floor to a supported slot; apply the exact lower bound to displayed results.
        time_select.select_option(value=max(choices)[1])
        page.locator('select[name="ticketPanel:rows:0:ticketAmount"]').select_option(label=str(request.adult_passengers))
        captcha = page.locator("#BookingS1Form_homeCaptcha_passCode")
        captcha.wait_for(state="visible")
        page.wait_for_function("() => { const img = document.getElementById('BookingS1Form_homeCaptcha_passCode'); return img && img.complete && img.naturalWidth > 0; }", timeout=TIMEOUT_MS)
        self._check_access()
        captcha.screenshot(path=str(captcha_path))
        captcha_path.chmod(0o600)

    def submit(self, captcha_text: str) -> BookingSearchResult:
        if self._page is None or self._submitted:
            raise BookingError("Session is closed or its one submission was already used")
        self._submitted = True
        self._stage = "checking session before submission"
        try:
            return self._submit_once(captcha_text)
        except BookingError as exc:
            raise BookingError(
                f"{exc} Stage: {self._stage}. {self._page_diagnostics()}"
            ) from exc
        except Exception as exc:
            # Playwright call logs can contain the CAPTCHA fill argument. Never
            # expose raw exception text or a full HTML/screenshot capture here.
            reason = type(exc).__name__
            if "strict mode violation" in str(exc):
                reason += "; selector matched multiple elements"
            raise BookingError(
                f"Booking failed while {self._stage} ({reason}; action timeout {TIMEOUT_MS // 1000}s). "
                f"{self._page_diagnostics()} No retry was attempted."
            ) from exc

    def _page_diagnostics(self) -> str:
        """Capture only structural state, never form values, HTML, or CAPTCHA."""
        try:
            state = self._page.evaluate(r"""() => {
                const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const seen = new Set();
                const roots = [];
                const visit = (node) => {
                    if (!node || typeof node.querySelectorAll !== 'function' || seen.has(node)) return;
                    seen.add(node);
                    roots.push(node);
                    node.querySelectorAll('*').forEach(el => { if (el.shadowRoot) visit(el.shadowRoot); });
                    node.querySelectorAll('iframe, frame').forEach(frame => {
                        try { if (frame.contentDocument) visit(frame.contentDocument); } catch (e) {}
                    });
                };
                visit(document);
                const tables = [];
                const choiceControls = [];
                const timeBlocks = [];
                const clock = /(?:[01]\d|2[0-3])[:：][0-5]\d/g;
                const describe = el => {
                    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
                    return el.tagName.toLowerCase() + (cls ? '.' + cls : '');
                };
                const captchaVisible = visible(document.querySelector('[name="homeCaptcha:securityCode"]'));
                let radios = 0;
                for (const root of roots) {
                    root.querySelectorAll('input[type=radio]').forEach(el => { if (!el.disabled && visible(el)) radios += 1; });
                    root.querySelectorAll('input[type=radio], button, a, [role="button"]').forEach(el => {
                        if (el.disabled || !visible(el) || choiceControls.length >= 6) return;
                        const ancestors = [];
                        for (let node = el, depth = 0; node && node.tagName && depth < 6; node = node.parentElement, depth++) {
                            const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                            ancestors.push({
                                tag: describe(node),
                                children: Array.from(node.children).slice(0, 8).map(describe),
                                times: (text.match(clock) || []).length
                            });
                        }
                        choiceControls.push({
                            tag: describe(el),
                            name: (el.getAttribute('name') || '').slice(0, 60),
                            ancestors
                        });
                    });
                    root.querySelectorAll('div, li, tr, section, label').forEach(el => {
                        if (!visible(el) || timeBlocks.length >= 8 || captchaVisible) return;
                        const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
                        const times = text.match(clock) || [];
                        if (times.length < 2 || times.length > 4 || text.length > 160) return;
                        const smaller = Array.from(el.children).some(child => {
                            const childText = (child.innerText || '').replace(/\s+/g, ' ').trim();
                            const childTimes = childText.match(clock) || [];
                            return childTimes.length >= 2 && childTimes.length <= 4 && childText.length <= 160;
                        });
                        if (smaller) return;
                        timeBlocks.push({
                            tag: describe(el),
                            children: Array.from(el.children).slice(0, 8).map(describe),
                            text: text.slice(0, 120)
                        });
                    });
                    root.querySelectorAll('table').forEach(t => {
                        if (!visible(t) || tables.length >= 8) return;
                        tables.push({
                            className: (t.getAttribute('class') || '').slice(0, 80),
                            rows: t.rows.length,
                            enabledRadios: t.querySelectorAll('input[type=radio]:enabled').length,
                            headers: Array.from(t.querySelectorAll('th')).slice(0, 12).map(h => h.innerText.trim().slice(0, 60))
                        });
                    });
                }
                return {
                    readyState: document.readyState,
                    searchButtonVisible: visible(document.getElementById('SubmitButton')),
                    captchaInputVisible: visible(document.querySelector('[name="homeCaptcha:securityCode"]')),
                    documentsScanned: roots.length,
                    visibleEnabledRadios: radios,
                    expectedResultTables: roots.reduce((n, root) => n + root.querySelectorAll('table.result_table').length, 0),
                    visibleTables: tables,
                    choiceControls,
                    timeBlocks
                };
            }""")
            return "Page state: " + json.dumps(state, ensure_ascii=False)
        except Exception:
            return "Page state unavailable (page closed or navigation still in progress)."

    def _submit_once(self, captcha_text: str) -> BookingSearchResult:
        page = self._page
        self._check_access()
        preflight = feedback_result(page.locator("body").inner_text())
        if preflight and preflight.status != Status.SUCCESS:
            return preflight
        self._stage = "filling CAPTCHA"
        page.locator('[name="homeCaptcha:securityCode"]').fill(captcha_text)
        # This selector belongs only to the search form. No result-page control
        # is ever clicked, including train radios and the Pickup Information step.
        self._stage = "submitting search form"
        page.locator("#SubmitButton").click(no_wait_after=True)
        self._stage = "waiting for train results or explicit error"
        try:
            found = page.wait_for_function(
                _SCAN_JS,
                arg={"table": RESULT_TABLE, "radio": TRAIN_RADIO, "feedback": FEEDBACK,
                     "patterns": f"{REJECTED}|{EXPIRED}|{EMPTY}"},
                timeout=TIMEOUT_MS,
            ).json_value()
        except Exception:
            self._check_access()
            error = feedback_result("\n".join(self._dialogs) + "\n" + page.locator("body").inner_text())
            if error and error.status != Status.SUCCESS:
                return error
            raise
        self._check_access()
        feedback = "\n".join(self._dialogs + page.locator(FEEDBACK).all_inner_texts())
        result = feedback_result(feedback)
        if result and result.status != Status.SUCCESS:
            return result
        if feedback.strip() and result is None:
            return BookingSearchResult(status=Status.UNKNOWN_ERROR, message="THSR reported: " + feedback.strip())
        body_result = feedback_result(page.locator("body").inner_text())
        if body_result and body_result.status != Status.SUCCESS:
            return body_result
        self._stage = "parsing train results"
        radios = found.get("trains") if isinstance(found, dict) else None
        table = page.locator(RESULT_TABLE)
        rows = table.locator("tr").filter(has=page.locator('input[type="radio"]:enabled'))
        if radios:
            trains = parse_train_radios(radios)
        elif rows.count():
            headers = table.locator("tr").filter(has=page.locator("th")).first.locator("th, td").all_inner_texts()
            data = [row.locator("td").all_inner_texts() for row in rows.all()]
            trains = parse_train_rows(headers, data)
        elif result or body_result:
            return result or body_result
        else:
            raise BookingError("No recognizable selectable train results")
        assert self._request is not None
        return BookingSearchResult(
            status=Status.SUCCESS,
            trains=[train for train in trains if train.departure >= self._request.after],
            message="Currently presented as bookable in this search; no reservation has been created.",
        )

    def close(self) -> None:
        self._page = None
        self._resources.close()
