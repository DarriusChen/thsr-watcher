"""Official reservation form: one search submission per human CAPTCHA answer.

The book command may then select one parsed train, fill a local preset, and
submit that reservation once. Pickup selectors follow the live form observed
on 2026-10-12. Unknown markup fails closed: it never means no availability,
never triggers another search, and never clicks twice.
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
    DocumentType, PickupDetails, ReservationResult, ReservationStatus,
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
({table, radio, feedback, patterns, staleOrigin}) => {
    if (staleOrigin !== null && performance.timeOrigin === staleOrigin) return null;
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
    const hasPattern = new RegExp(patterns, 'i').test((document.body && document.body.innerText) || '');
    if (!trains.length && !hasTableRadio && !hasFeedback && !hasPattern) return null;
    return {trains, hasTableRadio, hasFeedback, hasPattern};
}
"""
REJECTED = r"(驗證碼|檢測碼).{0,16}(錯誤|不正確|有誤)|invalid captcha|incorrect verification code"
EXPIRED = r"(連線|工作階段|操作|網頁).{0,16}(逾時|過期|超時)|session.{0,16}(expired|timeout|timed out)"
EMPTY = r"查無符合條件之車次|查無符合條件的車次|無符合條件之車次|no trains available"
CONFIRM_TRAIN = "確認車次"
PICKUP_BOX = re.compile(r"手機|行動電話|身分證|護照|居留證")
RESERVATION_BUTTON = re.compile(r"^\s*(完成訂位|確認訂位)\s*$")
DOCUMENT_TYPE = "#idInputRadio"
DOCUMENT_NUMBER = "#idNumber"
MOBILE_NUMBER = "#mobilePhone"
EMAIL_FIELD = "#email"
MEMBERSHIP_TGO = "#memberSystemRadio1"
MEMBERSHIP_NONE = "#memberSystemRadio3"
MEMBERSHIP_BOX = "#memberShipCheckBox"
MEMBERSHIP_DIALOG = "#step3ConfirmModal"
TERMS_CHECKBOX = 'input[name="agree"]'
DOCUMENT_NAME = {
    DocumentType.NATIONAL_ID: re.compile(r"身分證"),
    DocumentType.PASSPORT: re.compile(r"護照"),
    DocumentType.RESIDENCE_PERMIT: re.compile(r"居留證"),
}
RESERVATION_LABEL = "訂位代號"
RESERVATION_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{8})(?![A-Za-z0-9])")
RESERVATION_REJECTION = re.compile(r"訂位失敗|無法完成訂位|未能完成訂位|此車次已額滿|已無餘位|餘位不足")
OUTCOME_WAIT = r"訂位代號|訂位失敗|無法完成訂位|未能完成訂位|已額滿|已無餘位|餘位不足"
UNCERTAIN_MESSAGE = (
    "The reservation outcome is uncertain and it may already exist. "
    "Check THSR directly. This program will not submit again."
)



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


def reservation_outcome(text: str) -> ReservationResult | None:
    """Classify a reservation page without copying its text into the result."""
    codes = set()
    start = 0
    while True:
        at = text.find(RESERVATION_LABEL, start)
        if at < 0:
            break
        window = text[at + len(RESERVATION_LABEL):at + len(RESERVATION_LABEL) + 80]
        found = RESERVATION_CODE.search(window)
        if found:
            codes.add(found.group(1).upper())
        start = at + len(RESERVATION_LABEL)
    if len(codes) == 1:
        return ReservationResult(
            status=ReservationStatus.CONFIRMED, reservation_code=codes.pop(),
            message="Reservation confirmed. No payment was made.",
        )
    if len(codes) > 1:
        return ReservationResult(status=ReservationStatus.UNCERTAIN, message=UNCERTAIN_MESSAGE)
    if RESERVATION_REJECTION.search(text):
        return ReservationResult(
            status=ReservationStatus.REJECTED,
            message="THSR rejected the reservation. No payment was made.",
        )
    return None


def _uncertain() -> ReservationResult:
    return ReservationResult(status=ReservationStatus.UNCERTAIN, message=UNCERTAIN_MESSAGE)


