# API

> **Status:** skeleton ✅ (`/healthz`, `/api/v1/system`, `/api/v1/config/validate`); the rest
> 📐 M1–M2. Unimplemented designed routes are mounted **today** and return
> `501 {"status": "designed", "milestone": "..."}` — the API is honest about the roadmap.
> Interactive OpenAPI docs are served at `/api/docs`.

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
| Session cookie (httpOnly, SameSite=Lax) | the web UI | 📐 M1 |
| `Authorization: Bearer <token>` — scoped personal access tokens | automations, scripts | 📐 M1 |

Scopes: `read:events`, `read:streams`, `read:config`, `write:config`, `admin`. Tokens are
created in settings, shown once, revocable, and audit-logged.

## Surface

| Resource | Routes | Milestone |
|---|---|---|
| System | `GET /healthz` · `GET /api/v1/system` | ✅ |
| Config | `POST /api/v1/config/validate` ✅ · `GET/PUT /api/v1/config` + `POST …/apply` 📐 | M0 / M1 |
| Cameras | `GET /api/v1/cameras` · `GET /api/v1/cameras/{id}` (state, health, capabilities) | 📐 M1 |
| Streams | `GET /api/v1/streams/{camera}/live` → WebRTC/MSE negotiation via go2rtc | 📐 M1 |
| Recordings | `GET /api/v1/recordings?camera=&from=&to=` (timeline index) | 📐 M1 |
| Export | `POST /api/v1/export {camera, from, to}` → async job → MP4 (remux) | 📐 M1 |
| Events | `GET /api/v1/events?since=&camera=&kind=&q=` (`q` = semantic+FTS, M3) · `GET /api/v1/events/{id}` · media: `…/clip.mp4`, `…/snapshot.webp` · `POST …/feedback {verdict: up|down}` | 📐 M2 |
| Policies | `GET/PUT /api/v1/policies` · `POST /api/v1/policies/{id}/dry-run` | 📐 M4 |
| Live events | `WS /api/v1/ws` — subscribes to topic patterns (`event.*`, `system.*`) | 📐 M2 |
| Metrics | `GET /metrics` (Prometheus) | 📐 M2 |

### Example: what works today

```bash
curl -s localhost:8642/healthz
# {"status":"ok","version":"0.0.1"}

curl -s localhost:8642/api/v1/system | jq
# version, milestone, designed-feature warnings for the loaded config

curl -s -X POST localhost:8642/api/v1/config/validate \
  --data-binary @config/vidette.yaml -H "content-type: application/yaml" | jq
# {"valid": true, "errors": [], "warnings": ["understanding.vlm: designed — lands in M3", ...]}
```

### Example: designed route honesty

```bash
curl -s localhost:8642/api/v1/events | jq
# {"status": "designed", "milestone": "M2",
#  "docs": "https://github.com/baadev/vidette/blob/main/ROADMAP.md"}   ← HTTP 501
```

## Outbound: webhooks

Vidette also *calls you* — signed webhooks with text + media links per event. Spec, payload
schema and signature verification snippets live in
[events-and-automations.md](events-and-automations.md), so automation authors have one page
to read.
