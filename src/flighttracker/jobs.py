"""Scheduled jobs. Each one records a ``search_runs`` row so every number in the database can be
traced back to the run (and raw payload) that produced it.

Logging rule: these jobs run in GitHub Actions, whose logs are public on a public repo. Log only
counts and run ids, never routes, airports, dates or prices (those reveal travel plans). The
details are in the private database: look a run up with
``SELECT * FROM v_fares WHERE observation_id IN (SELECT id FROM fare_observations
WHERE search_run_id = <run id>)``. Enforced by tests/integration/test_pipeline.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine

from flighttracker.db import repositories as repo
from flighttracker.ingest.mappers import map_serpapi, map_travelpayouts
from flighttracker.models import Cabin
from flighttracker.sources.base import QuotaExceededError, SourceError, SourceName
from flighttracker.sources.serpapi import SerpApiClient
from flighttracker.sources.travelpayouts import TravelpayoutsClient

log = logging.getLogger(__name__)

MAX_MONTHS_PER_WATCH = 6


@dataclass
class JobResult:
    requests_made: int = 0
    observations_saved: int = 0
    rejected: int = 0
    skipped_reason: str | None = None


def months_in_window(start: date, end: date, today: date) -> Iterator[str]:
    """YYYY-MM strings for each month overlapping [max(start, today), end], capped."""
    first = max(start, today)
    if first > end:
        return
    year, month = first.year, first.month
    for _ in range(MAX_MONTHS_PER_WATCH):
        if date(year, month, 1) > end:
            return
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


# ---------------------------------------------------------------------------
# scan: Travelpayouts cached fares for every active watch
# ---------------------------------------------------------------------------
def run_scan(
    engine: Engine, client: TravelpayoutsClient, *, max_requests: int, today: date
) -> JobResult:
    result = JobResult()
    with engine.begin() as conn:
        watches = repo.list_watches(conn)
        run_id = repo.start_run(conn, SourceName.TRAVELPAYOUTS, "scan")

    # One request per (route, trip type, cabin, departure month); watches sharing these share data.
    requests: dict[tuple[str, str, str, str, str], None] = {}
    for watch in watches:
        for month in months_in_window(watch.depart_from, watch.depart_to, today):
            requests[(watch.origin, watch.destination, watch.trip_type, watch.cabin, month)] = None
    if len(requests) > max_requests:
        log.warning("scan: %d requests needed, capped at %d", len(requests), max_requests)

    error: str | None = None
    try:
        for origin, destination, trip_type, cabin, month in list(requests)[:max_requests]:
            raw = client.prices_for_dates(
                origin=origin,
                destination=destination,
                departure_month=month,
                one_way=trip_type == "one_way",
            )
            result.requests_made += 1
            mapped = map_travelpayouts(raw.payload, cabin=Cabin(cabin))
            result.rejected += sum(mapped.rejected.values())
            with engine.begin() as conn:
                raw_id = repo.save_raw(conn, run_id, raw)
                result.observations_saved += repo.save_fares(conn, run_id, raw_id, mapped.fares)
            log.info(
                "scan run #%d request %d: %d fares, %d rejected",
                run_id,
                result.requests_made,
                len(mapped.fares),
                sum(mapped.rejected.values()),
            )
    except SourceError as exc:
        error = str(exc)
        log.error("scan failed: %s", exc)
    finally:
        with engine.begin() as conn:
            repo.finish_run(
                conn,
                run_id,
                status="failed" if error else "succeeded",
                requests_made=result.requests_made,
                observations_saved=result.observations_saved,
                error=error,
            )
    if error:
        raise SourceError(error)
    return result


# ---------------------------------------------------------------------------
# verify: spend a small SerpApi budget confirming the most promising dates live
# ---------------------------------------------------------------------------
def coverage_date(watch: repo.Watch, today: date) -> tuple[date, date | None] | None:
    """Fallback date pair when there are no cached candidates: walk through the window one day
    per calendar day so repeated runs gradually cover it (deterministic, no randomness)."""
    start = max(watch.depart_from, today + timedelta(days=1))
    if start > watch.depart_to:
        return None
    span = (watch.depart_to - start).days + 1
    depart = start + timedelta(days=today.toordinal() % span)
    if watch.trip_type == "one_way":
        return depart, None
    if watch.min_nights is None or watch.max_nights is None:
        raise ValueError(f"round-trip watch #{watch.id} has no nights range")
    nights = (watch.min_nights + watch.max_nights) // 2
    return depart, depart + timedelta(days=nights)


def pick_verification_targets(
    engine: Engine, watches: list[repo.Watch], limit: int, today: date
) -> list[tuple[repo.Watch, date, date | None, str]]:
    """Choose up to ``limit`` (watch, depart, return, reason) targets. Watches that were checked
    live least recently go first, so the budget is shared fairly."""
    targets: list[tuple[repo.Watch, date, date | None, str]] = []
    with engine.connect() as conn:
        last_checked = {w.route_id: repo.last_live_check(conn, w.route_id) for w in watches}
        never = datetime.min.replace(tzinfo=UTC)  # never-checked routes sort first
        ordered = sorted(watches, key=lambda w: last_checked[w.route_id] or never)
        for watch in ordered:
            if len(targets) >= limit:
                break
            candidates = repo.cached_candidates(conn, watch, limit=1)
            if candidates:
                c = candidates[0]
                targets.append(
                    (
                        watch,
                        c.depart_date,
                        c.return_date,
                        "cheapest cached fare",
                    )
                )
                continue
            fallback = coverage_date(watch, today)
            if fallback:
                targets.append((watch, fallback[0], fallback[1], "coverage sample"))
    return targets


def run_verify(
    engine: Engine, client: SerpApiClient, *, max_searches: int, monthly_limit: int, today: date
) -> JobResult:
    result = JobResult()
    with engine.begin() as conn:
        watches = repo.list_watches(conn)
    if not watches or max_searches == 0:
        result.skipped_reason = "no active watches" if not watches else "max_searches is 0"
        return result

    targets = pick_verification_targets(engine, watches, max_searches, today)
    with engine.begin() as conn:
        run_id = repo.start_run(conn, SourceName.SERPAPI, "verify")

    error: str | None = None
    try:
        for watch, depart, ret, reason in targets:
            with engine.begin() as conn:
                if not repo.consume_quota(conn, SourceName.SERPAPI, 1, monthly_limit):
                    raise QuotaExceededError("SerpApi monthly budget is spent")
            raw = client.search(
                origin=watch.origin,
                destination=watch.destination,
                outbound_date=depart,
                return_date=ret,
                cabin=watch.cabin,
            )
            result.requests_made += 1
            mapped = map_serpapi(
                raw.payload,
                origin=watch.origin,
                destination=watch.destination,
                depart_date=depart,
                return_date=ret,
                cabin=Cabin(watch.cabin),
            )
            result.rejected += sum(mapped.rejected.values())
            with engine.begin() as conn:
                raw_id = repo.save_raw(conn, run_id, raw)
                result.observations_saved += repo.save_fares(conn, run_id, raw_id, mapped.fares)
                if mapped.insight:
                    repo.save_insight(conn, run_id, raw_id, mapped.insight)
            log.info(
                "verify run #%d search %d (%s): %d live fares",
                run_id,
                result.requests_made,
                reason,
                len(mapped.fares),
            )
    except QuotaExceededError as exc:
        result.skipped_reason = str(exc)
        log.warning("verify: %s", exc)
    except SourceError as exc:
        error = str(exc)
        log.error("verify failed: %s", exc)
    finally:
        status = "failed" if error else ("skipped" if result.requests_made == 0 else "succeeded")
        with engine.begin() as conn:
            repo.finish_run(
                conn,
                run_id,
                status=status,
                requests_made=result.requests_made,
                observations_saved=result.observations_saved,
                error=error or result.skipped_reason,
            )
    if error:
        raise SourceError(error)
    return result
