# ADR 0002: Hosting: GitHub Actions + Neon Postgres

**Status:** accepted · 2026-11-18

## Context
Jobs run a few times a day for a few seconds each. Requirements: $0, no exposed ports, always on,
no server to maintain.

## Decision
- **GitHub Actions** cron workflows run the jobs (free and unlimited on a public repo).
- **Neon** serverless Postgres (free tier) stores everything.
- The future dashboard runs locally (127.0.0.1) and is reachable from a phone over Tailscale.

Alternatives: a local Docker setup (only runs while the laptop is awake); a Pi or home server
(needs hardware); a VPS (costs money and exposes a host to patch).

## Consequences
- Neon's endpoint is internet-reachable; mitigated by TLS `verify-full`, generated passwords and
  least-privilege roles (see security.md).
- Workflow logs are public on a public repo: secrets are masked by GitHub, and the app never logs
  keys or full request URLs.
- Scheduled workflows can run late and pause after 60 days without repo activity.
- Neon's compute auto-suspends when idle; the first query of each job pays a cold start of about a
  second.
