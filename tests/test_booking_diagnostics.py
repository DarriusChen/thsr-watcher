from unittest.mock import MagicMock, Mock

import pytest
from playwright.sync_api import TimeoutError
from typer.testing import CliRunner

from thsr_watcher import booking_diagnostics as diagnostics
from thsr_watcher.booking import BookingError
from thsr_watcher.cli import app


@pytest.fixture
def setup(monkeypatch):
    runtime = MagicMock()
    driver = runtime.__enter__.return_value
    monkeypatch.setattr(diagnostics, "sync_playwright", Mock(return_value=runtime))
    browser = driver.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    return driver, browser, context, page


def test_manual_check_observes_without_operating_form(setup):
    driver, browser, context, page = setup
    lines = []
    def human_wait(*args, **kwargs):
        handlers = {call.args[0]: call.args[1] for call in page.on.call_args_list}
        request = Mock(method="POST", resource_type="document", failure="net::ERR_EMPTY_RESPONSE",
                       url="https://irs.thsrc.com.tw/IMINT/;jsessionid=PRIVATE?captcha=SECRET")
        handlers["requestfailed"](request)
        handlers["response"](Mock(request=request, status=200))
    page.wait_for_event.side_effect = human_wait
    diagnostics.manual_browser_check(lines.append)
    driver.chromium.launch.assert_called_once_with(headless=False, channel="chrome")
    browser.new_context.assert_called_once_with(locale="zh-TW", timezone_id="Asia/Taipei")
    page.goto.assert_called_once_with(diagnostics.RESERVATION_URL, wait_until="domcontentloaded")
    page.wait_for_event.assert_called_once_with("close", timeout=300_000)
    page.locator.assert_not_called()
    page.evaluate.assert_not_called()
    assert "net::ERR_EMPTY_RESPONSE" in "\n".join(lines)
    assert "SECRET" not in "\n".join(lines) and "PRIVATE" not in "\n".join(lines)
    context.close.assert_called_once()
    browser.close.assert_called_once()


def test_manual_check_time_limit_cleans_up(setup):
    _, browser, context, page = setup
    page.wait_for_event.side_effect = TimeoutError("timeout")
    lines = []
    diagnostics.manual_browser_check(lines.append)
    assert any("time limit" in line for line in lines)
    context.close.assert_called_once()
    browser.close.assert_called_once()


def test_manual_check_navigation_failure_cleans_up(setup):
    _, browser, context, page = setup
    page.goto.side_effect = RuntimeError("navigation failure")
    with pytest.raises(BookingError, match="RuntimeError"):
        diagnostics.manual_browser_check(Mock())
    page.wait_for_event.assert_not_called()
    context.close.assert_called_once()
    browser.close.assert_called_once()


def test_manual_check_cli_does_not_require_booking_inputs(monkeypatch):
    check = Mock()
    monkeypatch.setattr(diagnostics, "manual_browser_check", check)
    result = CliRunner().invoke(app, ["booking-manual-check"])
    assert result.exit_code == 0, result.output
    check.assert_called_once()
    assert "does not confirm" in result.output
