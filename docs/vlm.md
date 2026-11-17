# The VLM judge: local or cloud, your choice

The VLM judge reads your policy, looks at a few frames of an incident and answers
`alert`, `silent` or `unsure`, with a one-line description of what it saw. It is the part
of Vidette that actually understands a policy; everything else measures it.

One OpenAI-compatible client serves two backends, and you choose which one judges:

| Backend | What it is | Where the frames go |
|---|---|---|
| `local` | Ollama (default), llama.cpp, vLLM or any OpenAI-compatible server you run | your machine or your network |
| `cloud` | OpenAI, Google Gemini (OpenAI-compatible endpoint), OpenRouter, or any other compatible API | **to that vendor** |

Status: ✅ the client, the judge, budgets, the live verdict worker, the System page switch,
and `vidette eval` / `vidette label` with either backend, tested against a fake
OpenAI-compatible server. Not yet measured on a
real model or real footage: no accuracy claim exists until `vidette eval` produces one.

## Configure

```yaml
understanding:
  vlm:
    backend: local            # none | local | cloud: the default choice
    frames_per_call: 4        # 1 to 8 frames per incident
    local:
      base_url: http://localhost:11434/v1   # Ollama; in Docker: http://host.docker.internal:11434/v1
      model: qwen2.5vl:7b
      timeout_s: 120          # CPU inference with 4 images is slow
      max_calls_per_day: 2000
      max_calls_per_minute: 20
    cloud:                    # optional; choosing it sends camera frames to the vendor
      preset: openai          # openai | gemini | openrouter | custom
      model: gpt-4.1-mini     # each preset has a default; check your provider's list
      api_key: ${OPENAI_API_KEY}
      max_calls_per_day: 200
      prices:                 # yours, from the provider's price page; reports use them
        usd_per_mtok_in: 0.40
        usd_per_mtok_out: 1.60
```

The prices above are placeholders to show the shape, not quotes: Vidette ships no price
list, because prices change and a wrong default would make every cost report lie. Without
prices, a cloud judge's cost is reported as **unknown**, never as $0.

| Preset | Base URL | Key variable you would use | Default model |
|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | `${OPENAI_API_KEY}` | `gpt-4.1-mini` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `${GEMINI_API_KEY}` | `gemini-2.5-flash` |
| `openrouter` | `https://openrouter.ai/api/v1` | `${OPENROUTER_API_KEY}` | `google/gemini-2.5-flash` |
| `custom` | your `base_url` (required) | anything | your `model` (required) |

For Ollama, install it and pull a vision model yourself (`ollama pull qwen2.5vl:7b`, a few
GB); Vidette never downloads models.

## Choosing the backend

1. `understanding.vlm.backend` in the config is the default.
2. The **System page** has a switch (Off / Local / Cloud). The choice is stored in
   Vidette's database and wins over the config until you change it again. Next to each
   option the page says where frames go; Cloud is disabled until a `cloud:` section exists.
3. Command-line tools (`vidette label`, `vidette eval`) take `--backend` /
   `--judge vlm-local|vlm-cloud`; without it, `label` uses the System page's choice, then
   the config.

## Live: the verdict worker

With a backend selected, every incident that closes on a camera with a policy (Frigate
review item or clip-folder clip) is judged once per policy:

- an `alert` answer at or above the policy's sensitivity floor (relaxed 0.75, balanced
  0.5, paranoid any confidence) **confirms** the event, and confirmed events are what your
  notification rules deliver, with the model's one-line description as the summary;
- otherwise a `silent` answer **dismisses** it (kept, searchable, quiet);
- if every policy abstained, the event stays **observed** with the reason (for example
  "model 'qwen2.5vl:7b' not found; run `ollama pull qwen2.5vl:7b`").

Only incidents that ended in the last 15 minutes are judged, one at a time, newest
first: while the judge is busy, the most recent waiting incident is taken next and older
ones wait until they are too old. A slow or shared model then answers about what is
happening now, and a restart, a replay or a backfilled folder never turns into a burst of
old alerts. With the [pre-filter](prefilter.md) on, every clip is looked at first by a
local detector: clips with people (or moving vehicles) are judged first, clips where
something moved but nobody was seen only while the model is idle, and clips with nobody
in them and nothing but light or the usual movement are not judged at all. Frames come from
the clip itself when there is one (chosen once for all policies, kept under
`media_dir/judge-frames/<event id>/` and used for the Telegram album), else from
Frigate's recordings over the incident (event snapshots if the camera does not record).
The clip's [stream health](collect.md#stream-health) is kept with the event
(`intent.stream`). The System page shows how many were judged.

## What the judge sees and answers

