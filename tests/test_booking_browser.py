from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from thsr_watcher import booking_browser as adapter
from thsr_watcher.booking import BookingError, BookingSessionManager
from thsr_watcher.booking_models import BookingSearchRequest, BookingSearchStatus as Status


@pytest.fixture
def browser_setup(monkeypatch):
    driver = MagicMock()
    monkeypatch.setattr(adapter, "sync_playwright", Mock(return_value=Mock(start=Mock(return_value=driver))))
    browser = driver.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    locators = defaultdict(MagicMock)

    def locator(selector):
        result = locators[selector]
        result.is_visible.return_value = False
        return result

    page.locator.side_effect = locator
    page.get_by_role.return_value.is_visible.return_value = False
    page.goto.return_value.status = 200
    locators["body"].inner_text.return_value = "訂票 請輸入驗證碼"
    date_input = locators['[name="toTimeInputField"]']
    date_input.get_attribute.return_value = None
    date_input.evaluate.return_value = False
    date_input.input_value.return_value = "2026/09/15"
    locators['[name="toTimeTable"]'].locator.return_value.evaluate_all.return_value = [
        {"text": "17:00", "value": "1700"}, {"text": "17:30", "value": "1730"},
    ]
    locators["#BookingS1Form_homeCaptcha_passCode"].screenshot.side_effect = lambda path: Path(path).write_bytes(b"test png")
    # No additional access challenge.
    challenge = 'iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible, iframe[src*="challenges.cloudflare.com"]:visible'
    locators[challenge].count.return_value = 0
    locators[adapter.FEEDBACK].all_inner_texts.return_value = []
    locators[adapter.FEEDBACK].filter.return_value = locators[adapter.FEEDBACK]
    locators[adapter.RESULT_TABLE].filter.return_value = locators[adapter.RESULT_TABLE]
    request = BookingSearchRequest(origin="台北", destination="台中", travel_date="2026-09-15", after="17:10")
    return driver, browser, context, page, locators, request


def test_capture_and_submit_use_exact_page_once(browser_setup):
    driver, browser, context, page, locators, request = browser_setup
    table = locators[adapter.RESULT_TABLE]
    rows = MagicMock()
    rows.count.return_value = 2
    rows.all.return_value = [Mock(), Mock()]
    rows.all.return_value[0].locator.return_value.all_inner_texts.return_value = ["", "0149", "17:31", "18:18"]
    rows.all.return_value[1].locator.return_value.all_inner_texts.return_value = ["", "0845", "17:00", "18:15"]
    heading = MagicMock()
    heading.first.locator.return_value.all_inner_texts.return_value = ["選擇", "車次", "出發時間", "抵達時間"]
    table.locator.return_value.filter.side_effect = [rows, heading]
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        context.close.assert_not_called()
        locators['[name="homeCaptcha:securityCode"]'].fill.assert_not_called()
        locators["#SubmitButton"].click.assert_not_called()
        assert started.captcha_path.exists()
        result = manager.submit_captcha(started.session_id, " w3N6 ")
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == ["0149"]
    locators['[name="homeCaptcha:securityCode"]'].fill.assert_called_once_with("W3N6")
    locators['[name="toTimeTable"]'].select_option.assert_called_once_with(value="1700")
    locators['select[name="ticketPanel:rows:0:ticketAmount"]'].select_option.assert_called_once_with(label="1")
    assert [key for key, value in locators.items() if value.click.called] == ["#SubmitButton"]
    assert not any(value.check.called for value in locators.values())
    browser.new_context.assert_called_once()
    context.new_page.assert_called_once()
    page.goto.assert_called_once_with(adapter.RESERVATION_URL, wait_until="load")
    context.close.assert_called_once()
    browser.close.assert_called_once()
    driver.stop.assert_called_once()
    assert not started.captcha_path.exists()


def test_shared_driver_survives_closing_one_session(browser_setup):
    driver, browser, context, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        first = manager.start_search(request)
        second = manager.start_search(request)
        adapter.sync_playwright.assert_called_once()
        manager.close_session(first.session_id)
        driver.stop.assert_not_called()
        assert second.captcha_path.exists()
    driver.stop.assert_called_once()
    assert browser.new_context.call_count == 2


