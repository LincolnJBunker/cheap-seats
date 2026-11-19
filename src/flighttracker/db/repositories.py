"""All SQL the application runs. Every value is a bound parameter; no SQL is built from strings.

Functions take an open ``Connection`` so callers control transaction boundaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from flighttracker.models import NormalizedFare, PriceInsight, WatchInput
from flighttracker.sources.base import RawResponse, SourceName


# ---------------------------------------------------------------------------
# Runs and raw payloads
# ---------------------------------------------------------------------------
def start_run(conn: Connection, source: SourceName, job: str) -> int:
    row = conn.execute(
        text("INSERT INTO search_runs (source, job) VALUES (:source, :job) RETURNING id"),
        {"source": source.value, "job": job},
    ).one()
    return int(row.id)


def finish_run(
    conn: Connection,
    run_id: int,
    *,
    status: str,
    requests_made: int,
    observations_saved: int,
    error: str | None = None,
) -> None:
    conn.execute(
        text(
            """
            UPDATE search_runs
               SET status = CAST(:status AS run_status), finished_at = now(),
                   requests_made = :requests, observations_saved = :saved, error = :error
             WHERE id = :id
            """
        ),
        {
            "status": status,
            "requests": requests_made,
            "saved": observations_saved,
            "error": error[:2000] if error else None,
            "id": run_id,
        },
    )


def save_raw(conn: Connection, run_id: int, raw: RawResponse) -> int:
    row = conn.execute(
        text(
            """
            INSERT INTO raw_responses
                (search_run_id, endpoint, request_params, http_status, payload, payload_sha256)
            VALUES (:run, :endpoint, CAST(:params AS jsonb), :status, CAST(:payload AS jsonb), :sha)
            RETURNING id
            """
        ),
        {
            "run": run_id,
            "endpoint": raw.endpoint,
            "params": json.dumps(raw.request_params, default=str),
            "status": raw.http_status,
            "payload": json.dumps(raw.payload),
            "sha": raw.sha256,
        },
    ).one()
    return int(row.id)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def get_or_create_route(conn: Connection, origin: str, destination: str) -> int:
    row = conn.execute(
        text("SELECT fn_get_or_create_route(:o, :d) AS id"), {"o": origin, "d": destination}
    ).one()
    return int(row.id)


def save_fares(
    conn: Connection, run_id: int, raw_id: int | None, fares: list[NormalizedFare]
) -> int:
    route_ids: dict[tuple[str, str], int] = {}
    for fare in fares:
        key = (fare.origin, fare.destination)
        if key not in route_ids:
            route_ids[key] = get_or_create_route(conn, *key)
        obs = conn.execute(
            text(
                """
                INSERT INTO fare_observations
                    (route_id, search_run_id, raw_response_id, trip_type, cabin, depart_date,
                     return_date, price_cents, currency, stops, duration_min)
                VALUES (:route, :run, :raw, CAST(:trip AS trip_type), CAST(:cabin AS cabin_class),
                        :depart, :ret, :price, :currency, :stops, :duration)
                RETURNING id
                """
            ),
            {
                "route": route_ids[key],
                "run": run_id,
                "raw": raw_id,
                "trip": fare.trip_type.value,
                "cabin": fare.cabin.value,
                "depart": fare.depart_date,
                "ret": fare.return_date,
                "price": fare.price_cents,
                "currency": fare.currency,
                "stops": fare.stops,
                "duration": fare.duration_min,
            },
        ).one()
        for code in fare.carriers:
            # Placeholder airline row (name = code) if we haven't seen this carrier before.
            conn.execute(
                text("INSERT INTO airlines (iata, name) VALUES (:c, :name) ON CONFLICT DO NOTHING"),
                {"c": code, "name": code},
            )
            conn.execute(
                text(
                    "INSERT INTO fare_carriers (observation_id, airline_iata) VALUES (:o, :c) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"o": obs.id, "c": code},
            )
    return len(fares)


def save_insight(conn: Connection, run_id: int, raw_id: int | None, insight: PriceInsight) -> None:
    route_id = get_or_create_route(conn, insight.origin, insight.destination)
    conn.execute(
        text(
            """
            INSERT INTO route_price_insights
                (route_id, search_run_id, raw_response_id, trip_type, cabin, depart_date,
                 return_date, lowest_price_cents, level, typical_low_cents, typical_high_cents)
            VALUES (:route, :run, :raw, CAST(:trip AS trip_type), CAST(:cabin AS cabin_class),
                    :depart, :ret, :lowest, CAST(:level AS price_level), :low, :high)
            """
        ),
        {
            "route": route_id,
            "run": run_id,
            "raw": raw_id,
            "trip": insight.trip_type.value,
            "cabin": insight.cabin.value,
            "depart": insight.depart_date,
            "ret": insight.return_date,
            "lowest": insight.lowest_price_cents,
            "level": insight.level.value if insight.level else None,
            "low": insight.typical_low_cents,
            "high": insight.typical_high_cents,
        },
    )


def consume_quota(conn: Connection, source: SourceName, n: int, monthly_limit: int) -> bool:
    row = conn.execute(
        text("SELECT fn_consume_quota(CAST(:s AS fare_source), :n, :limit) AS ok"),
        {"s": source.value, "n": n, "limit": monthly_limit},
    ).one()
    return bool(row.ok)


def quota_status(conn: Connection, source: SourceName) -> tuple[int, int] | None:
    row = conn.execute(
        text(
            """
            SELECT used, monthly_limit FROM api_quota
             WHERE source = CAST(:s AS fare_source)
               AND period_start = date_trunc('month', now() AT TIME ZONE 'UTC')::date
            """
        ),
        {"s": source.value},
    ).one_or_none()
    return (int(row.used), int(row.monthly_limit)) if row else None


def refresh_route_stats(conn: Connection) -> None:
    conn.execute(text("SELECT sp_refresh_route_stats()"))


def purge_raw_responses(conn: Connection, days: int) -> int:
    row = conn.execute(text("SELECT fn_purge_raw_responses(:d) AS n"), {"d": days}).one()
    return int(row.n)


# ---------------------------------------------------------------------------
# Users and watches
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Watch:
    id: int
    user_id: UUID
    route_id: int
    origin: str
    destination: str
    trip_type: str
    cabin: str
    depart_from: date
    depart_to: date
    min_nights: int | None
    max_nights: int | None
    max_price_cents: int | None


WATCH_COLUMNS = """
    w.id, w.user_id, w.route_id, r.origin, r.destination, w.trip_type::text AS trip_type,
    w.cabin::text AS cabin, w.depart_from, w.depart_to, w.min_nights, w.max_nights,
    w.max_price_cents
