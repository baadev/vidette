# FAQ

## How is this different from Frigate?

[Frigate](https://github.com/blakeblackshear/frigate) is an excellent, mature, detection-centric
NVR — if you want real-time object detection with deep Home Assistant integration today, use
it, and we mean that.

Vidette starts one layer higher and makes different bets:

| | Frigate's center of gravity | Vidette's center of gravity |
|---|---|---|
| Core question | *what objects are in frame?* | *what is happening, and do you care?* |
| Alerting model | objects/zones (+ GenAI descriptions as an add-on) | the cascade: geometry + VLM verdicts fused into intent, plain-language policies as the primary UX |
| Closed ecosystems | not the focus | docs-as-product rescue guides (e.g. [Eufy via NAS/RTSP](cameras/eufy.md)) + a sidecar adapter pattern for bridgeable ones |
| Event outputs | strong HA/MQTT story | equal-first: signed webhooks with text+media, Apprise, HA/MQTT |
| Storage stance | recording + retention | + compaction tiers, off-site event backup, loud reliability monitoring |

We share ancestry rather than competing on plumbing: go2rtc, FFmpeg, ONNX-based detection.
You can run both side by side off the same go2rtc restreams while evaluating. If Vidette's
semantic layer is the only thing you want, tell us — a "Frigate companion mode" has come up
and is genuinely on the table (🔭).

## Can it really recognize intent?

Honest answer: **nothing recognizes intent with certainty — including humans.** What a good
human sentry does is read *behavioral evidence*: trajectory, attention, dwell, interaction.
Vidette operationalizes exactly that, in three bounded layers:

1. **Deterministic geometry** (Tier 2): approached the door vs. passed by; lingered; touched;
   returned three times. Cheap, objective, explainable — and already most of the value.
2. **Scene reasoning** (Tier 3): a VLM answers a *structured* question about selected frames
   ("is this person interacting with the door? delivery indicators?"), producing a score, not
   a verdict of guilt.
3. **Your threshold** (Tier 4 + feedback): sensitivity presets and 👍/👎 calibration decide
   what's worth your attention. You stay the judge; Vidette cuts the noise.

It will have false positives and misses; every release publishes accuracy against a public
clip set so you can see the error rates instead of trusting adjectives
([evaluation](architecture/ai-pipeline.md#evaluation-or-how-we-avoid-lying-to-ourselves)).

## What about face recognition?

Vidette ships one identity feature, scoped tightly: **trusted faces** (📐 M4) — opt-in,
local-only matching whose sole job is *suppressing* alerts caused by people you enrolled
(household members, regular visitors). "The system shouldn't page me about my own family
taking out the trash" is a legitimate ask that zones and schedules alone can't express.

The guardrails are product promises, not implementation details:

- **Suppression-only.** A match quiets alerts; it never powers "identify the stranger"
  features. An unknown face stays "a person" — judged by behavior, like everything else.
- **Local-only.** Embeddings are computed and stored on your box, encrypted at rest,
  deletable in one click; enrollment happens in the UI with the person's consent. No cloud
  biometrics, no third-party identity databases, ever.
- **Fail toward alerting.** An uncertain match never suppresses — a burglar who vaguely
  resembles your cousin still triggers the alarm.
- **Cloud VLMs never receive identity tasks.** Even if you opt into a cloud provider for
  scene reasoning, face matching runs locally, always.

Legal note: biometric data is regulated in many jurisdictions even for home use — enroll
only people who agreed, and check local rules if your cameras see public space. See also
[principles](project/principles.md#4-behavior-first-identity-only-by-consent).

## Do I need a GPU? Do I need any LLM at all?

No and no. Tiers 0–2 are deterministic and run on a $150 mini-PC; "person approached the door
and dwelled" alerts work with zero AI-model calls. The VLM tier is optional enrichment —
local via Ollama if you have the hardware, cloud if you opt in, off if you prefer. The system
is designed to *degrade gracefully*, not to hold your security hostage to a model.

## Is my footage sent anywhere?

Never by default. Destinations exist only where you configure them: your webhook, your MQTT
broker, an opt-in cloud VLM (selected keyframes only), an opt-in off-site backup. Zero
telemetry by default, forever — see the [privacy promise](../README.md#the-privacy-promise)
and [security model](architecture/security-model.md).

## Are you affiliated with Eufy or Anker?

No. Eufy is simply the itch that started the project — well-regarded hardware behind
frustrating software. The only integration path is the camera's built-in **NAS (RTSP)**
feature, available on supported models — a plain local stream, no cloud in the path
([guide](cameras/eufy.md)). The community's reverse-engineered cloud/P2P bridge
(bropat's heroic `eufy-security-client`) stopped being viable when Anker migrated its
backend and began shutting the legacy API — which is, of course,
[the argument for owning your recording layer](cameras/eufy.md#why-there-is-no-bridge)
in the first place. All trademarks belong to their owners.

## What about Scrypted? ZoneMinder? Shinobi?

Scrypted is a superb camera *hub* (plugins, HomeKit bridging) with an NVR bolted on and a
partly commercial model; ZoneMinder and Shinobi are the previous generations of open NVRs.
None center on scene understanding + plain-language policies + efficiency budgets. If one of
them already solves your problem, use it — the space is better with all of us in it.

## Why Python? Won't it be slow?

The hot paths aren't Python: video is written by FFmpeg (codec-copy), streamed by go2rtc (Go),
inferred by ONNX Runtime / llama.cpp (C++). Python orchestrates — and buys us the entire CV/ML
ecosystem and the largest contributor pool. Where orchestration itself gets hot, we go
process-parallel before we go rewrite-the-world. [ADR-0001](architecture/adr/0001-runtime-and-languages.md).

## Why Apache-2.0 and not MIT/AGPL?

Apache-2.0 = permissive adoption **plus an explicit patent grant** — relevant for an AI
project. AGPL would deter the integrations we want (and self-hosted NVRs have little
SaaS-clone risk to defend against anyway). We additionally refuse AGPL *dependencies* in the
default install so downstream users inherit no surprises.
[ADR-0006](architecture/adr/0006-license.md).

## Remote access from my phone?

Tailscale/WireGuard first (10 minutes, zero exposed ports), reverse proxy + TLS if you know
what you're doing, never raw port-forwarding. Vidette ships no cloud relay; if one ever
exists it will be optional and separate. iOS web-push requires installing the PWA to the home
screen — a real platform limitation we document instead of hiding.

## Can I trust a pre-1.0 security product?

Trust the posture, verify the claims: forced admin bootstrap, signed webhooks, no telemetry,
lockfiles, non-root containers — all inspectable in this repo, and
[SECURITY.md](../SECURITY.md) invites you to break them. Then again: the incumbent
alternative is a closed cloud app. Pick your risk.