@pytest.mark.parametrize("text,status", [
    ("驗證碼輸入錯誤", Status.CAPTCHA_REJECTED),
    ("連線逾時", Status.SESSION_EXPIRED),
    ("伺服器錯誤", Status.UNKNOWN_ERROR),
])
def test_page_feedback(browser_setup, text, status):
    _, _, _, _, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        locators[adapter.FEEDBACK].all_inner_texts.return_value = [text]
        assert manager.submit_captcha(started.session_id, "answer").status == status


def test_http_error_cannot_be_empty_success(browser_setup):
    _, _, _, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        response_handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "response")
        response_handler(Mock(status=429, request=Mock(resource_type="xhr", method="POST", url=adapter.RESERVATION_URL, failure="net::ERR_CONNECTION_RESET")))
        locators["body"].inner_text.return_value = "查無符合條件之車次"
        assert manager.submit_captcha(started.session_id, "answer").status == Status.UNKNOWN_ERROR
        locators["#SubmitButton"].click.assert_not_called()


def test_navigation_failure_releases_partial_browser(browser_setup):
    driver, browser, context, page, _, request = browser_setup
    page.goto.side_effect = RuntimeError("timeout")
    with BookingSessionManager() as manager:
        with pytest.raises(BookingError, match="timeout"):
            manager.start_search(request)
    context.close.assert_called_once()
    browser.close.assert_called_once()
    driver.stop.assert_called_once()


def test_explicit_empty_result_is_success(browser_setup):
    _, _, _, _, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        locators[adapter.FEEDBACK].all_inner_texts.return_value = ["查無符合條件之車次"]
        locators[adapter.RESULT_TABLE].locator.return_value.filter.return_value.count.return_value = 0
        result = manager.submit_captcha(started.session_id, "answer")
        assert result.status == Status.SUCCESS and result.trains == []


def test_timeout_is_not_empty_success_even_with_empty_text(browser_setup):
    _, _, _, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        page.wait_for_function.side_effect = RuntimeError("timeout")
        locators["body"].inner_text.return_value = "查無符合條件之車次"
        result = manager.submit_captcha(started.session_id, "answer")
        assert result.status == Status.UNKNOWN_ERROR


def test_dialog_captcha_rejection_after_submission(browser_setup):
    _, _, _, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "dialog")
        dialog = Mock(message="驗證碼輸入錯誤")
        locators["#SubmitButton"].click.side_effect = lambda **kwargs: handler(dialog)

        def scan_times_out(script, **kwargs):
            if script == adapter._SCAN_JS:
                raise RuntimeError("timeout")

        page.wait_for_function.side_effect = scan_times_out
        result = manager.submit_captcha(started.session_id, "answer")
        assert result.status == Status.CAPTCHA_REJECTED and result.captcha_attempts_remaining == 2
        dialog.dismiss.assert_called_once()
        locators["#SubmitButton"].click.assert_called_once()


def test_rejection_retry_recaptures_and_ignores_stale_outcome(browser_setup):
    _, _, context, page, locators, request = browser_setup
    feedback = locators[adapter.FEEDBACK]
    captcha_input = locators['[name="homeCaptcha:securityCode"]']
    page.evaluate.return_value = 1000.0
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        feedback.all_inner_texts.return_value = ["驗證碼輸入錯誤"]
        first = manager.submit_captcha(started.session_id, "wrong")
        assert first.status == Status.CAPTCHA_REJECTED and first.captcha_attempts_remaining == 2
        context.close.assert_not_called()
        assert locators["#BookingS1Form_homeCaptcha_passCode"].screenshot.call_count == 2
        assert locators['[name="selectStartStation"]'].select_option.call_count == 2
        # The previous rejection is still on the page until the retry submits.
        locators["body"].inner_text.return_value = "驗證碼輸入錯誤"
        locators["#SubmitButton"].click.side_effect = lambda **kwargs: locators["body"].inner_text.configure_mock(
            return_value="查無符合條件之車次")
        feedback.all_inner_texts.return_value = ["查無符合條件之車次"]
        locators[adapter.RESULT_TABLE].locator.return_value.filter.return_value.count.return_value = 0
        second = manager.submit_captcha(started.session_id, "right")
    assert second.status == Status.SUCCESS
    assert [call.args[0] for call in captcha_input.fill.call_args_list] == ["WRONG", "RIGHT"]
    assert locators["#SubmitButton"].click.call_count == 2
    scan_args = [call.kwargs["arg"]["staleOrigin"] for call in page.wait_for_function.call_args_list
                 if call.args[0] == adapter._SCAN_JS]
    assert scan_args == [None, 1000.0]
    context.close.assert_called_once()


