from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from thsr_watcher import cli
from thsr_watcher.models import Train
from thsr_watcher.timetable import TimetableError

runner = CliRunner()
ARGS = ["search", "--from", "台北", "--to", "台中", "--date", "2026-09-15", "--after", "17:00", "--before", "20:00"]


def test_search_output(monkeypatch):
    search = Mock(return_value=[Train(number="0149", departure="17:31", arrival="18:18")])
    monkeypatch.setattr(cli, "search_trains", search)
    result = runner.invoke(cli.app, ARGS)
    assert result.exit_code == 0, result.output
    assert "台北 → 台中" in result.output
    assert "2026-09-15 17:00–20:00" in result.output
    assert "Train 0149   17:31 → 18:18" in result.output
    assert "1 trains found" in result.output
    assert search.call_args.kwargs == {"headless": True}


def test_empty_and_headed(monkeypatch):
    search = Mock(return_value=[])
    monkeypatch.setattr(cli, "search_trains", search)
    result = runner.invoke(cli.app, ARGS + ["--headed"])
    assert result.exit_code == 0
    assert "No trains match" in result.output
    assert search.call_args.kwargs == {"headless": False}


def test_retrieval_failure(monkeypatch):
    monkeypatch.setattr(cli, "search_trains", Mock(side_effect=TimetableError("THSR returned HTTP 429")))
    result = runner.invoke(cli.app, ARGS)
    assert result.exit_code == 1
    assert "Timetable search failed: THSR returned HTTP 429" in result.output
    assert "No trains" not in result.output


@pytest.mark.parametrize(("option", "value", "message"), [("--from", "高雄", "unsupported station"), ("--to", "台北", "must be different"), ("--date", "oops", "YYYY-MM-DD"), ("--after", "25:00", "HH:MM"), ("--before", "16:00", "at or before")])
def test_invalid_inputs_do_not_launch_browser(monkeypatch, option, value, message):
    search = Mock()
    monkeypatch.setattr(cli, "search_trains", search)
    args = ARGS.copy()
    args[args.index(option) + 1] = value
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    assert message in result.output
    search.assert_not_called()


@pytest.mark.parametrize("args", [[], ["--help"], ["search", "--help"]])
def test_help_and_entrypoint(args):
    assert runner.invoke(cli.app, args).exit_code == 0


BOOKING_ARGS = ["booking-search", "--from", "台北", "--to", "台中", "--date", "2026-09-15", "--after", "17:00"]


@pytest.mark.parametrize(("extra_args", "chrome", "compatibility"), [
    ([], False, False),
    (["--chrome"], True, False),
    (["--compatibility"], False, True),
])
def test_booking_cli_handoff_and_cleanup(
    monkeypatch, tmp_path, extra_args, chrome, compatibility,
):
    from thsr_watcher.booking_models import BookingSearchResult, BookingSearchStatus, BookingSessionStarted
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    manager = MagicMock()
    manager.__enter__.return_value = manager
    now = datetime.now(timezone.utc)
    manager.start_search.return_value = BookingSessionStarted(session_id="session", captcha_path=tmp_path / "captcha.png", started_at=now, expires_at=now)
    manager.submit_captcha.return_value = BookingSearchResult(status=BookingSearchStatus.SUCCESS, trains=[Train(number="0149", departure="17:31", arrival="18:18")])
    factory = Mock(return_value=manager)
    monkeypatch.setattr(cli, "BookingSessionManager", factory)
    result = runner.invoke(cli.app, BOOKING_ARGS + extra_args, input="human\n")
    assert result.exit_code == 0, result.output
    assert str(tmp_path / "captcha.png") in result.output
    assert "0149" in result.output
    manager.submit_captcha.assert_called_once_with("session", "human")
    manager.__exit__.assert_called_once()
    factory.assert_called_once_with(
        headless=False, chrome=chrome, compatibility=compatibility,
    )


def test_booking_cli_validation_and_help(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(cli, "BookingSessionManager", factory)
    result = runner.invoke(cli.app, BOOKING_ARGS + ["--adults", "0"])
    assert result.exit_code == 2
    assert runner.invoke(cli.app, ["booking-search", "--help"]).exit_code == 0
    factory.assert_not_called()


def test_booking_cli_cancel_closes_manager(monkeypatch):
    from unittest.mock import MagicMock
    manager = MagicMock()
    manager.__enter__.return_value = manager
    monkeypatch.setattr(cli, "BookingSessionManager", Mock(return_value=manager))
    result = runner.invoke(cli.app, BOOKING_ARGS, input="")
    assert result.exit_code == 1
    manager.submit_captcha.assert_not_called()
    manager.__exit__.assert_called_once()
