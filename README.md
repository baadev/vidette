<div align="center">

# Vidette

**Alert policies in plain language, judged by a vision model on top of the recorder you
already have, measured before you trust them.**

*A **vidette** was a mounted sentry posted ahead of the picket line: the first to see
trouble coming, and the one trusted to tell a passing farmer from an approaching threat.*

[![v0.2](https://img.shields.io/badge/v0.2-development%20line-8a2be2)](ROADMAP.md)
[![Last release](https://img.shields.io/github/v/release/baadev/vidette?label=last%20release&color=lightgrey)](https://github.com/baadev/vidette/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![CI](https://github.com/baadev/vidette/actions/workflows/ci.yml/badge.svg?branch=v0.2)](https://github.com/baadev/vidette/actions/workflows/ci.yml?query=branch%3Av0.2)

[Status](#status) · [What it does](#what-it-does-today) · [How it works](#how-it-works) ·
[Try it](#try-it) · [Roadmap](ROADMAP.md) · [Docs](docs/README.md)

</div>

## Status

> **v0.2 is a development line, not a release.** No v0.2 images are published; run it from
> a checkout. The last release, v0.1.3, is the old NVR line, frozen at tag
> [`nvr-core`](https://github.com/baadev/vidette/tree/nvr-core) with its images unchanged.
> Why the turn: [ADR-0009](docs/architecture/adr/0009-intent-layer-over-frigate.md).
>
> Vidette is no longer an NVR; a recorder records and detects.
> [Frigate](https://frigate.video)'s review items, or a folder of clips (which
> `vidette collect` can fill from a camera Frigate cannot use), are its incidents. Vidette
> judges each one under a policy you wrote in plain words, decides who should be woken,
> sends the alert to Telegram with the frames that show the person, and turns every
> 👍 / 👎 into a labelled clip that `vidette eval` can score.
>
> **No accuracy numbers yet.** Every part below is tested against fakes (an RTSP camera, an
> OpenAI-compatible model, the Telegram API), and the clip-folder path has been running on
> a test deployment with a local model since November 2026. The first numbers will come
> from `vidette eval` on a labelled set of real clips, with the set's size next to them.

## Why

A camera that sees everything is easy. A notification you do not learn to ignore is hard.
Frigate already finds the person in the frame, and since 0.17 it can describe the scene and
rate a threat level. What you still cannot do is say, in your own words, which situations
should reach you, check how often that rule would have woken you last week, and see the
error rates before you rely on it.

> You tell Vidette:
>
> **"Wake us when someone tries to get in. Ignore passers-by, couriers, cats and
> headlights."**
>
> It asks a vision-language model a structured question about the few frames that matter,
> applies your rule, and stays quiet about everything else. And it tells you, with numbers
> from your own clips, how often it gets that right.

That last part is the product as much as the first: a policy you cannot measure is a
vibe, and a security system that runs on vibes gets switched off.

## What it does today

### Incidents from a recorder, or from a folder of clips

- **Frigate** (0.14 or newer): review items and tracked objects over MQTT; snapshots,
  frames and clips over its HTTP API, read-only. Vidette opens no camera connection
  ([docs/frigate.md](docs/frigate.md)).
- **Clip folders**: every new clip in a folder becomes an incident, so the pipeline runs
  without Frigate ([docs/clip-folders.md](docs/clip-folders.md)).
- **`vidette collect`** fills such a folder from an RTSP camera that Frigate cannot use
  ([docs/collect.md](docs/collect.md)):
  - **wake on motion** (the default): waits for a battery camera's stream and keeps each
    wake period as a clip with key frames and a JSON sidecar; `--base-station` for a
    camera behind a base station that answers 404 while the camera sleeps;
  - **`--continuous`**: for a camera that streams all the time, a bounded ring and a
    motion gate cut a clip with pre-roll around each motion; `--rotate` for a camera
    mounted on its side;
  - **`--early 8s`**: the first seconds of a recording go to the judge while the camera
    still records;
  - **`--prune-oldest`**: past 95 % of the folder's cap the oldest clips nothing needs are
    deleted down to 90 %, instead of the camera stopping. Labelled clips, events that
    alerted or were marked (and the other cameras around them), clips with faces and the
    last 24 hours are always kept.

  Hard caps on the folder and on free disk; the camera's URL is read from a file or the
  environment, never from the command line, and never printed.

### The judge: your policy, a vision model, levels

- **A policy in plain words**, optionally split into **levels** (say *critical*,
  *elevated*, *none*), each with named scenarios and a `wake` rule: `always`,
  `unless_quiet`, `unless_known` or `never`. The judge names one scenario; its level
  decides whether it is an alert and who is woken
  ([docs/telegram.md](docs/telegram.md#levels-who-wakes-for-what)).
- **Local or cloud**, your choice: one OpenAI-compatible client with a `local` backend
  (Ollama, llama.cpp, vLLM) and a `cloud` one (OpenAI, Gemini, OpenRouter or any compatible
  URL), set in the config or switched on the System page, which asks before frames go to a
  vendor ([docs/vlm.md](docs/vlm.md)).
- **Evidence, not vibes**: the judge sees real frames where the picture changed, with
  their times and where the stream stalled, and names the frames that show its subject. A
  scenario that no frame shows is an abstention, not an alert. Daily call budgets,
  timeouts; no verdict means no alert.

### Telegram: alerts you can argue with

- **A built-in bot**, long polling only (no webhook, no open port), answering only the
  chats you allow ([docs/telegram.md](docs/telegram.md)).
- **An alert** is an album of up to three frames that show the subject (the sharpest moment
  of each face, then the frames the judge points at) and the clip, with the camera, the
  time, what the judge saw and who the face check recognized.
- **👍 / 👎 / Кто это? / Не знаю**: 👎 asks why (passed by, own car, ours, an animal,
  light, a stalled recording, …). Each answer is feedback on the event and a reviewed label
  in the clip's dataset; the buttons then shrink to one that takes the answer back.
- **Early alerts** from the first seconds of a recording, for levels that wake for anyone;
  the same message is updated when the whole clip has been judged.
- **An evening digest**, sent only when something needs a look; `/status` (including where
  the last alert's time went), `/quiet`, `/help`.
- **Questions in plain words** («where else was this person?», «send the other cameras»):
  a small tool-calling assistant answers from events, clips and identities, on the judge's
  local model by default. The same read-only tools are served over MCP at
  `POST /api/v1/mcp` ([docs/assistant.md](docs/assistant.md)).

The bot speaks Russian for now; the judge's one-line description can be in any language
(`understanding.vlm.language`).

### People, opt-in

- **Known people**: local face recognition (OpenCV's YuNet and SFace) for people who
  agreed to be enrolled; the judge is told who it saw, and an `unless_known` level stays
  in the digest when everyone in the clip is known ([docs/people.md](docs/people.md)).
- **Anonymous identities**, off by default: every face found becomes a number ("stranger
  #12, 3rd time today") that is counted when it comes back, on any camera. Nobody is named
  unless you type a name. Mark one **familiar** (a neighbour, the postman: quiet like the
  household, while the critical level still wakes) or suspicious; merge, split, promote to
  a known person, and take any answer back. Unnamed identities are deleted after 30 days
  ([docs/people.md](docs/people.md#strangers-who-come-back)). These are biometric data of
  people who never agreed:
  [read this first](docs/people.md#privacy-and-the-law).

### The other cameras at that moment

For any clip-folder event: what every camera recorded around its moment, "the same
person" only when a face says so, approximate times for cameras whose video arrives late,
and all of them as one mosaic video (ffmpeg only). On the web event card and under the
alert ([docs/other-cameras.md](docs/other-cameras.md)).

### Measuring it

- **`vidette label draft | sheets | apply`**: a model drafts labels, numbered contact
  sheets are drawn for review on a phone, and a reply such as `3 courier, 7 alert, rest
  ok` (Russian works too) applies them. A draft is not truth until someone reviews it
  ([docs/label.md](docs/label.md)).
- **`vidette eval`**: precision, recall and false alarms per day with 95 % intervals,
  abstentions by reason, measured cost and latency, for VLM judges side by side with four
  model-free baselines ([docs/eval.md](docs/eval.md)).

### The web app

Events (with the other cameras and answers about faces), People and Identities, and System
(sources, the judge switch, the day's budget). An account is created on first start; there
are no default credentials.

## How it works

```mermaid
flowchart LR
    C[Cameras] --> F["Frigate
record · detect · review items"]
    C --> K["vidette collect
wake on motion · continuous"]
    K --> D[(Clip folder)]
    F -- "MQTT, HTTP" --> I[Incident]
    D --> I
    I --> P["Face check
opt-in, local"]
    P --> J{"Judge
your policy and its levels
local or cloud VLM, budgeted"}
    J -- "a level that wakes" --> T[["Telegram alert
frames · clip · why"]]
    J -- otherwise --> S["Kept, quiet
evening digest"]
    T -- "👍 / 👎 and a reason" --> L[Labelled clips]
    L --> E["vidette eval
precision · recall · false alarms/day · cost"]
    J -. same judge .-> E
```

- **The recorder does the cheap tiers** (motion, detection, tracking, zones). Vidette
  records nothing for Frigate's cameras; `vidette collect` exists only for cameras Frigate
  cannot use.
- **Vidette owns the expensive, rare tier**: one judgment per incident, against your
  policy, on a call budget.
- **The judge that runs at home is the one `vidette eval` scores**: same interface, same
  incident type, so offline numbers and live behaviour come from the same code path.

## Try it

You need [uv](https://docs.astral.sh/uv/) and a checkout of the `v0.2` branch.

### Without a camera, in five minutes

```bash
git clone -b v0.2 https://github.com/baadev/vidette && cd vidette/server
uv sync --locked
uv run vidette eval ../datasets/synthetic-sample
```

Precision, recall and false alarms per day for the four baselines on a five-entry
synthetic set. Its numbers mean nothing; it shows the format.

### Your own camera, without Frigate

Also needed: ffmpeg, an RTSP camera, a vision model behind an OpenAI-compatible API (for
example `ollama pull qwen2.5vl:7b`), a Telegram bot from
[@BotFather](https://t.me/BotFather) and Node.js for the web app.

1. **The camera's address**, readable by you only:

   ```bash
   mkdir -p ~/.config/vidette
   printf '%s\n' 'rtsp://USER:PASSWORD@192.0.2.10:554/live0' > ~/.config/vidette/rtsp-url
   chmod 600 ~/.config/vidette/rtsp-url
   ```

2. **The bot's token and your chat id** ([how to find it](docs/telegram.md#set-up)) in
   `~/.config/vidette/telegram.env` (`chmod 600`):

   ```
   VIDETTE_TELEGRAM_BOT_TOKEN=<token from BotFather>
   VIDETTE_TELEGRAM_ALLOWED_CHATS=<your chat id>
   ```

3. **The service's config**, `~/.config/vidette/serve.yaml`:

   ```yaml
   storage:
     media_dir: ${HOME}/vidette-data/media
     database: ${HOME}/vidette-data/vidette.db

   clip_folders:
     - path: ${HOME}/vidette-data/porch      # the folder vidette collect fills

   understanding:
     vlm:
       backend: local                         # none | local | cloud
       local:
         base_url: http://localhost:11434/v1  # Ollama, llama.cpp, vLLM
         model: qwen2.5vl:7b

   policies:
     - name: home
       description: >-
         A private house. Wake us for people who try to get in; ignore people
         walking past, couriers who leave a parcel, cats and headlights.
       levels:
         critical:
           wake: always                       # even for people we know, even in quiet hours
           scenarios:
             door_attempt: someone pulls or tries the door handle, a window or the gate
         elevated:
           wake: unless_known                 # quiet when the face check knows everyone
           scenarios:
             person_at_house: a person who stops at the house and stays, with no errand
         none:
           wake: never                        # the vocabulary for the evening digest
           scenarios:
             passer_by: walks past without stopping or touching anything

   notifications:
     telegram:
       token: ${VIDETTE_TELEGRAM_BOT_TOKEN}
       allowed_chats: ${VIDETTE_TELEGRAM_ALLOWED_CHATS}
   ```

   `unless_known` needs [known people](docs/people.md); without them it wakes like
   `unless_quiet`, and `vidette validate` says so.

4. **Judge and alert** (from `vidette/server`):

   ```bash
   (cd ../web && npm ci && npm run build)
   uv run vidette telegram --config ~/.config/vidette/serve.yaml \
       --env-file ~/.config/vidette/telegram.env check
   VIDETTE_WEB_DIST="$PWD/../web/dist" uv run vidette serve --host 127.0.0.1 \
       --config ~/.config/vidette/serve.yaml --env-file ~/.config/vidette/telegram.env
   ```

   Open http://127.0.0.1:8642 and create the admin account.

5. **Record**, in a second terminal (a battery camera wakes on motion; add `--continuous`
   for one that streams all the time):

   ```bash
   uv run vidette collect ~/vidette-data/porch --camera porch --check
   uv run vidette collect ~/vidette-data/porch --camera porch --early 8s \
       --prune-oldest --max-size 15GB --serve-config ~/.config/vidette/serve.yaml
   ```

   Every clip it keeps reaches the judge within seconds; when the verdict's level wakes
   someone, the alert arrives in Telegram with its buttons. On a Mac, launchd templates
   for both processes are in [deploy/macos/](deploy/macos/).

### Next to Frigate

[docs/getting-started.md](docs/getting-started.md) and [docs/frigate.md](docs/frigate.md):
a `frigate:` section instead of `clip_folders:`, and compose builds the image from the
checkout.

## Feature status

Legend: ✅ works on `v0.2` · 🚧 in progress · 📐 designed · 🔭 exploring. Details and
everything else: [ROADMAP.md](ROADMAP.md).

| Capability | Status |
|---|---|
| Frigate source: reviews and tracked objects over MQTT; snapshots, frames and clips over HTTP | ✅ tested on recorded-format fixtures |
| Clip folders as an incident source; `vidette collect` (wake on motion, `--continuous`, `--base-station`, `--rotate`, `--early`, `--prune-oldest`) | ✅ tested with fake RTSP cameras; running on real cameras |
| VLM judge, local or cloud, policies with levels, evidence frames, budgets; Off / Local / Cloud on the System page | ✅ tested with a fake model; running live on a local model; not measured yet |
| Telegram bot: alerts with the subject's frames and the clip, 👍 / 👎 with reasons as labels, early alerts, evening digest, `/status` `/quiet` | 🚧 tested against a fake Telegram API; running live |
| Questions in plain words to the bot; read-only MCP tools | 🚧 tested against fakes; slow on a shared model |
| The other cameras at an event's moment, one mosaic video | 🚧 clip folders only; the on-screen clock is not read |
| Known people (opt-in) | 🚧 small faces on security cameras are often unreadable |
| Anonymous identities with familiar and suspicious marks (opt-in, off by default) | 🚧 no accuracy figure; thresholds from one deployment |
| `vidette label`: model drafts, phone contact sheets, replies by number | ✅ |
| `vidette eval`: baselines, VLM judges side by side, intervals, cost | ✅ |
| Signed webhooks, Apprise (100+ services), web push, MQTT + Home Assistant discovery | ✅ on confirmed events only |
| The bot in English | 🔭 |
| A plain-language policy shown back as an inspectable spec | 📐 levels are written by hand |
| Dry run of a policy over past events | 📐 |
| Calibration from 👍 / 👎 | 📐 the answers are collected as labels |
| Published accuracy on a labelled set of real clips | 📐 needs the set |
| v0.2.0 release with its own images | 📐 |

## The privacy promise

- **Local-first.** Clips stay in Frigate or your clip folder; the judge runs on your
  network unless you choose a cloud backend, which needs its own config section, a
  confirmation on the System page, and receives a few still frames per incident, never
  streams.
- **You choose every destination.** Telegram is a cloud service: an alert's frames, its
  clip and the judge's description pass through it, to the chats you allowed. Names of
  recognized people go there only with `names: true`. Face crops, embeddings and reference
  photos never leave your machine.
- **Zero telemetry.** No accounts, no phone-home, no paywalled features.
- **Behaviour, not identity.** Vidette judges what is happening, not who someone is. The
  exceptions are opt-in: [known people](docs/people.md), recognized on your machine, only
  among people who agreed to be enrolled; and, off by default,
  [anonymous identities](docs/people.md#strangers-who-come-back): unknown faces counted as
  numbers, never named from any source, kept on your machine for 30 days unless you mark
  them. No age, gender, skin colour or clothing is used to match or flag anyone.

Threat model and hardening: [security-model.md](docs/architecture/security-model.md).

## Standing on giants

[Frigate](https://github.com/blakeblackshear/frigate) (detection, recording, review items),
[FFmpeg](https://ffmpeg.org) (every clip `vidette collect` keeps),
[OpenCV](https://github.com/opencv/opencv_zoo) (YuNet and SFace, for opt-in faces),
[Ollama](https://ollama.com) and [llama.cpp](https://github.com/ggml-org/llama.cpp) (local
judges), [Apprise](https://github.com/caronc/apprise) (100+ notification services),
[SQLite](https://sqlite.org), [FastAPI](https://fastapi.tiangolo.com).

## FAQ

**Do I need Frigate?** No. A folder of finished clips works as a source too
([clip-folders.md](docs/clip-folders.md)), filled by `vidette collect` or by a camera that
uploads its motion clips. For cameras that stream all the time, Frigate is still the better
recorder and detector.

**My camera runs on a battery.** It cannot feed Frigate continuously.
[`vidette collect`](docs/collect.md) records its wake periods over RTSP into a clip folder,
which Vidette judges live and [`vidette label`](docs/label.md) turns into a labelled set
for `vidette eval`. Behind a base station that answers 404 while the camera sleeps (a Eufy
HomeBase, for one) add `--base-station`.

**What does the judge need?** A vision model behind an OpenAI-compatible API: a local one
(Ollama, llama.cpp, vLLM) on a machine with a GPU or Apple silicon, or a cloud API you opt
into. Vidette itself is one small Python process. On a busy shared 27B model a verdict took
51 to 115 s ([measured](docs/telegram.md#measured-in-testing)); early alerts and a model of
its own are how alerts get faster.

**Frigate 0.17+ already writes GenAI summaries. Why this?** Those summaries describe and
rate; they do not decide whether your phone buzzes under a rule you wrote, and they come
without error rates. `vidette eval` scores Frigate's summaries as a baseline
(`frigate-genai`), so the comparison is on the table, not in an adjective.

**Is "intent recognition" real?** It is a probabilistic judgment from behaviour (approached,
lingered, tried the handle), and it will make mistakes. That is why the measuring tool came
first, and why every alert asks whether it was right. More: [FAQ](docs/faq.md).

## Contributing and contact

The most useful contribution right now is a labelled clip set you are allowed to share
(synthetic, staged or public domain; never footage of people who did not agree), or a
recorded Frigate session from `vidette frigate tail --record` that Vidette fails to parse.
Code: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

- Issues and discussions: [github.com/baadev/vidette](https://github.com/baadev/vidette)
- Email: **alex@baadev.com**

## License

[Apache-2.0](LICENSE) © 2026 Alexander Belov and Vidette contributors.
Camera and NVR trademarks belong to their owners; Vidette is not affiliated with Frigate or
any camera vendor.