def test_retry_timeout_ignores_stale_rejection_on_unchanged_document(browser_setup):
    _, _, _, page, locators, request = browser_setup
    page.evaluate.return_value = 1000.0
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        locators[adapter.FEEDBACK].all_inner_texts.return_value = ["驗證碼輸入錯誤"]
        assert manager.submit_captcha(started.session_id, "wrong").status == Status.CAPTCHA_REJECTED
        locators["body"].inner_text.return_value = "驗證碼輸入錯誤"

        def scan_times_out(script, **kwargs):
            if script == adapter._SCAN_JS:
                raise RuntimeError("timeout")

        page.wait_for_function.side_effect = scan_times_out
        result = manager.submit_captcha(started.session_id, "again")
    assert result.status == Status.UNKNOWN_ERROR
    assert "waiting for train results" in result.message


def test_browser_submits_once_per_captured_captcha(browser_setup, tmp_path):
    _, _, _, _, locators, request = browser_setup
    browser = adapter.PlaywrightBookingBrowser()
    try:
        browser.start(request, tmp_path / "captcha.png")
        locators[adapter.FEEDBACK].all_inner_texts.return_value = ["伺服器錯誤"]
        assert browser.submit("one").status == Status.UNKNOWN_ERROR
        with pytest.raises(BookingError, match="no unanswered CAPTCHA"):
            browser.submit("two")
        with pytest.raises(BookingError, match="No rejected CAPTCHA"):
            browser.refresh_captcha(tmp_path / "captcha.png")
    finally:
        browser.close()
    locators["#SubmitButton"].click.assert_called_once()


def test_failed_data_request_cannot_be_empty_success(browser_setup):
    _, _, _, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "requestfailed")
        locators["#SubmitButton"].click.side_effect = lambda **kwargs: handler(Mock(resource_type="xhr", method="POST", url=adapter.RESERVATION_URL, failure="net::ERR_CONNECTION_RESET"))
        locators[adapter.FEEDBACK].all_inner_texts.return_value = ["查無符合條件之車次"]
        assert manager.submit_captcha(started.session_id, "answer").status == Status.UNKNOWN_ERROR


def test_hidden_flatpickr_date_uses_picker_before_captcha(browser_setup):
    _, _, _, _, locators, request = browser_setup
    date_input = locators['[name="toTimeInputField"]']
    date_input.get_attribute.side_effect = lambda name: "hidden" if name == "type" else None
    date_input.evaluate.return_value = True
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        assert started.captcha_path.exists()
    date_input.fill.assert_not_called()
    assert date_input.evaluate.call_args.args[1] == "2026/09/15"
    date_input.input_value.assert_called_once()


def test_picker_rejected_date_stops_before_captcha(browser_setup):
    driver, browser, context, _, locators, request = browser_setup
    date_input = locators['[name="toTimeInputField"]']
    date_input.evaluate.return_value = True
    date_input.input_value.return_value = ""
    with BookingSessionManager() as manager:
        with pytest.raises(BookingError, match="did not accept"):
            manager.start_search(request)
    locators["#BookingS1Form_homeCaptcha_passCode"].screenshot.assert_not_called()
    locators["#SubmitButton"].click.assert_not_called()
    context.close.assert_called_once()
    browser.close.assert_called_once()
    driver.stop.assert_called_once()


