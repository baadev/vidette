<div align="center">

# Vidette

**Self-hosted video security that understands intent — not just motion.**

*A **vidette** was a mounted sentry posted ahead of the picket line — the first to see
trouble coming, and the one trusted to tell a passing farmer from an approaching threat.*

[![Status](https://img.shields.io/badge/status-design_preview_(M0)-8a2be2)](ROADMAP.md)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![CI](https://github.com/baadev/vidette/actions/workflows/ci.yml/badge.svg)](https://github.com/baadev/vidette/actions/workflows/ci.yml)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)

[Why](#why) · [How it works](#how-it-works) · [Quickstart](#quickstart) ·
[Roadmap](ROADMAP.md) · [Cameras](docs/cameras/README.md) · [Architecture](docs/architecture/overview.md)

</div>

> **Status: M1 — streaming & recording, in progress.** What runs today from this repo:
> RTSP ingest through a managed go2rtc gateway, codec-copy segment recording with an SQLite
> index, retention + disk-health janitor, range export (remux), signed-session auth with a
> first-run admin wizard, a WebRTC live wall and an hour-strip review UI with MP4 export.
> M1 closes when the [reference budgets](ROADMAP.md#m1--watch) are measured and published.
> Feedback: [issues](https://github.com/baadev/vidette/issues) · **alex@baadev.com**.

## Why

If you own cloud cameras, you already know the drill:

- The app takes ten seconds to show a live view that is another five seconds stale.
- "Smart alerts" means a ping for every cat, courier and headlight — or silence when it matters.
- Recordings live in someone else's cloud, on their retention terms, behind their paywall.
- No export, no webhooks, no API. Your own footage isn't really yours.
- One camera at a time, in an interface nobody who uses it would have shipped.

The hardware is usually fine. **The software is the product — and you don't own it.**

Vidette is the software half done right, running on your hardware: a universal, self-hosted
video security platform where vendor-specific quirks are just adapters, and where the
notification you get is the one you actually asked for.

## The north star

> You tell Vidette, in your own words:
>
> **"Alert me only when someone looks genuinely interested in getting in. Ignore passers-by."**
>
> And it watches the way a human sentry would: where a person is looking, whether they slow
> down, whether they approach the door or just cut through, whether they glance around, linger,
> reach for a handle or a window — and it stays quiet about everyone else.

That is not a claim of magic — it is a direction we execute toward in the open, tier by tier.
The [AI pipeline design](docs/architecture/ai-pipeline.md) explains exactly how far each
milestone gets, and the [FAQ](docs/faq.md#can-it-really-recognize-intent) gives the honest
answer about what "intent recognition" can and cannot do.

## What Vidette is

- **A universal NVR core.** Any RTSP/ONVIF camera works out of the box — including Eufy
  models with the NAS (RTSP) feature. Closed ecosystems connect through
  [adapters](docs/cameras/README.md) — vendor pain is a plugin, not a fork.
- **An understanding cascade.** Motion → detection → trajectory geometry → vision-language
  reasoning, each tier ~10–100× more expensive and invoked ~10–100× more rarely. Full local
  processing; cloud models strictly opt-in.
- **Plain-language policies.** Describe what matters in a sentence; Vidette compiles it into
  zones, triggers and a scene-level question for the VLM — and shows you the compiled result.
- **First-class outputs.** Signed webhooks with text + snapshot + clip, web push, Telegram /
  Discord / 100+ services via Apprise, MQTT with Home Assistant discovery, one-call clip export.
- **A fast, honest UI.** Multi-camera live wall with sub-second WebRTC, a timeline that scrubs
  like a video editor, event review with one-tap feedback that tunes your alerts.
- **Storage you can trust.** Codec-copy recording (no transcode), retention classes, archive
  compaction, off-site event backup, disk health monitoring. Recording is sacred: analysis
  sheds load first, the recorder last.

Three budgets are treated as product features and tracked per release: **compute, storage,
latency**. See [engineering principles](docs/project/principles.md).

## How it works

```mermaid
flowchart LR
    A[Camera streams] --> B{"Tier 0
motion gate
~free"}
    B -- "quiet (most of the time)" --> Z[Recorded only]
    B -- motion --> C{"Tier 1
object detector
tiny model, low fps"}
    C -- "no person / vehicle" --> Z
    C --> D{"Tier 2
tracking + zone geometry
pure math"}
    D -- "transit through public zone" --> Z
    D -- "approach · dwell · touch" --> E{"Tier 3
VLM scene reasoning
rare, budgeted"}
    E --> F["Tier 4
your policy, in plain language"]
    F -- matches --> G[["Notification
text + snapshot + clip"]]
    F -- otherwise --> Z
```

A single camera produces ~2.6 million frames a day. Asking a vision-language model about every
frame is absurd; asking it about the **ten moments that survived three cheap filters** is not.
That is the whole trick — and it is why Vidette targets small boxes (an N100 mini-PC) rather
than a GPU rack. Details and budgets: [AI pipeline](docs/architecture/ai-pipeline.md).

Recording never depends on analysis. Streams are written to disk codec-copy (no re-encoding)
regardless of what the AI tiers are doing. Details: [storage design](docs/architecture/storage.md).

## A taste of the config

```yaml
cameras:
  front-door:
    adapter: rtsp
    source:
      main: rtsp://cam:${CAM_PASSWORD}@10.0.20.11:554/stream1
      sub:  rtsp://cam:${CAM_PASSWORD}@10.0.20.11:554/stream2
    zones:
      door:   { kind: entry,  points: [[0.42, 0.31], [0.58, 0.31], [0.58, 0.78], [0.42, 0.78]] }
      street: { kind: public, points: [[0.00, 0.80], [1.00, 0.80], [1.00, 1.00], [0.00, 1.00]] }

policies:
  - name: entry-interest
    description: >
      Alert when a person shows interest in entering: approaching the door, lingering,
      touching the door or windows, peering in. Ignore pass-through pedestrians and
      routine deliveries.
    cameras: [front-door]
    sensitivity: balanced
    actions: [notify]
```

And the webhook you receive:

```json
{
  "event": "event.confirmed",
  "id": "01hxv7q8e9",
  "camera": "front-door",
  "started_at": "2026-07-07T21:14:03Z",
  "summary": "A person approached the front door, tried the handle twice, and looked through the side window. No delivery indicators.",
  "intent": { "label": "entry_attempt", "score": 0.87 },
  "policy": "entry-interest",
  "media": {
    "snapshot_url": "https://vidette.local/api/v1/events/01hxv7q8e9/snapshot.webp",
    "clip_url": "https://vidette.local/api/v1/events/01hxv7q8e9/clip.mp4"
  }
}
```

Payloads are HMAC-signed (`X-Vidette-Signature`), so your automations can trust them.
Full spec: [events & automations](docs/events-and-automations.md).

## Feature status

Legend: ✅ shipped · 🚧 in progress · 📐 designed (spec published, RFC open) · 🔭 exploring.
The full inventory lives in [ROADMAP.md](ROADMAP.md).

| Capability | Status | Milestone |
|---|---|---|
| Architecture, config schema, API skeleton, CI | ✅ | M0 |
| RTSP ingest via managed go2rtc, codec-copy recording + SQLite index | ✅ | M1 |
| Auth: first-run admin wizard, sessions, scoped API tokens | ✅ | M1 |
| Retention classes + watermark cleanup + disk health events | ✅ | M1 |
| Multi-camera live wall (WebRTC via authenticated WHEP proxy) | ✅ | M1 |
| Range export (remux, no re-encode) via UI + API | ✅ | M1 |
| Timeline review (hour strip + segment playback; scrub-strip previews pending) | 🚧 | M1 |
| ONVIF discovery, PTZ | 📐 | M1–M2 |
| Detection cascade tiers 0–2 (motion, objects, trajectories, zones) | 📐 | M2 |
| Events, webhooks (signed), web push, Apprise channels | 📐 | M2 |
| MQTT + Home Assistant discovery | 📐 | M2 |
| Eufy via built-in NAS (RTSP), supported models — [guide + caveats](docs/cameras/eufy.md) | 🚧 | M1 |
| VLM scene descriptions + intent scoring (local via Ollama, opt-in cloud) | 📐 | M3 |
| Semantic search over events ("someone touched the gate") | 📐 | M3 |
| Off-site event backup (S3-compatible) | 📐 | M3 |
| Plain-language policies (north star v1) + feedback loop | 📐 | M4 |
| Trusted faces: suppress alerts from your household (opt-in, local-only, [guardrails](docs/faq.md#what-about-face-recognition)) | 📐 | M4 |
| Plugin SDK stability, adapter registry, HA add-on, PWA push polish | 📐 | M5 |

## Camera support

| Ecosystem | Path | Status |
|---|---|---|
| Any RTSP camera | native | 📐 M1 |
| ONVIF (discovery, PTZ, events) | native | 📐 M1–M2 |
| Eufy | built-in NAS (RTSP) — **supported models only**, [check yours](docs/cameras/eufy.md) | 🚧 M1 |
| Reolink, Amcrest/Dahua, Hikvision, Tapo | native RTSP/ONVIF | 📐 M1 |
| UniFi Protect, Ring, Wyze, HomeKit | bridges | 🔭 |

Your camera not here? [Request it](https://github.com/baadev/vidette/issues/new?template=camera_support.yml) —
requests double as the demand map that orders the adapter backlog. Details: [docs/cameras](docs/cameras/README.md).

## Quickstart

> M1 honesty: this records, plays live and exports for RTSP cameras today (built from
> source; published images come with the M1 release). Reference budgets are not yet
> published — treat it as an enthusiastic alpha that never lies about itself.

```bash
git clone https://github.com/baadev/vidette.git && cd vidette
docker compose -f deploy/docker-compose.yml up -d --build
# UI + API → http://localhost:8642
```

Full guide, hardware sizing and first-camera walkthrough: [getting started](docs/getting-started.md).

## The privacy promise

- **Local-first.** Footage, events and models live on your hardware. Nothing leaves unless you
  explicitly configure a destination (a webhook, a cloud VLM, an off-site backup).
- **Zero telemetry by default.** Forever. If opt-in stats ever exist, the code that sends them
  will be readable in this repo.
- **No accounts, no phone-home, no paywalled features in the core.**
- **Behavior first.** Vidette judges *what is happening*, not *who someone is*. The only
  identity feature is opt-in, local-only [trusted faces](docs/faq.md#what-about-face-recognition) —
  suppressing alerts from people *you* enrolled; never stranger identification, never
  third-party face databases, never cloud biometrics.

The threat model and hardening guide live in [security-model.md](docs/architecture/security-model.md).

## Standing on giants

Vidette deliberately reuses the best of the ecosystem instead of rewriting it:
[go2rtc](https://github.com/AlexxIT/go2rtc) (stream gateway),
[FFmpeg](https://ffmpeg.org) (recording/remux),
[ONNX Runtime](https://onnxruntime.ai) (inference everywhere),
[Apprise](https://github.com/caronc/apprise) (100+ notification services),
[SQLite](https://sqlite.org) (+ sqlite-vec for semantic search).
[Frigate](https://github.com/blakeblackshear/frigate) deserves a special mention as prior art —
see [the FAQ](docs/faq.md#how-is-this-different-from-frigate) for an honest comparison.

## FAQ (the three everyone asks)

**How is this different from Frigate?** Frigate is an excellent detection-centric NVR. Vidette
starts one layer higher: scene understanding, plain-language policies, and an event-centric UX —
and it shares ancestry (go2rtc, FFmpeg) rather than competing on decode loops. You can run both.
[Longer answer](docs/faq.md#how-is-this-different-from-frigate).

**Is "intent recognition" real or snake oil?** It is probabilistic. Vidette bounds it with
cheap objective signals (zones, trajectories, dwell) and uses the VLM only as the final judge,
with budgets, calibrated thresholds and your feedback closing the loop. You stay the judge;
Vidette cuts the noise. [Longer answer](docs/faq.md#can-it-really-recognize-intent).

**Are you affiliated with Eufy/Anker?** No. Eufy is the itch that started the project. The
only integration path is the camera's built-in **NAS (RTSP)** feature on supported models —
the community cloud/P2P bridge died with the vendor's API migration, which is
[the thesis in one story](docs/cameras/eufy.md#why-there-is-no-bridge).

## Contributing & contact

Vidette is at the stage where **design review is the most valuable contribution** — read the
[architecture docs](docs/architecture/overview.md), then open an issue with what you'd do
differently. Code contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).
Security reports: [SECURITY.md](SECURITY.md).

- Issues & discussions: [github.com/baadev/vidette](https://github.com/baadev/vidette)
- Email: **alex@baadev.com**

## License

[Apache-2.0](LICENSE) © 2026 Alexander Belov and Vidette contributors.
Camera trademarks belong to their owners; Vidette is not affiliated with any camera vendor.
