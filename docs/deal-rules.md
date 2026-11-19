# Deal rules

> **Status: specification for Phase 1 and 2. Not implemented yet.** Phase 0 collects the data
> these rules need (`route_price_stats`, `route_price_insights`). Each rule set gets a version
> (`rule_version`, e.g. `v1`) stored on every `deals` row, so old alerts stay explainable after the
> rules change.

## Baseline (v1)
- **Own history** when the route/month has `n ≥ 20` de-duplicated observations: compare to
  `route_price_stats.median_cents` / `p10` / `p25` / `stddev`.
- Otherwise **Google's typical range** (`route_price_insights`), midpoint as the median.
  `baseline_source` records which one was used.

## Tiers (v1)
| Tier | Rule | Alert |
|---|---|---|
| good | price ≤ p25 **or** ≥ 20% below median | daily digest |
| great | price ≤ p10 **or** ≥ 35% below median | daily digest, highlighted |
| mistake | ≥ 55% below median **or** z ≤ −3, **and** confirmed by a live SerpApi search, **and** sanity checks pass | instant email |

Mistake-fare sanity checks: currency is USD; duration < 2.5× the route's typical duration; ≤ 2
stops. These keep data errors and absurd 40-hour itineraries from triggering alerts.

Every deal stores a plain-English `explanation`, for example:
> $312 RT SLC→NRT, Mar 2027: 48% below our 120-day median of $600 (42 observations); Google rates
> it LOW (typical $550–720).

## Buy / Watch / Wait (Phase 2, a heuristic, not a prediction)
Signals, all shown in the email:
1. Google `price_level` (low / typical / high).
2. Our percentile for this route/month.
3. Days to departure vs. the typical booking window (domestic ≈ 21–90 days, international
   ≈ 60–180 days).
4. 14-day trend of daily minimums.

**BUY** = low level or ≤ p25, inside or past the window. **WAIT** = high level and well before the
window. **WATCH** = everything else.

## Where to book (Phase 2)
From SerpApi booking options (costs an extra search, so only for good+ deals):
- Book **direct with the airline** if it's within $15 or 5% of the cheapest option: free 24-hour
  cancellation (US DOT rule for US-touching flights booked ≥ 7 days out), and easier changes and
  rebooking when flights are disrupted.
- Otherwise the cheapest **well-known OTA**; flag unknown sellers.
- Mistake fares: book fast, ideally direct; don't book anything non-refundable (hotels etc.) until
  the ticket is issued, because airlines sometimes cancel mistake fares.