def test_hidden_date_without_picker_fails_without_fill_timeout(browser_setup):
    _, _, _, _, locators, request = browser_setup
    date_input = locators['[name="toTimeInputField"]']
    date_input.get_attribute.side_effect = lambda name: "hidden" if name == "type" else None
    with BookingSessionManager() as manager:
        with pytest.raises(BookingError, match="picker is unavailable"):
            manager.start_search(request)
    date_input.fill.assert_not_called()
    locators["#BookingS1Form_homeCaptcha_passCode"].screenshot.assert_not_called()


def test_plain_visible_date_input_fallback(browser_setup):
    _, _, _, _, locators, request = browser_setup
    with BookingSessionManager() as manager:
        manager.start_search(request)
    locators['[name="toTimeInputField"]'].fill.assert_called_once_with("2026/09/15")


@pytest.mark.parametrize("event", ["requestfailed", "response"])
def test_known_analytics_failure_does_not_block_capture(browser_setup, event):
    _, _, _, page, _, request = browser_setup
    telemetry = Mock(resource_type="fetch", method="POST",
                     url="https://www.google-analytics.com/g/collect?secret=hidden",
                     failure="net::ERR_ABORTED")
    def navigate(*args, **kwargs):
        handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == event)
        handler(telemetry if event == "requestfailed" else Mock(request=telemetry, status=503))
        return Mock(status=200)
    page.goto.side_effect = navigate
    with BookingSessionManager() as manager:
        session = manager.start_search(request)
        assert session.captcha_path.exists()


@pytest.mark.parametrize("url", [
    "https://irs.thsrc.com.tw/IMINT/;jsessionid=private?captcha=SECRET#private",
    "https://unknown.example/data?token=SECRET",
])
def test_unknown_and_reservation_failures_stop_with_redacted_diagnostics(browser_setup, url):
    _, _, _, page, locators, request = browser_setup
    def navigate(*args, **kwargs):
        handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "requestfailed")
        handler(Mock(resource_type="xhr", method="POST", url=url, failure="net::ERR_ABORTED"))
        return Mock(status=200)
    page.goto.side_effect = navigate
    with BookingSessionManager() as manager:
        with pytest.raises(BookingError, match="net::ERR_ABORTED: POST xhr") as error:
            manager.start_search(request)
    assert "SECRET" not in str(error.value) and "private" not in str(error.value)
    locators["#BookingS1Form_homeCaptcha_passCode"].screenshot.assert_not_called()


def test_submission_preserves_network_failure_details(browser_setup):
    _, _, _, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "requestfailed")
        locators["#SubmitButton"].click.side_effect = lambda **kwargs: handler(Mock(
            resource_type="xhr", method="POST", url=adapter.RESERVATION_URL + "?captcha=SECRET",
            failure="net::ERR_CONNECTION_RESET"))
        result = manager.submit_captcha(started.session_id, "answer")
    assert result.status == Status.UNKNOWN_ERROR
    assert "net::ERR_CONNECTION_RESET" in result.message
    assert "irs.thsrc.com.tw/IMINT/" in result.message
    assert "SECRET" not in result.message


