from datetime import date, datetime, time, timezone

import pytest
from pydantic import ValidationError

from thsr_watcher.models import STATIONS, SearchRequest, Train, matching_trains


def test_typed_request(request_data):
    request = SearchRequest(**request_data)
    assert request.travel_date == date(2026, 9, 15)
    assert request.after == time(17)
    assert request.before == time(20)


@pytest.mark.parametrize("station", STATIONS)
@pytest.mark.parametrize("field", ["origin", "destination"])
def test_supported_stations(request_data, station, field):
    request_data.update(origin="南港" if station != "南港" else "左營", destination="左營" if station != "左營" else "南港")
    request_data[field] = station
    assert getattr(SearchRequest(**request_data), field) == station


@pytest.mark.parametrize(("field", "value", "message"), [
    ("origin", "高雄", "unsupported station"),
    ("destination", "臺北", "unsupported station"),
    ("origin", "", "unsupported station"),
    ("destination", "台北", "must be different"),
    ("travel_date", "2026-02-30", "valid calendar date"),
    ("travel_date", "2026/09/15", "YYYY-MM-DD"),
    ("travel_date", "2026-9-15", "YYYY-MM-DD"),
    ("travel_date", "2026-09-15T00:00:00", "YYYY-MM-DD"),
    ("travel_date", 0, "YYYY-MM-DD"),
    ("travel_date", datetime(2026, 9, 15), "YYYY-MM-DD"),
    ("travel_date", "2026-09-07", "past"),
    ("after", "24:00", "HH:MM"),
    ("before", "20:60", "HH:MM"),
    ("after", "7:00", "HH:MM"),
    ("after", "17:00:00", "HH:MM"),
    ("after", "17:00+08:00", "HH:MM"),
    ("after", 1700, "HH:MM"),
    ("after", time(17, tzinfo=timezone.utc), "HH:MM"),
    ("after", "21:00", "at or before"),
])
def test_invalid_request(request_data, field, value, message):
    request_data[field] = value
    with pytest.raises(ValidationError, match=message):
        SearchRequest(**request_data)


def test_today_and_equal_endpoints_are_valid(request_data):
    request_data.update(travel_date="2026-09-08", after="17:00", before="17:00")
    SearchRequest(**request_data)


def test_inclusive_filter_and_order(request_data):
    trains = [
        Train(number=str(i), departure=departure, arrival="23:00")
        for i, departure in enumerate(["20:01", "20:00", "17:01", "16:59", "17:00"])
    ]
    result = matching_trains(SearchRequest(**request_data), trains)
    assert [t.departure for t in result] == [time(17), time(17, 1), time(20)]
    assert trains[0].departure == time(20, 1)  # No mutation of input.
