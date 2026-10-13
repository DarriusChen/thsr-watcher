"""Reservation-search and local-booking types, with no browser dependencies."""

import re
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from thsr_watcher.models import ClockTime, SearchRequest, Station, Train

# Official letter codes for the Taiwan national-ID checksum.
_ID_LETTER_CODES = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}
_ID_WEIGHTS = (1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1)


class BookingSearchRequest(BaseModel):
    origin: Station
    destination: Station
    travel_date: date
    after: ClockTime
    adult_passengers: int = Field(default=1, strict=True, ge=1, le=10)

    @field_validator("travel_date", mode="before")
    @classmethod
    def valid_date(cls, value: object) -> date:
        return SearchRequest.valid_date(value)

    @model_validator(mode="after")
    def different_stations(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        return self


class BookingSessionStarted(BaseModel):
    session_id: str
    captcha_path: Path
    started_at: datetime
    expires_at: datetime


class BookingSearchStatus(StrEnum):
    SUCCESS = "SUCCESS"
    CAPTCHA_REJECTED = "CAPTCHA_REJECTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class BookingSearchResult(BaseModel):
    status: BookingSearchStatus
    trains: list[Train] = Field(default_factory=list)
    message: str = ""
    # Nonzero only while the same live session awaits another human answer.
    captcha_attempts_remaining: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def no_trains_on_failure(self) -> Self:
        if self.status != BookingSearchStatus.SUCCESS and self.trains:
            raise ValueError("Failed searches cannot contain bookable trains")
        if self.captcha_attempts_remaining and self.status != BookingSearchStatus.CAPTCHA_REJECTED:
            raise ValueError("Only a rejected CAPTCHA can leave attempts remaining")
        self.trains.sort(key=lambda train: (train.departure, train.number))
        return self


def national_id_valid(value: str) -> bool:
    """Taiwan national-ID checksum. The value must already be uppercase."""
    if not re.fullmatch(r"[A-Z][0-9]{9}", value):
        return False
    code = _ID_LETTER_CODES[value[0]]
    digits = (code // 10, code % 10, *(int(char) for char in value[1:]))
    return sum(digit * weight for digit, weight in zip(digits, _ID_WEIGHTS)) % 10 == 0


class DocumentType(StrEnum):
    NATIONAL_ID = "national_id"
    PASSPORT = "passport"
    RESIDENCE_PERMIT = "residence_permit"


class PickupDetails(BaseModel):
    """Identity used to create one reservation. Membership is the same-ID checkbox."""

    document_type: DocumentType
    document_number: str
    mobile: str
    email: str | None = None
    use_same_id_membership: bool = False

    @field_validator("document_number", mode="before")
    @classmethod
    def uppercase_document(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("mobile", mode="before")
    @classmethod
    def strip_mobile(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("email", mode="before")
    @classmethod
    def blank_email(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def valid_pickup(self) -> Self:
        if self.document_type == DocumentType.NATIONAL_ID:
            if not national_id_valid(self.document_number):
                raise ValueError("invalid national ID")
        elif not re.fullmatch(r"[A-Z0-9]{6,20}", self.document_number):
            raise ValueError("invalid document number")
        if not re.fullmatch(r"09[0-9]{8}", self.mobile):
            raise ValueError("invalid mobile number")
        if self.email is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.email):
            raise ValueError("invalid email")
        if self.use_same_id_membership and self.document_type != DocumentType.NATIONAL_ID:
            raise ValueError("membership checkbox requires a national ID")
        return self


class ReservationStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    NOT_SUBMITTED = "NOT_SUBMITTED"


class ReservationResult(BaseModel):
    status: ReservationStatus
    reservation_code: str | None = None
    message: str = ""

    @field_validator("reservation_code")
    @classmethod
    def reservation_code_shape(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Z0-9]{8}", value):
            raise ValueError("invalid reservation code")
        return value

    @model_validator(mode="after")
    def code_matches_status(self) -> Self:
        if self.status == ReservationStatus.CONFIRMED and not self.reservation_code:
            raise ValueError("A confirmed reservation requires a reservation code")
        if self.reservation_code and self.status != ReservationStatus.CONFIRMED:
            raise ValueError("Only a confirmed reservation can include a reservation code")
        return self
