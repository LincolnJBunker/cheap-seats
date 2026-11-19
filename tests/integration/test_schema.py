from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from flighttracker.db import repositories as repo
from flighttracker.models import TripType, WatchInput
from flighttracker.sources.base import SourceName

from .conftest import alembic_config

pytestmark = pytest.mark.integration

WATCH = WatchInput(
    origin="SLC",
    destination="NRT",
    trip_type=TripType.ROUND_TRIP,
    depart_from=date(2027, 3, 1),
    depart_to=date(2027, 5, 31),
    min_nights=7,
    max_nights=14,
)


def as_role(conn: Connection, role: str, user_id: UUID | None = None) -> None:
    """Switch the current transaction to a group role, optionally acting as an app user."""
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    if user_id:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})


def test_migration_round_trip(engine: Engine) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('public.watches')")).scalar() is None
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('public.watches')")).scalar() is not None


def test_route_upsert_is_idempotent_and_creates_placeholder_airports(engine: Engine) -> None:
    with engine.begin() as conn:
        first = repo.get_or_create_route(conn, "SLC", "NRT")
        second = repo.get_or_create_route(conn, "SLC", "NRT")
        name = conn.execute(text("SELECT name FROM airports WHERE iata = 'NRT'")).scalar()
    assert first == second
    assert name == "NRT"


def test_route_function_rejects_bad_codes(engine: Engine) -> None:
    with pytest.raises(DBAPIError, match="invalid IATA"), engine.begin() as conn:
        repo.get_or_create_route(conn, "slc", "NRT")


def test_database_enforces_watch_invariants_even_without_python_validation(
    engine: Engine,
) -> None:
    with engine.begin() as conn:
        user_id = repo.add_user(conn, "a@example.com")
        route_id = repo.get_or_create_route(conn, "SLC", "NRT")
    with (
        pytest.raises(IntegrityError, match="watches_nights_match_trip_type"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO watches (user_id, route_id, trip_type, depart_from, depart_to) "
                "VALUES (:u, :r, 'round_trip', '2027-03-01', '2027-04-01')"
            ),
            {"u": user_id, "r": route_id},
        )


def test_database_rejects_round_trip_fare_without_return_date(engine: Engine) -> None:
    with engine.begin() as conn:
        route_id = repo.get_or_create_route(conn, "SLC", "NRT")
        run_id = repo.start_run(conn, SourceName.SERPAPI, "test")
    with (
        pytest.raises(IntegrityError, match="fare_obs_return_matches_trip_type"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO fare_observations (route_id, search_run_id, trip_type, cabin, "
                "depart_date, price_cents, currency) "
                "VALUES (:r, :run, 'round_trip', 'economy', '2027-03-05', 50000, 'USD')"
            ),
            {"r": route_id, "run": run_id},
        )


def test_quota_never_overspends_under_concurrency(engine: Engine) -> None:
    def spend(_: int) -> bool:
        with engine.begin() as conn:
            return repo.consume_quota(conn, SourceName.SERPAPI, 1, 5)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(spend, range(12)))
    assert results.count(True) == 5
    with engine.connect() as conn:
        assert repo.quota_status(conn, SourceName.SERPAPI) == (5, 5)


def test_rls_isolates_users_for_api_role(engine: Engine) -> None:
    with engine.begin() as conn:
        alice = repo.add_user(conn, "alice@example.com")
        bob = repo.add_user(conn, "bob@example.com")
        repo.add_watch(conn, alice, WATCH)
        bob_watch = repo.add_watch(conn, bob, WATCH)

    with engine.begin() as conn:
        as_role(conn, "ft_api", alice)
        visible = conn.execute(text("SELECT user_id FROM watches")).scalars().all()
        assert visible == [alice]
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 1
        # Alice can't modify Bob's watch: the row is invisible, so nothing is updated.
        updated = conn.execute(
            text("UPDATE watches SET active = false WHERE id = :id"), {"id": bob_watch}
        )
        assert updated.rowcount == 0

    # Field tampering: Alice can't create a watch owned by Bob.
    with pytest.raises(DBAPIError, match="row-level security"), engine.begin() as conn:
        as_role(conn, "ft_api", alice)
        route_id = repo.get_or_create_route(conn, "SLC", "NRT")
        conn.execute(
            text(
                "INSERT INTO watches (user_id, route_id, trip_type, depart_from, depart_to) "
                "VALUES (:u, :r, 'one_way', '2027-03-01', '2027-04-01')"
            ),
            {"u": bob, "r": route_id},
        )


def test_api_role_without_user_sees_nothing(engine: Engine) -> None:
    with engine.begin() as conn:
        repo.add_watch(conn, repo.add_user(conn, "a@example.com"), WATCH)
    with engine.begin() as conn:
        as_role(conn, "ft_api")
        assert conn.execute(text("SELECT count(*) FROM watches")).scalar() == 0


def test_worker_role_can_run_the_pipeline_functions(engine: Engine) -> None:
    with engine.begin() as conn:
        as_role(conn, "ft_worker")
        repo.get_or_create_route(conn, "SLC", "LAX")
        assert repo.consume_quota(conn, SourceName.SERPAPI, 1, 10)
        repo.refresh_route_stats(conn)  # SECURITY DEFINER: worker doesn't own the view
        assert repo.purge_raw_responses(conn, 30) == 0


def test_api_role_cannot_write_market_data(engine: Engine) -> None:
    with pytest.raises(DBAPIError, match="permission denied"), engine.begin() as conn:
        as_role(conn, "ft_api")
        conn.execute(text("INSERT INTO search_runs (source, job) VALUES ('serpapi', 'x')"))


def test_hot_query_can_use_route_dates_index(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            conn.execute(
                text(
                    "EXPLAIN SELECT * FROM fare_observations WHERE route_id = 1 "
                    "AND depart_date = '2027-03-05' ORDER BY observed_at DESC"
                )
            ).scalars()
        )
    assert "fare_obs_route_dates_idx" in plan
