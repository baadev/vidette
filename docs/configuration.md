# Configuration reference

One YAML file: `/config/vidette.yaml`. The schema is executable
(`server/vidette/core/config.py`, pydantic) and this page mirrors it — a test keeps the
[annotated example](../deploy/config.example.yaml) valid against the schema, so example, code
and docs cannot drift apart silently.

**Validation is always available**, even for design-stage features:

```bash
vidette validate /config/vidette.yaml          # CLI (inside the container or `uv run`)
POST /api/v1/config/validate                   # API
```

Output separates **errors** (config cannot load) from **warnings** — including a warning for
every configured feature that is still 📐 design-stage, with its milestone. The config never
silently lies about what will happen.

## Conventions

- Keys are `snake_case`.
- Durations: `"90s"`, `"30m"`, `"12h"`, `"3d"`, or `"forever"`.
- Secrets are env references: `${VAR}` (strict — unset vars are an error listing every
  missing name). Never put literal secrets in the file.
- Zone points are normalized `[x, y]` in `0.0–1.0` — resolution-independent.

## `server`

```yaml
server:
  host: 0.0.0.0
  port: 8642
  base_url: null          # public URL used in notification links, e.g. https://vidette.example.com
  auth:
    mode: builtin         # builtin | none  (none = kiosk LANs only; loud permanent warning)
```

No credentials live here: the first run forces admin creation
([security model](architecture/security-model.md)).

## `storage`

```yaml
storage:
  media_dir: /media/vidette
  database: /config/vidette.db
  retention:              # per-class; per-camera overridable
    continuous: 3d
    motion: 14d
    events: 90d
    favorites: forever
  compaction:             # 📐 M3 — archive re-encode of cold continuous footage
    enabled: false
    after: 7d
    codec: hevc           # hevc | av1
  offsite:                # 📐 M3 — S3-compatible event backup
    enabled: false
```

Semantics: [storage design](architecture/storage.md).

## `cameras`

```yaml
cameras:
  front-door:                     # id: [a-z0-9-], stable, used in URLs/topics
    adapter: rtsp                 # rtsp (✅) | onvif (📐 M1) — Eufy uses rtsp via NAS (RTSP)
    name: "Front door"            # display name; defaults to id
    source:                       # adapter-specific for bridge adapters (see their docs)
      main: rtsp://user:${CAM_PASSWORD}@10.0.20.11:554/stream1
      sub:  rtsp://user:${CAM_PASSWORD}@10.0.20.11:554/stream2
    options: {}                   # adapter-specific extras (reserved for bridge adapters)
    record:
      mode: continuous            # continuous | motion | events | off
      retention: null             # override global storage.retention for this camera
    detect:
      enabled: true
      fps: 5                      # Tier 1 cadence during motion
      resolution: 720             # analysis substream height
    understand: true              # eligible for Tier 3 (VLM) promotion
    zones:
      door:   { kind: entry,  points: [[0.42, 0.31], [0.58, 0.31], [0.58, 0.78], [0.42, 0.78]] }
      street: { kind: public, points: [[0.0, 0.8], [1.0, 0.8], [1.0, 1.0], [0.0, 1.0]] }
```

