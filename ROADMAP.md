# Roadmap

This file is the single source of truth for **what exists, what is being built, and what is
designed but not started**. It is updated in the same change as the code it describes.

**Legend**, used consistently across the repo:

| Mark | Meaning |
|---|---|
| ✅ shipped | Merged on the line it names, tested, documented. You can use it today. |
| 🚧 in progress | Actively being built. |
| 📐 designed | Specification published (docs/ADR), code not started. |
| 🔭 exploring | Direction we believe in; no committed design yet. |
| ❌ out of scope | Deliberately not planned, with a reason. |

We promise **sequence, not dates**, with one exception: the two-week plan below, which the
maintainer set for himself on 2026-11-14.

---

## The turn (2026-11-14)

v0.1 built an NVR: recording, live view, a detection cascade, ONVIF. Frigate already does all
of that, better and on more hardware. The differentiator, plain-language policies judged by
a VLM, had no code, and nothing measured whether alerts were any good. Direction since then:
**Vidette is an intent layer on top of Frigate**
([ADR-0009](docs/architecture/adr/0009-intent-layer-over-frigate.md)). The NVR is frozen at
tag `nvr-core` (= v0.1.3); its inventory is at the bottom of this file.

## v0.2: intent layer over Frigate *(current, branch `v0.2`)*

**Done when:** one service in compose next to a working Frigate; a policy written in plain
words decides which review items notify; `vidette eval` reports its precision, recall,
false alarms per day and VLM cost on a labelled set of real clips; the README carries those
numbers with the set's size, and nothing better than them.

### Week 1: foundation and the meter

