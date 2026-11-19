from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture
def tp_payload() -> dict[str, Any]:
    return load_fixture("travelpayouts_prices_for_dates.json")


@pytest.fixture
def serp_payload() -> dict[str, Any]:
    return load_fixture("serpapi_google_flights_round_trip.json")
