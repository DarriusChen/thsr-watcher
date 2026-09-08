import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

from thsr_watcher import timetable
from thsr_watcher.models import SearchRequest
from thsr_watcher.timetable import TimetableError, parse_timetable


@pytest.fixture
def payload():
    return json.loads((Path(__file__).parent / "fixtures/timetable.json").read_text())


def test_real_response_parsing(payload, request_data):
    result = parse_timetable(payload, SearchRequest(**request_data))
    assert len(result) == 15
    assert result[0].number == "0845"
    assert result[0].departure.isoformat() == "17:11:00"
    assert result[0].arrival.isoformat() == "18:15:00"
    assert result[-1].departure.isoformat() == "19:46:00"
    assert [t.departure for t in result] == sorted(t.departure for t in result)


def test_empty_window(payload, request_data):
    request_data.update(after="00:00", before="01:00")
    assert parse_timetable(payload, SearchRequest(**request_data)) == []


def test_successful_empty_timetable(payload, request_data):
    payload["data"]["DepartureTable"]["TrainItem"] = []
    assert parse_timetable(payload, SearchRequest(**request_data)) == []


@pytest.mark.parametrize("payload", [None, [], {}, {"success": False}, {"success": True}, {"success": True, "data": None}])
def test_bad_response_is_failure(payload, request_data):
    with pytest.raises(TimetableError):
        parse_timetable(payload, SearchRequest(**request_data))


@pytest.mark.parametrize(("field", "value"), [("TrainNumber", None), ("DepartureTime", "oops"), ("DestinationTime", "24:30")])
def test_bad_row_is_not_silently_dropped(payload, request_data, field, value):
    payload["data"]["DepartureTable"]["TrainItem"][0][field] = value
    with pytest.raises(TimetableError, match="unexpected format"):
        parse_timetable(payload, SearchRequest(**request_data))


@pytest.mark.parametrize(("field", "value"), [("StartStationName", "左營"), ("EndStationName", "南港"), ("TitleSplit1", "2026/09/16(三) 17:00")])
def test_wrong_route_or_date(payload, request_data, field, value):
    payload["data"]["DepartureTable"]["Title"][field] = value
    with pytest.raises(TimetableError, match="different route or travel date"):
        parse_timetable(payload, SearchRequest(**request_data))


def test_ambiguous_cross_night_date_fails_explicitly(payload, request_data):
    row = next(r for r in payload["data"]["DepartureTable"]["TrainItem"] if r["DepartureTime"] == "17:11")
    row["IsCrossNight"] = True
    with pytest.raises(TimetableError, match="cross-night"):
        parse_timetable(payload, SearchRequest(**request_data))


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_http_failure_no_retry(status):
    with pytest.raises(TimetableError, match=f"HTTP {status}"):
        timetable._check_status(status)


@pytest.mark.parametrize("text", ["CAPTCHA", "Verify you are human", "人機驗證", "Access Denied"])
def test_challenge_handoff(text):
    page = MagicMock()
    page.locator.return_value.inner_text.return_value = text
    page.locator.return_value.count.return_value = 0
    with pytest.raises(TimetableError, match="Automation stopped"):
        timetable._check_challenge(page)
    page.get_by_role.assert_not_called()


@pytest.mark.parametrize("failure", [None, TimetableError("challenge"), PlaywrightError("timeout")])
def test_browser_cleanup(monkeypatch, request_data, failure):
    runtime = MagicMock()
    monkeypatch.setattr(timetable, "sync_playwright", lambda: runtime)
    browser = runtime.__enter__.return_value.chromium.launch.return_value
    context = browser.new_context.return_value
    search = MagicMock(return_value=[], side_effect=failure)
    monkeypatch.setattr(timetable, "_search_page", search)
    if failure:
        with pytest.raises(TimetableError):
            timetable.search_trains(SearchRequest(**request_data), headless=False)
    else:
        assert timetable.search_trains(SearchRequest(**request_data), headless=False) == []
    runtime.__enter__.return_value.chromium.launch.assert_called_once_with(headless=False)
    context.close.assert_called_once()
    browser.close.assert_called_once()
    runtime.__exit__.assert_called_once()


def test_public_form_boundary(monkeypatch, payload, request_data):
    page = MagicMock()
    page.goto.return_value.status = 200
    page.get_by_role.return_value.is_visible.return_value = False
    response = page.expect_response.return_value.__enter__.return_value.value
    response.status = 200
    response.json.return_value = payload
    monkeypatch.setattr(timetable, "_check_challenge", lambda page: None)
    result = timetable._search_page(page, SearchRequest(**request_data))
    assert result
    page.get_by_title.assert_any_call("出發站", exact=True)
    page.get_by_title.return_value.select_option.assert_any_call(label="台北")
    page.get_by_title.return_value.select_option.assert_any_call(label="台中")
    page.get_by_title.return_value.select_option.assert_any_call(label="單程")
    page.locator.return_value.fill.assert_any_call("2026/09/15")
    page.locator.return_value.fill.assert_any_call("17:00")
    page.get_by_role.assert_any_call("button", name="查詢", exact=True)
    predicate = page.expect_response.call_args.args[0]
    assert predicate(MagicMock(url=timetable.SEARCH_URL, request=MagicMock(method="POST")))
    assert not predicate(MagicMock(url="https://irs.thsrc.com.tw/", request=MagicMock(method="POST")))
    page.expect_response.assert_called_once()


def test_matching_row_wrong_run_date(payload, request_data):
    row = next(r for r in payload["data"]["DepartureTable"]["TrainItem"] if r["DepartureTime"] == "17:11")
    row["RunDate"] = "2026/09/16"
    with pytest.raises(TimetableError, match="different travel date"):
        parse_timetable(payload, SearchRequest(**request_data))


def test_page_http_failure_stops_before_form(request_data):
    page = MagicMock()
    page.goto.return_value.status = 429
    with pytest.raises(TimetableError, match="HTTP 429"):
        timetable._search_page(page, SearchRequest(**request_data))
    page.get_by_title.assert_not_called()
    page.expect_response.assert_not_called()


def test_non_json_response_is_failure(monkeypatch, request_data):
    page = MagicMock()
    page.goto.return_value.status = 200
    response = page.expect_response.return_value.__enter__.return_value.value
    response.status = 200
    response.json.side_effect = ValueError("invalid JSON")
    monkeypatch.setattr(timetable, "_check_challenge", lambda page: None)
    with pytest.raises(TimetableError, match="non-JSON"):
        timetable._search_page(page, SearchRequest(**request_data))


def test_dialog_error_is_reported(monkeypatch, request_data):
    page = MagicMock()
    page.goto.return_value.status = 200
    page.expect_response.side_effect = PlaywrightError("timeout")
    dialog = MagicMock(message="去程日期不可早於現在日期")
    page.on.side_effect = lambda event, handler: handler(dialog)
    monkeypatch.setattr(timetable, "_check_challenge", lambda page: None)
    with pytest.raises(TimetableError, match="去程日期不可早於現在日期"):
        timetable._search_page(page, SearchRequest(**request_data))
    dialog.dismiss.assert_called_once()


def test_context_creation_failure_closes_browser(monkeypatch, request_data):
    runtime = MagicMock()
    monkeypatch.setattr(timetable, "sync_playwright", lambda: runtime)
    browser = runtime.__enter__.return_value.chromium.launch.return_value
    browser.new_context.side_effect = PlaywrightError("context unavailable")
    with pytest.raises(TimetableError, match="Could not run"):
        timetable.search_trains(SearchRequest(**request_data))
    browser.close.assert_called_once()
