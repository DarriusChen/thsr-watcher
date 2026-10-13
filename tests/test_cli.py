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


def test_booking_cli_prompts_again_only_while_attempts_remain(monkeypatch, tmp_path):
    from thsr_watcher.booking_models import BookingSearchResult, BookingSearchStatus, BookingSessionStarted
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    manager = MagicMock()
    manager.__enter__.return_value = manager
    now = datetime.now(timezone.utc)
    manager.start_search.return_value = BookingSessionStarted(session_id="session", captcha_path=tmp_path / "captcha.png", started_at=now, expires_at=now)
    manager.submit_captcha.side_effect = [
        BookingSearchResult(status=BookingSearchStatus.CAPTCHA_REJECTED, captcha_attempts_remaining=2),
        BookingSearchResult(status=BookingSearchStatus.CAPTCHA_REJECTED, captcha_attempts_remaining=1),
        BookingSearchResult(status=BookingSearchStatus.CAPTCHA_REJECTED),
    ]
    monkeypatch.setattr(cli, "BookingSessionManager", Mock(return_value=manager))
    result = runner.invoke(cli.app, BOOKING_ARGS, input="one\ntwo\nthree\nfour\n")
    assert result.exit_code == 1
    assert "2 attempts remaining" in result.output and "1 attempts remaining" in result.output
    assert [call.args for call in manager.submit_captcha.call_args_list] == [
        ("session", "one"), ("session", "two"), ("session", "three"),
    ]
    manager.__exit__.assert_called_once()


