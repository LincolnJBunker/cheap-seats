from __future__ import annotations

from sqlalchemy import Engine, create_engine

from flighttracker.config import check_database_url


def make_engine(url: str) -> Engine:
    check_database_url(url)
    # Scheduled jobs are short-lived; a tiny pool is plenty and is kind to Neon's free tier.
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2, future=True)
