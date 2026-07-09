# Changelog

All notable changes to Vidette are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

## [0.1.1] — 2026-07-09

Field fixes from the first real battery-camera deployment (Eufy S3 Pro over NAS-RTSP).

### Fixed
- **Data loss on hard VM kill:** SQLite now runs `synchronous=FULL` in WAL mode (every
  commit fsynced) and the WAL is checkpointed each janitor tick and on shutdown. Before:
  commits lived only in an unsynced WAL — macOS sleep hard-killing Docker Desktop's VM
  discarded them, including the admin account (hence the re-appearing setup wizard).
- **Sleeping-camera hammering:** a camera that stalls repeatedly (battery model asleep) now
  backs off up to 5 minutes instead of reconnecting every ~45 s — which kept the camera
  awake, drained its battery, leaked one orphaned gateway session per cycle (40 in
  10 minutes observed), and spammed `recorder.stalled` events (now rate-limited). Status
  text says the camera is probably sleeping and that recording resumes automatically.
- **Live view in containers:** WebRTC's SDP answer only carried STUN-discovered public-IP
  candidates with ephemeral ports — unreachable from a LAN browser, so tiles fell back to
  snapshots. Two-part fix: an **MSE transport** (fMP4 over an authenticated same-origin
  WebSocket, relayed to go2rtc — works in every topology) as automatic fallback, and a
  `server.webrtc_candidates` / `VIDETTE_WEBRTC_CANDIDATES` setting to advertise a LAN
  address for direct sub-second WebRTC.

### Added
- Player keep-alive pool: navigating away parks the connected stream for 60 s, so
  returning to Live re-attaches instantly instead of renegotiating.
- Tile status now names the transport (`live · webrtc` / `live · mse`).

## [0.1.0] — 2026-07-09

First tagged release: Watch (M1) + Detect (M2) complete, published as a multi-arch container
image on `ghcr.io/baadev/vidette`. See the highlights below and the GitHub release notes.

### Added — M2 completion + camera management UI
- **Camera management beyond YAML:** UI-created cameras live in the database and merge into
  the effective config at boot and on change (hot-apply restarts capture briefly); the
  hand-written YAML remains the IaC source of truth and is never rewritten — id collisions
  resolve to the file with a warning. CRUD API under `/api/v1/config/cameras` (+ ONVIF
  `discover`, per-camera `probe`), Cameras page in the web app, and an SVG zone editor
  (file-defined cameras get a copy-paste YAML snippet instead of silent file edits).
- Web push (VAPID): self-hosted keys (generated once, stored in the DB), subscription API,
  PWA manifest + service worker, subscribe/unsubscribe from the System page; expired
  subscriptions pruned on 404/410.
- MQTT + Home Assistant discovery: availability (retained + LWT), per-camera person
  occupancy, full event JSON, system events; reconnect with backoff; deliberately no
  per-frame motion chatter.
- Live WebSocket event stream (`/api/v1/ws`) with topic filters; the Events page updates
  live and falls back to polling while disconnected.
- Prometheus `/metrics` (hand-rolled exposition, bearer-token scrape): pipeline/recorder/
  disk/notification/bus/event series.
- Event favorites: star in the UI, `favorite=true` filter, and footage upgraded to the
  `favorite` retention class (unstar returns it to the `event` class).
- Event engine hardening: snapshot retry for open confirmed events (gateway-warmup race
  observed live), and confirmed events now upgrade their footage to the `event` retention
  class on close.
- Duration values round-trip through `model_dump(mode="json")` ("3d", not ISO-8601 "P3D").

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
