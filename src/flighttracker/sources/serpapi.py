"""SerpApi Google Flights client.

Live Google Flights results via an official API (no scraping on our side), including Google's
``price_insights`` (typical price range, low/typical/high) used as a cold-start baseline.
Docs: https://serpapi.com/google-flights-api

SerpApi only accepts the key as a query parameter. It is stripped from anything we store, and the
httpx loggers are silenced (see logging_setup.py) so request URLs are never logged.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from flighttracker.sources.base import PoliteClient, RawResponse, SourceName

BASE_URL = "https://serpapi.com"
SEARCH = "/search.json"

TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


class SerpApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self._api_key = api_key
        # Free plan allows 50 searches/hour; one every 5s keeps bursts far below that.
        self._http = PoliteClient(
            source=SourceName.SERPAPI,
            base_url=BASE_URL,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=5.0,
            transport=transport,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def search(
        self,
        *,
        origin: str,
        destination: str,
        outbound_date: date,
        return_date: date | None,
        cabin: str = "economy",
        adults: int = 1,
    ) -> RawResponse:
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": outbound_date.isoformat(),
            "type": 1 if return_date else 2,  # 1 = round trip, 2 = one way
            "travel_class": TRAVEL_CLASS[cabin],
            "adults": adults,
            "currency": "USD",
            "hl": "en",
            "gl": "us",
            "api_key": self._api_key,
        }
        if return_date:
            params["return_date"] = return_date.isoformat()
        return self._http.get_json(SEARCH, params)