def _required(locator) -> bool:
    return locator.get_attribute("required") is not None or locator.get_attribute("aria-required") == "true"


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
        self._awaiting_answer = False
        self._rejected = False
        self._attempts = 0
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
        self._fill_criteria(request)
        self._capture_captcha(captcha_path)

    def refresh_captcha(self, captcha_path: Path) -> None:
        """After an explicit CAPTCHA rejection, recapture on the same live page."""
        if self._page is None or self._awaiting_answer or not self._rejected:
            raise BookingError("No rejected CAPTCHA to replace in this session")
        self._rejected = False
        self._check_access()
        assert self._request is not None
        self._fill_criteria(self._request)
        self._capture_captcha(captcha_path)

    def _fill_criteria(self, request: BookingSearchRequest) -> None:
        page = self._page
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

    def _capture_captcha(self, captcha_path: Path) -> None:
        page = self._page
        captcha = page.locator("#BookingS1Form_homeCaptcha_passCode")
        captcha.wait_for(state="visible")
        page.wait_for_function("() => { const img = document.getElementById('BookingS1Form_homeCaptcha_passCode'); return img && img.complete && img.naturalWidth > 0; }", timeout=TIMEOUT_MS)
        self._check_access()
        captcha.screenshot(path=str(captcha_path))
        captcha_path.chmod(0o600)
        self._awaiting_answer = True

    def submit(self, captcha_text: str) -> BookingSearchResult:
        """Submit once per captured CAPTCHA; a new answer needs refresh_captcha()."""
        if self._page is None or not self._awaiting_answer:
            raise BookingError("Session is closed or has no unanswered CAPTCHA")
        self._awaiting_answer = False
        self._dialogs.clear()
        self._stage = "checking session before submission"
        try:
            result = self._submit_once(captcha_text)
            self._rejected = result.status == Status.CAPTCHA_REJECTED
            return result
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
        retry = self._attempts > 0
        self._check_access()
        preflight = feedback_result(page.locator("body").inner_text())
        # On a retry the previous rejection is still displayed; it says nothing
        # about the new answer.
        if preflight and preflight.status != Status.SUCCESS and not (
            retry and preflight.status == Status.CAPTCHA_REJECTED
        ):
            return preflight
        # Until the document changes, any outcome on screen belongs to the
        # previous attempt.
        stale_origin = page.evaluate("() => performance.timeOrigin") if retry else None
        self._attempts += 1
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
                     "patterns": f"{REJECTED}|{EXPIRED}|{EMPTY}", "staleOrigin": stale_origin},
                timeout=TIMEOUT_MS,
            ).json_value()
        except Exception:
            self._check_access()
            text = "\n".join(self._dialogs)
            if stale_origin is None or page.evaluate("() => performance.timeOrigin") != stale_origin:
                text += "\n" + page.locator("body").inner_text()
            error = feedback_result(text)
            if error and error.status != Status.SUCCESS:
                return error
            raise
        self._check_access()
        feedback = "\n".join(
            self._dialogs + page.locator(FEEDBACK).filter(visible=True).all_inner_texts()
        )
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
        table = page.locator(RESULT_TABLE).filter(visible=True)
        rows = table.locator("tr").filter(
            visible=True, has=page.locator('input[type="radio"]:enabled'),
        )
        if radios:
            trains = parse_train_radios(radios)
        elif rows.count():
            headers = table.locator("tr").filter(
                visible=True, has=page.locator("th"),
            ).first.locator("th, td").all_inner_texts()
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

    def select_train(self, train_number: str) -> None:
        """Check one result radio and open the pickup form. Does not submit a reservation."""
        if self._page is None:
            raise BookingError("Session is closed; no train was selected.")
        if not re.fullmatch(r"[0-9]+", train_number):
            raise BookingError("Train is not in this search result; nothing was selected.")
        self._check_access()
        page = self._page
        radio = page.locator(f'{TRAIN_RADIO}[querycode="{train_number}"]').locator("visible=true")
        if radio.count() != 1:
            raise BookingError("Selected train is not a single visible choice; nothing was clicked.")
        radio.check()
        confirm = page.get_by_role("button", name=CONFIRM_TRAIN, exact=True)
        if confirm.count() != 1:
            raise BookingError(
                "Confirm-train control was not a single control; reservation was not submitted. "
                + self._visible_field_names()
            )
        confirm.click(no_wait_after=True)
        self._check_access()
        try:
            page.get_by_role("textbox", name=PICKUP_BOX).first.wait_for(state="visible", timeout=TIMEOUT_MS)
        except Exception as exc:
            raise BookingError(
                "Pickup form was not recognized; reservation was not submitted. "
                + self._visible_field_names()
            ) from exc

    def submit_reservation(self, pickup: PickupDetails) -> ReservationResult:
        """Fill the preset and click the reservation button once."""
        if self._page is None:
            raise BookingError("Session is closed; reservation was not submitted.")
        page = self._page
        clicked = False
        try:
            self._check_access()
            if re.search(EXPIRED, page.locator("body").inner_text(), re.I):
                raise BookingError("THSR session expired; reservation was not submitted.")
            document = self._visible(DOCUMENT_TYPE, "Document type")
            document_value = self._option_value(document, DOCUMENT_NAME[pickup.document_type])
            number = self._visible(DOCUMENT_NUMBER, "Document number")
            mobile = self._visible(MOBILE_NUMBER, "Mobile number")
            email = page.locator(EMAIL_FIELD)
            email_count = email.count()
            if email_count > 1 or (pickup.email and email_count != 1):
                raise BookingError("Email field was not a single control; reservation was not submitted.")
            if email_count == 1 and _required(email) and not pickup.email:
                raise BookingError(
                    "The pickup form requires an email and the preset has none; reservation was not submitted."
                )
            membership = page.locator(MEMBERSHIP_BOX)
            membership_count = membership.count()
            if pickup.use_same_id_membership and membership_count != 1:
                raise BookingError(
                    "Membership checkbox was not found; reservation was not submitted. "
                    + self._visible_field_names()
                )
            if not pickup.use_same_id_membership and membership_count > 1:
                raise BookingError(
                    "Membership checkbox was not a single control; reservation was not submitted."
                )
            tgo = self._visible(MEMBERSHIP_TGO, "Membership choice") if pickup.use_same_id_membership else None
            non_member = None if pickup.use_same_id_membership else self._visible(MEMBERSHIP_NONE, "Membership choice")
            terms = self._visible(TERMS_CHECKBOX, "Terms checkbox")
            button = self._reservation_button()
            document.select_option(value=document_value)
            number.fill(pickup.document_number)
            mobile.fill(pickup.mobile)
            if email_count == 1 and pickup.email:
                email.fill(pickup.email)
            if pickup.use_same_id_membership:
                tgo.check()
                membership.wait_for(state="visible", timeout=TIMEOUT_MS)
                if not membership.is_enabled():
                    raise BookingError(
                        "Membership checkbox was not found; reservation was not submitted. "
                        + self._visible_field_names()
                    )
                membership.check()
            else:
                if membership_count == 1 and membership.is_checked():
                    if not membership.is_enabled():
                        self._visible(MEMBERSHIP_TGO, "Membership choice").check()
                        membership.wait_for(state="visible", timeout=TIMEOUT_MS)
                    membership.uncheck()
                if not non_member.is_checked():
                    non_member.check()
            if not terms.is_checked():
                terms.check()
            button = self._reservation_button()
            self._check_access()
            clicked = True
            button.click(no_wait_after=True)
            if pickup.use_same_id_membership:
                self._confirm_membership()
            return self._read_reservation_outcome()
        except BookingError:
            if clicked:
                return _uncertain()
            raise
        except Exception:
            if clicked:
                return _uncertain()
            raise BookingError("Reservation was not submitted.")

    def _confirm_membership(self) -> None:
        """Click the TGo account confirmation once. Never clicks 重新輸入."""
        confirm = self._page.locator(MEMBERSHIP_DIALOG).get_by_role("button", name="確定", exact=True)
        try:
            confirm.wait_for(state="visible", timeout=TIMEOUT_MS)
        except Exception as exc:
            raise BookingError("Membership confirmation was not shown.") from exc
        chosen = confirm.locator("visible=true")
        if chosen.count() != 1:
            raise BookingError("Membership confirmation was not a single control.")
        label = " ".join(
            part.strip()
            for part in (chosen.inner_text() or "", chosen.get_attribute("value") or "")
            if part.strip()
        )
        if label != "確定":
            raise BookingError("Membership confirmation was not the confirm control.")
        self._check_access()
        chosen.click(no_wait_after=True)

    def _visible(self, selector: str, description: str):
        return self._single(self._page.locator(selector).locator("visible=true"), description)

    def _option_value(self, select, pattern: re.Pattern[str]) -> str:
        options = select.locator("option")
        matched = []
        for index in range(options.count()):
            option = options.nth(index)
            if pattern.search(option.inner_text() or ""):
                matched.append(option.get_attribute("value"))
        if len(matched) != 1 or not matched[0]:
            raise BookingError(
                "Document type was not a single control; reservation was not submitted. "
                + self._visible_field_names()
            )
        return matched[0]

    def _single(self, locator, description: str):
        if locator.count() != 1:
            raise BookingError(
                f"{description} was not a single control; reservation was not submitted. "
                + self._visible_field_names()
            )
        return locator

    def _reservation_button(self):
        button = self._page.get_by_role("button", name=RESERVATION_BUTTON)
        if button.count() != 1:
            raise BookingError(
                "Reservation button was not a single control; reservation was not submitted. "
                + self._visible_field_names()
            )
        label = (button.inner_text() or "") + (button.get_attribute("value") or "")
        if re.search(r"付款|信用卡|支付", label):
            raise BookingError("Payment controls are not used; reservation was not submitted.")
        return button

    def _read_reservation_outcome(self) -> ReservationResult:
        page = self._page
        try:
            page.wait_for_function(
                """(pattern) => new RegExp(pattern).test((document.body && document.body.innerText) || '')""",
                arg=OUTCOME_WAIT,
                timeout=TIMEOUT_MS,
            )
        except Exception:
            return _uncertain()
        try:
            self._check_access()
        except BookingError:
            return _uncertain()
        outcome = reservation_outcome(page.locator("body").inner_text())
        return outcome or _uncertain()

    def _visible_field_names(self) -> str:
        """Structural field names only; input values are never read."""
        try:
            names = self._page.evaluate("""() => {
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return Array.from(document.querySelectorAll('input, select, button'))
                    .filter(visible).slice(0, 12).map(el => {
                        const name = el.getAttribute('name') || '';
                        const type = el.getAttribute('type') || el.tagName.toLowerCase();
                        return (type + (name ? ' ' + name : '')).slice(0, 60);
                    });
            }""")
            return "Visible fields: " + ", ".join(names)
        except Exception:
            return "Visible fields unavailable."

    def close(self) -> None:
        self._page = None
        self._resources.close()
