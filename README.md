# Flight Deal Tracker

A personal flight-deal tracker: watches round-trip and one-way fares to destinations you choose,
records price history, and emails a daily digest. It's built to grow into deal and mistake-fare
detection, buy/wait advice, and where-to-book recommendations. It runs for **$0/month** on free
tiers, uses **official APIs only (no scraping)**, and exposes **no ports**.

```
GitHub Actions (cron)                         Neon Postgres (TLS, least-privilege roles, RLS)
 ├─ scan     every 6h ─ Travelpayouts API ─┐    raw_responses      (bronze: exact payloads)
 ├─ verify   every 3h ─ SerpApi (Google    ├──▶ fare_observations  (silver: normalized, cents)
 │                      Flights) budgeted ─┘    route_price_stats  (gold: percentiles)
 ├─ maintain daily    ─ refresh stats, purge raw payloads
 └─ digest   daily    ─ Resend ─▶ your inbox
```

## What's implemented (Phase 0)
- Postgres schema in 3NF: integer-cent prices, strict CHECK constraints, targeted indexes
  (including BRIN), a materialized view of route price percentiles, four purpose-built SQL
  functions, least-privilege roles and row-level security. See
  [data dictionary](docs/data-dictionary.md).
- Polite API clients: rate limits, exponential backoff, `Retry-After`, HTTPS only, credentials kept
  out of logs and the database.
- Documented, tested mappings from raw payloads to normalized rows. See
  [transformations](docs/transformations.md).
- A monthly API budget enforced atomically in the database (safe under concurrency).
- `ft` CLI and scheduled GitHub Actions workflows, plus an HTML and plain-text email digest.
- CI: ruff, strict mypy, 60+ unit and integration tests against real Postgres, pip-audit,
  gitleaks, CodeQL, Dependabot.

## Roadmap
1. **Deals**: good / great / mistake-fare tiers with plain-English explanations; instant alerts
   for mistake fares ([rules](docs/deal-rules.md)).
2. **Advice**: BUY / WATCH / WAIT and airline-direct vs OTA recommendations.
3. **Dashboard**: FastAPI (localhost) + React: price history charts, a deal feed and "how was
   this computed" panels.
4. **Multi-user**: managed auth and invites for friends and family (the schema and RLS are
   already in place).

## Quick start
```bash
uv sync && createdb flighttracker && createdb ft_test
cp .env.example .env
uv run alembic upgrade head
uv run pytest
uv run ft user add you@example.com
uv run ft watch add SLC NRT --depart 2027-03-01..2027-05-31 --nights 7-14 --user you@example.com
uv run ft digest --dry-run
```
Full setup (Neon, API keys, GitHub secrets): [docs/setup.md](docs/setup.md).

## Docs
- [Setup](docs/setup.md) · [Security](docs/security.md) · [Data dictionary](docs/data-dictionary.md)
- [Transformations](docs/transformations.md) · [Deal rules](docs/deal-rules.md)
- ADRs: [data sources](docs/adr/0001-data-sources.md) · [hosting](docs/adr/0002-hosting.md) ·
  [schema conventions](docs/adr/0003-schema-conventions.md)

## Stack
Python 3.12 · uv · httpx · Pydantic v2 · SQLAlchemy 2 Core + psycopg 3 · Alembic (SQL migrations) ·
Jinja2 · Typer · pytest + respx · PostgreSQL 16 (Neon) · GitHub Actions
