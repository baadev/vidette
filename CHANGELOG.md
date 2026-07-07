# Changelog

All notable changes to Vidette are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

### Added — M1 "Watch" (in progress)
- Recording pipeline: managed go2rtc gateway (config generated from `cameras:`, atomic
  writes, hot reload, health), codec-copy fMP4 segment recorder with crash-only supervision
  (watchdog, exponential backoff, hour-rollover safety), SQLite segment index.
- Storage operations: retention runtime (per-camera overrides, pressure deletions that never
  touch events/favorites), disk watermarks + write probes, all failures as loud system events.
- Range export: remuxed MP4 jobs (single worker, path-safety checks) via API and UI.
- Auth: first-run admin bootstrap (no default credentials), scrypt password hashing,
  session cookies, scoped API tokens, uniform login errors with backoff.
- REST API v1: auth, cameras (with live recorder state), recordings + hourly summary +
  segment files, export jobs, stream endpoints with an authenticated WHEP proxy and snapshot
  proxy (go2rtc admin API stays private), system events.
- Web app: login + first-run wizard, WebRTC live wall with snapshot fallback and keyboard
  navigation, review page (hour strip, segment playback, MP4 export), system page.
- SQLite store (WAL) with append-only migrations; `aiosqlite`; single-writer discipline.
- Dockerfile ships ffmpeg; compose publishes only the WebRTC media port.

### Added — M0
- Project genesis (M0): architecture documentation and ADRs, selling README, roadmap with
  status legend, contribution/security/Claude guides, growth strategy and brand docs.
- Executable configuration schema (pydantic) with `${ENV}` interpolation, validator CLI
  (`vidette validate`) and API endpoint (`POST /api/v1/config/validate`), covered by tests.
- Typed protocols for camera adapters, pipeline tiers and notifiers; in-process event bus;
  retention planner (pure, tested); HMAC webhook signing (tested).
- FastAPI skeleton: `/healthz`, `/api/v1/system`, honest `501 designed` stubs for M1/M2 surface.
- Web app shell (React + Vite, dark theme) with live health status.
- Docker Compose stack: vidette + go2rtc sidecar, optional `vlm` profile (Ollama).
- CI: ruff, mypy (strict), pytest; web typecheck and build.
