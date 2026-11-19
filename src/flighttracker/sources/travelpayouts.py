"""Travelpayouts (Aviasales) Data API client.

Returns *cached* fares that Aviasales users found in roughly the last week. Cheap and broad, so we
use it for wide scans and to pick candidate dates; it is never treated as proof of a live price.
Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
"""

from __future__ import annotations

from typing import Any

import httpx

from flighttracker.sources.base import PoliteClient, RawResponse, SourceName

BASE_URL = "https://api.travelpayouts.com"
PRICES_FOR_DATES = "/aviasales/v3/prices_for_dates"


class TravelpayoutsClient:
    def __init__(
        self,
        token: str,
        *,
        user_agent: str,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        # Token goes in a header (not the URL), so it can't leak through logged URLs.
        self._http = PoliteClient(
            source=SourceName.TRAVELPAYOUTS,
            base_url=BASE_URL,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=1.0,
            default_headers={"X-Access-Token": token},
            transport=transport,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def prices_for_dates(
        self,
        *,
        origin: str,
        destination: str | None,
        departure_month: str,
        one_way: bool,
        limit: int = 100,
    ) -> RawResponse:
        """Cheapest cached fares for a departure month (YYYY-MM).

        ``destination=None`` means "anywhere" from ``origin``.
        """
        params: dict[str, Any] = {
            "origin": origin,
            "departure_at": departure_month,
            "one_way": "true" if one_way else "false",
            "currency": "usd",
            "market": "us",
            "sorting": "price",
            "unique": "false",
            "limit": limit,
            "page": 1,
        }
        if destination:
            params["destination"] = destination
        return self._http.get_json(PRICES_FOR_DATES, params)
