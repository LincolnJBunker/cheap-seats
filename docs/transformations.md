# Data transformations

Every number the tracker shows passes through three layers. This document lists **every**
transformation between them. If you change a rule in code, change it here in the same commit.

| Layer | Where | What it holds |
|---|---|---|
| **Bronze** (raw) | `raw_responses` | The exact JSON a source returned, the request parameters (credentials removed), HTTP status and a SHA-256 of the payload. Kept 30 days. |
| **Silver** (normalized) | `fare_observations`, `fare_carriers`, `route_price_insights` | One row per priced itinerary, in our units. Each row links to its raw payload (`raw_response_id`) and job run (`search_run_id`). |
| **Gold** (derived) | `route_price_stats`, `deals` | Aggregates and judgements computed from silver. Every derived row stores the inputs it was computed from. |

Code: `src/flighttracker/ingest/mappers.py` (bronze to silver) and the materialized view in
`migrations/sql/0001_initial_up.sql` (silver to gold).

## Global rules

| Rule | Why |
|---|---|
| Money is stored as **integer cents** (`price_cents`), converted with `Decimal` and **round half up**: `$689.50` becomes `68950`, `$100.125` becomes `10013`. | Floats can't represent cents exactly (`0.1 + 0.2 != 0.3`). |
| Currency is stored as given (ISO 4217, uppercased). **Nothing is ever converted.** We request USD from both sources. Stats only use `USD` rows. | A silent currency conversion is the easiest way to show a fake "deal". |
| Dates are the **local calendar date at the airport** (`2027-03-05T22:30-07:00` becomes `2027-03-05`, not the UTC date). | That's the date on your ticket. |
| Timestamps (`observed_at`, `fetched_at`) are `timestamptz` stored in UTC, displayed in your time zone. | No ambiguity across DST. |
| Bad items are **rejected and counted, never "fixed"**. Reasons are logged per run (e.g. `price must be positive`, `invalid origin`). | A guessed value is worse than a missing one. |
| Unknown airport/airline codes get a **placeholder** reference row (name = code) so the foreign keys stay strict without dropping data. | Keeps referential integrity; names can be filled later. |

## Travelpayouts `GET /aviasales/v3/prices_for_dates` → `fare_observations`

**Freshness: cached.** These are prices other Aviasales users saw in roughly the last 2–7 days. The
digest labels them "cached" and they are never used as proof of a live fare.

| Source field | Our column | Transform |
|---|---|---|
| `origin_airport` (fallback `origin`) | `routes.origin` | As-is if it matches `^[A-Z]{3}$`, else the item is rejected. Fallback may be a metro code (e.g. `NYC`). |
| `destination_airport` (fallback `destination`) | `routes.destination` | Same as origin. Prefers the airport (`NRT`) over the metro code (`TYO`). |
| `return_at` present? | `trip_type` | Present: `round_trip`; absent: `one_way`. |
| `departure_at` | `depart_date` | Local date (see global rules). |
| `return_at` | `return_date` | Local date at the destination. |
| `price` | `price_cents` | Dollars to cents (see global rules). Must be > 0. |
| top-level `currency` | `currency` | Uppercased (`usd` becomes `USD`). |
| `transfers`, `return_transfers` | `stops` | One way: `transfers`. Round trip: **max** of the two directions (conservative: "up to N stops each way"). |
| `duration` | `duration_min` | Total minutes if a positive integer, else NULL. |
| `airline` | `fare_carriers` | One row with that 2-character code (Travelpayouts only gives the first carrier). |
| — | `cabin` | Taken from the watch that triggered the scan (the endpoint is economy-only). |

## SerpApi `engine=google_flights` → `fare_observations` + `route_price_insights`

**Freshness: live.** A real Google Flights search made by this tracker at `observed_at`.

| Source field | Our column | Transform |
|---|---|---|
| (our request) `departure_id`, `arrival_id` | `routes.origin/destination` | From the request, because Google prices the whole search. |
| (our request) `outbound_date`, `return_date` | `depart_date`, `return_date` | From the request. |
| `best_flights[]` + `other_flights[]` | one row each | Both lists are stored; "best" is Google's ranking, not ours. |
| `price` | `price_cents` | Dollars to cents. For round trips this is the **round-trip total**. |
| `search_parameters.currency` | `currency` | Uppercased; defaults to `USD` (what we request). |
| `len(flights) - 1` | `stops` | **Outbound itinerary only**: that's what Google lists in the first response. |
| `total_duration` | `duration_min` | Minutes; outbound only. |
| `flights[].flight_number` (`"DL 1234"`) | `fare_carriers` | First token if it's a 2-character airline code; deduplicated, in order. |
| `price_insights.price_level` | `route_price_insights.level` | `low`/`typical`/`high`; any other value becomes NULL (not guessed). |
| `price_insights.typical_price_range` `[a, b]` | `typical_low_cents`, `typical_high_cents` | Dollars to cents, sorted so low ≤ high. |
| `price_insights.lowest_price` | `lowest_price_cents` | Dollars to cents. |

## `route_price_stats` (materialized view)

Per **route × trip type × cabin × departure month**, over observations from the **last 120 days**,
USD only.

1. **De-duplicate first.** For each `(route, trip_type, cabin, depart_date, return_date, UTC
   observation day)` keep only the **cheapest** price. Without this, a cached fare seen four times a
   day would count four times and drag the median toward itself.
2. Then compute `n`, `min`, `p10`, `p25`, `median` (continuous percentiles, rounded to the cent),
   `mean`, `stddev` (sample).

The digest's "our typical price" for a watch is the **n-weighted average of the monthly medians**
across the watch's departure window. It is labelled "not reliable yet" while `n < 20`.

Refreshed by `ft maintain` (daily) via `sp_refresh_route_stats()`, which uses
`REFRESH ... CONCURRENTLY` so readers are never blocked.

## Verify job: which dates get a live check

The SerpApi budget is about 8 searches a day, so the choice is explicit:

1. Watches whose route was checked live **least recently** go first (fair sharing).
2. For each, the **cheapest cached fare from the last 7 days** that fits the watch (date window,
   nights range, trip type, cabin) and hasn't been checked live in the last 24 hours.
3. If there's no cached fare, a **coverage sample**: departure = window start + (today's ordinal
   day mod window length), return = departure + midpoint of the nights range. Deterministic, so
   repeated runs walk across the window.
