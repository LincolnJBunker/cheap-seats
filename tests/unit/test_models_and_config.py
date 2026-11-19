from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from flighttracker.config import check_database_url
from flighttracker.db.repositories import Watch
from flighttracker.jobs import coverage_date, months_in_window
from flighttracker.models import TripType, WatchInput

BASE: dict[str, Any] = {
    "origin": "SLC",
    "destination": "NRT",
    "trip_type": TripType.ROUND_TRIP,
    "depart_from": date(2027, 3, 1),
    "depart_to": date(2027, 5, 31),
    "min_nights": 7,
    "max_nights": 14,
}


class TestWatchInput:
    def test_valid_round_trip(self) -> None:
        assert WatchInput(**BASE).max_nights == 14

    @pytest.mark.parametrize(
        "override",
        [
            {"origin": "slc"},
            {"origin": "SLC'; DROP TABLE watches;--"},
            {"destination": "SLC"},
            {"depart_to": date(2027, 2, 1)},
            {"depart_to": date(2028, 6, 1)},
            {"min_nights": None},
            {"min_nights": 10, "max_nights": 5},
            {"max_price_cents": -100},
            {"unexpected_field": 1},
        ],
    )
    def test_rejects_invalid(self, override: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            WatchInput(**{**BASE, **override})

    def test_one_way_cannot_have_nights(self) -> None:
        with pytest.raises(ValidationError):
            WatchInput(**{**BASE, "trip_type": TripType.ONE_WAY})


class TestDatabaseUrl:
    def test_local_url_without_tls_is_fine(self) -> None:
        check_database_url("postgresql+psycopg:///flighttracker")
        check_database_url("postgresql+psycopg://localhost/flighttracker")

    def test_remote_url_must_force_tls(self) -> None:
        with pytest.raises(ValueError, match="sslmode"):
            check_database_url("postgresql+psycopg://u:p@ep-x.us-east-2.aws.neon.tech/neondb")
        check_database_url("postgresql+psycopg://u:p@ep-x.aws.neon.tech/neondb?sslmode=verify-full")

    def test_rejects_non_postgres(self) -> None:
        with pytest.raises(ValueError):
            check_database_url("mysql://localhost/x")


class TestScheduling:
    def test_months_in_window_starts_at_today_and_caps(self) -> None:
        months = list(months_in_window(date(2027, 1, 1), date(2028, 1, 1), date(2027, 2, 20)))
        assert months == ["2027-02", "2027-03", "2027-04", "2027-05", "2027-06", "2027-07"]

    def test_months_in_window_past_window_is_empty(self) -> None:
        assert list(months_in_window(date(2026, 1, 1), date(2026, 2, 1), date(2026, 11, 18))) == []

    def test_coverage_date_stays_inside_window(self) -> None:
        watch = Watch(
            id=1,
            user_id=uuid4(),
            route_id=1,
            origin="SLC",
            destination="NRT",
            trip_type="round_trip",
            cabin="economy",
            depart_from=date(2027, 3, 1),
            depart_to=date(2027, 3, 10),
            min_nights=7,
            max_nights=13,
            max_price_cents=None,
        )
        for offset in range(30):
            today = date(2026, 11, 1).fromordinal(date(2026, 11, 1).toordinal() + offset)
            target = coverage_date(watch, today)
            assert target is not None
            depart, ret = target
            assert date(2027, 3, 1) <= depart <= date(2027, 3, 10)
            assert ret is not None and (ret - depart).days == 10
