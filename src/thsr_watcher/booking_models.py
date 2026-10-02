"""Reservation-search types, with no browser or transport dependencies."""

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from thsr_watcher.models import ClockTime, SearchRequest, Station, Train


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

    @model_validator(mode="after")
    def no_trains_on_failure(self) -> Self:
        if self.status != BookingSearchStatus.SUCCESS and self.trains:
            raise ValueError("Failed searches cannot contain bookable trains")
        self.trains.sort(key=lambda train: (train.departure, train.number))
        return self
