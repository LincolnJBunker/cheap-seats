from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from flighttracker.sources.base import (
    PoliteClient,
    RateLimiter,
    SourceError,
    SourceName,
    sanitize_params,
)
from flighttracker.sources.serpapi import SerpApiClient
from flighttracker.sources.travelpayouts import TravelpayoutsClient


def no_sleep(_: float) -> None:
    return None


def test_sanitize_strips_credentials() -> None:
    cleaned = sanitize_params({"api_key": "s3cret", "Token": "t", "origin": "SLC"})
    assert cleaned == {"origin": "SLC"}


def test_rate_limiter_waits_remaining_interval() -> None:
    now = [100.0]
    slept: list[float] = []
    limiter = RateLimiter(5.0, clock=lambda: now[0], sleep=slept.append)
    limiter.wait()
    now[0] = 102.0
    limiter.wait()
    assert slept == [3.0]


def test_rejects_plain_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        PoliteClient(
            source=SourceName.SERPAPI,
            base_url="http://example.com",
            user_agent="t",
            timeout_seconds=1,
            min_interval_seconds=0,
        )


@respx.mock
def test_retries_on_429_then_succeeds() -> None:
    route = respx.get("https://serpapi.com/search.json").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"best_flights": []}),
        ]
    )
    client = SerpApiClient("s3cret", user_agent="test-agent", sleep=no_sleep)
    raw = client.search(
        origin="SLC", destination="NRT", outbound_date=date(2027, 3, 5), return_date=None
    )
    assert route.call_count == 2
    assert raw.http_status == 200
    # The key was sent to SerpApi but is NOT in what we store.
    assert route.calls.last.request.url.params["api_key"] == "s3cret"
    assert "api_key" not in raw.request_params
    assert "s3cret" not in str(raw.request_params)
    assert route.calls.last.request.headers["User-Agent"] == "test-agent"


@respx.mock
def test_gives_up_after_max_attempts() -> None:
    respx.get("https://serpapi.com/search.json").mock(return_value=httpx.Response(503))
    client = SerpApiClient("k", user_agent="t", sleep=no_sleep)
    with pytest.raises(SourceError, match="gave up"):
        client.search(
            origin="SLC", destination="NRT", outbound_date=date(2027, 3, 5), return_date=None
        )


@respx.mock
def test_client_errors_are_not_retried() -> None:
    route = respx.get("https://serpapi.com/search.json").mock(return_value=httpx.Response(401))
    client = SerpApiClient("k", user_agent="t", sleep=no_sleep)
    with pytest.raises(SourceError, match="401"):
        client.search(
            origin="SLC", destination="NRT", outbound_date=date(2027, 3, 5), return_date=None
        )
    assert route.call_count == 1


@respx.mock
def test_travelpayouts_token_goes_in_header_not_url() -> None:
    route = respx.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [], "currency": "usd"})
    )
    client = TravelpayoutsClient("tp-secret", user_agent="t", sleep=no_sleep)
    raw = client.prices_for_dates(
        origin="SLC", destination="NRT", departure_month="2027-03", one_way=False
    )
    request = route.calls.last.request
    assert request.headers["X-Access-Token"] == "tp-secret"
    assert "tp-secret" not in str(request.url)
    assert raw.request_params["departure_at"] == "2027-03"


@respx.mock
def test_non_json_response_raises() -> None:
    respx.get("https://serpapi.com/search.json").mock(
        return_value=httpx.Response(200, text="<html>oops</html>")
    )
    client = SerpApiClient("k", user_agent="t", sleep=no_sleep)
    with pytest.raises(SourceError, match="not JSON"):
        client.search(
            origin="SLC", destination="NRT", outbound_date=date(2027, 3, 5), return_date=None
        )