| Item | Status |
|---|---|
| Freeze the NVR core (tag `nvr-core`, deleted on `v0.2`), ADR-0009 | ✅ |
| CI from a lockfile (`uv.lock`, `uv sync --locked`), node24 actions; ruff, mypy strict, pytest green | ✅ locally (Python 3.12 and 3.13) and on GitHub |
| Frigate source: reviews, tracked objects, availability over MQTT; typed, lenient, filterable | ✅ |
| Frigate HTTP client: review items, snapshots, recording frames, clips | ✅ |
| Fixtures in recorded format + `vidette frigate check / tail --record / replay` | ✅ fixtures hand-built from Frigate 0.18 docs; real captures pending |
| Incidents become `observed` events (snapshot, clip, Frigate's severity and summary); never notify | ✅ |
| Judge interface + model-free baselines (`always`, `never`, `frigate-severity`, `frigate-genai`) | ✅ |
| `vidette eval`: dataset format, Wilson intervals, per-day rates, cost projection, `--scaffold` | ✅ |
| Labelled set of 30 to 50 real clips (passer-by, courier, door handle, lingering, cat, headlights, night noise) | 🚧 being collected by `vidette collect`; synthetic 5-entry sample ships as a format demo |
| `vidette collect`: wake periods of a battery camera become dataset clips (gentle wait loop, clean stops, hard disk caps, launchd template) | ✅ tested against a fake RTSP camera; run against real battery cameras |
| `vidette collect --continuous`: a bounded ring and a motion gate cut clips with pre-roll from a streaming camera; `--rotate`, `--base-station` ([ADR-0011](docs/architecture/adr/0011-continuous-collect.md)) | ✅ tested against a fake continuous camera; run against a real camera ([measured](docs/collect.md#measured-on-a-real-camera)) |
| `vidette collect --prune-oldest`: the oldest clips nothing needs make room under the folder cap; labels, alerts and marks, faces, the other cameras of an alert and the last 24 h are kept ([docs](docs/collect.md#pruning-the-oldest-clips---prune-oldest)) | ✅ tested on temporary datasets against the real database schemas; dry-run on real datasets |
| Clip folders as an incident source: the pipeline without Frigate ([ADR-0010](docs/architecture/adr/0010-clip-folders.md)) | ✅ |

### Week 2: the differentiator

| Item | Status |
|---|---|
| VLM judge: frame selection, structured JSON verdict, abstention, daily call budget, pace, timeouts, latency and cost; one client for OpenAI-compatible APIs with a `local` (Ollama) and a `cloud` (OpenAI, Gemini, OpenRouter, custom) backend ([docs/vlm.md](docs/vlm.md)) | ✅ tested on a fake OpenAI-compatible server; running live on a local model on a test deployment; not measured yet |
| `vidette eval` compares judges side by side, VLM included: precision, recall, false alarms/day, abstentions by reason, measured cost and latency | ✅ |
| Machine drafts in `labels.yaml` are not truth until reviewed (`status: draft`); reports count accepted-unchanged drafts | ✅ |
| `vidette label draft / sheets / apply`: VLM drafts from the set's vocabulary, PNG contact sheets for a phone, replies by number in Russian or English ([docs/label.md](docs/label.md)) | ✅ tested with a fake model; used with a local model on real clips |
| Known people: local face recognition (YuNet + SFace, opt-in extra), one line of facts for the judge, `known_people: quiet` on a policy, People page, confirmations from the event card, `vidette people` and contact-sheet replies ([docs/people.md](docs/people.md), [ADR-0012](docs/architecture/adr/0012-known-people.md)) | 🚧 works end to end with a fake engine in tests and on real clips; no accuracy figure yet; encryption at rest not done |
| Anonymous identities (opt-in `people.track_unknown`): everyone whose face is found gets one (weak ones for small faces), counted when they come back, never named; the sharpest moment of each face; answers that can be taken back; familiar people quiet like the household; recurrence facts for the judge, a `recurrence` rule that raises a policy's level, an Identities view (suggested duplicates, bulk merge, move a sighting, promote and take back), «Кто это?» and «незнакомец №12, 3-й раз» in Telegram, 30-day retention, `vidette people identities backfill` ([docs/people.md](docs/people.md#strangers-who-come-back), [ADR-0014](docs/architecture/adr/0014-anonymous-identities.md), [ADR-0016](docs/architecture/adr/0016-every-face-reversible-decisions.md)) | 🚧 tested with a fake engine; backfilled over real clips; thresholds from those clips, no accuracy figure |
| Policy levels: scenarios grouped by severity, each level with a `wake` rule (always, unless quiet, unless only known people, never); the judge names a scenario ([docs/telegram.md](docs/telegram.md#levels-who-wakes-for-what), [ADR-0013](docs/architecture/adr/0013-telegram-bot.md)) | 🚧 tested with a fake model; running live; not measured by `vidette eval` yet |
| Policy in plain words → inspectable `PolicySpec` shown back to the person | 📐 (levels are written by hand; no compiler) |
| Identity merge pass: identities that are certainly one person (the same moment on one camera, or clear faces that match unambiguously) joined after the fact, never across a person's decisions, every merge audited and reversible, `vidette people merge`, run by serve after new sightings ([ADR-0018](docs/architecture/adr/0018-identity-merge-pass.md)) | 🚧 tested with fake faces; run on a real store (572 → 565); thresholds from that store's score distribution, no accuracy figure |
| Deep analysis: a second look at the day's clips with people or vehicles by the judge's model (look at up to 8 frames, then decide from the description), next to the live verdict; disputes become review drafts; the evening digest quotes it; one shared slot with live calls first, `/slots` back-off, night window, daily cap, persistent queue ([docs/deep.md](docs/deep.md), [ADR-0019](docs/architecture/adr/0019-deep-analysis.md)) | 🚧 tested against a fake model server; run on a test deployment; not measured against the live judge |
| Verdicts move events from `observed` to `confirmed` / `dismissed`; only confirmed ones notify (verdict worker: recent closed incidents only, one at a time, sensitivity as a confidence floor) | ✅ tested with a fake model |
| Backend switch Off / Local / Cloud on the System page, stored, with where the frames go and today's usage; Cloud asks before it is stored | ✅ |
| Dry run: replay the last N days of Frigate review items through a policy ("this is what would have fired") | 📐 |
| Telegram bot in English (default) or Russian (`language: ru`), every phrase in one catalog per language: alerts with frames, clip and the verdict's reason; 👍/👎 (with a reason), "This is …", "?" become feedback and reviewed dataset labels, one button per answer that takes it back; the album shows the subject; the day's digest in the evening when something needs a look; `/status` `/live` `/quiet` ([docs/telegram.md](docs/telegram.md), [ADR-0013](docs/architecture/adr/0013-telegram-bot.md), [ADR-0017](docs/architecture/adr/0017-subject-frames-other-cameras-and-questions.md)) | 🚧 tested against a fake Telegram API; running live on a test deployment |
| The other cameras at an event's moment: every clip around it on every clip-folder camera, the same person only by face, approximate time for cameras whose video arrives late, one mosaic video; in the event card and «📹 Другие камеры» ([docs/other-cameras.md](docs/other-cameras.md), [ADR-0017](docs/architecture/adr/0017-subject-frames-other-cameras-and-questions.md)) | 🚧 tested; stitched on real clips of a test deployment; Frigate events have no neighbours yet; the on-screen clock is not read (📐) |
| Questions in plain words to the bot (tools over events, clips and identities; the local model by default, quick answers for the commonest), and the same read-only tools over MCP ([docs/assistant.md](docs/assistant.md)) | 🚧 tested against fakes; run against the real local model; answers take minutes on a shared model |
| HA sensor via MQTT | 📐 (delivery plumbing ✅) |
| `vidette eval` on what a running service said: `stored-live` / `stored-deep` / `stored-best` from its database (no model call), a service's policy as ground truth (`--policy-from-config`), several datasets as one, false alarms and misses per day ([docs/eval.md](docs/eval.md#scoring-what-the-service-already-said)) | ✅ tested on temporary databases; run on a test deployment |
| First published numbers: `vidette eval` on the real set, VLM judge vs the baselines | 🚧 first numbers on a test deployment's reviewed set, 32 clips with 3 positives ([docs/eval.md](docs/eval.md#first-numbers-on-a-test-deployment)); too few positives for a recall figure |
| Release v0.2.0 with its own images (v0.1.x images untouched) | 📐 |

## Later

| Item | Status |
|---|---|
| Calibration: 👍/👎 move a scenario on a camera a step quieter (a confidence bar, then the digest) or back, never past the floor of critical scenarios; said in one line with Undo, `/tuning`, the System page, `vidette policy calibration` ([docs/calibration.md](docs/calibration.md), [ADR-0020](docs/architecture/adr/0020-calibration-from-marks.md)) | ✅ tested against a fake Telegram API and a temporary database |
| Geometry from Frigate's tracked objects (approached, lingered, touched) as a cheap pre-filter before the VLM | 🔭 |
| Snapshot and clip cache pruning in `media_dir` | 📐 |
| Frigate's authenticated port (8971) | 🔭 |
| Other incident sources (Scrypted, Home Assistant camera events); clip folders shipped ([ADR-0010](docs/architecture/adr/0010-clip-folders.md)) | 🔭 |
| Home Assistant add-on packaging | 🔭 |
| Contribute the policy layer upstream to Frigate, if it fits there | 🔭 |

## Out of scope on this line

| Item | Why |
|---|---|
| Recording, live view, timeline, export | Frigate does it ([ADR-0009](docs/architecture/adr/0009-intent-layer-over-frigate.md)) |
| Own detector, tracker, motion gate | Frigate does it |
| Camera adapters (RTSP, ONVIF, Eufy) | Frigate and go2rtc do it |
| Semantic search over events | Frigate does it (0.14+) |
| Face recognition for cameras under Frigate | Frigate does it (0.16+); Vidette may use its sub-labels. For clip folders, known people ([ADR-0012](docs/architecture/adr/0012-known-people.md)) |
| Naming people nobody enrolled | Never: an unknown face is at most a number a household counts locally ([ADR-0014](docs/architecture/adr/0014-anonymous-identities.md)); no name from any source, no third-party identity data, no appearance attributes ([principles](docs/project/principles.md)) |

---

## Frozen at tag `nvr-core` (v0.1.3)

Shipped on v0.1 and kept intact at the tag, not maintained on `v0.2`: go2rtc lifecycle
management and keep-warm holders; RTSP adapter and ONVIF discovery (beta); codec-copy fMP4
recorder with SQLite index; retention classes and watermark cleanup; live wall (WebRTC/MSE);
timeline with scrub previews; range export; motion gate, YOLOX-tiny detector (ONNX Runtime),
IoU tracker with zone semantics and passer-by suppression; managed cameras and zone editor in
the UI; first-run camera wizard. Field notes from that line (Eufy single-client RTSP,
battery cameras) stay in [docs/cameras/eufy.md](docs/cameras/eufy.md). Never done there: the
N100 reference run and the 7-day soak.

## How to influence this roadmap

- **Design feedback**: an issue, or a comment on any ADR in
  [docs/architecture/adr](docs/architecture/adr/).
- **A clip set or a Frigate recording that breaks the parser**: the two most useful things
  anyone can send right now.
- **Anything else**: issues, or alex@baadev.com.
