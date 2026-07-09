# API

> **Status:** the M1 surface is ✅ live — auth, cameras, recordings, streams, export, system.
> Events (M2) and policies (M4) remain honest `501 {"status": "designed", …}` stubs.
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
| Session cookie (httpOnly, SameSite=Lax, 14 d) | the web UI | ✅ |
| `Authorization: Bearer vd_…` — scoped personal access tokens | automations, scripts | ✅ |

Scopes: `read:events`, `read:streams`, `read:config`, `write:config`, `admin`. Tokens are
created in settings, shown once, revocable, and audit-logged.

## Surface

| Resource | Routes | Milestone |
|---|---|---|
| System | `GET /healthz` (public) · `GET /api/v1/system` · `GET /api/v1/system/events` | ✅ |
| Auth | `GET /api/v1/auth/status` (public) · `POST …/bootstrap` · `POST …/login` · `POST …/logout` · `GET …/me` · `POST/GET/DELETE …/tokens` | ✅ M1 |
| Config | `POST /api/v1/config/validate` | ✅ |
| Managed cameras | `GET/POST /api/v1/config/cameras` · `PUT/DELETE …/{id}` · `POST …/discover` (ONVIF) · `POST …/{id}/probe` — UI-created cameras live in the DB and hot-apply; the YAML file stays the IaC source of truth (id collisions: the file wins) | ✅ |
| Web push | `GET /api/v1/push/vapid-key` · `POST/DELETE /api/v1/push/subscriptions` | ✅ |
| Cameras | `GET /api/v1/cameras` · `GET /api/v1/cameras/{id}` (recorder state, probe, stream readiness) | ✅ M1 |
| Streams | `GET /api/v1/streams/{camera}` · `POST …/whep` (SDP in/out, authenticated proxy to go2rtc) · `GET …/snapshot.jpeg` | ✅ M1 |
| Recordings | `GET /api/v1/recordings?camera=&from_ts=&to_ts=` · `GET …/summary?camera=&day=` · `GET …/segments/{id}/file` | ✅ M1 |
| Export | `POST /api/v1/export` → job · `GET /api/v1/export/{id}` · `GET …/download` | ✅ M1 |
| Events | `GET /api/v1/events?since_ts=&camera=&limit=` · `GET /api/v1/events/{id}` · media: `…/snapshot.jpeg`, `…/clip.mp4` (lazy remux) · `POST …/feedback {verdict: up\|down}` — semantic `q=` search lands in M3 | ✅ |
| Policies | `GET/PUT /api/v1/policies` · `POST /api/v1/policies/{id}/dry-run` | 📐 M4 |
| Live events | `WS /api/v1/ws?topics=event.*,system.*` — JSON frames `{topic, payload}`; closes 4401 unauthenticated, 4400 on invalid topics | ✅ |
| Metrics | `GET /metrics` (Prometheus text; scrape with `Authorization: Bearer vd_…`) | ✅ |

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
