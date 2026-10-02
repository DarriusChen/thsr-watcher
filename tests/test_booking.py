import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from thsr_watcher.booking import BookingError, BookingSessionManager
from thsr_watcher.booking_models import BookingSearchRequest, BookingSearchResult, BookingSearchStatus as Status
from thsr_watcher.booking_browser import feedback_result, parse_train_radios, parse_train_rows


@pytest.fixture
def booking_request():
    return BookingSearchRequest(origin="台北", destination="台中", travel_date="2026-09-15", after="17:00")


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.answers = []
        self.result = BookingSearchResult(status=Status.SUCCESS)

    def start(self, request, captcha_path):
        self.request = request
        self.path = captcha_path
        captcha_path.write_bytes(b"fake challenge")

    def submit(self, answer):
        assert not self.closed
        self.answers.append(answer)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


@pytest.mark.parametrize("changes", [
    {"origin": "高雄"}, {"destination": "台北"}, {"travel_date": "2026-09-07"},
    {"travel_date": "2026-02-30"}, {"after": "25:00"}, {"after": "17:00:01"},
    {"adult_passengers": 0}, {"adult_passengers": 11}, {"adult_passengers": True},
])
def test_request_validation(booking_request, changes):
    with pytest.raises(ValidationError):
        BookingSearchRequest(**(booking_request.model_dump() | changes))


def test_same_live_session_unique_ids_and_one_attempt(booking_request):
    browsers = []

    def factory():
        browser = FakeBrowser()
        browsers.append(browser)
        return browser

    with BookingSessionManager(browser_factory=factory) as manager:
        first = manager.start_search(booking_request)
        second = manager.start_search(booking_request)
        assert first.session_id != second.session_id
        assert first.captcha_path.exists() and second.captcha_path.exists()
        assert not any(browser.closed for browser in browsers)
        assert manager.submit_captcha(second.session_id, " w3N6 ").status == Status.SUCCESS
        assert browsers[1].answers == ["W3N6"]
        assert browsers[0].answers == []
        assert not second.captcha_path.parent.exists()
        assert first.captcha_path.exists()
        assert manager.submit_captcha(second.session_id, "again").status == Status.SESSION_EXPIRED
        assert browsers[1].answers == ["W3N6"]
    assert all(browser.closed for browser in browsers)
    assert not first.captcha_path.parent.exists()


@pytest.mark.parametrize("status", [Status.CAPTCHA_REJECTED, Status.SESSION_EXPIRED, Status.UNKNOWN_ERROR])
def test_submission_outcomes_close_session(booking_request, status):
    browser = FakeBrowser()
    browser.result = BookingSearchResult(status=status)
    with BookingSessionManager(browser_factory=lambda: browser) as manager:
        started = manager.start_search(booking_request)
        assert manager.submit_captcha(started.session_id, "answer").status == status
        assert browser.closed
        assert not started.captcha_path.exists()


def test_expiry_and_unknown_do_not_submit(booking_request):
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    browser = FakeBrowser()
    with BookingSessionManager(browser_factory=lambda: browser, clock=lambda: now) as manager:
        assert manager.submit_captcha("missing", "answer").status == Status.SESSION_EXPIRED
        session = manager.start_search(booking_request)
        now = session.expires_at
        assert manager.submit_captcha(session.session_id, "answer").status == Status.SESSION_EXPIRED
        assert browser.answers == [] and browser.closed
        assert not session.captcha_path.parent.exists()


def test_explicit_close_sweep_and_context_cancellation(booking_request):
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    with pytest.raises(KeyboardInterrupt):
        with BookingSessionManager(browser_factory=FakeBrowser, clock=lambda: now) as manager:
            first = manager.start_search(booking_request)
            manager.close_session(first.session_id)
            manager.close_session(first.session_id)
            second = manager.start_search(booking_request)
            now += timedelta(minutes=6)
            manager.expire_sessions()
            assert not second.captcha_path.exists()
            third = manager.start_search(booking_request)
            raise KeyboardInterrupt
    assert all(not item.captcha_path.parent.exists() for item in (first, second, third))


