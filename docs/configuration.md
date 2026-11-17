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
    language: null                # e.g. German; default: the Telegram bot's language
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
  deep:                           # deep analysis: docs/deep.md, ADR-0019
    enabled: false
    backend: local                # local | cloud: one of the endpoints above
    window: "01:00-07:00"         # local time, may wrap midnight; back to back inside it
    outside_window: trickle       # trickle: free moments and turns | off
    trickle_every: 30m            # outside the window: a turn after this long without a clip
    max_clips_per_day: null       # a safety valve; null: no cap
    lookback: 36h                 # events this recent are queued
    frames: 8                     # per clip
    frames_per_call: null         # default: all of them in one look (at most 8)
    max_output_tokens: 600
    language: null                # default: vlm.language
    include_unjudged: true        # also clips the live judge never reached (last)
    min_confidence: 0.6           # below it a verdict counts as uncertain
    slots: check                  # check | ignore: send only when the server has an idle slot
    min_idle_slots: 1
    max_attempts: 3
    label_disputes: true          # disputed and uncertain clips become review drafts
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

### Levels

A policy can grade what it cares about. The judge then names the one scenario that fits
(or `other`), and the scenario's level decides whether the event is an alert and who is
woken ([telegram.md](telegram.md#levels-who-wakes-for-what),
[ADR-0013](architecture/adr/0013-telegram-bot.md)):

```yaml
policies:
  - name: home
    description: A private house with a few cameras around it.
    levels:                       # highest first
      critical:
        title: critical           # shown in notifications (optional)
        wake: always              # always | unless_quiet | unless_known | never
        scenarios:                # snake_case id: what it means (told to the judge)
          touching_equipment: someone touches or opens the electrical panel or a camera
      elevated:
        wake: unless_known        # not when only known people are seen (people.enabled)
        scenarios:
          person_at_house: {title: person at the house, description: a person who stops at the gate}
      none:
        wake: never               # not an alert; the vocabulary for the digest
        scenarios:
          passer_by: walks past without stopping or touching anything, even close to the house
```

Scenario ids are unique across levels; `other`, `unsure`, `unclear` and `unlabelled` are
reserved (`other` is always there: none of the scenarios). `vidette validate` warns when
`unless_known` is used while `people.enabled` is false.

A policy with levels may raise a verdict when the same unknown face keeps coming back
(needs `people.track_unknown`; [people.md](people.md#strangers-who-come-back),
[ADR-0014](architecture/adr/0014-anonymous-identities.md)):

```yaml
    recurrence:
      raise_to: elevated          # a level of this policy
      sightings: 3                # the same anonymous identity this many times...
      within: 24h                 # ...within this window, this time included
      cameras: 2                  # or on this many cameras... (null: off)
      cameras_within: 10m         # ...within this window
      suspicious: true            # an identity marked suspicious raises on any sighting
```

Only a verdict that named a scenario of a lower level is raised; `other`, an abstention
and anything at or above `raise_to` stay as the judge said.

## `calibration`

```yaml
calibration:                      # 👍/👎 change who is woken (docs/calibration.md, ADR-0020)
  enabled: true                   # false: marks only become labels
  marks: 4                        # 👎 for one step, with no 👍 among them
  window: 14d                     # older marks do not count
  bar: 0.9                        # the first step: wakes only from this confidence
  protected_bar: 0.75             # the most a protected scenario's bar can be raised to
  protect: []                     # more scenarios or levels that never leave the alerts
```

Per scenario of a policy with levels, per camera: after `marks` counted 👎 and no 👍, one
step quieter (a confidence bar, then the digest only); a 👍 on an alert a step kept in the
digest takes it back. Every scenario of a `wake: always` level is protected: at most
`protected_bar`, never the digest. Steps live in the database, never in this file;
`/tuning`, the System page and `vidette policy calibration` show and undo them.
`vidette validate` refuses a `protect` name no policy has and a `protected_bar` above
`bar`.

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
  track_unknown: false            # anonymous identities of faces nobody enrolled (ADR-0014):
                                  # biometric data of passers-by, read people.md first
  identities:
    dir: ~/vidette-data/identities  # default $VIDETTE_DATA/identities; never in a git tree
    match: 0.5                    # SFace cosine for a sighting to join an identity
    margin: 0.06                  # ...when no other identity is this close
    min_face_px: 40               # readable faces smaller than this start a weak identity
    near_known: 0.3               # this close to someone enrolled: marked, never raises
    retention_days: 30            # unnamed, unflagged identities go after this long
    merge:                        # the merge pass (ADR-0018): audited, reversible
      auto: true                  # vidette serve runs it after new sightings
      every: 30m                  # at most this often
      clear: 0.5                  # clear faces: best pair at least this...
      clear_floor: 0.4            # ...no pair below this, and the closest by `margin`
      margin: 0.06
      same_moment: 0.85           # any faces this alike, on one camera...
      same_moment_within: 10m     # ...this close in time
  extra_fps: 6                    # a second look around every face found (ADR-0016)
  extra_frames: 24                # at most this many more frames a clip; 0 turns it off
```

Policies are judged live by the verdict worker once a VLM backend is selected
([vlm.md](vlm.md)); without one, incidents stay `observed` and nothing is notified. Measure
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

### `notifications.telegram`

The Telegram bot: alerts with feedback buttons, the day's digest, `/status`, `/live`,
`/quiet` ([telegram.md](telegram.md), [ADR-0013](architecture/adr/0013-telegram-bot.md),
[ADR-0017](architecture/adr/0017-subject-frames-other-cameras-and-questions.md)).

```yaml
notifications:
  telegram:
    enabled: true
    token: ${VIDETTE_TELEGRAM_BOT_TOKEN}       # required, always a ${VAR}
    allowed_chats: ${VIDETTE_TELEGRAM_ALLOWED_CHATS}   # "id,id" or a list; nobody else
    topics: ${VIDETTE_TELEGRAM_TOPICS}         # "<chat>:alerts=<thread>,digest=<thread>;…"
    recipients: all               # all | first (a trial: only the first chat gets alerts)
    language: en                  # en | ru: the bot's messages, and what models write
    names: false                  # names of recognized people in messages (privacy)
    frames: 3                     # frames in an alert's album that show the subject (1 to 9)
    max_video_mb: 20              # bigger clips are linked (clip_url), not sent
    collapse: 5m                  # one camera's alerts within this edit the first one
    digest_at: "21:00"            # the day's digest, local time; null: only on request
    digest_until: "23:30"         # a late or retried digest still goes until then; null: midnight
    quiet_for: 1h                 # /quiet
    late_alerts_for: 2h           # alerts Telegram could not take go late for this long; 0: never
    labels: true                  # answers become reviewed labels in the clip's dataset
    camera_names: {}              # camera → name in messages
    clip_url: null                # default server.base_url
    api_url: https://api.telegram.org
```

### `cross_camera`

The other cameras at the moment of an event ([other-cameras.md](other-cameras.md)).

```yaml
cross_camera:
  before: 15s                     # look this long before the moment a person was first seen
  after: 45s                      # ...and this long after it
  lag_up_to: {}                   # camera → how late its video can arrive, e.g. {porch: 2m}
  max_video_mb: 19                # a longer mosaic is encoded smaller, then cut into parts
```

### `assistant`

Questions in plain words in Telegram, and the same tools over MCP
([assistant.md](assistant.md)).

```yaml
assistant:
  enabled: true
  backend: local                  # local | cloud | none: an understanding.vlm endpoint
  max_calls_per_day: 300
  max_steps: 3                    # tool rounds before it must answer
  timeout_s: 300                  # one model call
```

`backend: cloud` sends questions and what the tools found (never a name, a face or a
frame) to the cloud vendor; `vidette validate` says so.

`vidette validate` warns when `recipients: first` leaves allowed chats without alerts,
when `names: true` sends names to Telegram, and when the removed `digest_every` or
`digest_settle` are still there (they are ignored: there is no hourly digest any more).

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

`vidette serve --env-file PATH` (repeatable) reads `KEY=VALUE` lines before the config
is loaded, for `${VAR}`s such as a bot token: plain assignments, not shell (`;` and `$`
stay as they are), variables already set win, values are never printed, and a file
readable by others is reported.

## `telemetry`

```yaml
telemetry:
  enabled: false                  # default; and there is nothing it would send anyway
```

Exists so the promise is visible in the schema.
