# Deep analysis: a second look at the day's clips

The live judge has minutes and four frames, because someone may be woken. Deep analysis
has the night: it looks again at the clips with people, faces or vehicles (and those the
live judge never reached), with more frames, describes what happens step by step, and
judges from that description under the same policy. Its verdict is kept next to the live
one, never over it. Where the two disagree, or either is unsure, the clip goes to the
next labelling batch; the evening digest quotes the deep descriptions. Design:
[ADR-0019](architecture/adr/0019-deep-analysis.md).

Status: 🚧 built and tested against a fake model server (look, decide, the queue, the
shared slot, preemption, back-off, review drafts, the digest); run on a test deployment
with a 27B vision model on a shared llama.cpp server. Whether it is better than the live
judge is not measured yet: the disputes it sends to labelling are how it will be.

## Turn it on

```yaml
understanding:
  vlm:
    backend: local          # the endpoint deep analysis uses too (backend: below)
    language: Russian       # the descriptions' language
    local: {base_url: http://127.0.0.1:8080/v1, model: …, api_key: ${LLAMA_KEY}}
  deep:
    enabled: true
    window: "01:00-07:00"   # local time, may wrap midnight; back to back inside it
    outside_window: trickle # or off
    trickle_every: 10m      # outside the window, at most one clip this often
    max_clips_per_day: 150
    frames: 8               # per clip, `frames_per_call` at a time (default: all in one)
    slots: check            # read the server's /slots, send only with an idle slot
    label_disputes: true    # disputed and uncertain clips become review drafts
```

It runs inside `vidette serve`; there is nothing else to start.

## What it does with a clip

1. **Look**: up to eight frames spread over the clip where the picture changed, with their
   times, the stalls between them and the face check's line, and no policy. Answer: what
   happens, in order (who is where, what their hands do, where they go, which vehicles
   move or stop), how many people and vehicles, which frames show them.
2. **Decide**: no frames; the policy and its levels, the clip's facts and what the look
   found. The answer has the live judge's shape with a description of two to four plain
   sentences, so a scenario that no frame shows is still an abstention.
3. **Compare** with the live verdict: *agree*, *disputed* (alert or level differs, or no
   live verdict and deep says alert), *uncertain* (either unsure, or below
   `min_confidence`), *only deep* (no live verdict, nothing to wake for).
4. **Keep**: the verdict, the looks, the frames, timings and the comparison in the event's
   `deep` field; a disputed or uncertain clip in a dataset folder gets a draft entry in
   `labels.yaml` with the deep scenario proposed and both verdicts in `notes`, so
   `vidette label sheets` shows exactly those.

## One request at a time, never in someone else's slot

A shared model server may give this installation one parallel slot. The live judge, the
assistant and deep analysis share it in one process. Inside the window live calls go
first: deep analysis takes the slot only while the live judge rests, and a live call that
arrives cancels the deep request (the job goes back to the queue; the frames it already
described are not asked again). Outside the window a busy live judge may never rest, so
the trickle takes one clip every `trickle_every` as a turn of its own: in line with the
live calls, not cancelled. A live verdict then waits for at most one deep request (about
a minute on a shared 27B model) once per `trickle_every`; `outside_window: off` if that is
too much. Before every request the server's `GET /slots` (llama.cpp, with the API key) must
show an idle slot; otherwise it waits 15 s, doubling up to 5 min. `slots: ignore` for
servers without that endpoint (Ollama, hosted APIs).

## The queue

One row per event (`deep_jobs` in the server's database): queued, running, done, failed or
skipped, with the attempt count, the local day it counted against the cap and per-request
timings. It survives restarts (a running job is queued again) and never analyses an event
twice. Priority: alerts, then alert-level verdicts below the bar, unsure verdicts with a
person, faces, people, vehicles, and last the clips the live judge never reached; newest
first within each.

```bash
vidette deep --config ~/.config/vidette/serve.yaml status      # queue, today's count, last clips
vidette deep --config ~/.config/vidette/serve.yaml show <event>  # one deep verdict as JSON
```

## The digest

The evening digest (docs/telegram.md) quotes the deep description of an event when there
is one, lists what deep analysis flagged that the live judge did not (also from the end of
last night, after the last digest), and adds one line when disputed clips went to
labelling. It is still sent once a day, and only when something needs a look.

## Budgets

| | |
|---|---|
| Compute | two requests per clip on the judge's server: one with up to eight frames, one text-only. Timings from the first runs are in the queue's stats (`vidette deep status`) |
| Storage | a few kB of JSON per event, the chosen frames under `media_dir/deep-frames/` |
| Latency | at night none added to live verdicts (they take the slot back); by day a live verdict may wait for one deep request once per `trickle_every` |
| Money | none on a local server; on a cloud backend, two calls per clip up to `max_clips_per_day` |
