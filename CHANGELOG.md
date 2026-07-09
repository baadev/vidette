# Changelog

All notable changes to Vidette are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

### Added — M2 "Detect" (core)
- The understanding cascade, tiers 0–2: ffmpeg substream decoder → pure-numpy motion gate
  (EMA background, day/night damping) → YOLOX-tiny detector via ONNX Runtime (Apache-2.0
  model, sha256-pinned download, CoreML/CPU providers, lazy load with loud motion-only
  degrade) → two-stage IoU tracker with zone algebra (approach/dwell/touch/loiter/
  repeat-pass; `public` zones suppress passers-by before any alert logic).
- Event engine: one open event per camera, sensitivity-scaled promotion (policy geometric
  skeleton), snapshots at promotion, lazy clip materialization (remux), `event.confirmed` /
  `event.ended` on the internal bus; events API (list/get/snapshot/clip/feedback) replaces
  the M2 `501` stub; Events page in the web app (feed, chips, clips, 👍/👎).
- Notifications: dispatcher wired to the bus with `when:` rules; signed webhooks per the
  published contract (HMAC, timestamp-refreshing retries, stable delivery id) and Apprise
  channels (Telegram/Discord/100+); delivery failures become rate-limited system events and
  can never notify about themselves (loop guard).
- System events now mirror onto the bus under `system.*` so notification rules match the
  documented patterns.

### Added — M1 completion
- ONVIF adapter (beta): WS-Discovery (`vidette discover`), SOAP profiles → main/sub stream
  selection, WSSE PasswordDigest with HTTP-digest fallback, actionable probe diagnostics.
- Timeline scrub-strip previews: per-hour 1 fps strips generated in the background, served
  via `GET /api/v1/recordings/preview`, hover-scrub UI in Review.
- First-run wizard step 2: camera checklist with live probe/snapshot verification (Setup page).

### Changed
- **Eufy: NAS (RTSP) is the only integration path.** The planned bridge adapter over
  `eufy-security-ws` was removed before shipping — Anker's backend migration shut down the
  legacy API the community client relied on, so a bridge is not technically viable today.
  Eufy models with the built-in NAS (RTSP) feature connect through the plain `rtsp` adapter;
  `docs/cameras/eufy.md` was rewritten accordingly, and `vidette validate` now answers
  `adapter: eufy` with a hint pointing at the guide. (Roadmap rows for bridge live view,
  vendor events and HomeBase clip ingestion were withdrawn — marked blocked, not pretended.)

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
