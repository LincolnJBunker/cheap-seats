# Data dictionary

Schema source of truth: [`migrations/sql/0001_initial_up.sql`](../migrations/sql/0001_initial_up.sql).
How values get into these tables: [transformations.md](transformations.md).

```
airports ─┬─< routes >─┬─< watches >── users ──< alerts_sent >── deals
airlines  │            ├─< fare_observations >─┬─< fare_carriers >── airlines
          │            │        │              ├─< booking_options
          │            │        └── raw_responses ──> search_runs
          │            └─< route_price_insights
          └ (route_price_stats: materialized view over fare_observations)
```

## Reference

| Table | Grain | Notes |
|---|---|---|
| `airports` | one IATA airport (or metro) code | `name = iata` means a placeholder created on first sight. |
| `airlines` | one 2-character IATA airline code | Same placeholder rule. |
| `routes` | one **directed** airport pair | Shared by every watch and observation. `UNIQUE(origin, destination)`. |

## User-owned (row-level security)

| Table | Grain | Key columns |
|---|---|---|
| `users` | a person | `email` (case-insensitive `citext`, unique), `tz` for digest timing. |
| `watches` | something a user wants tracked | `route_id`, `trip_type`, `cabin`, `depart_from..depart_to`, `min_nights..max_nights` (round trips only, enforced by CHECK), optional `max_price_cents`, `active` (soft delete keeps history). |
| `alerts_sent` | one alert, per deal per user per channel | Primary key makes sending idempotent: retries can't double-email. |

## Provenance

| Table | Grain | Key columns |
|---|---|---|
| `search_runs` | one execution of a job against one source | `job` (`scan`/`verify`), `status`, `requests_made`, `observations_saved`, `error`. |
| `raw_responses` | one HTTP response | `request_params` (credentials stripped), `payload` (exact JSON), `payload_sha256`. Purged after 30 days. |

## Market data

| Table | Grain | Key columns |
|---|---|---|
| `fare_observations` | one priced itinerary seen at one moment | `price_cents` + `currency`, `depart_date`, `return_date` (NULL for one-way, enforced by CHECK), `stops`, `duration_min`, `observed_at`. Freshness comes from the run's source (`serpapi` = live, `travelpayouts` = cached). |
| `fare_carriers` | (observation, airline) | Junction table, so carriers aren't stored as a list in one column. |
| `booking_options` | (observation, seller) | Where it can be bought and for how much. *Populated in Phase 2.* |
| `route_price_insights` | one Google `price_insights` block | `level` (low/typical/high), typical range. Cold-start baseline. |

## Derived

| Object | Grain | Notes |
|---|---|---|
| `route_price_stats` (mat. view) | route × trip type × cabin × departure month | Percentiles over 120 days of de-duplicated daily minimums. |
| `deals` | (observation, rule version) | Tier, the baseline it was compared to, % below, z-score, and a plain-English `explanation`. *Populated in Phase 1.* |
| `api_quota` | (source, month) | `used <= monthly_limit` enforced by CHECK and by `fn_consume_quota`. |
| `v_fares` (view) | one observation | Everything joined and labelled for humans: dollars, nights, carriers, `live`/`cached`. Start here when exploring in `psql`. |

## Functions

| Function | Why it lives in the database |
|---|---|
| `fn_get_or_create_route(origin, dest)` | Atomic upsert; parallel jobs can't create duplicates or race. |
| `fn_consume_quota(source, n, limit)` | A single conditional `UPDATE` with a row lock: concurrent jobs **cannot** overspend the API budget. Tested with 12 concurrent callers. |
| `sp_refresh_route_stats()` | `SECURITY DEFINER`: only the view owner can refresh, so the worker gets `EXECUTE` on this function instead of ownership. |
| `fn_purge_raw_responses(days)` | Retention in one statement; observations keep their data (FK becomes NULL). |
| `app_current_user_id()` | Reads `app.user_id` for RLS policies; NULL when unset, so policies match nothing. |

## Indexes and the query each one serves

| Index | Query |
|---|---|
| `fare_obs_route_dates_idx (route_id, depart_date, return_date, observed_at DESC)` | Price history for a route and date pair; "already checked live in the last 24h?" |
| `fare_obs_route_price_idx (route_id, trip_type, cabin, price_cents)` | Cheapest fare on a route. |
| `fare_obs_observed_brin (observed_at)` BRIN | Time-range scans on an append-only table, at a tiny fraction of a B-tree's size. |
| `watches_user_active_idx (user_id) WHERE active` | A user's active watches (partial index skips removed ones). |
| `route_price_stats_key` (unique) | Required for `REFRESH MATERIALIZED VIEW CONCURRENTLY`. |
| `raw_responses_fetched_idx` | Retention purge. |