def test_browser_failure_is_not_empty_success(booking_request):
    browser = FakeBrowser()
    browser.result = RuntimeError("HTTP 503")
    with BookingSessionManager(browser_factory=lambda: browser) as manager:
        session = manager.start_search(booking_request)
        result = manager.submit_captcha(session.session_id, "answer")
        assert result.status == Status.UNKNOWN_ERROR
        assert browser.closed and not session.captcha_path.exists()


def test_partial_start_failure_removes_files_and_closes_browser(booking_request):
    browser = FakeBrowser()
    original_start = browser.start

    def fail(request, path):
        original_start(request, path)
        raise RuntimeError("page timeout")

    browser.start = fail
    with BookingSessionManager(browser_factory=lambda: browser) as manager:
        with pytest.raises(BookingError, match="page timeout"):
            manager.start_search(booking_request)
    assert browser.closed and not browser.path.parent.exists()


def test_cleanup_failure_still_removes_files_and_closes_other_sessions(booking_request):
    browsers = [FakeBrowser(), FakeBrowser()]
    factory = Mock(side_effect=browsers)
    manager = BookingSessionManager(browser_factory=factory)
    sessions = [manager.start_search(booking_request) for _ in range(2)]
    browsers[0].close = Mock(side_effect=RuntimeError("close failed"))
    with pytest.raises(BookingError):
        manager.close()
    assert browsers[1].closed
    assert all(not session.captcha_path.parent.exists() for session in sessions)


def test_empty_answer_closes_without_submission(booking_request):
    browser = FakeBrowser()
    with BookingSessionManager(browser_factory=lambda: browser) as manager:
        session = manager.start_search(booking_request)
        assert manager.submit_captcha(session.session_id, " ").status == Status.UNKNOWN_ERROR
        assert browser.answers == [] and browser.closed


def test_parse_fixture_and_fail_closed():
    payload = json.loads((Path(__file__).parent / "fixtures/booking_results.json").read_text())
    trains = parse_train_rows(payload["headers"], payload["rows"])
    assert [t.number for t in trains] == ["0845", "0149", "0673"]
    assert trains[0].departure.strftime("%H:%M") == "17:11"
    for headers, rows in [(payload["headers"], []), (["changed"], payload["rows"]), (payload["headers"], [["broken"]])]:
        with pytest.raises(BookingError):
            parse_train_rows(headers, rows)


def test_train_radios_use_querycode_and_fall_back_to_label_times():
    trains = parse_train_radios([
        {"code": "0661", "departure": "", "arrival": "", "label": "22:26 arrow_right_alt 23:29"},
        {"code": "0653", "departure": "22:05", "arrival": "23:10", "label": ""},
    ])
    assert [(t.number, t.departure.strftime("%H:%M"), t.arrival.strftime("%H:%M")) for t in trains] == [
        ("0653", "22:05", "23:10"), ("0661", "22:26", "23:29"),
    ]


@pytest.mark.parametrize("radio", [
    {"code": None, "label": "22:05 arrow_right_alt 23:10", "attributeNames": ["type", "name", "value"]},
    {"code": "0653", "label": "22:05"},
    {"code": "0653", "label": "21:00 22:05 23:10"},
])
def test_train_radio_without_number_or_two_times_is_unknown(radio):
    with pytest.raises(BookingError, match="availability is unknown"):
        parse_train_radios([radio])


def test_feedback_requires_explicit_outcome():
    assert feedback_result("驗證碼輸入錯誤").status == Status.CAPTCHA_REJECTED
    assert feedback_result("連線逾時，請重新操作").status == Status.SESSION_EXPIRED
    assert feedback_result("查無符合條件之車次").status == Status.SUCCESS
    assert feedback_result("請輸入驗證碼") is None
    assert feedback_result("伺服器錯誤") is None
