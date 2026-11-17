# Changelog

All notable changes to Vidette are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

Development line `v0.2`: Vidette becomes an intent layer on top of Frigate and of folders
of clips ([ADR-0009](docs/architecture/adr/0009-intent-layer-over-frigate.md)). Not
released; no images published from it; the last release is v0.1.3.

### Added
- **`vidette eval` on stored verdicts** ([docs/eval.md](docs/eval.md#scoring-what-the-service-already-said)):
  `--judge stored-live | stored-deep | stored-best` score what a running service already
  said, read-only from its database (`--db`, default the config's), matched by clip path,
  abstaining with "not in the database" or "not judged"; `--policy-from-config NAME` takes
  a service's policy with levels as ground truth (its alerting scenarios); several
  datasets are scored as one (`vidette eval A B C`, ids prefixed with the set's name);
  the report adds false alarms and misses per day (text and JSON). First numbers on a
  test deployment in docs/eval.md.
- **Calibration from 👍/👎** ([docs/calibration.md](docs/calibration.md),
  [ADR-0020](docs/architecture/adr/0020-calibration-from-marks.md)): per scenario of a
  policy and per camera, after 4 counted 👎 and no 👍 within 14 days, one step quieter:
  first the alert wakes only when the judge is at least 90 % sure, then it goes to the
  evening digest only (still confirmed, listed as "in the digest by your marks"). "The
  recording froze", "Wrong camera" and "One of us" do not count; mostly "Right, but not
  important" skips the bar. A 👍 on an alert a step kept in the digest takes it back at
  once. Scenarios of a `wake: always` level (and `calibration.protect`) never go to the
  digest and get a bar of at most 75 %. Steps are stored in a new `calibration` table with
  their evidence (schema v7), never in the config; the bot says each one in one silent
  line with an Undo button, `/tuning` lists them, the System page has a Calibration card
  (`GET /api/v1/calibration`, `POST /api/v1/calibration/{id}/revert`), and `vidette policy
  calibration [undo ID]` works from the command line. 👍/👎 from the web page now also
  record a review and trigger calibration. Config: `calibration:` (on by default).
- **The bot in English** (`notifications.telegram.language: en | ru`, default `en`): every
  message, button, toast, prompt, the digest, `/help` and the assistant's quick answers
  come from one catalog per language (`vidette.telegram.i18n`, English the reference,
  Russian a full translation); a test keeps the catalogs complete with the same
  placeholders and fails on a Cyrillic phrase outside them. Free text from models (the
  judge's description, deep analysis, the assistant) follows the same language through
  the prompt unless `understanding.vlm.language` is set. Questions are understood in
  English and Russian whatever the setting. The assistant's tool results are plain
  English for the model.
- **Deep analysis** ([docs/deep.md](docs/deep.md),
  [ADR-0019](docs/architecture/adr/0019-deep-analysis.md)): `understanding.deep` gives the
  day's clips with people, faces or vehicles (and those the live judge never reached) a
  second look by the judge's model: one request with up to eight frames and no policy
  that describes what happens, then a text-only request that decides under the policy
  from that description, in the owner's language. The result is stored next to the live
  verdict (`events.deep`), compared with it (agree, disputed, uncertain, only deep);
  disputed and uncertain clips become `status: draft` entries in their dataset with both
  verdicts in `notes`. A persistent queue (`deep_jobs`) with priorities, retries, a
  night window (01:00-07:00); it uses every moment the slot is free, and outside the
  window takes a turn of its own after `trickle_every` without a finished clip; no daily
  cap unless `max_clips_per_day` is set. One model slot per server for the whole process
  (`vidette.vlm.gate`): the live judge and the assistant go first and take the slot back
  from a deep request; before each request the server's
  `/slots` must show an idle slot, else it backs off. The evening digest quotes deep
  descriptions and lists what only the deep look flagged. `vidette deep status | show`.
- **Identity merge pass** ([ADR-0018](docs/architecture/adr/0018-identity-merge-pass.md),
  [docs/people.md](docs/people.md#the-merge-pass-one-person-found-later)): identities that
  are certainly one person are joined after the fact: the same moment on one camera
  (faces 0.85 alike within 10 minutes) or clear faces that match like a live join with
  no closer rival. Complete linkage; never across one clip, a person's split or undo, two
  marked identities, a known person, or the household/stranger line. Audited (`merges`),
  reversible (`--unmerge`, `--undo RUN`, sightings kept apart afterwards), a backup
  before every pass that merges; old numbers resolve to the identity they went into.
  `vidette people merge` (dry run by default) prints the score distribution behind the
  thresholds; `vidette serve` runs it after new sightings (`people.identities.merge`).
- **Quieter alerts that can be argued with**
  ([ADR-0017](docs/architecture/adr/0017-subject-frames-other-cameras-and-questions.md)):
  an alert's album holds only frames that show its subject (the best moment of each face,
  then the frames the judge says show what its verdict is about; at most `frames`, now
  3), not four evenly spread frames of an empty street. The judge names those frames
  (`subject_frames`), and a scenario that alerts with no frame showing it is an
  abstention, not an alert. The prompt says that ordinary use (getting into a car,
  unlocking a door) is not an attempt, that an alerting scenario needs the act in the
  frames, and to describe what people do rather than a guessed gender, age or
  ethnicity. 👎 offers reasons (passed by, own car, ours, animal, vehicle, light, stalled
  recording, wrong camera, right but not worth it, other with a typed answer), kept on the
  event (`intent.review`) and as the dataset entry's new `reason`; after any answer the
  buttons shrink to one that says what was chosen, and tapping it takes the answer back
  exactly (feedback, label, face decision).
- **The other cameras at the moment of an event**
  ([docs/other-cameras.md](docs/other-cameras.md)): every clip-folder clip overlapping
  15 s before to 45 s after the moment a person was first seen, on every camera, found on
  demand; "the same person" only when an identity or an enrolled person is in both face
  reports; `cross_camera.lag_up_to` for cameras whose video arrives late (measured 0 to
  100 s for a battery camera behind a base station, 0 to 4.5 s for cameras that stream all
  the time), their times marked approximate. One mosaic video in real time (landscape
  cameras stacked, a portrait camera beside them, grey where a camera recorded nothing),
  ffmpeg only, split under the size limit. Web event card, «📹 Другие камеры» under an
  alert, `GET /api/v1/events/{id}/linked` and `…/linked.mp4`.
- **Questions in plain words, and MCP** ([docs/assistant.md](docs/assistant.md)): free
  text from an allowed chat (a reply to an alert makes it the context) goes to a small
  tool-calling assistant: find events, an event's details, the clips at a moment, where an
  identity was seen, send the mosaic, mark an event, today's digest. The judge's local
  endpoint by default (native tool calls, a JSON-action prompt for servers without them),
  `assistant.backend: cloud` opt-in and never given a name, a face or a frame. The three
  commonest questions skip the model. The read-only tools at `POST /api/v1/mcp` behind the
  API's auth.
- **Anonymous identities** ([docs/people.md](docs/people.md#strangers-who-come-back),
  [ADR-0014](docs/architecture/adr/0014-anonymous-identities.md)), opt-in
  `people.track_unknown` (off by default): a readable unknown face of at least 64 px that
  is not close to anyone enrolled joins the identity it matches (SFace cosine ≥ 0.50 and
  0.06 clear of any other) or starts "stranger #N"; never named unless someone types a
  name. The judge's face line says how often and where ("this is the 3rd time today;
  cameras in the last hour: …"), counts only for a cloud judge; `recurrence:` on a policy
  with levels raises a lower scenario to `raise_to` after N sightings within a window or
  on several cameras within minutes, and on any sighting of an identity marked
  suspicious. Telegram: «незнакомец №12, 3-й раз за сегодня»; "Это свой" and "This is …"
  promote the identity to a known person. Web: People → Identities with every sighting
  and its clip; rename, mark suspicious or familiar, merge, split, promote, delete. One
  SQLite file and one crop per sighting in `~/vidette-data/identities` (0700/0600,
  refused in a git tree, `secure_delete`), 30-day retention for unnamed, unflagged
  identities; `vidette people identities list | backfill | expire | delete`. No
  appearance attribute anywhere.
- **Every face, its best moment, answers that can be taken back**
  ([ADR-0016](docs/architecture/adr/0016-every-face-reversible-decisions.md)): with
  `track_unknown`, everyone whose face is found gets an identity (readable faces from
  40 px join or start one; faces too small to compare start a weak one, merged by hand;
  a face close to someone enrolled gets one marked so, and never raises a level). A
  second look around every face (6 frames a second, at most 24 more frames) keeps the
  sharpest, most frontal moment as the crop and embedding: on ten clips with faces, 142
  readable faces instead of 40 and a sharper crop in 8 of 10. Every answer about a face
  records what it changed and can be changed or taken back (web: Change, Undo,
  Familiar…, New identity…, Add to…; Telegram: «Кто это?» with Свой, Знакомый, Новый
  знакомый…, Новая личность…, Чужой, Отменить решение, a name by reply). "Familiar"
  (formerly "ignored") counts like the household for `unless_known` and `known_people:
  quiet`; the critical list still wakes. Identities: likely duplicates with one-tap
  merge, bulk merge, moving one sighting, taking a promotion back.
- **Telegram bot** ([docs/telegram.md](docs/telegram.md),
  [ADR-0013](docs/architecture/adr/0013-telegram-bot.md)): `notifications.telegram` in
  `vidette serve`, long polling only (no webhook, no open port). An alert is an album of
  key frames and the clip (≤ 20 MB, else a link) plus a text with the camera, time, the
  judge's description and who the face check recognized, in the chat's alerts topic, once
  per event and chat; one camera's alerts within 5 minutes edit the first message.
  Buttons 👍 / 👎 / "Это свой" / "Не знаю" record the event's feedback, a reviewed label in
  the clip's dataset (`labels.yaml`, through the `vidette label apply` code) and, for
  "Это свой", a face confirmation. An hourly silent digest of everything that did not
  wake anyone, with the reason for events without a verdict; `/status`, `/live` (says
  live view will be part of the Vidette app), `/quiet`. Only allowed chats get any
  answer; `recipients: first` for a trial; `names` off by default. `vidette telegram check | try CLIP` checks the setup and
  sends one real clip as a test alert. launchd template
  [deploy/macos/dev.vidette.serve.plist](deploy/macos/dev.vidette.serve.plist).
- **Policy levels**: `policies[].levels` (highest first), each with scenarios and a `wake`
  rule (`always`, `unless_quiet`, `unless_known`, `never`). The judge names one scenario
  (an `enum` in the JSON schema) and how many people it saw; the level decides whether the
  event is an alert and who wakes. `understanding.vlm.language` for the description.
- **`vidette serve --env-file`**: `KEY=VALUE` files for `${VAR}`s, read as plain
  assignments (not shell), never printed; a file readable by others is reported.
- **Known people** ([docs/people.md](docs/people.md),
  [ADR-0012](docs/architecture/adr/0012-known-people.md)): opt-in local face recognition
  for enrolled people (`pip install 'vidette[people]'`; YuNet MIT + SFace Apache-2.0 from
  the OpenCV model zoo, pinned commit, sha256-checked, downloaded only by the CLI). A face
  check over each clip's frames gives the judge one line of facts (names for a local model
  only), counts unreadable faces as "no clear face", never as strangers, and is stored with
  the clip or event. `known_people: quiet` on a policy; `vidette people models | add |
  list | match | scan | confirm | sheet | remove`; `vidette label draft --people` and
  contact-sheet replies `5 Sam` / `5.2 stranger`; `vidette eval --people
  --known-people`; a People page and confirm buttons on event cards. The people folder is
  refused inside a git work tree and kept 0700/0600.
- **Repository privacy checks**: `.github/scripts/privacy_check.py` (credentials in camera
  URLs, private addresses outside documentation ranges, images or video outside the
  folders allowed to hold them and never under `faces/` or `people/`, personal e-mail
  addresses) and gitleaks with a pinned, checksum-verified binary, in CI; the same as a
  local hook (`make hooks` or `.pre-commit-config.yaml`). `.gitignore` covers datasets,
  face crops, model weights and camera URL files.
- **Frigate source**: review items, tracked objects and availability over MQTT, parsed
  leniently into source-agnostic incidents; camera and severity filters; reconnect backoff;
  malformed payloads counted and reported. Read-only HTTP client for review items,
  snapshots, recording frames and clips. Built against the payloads documented for Frigate
  0.18; reviews need 0.14 or newer.
- **Events from Frigate**: one `observed` event per review item with a cached snapshot, a
  clip fetched on request, and Frigate's own severity and GenAI summary. `observed` never
  notifies.
- **`vidette eval`**: labelled dataset format, judge interface, four model-free baselines,
  precision and recall with 95 % Wilson intervals, false alarms per day, VLM cost
  projection, `--scaffold` for folders of exported clips, text and JSON reports. A
  five-entry synthetic sample in `datasets/`.
- **`vidette frigate check | tail --record | replay`.**
- **`vidette collect`** ([docs/collect.md](docs/collect.md)): waits for a battery camera's
  RTSP stream (Eufy NAS RTSP) and keeps each wake period as a dataset clip with 4 to 8 key
  frames and a JSON sidecar. TCP port check while the camera sleeps, one client, clean
  TEARDOWN on every stop, backoff on 404 and silent sessions; hard cap on the folder
  (default 5 GB) and a free-space floor (default 10 GB); credentials only from the
  environment or `~/.config/vidette/rtsp-url`, scrubbed from every line it prints;
  launchd template for macOS.
- **VLM judge** ([docs/vlm.md](docs/vlm.md)): one OpenAI-compatible client, two
  backends the user chooses between: `local` (Ollama by default) and `cloud` (presets for
  OpenAI, Gemini's OpenAI-compatible endpoint, OpenRouter, or any custom URL). Frames
  spread over the clip and shrunk to 768 px, JSON-schema answers (with fallbacks for
  servers that lack them), `unsure` as an abstention, a hard daily call budget, a
  per-minute pace, timeouts, and latency, tokens and cost (from prices you configure, never
  defaulted) on every verdict.
- **`vidette eval --judge vlm-local --judge vlm-cloud`** compares judges side by side with
  a measured cost and latency table and abstentions by reason; VLM judges run only when
  named. Machine-drafted labels (`status: draft`) are excluded from scores until reviewed
  (`--include-drafts` scores them, loudly).
- **`vidette label`** ([docs/label.md](docs/label.md)): `draft` gives every new clip a
  numbered entry with the model's proposal (scenario from the set's vocabulary,
  alert/silent under the first policy, one line, confidence) and draws PNG contact sheets
  (8 to 12 clips, large numbers, four key frames each, pure Python, no imaging library);
  `apply` takes a reply such as `3 courier, 7 alert, rest ok` (Russian too), refuses
  anything ambiguous as a whole, and rewrites only the blocks it numbered; hand-written
  parts of `labels.yaml` stay byte for byte. `observed_hours` follows the collector's
  watched time.
- **Verdict worker**: each closed incident on a camera with a policy is judged by the
  selected VLM backend; an alert verdict (above the policy's sensitivity floor) moves the
  event to `confirmed` and notifies, a silent one to `dismissed`; abstentions leave it
  `observed` with the reason. Only incidents that ended in the last 15 minutes are judged,
  never the same event twice.
- **System page: Verdicts card** with an Off / Local / Cloud switch (stored in the
  database, wins over the config), the model, endpoint and where frames go for each, the
  day's calls against the budget, and a confirmation before switching to Cloud.
  `GET/PUT /api/v1/settings/vlm`. Clip folders show on the System page.
- **Clip folders as an incident source** ([ADR-0010](docs/architecture/adr/0010-clip-folders.md),
  [docs/clip-folders.md](docs/clip-folders.md)): `clip_folders:` turns each new clip in a
  folder into an incident and an event with a snapshot and in-place clip playback, so the
  pipeline runs without Frigate.
- **`vidette collect --continuous`** ([docs/collect.md](docs/collect.md#continuous-mode),
  [ADR-0011](docs/architecture/adr/0011-continuous-collect.md)): for a camera that streams
  all the time (or for every wake period of one that does not). One ffmpeg and one RTSP
  session feed a ring of 60 s stream-copied segments (default 3 GB, 24 h) and a motion
  gate (4 fps, 256×144 grey frame differences counted in pure Python, VideoToolbox decode
  on a Mac: about 5 % of a core for 1080p at 15 fps). Each motion event becomes a clip
  with 3 s of pre-roll and post-roll, cut from the ring by stream copy and finalized like a
  wake-period clip (MP4, key frames, sidecar with the gate's numbers), so `vidette label`
  and clip folders read it unchanged. The date/time overlay is masked by default;
  `--mask` adds regions. Disk priority: free-space floor, then clips, then the ring, which
  yields first.
- **`vidette collect --rotate 90|180|270`**: key frames turned upright for a camera on its
  side, the MP4 tagged with display rotation, pixels untouched.
- **`vidette collect --base-station`**: behind a base station such as a Eufy HomeBase a 404
  means the camera is asleep (the base station answers quickly and does not wake the camera), so it
  is checked every 5 s instead of backing off to 5 minutes. Opt-in; the default is
  unchanged.
- **Stream health per clip** ([docs/collect.md](docs/collect.md#stream-health)): the
  sidecar's `health` block, from packet times alone (no decoding, about 0.1 s a clip):
  frames, pauses over 0.5 s and the time they cover, the longest one, frames that came in
  a heap after a pause, the longest pauses as `[start, end]`. A pause of 2 s or more, or
  pauses covering a tenth of the clip, mark it interrupted.
- **Frames where the picture changed, with their times**
  ([docs/vlm.md](docs/vlm.md#what-the-judge-sees-and-answers)): from a clip, the judge
  gets real frames picked by packet, two per episode of change (found on 64×36 grey
  thumbnails of the key frames, no full decode), each frame's time in the recording, and
  every stall between two frames from the clip's stream health; the system prompt says a
  person who changes place is walking and that how long someone stayed is read only from
  frames without a stall between them. Frames are chosen once per event for all policies,
  kept under `media_dir/judge-frames/`, and become the Telegram album. Levels answers carry
  `first_person_frame` (`person_at_s`). The bot says «запись прерывалась» in the alert and
  the digest when the recording paused.
- **Early alerts** ([docs/telegram.md](docs/telegram.md#early-alerts-before-the-clip-ends)):
  `vidette collect --early 8s` copies the first seconds of a wake-period recording, or of
  a continuous mode's motion event, to `.early/` while it goes on (half-second Matroska
  clusters, flushed at once, make the part file and the open ring segment readable);
  the clip folder source publishes it within a second as a partial incident of the clip
  to come; an early alert goes out at once and the whole clip, judged under the same
  event, edits that message (the clip replaces the album's last item, the verdict is
  updated, a higher level buzzes once more, "not an alert after all" says so). An early
  "nothing" never hides a later alert.
- **Where the time went**: every event keeps `intent.timeline` (camera woke or motion,
  first frame, person seen, early window, clip closed and picked up, judge queued, started,
  finished) and an alert its `sent_at`; `/status` shows the stages of the last alert.
- The VLM judge sends a prompt the server refused for its context size once more with
  the same frames 20 % smaller.
- **`vidette collect --prune-oldest`**
  ([docs/collect.md](docs/collect.md#pruning-the-oldest-clips---prune-oldest)): a folder
  past 95 % of `--max-size` loses its oldest clips, each with its sidecar, key frames,
  face crops and early window, until it is under 90 %, instead of stopping the camera.
  Never pruned: labelled entries (any status), events of `vidette serve` that alerted or
  were marked (read-only from its database, via `--serve-config`), clips on any camera
  from 1 min before to 3 min after such an event, clips with face sightings or face
  decisions, and clips younger than `--prune-min-age` (24 h). Anything unreadable means
  no prune and the old stop, with the reason in the log. Off by default.
- **launchd template for a second camera**:
  [deploy/macos/dev.vidette.collect.camera2.plist](deploy/macos/dev.vidette.collect.camera2.plist).
- `server/uv.lock`; CI installs from it (`--locked`).

### Changed
- **README** describes v0.2 as it works today: incidents from Frigate or clip folders,
  `vidette collect` (wake on motion, continuous, early windows, `--prune-oldest`), the
  judge with policy levels (local or cloud), the Telegram bot with feedback and
  questions, known people and anonymous identities, the other cameras, `vidette label`
  and `vidette eval`; a quickstart for a camera without Frigate; what is not built yet
  marked as roadmap. The badges say v0.2 is a development line and that the release
  badge shows the last release (v0.1.3); CI's badge follows `v0.2`.
  [Getting started](docs/getting-started.md) no longer says verdicts do not exist.
- **No hourly Telegram digest.** `notifications.telegram.digest_at` (21:00) sends the
  day's digest once, silently, only when something needs a look (confirmed events that
  woke nobody, unanswered alerts), in plain lines; `/status` gives the same for today so
  far. The day is recorded before sending, so a restart never repeats it.
  `digest_every` and `digest_settle` are ignored with a `vidette validate` warning.
- `vidette collect` keeps the camera's own clock (RTP timestamps) behind a restreamer too,
  with a jump guard on the input (`setts`): a packet more than 10 s after the previous
  one, or not after it, continues one frame later, so go2rtc splicing a reconnect no
  longer ends a clip at once or cuts the ring at every key frame. Arrival-time stamps
  turned delivery jitter into frozen pictures (measured: arrival pauses up to 13 s on a
  weak link, while the camera's timestamps of the same packets were even). The clip's
  `--max` is enforced by the collector from ffmpeg's progress (ffmpeg's `-t` reads the
  timestamps before the guard; a clip may run up to a second over).
  `--wallclock-timestamps` stays, no longer recommended.
- `vidette collect --continuous` cuts a clip as soon as the ring holds its end, reading the
  open segment, instead of waiting up to one segment (a minute) for it to finish.
- Clip folders take a `vidette collect` clip as soon as its sidecar is there (written
  last) instead of after `settle`, and list such a dataset every 2 s: settle and poll
  had added 14 s before the judge saw a clip.
- The verdict worker judges the newest waiting incident first; a backlog on a slow or
  shared model delays old clips, not the current one.
- A clip folder's incident id is the clip's full path: two folders that cut a clip in the
  same second no longer share one event. In a `vidette collect` dataset only `clips/` is
  read; the continuous mode's lookback ring (`ring/*.mkv`) never becomes an incident.
- Examples use documentation address ranges.
- Config: `understanding.vlm` is now `backend: none | local | cloud` with `local:` and
  `cloud:` sections (the v0.1 `provider`/`send` keys are rejected with a pointer).
- Config: new `frigate:` section; `cameras:`, retention, compaction, off-site, detector,
  tracker, faces and embeddings sections removed (a v0.1 config fails with a pointer to
  ADR-0009). Home Assistant discovery announces Frigate's cameras.
- Web app: Events (default), People (with Identities) and System pages; the header fits a
  phone screen.
- CI actions moved to node24 releases; the image no longer installs ffmpeg; compose builds
  from the checkout and drops the go2rtc sidecar.
- Release workflow: the `latest` image tag moves only to the newest stable tag
  (`v0.2.0-rc.1` publishes just `0.2.0-rc.1` and a GitHub pre-release; `v0.2.0` moves
  `latest`; a `v0.1.x` hotfix after it does not take `latest` back). Docker and release
  actions moved to their node24 majors.

### Removed (frozen at tag `nvr-core`)
- Recording, retention janitor, export, previews; go2rtc management and keep-warm holders;
  motion gate, YOLOX detector, IoU tracker and the Tier-2 event engine; RTSP and ONVIF
  adapters; camera, stream, recording and export API routes; Live, Review, Cameras and
  Setup pages. Dependencies `numpy` and `onnxruntime`.

### Fixed
- **A Telegram outage no longer loses the evening digest or the alerts.** A digest
  Telegram did not take (no connection, `429`, `5xx`) is tried again with a growing pause,
  at once when the bot hears Telegram again, until `notifications.telegram.digest_until`
  (23:30), and never twice: one that may have arrived is not repeated. Alerts Telegram
  could not take wait in the database and go when it answers again, marked as late with
  the time they were due, for `late_alerts_for` (2 h); older ones are counted in one line
  and listed in the digest. Edits of alerts wait the same way. After 3 minutes without
  any answer the HTTP client is replaced by a new one (a long-running service once failed
  to connect for over an hour while the machine reached Telegram, and a restart fixed
  it). Every Bot API error now says whether the request may have arrived
  ([docs/telegram.md](docs/telegram.md#limits-and-failures)).

## [0.1.3] — 2026-07-09

Two live-tile fixes chasing the burst-publishing camera from v0.1.2 (its RTSP stream
appears and disappears with the camera's own recording sessions).

### Fixed
- **MSE could wait forever:** a stream WebSocket that opens but never delivers media left
  the tile in "connecting" indefinitely — it now falls back to snapshots (with the
  server's diagnosis) after 15 s without playback.
- **Live view never came back on its own:** a failed tile now quietly retries the live
  transports every 30 s, so live resumes the moment the camera publishes again — no
  manual Retry needed.

## [0.1.2] — 2026-07-09

Round two of field fixes for battery cameras, this time on permanent
power. Root cause of "camera doesn't load at all": Eufy serves exactly **one RTSP client**
at a time and holds dead sessions after unclean hangups, so every gateway connection churn
risked a minutes-long `404 Stream Not Found` lockout; meanwhile the recorder died on
startup probing and the v0.1.1 sleep backoff (then unconditional) kept it down for
5-minute stretches.

### Fixed
- **Recorder could not start against a degraded restream:** ffmpeg joined the gateway
  stream while its camera producer was still (re)connecting, gave up probing before any
  SPS/keyframe arrived and died with "dimensions not set / Could not write header" —
  12 consecutive deaths observed. The record command now probes up to 10 s
  (`-analyzeduration/-probesize`).
- **5-minute stall backoff applied to mains cameras:** the sleepy-camera protection from
  v0.1.1 is now opt-in via `power_profile: battery`; the default (`mains`) retries every
  failure within 30 s and never claims the camera is "probably sleeping".
- **Orphaned footage invisible after a crash:** segment files written but not yet indexed
  when the process died are now adopted at boot (real duration probed); truncated tails
  (no moov atom — unplayable) are removed instead of leaking disk forever.

### Added
- **`power_profile` per camera** (`mains` default | `battery`), in YAML and as a
  "Battery-powered camera" toggle in the camera form.
- **Keep-warm stream holders:** for mains cameras the server pins one lightweight
  consumer to the gateway so the camera connection never churns (no more single-client
  RTSP lockouts) and live view attaches instantly. Visible under `keepwarm` in
  `GET /api/v1/system`.
- **Live tiles explain themselves:** when a tile falls back to snapshots it now shows the
  recorder's actual diagnosis (`last_error`, also new in `GET /api/v1/cameras`),
  refreshed while degraded.

## [0.1.1] — 2026-07-09

Field fixes for battery cameras served over NAS-RTSP.

### Fixed
- **Data loss on hard VM kill:** SQLite now runs `synchronous=FULL` in WAL mode (every
  commit fsynced) and the WAL is checkpointed each janitor tick and on shutdown. Before:
  commits lived only in an unsynced WAL — macOS sleep hard-killing Docker Desktop's VM
  discarded them, including the admin account (hence the re-appearing setup wizard).
- **Sleeping-camera hammering:** a camera that stalls repeatedly (battery model asleep) now
  backs off up to 5 minutes instead of reconnecting every ~45 s — which kept the camera
  awake, drained its battery, leaked one orphaned gateway session per cycle (40 in
  10 minutes observed), and spammed `recorder.stalled` events (now rate-limited). Status
  text says the camera is probably sleeping and that recording resumes automatically.
- **Live view in containers:** WebRTC's SDP answer only carried STUN-discovered public-IP
  candidates with ephemeral ports — unreachable from a LAN browser, so tiles fell back to
  snapshots. Two-part fix: an **MSE transport** (fMP4 over an authenticated same-origin
  WebSocket, relayed to go2rtc — works in every topology) as automatic fallback, and a
  `server.webrtc_candidates` / `VIDETTE_WEBRTC_CANDIDATES` setting to advertise a LAN
  address for direct sub-second WebRTC.

### Added
- Player keep-alive pool: navigating away parks the connected stream for 60 s, so
  returning to Live re-attaches instantly instead of renegotiating.
- Tile status now names the transport (`live · webrtc` / `live · mse`).

## [0.1.0] — 2026-07-09

First tagged release: Watch (M1) + Detect (M2) complete, published as a multi-arch container
image on `ghcr.io/baadev/vidette`. See the highlights below and the GitHub release notes.

### Added — M2 completion + camera management UI
- **Camera management beyond YAML:** UI-created cameras live in the database and merge into
  the effective config at boot and on change (hot-apply restarts capture briefly); the
  hand-written YAML remains the IaC source of truth and is never rewritten — id collisions
  resolve to the file with a warning. CRUD API under `/api/v1/config/cameras` (+ ONVIF
  `discover`, per-camera `probe`), Cameras page in the web app, and an SVG zone editor
  (file-defined cameras get a copy-paste YAML snippet instead of silent file edits).
- Web push (VAPID): self-hosted keys (generated once, stored in the DB), subscription API,
  PWA manifest + service worker, subscribe/unsubscribe from the System page; expired
  subscriptions pruned on 404/410.
- MQTT + Home Assistant discovery: availability (retained + LWT), per-camera person
  occupancy, full event JSON, system events; reconnect with backoff; deliberately no
  per-frame motion chatter.
- Live WebSocket event stream (`/api/v1/ws`) with topic filters; the Events page updates
  live and falls back to polling while disconnected.
- Prometheus `/metrics` (hand-rolled exposition, bearer-token scrape): pipeline/recorder/
  disk/notification/bus/event series.
- Event favorites: star in the UI, `favorite=true` filter, and footage upgraded to the
  `favorite` retention class (unstar returns it to the `event` class).
- Event engine hardening: snapshot retry for open confirmed events (gateway-warmup race
  observed live), and confirmed events now upgrade their footage to the `event` retention
  class on close.
- Duration values round-trip through `model_dump(mode="json")` ("3d", not ISO-8601 "P3D").

### Added — M2 "Detect" (core)
- The understanding cascade, tiers 0–2: ffmpeg substream decoder → pure-numpy motion gate
  (EMA background, day/night damping) → YOLOX-tiny detector via ONNX Runtime (Apache-2.0
  model, sha256-pinned download, CoreML/CPU providers, lazy load with loud motion-only
  degrade) → two-stage IoU tracker with zone algebra (approach/dwell/touch/loiter/
  repeat-pass; `public` zones suppress passers-by before any alert logic).
- Event engine: one open event per camera, sensitivity-scaled promotion (policy geometric
  skeleton), snapshots at promotion, lazy clip materialization (remux), `event.confirmed` /
  `event.ended` on the internal bus; events API (list/get/snapshot/clip/feedback) replaces
  the M2 `501` stub; Events page in the web app (feed, chips, clips, 👍/👎).
- Notifications: dispatcher wired to the bus with `when:` rules; signed webhooks per the
  published contract (HMAC, timestamp-refreshing retries, stable delivery id) and Apprise
  channels (Telegram/Discord/100+); delivery failures become rate-limited system events and
  can never notify about themselves (loop guard).
- System events now mirror onto the bus under `system.*` so notification rules match the
  documented patterns.

### Added — M1 completion
- ONVIF adapter (beta): WS-Discovery (`vidette discover`), SOAP profiles → main/sub stream
  selection, WSSE PasswordDigest with HTTP-digest fallback, actionable probe diagnostics.
- Timeline scrub-strip previews: per-hour 1 fps strips generated in the background, served
  via `GET /api/v1/recordings/preview`, hover-scrub UI in Review.
- First-run wizard step 2: camera checklist with live probe/snapshot verification (Setup page).

### Changed
- **Eufy: NAS (RTSP) is the only integration path.** The planned bridge adapter over
  `eufy-security-ws` was removed before shipping — Anker's backend migration shut down the
  legacy API the community client relied on, so a bridge is not technically viable today.
  Eufy models with the built-in NAS (RTSP) feature connect through the plain `rtsp` adapter;
  `docs/cameras/eufy.md` was rewritten accordingly, and `vidette validate` now answers
  `adapter: eufy` with a hint pointing at the guide. (Roadmap rows for bridge live view,
  vendor events and HomeBase clip ingestion were withdrawn — marked blocked, not pretended.)

### Added — M1 "Watch" (in progress)
- Recording pipeline: managed go2rtc gateway (config generated from `cameras:`, atomic
  writes, hot reload, health), codec-copy fMP4 segment recorder with crash-only supervision
  (watchdog, exponential backoff, hour-rollover safety), SQLite segment index.
- Storage operations: retention runtime (per-camera overrides, pressure deletions that never
  touch events/favorites), disk watermarks + write probes, all failures as loud system events.
- Range export: remuxed MP4 jobs (single worker, path-safety checks) via API and UI.
- Auth: first-run admin bootstrap (no default credentials), scrypt password hashing,
  session cookies, scoped API tokens, uniform login errors with backoff.
- REST API v1: auth, cameras (with live recorder state), recordings + hourly summary +
  segment files, export jobs, stream endpoints with an authenticated WHEP proxy and snapshot
  proxy (go2rtc admin API stays private), system events.
- Web app: login + first-run wizard, WebRTC live wall with snapshot fallback and keyboard
  navigation, review page (hour strip, segment playback, MP4 export), system page.
- SQLite store (WAL) with append-only migrations; `aiosqlite`; single-writer discipline.
- Dockerfile ships ffmpeg; compose publishes only the WebRTC media port.

### Added — M0
- Project genesis (M0): architecture documentation and ADRs, selling README, roadmap with
  status legend, contribution/security/Claude guides, growth strategy and brand docs.
- Executable configuration schema (pydantic) with `${ENV}` interpolation, validator CLI
  (`vidette validate`) and API endpoint (`POST /api/v1/config/validate`), covered by tests.
- Typed protocols for camera adapters, pipeline tiers and notifiers; in-process event bus;
  retention planner (pure, tested); HMAC webhook signing (tested).
- FastAPI skeleton: `/healthz`, `/api/v1/system`, honest `501 designed` stubs for M1/M2 surface.
- Web app shell (React + Vite, dark theme) with live health status.
- Docker Compose stack: vidette + go2rtc sidecar, optional `vlm` profile (Ollama).
- CI: ruff, mypy (strict), pytest; web typecheck and build.
