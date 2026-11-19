"""Integration tests run against a real, disposable Postgres database.

Point FT_TEST_DATABASE_URL at an empty database you don't care about (default: local ft_test).
The schema is migrated down and up once per session, and tables are truncated between tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from flighttracker.db.engine import make_engine

ROOT = Path(__file__).resolve().parents[2]
TEST_URL = os.environ.get("FT_TEST_DATABASE_URL", "postgresql+psycopg:///ft_test")

DATA_TABLES = (
    "alerts_sent, deals, api_quota, route_price_insights, booking_options, fare_carriers, "
    "fare_observations, raw_responses, search_runs, watches, users, routes, airlines, airports"
)


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = TEST_URL
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = make_engine(TEST_URL)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip(f"no test database at {TEST_URL}")
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean(request: pytest.FixtureRequest) -> Iterator[None]:
    if "engine" not in request.fixturenames:
        yield
        return
    eng: Engine = request.getfixturevalue("engine")
    yield
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))
        conn.execute(text("REFRESH MATERIALIZED VIEW route_price_stats"))