def test_train_radios_are_parsed_without_clicking_them(browser_setup):
    _, _, _, page, locators, request = browser_setup
    page.wait_for_function.return_value.json_value.return_value = {"trains": [
        {"code": "0845", "departure": "17:00", "arrival": "18:15", "label": "17:00 arrow_right_alt 18:15"},
        {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": "17:31 arrow_right_alt 18:18"},
    ]}
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer")
    assert result.status == Status.SUCCESS
    assert [(train.number, train.departure.strftime("%H:%M")) for train in result.trains] == [("0149", "17:31")]
    assert [key for key, value in locators.items() if value.click.called] == ["#SubmitButton"]


@pytest.mark.parametrize("hidden_text", ["驗證碼輸入錯誤", "連線逾時", "伺服器錯誤"])
@pytest.mark.parametrize("has_trains", [True, False])
def test_hidden_feedback_does_not_override_visible_outcome(browser_setup, hidden_text, has_trains):
    _, _, _, page, locators, request = browser_setup
    feedback = locators[adapter.FEEDBACK]
    feedback.all_inner_texts.return_value = [hidden_text]
    visible_feedback = Mock()
    visible_feedback.all_inner_texts.return_value = [] if has_trains else ["查無符合條件之車次"]
    feedback.filter.side_effect = lambda **kwargs: visible_feedback if kwargs.get("visible") else feedback
    page.wait_for_function.return_value.json_value.return_value = {"trains": [
        {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
    ] if has_trains else []}
    locators[adapter.RESULT_TABLE].locator.return_value.filter.return_value.count.return_value = 0
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer")
        assert not started.captcha_path.exists()
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == (["0149"] if has_trains else [])
    assert result.captcha_attempts_remaining == 0
    assert locators["#BookingS1Form_homeCaptcha_passCode"].screenshot.call_count == 1


def test_table_fallback_ignores_hidden_tables_rows_and_headings(browser_setup):
    _, _, _, page, locators, request = browser_setup
    page.wait_for_function.return_value.json_value.return_value = {"trains": []}
    tables = locators[adapter.RESULT_TABLE]
    visible_table = MagicMock()
    tables.filter.side_effect = lambda **kwargs: visible_table if kwargs.get("visible") else tables
    # An unfiltered table collection includes an obsolete hidden template.
    tables.locator.return_value.filter.side_effect = AssertionError("Hidden table was read")
    selectable_rows = MagicMock()
    visible_row = Mock()
    visible_row.locator.return_value.all_inner_texts.return_value = ["0149", "17:31", "18:18"]
    hidden_row = Mock()
    hidden_row.locator.return_value.all_inner_texts.return_value = ["0999", "18:00", "19:00"]
    headings = MagicMock()

    def filter_rows(**kwargs):
        if kwargs["has"] is locators["th"]:
            headings.first.locator.return_value.all_inner_texts.return_value = (
                ["車次", "出發時間", "抵達時間"] if kwargs.get("visible") else ["obsolete"]
            )
            return headings
        rows = [visible_row] if kwargs.get("visible") else [hidden_row, visible_row]
        selectable_rows.count.return_value = len(rows)
        selectable_rows.all.return_value = rows
        return selectable_rows

    visible_table.locator.return_value.filter.side_effect = filter_rows
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer")
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == ["0149"]
    hidden_row.locator.assert_not_called()


def test_train_radio_without_number_reports_attribute_names(browser_setup):
    _, _, _, page, locators, request = browser_setup
    page.wait_for_function.return_value.json_value.return_value = {"trains": [
        {"code": None, "label": "22:05 arrow_right_alt 23:10",
         "attributeNames": ["type", "name", "value", "class"]},
    ]}
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer")
    assert result.status == Status.UNKNOWN_ERROR
    assert "radio attributes: type, name, value, class" in result.message
    assert "parsing train results" in result.message


def test_result_wait_timeout_reports_stage_and_safe_page_state(browser_setup):
    from playwright.sync_api import TimeoutError
    _, _, _, page, locators, request = browser_setup
    page.evaluate.return_value = {
        "readyState": "complete", "searchButtonVisible": False,
        "captchaInputVisible": False, "expectedResultTables": 0,
        "visibleTables": [{"className": "changed-results", "rows": 4,
                           "enabledRadios": 3, "headers": ["車次", "出發時間"]}],
    }
    with BookingSessionManager() as manager:
        session = manager.start_search(request)
        page.wait_for_function.side_effect = TimeoutError("sensitive call log: SECRET")
        result = manager.submit_captcha(session.session_id, "SECRET")
    assert result.status == Status.UNKNOWN_ERROR
    assert "waiting for train results" in result.message
    assert "TimeoutError" in result.message
    assert "changed-results" in result.message
    assert "SECRET" not in result.message
    assert not session.captcha_path.exists()
    locators["#SubmitButton"].click.assert_called_once_with(no_wait_after=True)


def test_fill_failure_reports_stage_without_captcha_or_call_log(browser_setup):
    from playwright.sync_api import TimeoutError
    _, _, _, page, locators, request = browser_setup
    page.evaluate.side_effect = RuntimeError("page closed")
    with BookingSessionManager() as manager:
        session = manager.start_search(request)
        locators['[name="homeCaptcha:securityCode"]'].fill.side_effect = TimeoutError('fill("SECRET") timed out')
        result = manager.submit_captcha(session.session_id, "SECRET")
    assert result.status == Status.UNKNOWN_ERROR
    assert "filling CAPTCHA" in result.message
    assert "Page state unavailable" in result.message
    assert "SECRET" not in result.message
    locators["#SubmitButton"].click.assert_not_called()


@pytest.mark.parametrize(("chrome", "compatibility", "expected"), [
    (False, False, {"headless": False}),
    (True, False, {"headless": False, "channel": "chrome"}),
    (False, True, {
        "headless": False, "channel": "chrome",
        "args": adapter.COMPATIBILITY_ARGS,
    }),
])
def test_browser_launch_modes_keep_session_isolated(
    browser_setup, chrome, compatibility, expected,
):
    driver, browser, context, _, _, request = browser_setup
    with BookingSessionManager(chrome=chrome, compatibility=compatibility) as manager:
        started = manager.start_search(request)
        assert started.captcha_path.exists()
        driver.chromium.launch.assert_called_once_with(**expected)
        browser.new_context.assert_called_once_with(locale="zh-TW", timezone_id="Asia/Taipei")
        driver.chromium.launch_persistent_context.assert_not_called()
    context.close.assert_called_once()
    driver.stop.assert_called_once()


def test_chrome_launch_failure_does_not_fallback_or_leak_driver(browser_setup):
    driver, _, _, _, _, request = browser_setup
    driver.chromium.launch.side_effect = RuntimeError("Chrome not installed")
    with BookingSessionManager(chrome=True) as manager:
        with pytest.raises(BookingError, match="Chrome not installed"):
            manager.start_search(request)
    driver.chromium.launch.assert_called_once_with(headless=False, channel="chrome")
    driver.stop.assert_called_once()


@pytest.mark.parametrize("has_outcome", [False, True])
def test_details_ga_failure_requires_independent_search_outcome(browser_setup, has_outcome):
    from playwright.sync_api import TimeoutError
    _, _, _, page, locators, request = browser_setup
    with BookingSessionManager() as manager:
        session = manager.start_search(request)
        handler = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "requestfailed")
        locators["#SubmitButton"].click.side_effect = lambda **kwargs: handler(Mock(
            resource_type="xhr", method="GET",
            url="https://irs.thsrc.com.tw/IMINT/js/GA/DetailsGA.js",
            failure="net::ERR_EMPTY_RESPONSE"))
        if has_outcome:
            locators[adapter.FEEDBACK].all_inner_texts.return_value = ["查無符合條件之車次"]
            locators[adapter.RESULT_TABLE].locator.return_value.filter.return_value.count.return_value = 0
        else:
            page.wait_for_function.side_effect = TimeoutError("No result")
        result = manager.submit_captcha(session.session_id, "answer")
    assert result.status == (Status.SUCCESS if has_outcome else Status.UNKNOWN_ERROR)
    if not has_outcome:
        assert "waiting for train results" in result.message
        assert "TimeoutError" in result.message
    assert "net::ERR_EMPTY_RESPONSE" not in result.message
    assert not session.captcha_path.exists()


def test_ga_exception_does_not_cover_documents_posts_or_other_scripts():
    url = "https://irs.thsrc.com.tw/IMINT/js/GA/DetailsGA.js"
    assert not adapter._is_telemetry(Mock(url=url, method="GET", resource_type="document"))
    assert not adapter._is_telemetry(Mock(url=url, method="POST", resource_type="xhr"))
    assert not adapter._is_telemetry(Mock(
        url="https://irs.thsrc.com.tw/IMINT/js/reservation.js", method="GET", resource_type="xhr"))
