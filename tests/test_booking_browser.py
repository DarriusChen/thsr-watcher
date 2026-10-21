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


def test_later_result_pages_are_included_without_another_search(browser_setup):
    _, _, _, page, locators, request = browser_setup
    pages = [
        {"trains": [
            {"code": "0845", "departure": "17:00", "arrival": "18:15", "label": ""},
            {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
        ], "pager": {"kind": "uikit", "hasNext": True, "active": "1"}},
        {"trains": [
            {"code": "0803", "departure": "18:05", "arrival": "19:00", "label": ""},
        ], "pager": {"kind": "uikit", "hasNext": False, "active": "2"}},
    ]

    def wait(script, **kwargs):
        result = Mock()
        if script == adapter._SCAN_JS:
            result.json_value.return_value = pages.pop(0)
        return result

    page.wait_for_function.side_effect = wait
    locators[adapter.RESULT_NEXT].count.return_value = 1
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        later = manager.load_later_trains(started.session_id)
        retained = manager._sessions[started.session_id]
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == ["0149"]
    assert later.status == Status.SUCCESS
    assert [train.number for train in later.trains] == ["0149", "0803"]
    locators[adapter.RESULT_NEXT].click.assert_called_once_with(no_wait_after=True)
    locators["#SubmitButton"].click.assert_called_once()
    assert not any(value.check.called for value in locators.values())
    assert retained.browser._train_page["0803"] == "2"


def test_later_train_button_is_followed_in_the_same_search(browser_setup):
    _, _, _, page, locators, request = browser_setup
    pages = [
        {"trains": [
            {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
        ], "pager": {"kind": "later", "hasNext": True, "active": ""}},
        {"trains": [
            {"code": "0803", "departure": "18:05", "arrival": "19:00", "label": ""},
        ], "pager": {"kind": "none", "hasNext": False, "active": ""}},
    ]

    def wait(script, **kwargs):
        result = Mock()
        if script == adapter._SCAN_JS:
            result.json_value.return_value = pages.pop(0)
        return result

    later = Mock()
    later.count.return_value = 1
    later.is_visible.return_value = False

    def get_by_role(role, name=None, **kwargs):
        if role == "button" and getattr(name, "pattern", None) == adapter.LATER_TRAINS.pattern:
            return later
        hidden = Mock()
        hidden.is_visible.return_value = False
        hidden.count.return_value = 0
        return hidden

    page.wait_for_function.side_effect = wait
    page.get_by_role.side_effect = get_by_role
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        loaded = manager.load_later_trains(started.session_id)
        retained = manager._sessions[started.session_id]
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == ["0149"]
    assert loaded.status == Status.SUCCESS
    assert [train.number for train in loaded.trains] == ["0149", "0803"]
    later.click.assert_called_once_with(no_wait_after=True)
    locators["#SubmitButton"].click.assert_called_once()
    assert retained.browser._result_nav == "later"
    assert retained.browser._train_page == {"0149": "0", "0803": "1"}
    assert retained.browser._result_page_index == 1


def test_step_result_page_replaces_the_list_with_one_page(browser_setup):
    _, _, _, page, _, request = browser_setup
    pages = [
        {"trains": [
            {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
        ], "pager": {"kind": "later", "hasNext": True, "hasEarlier": False, "active": ""}},
        {"trains": [
            {"code": "0803", "departure": "18:05", "arrival": "19:00", "label": ""},
        ], "pager": {"kind": "later", "hasNext": True, "hasEarlier": True, "active": ""}},
        {"trains": [
            {"code": "0999", "departure": "19:05", "arrival": "20:00", "label": ""},
        ], "pager": {"kind": "none", "hasNext": False, "hasEarlier": True, "active": ""}},
    ]

    def wait(script, **kwargs):
        result = Mock()
        if script == adapter._SCAN_JS:
            result.json_value.return_value = pages.pop(0)
        return result

    later = Mock()
    later.count.return_value = 1

    def get_by_role(role, name=None, **kwargs):
        if role == "button" and getattr(name, "pattern", None) == adapter.LATER_TRAINS.pattern:
            return later
        hidden = Mock()
        hidden.is_visible.return_value = False
        hidden.count.return_value = 0
        return hidden

    page.wait_for_function.side_effect = wait
    page.get_by_role.side_effect = get_by_role
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        stepped = manager.step_trains(started.session_id, "later")
        retained = manager._sessions[started.session_id]
    assert stepped.status == Status.SUCCESS
    assert [train.number for train in stepped.trains] == ["0803"]
    assert [train.number for train in retained.trains] == ["0803"]
    assert retained.browser.has_earlier_results() is True
    assert retained.browser.can_step_later() is True
    later.click.assert_called_once_with(no_wait_after=True)
    assert len(pages) == 1


@pytest.mark.parametrize("direction", ["earlier", "later"])
@pytest.mark.parametrize("include_overlap", [True, False])
def test_explicit_page_navigation_keeps_trains_before_initial_after(browser_setup, direction, include_overlap):
    _, _, _, page, locators, request = browser_setup
    request = BookingSearchRequest.model_validate(request.model_dump() | {"after": "14:00"})
    locators['[name="toTimeTable"]'].locator.return_value.evaluate_all.return_value = [
        {"text": "14:00", "value": "1400"},
    ]

    def scan(trains):
        return {
            "trains": [
                {"code": number, "departure": departure, "arrival": "18:00", "label": ""}
                for number, departure in trains
            ],
            "pager": {"kind": "later", "hasNext": True, "hasEarlier": True},
        }

    page.wait_for_function.return_value.json_value.return_value = scan([
        ("829", "13:19"), ("833", "14:19"),
    ])
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        initial = manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        assert [train.number for train in initial.trains] == ["833"]
        browser = manager._sessions[started.session_id].browser
        adjacent_trains = [("825", "12:19"), ("829", "13:19")]
        if include_overlap:
            adjacent_trains.append(("833", "14:19"))
        adjacent = scan(adjacent_trains)
        browser._read_earlier_result_page = Mock(return_value=adjacent)
        browser._read_next_result_page = Mock(return_value=adjacent)
        browser.select_train = Mock()
        stepped = manager.step_trains(started.session_id, direction)
        assert stepped.status == Status.SUCCESS
        assert [train.number for train in stepped.trains] == [number for number, _ in adjacent_trains]
        browser.select_train.assert_not_called()
        selected = manager.select_train(started.session_id, "825")
        assert isinstance(selected, adapter.Train)
        assert selected.number == "825"
        browser.select_train.assert_called_once_with("825")
    locators["#SubmitButton"].click.assert_called_once_with(no_wait_after=True)


@pytest.mark.parametrize("action", ["step", "load"])
@pytest.mark.parametrize("surface", ["panel", "body"])
def test_paged_timeout_is_not_reported_as_bookable(browser_setup, action, surface):
    _, _, _, page, locators, request = browser_setup
    pages = [
        {"trains": [
            {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
        ], "pager": {"kind": "later", "hasNext": True, "hasEarlier": False, "active": ""}},
        {"trains": [
            {"code": "0803", "departure": "18:05", "arrival": "19:00", "label": ""},
        ], "pager": {"kind": "later", "hasNext": False, "hasEarlier": True, "active": ""}},
    ]

    def wait(script, **kwargs):
        result = Mock()
        if script == adapter._SCAN_JS:
            if len(pages) == 1:
                if surface == "panel":
                    locators[adapter.FEEDBACK].all_inner_texts.return_value = ["連線逾時"]
                else:
                    locators["body"].inner_text.return_value = "車次列表 連線逾時，請重新操作"
            result.json_value.return_value = pages.pop(0)
        return result

    later = Mock()
    later.count.return_value = 1

    def get_by_role(role, name=None, **kwargs):
        if role == "button" and getattr(name, "pattern", None) == adapter.LATER_TRAINS.pattern:
            return later
        hidden = Mock()
        hidden.is_visible.return_value = False
        hidden.count.return_value = 0
        return hidden

    page.wait_for_function.side_effect = wait
    page.get_by_role.side_effect = get_by_role
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        opened = manager.step_trains(started.session_id, "later") if action == "step" else manager.load_later_trains(started.session_id)
        retained = manager._sessions[started.session_id]
    assert opened.status == Status.UNKNOWN_ERROR
    assert opened.trains == []
    assert "連線逾時" in opened.message
    assert [train.number for train in retained.trains] == ["0149"]


def test_unrecognized_result_pager_does_not_return_a_partial_list(browser_setup):
    _, _, _, page, locators, request = browser_setup
    page.wait_for_function.return_value.json_value.return_value = {"trains": [
        {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
    ], "pager": {"kind": "unknown", "hasNext": False, "active": ""}}
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        later = manager.load_later_trains(started.session_id)
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == ["0149"]
    assert later.status == Status.UNKNOWN_ERROR
    assert later.trains == []
    assert "pagination was not recognized" in later.message
    assert locators[adapter.RESULT_NEXT].click.called is False


def test_next_result_page_timeout_does_not_return_a_partial_list(browser_setup):
    _, _, _, page, locators, request = browser_setup
    scans = {"n": 0}

    def wait(script, **kwargs):
        if script != adapter._SCAN_JS:
            return Mock()
        scans["n"] += 1
        if scans["n"] > 1:
            raise TimeoutError("next page")
        result = Mock()
        result.json_value.return_value = {"trains": [
            {"code": "0149", "departure": "17:31", "arrival": "18:18", "label": ""},
        ], "pager": {"kind": "uikit", "hasNext": True, "active": "1"}}
        return result

    page.wait_for_function.side_effect = wait
    locators[adapter.RESULT_NEXT].count.return_value = 1
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        result = manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        later = manager.load_later_trains(started.session_id)
    assert result.status == Status.SUCCESS
    assert [train.number for train in result.trains] == ["0149"]
    assert later.status == Status.UNKNOWN_ERROR
    assert later.trains == []
    assert "next result page did not appear" in later.message
    locators[adapter.RESULT_NEXT].click.assert_called_once()


@pytest.mark.parametrize("count,status,clicks", [
    (10, Status.SUCCESS, 9),
    (11, Status.UNKNOWN_ERROR, 9),
    (12, Status.UNKNOWN_ERROR, 9),
])
def test_result_page_limit_includes_the_first_page(browser_setup, count, status, clicks):
    _, _, _, page, locators, request = browser_setup
    queued = [{
        "trains": [{"code": str(1000 + index), "departure": "18:00", "arrival": "19:00", "label": ""}],
        "pager": {"kind": "uikit", "hasNext": index < count - 1, "active": str(index + 1)},
    } for index in range(count)]

    def wait(script, **kwargs):
        result = Mock()
        if script == adapter._SCAN_JS:
            result.json_value.return_value = queued.pop(0)
        return result

    page.wait_for_function.side_effect = wait
    locators[adapter.RESULT_NEXT].count.return_value = 1
    with BookingSessionManager() as manager:
        started = manager.start_search(request)
        manager.submit_captcha(started.session_id, "answer", retain_on_success=True)
        later = manager.load_later_trains(started.session_id)
        retained = manager._sessions[started.session_id]
    assert later.status == status
    assert locators[adapter.RESULT_NEXT].click.call_count == clicks
    if status == Status.SUCCESS:
        assert len(later.trains) == 10
    else:
        assert later.trains == []
        assert "read limit" in later.message
        assert [train.number for train in retained.trains] == ["1000"]


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


def _control(count=1, text="", required=False, checked=False, value=None):
    control = MagicMock()
    control.count.return_value = count
    control.inner_text.return_value = text
    control.is_checked.return_value = checked

    def attribute(name):
        if name == "required" and required:
            return ""
        if name == "value":
            return value
        return None

    control.get_attribute.side_effect = attribute
    return control


def _options(*pairs):
    nodes = []
    for value, text in pairs:
        option = MagicMock()
        option.inner_text.return_value = text
        option.get_attribute.side_effect = lambda name, value=value: value if name == "value" else None
        nodes.append(option)
    found = MagicMock()
    found.count.return_value = len(nodes)
    found.nth.side_effect = lambda index: nodes[index]
    return found


def _visible(control):
    host = MagicMock()
    host.locator.return_value = control
    return host


def _reservation_page(body_text="取票資訊"):
    browser = adapter.PlaywrightBookingBrowser()
    page = MagicMock()
    browser._page = page
    body = MagicMock()
    body.inner_text.return_value = body_text
    document = _control()
    document.locator.return_value = _options(("0", "身分證字號"), ("1", "護照/居留證/入出境許可證號碼"))
    controls = {
        "body": body,
        adapter.DOCUMENT_TYPE: _visible(document),
        adapter.DOCUMENT_NUMBER: _visible(_control()),
        adapter.MOBILE_NUMBER: _visible(_control()),
        adapter.EMAIL_FIELD: _control(count=0),
        adapter.MEMBERSHIP_TGO: _visible(_control()),
        adapter.MEMBERSHIP_NONE: _visible(_control()),
        adapter.MEMBERSHIP_BOX: _control(),
        adapter.TERMS_CHECKBOX: _visible(_control()),
        adapter.MEMBERSHIP_DIALOG: MagicMock(),
    }
    confirm = _control(text="確定")
    confirm.locator.return_value = confirm
    controls[adapter.MEMBERSHIP_DIALOG].get_by_role.return_value = confirm
    page.locator.side_effect = lambda selector: controls.get(selector, MagicMock(count=MagicMock(return_value=0)))
    page.evaluate.return_value = ["select idInputRadio"]
    button = _control(text="完成訂位")
    roles = {("button", adapter.RESERVATION_BUTTON.pattern, None): button}

    def get_by_role(role, name=None, exact=None):
        pattern = getattr(name, "pattern", name)
        return roles[(role, pattern, exact)]

    page.get_by_role.side_effect = get_by_role
    return browser, page, controls, button


def test_reservation_outcome_keeps_personal_text_out_of_the_result():
    from thsr_watcher.booking_models import ReservationStatus

    secret = "A123456789"
    confirmed = adapter.reservation_outcome(f"取票人 {secret} 訂位代號 AB12CD34")
    assert confirmed.status == ReservationStatus.CONFIRMED
    assert confirmed.reservation_code == "AB12CD34"
    assert secret not in confirmed.message
    rejected = adapter.reservation_outcome(f"{secret} 此車次已額滿")
    assert rejected.status == ReservationStatus.REJECTED and secret not in rejected.message
    uncertain = adapter.reservation_outcome("訂位代號 AB12CD34 訂位代號 ZZ99YY88")
    assert uncertain.status == ReservationStatus.UNCERTAIN
    assert "AB12CD34" not in uncertain.message and "ZZ99YY88" not in uncertain.message
    assert adapter.reservation_outcome("請確認訂位資料") is None
    table = (
        "取票識別碼\tA123456789\n"
        "序號\t訂位代號\t交易狀態\n"
        "1\tAB12CD34\t未付款（付款期限：2026-10-14）\n"
    )
    listed = adapter.reservation_outcome(table)
    assert listed.status == ReservationStatus.CONFIRMED
    assert listed.reservation_code == "AB12CD34"
    assert secret not in listed.message


@pytest.mark.parametrize("field", ["取票人護照 P1234567", "乘車日期 20261022"])
@pytest.mark.parametrize("rejection", ["", "訂位失敗\n", "無法完成訂位\n"])
def test_reservation_outcome_does_not_scan_into_other_fields(field, rejection):
    from thsr_watcher.booking_models import ReservationStatus

    result = adapter.reservation_outcome(f"訂位代號：\n{rejection}{field}")
    if rejection:
        assert result.status == ReservationStatus.REJECTED
        assert result.reservation_code is None
        assert field not in result.message
    else:
        assert result is None


@pytest.mark.parametrize("text", [
    "訂位代號： AB12CD34",
    "訂位代號\nAB12CD34",
    "訂位代號：\n12345678",
    "序號\t交易狀態\t訂位代號\n1\t未付款\tAB12CD34",
])
def test_reservation_outcome_reads_only_the_code_value(text):
    from thsr_watcher.booking_models import ReservationStatus

    result = adapter.reservation_outcome(text)
    assert result.status == ReservationStatus.CONFIRMED
    assert result.reservation_code in {"AB12CD34", "12345678"}


def test_reservation_outcome_with_code_and_rejection_is_uncertain():
    from thsr_watcher.booking_models import ReservationStatus

    result = adapter.reservation_outcome("訂位代號 AB12CD34\n訂位失敗")
    assert result.status == ReservationStatus.UNCERTAIN
    assert result.reservation_code is None
    assert "AB12CD34" not in result.message


def test_membership_checkbox_and_single_reservation_click():
    from thsr_watcher.booking_models import DocumentType, PickupDetails, ReservationStatus

    browser, _, controls, button = _reservation_page("訂位代號 AB12CD34")
    pickup = PickupDetails(
        document_type=DocumentType.NATIONAL_ID, document_number="A123456789",
        mobile="0912345678", use_same_id_membership=True,
    )
    result = browser.submit_reservation(pickup)
    document = controls[adapter.DOCUMENT_TYPE].locator.return_value
    number = controls[adapter.DOCUMENT_NUMBER].locator.return_value
    assert result.status == ReservationStatus.CONFIRMED
    document.select_option.assert_called_once_with(value="0")
    number.fill.assert_called_once_with("A123456789")
    controls[adapter.MEMBERSHIP_TGO].locator.return_value.check.assert_called_once()
    controls[adapter.MEMBERSHIP_BOX].check.assert_called_once()
    controls[adapter.TERMS_CHECKBOX].locator.return_value.check.assert_called_once()
    button.click.assert_called_once()
    controls[adapter.MEMBERSHIP_DIALOG].get_by_role.return_value.click.assert_called_once()
    assert "A123456789" not in result.message


def test_missing_membership_confirmation_does_not_click_again():
    from thsr_watcher.booking_models import DocumentType, PickupDetails, ReservationStatus

    browser, _, controls, button = _reservation_page()
    confirm = controls[adapter.MEMBERSHIP_DIALOG].get_by_role.return_value
    confirm.wait_for.side_effect = TimeoutError("dialog")
    pickup = PickupDetails(
        document_type=DocumentType.NATIONAL_ID, document_number="A123456789",
        mobile="0912345678", use_same_id_membership=True,
    )
    result = browser.submit_reservation(pickup)
    assert result.status == ReservationStatus.UNCERTAIN
    assert button.click.call_count == 1 and confirm.click.call_count == 0
    assert pickup.document_number not in result.message


def test_passport_uses_the_combined_document_option():
    from thsr_watcher.booking_models import DocumentType, PickupDetails

    browser, _, controls, button = _reservation_page("訂位代號 AB12CD34")
    pickup = PickupDetails(document_type=DocumentType.PASSPORT, document_number="P1234567", mobile="0912345678")
    browser.submit_reservation(pickup)
    document = controls[adapter.DOCUMENT_TYPE].locator.return_value
    document.select_option.assert_called_once_with(value="1")
    button.click.assert_called_once()


def test_skipped_membership_is_unchecked_and_a_missing_checkbox_does_not_submit():
    from thsr_watcher.booking import BookingError
    from thsr_watcher.booking_models import DocumentType, PickupDetails

    browser, _, controls, button = _reservation_page()
    checkbox = controls[adapter.MEMBERSHIP_BOX]
    checkbox.is_checked.return_value = True
    pickup = PickupDetails(document_type=DocumentType.NATIONAL_ID, document_number="A123456789", mobile="0912345678")
    browser.submit_reservation(pickup)
    checkbox.uncheck.assert_called_once()
    checkbox.check.assert_not_called()
    controls[adapter.MEMBERSHIP_NONE].locator.return_value.check.assert_called_once()
    controls[adapter.MEMBERSHIP_DIALOG].get_by_role.return_value.click.assert_not_called()
    assert button.click.call_count == 1

    checkbox.count.return_value = 0
    button.click.reset_mock()
    wanted = pickup.model_copy(update={"use_same_id_membership": True})
    with pytest.raises(BookingError, match="Membership checkbox was not found") as exc:
        browser.submit_reservation(wanted)
    assert button.click.call_count == 0
    assert wanted.document_number not in str(exc.value) and wanted.mobile not in str(exc.value)


def test_required_email_and_failed_fill_do_not_click_or_echo_secrets():
    from thsr_watcher.booking import BookingError
    from thsr_watcher.booking_models import DocumentType, PickupDetails

    browser, _, controls, button = _reservation_page()
    email = controls[adapter.EMAIL_FIELD]
    email.count.return_value = 1
    email.get_attribute.side_effect = lambda name: "" if name == "required" else None
    pickup = PickupDetails(document_type=DocumentType.NATIONAL_ID, document_number="A123456789", mobile="0912345678")
    with pytest.raises(BookingError, match="requires an email") as exc:
        browser.submit_reservation(pickup)
    assert button.click.call_count == 0 and pickup.document_number not in str(exc.value)

    email.count.return_value = 0
    number = controls[adapter.DOCUMENT_NUMBER].locator.return_value
    number.fill.side_effect = RuntimeError("filled A123456789")
    with pytest.raises(BookingError, match="Reservation was not submitted") as exc:
        browser.submit_reservation(pickup)
    assert "A123456789" not in str(exc.value) and button.click.call_count == 0


def test_outcome_after_the_only_click_is_uncertain_or_rejected():
    from thsr_watcher.booking_models import DocumentType, PickupDetails, ReservationStatus

    browser, page, _, button = _reservation_page("此車次已額滿，證件 A123456789")
    pickup = PickupDetails(document_type=DocumentType.NATIONAL_ID, document_number="A123456789", mobile="0912345678")
    rejected = browser.submit_reservation(pickup)
    assert rejected.status == ReservationStatus.REJECTED and button.click.call_count == 1
    assert "A123456789" not in rejected.message

    page.wait_for_function.side_effect = TimeoutError("A123456789")
    button.click.reset_mock()
    uncertain = browser.submit_reservation(pickup)
    assert uncertain.status == ReservationStatus.UNCERTAIN and button.click.call_count == 1
    assert "A123456789" not in uncertain.message and "will not submit again" in uncertain.message


def test_payment_label_and_missing_train_are_not_clicked():
    from thsr_watcher.booking import BookingError

    browser, page, _, button = _reservation_page()
    button.get_attribute.side_effect = lambda name: "付款" if name == "value" else None
    from thsr_watcher.booking_models import DocumentType, PickupDetails
    pickup = PickupDetails(document_type=DocumentType.NATIONAL_ID, document_number="A123456789", mobile="0912345678")
    with pytest.raises(BookingError, match="Payment controls"):
        browser.submit_reservation(pickup)
    button.click.assert_not_called()

    radio = MagicMock()
    radio.count.return_value = 0
    body = MagicMock()
    body.inner_text.return_value = "車次列表"

    def locator(selector):
        if selector == "body":
            return body
        found = MagicMock()
        found.locator.return_value = radio
        found.count.return_value = 0
        return found

    page.locator.side_effect = locator
    with pytest.raises(BookingError, match="nothing was clicked"):
        browser.select_train("0149")
    radio.check.assert_not_called()


def test_select_train_checks_one_radio_and_one_confirm_button():
    browser = adapter.PlaywrightBookingBrowser()
    page = MagicMock()
    browser._page = page
    body = MagicMock()
    body.inner_text.return_value = "車次列表"
    radio = MagicMock()
    radio.count.return_value = 1
    challenge = MagicMock()
    challenge.count.return_value = 0

    def locator(selector):
        if selector == "body":
            return body
        if "querycode" in selector:
            found = MagicMock()
            found.locator.return_value = radio
            return found
        return challenge

    page.locator.side_effect = locator
    confirm = _control()
    pickup = MagicMock()
    page.get_by_role.side_effect = lambda role, name=None, exact=None: confirm if role == "button" else pickup
    browser.select_train("0149")
    radio.check.assert_called_once()
    confirm.click.assert_called_once()
    pickup.first.wait_for.assert_called_once()


def test_select_train_does_not_leave_the_current_page():
    browser = adapter.PlaywrightBookingBrowser()
    page = MagicMock()
    browser._page = page
    browser._has_earlier = True
    body = MagicMock()
    body.inner_text.return_value = "車次列表"
    radio = MagicMock()
    radio.count.return_value = 0
    earlier = MagicMock()
    earlier.count.return_value = 1
    challenge = MagicMock()
    challenge.count.return_value = 0

    def locator(selector):
        if selector == "body":
            return body
        if "querycode" in selector:
            found = MagicMock()
            found.locator.return_value = radio
            return found
        if selector == '[data-thsr-watcher-earlier="1"]':
            return earlier
        return challenge

    page.locator.side_effect = locator
    with pytest.raises(BookingError, match="nothing was clicked"):
        browser.select_train("0149")
    earlier.click.assert_not_called()
    radio.check.assert_not_called()


def test_ga_exception_does_not_cover_documents_posts_or_other_scripts():
    url = "https://irs.thsrc.com.tw/IMINT/js/GA/DetailsGA.js"
    assert not adapter._is_telemetry(Mock(url=url, method="GET", resource_type="document"))
    assert not adapter._is_telemetry(Mock(url=url, method="POST", resource_type="xhr"))
    assert not adapter._is_telemetry(Mock(
        url="https://irs.thsrc.com.tw/IMINT/js/reservation.js", method="GET", resource_type="xhr"))


@pytest.mark.parametrize("blocked_at", ["terms", "membership_dialog"])
def test_access_error_prevents_next_reservation_click(blocked_at):
    from thsr_watcher.booking_models import PickupDetails, ReservationStatus

    browser, _, controls, button = _reservation_page()
    pickup = PickupDetails(
        document_type="national_id", document_number="A123456789", mobile="0912345678",
        use_same_id_membership=True,
    )

    def blocked(*args, **kwargs):
        browser._request_errors.append("HTTP 403")

    confirm = controls[adapter.MEMBERSHIP_DIALOG].get_by_role.return_value
    if blocked_at == "terms":
        controls[adapter.TERMS_CHECKBOX].locator.return_value.check.side_effect = blocked
        with pytest.raises(BookingError, match="HTTP 403"):
            browser.submit_reservation(pickup)
        button.click.assert_not_called()
    else:
        confirm.wait_for.side_effect = blocked
        result = browser.submit_reservation(pickup)
        assert result.status == ReservationStatus.UNCERTAIN
        button.click.assert_called_once()
    confirm.click.assert_not_called()
