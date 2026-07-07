# ADR-0008: SQLite (WAL) + sqlite-vec + FTS5; Postgres deferred

- **Status:** accepted
- **Date:** 2026-07-07

## Context

The store holds metadata (segments, events, users, audit), embeddings for semantic search,
and full-text over event summaries — at home-deployment scale: tens of cameras, millions of
segment rows, thousands of events/day, single node. Zero-configuration operation is an
onboarding budget item; every external service in the default stack is a tax on every user.

## Decision

**SQLite in WAL mode** as the only database in the default install: one file at
`/config/vidette.db`, `sqlite-vec` for event-keyframe embeddings, FTS5 for summary search,
nightly `VACUUM INTO` snapshots joining the backup set. Media files remain the source of
truth for video; the DB is rebuildable by a media scan. Postgres support is **deferred, not
rejected** — the schema avoids SQLite-only exotica so a future backend swap stays feasible.

## Consequences

- ✅ Zero-config, zero-daemon, backup = one file; perfectly adequate write rates for segment
  indexing at target scale; vec + FTS cover M3 search without new services.
- ✅ One less thing to break at 3 a.m. — reliability is a security feature here.
- ⚠️ Single-writer semantics require disciplined write paths (one writer process, queued
  writes) — enforced by the process model, and it's good architecture anyway.
- ⚠️ Multi-node (M5 🔭) will need a real server DB — tripwire: the multi-node design doc
  triggers the Postgres ADR; not before.

## Alternatives considered

- **Postgres (+pgvector) from day one** — better concurrency we don't need yet, at the cost
  of every user running a database server to watch their driveway.
- **Timeseries DBs** (Influx/Timescale) for segments — wrong shape; segments are an index,
  not analytics.
- **Embedded KV (RocksDB/badger)** — loses SQL, FTS, vec, and the entire ops story
  (inspectability with standard tools) for no measured win.
