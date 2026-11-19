"""Command-line entry point: ``ft``.

Locally it manages users and watches; in GitHub Actions it runs the scheduled jobs.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, date, datetime
from typing import Annotated

import typer
from pydantic import ValidationError

from flighttracker import jobs
from flighttracker.config import Settings, get_settings
from flighttracker.db import repositories as repo
from flighttracker.db.engine import make_engine
from flighttracker.errors import safe_error_message
from flighttracker.logging_setup import configure_logging
from flighttracker.models import Cabin, TripType, WatchInput
from flighttracker.notify.email import render_digest, send_via_resend
from flighttracker.sources.base import SourceError, SourceName
from flighttracker.sources.serpapi import SerpApiClient
from flighttracker.sources.travelpayouts import TravelpayoutsClient

log = logging.getLogger("flighttracker")

app = typer.Typer(
    help="Flight deal tracker",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks print local variables (routes, dates, emails) into public CI logs.
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
user_app = typer.Typer(help="Manage users", no_args_is_help=True)
watch_app = typer.Typer(help="Manage watched routes", no_args_is_help=True)
app.add_typer(user_app, name="user")
app.add_typer(watch_app, name="watch")

DATE_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
NIGHTS_RANGE = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _today() -> date:
    return datetime.now(UTC).date()


def _settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        # Only show which settings are wrong; never echo their values.
        fields = ", ".join("FT_" + str(e["loc"][0]).upper() for e in exc.errors())
        typer.secho(f"Configuration error in: {fields}", fg="red", err=True)
        raise typer.Exit(2) from None


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg="red", err=True)
    return typer.Exit(1)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    configure_logging(logging.DEBUG if verbose else logging.INFO)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
@user_app.command("add")
def user_add(
    email: str,
    tz: Annotated[str, typer.Option(help="IANA time zone for digests")] = "America/Denver",
) -> None:
    """Create (or update) a user."""
    engine = make_engine(_settings().database_url.get_secret_value())
    with engine.begin() as conn:
        user_id = repo.add_user(conn, email.strip().lower(), tz)
    typer.echo(f"user {email} -> {user_id}")


# ---------------------------------------------------------------------------
# watches
# ---------------------------------------------------------------------------
@watch_app.command("add")
def watch_add(
    origin: str,
    destination: str,
    depart: Annotated[str, typer.Option(help="Departure window: YYYY-MM-DD..YYYY-MM-DD")],
    user: Annotated[str, typer.Option(help="Owner's email (must exist: ft user add)")],
    nights: Annotated[str | None, typer.Option(help="Round trip length, e.g. 7-14")] = None,
    one_way: Annotated[
        bool, typer.Option("--one-way", help="One-way instead of round trip")
    ] = False,
    cabin: Annotated[Cabin, typer.Option()] = Cabin.ECONOMY,
    max_price: Annotated[int | None, typer.Option(help="Alert threshold in whole USD")] = None,
) -> None:
    """Track a route, e.g. ft watch add SLC NRT --depart 2027-03-01..2027-05-31 --nights 7-14"""
    window = DATE_RANGE.match(depart)
    if not window:
        raise _fail("--depart must look like 2027-03-01..2027-05-31")
    min_n = max_n = None
    if nights:
        match = NIGHTS_RANGE.match(nights)
        if not match:
            raise _fail("--nights must look like 7-14")
        min_n, max_n = int(match.group(1)), int(match.group(2))
    try:
        watch = WatchInput(
            origin=origin.upper(),
            destination=destination.upper(),
            trip_type=TripType.ONE_WAY if one_way else TripType.ROUND_TRIP,
            cabin=cabin,
            depart_from=date.fromisoformat(window.group(1)),
            depart_to=date.fromisoformat(window.group(2)),
            min_nights=min_n,
            max_nights=max_n,
            max_price_cents=max_price * 100 if max_price else None,
        )
    except ValidationError as exc:
        reasons = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        raise _fail(f"Invalid watch: {reasons}") from None
    except ValueError as exc:
        raise _fail(f"Invalid watch: {exc}") from None

    engine = make_engine(_settings().database_url.get_secret_value())
    with engine.begin() as conn:
        user_id = repo.get_user_id(conn, user.strip().lower())
        if user_id is None:
            raise _fail(f"No user {user}. Run: ft user add {user}")
        watch_id = repo.add_watch(conn, user_id, watch)
    typer.echo(f"watch #{watch_id}: {watch.origin}->{watch.destination} {watch.trip_type.value}")


@watch_app.command("list")
def watch_list(user: Annotated[str | None, typer.Option()] = None) -> None:
    """List active watches."""
    engine = make_engine(_settings().database_url.get_secret_value())
    with engine.connect() as conn:
        user_id = repo.get_user_id(conn, user.lower()) if user else None
        watches = repo.list_watches(conn, user_id)
    if not watches:
        typer.echo("No active watches.")
    for w in watches:
        nights = f" {w.min_nights}-{w.max_nights}n" if w.min_nights else ""
        cap = f" <${w.max_price_cents // 100}" if w.max_price_cents else ""
        typer.echo(
            f"#{w.id:<4} {w.origin}->{w.destination} {w.trip_type:<10} {w.cabin:<8} "
            f"{w.depart_from}..{w.depart_to}{nights}{cap}"
        )


@watch_app.command("remove")
def watch_remove(watch_id: int, user: Annotated[str, typer.Option()]) -> None:
    """Deactivate a watch (history is kept)."""
    engine = make_engine(_settings().database_url.get_secret_value())
    with engine.begin() as conn:
        user_id = repo.get_user_id(conn, user.lower())
        if user_id is None or not repo.deactivate_watch(conn, user_id, watch_id):
            raise _fail(f"No active watch #{watch_id} for {user}")
    typer.echo(f"watch #{watch_id} deactivated")


# ---------------------------------------------------------------------------
# scheduled jobs
# ---------------------------------------------------------------------------
@app.command()
def scan() -> None:
    """Fetch cached fares from Travelpayouts for every active watch."""
    settings = _settings()
    if not settings.travelpayouts_token:
        raise _fail("FT_TRAVELPAYOUTS_TOKEN is not set")
    engine = make_engine(settings.database_url.get_secret_value())
    client = TravelpayoutsClient(
        settings.travelpayouts_token.get_secret_value(),
        user_agent=settings.user_agent,
        timeout_seconds=settings.http_timeout_seconds,
    )
    try:
        result = jobs.run_scan(
            engine, client, max_requests=settings.travelpayouts_max_per_run, today=_today()
        )
    except SourceError as exc:
        raise _fail(f"scan failed: {exc}") from None
    finally:
        client.close()
    typer.echo(
        f"scan: {result.requests_made} requests, {result.observations_saved} fares saved, "
        f"{result.rejected} rejected"
    )


@app.command()
def verify(
    max_searches: Annotated[
        int | None, typer.Option(help="Override FT_SERPAPI_MAX_PER_RUN")
    ] = None,
) -> None:
    """Confirm the most promising dates with live Google Flights prices (SerpApi, budgeted)."""
    settings = _settings()
    if not settings.serpapi_key:
        raise _fail("FT_SERPAPI_KEY is not set")
    engine = make_engine(settings.database_url.get_secret_value())
    client = SerpApiClient(
        settings.serpapi_key.get_secret_value(),
        user_agent=settings.user_agent,
        timeout_seconds=settings.http_timeout_seconds,
    )
    try:
        result = jobs.run_verify(
            engine,
            client,
            max_searches=settings.serpapi_max_per_run if max_searches is None else max_searches,
            monthly_limit=settings.serpapi_monthly_limit,
            today=_today(),
        )
    except SourceError as exc:
        raise _fail(f"verify failed: {exc}") from None
    finally:
        client.close()
    with engine.connect() as conn:
        quota = repo.quota_status(conn, SourceName.SERPAPI)
    used = f"{quota[0]}/{quota[1]}" if quota else "0"
    note = f" ({result.skipped_reason})" if result.skipped_reason else ""
    typer.echo(
        f"verify: {result.requests_made} searches, {result.observations_saved} fares saved, "
        f"SerpApi budget used this month {used}{note}"
    )


@app.command()
def digest(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print instead of sending")] = False,
) -> None:
    """Email each user a summary of their watches."""
    settings = _settings()
    engine = make_engine(settings.database_url.get_secret_value())
    with engine.connect() as conn:
        users = repo.list_users(conn)
        per_user = [
            (email, [repo.watch_digest(conn, w) for w in repo.list_watches(conn, user_id)])
            for user_id, email, _tz in users
        ]
    resend_key = settings.resend_api_key
    if not dry_run and not resend_key:
        raise _fail("FT_RESEND_API_KEY is not set (use --dry-run to print)")
    for email, digests in per_user:
        if not digests:
            continue
        message = render_digest(email, digests, _today())
        if dry_run:
            typer.echo(f"--- to: {message.to}\nsubject: {message.subject}\n\n{message.text}")
            continue
        if resend_key is None:  # unreachable: checked above; keeps the type checker honest
            raise _fail("FT_RESEND_API_KEY is not set")
        send_via_resend(
            message, api_key=resend_key.get_secret_value(), sender=settings.alert_email_from
        )
        typer.echo(f"digest sent ({len(digests)} watches)")


@app.command()
def maintain(
    raw_retention_days: Annotated[int, typer.Option(min=1, max=365)] = 30,
) -> None:
    """Refresh route statistics and purge old raw payloads."""
    engine = make_engine(_settings().database_url.get_secret_value())
    with engine.begin() as conn:
        repo.refresh_route_stats(conn)
        purged = repo.purge_raw_responses(conn, raw_retention_days)
    typer.echo(
        f"route stats refreshed; purged {purged} raw responses older than {raw_retention_days} days"
    )


def main() -> None:
    """Console entry point. Unexpected errors are reported without their message or traceback,
    because those can contain routes, dates or emails and these logs are public in CI."""
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("unhandled error", exc_info=True)  # only visible with --verbose locally
        typer.secho(f"ft failed: {safe_error_message(exc)}", fg="red", err=True)
        sys.exit(1)
