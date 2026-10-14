"""Process-local, single-owner booking sessions; no messaging or browser selectors."""

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from thsr_watcher.booking_models import (
    BookingSearchRequest, BookingSearchResult, BookingSearchStatus,
    BookingSessionStarted, PickupDetails, ReservationResult, ReservationStatus,
)
from thsr_watcher.models import Train


CAPTCHA_FILE = "captcha.png"


class BookingError(Exception):
    """A session could not be prepared or its resources could not be released."""


class BookingBrowser(Protocol):
    def start(self, request: BookingSearchRequest, captcha_path: Path) -> None: ...
    def submit(self, captcha_text: str) -> BookingSearchResult: ...
    def refresh_captcha(self, captcha_path: Path) -> None: ...
    def has_later_results(self) -> bool: ...
    def has_earlier_results(self) -> bool: ...
    def can_step_later(self) -> bool: ...
    def load_later_trains(self) -> BookingSearchResult: ...
    def step_result_page(self, direction: str) -> BookingSearchResult: ...
    def select_train(self, train_number: str) -> None: ...
    def submit_reservation(self, pickup: PickupDetails) -> ReservationResult: ...
    def close(self) -> None: ...


@dataclass
class _Session:
    browser: BookingBrowser
    directory: Path
    expires_at: datetime
    attempts: int = 0
    trains: list[Train] | None = None
    selected: Train | None = None

    @property
    def captcha_path(self) -> Path:
        return self.directory / CAPTCHA_FILE


