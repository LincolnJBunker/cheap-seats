# ADR 0003: Schema conventions

**Status:** accepted · 2026-11-18

## Decisions
1. **Money as integer cents + ISO currency**: exact arithmetic; no silent conversion.
2. **3NF.** Routes are their own entity shared by watches and observations; carriers sit in a
   junction table rather than an array column; derivable attributes (e.g. live vs cached) come from
   joins (`search_runs.source`) instead of being copied into every row.
3. **The database enforces invariants too**: CHECK constraints (watch nights match trip type,
   return dates match trip type, quota ≤ limit), strict foreign keys, enums. Python validates
   first, but the database doesn't trust the application.
   *Lesson learned:* a CHECK that evaluates to NULL **passes**, so nullable columns in CHECKs need
   explicit `IS NOT NULL`. A test caught this.
4. **Raw payloads kept 30 days** (bronze layer) so every derived number can be audited, then
   purged to stay within Neon's free storage.
5. **Logic placement:** SQL functions only where the database does it better (atomic upserts,
   concurrent-safe quota, privileged refresh). Deal scoring stays in Python, where it's
   unit-testable and versioned.
6. **Hand-written SQL migrations** (via Alembic) rather than ORM autogeneration, so functions, RLS
   policies and grants are reviewed as SQL.

## Scale path
`fare_observations` is append-only. At MVP volume (~10k rows/month) a single table with a BRIN
index on `observed_at` is enough. If it grows past roughly 10M rows: declarative monthly range
partitioning on `observed_at` and dropping old partitions instead of `DELETE`.
