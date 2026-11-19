# Security

Threat model, MVP: one user; jobs run in GitHub Actions against Neon Postgres; no web server.
The assets are API keys, the database credentials, and your email and travel plans.

## Network exposure
- **Nothing listens on a port.** Jobs are short-lived processes that make outbound HTTPS calls.
- The only internet-reachable endpoint is Neon's managed Postgres. Protections: TLS is required
  (the app refuses remote URLs without `sslmode=require|verify-ca|verify-full`; use `verify-full`),
  strong generated passwords, and least-privilege roles. (Neon's IP allowlist is a paid feature;
  GitHub runner IPs change anyway.)
- The future dashboard binds to `127.0.0.1`, with phone access over Tailscale.

## Database roles

| Role | Type | Can |
|---|---|---|
| owner (`neondb_owner`) | login | DDL. Used **only** by `migrate.yml` / local migrations (`FT_MIGRATION_DATABASE_URL`). |
| `ft_worker` | group (NOLOGIN) | Read everything; write market data and runs; manage users and watches. Execute the pipeline functions. Trusted backend. |
| `ft_api` | group (NOLOGIN) | Read market data; read and write **only its own** watches (RLS). For the future dashboard. |

Create a login role for the jobs and make it a member of the group (Neon SQL editor, as owner):

```sql
CREATE ROLE ft_worker_login LOGIN PASSWORD '<generate a long random password>';
GRANT ft_worker TO ft_worker_login;
```

Use `ft_worker_login` in `FT_DATABASE_URL`. It cannot alter or drop tables.

## Row-level security
Enabled on `users`, `watches` and `alerts_sent`. For `ft_api`, policies compare `user_id` to
`app_current_user_id()`, which the app sets per transaction with
`set_config('app.user_id', $1, true)` (parameterized). Unset means no rows. `WITH CHECK` stops a
user from inserting or moving a row into someone else's account (field tampering).
Tests: `tests/integration/test_schema.py::test_rls_isolates_users_for_api_role`.

## Checklist

| Item | Status | How |
|---|---|---|
| Hide API keys | Done | GitHub Actions secrets; local `.env` (gitignored); `SecretStr` everywhere; config errors print variable *names* only. |
| Keys out of logs | Done | Travelpayouts token sent as a header. SerpApi's key must be a query param, so `httpx`/`httpcore` loggers are pinned to WARNING and transport errors are logged by type, not message. |
| Keys out of the database | Done | `sanitize_params` strips credential params before `raw_responses`; integration test asserts no secret is stored. |
| Purge Git secrets | Prevented | New repo with nothing to purge. `gitleaks` pre-commit hook + CI job; enable GitHub secret scanning + push protection in repo settings. If a secret ever leaks: **rotate it first**, then rewrite history with `git filter-repo`. |
| Public DB key | N/A | Supabase/Firebase browser-key pattern. Our browser never talks to the database. |
| Row-level security | Done | See above. |
| Encrypt sensitive data | Done (platform) | TLS in transit (`verify-full`), Neon encryption at rest. Column-level encryption isn't needed: the only personal field is your email. |
| Server-side auth | Phase 3/4 | Dashboard is localhost-only. A `current_user` dependency will set `app.user_id`; RLS enforces it. |
| Lock record access / block field tampering | Done | Pydantic models with `extra="forbid"`; `user_id` comes from the session/CLI owner, never request bodies; RLS `WITH CHECK`. |
| Secure cookies, hash passwords, rate-limit login, bot protection | Phase 4 | Plan: managed auth (Clerk/Auth0/Neon Auth), so we never store passwords. If self-hosted: argon2id, `HttpOnly; Secure; SameSite=Lax` cookies, login rate limiting, Turnstile on signup. |
| Parameterize queries | Done | All SQL uses bound parameters (`text()` + `:name`); ruff's bandit rules (S608) run in CI. |
| Validate all input | Done | CLI input → `WatchInput`; **third-party API responses are untrusted** → validated per item; the database re-checks with CHECK constraints (tested independently of Python). |
| Escape user content | Done | Jinja2 autoescape for HTML email (airline names come from APIs; tested with `<script>`). React escapes by default; `dangerouslySetInnerHTML` will be banned by lint. |
| Restrict file uploads | N/A | No uploads. |
| Trim API responses | Phase 3 | FastAPI `response_model`s; raw payloads never sent to the browser. |
| Security headers | Phase 3 | CSP, `X-Content-Type-Options`, `frame-ancestors 'none'`, `Referrer-Policy`. |
| Force HTTPS | Done | `PoliteClient` refuses non-HTTPS base URLs; no redirects followed; DB TLS required. Localhost dashboard exempt; Tailscale Serve provides HTTPS. |
| Scan dependencies | Done | `pip-audit --strict` in CI, Dependabot (uv + Actions), CodeQL (Python + Actions), `uv.lock` pinned. |
| CI hardening | Done | Actions pinned to commit SHAs; `permissions: contents: read`; `persist-credentials: false`; workflow inputs passed via env to avoid template injection; jobs serialized with `concurrency`. |

## Respecting the data sources
- No scraping. Google Flights data only via SerpApi's API; cached fares via Travelpayouts' API.
  See [ADR 0001](adr/0001-data-sources.md).
- Client-side rate limits (Travelpayouts ≥ 1s apart, SerpApi ≥ 5s apart), exponential backoff with
  jitter on 429/5xx, `Retry-After` honoured, at most 3 attempts, descriptive User-Agent.
- Hard budgets: the SerpApi monthly quota is enforced in the database; per-run caps in config.