class BookingSessionManager:
    """Use on one thread, in one process (including all Playwright calls).

    Context-manager exit closes abandoned sessions. The local TTL is a resource
    policy, not a claim about THSR's expiration time. Call expire_sessions() from
    a long-lived owner's lifecycle, or close_session() when a user cancels.
    """

    def __init__(
        self, *, headless: bool = False, chrome: bool = False,
        compatibility: bool = False,
        browser_factory: Callable[[], BookingBrowser] | None = None,
        ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_captcha_attempts: int = 3,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Session TTL must be positive")
        if type(max_captcha_attempts) is not int or not 1 <= max_captcha_attempts <= 3:
            raise ValueError("CAPTCHA attempts must be an integer from 1 to 3")
        if browser_factory is None:
            from thsr_watcher.booking_browser import PlaywrightBookingFactory
            browser_factory = PlaywrightBookingFactory(
                headless=headless, chrome=chrome, compatibility=compatibility,
            )
        self._factory = browser_factory
        self._ttl = ttl
        self._clock = clock
        self._max_captcha_attempts = max_captcha_attempts
        self._sessions: dict[str, _Session] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start_search(self, request: BookingSearchRequest) -> BookingSessionStarted:
        self.expire_sessions()
        session_id = uuid4().hex
        directory = Path(tempfile.mkdtemp(prefix=f"thsr-watcher-{session_id}-"))
        browser = None
        try:
            browser = self._factory()
            path = directory / CAPTCHA_FILE
            browser.start(request, path)
            started = self._clock()
            expires = started + self._ttl
            self._sessions[session_id] = _Session(browser, directory, expires)
            return BookingSessionStarted(
                session_id=session_id, captcha_path=path,
                started_at=started, expires_at=expires,
            )
        except BaseException as exc:
            try:
                if browser is not None:
                    browser.close()
            finally:
                shutil.rmtree(directory)
            if isinstance(exc, Exception):
                raise BookingError("Could not prepare reservation search: " + str(exc)) from exc
            raise

    def submit_captcha(
        self, session_id: str, captcha_text: str, *, retain_on_success: bool = False,
    ) -> BookingSearchResult:
        session = self._sessions.get(session_id)
        if session is None:
            return BookingSearchResult(
                status=BookingSearchStatus.SESSION_EXPIRED,
                message="Session is unknown or already closed; explicitly start a new search.",
            )
        keep_open = False
        try:
            if self._clock() >= session.expires_at:
                return BookingSearchResult(
                    status=BookingSearchStatus.SESSION_EXPIRED,
                    message="Local session deadline elapsed; no search was submitted.",
                )
            if not isinstance(captcha_text, str) or not captcha_text.strip():
                return BookingSearchResult(
                    status=BookingSearchStatus.UNKNOWN_ERROR,
                    message="A nonempty human CAPTCHA answer is required; session closed.",
                )
            # This is the exact object that captured the challenge, never a new browser.
            result = session.browser.submit(captcha_text.strip().upper())
            session.attempts += 1
            remaining = self._max_captcha_attempts - session.attempts
            if result.status == BookingSearchStatus.SUCCESS and retain_on_success:
                session.trains = list(result.trains)
                keep_open = True
                return result
            if result.status != BookingSearchStatus.CAPTCHA_REJECTED or remaining <= 0:
                return result
            try:
                session.browser.refresh_captcha(session.captcha_path)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, BookingError) else type(exc).__name__
                raise BookingError(
                    f"{result.message} A new CAPTCHA could not be captured ({detail}); session closed."
                ) from exc
            keep_open = True
            return BookingSearchResult(
                status=result.status,
                message=result.message + " A new CAPTCHA was captured in the same session.",
                captcha_attempts_remaining=remaining,
            )
        except BookingError as exc:
            return BookingSearchResult(
                status=BookingSearchStatus.UNKNOWN_ERROR,
                message=str(exc),
            )
        except Exception:
            return BookingSearchResult(
                status=BookingSearchStatus.UNKNOWN_ERROR,
                message="Browser or parser failed; availability is unknown. No retry was attempted.",
            )
        finally:
            if not keep_open:
                self.close_session(session_id)

    def has_later_trains(self, session_id: str) -> bool:
        """Whether this retained search is still showing a later-train control."""
        session = self._open_result_session(session_id)
        return session is not None and session.browser.has_later_results()

    def has_earlier_trains(self, session_id: str) -> bool:
        """Whether this retained search is still showing an earlier-train control."""
        session = self._open_result_session(session_id)
        return session is not None and session.browser.has_earlier_results()

    def can_step_later(self, session_id: str) -> bool:
        """Whether book can move to the next page with one recognized control."""
        session = self._open_result_session(session_id)
        return session is not None and session.browser.can_step_later()

    def _open_result_session(self, session_id: str) -> _Session | None:
        session = self._sessions.get(session_id)
        if session is None or session.trains is None or self._clock() >= session.expires_at:
            return None
        return session

    def load_later_trains(self, session_id: str) -> BookingSearchResult:
        """Read later result pages in the retained search. Does not submit again."""
        session = self._sessions.get(session_id)
        if session is None or session.trains is None:
            return BookingSearchResult(
                status=BookingSearchStatus.SESSION_EXPIRED,
                message="Session is unknown or already closed; explicitly start a new search.",
            )
        if self._clock() >= session.expires_at:
            self.close_session(session_id)
            return BookingSearchResult(
                status=BookingSearchStatus.SESSION_EXPIRED,
                message="Local session deadline elapsed; later trains were not read.",
            )
        try:
            result = session.browser.load_later_trains()
        except BookingError as exc:
            return BookingSearchResult(status=BookingSearchStatus.UNKNOWN_ERROR, message=str(exc))
        except Exception:
            return BookingSearchResult(
                status=BookingSearchStatus.UNKNOWN_ERROR,
                message="Browser or parser failed; availability is unknown. No retry was attempted.",
            )
        session.trains = list(result.trains)
        return result

    def step_trains(self, session_id: str, direction: str) -> BookingSearchResult:
        """Replace the retained list with one adjacent result page. Does not submit again."""
        session = self._sessions.get(session_id)
        if session is None or session.trains is None:
            return BookingSearchResult(
                status=BookingSearchStatus.SESSION_EXPIRED,
                message="Session is unknown or already closed; explicitly start a new search.",
            )
        if self._clock() >= session.expires_at:
            self.close_session(session_id)
            return BookingSearchResult(
                status=BookingSearchStatus.SESSION_EXPIRED,
                message="Local session deadline elapsed; the result page was not changed.",
            )
        try:
            result = session.browser.step_result_page(direction)
        except BookingError as exc:
            return BookingSearchResult(status=BookingSearchStatus.UNKNOWN_ERROR, message=str(exc))
        except Exception:
            return BookingSearchResult(
                status=BookingSearchStatus.UNKNOWN_ERROR,
                message="Browser or parser failed; availability is unknown. No retry was attempted.",
            )
        session.trains = list(result.trains)
        session.selected = None
        return result

    def select_train(self, session_id: str, train_number: str) -> Train | ReservationResult:
        """Open the pickup form for one parsed train, or report that nothing was submitted."""
        session = self._retained_session(session_id)
        if isinstance(session, ReservationResult):
            return session
        if self._clock() >= session.expires_at:
            self._release(session_id)
            return ReservationResult(
                status=ReservationStatus.NOT_SUBMITTED,
                message="Local session deadline elapsed; no train was selected.",
            )
        matches = [train for train in session.trains or [] if train.number == train_number]
        if len(matches) != 1:
            return ReservationResult(
                status=ReservationStatus.NOT_SUBMITTED,
                message=(
                    "Train is not on this page. Return to the page that lists it before selecting. "
                    "Nothing was selected."
                ),
            )
        try:
            session.browser.select_train(train_number)
        except BookingError as exc:
            self._release(session_id)
            return ReservationResult(status=ReservationStatus.NOT_SUBMITTED, message=str(exc))
        except Exception:
            self._release(session_id)
            return ReservationResult(
                status=ReservationStatus.NOT_SUBMITTED,
                message="Could not open the pickup form; no reservation was submitted.",
            )
        session.selected = matches[0]
        return matches[0]

    def submit_reservation(self, session_id: str, pickup: PickupDetails) -> ReservationResult:
        """Submit once using pickup details already collected. Closes the session."""
        session = self._retained_session(session_id)
        if isinstance(session, ReservationResult):
            return session
        if session.selected is None:
            self._release(session_id)
            return ReservationResult(
                status=ReservationStatus.NOT_SUBMITTED,
                message="No train is selected; reservation was not submitted.",
            )
        if self._clock() >= session.expires_at:
            self._release(session_id)
            return ReservationResult(
                status=ReservationStatus.NOT_SUBMITTED,
                message="Local session deadline elapsed; reservation was not submitted.",
            )
        try:
            result = session.browser.submit_reservation(pickup)
        except BookingError as exc:
            result = ReservationResult(status=ReservationStatus.NOT_SUBMITTED, message=str(exc))
        except Exception:
            result = ReservationResult(
                status=ReservationStatus.UNCERTAIN,
                message="The reservation outcome is uncertain and it may already exist. "
                "Check THSR directly. This program will not submit again.",
            )
        self._release(session_id)
        return result

    def _retained_session(self, session_id: str) -> _Session | ReservationResult:
        session = self._sessions.get(session_id)
        if session is None or session.trains is None:
            return ReservationResult(
                status=ReservationStatus.NOT_SUBMITTED,
                message="Session is unknown or already closed; no reservation was submitted.",
            )
        return session

    def _release(self, session_id: str) -> None:
        try:
            self.close_session(session_id)
        except BookingError:
            return

    def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            try:
                try:
                    session.browser.close()
                finally:
                    shutil.rmtree(session.directory)
            except Exception as exc:
                raise BookingError("Failed to release a booking session resource") from exc

    def expire_sessions(self) -> None:
        for session_id, session in list(self._sessions.items()):
            if self._clock() >= session.expires_at:
                self.close_session(session_id)

    def close(self) -> None:
        errors = []
        for session_id in list(self._sessions):
            try:
                self.close_session(session_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise BookingError("Failed to release a booking session resource") from errors[0]
