# Test fixtures

These JSON files are **hand-written samples** shaped after each provider's documented response
format. They are not recordings of real responses:

- `travelpayouts_prices_for_dates.json`: Aviasales Data API `/aviasales/v3/prices_for_dates`
- `serpapi_google_flights_round_trip.json`: SerpApi `engine=google_flights`, round trip

Each includes deliberately bad items (negative price, malformed airport code, itinerary with no
segments) so the tests can check that the mappers reject them.

Once you have real API keys, replace or supplement these with recorded responses (strip anything
personal first), and keep the bad-item cases.
