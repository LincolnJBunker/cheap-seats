"""Initial schema: reference data, users/watches, provenance, fares, derived stats, functions,
roles and row-level security.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-18

SQL lives in migrations/sql/<revision>_{up,down}.sql so it can be reviewed as plain SQL.
"""

from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _run(filename: str) -> None:
    # Executed through the raw psycopg connection with no parameters, so the file can contain
    # several statements, $$-quoted function bodies and literal % characters.
    op.get_bind().connection.driver_connection.execute((SQL_DIR / filename).read_text())


def upgrade() -> None:
    _run("0001_initial_up.sql")


def downgrade() -> None:
    _run("0001_initial_down.sql")
