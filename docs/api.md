# API

> **Status (v0.2):** auth, events (with Frigate snapshots and clips), push, WebSocket,
> metrics and system are live. The v0.1 camera, stream, recording and export routes left with
> the NVR core (tag `nvr-core`, [ADR-0009](architecture/adr/0009-intent-layer-over-frigate.md))
> and now answer 404. Policies remain an honest `501 {"status": "designed", …}` stub until
> the verdict worker lands. Interactive OpenAPI docs are served at `/api/docs`.

## Principles

- REST + JSON; WebSocket for live event flow. `Content-Type: application/json; charset=utf-8`.
- Versioned base path `/api/v1`; additive changes only within a version.
- Timestamps: ISO 8601, UTC, milliseconds (`2026-07-07T21:14:03.412Z`).
- IDs: opaque strings, lexicographically time-sortable.
- Pagination: cursor-based (`?cursor=`, `limit` ≤ 200; response carries `next_cursor`).
- Errors: RFC 7807 problem+json (`type`, `title`, `status`, `detail`, `instance`) — and
  `detail` always says what to do next.
- Everything the UI can do, the API can do — the UI is an API client with no private routes.

## Authentication

| Method | Use | Status |
|---|---|---|
| Session cookie (httpOnly, SameSite=Lax, 14 d) | the web UI | ✅ |
| `Authorization: Bearer vd_…` — scoped personal access tokens | automations, scripts | ✅ |

Scopes: `read:events`, `read:streams`, `read:config`, `write:config`, `admin`. Tokens are
created in settings, shown once, revocable, and audit-logged.

## Surface

| Resource | Routes | Milestone |
|---|---|---|
| System | `GET /healthz` (public) · `GET /api/v1/system` (stage, config warnings, Frigate source and clip folder state) · `GET /api/v1/system/events` | ✅ |
| Settings | `GET /api/v1/settings/vlm` (read:config): the chosen VLM backend, each option's model, endpoint, where frames go and today's calls against the budget, the verdict worker's counters · `PUT /api/v1/settings/vlm {backend: none\|local\|cloud}` (write:config): stored, wins over the config; 409 with what to add when the backend is not configured; recorded as `vlm.backend_changed` | ✅ |
| Auth | `GET /api/v1/auth/status` (public) · `POST …/bootstrap` · `POST …/login` · `POST …/logout` · `GET …/me` · `POST/GET/DELETE …/tokens` | ✅ M1 |
| Config | `POST /api/v1/config/validate` | ✅ |
| Web push | `GET /api/v1/push/vapid-key` · `POST/DELETE /api/v1/push/subscriptions` | ✅ |
| Cameras, streams, recordings, export, managed cameras | v0.1 only (tag `nvr-core`) | ❌ on v0.2 |
| Events | `GET /api/v1/events?since_ts=&camera=&favorite=&limit=` · `GET /api/v1/events/{id}` (with `source`, `upstream`) · media: `…/snapshot.jpeg` (cached from Frigate or a clip folder's key frame), `…/clip.mp4` (fetched from Frigate's recordings once and cached, or served from the clip folder) · `POST …/feedback {verdict: up\|down}` · `POST …/favorite {favorite}` | ✅ |
| People | `GET /api/v1/people` (read:config): state, enrolled people, their reference faces · `POST /api/v1/people {name}` · `DELETE /api/v1/people/{id}` · `POST /api/v1/people/{id}/photos` (raw JPEG/PNG/WebP body, ≤ 15 MB; every readable face is enrolled) · `GET …/faces/{face}.jpg` · `DELETE …/faces/{face}` · on events: `people` in `GET /api/v1/events/{id}`, `GET /api/v1/events/{id}/people/{n}.jpg`, `POST /api/v1/events/{id}/people/{n} {answer: name\|stranger\|unsure}`. Changes need write:config; 409 while `people.enabled` is false ([people.md](people.md)) | ✅ |
| Policies | `GET/PUT /api/v1/policies` · `POST /api/v1/policies/{id}/dry-run` | 📐 v0.2 week 2 (policies live in the config file; they are judged live already) |
| Live events | `WS /api/v1/ws?topics=event.*,system.*`: JSON frames `{topic, payload}` (including `event.observed`); closes 4401 unauthenticated, 4400 on invalid topics | ✅ |
| Metrics | `GET /metrics` (Prometheus text; scrape with `Authorization: Bearer vd_…`) | ✅ |

### Example: what works today

```bash
curl -s localhost:8642/healthz
# {"status":"ok","version":"0.0.1"}

curl -s localhost:8642/api/v1/system | jq
# version, milestone, designed-feature warnings for the loaded config

curl -s -X POST localhost:8642/api/v1/config/validate \
  --data-binary @config/vidette.yaml -H "content-type: application/yaml" | jq
# {"valid": true, "errors": [], "warnings": ["policies: understanding.vlm.backend is none, ...", ...]}
```

### Example: designed route honesty

```bash
curl -s localhost:8642/api/v1/policies | jq
# {"status": "designed", "milestone": "v0.2",
#  "docs": "https://github.com/baadev/vidette/blob/main/ROADMAP.md"}   ← HTTP 501
```

## Outbound: webhooks

Vidette also *calls you* — signed webhooks with text + media links per event. Spec, payload
schema and signature verification snippets live in
[events-and-automations.md](events-and-automations.md), so automation authors have one page
to read.
