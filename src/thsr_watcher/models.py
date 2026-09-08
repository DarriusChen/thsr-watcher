"""Timetable inputs and results, independent of the website and CLI."""

import re
from datetime import date, datetime, time
from typing import Annotated, Literal, Self, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

Station = Literal[
    "南港", "台北", "板橋", "桃園", "新竹", "苗栗",
    "台中", "彰化", "雲林", "嘉義", "台南", "左營",
]
STATIONS: tuple[str, ...] = get_args(Station)


def taipei_today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def parse_time(value: object) -> time:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        try:
            value = time.fromisoformat(value)
        except ValueError:
            pass
    if not isinstance(value, time) or value.tzinfo is not None or value.second or value.microsecond:
        raise ValueError("time must be HH:MM (00:00–23:59), without seconds or timezone")
    return value


ClockTime = Annotated[time, BeforeValidator(parse_time)]


class SearchRequest(BaseModel):
    origin: Station
    destination: Station
    travel_date: date
    after: ClockTime
    before: ClockTime

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def supported_station(cls, value: object) -> object:
        if value not in STATIONS:
            raise ValueError("unsupported station; choose from: " + "、".join(STATIONS))
        return value

    @field_validator("travel_date", mode="before")
    @classmethod
    def valid_date(cls, value: object) -> date:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                pass
        if type(value) is not date:
            raise ValueError("date must be a valid calendar date in YYYY-MM-DD format")
        if value < taipei_today():
            raise ValueError("travel date cannot be in the past (Asia/Taipei)")
        return value

    @model_validator(mode="after")
    def valid_route_and_window(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        if self.after > self.before:
            raise ValueError("--after must be at or before --before on the same day")
        return self


class Train(BaseModel):
    number: str = Field(pattern=r"^[0-9]+$")
    departure: ClockTime
    arrival: ClockTime


def matching_trains(request: SearchRequest, trains: list[Train]) -> list[Train]:
    """Both departure-time endpoints are inclusive; arrival may be the next day."""
    return sorted(
        (train for train in trains if request.after <= train.departure <= request.before),
        key=lambda train: (train.departure, train.number),
    )
