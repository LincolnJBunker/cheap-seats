"""End-to-end: scan (Travelpayouts) -> verify (SerpApi) -> stats -> digest, with HTTP mocked."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from flighttracker import jobs
from flighttracker.db import repositories as repo
from flighttracker.models import TripType, WatchInput
from flighttracker.notify.email import render_digest
from flighttracker.sources.serpapi import SerpApiClient
from flighttracker.sources.travelpayouts import TravelpayoutsClient

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 18)
TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERP_URL = "https://serpapi.com/search.json"


def no_sleep(_: float) -> None:
    return None


@respx.mock
def test_full_pipeline(
    engine: Engine, tp_payload: dict[str, Any], serp_payload: dict[str, Any]
) -> None:
    with engine.begin() as conn:
        user_id = repo.add_user(conn, "me@example.com")
        repo.add_watch(
            conn,
            user_id,
            WatchInput(
                origin="SLC",
                destination="NRT",
                trip_type=TripType.ROUND_TRIP,
                depart_from=date(2027, 3, 1),
                depart_to=date(2027, 4, 15),
                min_nights=7,
                max_nights=14,
            ),
        )

    tp_route = respx.get(TP_URL).mock(return_value=httpx.Response(200, json=tp_payload))
    serp_route = respx.get(SERP_URL).mock(return_value=httpx.Response(200, json=serp_payload))

    # --- scan: one request per departure month (March, April) ---
    tp = TravelpayoutsClient("tp-secret", user_agent="test", sleep=no_sleep)
    scan = jobs.run_scan(engine, tp, max_requests=10, today=TODAY)
    assert tp_route.call_count == 2
    assert scan.observations_saved == 4  # 2 valid items x 2 months
    assert scan.rejected == 4

    # --- verify: picks the cheapest cached candidate that fits (SLC-NRT 03-05..03-15) ---
    serp = SerpApiClient("serp-secret", user_agent="test", sleep=no_sleep)
    verify = jobs.run_verify(engine, serp, max_searches=1, monthly_limit=240, today=TODAY)
    assert verify.requests_made == 1
    params = serp_route.calls.last.request.url.params
    assert (params["outbound_date"], params["return_date"]) == ("2027-03-05", "2027-03-15")

    with engine.begin() as conn:
        # No credential ever reaches the database.
        stored = conn.execute(text("SELECT request_params::text FROM raw_responses")).scalars()
        assert all("secret" not in p for p in stored)
        # Every observation traces back to a run and a raw payload.
        orphans = conn.execute(
            text("SELECT count(*) FROM fare_observations WHERE raw_response_id IS NULL")
        ).scalar()
        assert orphans == 0
        runs = conn.execute(text("SELECT job, status::text FROM search_runs ORDER BY id")).all()
        assert [tuple(r) for r in runs] == [("scan", "succeeded"), ("verify", "succeeded")]
        assert repo.quota_status(conn, repo.SourceName.SERPAPI) == (1, 240)

        repo.refresh_route_stats(conn)
        digest = repo.watch_digest(conn, repo.list_watches(conn)[0])

    assert digest.cheapest_live is not None and digest.cheapest_live.price_cents == 64100
    assert digest.cheapest_cached is not None and digest.cheapest_cached.price_cents == 61200
    assert digest.google_level == "low"
    assert digest.sample_n > 0

    email = render_digest("me@example.com", [digest], TODAY)
    assert "$641" in email.text and "$612" in email.text

    # --- a second verify run skips dates already checked live in the last 24h ---
    with engine.connect() as conn:
        watch = repo.list_watches(conn)[0]
        remaining = repo.cached_candidates(conn, watch)
    assert all(
        (c.depart_date, c.return_date) != (date(2027, 3, 5), date(2027, 3, 15)) for c in remaining
    )


@respx.mock
def test_verify_stops_when_budget_is_spent(engine: Engine, serp_payload: dict[str, Any]) -> None:
    with engine.begin() as conn:
        repo.add_watch(
            conn,
            repo.add_user(conn, "me@example.com"),
            WatchInput(
                origin="SLC",
                destination="LAX",
                trip_type=TripType.ONE_WAY,
                depart_from=date(2027, 1, 1),
                depart_to=date(2027, 1, 31),
            ),
        )
        assert repo.consume_quota(conn, repo.SourceName.SERPAPI, 3, 3)

    route = respx.get(SERP_URL).mock(return_value=httpx.Response(200, json=serp_payload))
    serp = SerpApiClient("k", user_agent="test", sleep=no_sleep)
    result = jobs.run_verify(engine, serp, max_searches=1, monthly_limit=3, today=TODAY)
    assert route.call_count == 0
    assert result.skipped_reason and "budget" in result.skipped_reason
