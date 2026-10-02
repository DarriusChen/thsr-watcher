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
    BookingSessionStarted,
)


class BookingError(Exception):
    """A session could not be prepared or its resources could not be released."""


class BookingBrowser(Protocol):
    def start(self, request: BookingSearchRequest, captcha_path: Path) -> None: ...
    def submit(self, captcha_text: str) -> BookingSearchResult: ...
    def close(self) -> None: ...


@dataclass
class _Session:
    browser: BookingBrowser
    directory: Path
    expires_at: datetime


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
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Session TTL must be positive")
        if browser_factory is None:
            from thsr_watcher.booking_browser import PlaywrightBookingFactory
            browser_factory = PlaywrightBookingFactory(
                headless=headless, chrome=chrome, compatibility=compatibility,
            )
        self._factory = browser_factory
        self._ttl = ttl
        self._clock = clock
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
            path = directory / "captcha.png"
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

    def submit_captcha(self, session_id: str, captcha_text: str) -> BookingSearchResult:
        session = self._sessions.get(session_id)
        if session is None:
            return BookingSearchResult(
                status=BookingSearchStatus.SESSION_EXPIRED,
                message="Session is unknown or already closed; explicitly start a new search.",
            )
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
            return session.browser.submit(captcha_text.strip().upper())
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
            self.close_session(session_id)

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
