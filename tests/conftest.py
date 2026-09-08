from datetime import date

import pytest

from thsr_watcher import models


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(models, "taipei_today", lambda: date(2026, 9, 8))


@pytest.fixture
def request_data():
    return dict(origin="台北", destination="台中", travel_date="2026-09-15", after="17:00", before="20:00")
