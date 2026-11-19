"""Validated domain models shared by mappers, repositories and the CLI."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

IataAirport = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
IataAirline = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class TripType(StrEnum):
    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"


class Cabin(StrEnum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class PriceLevel(StrEnum):
    LOW = "low"
    TYPICAL = "typical"
    HIGH = "high"


class NormalizedFare(BaseModel):
    """One priced itinerary, in our units: integer cents, ISO currency, local departure dates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: IataAirport
    destination: IataAirport
    trip_type: TripType
    cabin: Cabin
    depart_date: date
    return_date: date | None
    price_cents: int = Field(gt=0, lt=10_000_000)
    currency: Currency
    stops: int | None = Field(default=None, ge=0, le=6)
    duration_min: int | None = Field(default=None, ge=1, le=10_080)
    carriers: tuple[IataAirline, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.trip_type is TripType.ONE_WAY and self.return_date is not None:
            raise ValueError("one-way fares cannot have a return date")
        if self.trip_type is TripType.ROUND_TRIP:
            if self.return_date is None:
                raise ValueError("round-trip fares need a return date")
            if self.return_date < self.depart_date:
                raise ValueError("return date is before departure")
        return self


class PriceInsight(BaseModel):
    """Google's assessment of a specific search (from SerpApi price_insights)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: IataAirport
    destination: IataAirport
    trip_type: TripType
    cabin: Cabin
    depart_date: date
    return_date: date | None
    lowest_price_cents: int | None = Field(default=None, gt=0)
    level: PriceLevel | None = None
    typical_low_cents: int | None = Field(default=None, gt=0)
    typical_high_cents: int | None = Field(default=None, gt=0)


class WatchInput(BaseModel):
    """What a user asks us to track. Validated before it ever reaches SQL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: IataAirport
    destination: IataAirport
    trip_type: TripType
    cabin: Cabin = Cabin.ECONOMY
    depart_from: date
    depart_to: date
    min_nights: int | None = Field(default=None, ge=1, le=60)
    max_nights: int | None = Field(default=None, ge=1, le=60)
    max_price_cents: int | None = Field(default=None, gt=0, lt=10_000_000)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.depart_to < self.depart_from:
            raise ValueError("depart window ends before it starts")
        if (self.depart_to - self.depart_from).days > 366:
            raise ValueError("depart window can be at most a year")
        if self.trip_type is TripType.ONE_WAY:
            if self.min_nights is not None or self.max_nights is not None:
                raise ValueError("one-way watches don't take nights")
        else:
            if self.min_nights is None or self.max_nights is None:
                raise ValueError("round-trip watches need --nights MIN-MAX")
            if self.max_nights < self.min_nights:
                raise ValueError("max nights is less than min nights")
        return self
