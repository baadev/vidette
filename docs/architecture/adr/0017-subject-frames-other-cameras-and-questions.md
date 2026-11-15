# ADR-0017: Alerts that show their subject and can be argued with, the other cameras at that moment, and questions in plain words

- **Status:** accepted
- **Date:** 2026-11-15
- **Amends:** [ADR-0013](0013-telegram-bot.md) (the hourly digest becomes one evening
  digest that is sent only when something needs a look; answers become one button that
  takes them back; free text from allowed chats is now read), and
  [ADR-0015](0015-truthful-timing-and-early-verdicts.md), decision 4 (an early window
  alerts only at a level that wakes for anyone). Everything else in those ADRs stands.

## Context

Two days on a test deployment with clip-folder cameras, the bot running live:

- **Almost every alert was false.** 12 events alerted; 9 were answered, all 9 "false
  alarm"; none "right". By cause: passers-by close to a house, or stopping for a moment
  (5), people using their own car (3, named "someone tries a car" by the judge), early
  alerts that the whole clip then called ordinary (2), frames with nobody in them (1).
  The one plausible alert was never answered.
- **The album showed an empty street.** Four frames spread over where the picture
  changed; usually one had the person in it.
- **The hourly digest was noise**, and its lines (`прохожие 2 · без вердикта 1 ×3`) read
  like a log.
- **An event is one camera.** A person who walks along the street is usually on two or
  three cameras within a minute, and an alert came with one camera's clip. Each camera's
  clip times follow its own clock (ADR-0015). Measured against the cameras' on-screen
  clocks (12 clips per camera, read by hand from the picture): cameras that stream all
  the time stamp their clips 0 to 4.5 s after the moment on their screen; a battery
  camera that wakes through a base station 0 to 100 s after it, different on every wake
  (median about 13 s). Second-level alignment holds for cameras that stream all the
  time, not for one that has to wake up.
- **Questions had no place to go.** "Where else was this person?", "send what the other
  cameras saw then" were typed into the chat and ignored.

## Decision

### A. Quieter alerts that can be argued with

1. **The judge names the frames that show its subject** (`subject_frames`, clearest
   first). A scenario of an alerting level with no such frame is an abstention ("named it
   but pointed at no frame that shows it"), not an alert.
2. **The prompt says what the policy text kept missing:** ordinary use is not an attempt
   (getting into a car, loading it, unlocking a door and walking in); an alerting scenario
   needs the act in the frames, otherwise the closest scenario that does not alert;
   describe what people do, not a guessed gender, age or ethnicity.
3. **The album is the subject**: the best moment of each face the check found (largest
   first, extracted from the clip), then the judge's subject frames; at most `frames` (now
   3). Older events fall back to the judged frames from the first one with a person.
4. **An early window alerts only at a level that wakes for anyone** (`wake: always`).
   Other levels wait for the whole clip. A policy without levels is unchanged.
5. **👎 asks why.** A short list of reasons, the likely ones for this event first
   ("passed by", "own car", "ours", animal, vehicle, light, a stalled recording, wrong
   camera, "right but not worth it", "other: I will type it"). The reason is kept on the
   event (`intent.review`) and as the dataset entry's `reason`, with the truth label it
   implies when the policy has that scenario.
6. **One button per answer.** After an answer the keyboard shrinks to a button that says
   what was chosen; tapping it takes that answer back exactly (feedback, label with its
   previous state, face decision) and shows the full buttons again.
7. **One digest a day**, at `digest_at` (21:00), silent, and only when something needs a
   look (a confirmed event that woke nobody, an unanswered alert); plain sentences. The
   day is recorded before sending, so a restart never repeats it. `/status` and a question
   give today's digest any time.

### B. The other cameras at that moment

1. **Linked clips are found, not stored**: every clip of every clip-folder camera that
   overlaps a window around the event's moment (when the judge first saw a person, else
   the clip's start; `cross_camera.before` 15 s, `after` 45 s), read from the folders'
   sidecars on demand, so a clip that arrives later is found too.
2. **A camera whose video arrives late** (`cross_camera.lag_up_to`, e.g. 2 min for a
   battery camera) is searched that much later and every mention of it says its time is
   approximate. Reading the on-screen clock to place such a camera exactly is 📐 designed,
   not built: it needs per-camera OCR, which ADR-0015 kept out of the pipeline.
3. **The same person only by face.** A clip shows "the same person" only when an
   anonymous identity or an enrolled person is in both events' face reports. Without a
   face the words are "at the same time", never "the same person". Nothing about how
   anyone looks is used to link people.
4. **One video**: a mosaic in real time, one tile per camera (landscape cameras stacked,
   a camera mounted on its side next to them at full height), each tile showing its
   camera's clips at the moment they were recorded and dark grey when it recorded
   nothing. The cameras' own on-screen clocks stay in the picture; nothing is drawn on
   top (the message says which tile is which camera), so no font or text renderer is
   needed. Over the size limit it is encoded smaller, then cut into parts.
