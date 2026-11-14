# Configuration reference

One YAML file: `/config/vidette.yaml`. The schema is executable
(`server/vidette/core/config.py`, pydantic) and this page mirrors it. A test keeps the
[annotated example](../deploy/config.example.yaml) valid against the schema, so example, code
and docs cannot drift apart silently.

This page describes the v0.2 line (intent layer over Frigate,
[ADR-0009](architecture/adr/0009-intent-layer-over-frigate.md)). The v0.1 NVR schema
(`cameras:`, retention, detector, zones) lives at tag `nvr-core`; a v0.1 config fails
validation on v0.2 with a message that says so.

**Validation is always available:**

```bash
vidette validate /config/vidette.yaml          # CLI (inside the container or `uv run`)
POST /api/v1/config/validate                   # API
```

Output separates **errors** (the config cannot load) from **warnings**, including one for
every configured feature that does not work yet. The config never silently lies about what
will happen.

## Conventions

- Keys are `snake_case`. Unknown keys are errors, not silent no-ops.
- Secrets are env references: `${VAR}` (strict: unset variables are an error listing every
  missing name). Never put literal secrets in the file.
- Camera names are Frigate's camera keys (`back_yard`), not their `friendly_name`.

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
  media_dir: /media/vidette   # snapshots and clips cached from Frigate for Vidette's events
  database: /config/vidette.db
```

Vidette records nothing. `media_dir` holds one snapshot per event and clips fetched on
request. Nothing prunes it yet; snapshots add tens of kilobytes per event, clips more.

## `frigate`

```yaml
frigate:
  url: http://frigate:5000        # Frigate's internal API port (required)
  mqtt:                           # the broker Frigate publishes to (required)
    host: mqtt
    port: 1883
    username: null
    password: null                # ${FRIGATE_MQTT_PASSWORD}
    topic_prefix: frigate         # Frigate's mqtt.topic_prefix
  cameras: all                    # or a list of Frigate camera keys
  severities: [alert, detection]  # which review severities become incidents
  tracked_objects: true           # also read <prefix>/events
```

Without this section (and without `clip_folders`) Vidette starts, warns, and receives
nothing. Supported Frigate versions, what to enable in Frigate and network layouts:
[frigate.md](frigate.md).

## `clip_folders`

```yaml
clip_folders:                     # incidents from folders of finished clips (ADR-0010)
  - path: /clips/front-door       # required; read-only is enough
    camera: camera                # event camera when a clip has no vidette-collect sidecar
    poll_every: 10s               # scan interval, at least 1s
    settle: 5s                    # a file must be unchanged this long before it is read
    backfill: false               # true: clips present on the first start count too
```

For cameras Frigate cannot use, such as a battery camera collected with `vidette collect`.
Each clip is one incident; details and limits: [clip-folders.md](clip-folders.md).

## `understanding`

```yaml
understanding:
  vlm:                            # the VLM judge: docs/vlm.md
    backend: none                 # none | local | cloud; the System page can switch it
    frames_per_call: 4            # 1 to 8
    local:                        # an OpenAI-compatible server you run (Ollama default)
      base_url: http://localhost:11434/v1
      model: qwen2.5vl:7b
      api_key: null
      timeout_s: 120
      max_calls_per_day: 2000     # hard budget: the judge abstains past it
      max_calls_per_minute: 20
      max_output_tokens: 400
      max_image_side: 768         # frames are shrunk to this before sending
      prices: {}                  # usd_per_mtok_in/out, usd_per_image, usd_per_call
    cloud:                        # optional; choosing it sends camera frames to the vendor
      preset: openai              # openai | gemini | openrouter | custom (+ base_url)
      base_url: null              # default from the preset
      model: null                 # default from the preset
      api_key: ${OPENAI_API_KEY}  # required, always a ${VAR}
      timeout_s: 45
      max_calls_per_day: 200
      max_calls_per_minute: 10
      prices: {}                  # set them, or cost reports say "unknown"
```

The v0.1 keys `provider` and `send` are rejected with a pointer here. `vidette validate`
warns when a cloud section exists (frames would go to that vendor when it is selected)
and when its prices are missing.

## `policies`

```yaml
policies:
  - name: entry-interest          # [a-z0-9-], unique
    description: >                # plain language: what should alert and what should not
      Alert when a person shows interest in getting in: tries the door handle, lingers at
      the door or windows, peers inside. Ignore people passing by on the street, couriers
      dropping off a parcel, cats and car headlights.
    cameras: all                  # or Frigate camera keys; must be in frigate.cameras when
                                  # that is an explicit list
    sensitivity: balanced         # relaxed | balanced | paranoid
    actions: [notify]
    known_people: judge           # judge | quiet (with people.enabled; docs/people.md)
```

`known_people: quiet` tells the judge that people the local face check recognized as
known never need an alert by themselves; anyone else is judged as usual. `vidette
validate` warns when it is set while `people.enabled` is false.

## `people`

Known people: local face recognition for people who agreed to be enrolled
([people.md](people.md), [ADR-0012](architecture/adr/0012-known-people.md)). Needs
`pip install 'vidette[people]'` and the model files (`vidette people models`).

```yaml
people:
  enabled: false                  # opt-in
  dir: ~/vidette-data/people      # default $VIDETTE_DATA/people, else ~/vidette-data/people;
                                  # refused inside a git work tree
  models_dir: ~/.cache/vidette/models   # default; serve never downloads, the CLI does
  threshold: 0.4                  # SFace cosine similarity for a match (0.2 to 0.95)
  min_face_px: 40                 # smaller faces are "unclear", never "unknown"
  frames_per_second: 2            # sampled from each clip at full resolution
  max_frames: 24
  max_faces_per_person: 60
```

Policies are stored and validated today but **not evaluated**: incidents become
`observed` events and nothing is notified until the verdict worker lands (week 2). Measure
a policy offline before trusting it: [eval.md](eval.md).

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

`event.observed` (an incident without a verdict) never reaches a channel, even under
`when: event.*`. Delivery contract and signature verification:
[events-and-automations.md](events-and-automations.md).

## `integrations`

```yaml
integrations:
  mqtt:                           # Vidette's own outbound MQTT
    enabled: false
    host: mqtt
    port: 1883
    username: null
    password: null                # ${MQTT_PASSWORD}
    topic_prefix: vidette
    discovery: true               # Home Assistant MQTT discovery
```

This is separate from `frigate.mqtt` on purpose, even when both point at the same broker.
Discovery announces the cameras in `frigate.cameras` on connect; with `cameras: all`, each
camera is announced the first time an event for it arrives.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VIDETTE_CONFIG` | `/config/vidette.yaml` | config file path (missing file: starts with defaults and a warning) |
| `VIDETTE_WEB_DIST` | `/app/web-dist` | built web app directory |

## `telemetry`

```yaml
telemetry:
  enabled: false                  # default; and there is nothing it would send anyway
```

Exists so the promise is visible in the schema.