Zone kinds and their semantics (`entry`, `object`, `private`, `public`) are defined in the
[cascade doc](architecture/ai-pipeline.md#tier-2--trajectory-geometry). Draw them in the
web app's zone editor: for UI-managed cameras it saves directly; for file-defined cameras it
produces a YAML snippet to paste here — Vidette never rewrites your config file.

### Cameras from the UI (managed cameras)

Cameras created on the web app's **Cameras** page live in the database, not in this file.
The merge rules, chosen so both audiences win:

- This YAML file is the infrastructure-as-code source of truth — the UI **never edits it**.
- UI-managed cameras are merged in at boot and hot-applied on change (capture restarts for
  a few seconds when the camera set changes).
- An id defined in both places resolves to the file, with a loud warning; delete the UI
  copy to silence it.
- `GET /api/v1/config/cameras` shows every camera with its origin (`file` / `managed`).

## `understanding`

```yaml
understanding:
  detector:
    model: auto                   # auto picks by hardware; or explicit model id
    hardware: auto                # auto | cpu | cuda | openvino | coreml | hailo | coral
  tracker:
    engine: bytetrack
  vlm:                            # 📐 M3
    provider: none                # none | ollama | llama-cpp | openai | anthropic | google
    model: null                   # e.g. qwen2.5-vl:7b
    base_url: null                # e.g. http://ollama:11434
    api_key: null                 # cloud providers: ${PROVIDER_KEY}
    max_calls_per_minute: 6       # hard budget; overflow degrades to Tier 2 alerts
    send: keyframes               # keyframes | crops — what leaves the box for cloud providers
  faces:                          # 📐 M4 — trusted-faces suppression, opt-in & local-only
    enabled: false                # enrollment happens in the UI; biometrics never live in YAML
    min_confidence: 0.8           # below this, a match never suppresses (fail toward alerting)
  embeddings:                     # 📐 M3 — semantic search
    enabled: false
    model: siglip2-base
```

Trusted-faces guardrails (suppression-only, local-only, fail-toward-alerting) are product
promises — see the [FAQ](faq.md#what-about-face-recognition) and
[cascade design](architecture/ai-pipeline.md#trusted-faces-suppression--m4).

## `policies`

```yaml
policies:
  - name: entry-interest
    description: >
      Alert when a person shows interest in entering: approaching the door, lingering,
      touching the door or windows, peering in. Ignore pass-through pedestrians and
      routine deliveries.
    cameras: [front-door]         # or "all"
    sensitivity: balanced         # relaxed | balanced | paranoid
    ignore_trusted: true          # default: skip alerts for enrolled trusted faces (M4)
    actions: [notify]
```

`description` is the plain-language policy (Tier 4, 📐 M4). Until M4, policies fall back to
their geometric skeleton (zone + trigger heuristics per sensitivity preset) — configuring
them early is not a no-op, and `vidette validate` tells you exactly which interpretation is
active.

## `notifications`

```yaml
notifications:
  channels:
    push:     { kind: webpush }
    telegram: { kind: apprise, url: "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" }
    hooks:
      kind: webhook
      url: https://automation.example.com/vidette
      secret: ${VIDETTE_WEBHOOK_SECRET}
      include: [summary, snapshot_url, clip_url]
  rules:
    - when: event.confirmed       # patterns: event.*, system.*, exact types
      channels: [push, telegram, hooks]
    - when: system.*
      channels: [hooks]
```

Full delivery contract and signature verification:
[events-and-automations.md](events-and-automations.md).

## `integrations`

```yaml
integrations:
  mqtt:
    enabled: false
    host: mqtt.local
    port: 1883
    username: null
    password: null                # ${MQTT_PASSWORD}
    topic_prefix: vidette
    discovery: true               # Home Assistant MQTT discovery
```

## Environment variables

Deployment-level knobs live in the environment, not the YAML (they describe *where things
run*, not *what to do*):

| Variable | Default | Purpose |
|---|---|---|
| `VIDETTE_CONFIG` | `/config/vidette.yaml` | config file path (missing file → wizard mode) |
| `VIDETTE_GO2RTC_URL` | `http://go2rtc:1984` | gateway admin API (compose-internal) |
| `VIDETTE_GO2RTC_RTSP` | `rtsp://go2rtc:8554` | gateway restream base the recorder reads from |
| `VIDETTE_GO2RTC_CONF` | next to the database | where the generated go2rtc config is written |
| `VIDETTE_WEB_DIST` | `/app/web-dist` | built web app directory |

## `telemetry`

```yaml
telemetry:
  enabled: false                  # default; and there is nothing it would send today anyway
```

Exists so the promise is visible in the schema. If opt-in stats ever ship, the exact payload
will be documented here and readable in the source first.