5. **Where it shows**: the web event card (each clip, the mosaic), the bot's
   «📹 Другие камеры» under an alert, and questions (C).

### C. Questions in plain words, and the same tools over MCP

1. **Free text from an allowed chat is a question**; a reply to an alert (its text, its
   album or an answer about it) makes that event the context. Nothing from other chats is
   read.
2. **Tools, not free generation**: find events, an event's details, the clips at a moment,
   where an identity was seen, send the mosaic, mark an event the way the buttons do,
   today's digest. The model chooses tools and words the answer; every fact comes from a
   tool.
3. **The model is the judge's local endpoint by default** (`assistant.backend: local`).
   Native tool calls; a server without them gets the tools as a JSON-action prompt. A
   cloud backend is opt-in and never receives a name, a face crop, an embedding or a
   frame: results for it carry "свой" and "№12" only.
4. **The three commonest questions skip the model**: "the other cameras at this moment",
   "where else was this person", "today". A shared model answers in one to three minutes;
   these answer in seconds.
5. **MCP**: the read-only tools at `POST /api/v1/mcp` (JSON-RPC, MCP's streamable HTTP
   without sessions), behind the API's own auth (`read:events`), on the server's address
   (127.0.0.1 by default). No names in results: an MCP client may hand them to a cloud
   model. Videos are API links, not bytes.

## Measured

- **False alerts, replayed.** The 11 alerted events of those two days that still had their
  frames, judged again by the same model on the same frames with the new prompt and the
  tightened policy: 0 alerts (7 passers-by, 2 people using their own car, 1 with nobody in
  the frames; the two early ones are now also held back by A4), and 1 abstention: the one
  plausible event, whose later frames were too dark ("unsure"). Of a random sample of 8
  quiet events with people in them, 7 stayed quiet and 1 became a false critical
  "someone tries a car" (a person standing next to a car). The car scenario was tightened
  again (a hand on the handle of a car that stays shut; standing or walking next to a car
  is not it), and a second run of that event, the two car events and the plausible one
  gave: quiet, own car, own car, and this time an alert for the plausible one ("a person
  comes onto the property"). The same frames can be judged differently on two runs.
  These are judgments on one deployment, not a rate.
- **The mosaic**: a minute of three cameras, 1044×720,
  15 frames a second: 1.1 MB, 2.3 s to make on an Apple-silicon Mac; finding the clips
  around a moment in three folders of about a thousand clips each: 0.06 s.
- **A question to the shared local model** (a 27B model on 6 slots of 2048 tokens, shared
  with other work): about 50 s for one tool call when the server is busy, 1 to 3 minutes
  for an answer; the quick answers take under a second plus the upload.

## Consequences

- ✅ An alert shows the person, wakes less often, and every answer is a labelled reason the
  next policy change can be measured against.
- ✅ "What did the other cameras see" is one tap, and the answer says plainly when a camera
  was asleep.
- ⚠️ **Fewer alerts includes fewer true ones.** The stricter rules and the evidence rule
  can turn a real but dimly lit event into an abstention (the replayed plausible event,
  on one of its two runs). The digest lists confirmed events that woke nobody; abstentions
  are only counted.
- ⚠️ **The judge is not deterministic**: the replay saw one new false critical alert
  before the policy was tightened a second time, and one event judged two ways. Policy
  wording is tuned on a handful of events; the reasons under 👎 are what will show whether
  it holds.
- ⚠️ A battery camera's place in the mosaic can be off by up to its lag; the caption
  says so. Cameras that stream all the time line up within a few seconds.
- ⚠️ Questions use the shared model's slots and its daily budget (`assistant.max_calls_per_day`).
- ⚠️ Compute: a mosaic is one ffmpeg run per request (seconds); kept a day under
  `media_dir/linked/`.
- **Tripwires:** if same-person linking ever wants appearance (clothes, height, colour),
  stop: that is profiling, which this project does not build (ADR-0014). If questions
  need a cloud model to be useful, keep names and faces out of it by construction, not
  by prompt. If the bot starts to need chats beyond the configured ones, see ADR-0013's
  tripwire.

## Alternatives considered

- **Keep four spread frames and add a person detector to pick them**: a new model and a
  decode of every clip; the judge already looks at the frames and the face check already
  found the best face moment.
- **Raise the confidence threshold instead of the evidence rule**: the false alerts came
  with confidence 0.85 to 0.95; confidence did not separate them.
- **Burn camera names and times into the mosaic**: needs ffmpeg with a font renderer or an
  imaging library; the cameras already show their clock, and a caption is readable.
- **Link people across cameras by time and position**: a guess presented as identity; it
  is shown as "at the same time" instead.
- **A cloud model for questions by default**: faster, but the facts are a household's
  comings and goings; local stays the default.