"""


def _watch(row: Any) -> Watch:
    return Watch(**row._mapping)


def add_user(conn: Connection, email: str, tz: str = "America/Denver") -> UUID:
    row = conn.execute(
        text(
            """
            INSERT INTO users (email, tz) VALUES (:email, :tz)
            ON CONFLICT (email) DO UPDATE SET tz = EXCLUDED.tz
            RETURNING id
            """
        ),
        {"email": email, "tz": tz},
    ).one()
    return UUID(str(row.id))


def get_user_id(conn: Connection, email: str) -> UUID | None:
    row = conn.execute(
        text("SELECT id FROM users WHERE email = :email"), {"email": email}
    ).one_or_none()
    return UUID(str(row.id)) if row else None


def list_users(conn: Connection) -> list[tuple[UUID, str, str]]:
    rows = conn.execute(text("SELECT id, email::text AS email, tz FROM users ORDER BY created_at"))
    return [(UUID(str(r.id)), r.email, r.tz) for r in rows]


def add_watch(conn: Connection, user_id: UUID, watch: WatchInput) -> int:
    route_id = get_or_create_route(conn, watch.origin, watch.destination)
    row = conn.execute(
        text(
            """
            INSERT INTO watches (user_id, route_id, trip_type, cabin, depart_from, depart_to,
                                 min_nights, max_nights, max_price_cents)
            VALUES (:user, :route, CAST(:trip AS trip_type), CAST(:cabin AS cabin_class),
                    :dfrom, :dto, :minn, :maxn, :maxp)
            RETURNING id
            """
        ),
        {
            "user": user_id,
            "route": route_id,
            "trip": watch.trip_type.value,
            "cabin": watch.cabin.value,
            "dfrom": watch.depart_from,
            "dto": watch.depart_to,
            "minn": watch.min_nights,
            "maxn": watch.max_nights,
            "maxp": watch.max_price_cents,
        },
    ).one()
    return int(row.id)


def list_watches(conn: Connection, user_id: UUID | None = None) -> list[Watch]:
    rows = conn.execute(
        text(
            f"""
            SELECT {WATCH_COLUMNS}
              FROM watches w JOIN routes r ON r.id = w.route_id
             WHERE w.active AND (CAST(:user AS uuid) IS NULL OR w.user_id = CAST(:user AS uuid))
             ORDER BY w.id
            """  # noqa: S608 - WATCH_COLUMNS is a module constant; values are bound
        ),
        {"user": user_id},
    )
    return [_watch(r) for r in rows]


def deactivate_watch(conn: Connection, user_id: UUID, watch_id: int) -> bool:
    result = conn.execute(
        text("UPDATE watches SET active = false WHERE id = :id AND user_id = :user AND active"),
        {"id": watch_id, "user": user_id},
    )
    return result.rowcount == 1


# ---------------------------------------------------------------------------
# Verify-job helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    depart_date: date
    return_date: date | None
    cached_price_cents: int


def cached_candidates(conn: Connection, watch: Watch, limit: int = 5) -> list[Candidate]:
    """Cheapest cached (Travelpayouts) date pairs from the last 7 days that fit the watch and
    have not been checked live in the last 24 hours."""
    rows = conn.execute(
        text(
            """
            SELECT fo.depart_date, fo.return_date, min(fo.price_cents) AS price_cents
              FROM fare_observations fo
              JOIN search_runs sr ON sr.id = fo.search_run_id
             WHERE fo.route_id = :route
               AND sr.source = 'travelpayouts'
               AND fo.observed_at >= now() - interval '7 days'
               AND fo.trip_type = CAST(:trip AS trip_type)
               AND fo.cabin = CAST(:cabin AS cabin_class)
               AND fo.depart_date BETWEEN GREATEST(:dfrom, current_date + 1) AND :dto
               AND (CAST(:minn AS int) IS NULL
                    OR (fo.return_date - fo.depart_date) BETWEEN :minn AND :maxn)
               AND NOT EXISTS (
                   SELECT 1
                     FROM fare_observations live
                     JOIN search_runs lsr ON lsr.id = live.search_run_id
                    WHERE lsr.source = 'serpapi'
                      AND live.route_id = fo.route_id
                      AND live.depart_date = fo.depart_date
                      AND live.return_date IS NOT DISTINCT FROM fo.return_date
                      AND live.observed_at >= now() - interval '24 hours')
             GROUP BY fo.depart_date, fo.return_date
             ORDER BY price_cents
             LIMIT :limit
            """
        ),
        {
            "route": watch.route_id,
            "trip": watch.trip_type,
            "cabin": watch.cabin,
            "dfrom": watch.depart_from,
            "dto": watch.depart_to,
            "minn": watch.min_nights,
            "maxn": watch.max_nights,
            "limit": limit,
        },
    )
    return [Candidate(r.depart_date, r.return_date, int(r.price_cents)) for r in rows]


def last_live_check(conn: Connection, route_id: int) -> datetime | None:
    row = conn.execute(
        text(
            """
            SELECT max(fo.observed_at) AS last
              FROM fare_observations fo JOIN search_runs sr ON sr.id = fo.search_run_id
             WHERE fo.route_id = :route AND sr.source = 'serpapi'
            """
        ),
        {"route": route_id},
    ).one()
    return row.last  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FareSummary:
    price_cents: int
    currency: str
    depart_date: date
    return_date: date | None
    stops: int | None
    carriers: str | None
    observed_at: datetime


@dataclass(frozen=True)
class WatchDigest:
    watch: Watch
    cheapest_live: FareSummary | None
    cheapest_cached: FareSummary | None
    median_cents: int | None
    sample_n: int
    google_level: str | None
    google_typical_low_cents: int | None
    google_typical_high_cents: int | None


def _cheapest(conn: Connection, watch: Watch, source: str, hours: int) -> FareSummary | None:
    row = conn.execute(
        text(
            """
            SELECT v.price, v.currency, v.depart_date, v.return_date, v.stops, v.carriers,
                   v.observed_at
              FROM v_fares v
             WHERE v.origin = :o AND v.destination = :d
               AND v.source = CAST(:src AS fare_source)
               AND v.trip_type = CAST(:trip AS trip_type)
               AND v.cabin = CAST(:cabin AS cabin_class)
               AND v.observed_at >= now() - make_interval(hours => :hours)
               AND v.depart_date BETWEEN GREATEST(:dfrom, current_date + 1) AND :dto
               AND (CAST(:minn AS int) IS NULL OR v.nights BETWEEN :minn AND :maxn)
             ORDER BY v.price, v.observed_at DESC
             LIMIT 1
            """
        ),
        {
            "o": watch.origin,
            "d": watch.destination,
            "src": source,
            "trip": watch.trip_type,
            "cabin": watch.cabin,
            "hours": hours,
            "dfrom": watch.depart_from,
            "dto": watch.depart_to,
            "minn": watch.min_nights,
            "maxn": watch.max_nights,
        },
    ).one_or_none()
    if row is None:
        return None
    return FareSummary(
        price_cents=int(row.price * 100),
        currency=row.currency,
        depart_date=row.depart_date,
        return_date=row.return_date,
        stops=row.stops,
        carriers=row.carriers,
        observed_at=row.observed_at,
    )


def watch_digest(conn: Connection, watch: Watch) -> WatchDigest:
    stats = conn.execute(
        text(
            """
            SELECT sum(n)::int AS n,
                   -- n-weighted average of monthly medians across the watch window
                   round(sum(median_cents::numeric * n) / NULLIF(sum(n), 0))::int AS median
              FROM route_price_stats
             WHERE route_id = :route
               AND trip_type = CAST(:trip AS trip_type)
               AND cabin = CAST(:cabin AS cabin_class)
               AND depart_month BETWEEN date_trunc('month', CAST(:dfrom AS date))::date AND :dto
            """
        ),
        {
            "route": watch.route_id,
            "trip": watch.trip_type,
            "cabin": watch.cabin,
            "dfrom": watch.depart_from,
            "dto": watch.depart_to,
        },
    ).one()
    insight = conn.execute(
        text(
            """
            SELECT level::text AS level, typical_low_cents, typical_high_cents
              FROM route_price_insights
             WHERE route_id = :route
               AND trip_type = CAST(:trip AS trip_type)
               AND depart_date BETWEEN :dfrom AND :dto
             ORDER BY observed_at DESC
             LIMIT 1
            """
        ),
        {
            "route": watch.route_id,
            "trip": watch.trip_type,
            "dfrom": watch.depart_from,
            "dto": watch.depart_to,
        },
    ).one_or_none()
    return WatchDigest(
        watch=watch,
        cheapest_live=_cheapest(conn, watch, "serpapi", 48),
        cheapest_cached=_cheapest(conn, watch, "travelpayouts", 48),
        median_cents=stats.median,
        sample_n=stats.n or 0,
        google_level=insight.level if insight else None,
        google_typical_low_cents=insight.typical_low_cents if insight else None,
        google_typical_high_cents=insight.typical_high_cents if insight else None,
    )
