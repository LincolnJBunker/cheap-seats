"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

SQL lives in migrations/sql/<revision>_{up,down}.sql so it can be reviewed as plain SQL.
"""

from pathlib import Path

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _run(filename: str) -> None:
    # Executed through the raw psycopg connection with no parameters, so the file can contain
    # several statements, $$-quoted function bodies and literal % characters.
    op.get_bind().connection.driver_connection.execute((SQL_DIR / filename).read_text())


def upgrade() -> None:
    _run("${up_revision}_up.sql")


def downgrade() -> None:
    _run("${up_revision}_down.sql")
