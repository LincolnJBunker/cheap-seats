"""Raw source payloads -> normalized fares (bronze -> silver).

Pure functions: no I/O, no clock, no database. Every rule here is documented, with examples, in
docs/transformations.md. Change one, change the other.

Third-party payloads are untrusted input. Each item is validated; items that fail are skipped and
counted in ``MapResult.rejected`` (with a reason), never silently "fixed".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from flighttracker.models import Cabin, NormalizedFare, PriceInsight, PriceLevel, TripType

IATA_AIRPORT = re.compile(r"^[A-Z]{3}$")
# SerpApi flight numbers look like "DL 1234" / "B6 61": the airline code is the first token.
FLIGHT_NUMBER = re.compile(r"^([A-Z0-9]{2})\s*\d{1,4}[A-Z]?$")


@dataclass
class MapResult:
    fares: list[NormalizedFare] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)
    insight: PriceInsight | None = None


def to_cents(amount: Any) -> int:
    """Dollars (int/float/str) -> integer cents, rounding half up. Raises ValueError."""
    if isinstance(amount, bool) or amount is None:
        raise ValueError("missing price")
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {amount!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"price must be positive: {amount!r}")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _local_date(value: Any) -> date:
    """ISO datetime with offset (e.g. 2027-03-05T07:10:00-07:00) -> its *local* calendar date."""
    if not isinstance(value, str):
        raise ValueError("missing date")
    return datetime.fromisoformat(value).date()


# ---------------------------------------------------------------------------
# Travelpayouts /aviasales/v3/prices_for_dates
# ---------------------------------------------------------------------------
def map_travelpayouts(payload: dict[str, Any], *, cabin: Cabin = Cabin.ECONOMY) -> MapResult:
    result = MapResult()
    if payload.get("success") is not True:
        result.rejected["response not successful"] += 1
        return result

    currency = str(payload.get("currency", "")).upper()
    items = payload.get("data")
    if not isinstance(items, list):
        result.rejected["data is not a list"] += 1
        return result

    for item in items:
        if not isinstance(item, dict):
            result.rejected["item is not an object"] += 1
            continue
        try:
            result.fares.append(_map_tp_item(item, currency, cabin))
        except (ValueError, ValidationError) as exc:
            result.rejected[_reason(exc)] += 1
    return result


def _map_tp_item(item: dict[str, Any], currency: str, cabin: Cabin) -> NormalizedFare:
    # Prefer the specific airport; fall back to the metro/city code (e.g. NYC) when absent.
    origin = item.get("origin_airport") or item.get("origin")
    destination = item.get("destination_airport") or item.get("destination")
    return_at = item.get("return_at")
    trip_type = TripType.ROUND_TRIP if return_at else TripType.ONE_WAY

    transfers = item.get("transfers")
    return_transfers = item.get("return_transfers")
    # Round trips: "stops" = the worse of the two directions (conservative).
    stop_values = [
        v for v in (transfers, return_transfers if return_at else None) if isinstance(v, int)
    ]
    stops = max(stop_values) if stop_values else None

    duration = item.get("duration")
    airline = item.get("airline")
    carriers = (airline,) if isinstance(airline, str) and airline else ()

    return NormalizedFare(
        origin=origin,
        destination=destination,
        trip_type=trip_type,
        cabin=cabin,
        depart_date=_local_date(item.get("departure_at")),
        return_date=_local_date(return_at) if return_at else None,
        price_cents=to_cents(item.get("price")),
        currency=currency,
        stops=stops,
        duration_min=duration if isinstance(duration, int) and duration > 0 else None,
        carriers=carriers,
    )


# ---------------------------------------------------------------------------
# SerpApi google_flights
# ---------------------------------------------------------------------------
def map_serpapi(
    payload: dict[str, Any],
    *,
    origin: str,
    destination: str,
    depart_date: date,
    return_date: date | None,
    cabin: Cabin = Cabin.ECONOMY,
) -> MapResult:
    """Map a google_flights search. Dates/route come from our request because Google prices the
    whole search (for round trips, ``price`` is the round-trip total)."""
    result = MapResult()
    if payload.get("error"):
        result.rejected["api error"] += 1
        return result

    trip_type = TripType.ROUND_TRIP if return_date else TripType.ONE_WAY
    currency = str(payload.get("search_parameters", {}).get("currency", "USD")).upper()

    options = [*(payload.get("best_flights") or []), *(payload.get("other_flights") or [])]
    for option in options:
        if not isinstance(option, dict):
            result.rejected["option is not an object"] += 1
            continue
        try:
            result.fares.append(
                _map_serp_option(
                    option,
                    origin,
                    destination,
                    trip_type,
                    cabin,
                    depart_date,
                    return_date,
                    currency,
                )
            )
        except (ValueError, ValidationError) as exc:
            result.rejected[_reason(exc)] += 1

    result.insight = _map_insight(
        payload.get("price_insights"),
        origin,
        destination,
        trip_type,
        cabin,
        depart_date,
        return_date,
    )
    return result


def _map_serp_option(
    option: dict[str, Any],
    origin: str,
    destination: str,
    trip_type: TripType,
    cabin: Cabin,
    depart_date: date,
    return_date: date | None,
    currency: str,
) -> NormalizedFare:
    segments = option.get("flights")
    if not isinstance(segments, list) or not segments:
        raise ValueError("no flight segments")

    carriers: list[str] = []
    for seg in segments:
        match = FLIGHT_NUMBER.match(str(seg.get("flight_number", "")).strip())
        if match and match.group(1) not in carriers:
            carriers.append(match.group(1))

    total = option.get("total_duration")
    return NormalizedFare(
        origin=origin,
        destination=destination,
        trip_type=trip_type,
        cabin=cabin,
        depart_date=depart_date,
        return_date=return_date,
        price_cents=to_cents(option.get("price")),
        currency=currency,
        stops=len(segments) - 1,  # outbound itinerary only (that's what Google lists here)
        duration_min=total if isinstance(total, int) and total > 0 else None,
        carriers=tuple(carriers),
    )


def _map_insight(
    raw: Any,
    origin: str,
    destination: str,
    trip_type: TripType,
    cabin: Cabin,
    depart_date: date,
    return_date: date | None,
) -> PriceInsight | None:
    if not isinstance(raw, dict):
        return None
    level_raw = str(raw.get("price_level", "")).lower()
    level = PriceLevel(level_raw) if level_raw in PriceLevel._value2member_map_ else None
    typical = raw.get("typical_price_range")
    low = high = None
    if isinstance(typical, list) and len(typical) == 2:
        try:
            low, high = sorted((to_cents(typical[0]), to_cents(typical[1])))
        except ValueError:
            low = high = None
    lowest = None
    if raw.get("lowest_price") is not None:
        try:
            lowest = to_cents(raw["lowest_price"])
        except ValueError:
            lowest = None
    try:
        return PriceInsight(
            origin=origin,
            destination=destination,
            trip_type=trip_type,
            cabin=cabin,
            depart_date=depart_date,
            return_date=return_date,
            lowest_price_cents=lowest,
            level=level,
            typical_low_cents=low,
            typical_high_cents=high,
        )
    except ValidationError:
        return None


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ())) or "item"
        return f"invalid {loc}"
    return str(exc)[:80]
