-- Reverses 0001. Group roles ft_worker / ft_api are cluster-wide and may be used by other
-- databases, so they are left in place (drop them manually if you really want to).

DROP VIEW IF EXISTS v_fares;
DROP MATERIALIZED VIEW IF EXISTS route_price_stats;

DROP FUNCTION IF EXISTS sp_refresh_route_stats();
DROP FUNCTION IF EXISTS fn_purge_raw_responses(integer);
DROP FUNCTION IF EXISTS fn_consume_quota(fare_source, integer, integer);
DROP FUNCTION IF EXISTS fn_get_or_create_route(char, char);

DROP TABLE IF EXISTS alerts_sent, deals, api_quota, route_price_insights, booking_options,
    fare_carriers, fare_observations, raw_responses, search_runs, watches, users, routes,
    airlines, airports;

-- Dropped after the tables because RLS policies reference it.
DROP FUNCTION IF EXISTS app_current_user_id();

DROP TYPE IF EXISTS alert_channel, price_level, baseline_source, deal_tier, seller_type,
    run_status, fare_source, cabin_class, trip_type;
