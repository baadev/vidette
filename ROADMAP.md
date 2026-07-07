# Roadmap

This file is the single source of truth for **what exists, what is being built, and what is
designed but not started**. It is updated in the same PR as the code it describes.

**Legend** — used consistently across the repo:

| Mark | Meaning |
|---|---|
| ✅ shipped | Merged, tested, documented. You can use it today. |
| 🚧 in progress | Actively being built on `main` or a branch. |
| 📐 designed | Specification published (docs/ADR), RFC open for comments, code not started. |
| 🔭 exploring | Direction we believe in; no committed design yet. |
| ❌ out of scope | Deliberately not planned — with a reason. |

We promise **sequence, not dates**. Every milestone ships with explicit efficiency budgets
(see [principles](docs/project/principles.md)); a milestone is not "done" because features
exist — it is done when the budgets hold.

---

## M0 — Foundation *(current)*

**Goal:** publish a reviewable architecture and a running shell, so the design is hardened by
public critique before the expensive code is written.

**Done when:** docs cover every M1–M4 subsystem; config schema is executable and tested;
CI is green; the compose stack starts and validates a real config.

| Item | Status |
|---|---|
| Architecture docs + ADRs (runtime, gateway, cascade, storage, plugins, license, web, DB) | ✅ |
| Selling README, roadmap, contribution/security/Claude guides | ✅ |
| Config schema (pydantic) + validator CLI/API + tests | ✅ |
| Event model, adapter/pipeline/notifier protocols (typed, tested where pure) | ✅ |
| Retention planner (pure logic) + webhook signing + tests | ✅ |
| Web app shell (dark, fast, honest status page) | ✅ |
| Compose stack (vidette + go2rtc, optional ollama profile) | ✅ |
| CI (ruff, mypy strict, pytest; web typecheck + build) | ✅ |
| Architecture RFC issue open for community review | ✅ ([#1](https://github.com/baadev/vidette/issues/1)) |

## M1 — Watch

**Goal:** replace the vendor app for *viewing and keeping* footage. This is the trust milestone:
if we lose frames, nothing else matters.

**Done when (budgets):** 4×1080p cameras on an Intel N100: < 25 % CPU total, live view p50
latency < 1 s (WebRTC), zero dropped segments over 7 days, cold start → first live frame < 3 s.
*The budgets are not yet measured — M1 stays open until they are published.*

| Item | Status |
|---|---|
| go2rtc lifecycle management (config generation, hot reload, health) | ✅ |
| RTSP adapter (manual URLs, main+sub) | ✅ |
| ONVIF adapter (discovery, profiles) | 📐 |
| Codec-copy recorder → fMP4 segments + SQLite index (watchdog, backoff, hour-rollover safe) | ✅ |
| Retention classes (continuous/motion/events/favorites) + watermark cleanup runtime | ✅ |
| Auth: first-run admin bootstrap, sessions, scoped API tokens | ✅ |
| Live wall: WebRTC via authenticated WHEP proxy, snapshot fallback, keyboard-first | ✅ |
| Timeline: hour strip + segment playback + gap rendering | ✅ |
| Timeline scrub-strip previews (fast visual scrubbing) | 📐 |
| Range export: remuxed MP4 (no re-encode) via UI + API | ✅ |
| First-run wizard: add-camera + stream-verification steps (admin step ✅) | 🚧 |
| Disk health: free-space watermarks, write probes, loud failure events | ✅ |
| Reference-budget benchmark run + published numbers | 🚧 |

## M2 — Detect

**Goal:** alerts worth enabling: objects, trajectories and zone semantics kill the
"motion spam" class of notifications *without any LLM involved*.

**Done when (budgets):** tiers 0–2 add < 15 % CPU on the M1 reference box at 5 detect-fps ×
4 cameras; motion→notification p50 < 2 s; false-alert rate measurably below "raw motion"
baseline on the reference clip set.

| Item | Status |
|---|---|
| Tier 0 motion gate (substream, frame-diff) | 📐 |
| Tier 1 detector (Apache-2.0 models via ONNX Runtime; CPU/OpenVINO/CUDA/CoreML) | 📐 |
| Tier 2 ByteTrack + trajectory features (approach vector, dwell, loiter, repeat-pass) | 📐 |
| Zone editor (entry/object/private/public) with per-kind semantics | 📐 |
| Event engine: lifecycle, dedupe, review UI, favorites | 📐 |
| Notifications: signed webhooks, web push (VAPID), Apprise channels | 📐 |
| MQTT + Home Assistant discovery (camera, motion, person, event entities) | 📐 |
| Eufy adapter **preview** via eufy-security-ws sidecar (live, events, clip pull) | 📐 |
| Prometheus `/metrics` | 📐 |
| Reference clip set + accuracy harness (public, versioned) | 📐 |

## M3 — Understand

**Goal:** the differentiator: events become sentences, search becomes semantic, storage
becomes durable beyond one box.

**Done when (budgets):** VLM calls ≤ configured budget with zero pipeline stalls; event
summary latency p50 < 10 s (local 7B-class VLM on reference GPU / < 4 s cloud); storage
compaction ≥ 60 % size reduction on archived continuous footage.

| Item | Status |
|---|---|
| Tier 3 VLM worker: best-shot selection, structured verdicts, budgets, caching | 📐 |
| Providers: Ollama / llama.cpp local; OpenAI / Anthropic / Google opt-in | 📐 |
| Intent scoring v1 (approach/dwell/touch × VLM verdict fusion) | 📐 |
| Semantic search: SigLIP-class embeddings + sqlite-vec + FTS5 | 📐 |
| Eufy adapter stable: HomeBase clip ingestion ("your recordings, finally yours") | 📐 |
| Archive compaction (HEVC/AV1 re-encode of cold continuous footage, hw-accel) | 📐 |
| Off-site event backup (S3-compatible) + nightly DB snapshot | 📐 |
| Clip redaction on export (blur regions) | 🔭 |

## M4 — Converse *(north star v1)*

**Goal:** "tell it what you care about, in your language."

| Item | Status |
|---|---|
| Policy compiler: NL → inspectable PolicySpec (zones, triggers, VLM question, thresholds) | 📐 |
| Per-policy calibration from event feedback (👍/👎 adjusts thresholds) | 📐 |
| Sensitivity presets (relaxed/balanced/paranoid) with visible semantics | 📐 |
| Policy dry-run: replay last N days, show what *would* have fired | 📐 |
| Trusted faces: local enrollment UI + alert suppression (uncertain match never suppresses) | 📐 |
| Multi-camera reasoning (same track across cameras) | 🔭 |

## M5 — Ecosystem

| Item | Status |
|---|---|
| Adapter/plugin SDK v1 (semver, conformance tests, `vidette-adapter-*` registry page) | 📐 |
| Home Assistant add-on packaging | 📐 |
| PWA polish: installable, offline event review, iOS push caveats documented | 📐 |
| Bridges: UniFi Protect, Ring (ring-mqtt), Wyze, HomeKit via go2rtc | 🔭 |
| Multi-node: remote recorder agents, one UI | 🔭 |
| Fine-tuned intent models ("Vidette+", optional, never required) | 🔭 |

---

## Capability inventory

The complete list of key functionality and its implementation state.

### Ingest & cameras
| Capability | Status | Milestone |
|---|---|---|
| Manual RTSP sources (main/sub streams) | ✅ | M1 |
| ONVIF discovery, profiles, PTZ | 📐 | M1–M2 |
| Adapter SDK (typed protocol, entry points, sidecar bridges) | ✅ interfaces / 📐 3rd-party runtime | M0/M2 |
| Eufy via eufy-security-ws (live, events, station clip pull) | 📐 | M2–M3 |
| Two-way audio | 🔭 | M5 |

### Recording & storage
| Capability | Status | Milestone |
|---|---|---|
| Codec-copy fMP4 segment recorder + SQLite index | ✅ | M1 |
| Retention classes + watermark cleanup (planner + runtime, tested) | ✅ | M1 |
| Scrub-strip previews for fast timelines | 📐 | M1 |
| Archive compaction (HEVC/AV1) | 📐 | M3 |
| Off-site event backup (S3-compatible) | 📐 | M3 |
| Disk health monitoring (watermarks, write probes, loud events) | ✅ | M1 |

### Understanding
| Capability | Status | Milestone |
|---|---|---|
| Motion gate (Tier 0) | 📐 | M2 |
| Object detection (Tier 1, permissive-license models) | 📐 | M2 |
| Tracking + trajectory geometry (Tier 2) | 📐 | M2 |
| Zone semantics incl. `public` (ignore passers-by) and `object` (wall equipment) | 📐 | M2 |
| VLM scene reasoning + text summaries (Tier 3) | 📐 | M3 |
| Plain-language policies + compiler (Tier 4) | 📐 | M4 |
| Semantic event search | 📐 | M3 |
| Feedback-driven calibration | 📐 | M4 |
| Trusted-faces suppression (opt-in, local embeddings, [guardrails](docs/faq.md#what-about-face-recognition)) | 📐 | M4 |

### Review & UX
| Capability | Status | Milestone |
|---|---|---|
| Live wall (all cameras, WebRTC via authed WHEP proxy, keyboard-first) | ✅ | M1 |
| Timeline (hour strip, segment playback) + range export | ✅ | M1 |
| Scrub-strip preview scrubbing | 📐 | M1 |
| Event feed with clips, favorites, feedback | 📐 | M2 |
| First-run wizard (admin ✅; add-camera + verify steps 🚧) | 🚧 | M1 |
| PWA + web push | 📐 | M2/M5 |

### Outputs & integrations
| Capability | Status | Milestone |
|---|---|---|
| Signed webhooks (HMAC implemented & tested) | ✅ signing / 📐 delivery | M0/M2 |
| Apprise channels (Telegram, Discord, …) | 📐 | M2 |
| MQTT + Home Assistant discovery | 📐 | M2 |
| REST API: auth, cameras, recordings, streams (WHEP/snapshot), export, system events | ✅ | M1 |
| WebSocket event stream | 📐 | M2 |
| Prometheus metrics | 📐 | M2 |

### Operations & security
| Capability | Status | Milestone |
|---|---|---|
| Config schema + validation (CLI, API) | ✅ | M0 |
| Single-container deploy + go2rtc sidecar compose | ✅ | M0/M1 |
| Forced admin bootstrap, session + scoped API tokens | ✅ | M1 |
| Published container images (ghcr) | 🚧 | M1 release |
| Zero telemetry by default | ✅ (policy & code) | M0 |
| SBOM + signed releases | 🔭 | M5 |

---

## How to influence this roadmap

- **Design feedback** → comment on the Architecture RFC issue or any ADR in
  [docs/architecture/adr](docs/architecture/adr/).
- **Camera demand** → [camera support request](https://github.com/baadev/vidette/issues/new?template=camera_support.yml);
  the backlog is ordered by these.
- **Anything else** → issues, or alex@baadev.com.
