"""Error messages that are safe to print in public CI logs.

Postgres error text can contain the offending row ("Failing row contains (... SLC, NRT,
2027-03-01 ...)") and SQLAlchemy can append bound parameters. Neither may reach GitHub Actions
logs, so database errors are reduced to their type, SQLSTATE and constraint name.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError, SQLAlchemyError


def safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, DBAPIError):
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None) or "unknown"
        diag = getattr(orig, "diag", None)
        constraint = getattr(diag, "constraint_name", None)
        table = getattr(diag, "table_name", None)
        parts = [f"database error {type(orig).__name__} (SQLSTATE {sqlstate})"]
        if table:
            parts.append(f"table={table}")
        if constraint:
            parts.append(f"constraint={constraint}")
        return " ".join(parts)
    if isinstance(exc, SQLAlchemyError):
        return f"database error {type(exc).__name__}"
    return f"unexpected error {type(exc).__name__}"