- **Frames**: from a clip, real frames picked by its packets, never instants on its
  timeline (an instant inside a stalled stretch would show the frame before the stall
  again). Where the picture changed is found on 64×36 grey thumbnails of the key frames
  (about half a second for two minutes of 1080p, no full decode): runs of change are
  episodes, each episode shown gets two frames so the judge sees how things moved, and
  with more episodes than that the longest are shown (a person working at a door lasts
  longer than headlights sweeping past). Nothing changed: frames spread over the clip.
  Without a clip, key frames already on disk (a dataset entry) are used as they are,
  spread evenly. Each frame is shrunk to `max_image_side` (768 px by default) and sent as
  a JPEG data URL. Measured on a two-minute night clip of a person walking past: the old
  even spread over the clip put the person in one frame of four; the episodes put them in two,
  three seconds apart, at different places.
- **Text**: the policy's name and words, the camera, local time of day, how long the
  recording is ("not how long anyone stayed") and what the upstream detector reported (if
  anything). Never a dataset's ground truth. From a clip, also each frame's time in the
  recording and, from its stream health, every stall between two frames ("the video
  stalled between frames 2 and 3 (70.3 to 83.5 s): how much time passed is unknown") and
  whether the recording was interrupted. The system prompt says what that means: a person
  whose position changes between frames is walking, how long someone stayed is judged only
  from frames without a stall between them. All of it is short on purpose: on a shared
  llama-server slot of 2048 tokens, four frames at 640 px and a policy with levels came to
  1916 prompt tokens, leaving about 130 for the answer (about 65 used). A prompt the
  server refuses for its context size is sent once more with the same frames 20 %
  smaller; the verdict counts both calls.
- **Who it saw**, with [known people](people.md) on: one line from the local face check
  (who was recognized and how sure, faces that matched nobody, frames without a clear
  face). Names go to a local model only; a cloud model hears "a known person".
- **Answer**, enforced with a JSON schema where the server supports it (falls back to
  JSON mode, then to instructions, and remembers what worked):

  ```json
  {"decision": "alert", "label": "door_handle_attempt",
   "description": "A person in a hood pulls the front door handle twice.", "confidence": 0.86}
  ```

- **`unsure` is an abstention.** So are timeouts, unreadable answers, missing frames and an
  exhausted budget. An abstention counts as "no alert" and never notifies; the verdict
  records which one it was, and `vidette eval` counts them by reason.
- **Policies with levels** ([telegram.md](telegram.md#levels-who-wakes-for-what)) get the
  levels' scenarios instead of the alert/silent question, and answer with a scenario id
  (enforced by an `enum` in the schema), a description, how many people are visible, the
  first frame that shows a person (kept as `person_at_s`, seconds into the clip), and a
  confidence:

  ```json
  {"scenario": "touching_equipment", "description": "A person opens the meter box.",
   "persons": 1, "first_person_frame": 2, "confidence": 0.8}
  ```

  The scenario's level makes it an alert or not; `other` is never one; `unsure` (or an
  id the policy does not have) is an abstention. `understanding.vlm.language` asks for the
  description in another language; the rest of the prompt stays English.

## Limits that cannot be skipped

| Limit | Default (local / cloud) | When it hits |
|---|---|---|
| `max_calls_per_day` | 2000 / 200 | the judge abstains with "daily VLM budget reached" until midnight |
| `max_calls_per_minute` | 20 / 10 | calls wait their turn |
| `timeout_s` | 120 / 45 | the call is abandoned, the judge abstains |
| `max_output_tokens` | 400 | the answer is cut; a cut answer that is not JSON abstains |

Every request counts against the day's budget, including failed ones (providers may bill
them).

A server with a small context per request rejects the call before the model sees it
(`HTTP 400 … exceeds the available context size`); the judge then tries once more with the
frames 20 % smaller, and abstains if that fails too. Every first attempt that fails costs a
call and leaves the judge with the smaller frames, so fit the first one. Where the tokens
go, measured on a llama-server with 2048 tokens per slot and a Qwen3 vision model (about
32 × 32 px per image token, four frames):

| Part | Tokens |
|---|---|
| system prompt | 197 |
| a policy with 14 scenarios on three levels (description and levels) | 600 to 750 |
| the clip's facts: camera, time, frame times, the face check's line | 110 to 180 |
| answer instructions | 84 |
| four frames at 640 × 360 px / 576 × 324 px | 888 / 728 |

Keep the policy's words short (each scenario said once; the judge reads the whole list on
every call), then lower `max_image_side` until the first attempt fits with room for the
answer (about 100 tokens). Fewer `frames_per_call` is the last resort: it removes moments,
not pixels. Command-line tools keep their count in `~/.local/state/vidette/vlm-usage.json`; the
server keeps its own in its database.

## Comparing local and cloud

```bash
uv run vidette eval ~/vidette-data/front-door --judge vlm-local --judge vlm-cloud
```

Both judges score the same entries; the report puts them side by side: precision and
recall with 95 % intervals, false alarms per day, abstentions by reason, and a table of
what was measured on that run (calls, images, tokens, cost from your prices, cost per day
at the set's volume, latency p50 and p95). Add `--judge frigate-severity` or
`--judge always` to see what the VLM buys over the free baselines. VLM judges run only
when named: a plain `vidette eval` never calls a model.
