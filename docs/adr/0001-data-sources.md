# ADR 0001: Data sources: APIs only, no scraping

**Status:** accepted · 2026-11-18

## Context
We need live and historical fares for arbitrary routes, for free or close to it, without
violating any provider's terms.

Options considered:
- **Scrape Google Flights directly** (e.g. the `fast-flights` library). Google's ToS forbid
  automated access that ignores its machine-readable instructions, and `google.com/robots.txt`
  disallows `/travel/flights/search`, `/travel/flights/s/` and `/travel/flights/booking`. The
  library also works by impersonating a browser, which means evading bot detection. Rejected.
- **Amadeus Self-Service API**: shut down July 2026. Not available.
- **Kiwi Tequila**: closed to new public sign-ups.
- **SerpApi Google Flights**: official API; returns Google Flights results plus `price_insights`
  and booking options. Free plan: 250 searches/month, 50/hour.
- **Travelpayouts Data API**: free; cached prices from Aviasales users' searches over the past
  days. Broad and cheap, but not live.

## Decision
Use **Travelpayouts for wide, frequent scanning** and **SerpApi for a small budget of live
confirmations**. Sources implement a common client shape (`sources/`), so another provider (Duffel,
a paid API) can be added without changing the pipeline.

## Consequences
- Coverage is limited to about 10–20 watched routes on free tiers; the budget is enforced in the
  database (`fn_consume_quota`).
- Cached data can be stale, so it's always labelled "cached", and mistake-fare alerts will require
  live confirmation.
