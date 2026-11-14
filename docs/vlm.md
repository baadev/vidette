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

Only incidents that ended in the last 15 minutes are judged, one at a time, so a restart,
a replay or a backfilled folder never turns into a burst of old alerts. Frames come from
the clip folder's key frames, else from Frigate's recordings over the incident (event
snapshots if the camera does not record). The System page shows how many were judged.

## What the judge sees and answers

- **Frames**: key frames already on disk (a collected clip, a dataset entry) are used as
  they are, spread evenly; otherwise frames are taken from the clip with ffmpeg, centred in
  equal slices of its duration. Each frame is shrunk to `max_image_side` (768 px by
  default) and sent as a JPEG data URL.
- **Text**: the policy's name and words, the camera, local time of day, duration and what
  the upstream detector reported (if anything). Never a dataset's ground truth.
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

## Limits that cannot be skipped

| Limit | Default (local / cloud) | When it hits |
|---|---|---|
| `max_calls_per_day` | 2000 / 200 | the judge abstains with "daily VLM budget reached" until midnight |
| `max_calls_per_minute` | 20 / 10 | calls wait their turn |
| `timeout_s` | 120 / 45 | the call is abandoned, the judge abstains |
| `max_output_tokens` | 400 | the answer is cut; a cut answer that is not JSON abstains |

Every request counts against the day's budget, including failed ones (providers may bill
them). Command-line tools keep their count in `~/.local/state/vidette/vlm-usage.json`; the
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
