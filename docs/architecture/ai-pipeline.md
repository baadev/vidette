# The understanding cascade

> **Status: 📐 designed.** Tiers 0–2 land in M2, Tier 3 in M3, Tier 4 in M4. Protocols are
> implemented and typed in `server/vidette/pipeline/base.py`. All numbers below are **design
> targets**, not measurements; the M2 milestone ships a public reference clip set and an
> accuracy harness so every number becomes a measurement.

## The problem, stated honestly

A 1080p camera produces ~2.6 million frames per day. The user wants one notification:
*"someone is showing interest in my door."* Every architecture in this space is a strategy for
throwing away 99.999 % of frames as cheaply as possible while keeping the ones that matter.

Naive approaches fail on one of our three budgets:

- **VLM-on-everything** — semantically great, computationally absurd (86k calls/day at 1 fps).
- **Motion-only** (classic NVRs) — computationally free, semantically useless: every cat,
  courier and headlight pings you until you disable notifications. This is the vendor-app
  status quo we exist to kill.
- **Detector-only** (modern NVRs) — "person detected" is better, but a person *walking past*
  and a person *casing your door* are the same alert.

## The cascade

Each tier is ~10–100× more expensive per invocation than the previous one and runs ~10–100×
more rarely. Cheap objective signals gate expensive semantic reasoning.

| Tier | Question | Mechanism | Invocation rate (target) | Cost (target) |
|---|---|---|---|---|
| **T0 Motion gate** | did pixels change meaningfully? | frame-diff on 640p substream @ 5 fps | continuous | < 2 % of one N100 core per camera |
| **T1 Detection** | is it a person / vehicle / animal / package? | small open detector (ONNX) on motion regions | during motion only | 10–30 ms/frame on N100 iGPU (OpenVINO) |
| **T2 Trajectory geometry** | what is it *doing* spatially? | ByteTrack + zone algebra + kinematics | per active track, pure math | negligible |
| **T3 Scene reasoning** | what is *happening*, in words? | VLM on selected keyframes, structured output | on T2 promotion only: ~10–50 calls/day/camera | seconds; budgeted & queued |
| **T4 Policy** | does the *user* care? | compiled plain-language policy over T2+T3 facts | per candidate event | negligible |

The efficiency claim in one line: **the cascade does ~3–4 orders of magnitude less semantic
compute than VLM-on-everything while answering a more precise question than detector-only.**

### Tier 0 — motion gate

Decode only the **substream** (the main stream goes to disk untouched — see
[storage.md](storage.md)). Weighted frame differencing with slow background adaptation,
day/night transition damping, and per-region sensitivity. Output: motion regions + a global
"scene active" bit that wakes Tier 1.

Design notes: camera-side motion events (ONVIF, bridge-adapter push) are used as *additional*
wake signals when available — useful for battery cameras that sleep, where we cannot decode
continuously.

### Tier 1 — detection

Small object detector over motion regions, batched across cameras.

Model candidates (permissive licenses only — **no AGPL** in the default install, ADR-0006;
licenses re-verified at integration time):

| Candidate | License | Notes |
|---|---|---|
| RF-DETR (nano/small) | Apache-2.0 | strong accuracy/latency at small sizes |
| D-FINE (S) | Apache-2.0 | strong CPU/OpenVINO story |
| RT-DETRv2 (S) | Apache-2.0 | well-supported ONNX export |
| YOLOX (s) | Apache-2.0 | battle-tested fallback |

Execution: **ONNX Runtime** with auto-selected execution provider — TensorRT/CUDA (NVIDIA),
OpenVINO (Intel CPU/iGPU — the N100 path), CoreML (Apple Silicon), CPU fallback. Hailo-8 and
Coral are plugin targets (M3+), driven by demand from the
[camera/hardware issue funnel](../../.github/ISSUE_TEMPLATE/camera_support.yml).

Classes at M2: `person`, `vehicle`, `animal`, `package`. Class list is deliberately short:
everything else is Tier 3's job.

### Tier 2 — trajectory geometry

ByteTrack (MIT) associates detections into tracks; then *pure math* extracts the features that
carry most of the "intent" signal embarrassingly cheaply:

| Feature | Definition | Signal |
|---|---|---|
| `approach` | velocity component toward an `entry`/`object` zone | walking *toward* the door vs. past it |
| `dwell` | time inside a zone | lingering at the gate |
| `loiter` | low displacement / high path length ratio | pacing, waiting |
| `repeat_pass` | re-entry count within a window | casing behavior |
| `speed_var` | acceleration profile | slowing down at your driveway |
| `touch` | track bbox intersects `entry`/`object` zone for > N s at near-zero velocity | at the door handle; at the wall-mounted equipment |

**Zone algebra** gives users cheap, deterministic control:

| Zone kind | Semantics |
|---|---|
| `entry` | doors, gates, windows — approach/dwell/touch here is high-signal |
| `object` | protected things (wall equipment, bike, car) — interaction detection |
| `private` | yard, porch — presence is notable, transit is not |
| `public` | sidewalk, street — **tracks that never leave it are suppressed**. This single rule kills the passers-by problem before any AI runs. |

Tier 2 alone (no LLM anywhere) already supports alerts like *"person approached the door and
stayed > 10 s"* — Vidette degrades gracefully to a fully deterministic system when the VLM is
disabled or over budget.

### Trusted-faces suppression *(📐 M4)*

The one identity feature, scoped tight ([guardrails](../faq.md#what-about-face-recognition)):
when a person track offers a usable face crop, it is matched against **locally stored
embeddings of enrolled household members**. A confident match marks the track `trusted`, and
policies with `ignore_trusted: true` (the default) skip promotion and notification for it —
the footage is still recorded and reviewable, just quiet. Two hard rules: an **uncertain
match never suppresses** (fail toward alerting — a stranger who vaguely resembles your cousin
still alarms), and matching never runs as identification — unknown faces stay "a person".
Model candidates (licenses verified at integration): a lightweight face detector plus an
ArcFace-class embedder via ONNX Runtime, sharing Tier 1's execution provider; enrollment and
deletion happen in the UI, never in YAML.

### Tier 3 — scene reasoning (VLM)

**Trigger** (promotion from T2): entered `entry`/`object` zone · `dwell` over threshold in
`private` · `loiter`/`repeat_pass` flags · vendor "doorbell pressed" events.

**Best-shot selection:** the VLM never sees a raw stream. Per track we select K frames
(sharpest, largest subject, plus one wide context frame), which bounds cost and improves
answers.

**Structured verdicts:** the model answers a fixed JSON schema, not free prose:

```json
{
  "activity": "trying door handle",
  "actors": [{"type": "person", "carrying": "none", "attention": "at door"}],
  "intent_risk": 0.87,
  "delivery_indicators": false,
  "summary": "A person approached the front door, tried the handle twice, and looked through the side window."
}
```

**Fusion:** the event's final intent score is a calibrated fusion of T2 geometry and the T3
verdict — the VLM is a judge, not an oracle. Disagreement (calm geometry + alarmed VLM, or
vice versa) lowers confidence and is surfaced as such.

**Budgets:** hard per-camera and global `max_calls_per_minute`; per-track dedupe and verdict
caching; a queue that degrades to T2-only alerts rather than stalling
(the [shedding ladder](overview.md#data-flow-and-backpressure)).

**Providers:** local first — Ollama / llama.cpp server. Candidates (re-verified at
integration): Qwen2.5-VL-7B (Apache-2.0), Moondream2 (Apache-2.0, tiny), SmolVLM2 (Apache-2.0),
LLaVA-OneVision. Cloud (OpenAI/Anthropic/Google) is **opt-in per camera**, marked in the UI,
and receives only the selected keyframes — never continuous video.

### Tier 4 — plain-language policies *(north star)*

The user writes what they care about:

> "Alert me only when someone looks genuinely interested in getting in. Ignore passers-by and
> routine deliveries."

A **policy compiler** (LLM, run at config time, not per event) turns this into an inspectable
`PolicySpec`: which zones matter, which T2 triggers arm it, what question T3 asks, what
threshold fires it. **The compiled spec is shown to the user for approval** — no mystery
meat. At runtime, policy evaluation is cheap and deterministic; the LLM is not in the hot path.

Calibration closes the loop: 👍/👎 on each notification adjusts per-policy thresholds
(conservative priors, monotone updates, visible in settings — "your paranoia dial, learned").
Policy **dry-run** replays the last N days and shows what *would* have fired before you commit.

## Latency budget (design target)

Motion start → phone notification, p50, 4-camera N100 reference box:

| Stage | Target |
|---|---|
| T0 motion confirm | ≤ 400 ms |
| T1 first detection | ≤ 300 ms |
| T2 promotion (for immediate triggers like `touch`) | ≤ 300 ms |
| notification dispatch (T2-grade alert) | **≤ 2 s total** |
| T3 verdict enrichment (local 7B VLM) | +3–10 s, delivered as an update to the same notification |

The pattern: **fast honest alert first, rich description as an edit** — never hold the alarm
hostage to the essay.

## Evaluation, or: how we avoid lying to ourselves

- M2 ships a **public reference clip set** (contributed, consented, license-clean) covering:
  passers-by, deliveries, door approaches, handle attempts, wall-equipment interaction, pets,
  headlights, rain/snow/night.
- Every release publishes precision/recall per scenario against this set, plus the three
  budgets on reference hardware. Regressions block the release.
- False-alert rate is a **first-class metric** — the product's entire reason to exist is the
  quality of its silence.
