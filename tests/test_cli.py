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
