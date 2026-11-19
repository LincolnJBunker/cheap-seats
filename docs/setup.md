# Setup

## 1. Local development
Prereqs: [uv](https://docs.astral.sh/uv/) and a local PostgreSQL 16+.

```bash
uv sync
uv run pre-commit install
createdb flighttracker && createdb ft_test
cp .env.example .env            # then fill in values; .env is gitignored
uv run alembic upgrade head
uv run pytest                   # unit + integration (uses ft_test)
```

## 2. Accounts (all free tiers)
| Service | What for | Where the key goes |
|---|---|---|
| [Neon](https://neon.tech) | Postgres | `FT_DATABASE_URL`, `FT_MIGRATION_DATABASE_URL` |
| [Travelpayouts](https://www.travelpayouts.com) | Cached fares (sign up, then API token in your profile) | `FT_TRAVELPAYOUTS_TOKEN` |
| [SerpApi](https://serpapi.com) | Live Google Flights (250 searches/month free) | `FT_SERPAPI_KEY` |
| [Resend](https://resend.com) | Email (sends to your own account address without a domain) | `FT_RESEND_API_KEY` |

## 3. Neon database
1. Create a project (Postgres 16 or 17). Copy the **owner** connection string and add
   `?sslmode=verify-full&sslrootcert=system`. Change `postgresql://` to `postgresql+psycopg://`.
2. Run migrations with the owner URL:
   ```bash
   FT_MIGRATION_DATABASE_URL='<owner url>' uv run alembic upgrade head
   ```
3. Create the least-privilege login for jobs (SQL editor, as owner). See
   [security.md](security.md#database-roles). Its URL becomes `FT_DATABASE_URL`.

## 4. Your user and watches (run locally against Neon)
```bash
uv run ft user add you@example.com
uv run ft watch add SLC NRT --depart 2027-03-01..2027-05-31 --nights 7-14 --user you@example.com
uv run ft watch add SLC LAX --one-way --depart 2027-01-05..2027-01-20 --max-price 90 --user you@example.com
uv run ft watch list
```
Watches live in the database, not the repo, so your travel plans don't show up on GitHub.

## 5. GitHub
1. Push the repo (public: unlimited Actions minutes and free CodeQL).
2. **Settings → Secrets and variables → Actions**: add `FT_DATABASE_URL`,
   `FT_MIGRATION_DATABASE_URL`, `FT_TRAVELPAYOUTS_TOKEN`, `FT_SERPAPI_KEY`, `FT_RESEND_API_KEY`.
3. **Settings → Code security**: enable secret scanning, push protection and Dependabot alerts.
4. Run **Actions → scan → Run workflow**, then **verify**, then **digest** to test it end to end.

Schedules (UTC): scan every 6h at :17, verify every 3h at :47, maintain 09:30, digest 14:00.
GitHub may start scheduled runs a few minutes late, and disables them after 60 days without repo
activity (Dependabot PRs usually prevent that).