def test_booking_cli_validation_and_help(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(cli, "BookingSessionManager", factory)
    result = runner.invoke(cli.app, BOOKING_ARGS + ["--adults", "0"])
    assert result.exit_code == 2
    assert runner.invoke(cli.app, ["booking-search", "--help"]).exit_code == 0
    factory.assert_not_called()


def test_book_preset_saves_only_after_yes_and_masks_values(monkeypatch, tmp_path):
    from thsr_watcher import booking_preset

    path = tmp_path / "booking-preset.json"
    monkeypatch.setattr(booking_preset, "default_preset_path", lambda: path)
    saved = runner.invoke(cli.app, ["book-preset"], input="身分證\nA123456789\n0912345678\nprivate@example.com\nn\nyes\n")
    assert saved.exit_code == 0, saved.output
    assert "A123456789" not in saved.output and "0912345678" not in saved.output
    assert "private@example.com" not in saved.output
    assert "membership checkbox off" in saved.output
    assert path.read_text(encoding="utf-8")
    shown = runner.invoke(cli.app, ["book-preset", "show"])
    assert shown.exit_code == 0 and "A123456789" not in shown.output
    cleared = runner.invoke(cli.app, ["book-preset", "clear"])
    assert cleared.exit_code == 0 and not path.exists()


def test_book_preset_rejects_bad_input_without_writing(monkeypatch, tmp_path):
    from thsr_watcher import booking_preset

    path = tmp_path / "booking-preset.json"
    monkeypatch.setattr(booking_preset, "default_preset_path", lambda: path)
    declined = runner.invoke(cli.app, ["book-preset"], input="身分證\nA123456789\n0912345678\n\ny\nno\n")
    assert declined.exit_code == 1 and not path.exists()
    assert "A123456789" not in declined.output
    invalid = runner.invoke(cli.app, ["book-preset"], input="護照\nAB123456\n0912345678\n\ny\n")
    assert invalid.exit_code == 2 and not path.exists()
    assert "AB123456" not in invalid.output and "national ID" in invalid.output


def test_book_uses_preset_without_asking_for_it(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from thsr_watcher.booking_models import (
        BookingSearchResult, BookingSearchStatus, BookingSessionStarted, PickupDetails, ReservationResult,
        ReservationStatus,
    )
    from thsr_watcher.booking_preset import PresetError

    factory = Mock()
    monkeypatch.setattr(cli, "BookingSessionManager", factory)
    monkeypatch.setattr(cli, "load_preset", Mock(side_effect=PresetError("No booking preset. Run book-preset before book.")))
    missing = runner.invoke(cli.app, ["book", "--from", "台北", "--to", "台中", "--date", "2026-09-15", "--after", "17:00"])
    assert missing.exit_code == 2 and "No booking preset" in missing.output
    factory.assert_not_called()

    pickup = PickupDetails(document_type="national_id", document_number="A123456789", mobile="0912345678", use_same_id_membership=True)
    monkeypatch.setattr(cli, "load_preset", Mock(return_value=pickup))
    manager = MagicMock()
    manager.__enter__.return_value = manager
    now = datetime.now(timezone.utc)
    manager.start_search.return_value = BookingSessionStarted(
        session_id="session", captcha_path=tmp_path / "captcha.png", started_at=now, expires_at=now,
    )
    train = Train(number="0149", departure="17:31", arrival="18:18")
    manager.submit_captcha.return_value = BookingSearchResult(status=BookingSearchStatus.SUCCESS, trains=[train])
    manager.select_train.return_value = train
    manager.submit_reservation.return_value = ReservationResult(
        status=ReservationStatus.CONFIRMED, reservation_code="AB12CD34",
        message="Reservation confirmed. No payment was made.",
    )
    factory.return_value = manager
    result = runner.invoke(
        cli.app, ["book", "--from", "台北", "--to", "台中", "--date", "2026-09-15", "--after", "17:00"],
        input="human\n0149\n",
    )
    assert result.exit_code == 0, result.output
    assert "Document number" not in result.output and "A123456789" not in result.output
    assert "AB12CD34" in result.output and "Train number" in result.output
    manager.submit_captcha.assert_called_once_with("session", "human", retain_on_success=True)
    manager.submit_reservation.assert_called_once_with("session", pickup)
    factory.assert_called_once_with(headless=False, chrome=False, compatibility=False, ttl=cli.BOOKING_TTL)


def test_book_reports_uncertain_without_another_prompt(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from thsr_watcher.booking_models import (
        BookingSearchResult, BookingSearchStatus, BookingSessionStarted, PickupDetails, ReservationResult,
        ReservationStatus,
    )

    pickup = PickupDetails(document_type="national_id", document_number="A123456789", mobile="0912345678")
    monkeypatch.setattr(cli, "load_preset", Mock(return_value=pickup))
    manager = MagicMock()
    manager.__enter__.return_value = manager
    now = datetime.now(timezone.utc)
    manager.start_search.return_value = BookingSessionStarted(
        session_id="session", captcha_path=tmp_path / "captcha.png", started_at=now, expires_at=now,
    )
    train = Train(number="0149", departure="17:31", arrival="18:18")
    manager.submit_captcha.return_value = BookingSearchResult(status=BookingSearchStatus.SUCCESS, trains=[train])
    manager.select_train.return_value = train
    manager.submit_reservation.return_value = ReservationResult(
        status=ReservationStatus.UNCERTAIN,
        message="The reservation outcome is uncertain and it may already exist. Check THSR directly. This program will not submit again.",
    )
    monkeypatch.setattr(cli, "BookingSessionManager", Mock(return_value=manager))
    result = runner.invoke(
        cli.app, ["book", "--from", "台北", "--to", "台中", "--date", "2026-09-15", "--after", "17:00"],
        input="human\n0149\n",
    )
    assert result.exit_code == 1
    assert "UNCERTAIN" in result.output and "will not submit again" in result.output
    assert manager.submit_reservation.call_count == 1


def test_booking_cli_cancel_closes_manager(monkeypatch):
    from unittest.mock import MagicMock
    manager = MagicMock()
    manager.__enter__.return_value = manager
    monkeypatch.setattr(cli, "BookingSessionManager", Mock(return_value=manager))
    result = runner.invoke(cli.app, BOOKING_ARGS, input="")
    assert result.exit_code == 1
    manager.submit_captcha.assert_not_called()
    manager.__exit__.assert_called_once()
