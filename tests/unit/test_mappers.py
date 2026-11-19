from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from flighttracker.ingest.mappers import map_serpapi, map_travelpayouts, to_cents
from flighttracker.models import PriceLevel, TripType


class TestToCents:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [(612, 61200), (689.5, 68950), ("19.99", 1999), (0.005, 1), (100.125, 10013)],
    )
    def test_converts_and_rounds_half_up(self, amount: Any, expected: int) -> None:
        assert to_cents(amount) == expected

    @pytest.mark.parametrize("amount", [0, -5, None, True, "abc", float("nan"), float("inf")])
    def test_rejects_non_positive_or_non_numeric(self, amount: Any) -> None:
        with pytest.raises(ValueError):
            to_cents(amount)


class TestTravelpayouts:
    def test_maps_valid_items(self, tp_payload: dict[str, Any]) -> None:
        result = map_travelpayouts(tp_payload)
        assert len(result.fares) == 2
        first = result.fares[0]
        assert (first.origin, first.destination) == ("SLC", "NRT")  # airport, not metro "TYO"
        assert first.trip_type is TripType.ROUND_TRIP
        assert first.price_cents == 61200
        assert first.currency == "USD"
        assert first.depart_date == date(2027, 3, 5)  # local date at origin
        assert first.return_date == date(2027, 3, 15)  # local date at destination
        assert first.stops == 1  # max(outbound 1, return 0)
        assert first.carriers == ("DL",)
        assert first.duration_min == 1210

    def test_rejects_bad_items_with_reasons(self, tp_payload: dict[str, Any]) -> None:
        result = map_travelpayouts(tp_payload)
        assert sum(result.rejected.values()) == 2
        assert any("price must be positive" in r for r in result.rejected)
        assert any("origin" in r for r in result.rejected)

    def test_unsuccessful_response_maps_to_nothing(self) -> None:
        result = map_travelpayouts({"success": False, "error": "unauthorized"})
        assert result.fares == []
        assert result.rejected["response not successful"] == 1

    def test_one_way_when_no_return(self) -> None:
        payload = {
            "success": True,
            "currency": "usd",
            "data": [
                {
                    "origin_airport": "SLC",
                    "destination_airport": "LAX",
                    "price": 59,
                    "departure_at": "2027-01-10T06:00:00-07:00",
                    "transfers": 0,
                }
            ],
        }
        fare = map_travelpayouts(payload).fares[0]
        assert fare.trip_type is TripType.ONE_WAY
        assert fare.return_date is None
        assert fare.stops == 0


class TestSerpApi:
    def map(self, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
        return map_serpapi(
            payload,
            origin="SLC",
            destination="NRT",
            depart_date=date(2027, 3, 5),
            return_date=date(2027, 3, 15),
        )

    def test_maps_best_and_other_flights(self, serp_payload: dict[str, Any]) -> None:
        result = self.map(serp_payload)
        assert [f.price_cents for f in result.fares] == [64100, 70200]
        best = result.fares[0]
        assert best.carriers == ("DL",)  # deduplicated across segments
        assert best.stops == 1
        assert best.duration_min == 855
        assert result.fares[1].carriers == ("UA", "NH")

    def test_rejects_option_without_segments(self, serp_payload: dict[str, Any]) -> None:
        assert self.map(serp_payload).rejected["no flight segments"] == 1

    def test_maps_price_insights(self, serp_payload: dict[str, Any]) -> None:
        insight = self.map(serp_payload).insight
        assert insight is not None
        assert insight.level is PriceLevel.LOW
        assert (insight.typical_low_cents, insight.typical_high_cents) == (78000, 115000)
        assert insight.lowest_price_cents == 64100

    def test_api_error_maps_to_nothing(self) -> None:
        result = self.map({"error": "Invalid API key"})
        assert result.fares == [] and result.insight is None

    def test_unknown_price_level_is_dropped_not_guessed(self, serp_payload: dict[str, Any]) -> None:
        serp_payload["price_insights"]["price_level"] = "bargain"
        insight = self.map(serp_payload).insight
        assert insight is not None and insight.level is None
