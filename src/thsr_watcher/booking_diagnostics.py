"""Manual-only comparison in the same Chrome environment as booking-search."""

import re
from collections.abc import Callable
from contextlib import ExitStack

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from thsr_watcher.booking import BookingError
from thsr_watcher.booking_browser import (
    RESERVATION_URL, TIMEOUT_MS, _is_telemetry, _request_description,
)


def manual_browser_check(emit: Callable[[str], None]) -> None:
    """Open once; the human operates the form and closes the window to finish.

    No form reads/writes, CAPTCHA capture, submission, browser state imports,
    retries, response-body logging or train selection are performed here.
    """
    try:
        with ExitStack() as resources:
            driver = resources.enter_context(sync_playwright())
            browser = driver.chromium.launch(headless=False, channel="chrome")
            resources.callback(browser.close)
            context = browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
            resources.callback(context.close)
            page = context.new_page()
            page.set_default_navigation_timeout(TIMEOUT_MS)

            def failed(request):
                if request.resource_type in ("document", "xhr", "fetch") and not _is_telemetry(request):
                    code = re.search(r"net::ERR_[A-Z0-9_]+", request.failure or "")
                    emit(f"FAILED {code.group() if code else 'network error'}: {_request_description(request)}")

            def received(response):
                request = response.request
                if _is_telemetry(request):
                    return
                if request.resource_type == "document" or (
                    request.resource_type in ("xhr", "fetch") and response.status >= 400
                ):
                    emit(f"HTTP {response.status}: {_request_description(request)}")

            page.on("requestfailed", failed)
            page.on("response", received)
            emit("Manual diagnostic: fill all criteria and CAPTCHA in Chrome yourself, then search ONCE.")
            emit("Stop at train results or any error/challenge. Do not select a train or continue booking.")
            emit("Close this Chrome window to finish. It will also close after five minutes.")
            page.goto(RESERVATION_URL, wait_until="domcontentloaded")
            try:
                # This pumps browser events while the human works; no polling or
                # terminal input blocks Playwright's synchronous event delivery.
                page.wait_for_event("close", timeout=300_000)
            except PlaywrightTimeoutError:
                emit("Manual diagnostic time limit reached; closing Chrome without retry.")
    except Exception as exc:
        raise BookingError(f"Manual browser diagnostic failed ({type(exc).__name__}); no retry attempted.") from exc
