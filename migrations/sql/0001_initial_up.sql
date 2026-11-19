-- 0001: initial schema.
-- Conventions: prices are integer cents + ISO-4217 currency, all timestamps are timestamptz (UTC),
-- identity primary keys, CHECK constraints for every invariant the database can enforce.
-- Table/column docs: docs/data-dictionary.md. Transform rules: docs/transformations.md.

CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------------------
-- Enumerated types
-- ---------------------------------------------------------------------------
CREATE TYPE trip_type       AS ENUM ('one_way', 'round_trip');
CREATE TYPE cabin_class     AS ENUM ('economy', 'premium_economy', 'business', 'first');
CREATE TYPE fare_source     AS ENUM ('travelpayouts', 'serpapi');
CREATE TYPE run_status      AS ENUM ('running', 'succeeded', 'failed', 'skipped');
CREATE TYPE seller_type     AS ENUM ('airline', 'ota', 'unknown');
CREATE TYPE deal_tier       AS ENUM ('good', 'great', 'mistake');
CREATE TYPE baseline_source AS ENUM ('own_history', 'google_typical');
CREATE TYPE price_level     AS ENUM ('low', 'typical', 'high');
CREATE TYPE alert_channel   AS ENUM ('email');

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
-- Rows may be created as placeholders (name = code) when a source returns a code we have not
-- seen; this keeps foreign keys strict without dropping data. A later sync can fill in names.
CREATE TABLE airports (
    iata          char(3) PRIMARY KEY CHECK (iata ~ '^[A-Z]{3}$'),
    name          text    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    city          text             CHECK (length(city) <= 120),
    country_code  char(2)          CHECK (country_code ~ '^[A-Z]{2}$'),
    tz            text             CHECK (length(tz) <= 64),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE airlines (
    iata        char(2) PRIMARY KEY CHECK (iata ~ '^[A-Z0-9]{2}$'),
    name        text    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- A route is a directed airport pair. Shared by all users' watches and all observations.
CREATE TABLE routes (
    id           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    origin       char(3) NOT NULL REFERENCES airports (iata),
    destination  char(3) NOT NULL REFERENCES airports (iata),
    CONSTRAINT routes_distinct_endpoints CHECK (origin <> destination),
    CONSTRAINT routes_origin_destination_key UNIQUE (origin, destination)
);
CREATE INDEX routes_destination_idx ON routes (destination);

-- ---------------------------------------------------------------------------
-- Users and what they watch (user-owned; protected by row-level security)
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email       citext NOT NULL UNIQUE
                CHECK (length(email) <= 254 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
    tz          text NOT NULL DEFAULT 'America/Denver' CHECK (length(tz) <= 64),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE watches (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    route_id         integer NOT NULL REFERENCES routes (id),
    trip_type        trip_type   NOT NULL,
    cabin            cabin_class NOT NULL DEFAULT 'economy',
    depart_from      date NOT NULL,
    depart_to        date NOT NULL,
    min_nights       smallint,
    max_nights       smallint,
    max_price_cents  integer CHECK (max_price_cents > 0),
    active           boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT watches_window_order CHECK (depart_to >= depart_from),
    CONSTRAINT watches_window_length CHECK (depart_to - depart_from <= 366),
    -- Explicit IS NOT NULL: a CHECK that evaluates to NULL counts as passing, so
    -- "min_nights BETWEEN 1 AND 60" alone would accept a round trip with no nights.
    CONSTRAINT watches_nights_match_trip_type CHECK (
        (trip_type = 'one_way' AND min_nights IS NULL AND max_nights IS NULL)
        OR (trip_type = 'round_trip'
            AND min_nights IS NOT NULL AND max_nights IS NOT NULL
            AND min_nights BETWEEN 1 AND 60
            AND max_nights BETWEEN min_nights AND 60)
    )
);
CREATE INDEX watches_user_active_idx ON watches (user_id) WHERE active;
CREATE INDEX watches_route_idx ON watches (route_id);

-- ---------------------------------------------------------------------------
-- Provenance: every job run and every raw API payload (the "bronze" layer)
-- ---------------------------------------------------------------------------
CREATE TABLE search_runs (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source              fare_source NOT NULL,
    job                 text NOT NULL CHECK (job ~ '^[a-z_]{1,32}$'),
    status              run_status NOT NULL DEFAULT 'running',
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    requests_made       integer NOT NULL DEFAULT 0 CHECK (requests_made >= 0),
    observations_saved  integer NOT NULL DEFAULT 0 CHECK (observations_saved >= 0),
    error               text CHECK (length(error) <= 2000),
    CONSTRAINT search_runs_finish_after_start CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX search_runs_started_idx ON search_runs (started_at DESC);

-- request_params never contains credentials (stripped in code, enforced by a test).
CREATE TABLE raw_responses (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    search_run_id   bigint NOT NULL REFERENCES search_runs (id) ON DELETE CASCADE,
    endpoint        text   NOT NULL CHECK (length(endpoint) <= 200),
    request_params  jsonb  NOT NULL,
    http_status     smallint NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    payload         jsonb  NOT NULL,
    payload_sha256  char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    fetched_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX raw_responses_run_idx ON raw_responses (search_run_id);
CREATE INDEX raw_responses_sha_idx ON raw_responses (payload_sha256);
CREATE INDEX raw_responses_fetched_idx ON raw_responses (fetched_at);

-- ---------------------------------------------------------------------------
-- Normalized market data (the "silver" layer). Not user-owned: shared across all users.
-- ---------------------------------------------------------------------------
CREATE TABLE fare_observations (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route_id         integer NOT NULL REFERENCES routes (id),
    search_run_id    bigint  NOT NULL REFERENCES search_runs (id),
    raw_response_id  bigint  REFERENCES raw_responses (id) ON DELETE SET NULL,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    trip_type        trip_type   NOT NULL,
    cabin            cabin_class NOT NULL,
    depart_date      date NOT NULL,
    return_date      date,
    price_cents      integer NOT NULL CHECK (price_cents > 0 AND price_cents < 10000000),
    currency         char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    stops            smallint CHECK (stops BETWEEN 0 AND 6),
    duration_min     integer CHECK (duration_min BETWEEN 1 AND 10080),
    CONSTRAINT fare_obs_return_matches_trip_type CHECK (
        (trip_type = 'one_way' AND return_date IS NULL)
        OR (trip_type = 'round_trip' AND return_date IS NOT NULL AND return_date >= depart_date)
    )
);
-- Hot paths: "prices for this route and these dates over time" and "cheapest on this route".
CREATE INDEX fare_obs_route_dates_idx
    ON fare_observations (route_id, depart_date, return_date, observed_at DESC);
CREATE INDEX fare_obs_route_price_idx
    ON fare_observations (route_id, trip_type, cabin, price_cents);
-- Append-only, time-ordered table: BRIN is tiny and ideal for observed_at range scans.
CREATE INDEX fare_obs_observed_brin ON fare_observations USING brin (observed_at);
CREATE INDEX fare_obs_run_idx ON fare_observations (search_run_id);

-- Marketing carriers on an itinerary (junction table instead of an array column, to stay 1NF).
CREATE TABLE fare_carriers (
    observation_id  bigint  NOT NULL REFERENCES fare_observations (id) ON DELETE CASCADE,
    airline_iata    char(2) NOT NULL REFERENCES airlines (iata),
    PRIMARY KEY (observation_id, airline_iata)
);
CREATE INDEX fare_carriers_airline_idx ON fare_carriers (airline_iata);

-- Where an itinerary can be bought, and for how much (SerpApi booking options).
CREATE TABLE booking_options (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id  bigint NOT NULL REFERENCES fare_observations (id) ON DELETE CASCADE,
    seller_name     text   NOT NULL CHECK (length(seller_name) BETWEEN 1 AND 120),
    seller_type     seller_type NOT NULL,
    price_cents     integer NOT NULL CHECK (price_cents > 0 AND price_cents < 10000000),
    CONSTRAINT booking_options_unique_seller UNIQUE (observation_id, seller_name)
);

-- Google's own view of a search (via SerpApi price_insights). Used as the cold-start baseline.
CREATE TABLE route_price_insights (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route_id             integer NOT NULL REFERENCES routes (id),
    search_run_id        bigint  NOT NULL REFERENCES search_runs (id),
    raw_response_id      bigint  REFERENCES raw_responses (id) ON DELETE SET NULL,
    observed_at          timestamptz NOT NULL DEFAULT now(),
    trip_type            trip_type NOT NULL,
    cabin                cabin_class NOT NULL,
    depart_date          date NOT NULL,
    return_date          date,
    lowest_price_cents   integer CHECK (lowest_price_cents > 0),
    level                price_level,
    typical_low_cents    integer CHECK (typical_low_cents > 0),
    typical_high_cents   integer CHECK (typical_high_cents > 0),
    CONSTRAINT insights_typical_order CHECK (typical_high_cents >= typical_low_cents)
);
CREATE INDEX insights_route_dates_idx
    ON route_price_insights (route_id, depart_date, observed_at DESC);

-- ---------------------------------------------------------------------------
-- Derived data (the "gold" layer)
-- ---------------------------------------------------------------------------
-- Route price distribution per departure month over the last 120 days of observations.
-- To stop a fare that is re-observed many times from dominating, each
-- (route, trip, cabin, depart_date, return_date, observation day) contributes only its
-- cheapest price. See docs/transformations.md ("route_price_stats").
CREATE MATERIALIZED VIEW route_price_stats AS
WITH daily_min AS (
    SELECT route_id, trip_type, cabin, depart_date, return_date,
           (observed_at AT TIME ZONE 'UTC')::date AS observed_day,
           min(price_cents) AS price_cents
    FROM fare_observations
    WHERE observed_at >= now() - interval '120 days'
      AND currency = 'USD'
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT route_id,
       trip_type,
       cabin,
       date_trunc('month', depart_date)::date                                   AS depart_month,
       count(*)::integer                                                        AS n,
       min(price_cents)::integer                                                AS min_cents,
       round(percentile_cont(0.10) WITHIN GROUP (ORDER BY price_cents))::integer AS p10_cents,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY price_cents))::integer AS p25_cents,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY price_cents))::integer AS median_cents,
       round(avg(price_cents))::integer                                         AS mean_cents,
       round(stddev_samp(price_cents))::integer                                 AS stddev_cents,
       now()                                                                    AS refreshed_at
FROM daily_min
GROUP BY route_id, trip_type, cabin, date_trunc('month', depart_date);
-- Unique index is required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX route_price_stats_key
    ON route_price_stats (route_id, trip_type, cabin, depart_month);

CREATE TABLE deals (
    id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id         bigint NOT NULL REFERENCES fare_observations (id) ON DELETE CASCADE,
    tier                   deal_tier NOT NULL,
    baseline_source        baseline_source NOT NULL,
    baseline_median_cents  integer NOT NULL CHECK (baseline_median_cents > 0),
    baseline_n             integer CHECK (baseline_n >= 0),
    pct_below              numeric(5, 2) NOT NULL CHECK (pct_below BETWEEN -1000 AND 100),
    z_score                numeric(6, 2),
    rule_version           text NOT NULL CHECK (rule_version ~ '^v[0-9]+(\.[0-9]+)*$'),
    explanation            text NOT NULL CHECK (length(explanation) BETWEEN 1 AND 1000),
    detected_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT deals_one_per_observation_rule UNIQUE (observation_id, rule_version)
);
CREATE INDEX deals_detected_idx ON deals (detected_at DESC);

-- One row per (deal, user, channel): the primary key makes alert sending idempotent.
CREATE TABLE alerts_sent (
    deal_id  bigint NOT NULL REFERENCES deals (id) ON DELETE CASCADE,
    user_id  uuid   NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    channel  alert_channel NOT NULL,
    sent_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (deal_id, user_id, channel)
);
CREATE INDEX alerts_sent_user_idx ON alerts_sent (user_id, sent_at DESC);

-- Monthly API budget per source.
CREATE TABLE api_quota (
    source         fare_source NOT NULL,
    period_start   date NOT NULL CHECK (extract(day FROM period_start) = 1),
    used           integer NOT NULL DEFAULT 0 CHECK (used >= 0),
    monthly_limit  integer NOT NULL CHECK (monthly_limit > 0),
    PRIMARY KEY (source, period_start),
    CONSTRAINT api_quota_within_limit CHECK (used <= monthly_limit)
);

-- ---------------------------------------------------------------------------
-- Human-readable view: one row per observation, with every label spelled out.
-- security_invoker = the caller's privileges and RLS apply, not the view owner's.
-- ---------------------------------------------------------------------------
CREATE VIEW v_fares WITH (security_invoker = true) AS
SELECT fo.id                                          AS observation_id,
       r.origin,
       r.destination,
       fo.trip_type,
       fo.cabin,
       fo.depart_date,
       fo.return_date,
       fo.return_date - fo.depart_date                AS nights,
       round(fo.price_cents / 100.0, 2)               AS price,
       fo.currency,
       fo.stops,
       fo.duration_min,
       (SELECT string_agg(fc.airline_iata, ',' ORDER BY fc.airline_iata)
          FROM fare_carriers fc WHERE fc.observation_id = fo.id) AS carriers,
       sr.source,
       CASE sr.source WHEN 'serpapi' THEN 'live' ELSE 'cached' END AS freshness,
       fo.observed_at,
       fo.raw_response_id
FROM fare_observations fo
JOIN routes r       ON r.id = fo.route_id
JOIN search_runs sr ON sr.id = fo.search_run_id;

-- ---------------------------------------------------------------------------
-- Functions (only where the database is the right place for the logic)
-- ---------------------------------------------------------------------------

-- Atomic "get or create" for a route, safe when parallel jobs see the same new pair.
-- Unknown airport codes get placeholder rows so the foreign keys stay strict.
CREATE FUNCTION fn_get_or_create_route(p_origin char(3), p_destination char(3))
RETURNS integer
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_id integer;
BEGIN
    IF p_origin !~ '^[A-Z]{3}$' OR p_destination !~ '^[A-Z]{3}$' THEN
        RAISE EXCEPTION 'invalid IATA code(s): %, %', p_origin, p_destination
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO airports (iata, name)
    VALUES (p_origin, p_origin), (p_destination, p_destination)
    ON CONFLICT (iata) DO NOTHING;

    INSERT INTO routes (origin, destination)
    VALUES (p_origin, p_destination)
    ON CONFLICT ON CONSTRAINT routes_origin_destination_key DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT id INTO v_id FROM routes
        WHERE origin = p_origin AND destination = p_destination;
    END IF;
    RETURN v_id;
END;
$$;

-- Atomically spend API budget. Returns false (and spends nothing) if it would exceed the limit.
-- The conditional UPDATE takes a row lock, so concurrent callers can never overspend.
CREATE FUNCTION fn_consume_quota(p_source fare_source, p_n integer, p_monthly_limit integer)
RETURNS boolean
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_period date := date_trunc('month', now() AT TIME ZONE 'UTC')::date;
BEGIN
    IF p_n IS NULL OR p_n <= 0 THEN
        RAISE EXCEPTION 'p_n must be a positive integer' USING ERRCODE = '22023';
    END IF;

    INSERT INTO api_quota (source, period_start, used, monthly_limit)
    VALUES (p_source, v_period, 0, p_monthly_limit)
    ON CONFLICT (source, period_start) DO NOTHING;

    UPDATE api_quota
       SET used = used + p_n
     WHERE source = p_source
       AND period_start = v_period
       AND used + p_n <= monthly_limit;

    RETURN FOUND;
END;
$$;

-- Refresh route stats without blocking readers. SECURITY DEFINER because only the
-- materialized view's owner may refresh it; the worker role is granted EXECUTE only.
CREATE FUNCTION sp_refresh_route_stats()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY route_price_stats;
END;
$$;

-- Retention: delete raw payloads older than p_days (observations keep their data;
-- their raw_response_id becomes NULL). Returns the number of rows deleted.
CREATE FUNCTION fn_purge_raw_responses(p_days integer)
RETURNS integer
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_deleted integer;
BEGIN
    IF p_days IS NULL OR p_days < 1 THEN
        RAISE EXCEPTION 'p_days must be >= 1' USING ERRCODE = '22023';
    END IF;
    DELETE FROM raw_responses WHERE fetched_at < now() - make_interval(days => p_days);
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$;

-- The current application user, set per transaction with set_config('app.user_id', ..., true).
-- Returns NULL when unset, so RLS policies then match nothing.
CREATE FUNCTION app_current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path = public, pg_temp
AS $$
    SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

-- ---------------------------------------------------------------------------
-- Roles and privileges (least privilege). Group roles are NOLOGIN; create login roles that
-- are members of them (see docs/security.md). Role creation is idempotent because roles are
-- cluster-wide.
--   ft_worker: trusted backend (scheduled jobs, local CLI). Reads everything, writes market data.
--   ft_api:    future dashboard / multi-user API. Sees only the current user's rows.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ft_worker') THEN
        CREATE ROLE ft_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ft_api') THEN
        CREATE ROLE ft_api NOLOGIN;
    END IF;
END;
$$;

-- Functions are executable by PUBLIC by default; lock ours down and grant explicitly.
-- (Not "ALL FUNCTIONS IN SCHEMA": that would also strip the citext extension's functions.)
REVOKE ALL ON FUNCTION fn_get_or_create_route(char, char), fn_consume_quota(fare_source, integer, integer),
    sp_refresh_route_stats(), fn_purge_raw_responses(integer), app_current_user_id() FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ft_worker, ft_api;

-- Worker
GRANT SELECT, INSERT, UPDATE ON airports, airlines, routes TO ft_worker;
GRANT SELECT, INSERT, UPDATE ON search_runs TO ft_worker;
GRANT SELECT, INSERT ON raw_responses, fare_observations, fare_carriers, booking_options,
    route_price_insights, deals TO ft_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, watches TO ft_worker;
GRANT SELECT, INSERT ON alerts_sent TO ft_worker;
GRANT SELECT ON api_quota, route_price_stats, v_fares TO ft_worker;
GRANT EXECUTE ON FUNCTION fn_get_or_create_route(char, char), fn_consume_quota(fare_source, integer, integer),
    sp_refresh_route_stats(), fn_purge_raw_responses(integer), app_current_user_id() TO ft_worker;
-- fn_consume_quota / fn_purge_raw_responses run as the caller, so the caller needs these too.
GRANT INSERT, UPDATE ON api_quota TO ft_worker;
GRANT DELETE ON raw_responses TO ft_worker;

-- API (read market data, manage only its own watches)
GRANT SELECT ON airports, airlines, routes, fare_observations, fare_carriers, booking_options,
    route_price_insights, route_price_stats, deals, v_fares TO ft_api;
GRANT SELECT ON users TO ft_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON watches TO ft_api;
GRANT SELECT ON alerts_sent TO ft_api;
GRANT EXECUTE ON FUNCTION app_current_user_id(), fn_get_or_create_route(char, char) TO ft_api;
GRANT INSERT ON airports, routes TO ft_api;

-- ---------------------------------------------------------------------------
-- Row-level security on user-owned tables
-- ---------------------------------------------------------------------------
ALTER TABLE users       ENABLE ROW LEVEL SECURITY;
ALTER TABLE watches     ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts_sent ENABLE ROW LEVEL SECURITY;

CREATE POLICY users_self ON users
    FOR SELECT TO ft_api
    USING (id = app_current_user_id());

CREATE POLICY watches_owner ON watches
    FOR ALL TO ft_api
    USING (user_id = app_current_user_id())
    WITH CHECK (user_id = app_current_user_id());

CREATE POLICY alerts_sent_owner ON alerts_sent
    FOR SELECT TO ft_api
    USING (user_id = app_current_user_id());

-- The worker is the trusted backend and processes every user's watches.
CREATE POLICY users_worker       ON users       FOR ALL TO ft_worker USING (true) WITH CHECK (true);
CREATE POLICY watches_worker     ON watches     FOR ALL TO ft_worker USING (true) WITH CHECK (true);
CREATE POLICY alerts_sent_worker ON alerts_sent FOR ALL TO ft_worker USING (true) WITH CHECK (true);
