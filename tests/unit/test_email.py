from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from flighttracker.db.repositories import FareSummary, Watch, WatchDigest
from flighttracker.notify.email import render_digest


def make_digest(carriers: str) -> WatchDigest:
    watch = Watch(
        id=1,
        user_id=uuid4(),
        route_id=1,
        origin="SLC",
        destination="NRT",
        trip_type="round_trip",
        cabin="economy",
        depart_from=date(2027, 3, 1),
        depart_to=date(2027, 5, 31),
        min_nights=7,
        max_nights=14,
        max_price_cents=60000,
    )
    fare = FareSummary(
        price_cents=64100,
        currency="USD",
        depart_date=date(2027, 3, 5),
        return_date=date(2027, 3, 15),
        stops=1,
        carriers=carriers,
        observed_at=datetime(2026, 11, 18, tzinfo=UTC),
    )
    return WatchDigest(
        watch=watch,
        cheapest_live=fare,
        cheapest_cached=None,
        median_cents=None,
        sample_n=0,
        google_level="low",
        google_typical_low_cents=78000,
        google_typical_high_cents=115000,
    )


def test_digest_contains_key_numbers() -> None:
    email = render_digest("me@example.com", [make_digest("DL")], date(2026, 11, 18))
    assert "$641" in email.html and "$641" in email.text
    assert "typical $780" in email.html
    assert "no history yet" in email.html
    assert "nothing in the last 48h" in email.text


def test_third_party_strings_are_escaped_in_html() -> None:
    email = render_digest(
        "me@example.com", [make_digest("<script>alert(1)</script>")], date(2026, 11, 18)
    )
    assert "<script>" not in email.html
    assert "&lt;script&gt;" in email.html
