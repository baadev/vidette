# Telegram: alerts with buttons that teach, the day's digest, three commands

The Telegram bot is how a verdict reaches a phone. An alert is an album (the frames that
show the person or the thing it is about, and the clip) and a short text: the camera, the
time, what the judge saw, who the face check recognized, and four buttons. Each tap is
recorded as feedback on the event and, for clips in a dataset folder, as a reviewed label
for `vidette eval`; a 👎 asks why. In the evening, a short digest of the day when
something needs a look, and on request any time. Design and guardrails:
[ADR-0013](architecture/adr/0013-telegram-bot.md),
[ADR-0017](architecture/adr/0017-subject-frames-other-cameras-and-questions.md).

Status: 🚧 everything below is built and tested against a fake Telegram API (no network);
run live with clip-folder cameras and a local judge. No accuracy figure exists for any policy; that needs `vidette eval` on labelled
clips, which the buttons now help collect.

## Set up

1. **Create a bot** with [@BotFather](https://t.me/BotFather) and keep its token out of
   the config. If you want topics ("Alerts", "Digest") in a private chat, turn on topics
   for the bot in BotFather and create them in the chat.
2. **Find the chat ids** of the people who may use the bot (each person writes to the
   bot once; `getUpdates` shows `chat.id`), and the topic ids (`message_thread_id`).
3. **A `KEY=VALUE` file, readable by you only:**

   ```bash
   cat > ~/.config/vidette/telegram.env <<'EOF'
   VIDETTE_TELEGRAM_BOT_TOKEN=<token from BotFather>
   VIDETTE_TELEGRAM_ALLOWED_CHATS=111111111,222222222
   VIDETTE_TELEGRAM_TOPICS=111111111:alerts=2,digest=3;222222222:alerts=2,digest=3
   EOF
   chmod 600 ~/.config/vidette/telegram.env
   ```

   `vidette serve --env-file` reads it as plain assignments, not shell: `;` and `$` in a
   value are kept as they are. Variables already set in the environment win.
4. **The config.** Keep the service's config in its own file (here
   `~/.config/vidette/serve.yaml`): the command-line tools read
   `~/.config/vidette/vidette.yaml` by default, and a `${VAR}` they cannot resolve stops
   them.

   ```yaml
   notifications:
     telegram:
       token: ${VIDETTE_TELEGRAM_BOT_TOKEN}
       allowed_chats: ${VIDETTE_TELEGRAM_ALLOWED_CHATS}   # "id,id" or a YAML list
       topics: ${VIDETTE_TELEGRAM_TOPICS}                 # "<chat>:alerts=<t>,digest=<t>;…"
       recipients: first      # first: a trial, only the first chat gets alerts; all: everyone
       language: en           # en (default) | ru
       names: false           # true: "one of us: Sam" and the name buttons (privacy below)
       camera_names: {front_door: Front door}
   ```

5. **Check, then send one test alert** on a real clip (the judge and the face check run as
   they would live; the message is marked as a test and says where the routing would have
   put it):

   ```bash
   cd ~/development/vidette/server
   uv run vidette telegram --config ~/.config/vidette/serve.yaml \
     --env-file ~/.config/vidette/telegram.env check
   #   bot      @your_bot
   #   chats    2 allowed, 1 get alerts (recipients: first)
   #     chat 1: gets alerts; alerts topic set, digest topic set
   uv run vidette telegram --config ~/.config/vidette/serve.yaml \
     --env-file ~/.config/vidette/telegram.env try ~/vidette-data/porch/clips/20260115-101500.mp4
   ```

   Its buttons are answered by the running `vidette serve`, which shares the database.
6. **Run the service.** On a Mac, [deploy/macos/dev.vidette.serve.plist](../deploy/macos/dev.vidette.serve.plist)
   runs `vidette serve` under launchd with the env file; its header has the install, stop
   and log commands.

## Levels: who wakes for what

A policy with `levels` asks the judge to name the one scenario that fits (or `other`);
the scenario's level decides whether it is an alert and who is woken. Highest level first.

```yaml
policies:
  - name: home
    description: A private house with a few cameras around it.
    levels:
      critical:
        title: critical                # shown in the alert
        wake: always                   # even in quiet hours, even for people we know
        scenarios:
          touching_equipment: someone touches or opens the electrical panel or a camera
          door_or_gate_attempt: someone pulls or tries to open a door, a handle or the gate
          car_tampering: {title: car doors, description: someone pulls the doors of a car}
      elevated:
        title: person at the house
        wake: unless_known             # not when only known people are there
        scenarios:
          person_at_house: >-
            a person who stops at the house: comes into the yard, stands or waits at the
            gate or a door, rings or knocks; seen in place in frames several seconds apart
            with no stall between them. Someone who keeps walking past is passer_by
      none:
        wake: never                    # not an alert; the vocabulary for the digest
        scenarios:
          passer_by:
            title: passers-by
            description: >-
              walks past on the street or the sidewalk, even close to the door or the gate,
              without stopping and without touching the door, the gate, a window, the
              electrical panel, a camera or a car
          household_member: someone who lives here doing ordinary things
```

Write the levels the way the judge must tell them apart. A camera that looks onto a
street sees many people pass close to the house: say in the policy and in the passer-by
scenario that walking past, even close, is not the elevated level, and make the elevated
scenario ask for a person *stopping*. The judge is told each frame's time and where the
video stalled ([vlm.md](vlm.md#what-the-judge-sees-and-answers)), so "stays in place"
means frames several seconds apart without a stall between them, not a frozen picture.
Critical scenarios stay what they are whoever does them: the elevated level is for
people the face check did not recognize (`unless_known`), the critical one wakes for
anyone.

A step of [calibration](calibration.md) can send a scenario's alerts on one camera to the
digest (all of them, or those the judge is less than 90 % sure of); never for a `wake:
always` level.

| `wake` | Now, in the alerts topic | Otherwise |
|---|---|---|
| `always` | always, also during `/quiet` and when the person is known | |
| `unless_quiet` | unless `/quiet` is on (a policy without levels alerts like this) | digest |
| `unless_known` | unless `/quiet` is on, or the face check recognized everyone the judge saw | digest |
| `never` | never: the event is not an alert | digest |

"Recognized everyone" is narrow on purpose: at least one known face, no face that matched
nobody, and the judge's count of people no larger than the known ones. No face check, a
face too small to tell, or a second person whose face never turned to the camera all
wake someone. An `unless_known` event that went to the digest is still confirmed: it is
listed there with who it was.

The judge never sees the wake rules, only the scenarios and whether each level is an
alert. The bot's `language` (`en` by default, `ru`) is also the language the judge writes
its one-line description in, unless `understanding.vlm.language` names another (the rest of
the prompt stays English).

## Language

```yaml
notifications:
  telegram:
    language: en   # en (default) | ru: every message, button, the digest and /help
```

Every phrase the bot sends lives in one catalog per language
(`server/vidette/telegram/i18n.py`; English is the reference, a test keeps the others
complete). Policy level and scenario `title`s and `camera_names` are your own words and
are shown as written. Free text from models (the judge's description, deep analysis, the
assistant) follows `language` through the prompt. Questions to the bot are understood in
English and Russian whatever the setting. The examples below are in English; with
`language: ru` the same messages read «Тревога: критично», «👍 верно», «Кто это?».

## The alert

```
🚨 Alert: critical
📍 Front door, 21:14:03
👤 stranger
💬 A person in a hood pulls the door handle twice.
[👍 right] [👎 false alarm]
[Who is it?] [Not sure]
```

- Above the text, an album: up to `frames` (3) frames that show the subject, and the clip
  when it is at most `max_video_mb` (20 MB; Telegram's limit for bots is 50). The frames
  are the best moment of each face the check found (largest first), then the judge's own
  frames that it said show what its verdict is about; never an empty street. An event
  judged before this existed shows the judged frames from the first one with a person.
  A bigger clip becomes a link to `clip_url` (default `server.base_url`), which works
  only where that address does.
- The album arrives silently; the text with the buttons is what buzzes.
- One camera's alerts within `collapse` (5 min) at the same or a lower level edit the
  first message ("More on this camera: 2" and their times) instead of buzzing again; a
  higher level is a new alert.
- One alert per event and chat, enforced by the database.

The person line: `one of us: Sam` (with `names: true`, else `one of us`), `stranger` (a
readable face that matched nobody), `no people in view` (the judge counted nobody), `faces
not checked` (known people are off). With anonymous identities
([people.md](people.md#strangers-who-come-back)): `stranger #12, 3rd time today` (`first
time`, `4th time in 3 days`; the cameras when two or more saw it within the hour),
`⚠️ suspicious #12` for one you marked, `familiar: Neighbour` for a familiar person (the
name only with `names: true`), `stranger #40 (small face)` for a face too small to
compare, `no face found` when the judge saw someone whose face was never found, and
`· level raised: not the first visit` when a policy's `recurrence` rule raised
the level; no face crop of an identity is ever sent. A line
`📼 The recording paused: 38 pauses, up to 13 s` says the video paused (the clip's
[stream health](collect.md#stream-health)); the judge was told where.

### Early alerts: before the clip ends

A battery camera's clip ends when the camera goes back to sleep, often a minute or two
after the person has gone. With `vidette collect --early 8s`, the collector writes the
first 8 seconds of each recording to `.early/` while it goes on, and the clip folder
source picks that up within a second:

1. The early window is judged at once. If it is an alert at a level that wakes for
   anyone (`wake: always`), the alert goes out as usual (routing, quiet hours, collapse,
   one per event and chat), marked
   "From the first seconds of the recording"; its album holds the frames that show the subject and the
   first seconds as a video. Anything else, a lower level included, changes nothing yet:
   in testing, a person who seemed to stop in the first 8 s mostly walked on, and two of
   the nine alerts marked false in two days were such early alerts that the whole clip
   then called ordinary ([ADR-0017](architecture/adr/0017-subject-frames-other-cameras-and-questions.md)).
2. When the clip ends it is judged whole, under the same event, also after an early alert.
   - no early alert: the clip is judged like any other, so an early "nothing" never hides
     a later alert;
   - after an early alert, the same message is edited: the clip replaces the album's last
     item (or answers the alert when there was no album; a link when it is too big), the
     text shows the whole clip's verdict, "Checked against the whole clip";
   - a higher level from the whole clip also sends one short message answering the alert
     ("⬆️ Level raised: critical"), the only second buzz for an event;
   - not an alert after all: the text says "not an alert"; the event is dismissed;
   - no verdict for the whole clip: the early verdict stands.

How early the alert can be: the window (8 s) plus the judge's time plus a second or two.
On a shared judge that takes a minute per call it is still a minute; with a model of its
own, the target is 20 to 30 s from the person appearing.

## The buttons

Under a new alert: **👍 right**, **👎 false alarm**, **Who is it?** (when a face was found) and
**Not sure**. After an answer the buttons shrink to one that says what was chosen
("👎 walked past", "👤 familiar “Neighbour”"): tapping it takes that answer back exactly
(the event's feedback, the dataset label, the face decision) and brings the full buttons
again. No keyboard of six buttons is left on old alerts.

**Why was it false?** 👎 is recorded at once and the bot offers the reasons, the most
likely first for this event (a stalled recording offers "The recording froze" first, a
verdict about a car "Got into a car, theirs", a person at the house "Walked past"):

| Reason | Stored as | The clip's label |
|---|---|---|
| Walked past | `passing` | `passer_by` when the policy has it, else `other` |
| Got into a car, theirs | `car_use` | `car_use` (or `car_passing`) when the policy has it |
| One of us or familiar | `ours` | `household_member` |
| An animal, A car drove past | `animal`, `vehicle` | `animal`, `car_passing` |
| Light, shadow, rain; The recording froze; Wrong camera | `light`, `stalled`, `camera` | `other` |
| Right, but not important | `unimportant` | the judge's own scenario: it saw right, the alert was not worth it |
| Other: I'll write | `other` | `other`; the bot asks what it was, the reply is the entry's `notes` |

The reason is kept on the event (`intent.review`) and as the entry's `reason` in
`labels.yaml`; the digest counts the reasons ("False most often: walked past (5)").

| Button | The event | The clip's dataset (`labels.yaml`) | Known people |
|---|---|---|---|
| 👍 right | feedback `up` | `N <the scenario the judge named>`, reviewed | |
| 👎 false alarm | feedback `down` | `N other` (none of the policy's scenarios), reviewed | |
| Not sure | | `N ?`: unlabelled, reviewed, not scored | |
| Who is it? → One of us: a name | | `N household_member`, unless the level wakes `always` | the face is confirmed as that person (`vidette people confirm`); a face of an anonymous identity promotes the whole identity |
| Who is it? → Familiar: a name, New familiar…, New identity…, Stranger | | | the face goes into that identity, a new familiar or unknown one (the bot asks for its name: reply to its message), or stays a stranger with an identity ([people.md](people.md#change-your-mind)) |
| Who is it? → Take it back | | | the answer about that face is taken back, exactly |

- The bot edits the alert ("✅ Marked: false alarm") and keeps the buttons, so a wrong tap
  can be corrected; the last answer wins, as with `vidette label apply`.
- A clip without an entry gets one first, numbered like `vidette label draft` would,
  with the live verdict as its `draft`; the policy's scenarios (and `other`) are added to
  the dataset's vocabulary if missing. `labels.yaml` is created from the starter template
  if the folder has none. Everything goes through the same code as
  [`vidette label apply`](label.md) and is validated before the file is replaced.
- "Who is it?" appears when the face check found a face and there is something to say
  about it: anonymous identities are on, or (with `names: true`) someone is enrolled.
  With several faces it asks which one first ("Face 1: …"). Enrolled people and named
  familiar identities are listed by name only with `names: true`. A name typed in reply
  to the bot's question is read only in an allowed chat and only as a reply to that
  question; after a restart of the service, tap the button again.
- `labels: false` keeps the feedback on the event only.
- **Calibration** ([calibration.md](calibration.md)): after 4 👎 and no 👍 on one scenario
  of one camera, that scenario on that camera wakes only when the judge is sure, later
  only in the digest; critical scenarios never go to the digest. The bot says each change
  in one line with an Undo button, and `/tuning` lists what is in force.

## The other cameras, and questions

Under an alert, when there are other clip-folder cameras, **📹 Other cameras** sends every
camera at the moment of the alert as one video, a reply to the alert, with which tile is
which camera and what each recorded; a camera that recorded nothing is named as such
(battery cameras record only while awake). Details: [other-cameras.md](other-cameras.md).

Any other text from an allowed chat is a question, answered by a small assistant with the
local model by default: "where else was this person?" or "send the other cameras" as a reply to
an alert, "what happened at the gate after 9?", "digest". Details and privacy:
[assistant.md](assistant.md).

## The digest and the commands

There is no hourly digest. Once a day at `digest_at` (21:00, local time), silently, into
the chat's digest topic, the day in a few plain lines, **only when something needs a
look**: an event that was confirmed but woke nobody (known people, quiet hours), or alerts
nobody answered yet. A day of passers-by and answered alerts sends nothing.

```
🌙 The day, January 14
Alerts: 3. By your marks: false 2, unanswered 1.
False most often: walked past (2).
1 alert waits for an answer, the last at 18:40 (Porch). The buttons are under the alert.
No alert, but worth a look:
• 20:05 Garden: person at the house, one of us: Sam
All day: 410 clips: passers-by 90, cars 60, animals 4.
Not checked by the judge: 150 (the queue did not reach it).
```

The day is written down before the message goes out, so a restart never sends it twice. A
service that was down at 21:00 sends it when it is back, and a digest Telegram did not take
(no connection, `429`, `5xx`) is tried again after 30 s, then 1, 2, 4, 8 minutes and every
10 minutes, at once when the bot hears Telegram again; both only until `digest_until`
(23:30, the same evening). A digest whose answer was lost after it went out may have
arrived, and is not sent again. `/status` answers with the same lines for today so far,
always, also when nothing happened.

| Command | What it does | Who |
|---|---|---|
| `/status` | cameras, events and alerts today, the last alert and where its time went, the judge; then today's digest so far | every allowed chat (the digest: recipients) |
| `/live` | says that live view will be part of the Vidette app itself; no links to a separate restreamer | every allowed chat |
| `/quiet` | quiet for `quiet_for` (1 h): `always` levels still alert, the rest is in the digest; `/quiet off` ends it | per chat |
| `/tuning` | what your 👍/👎 changed ([calibration.md](calibration.md)), one Undo button per change | every allowed chat |
| `/help` | what the bot does | every allowed chat |
| any other text | a question for the assistant ([assistant.md](assistant.md)) | every allowed chat |

Commands are answered in the topic they were sent in. Commands older than 10 minutes
(sent while the service was down) are ignored; button presses are not.

### Where the time went

Every event keeps when each stage happened (`intent.timeline`, epoch seconds): the
camera woke (`requested`, the recording session opened) or the motion gate opened
(`motion`), the `first_frame`, `person_seen` (the first frame the judge saw a person in),
the early window written and picked up, the clip `closed` and `picked_up`, the judge
queued, started and finished (`early_` ones for the early window), and the alert's
`sent_at` with its message. `/status` shows it for the last alert:

```
⏱ Where the time went
camera woke up: 00:05:43
→ first frame: +1 s
→ person in view: +1:16
→ clip ready: +52 s
→ the server took the clip: +6 s
→ the judge started: +1:58
→ the judge answered: +1:04
→ alert sent: +3 s
From the person in view to the alert: 4:03
```

All stages run on the recording machine's clock. A camera's stream can itself lag behind
the camera (a base station that buffers a weak link delivered one camera's video
2 s to 2 min late, judged by its on-screen clock, different from one wake to the next);
no stage here can see that lag.

## Limits and failures

- **Only allowed chats.** Anything from another chat is ignored without an answer.
- **No verdict, no alert.** A judge that is down, over its daily budget, too slow or
  unsure produces no alert by a simpler rule; the digest counts those events with the
  reason.
- **Latency:** a clip-folder incident exists only when the clip ends (or, with
  [early windows](#early-alerts-before-the-clip-ends), after its first seconds), then the
  face check (about 1 to 2 s) and the judge run. The verdict worker takes the newest waiting
  incident first, so a backlog delays old clips rather than the current one, and drops
  incidents that ended more than 15 minutes ago.
- **Telegram's limits:** a `429` is waited out as Telegram asks (up to 60 s) and retried;
  network errors and 5xx back off 1, 2, 4 s. An upload or a digest that may have arrived
  (its answer was lost after it went out) is never repeated. Failures become
  `telegram.failed` system events (rate-limited, without the token).
- **Telegram out of reach** (no connection, or a `429`/`5xx` after those retries):
  - An alert waits in the database (a restart keeps it) and goes when the bot hears
    Telegram again (the long poll is the probe; otherwise after 30 s, doubling to 5 min),
    with "⏳ Sent late: Telegram could not be reached at 21:14:05" under the camera and
    the time. The usual rules hold, by the time it was due: one alert per event and chat;
    a camera's alerts within `collapse` become one message; an early alert the whole clip
    then called ordinary is not sent; an album that already went out is not sent again.
  - An alert older than `late_alerts_for` (2 h) is not sent any more: the chat gets one
    line ("⏳ 3 alerts from 21:14 to 22:05 were not sent: …") and the digest lists them
    under "No alert, but worth a look". `late_alerts_for: 0` drops them at once, as
    before this existed.
  - An edit of an alert (another alert folded into it, the whole clip after an early
    alert) waits the same way and is redone.
  - No answer at all for 3 minutes: the bot's HTTP client is replaced by a new one (new
    connections, DNS and proxy settings read again), again every 3 minutes while it
    lasts, said once in the log. A long-running service once failed to connect for over
    an hour while the machine itself reached Telegram, and a restart fixed it; this does
    what the restart did.
- **Two pollers** (a second Vidette with the same token, or a webhook set on the bot)
  make Telegram answer `409`; the bot waits 30 s and tries again, and says so.

## Measured in testing

Clip-folder cameras, the judge a 27B vision model behind llama-server shared with other
work (2048 tokens per slot):

- **The judge is the bottleneck.** One verdict took 51 to 115 s, most of it waiting for a
  free slot. The cameras cut about 100 to 150 clips an hour together; the judge answers
  a few dozen. Newest first keeps the current clip at the front; the rest are counted in
  the digest as "the queue did not reach it".
- **Context:** four frames at 768 px plus a policy with levels were 2101 tokens and the
  server refused them; at 640 px, 1565 in and about 50 out.
- **A small figure is easy to miss.** On a test clip a person about 100 px tall at the
  far end of a 1920 px picture touches something on a wall; at 640 px the judge missed it
  and chose `animal`, and the face check found no readable face (24 frames). The test alert said so, as it should. Catching this needs more pixels
  on that spot (a crop or a narrower camera), not a different wording.

These are observations, not rates: rates need `vidette eval` on labelled clips.

## Privacy

- Telegram is a cloud service: frames, the clip, the judge's description and the camera
  names pass through its servers. Configure it only for chats of people who agreed.
- `names: true` also sends the names of recognized people, in the text and on the
  "This is …" buttons. `vidette validate` warns while it is on. Face crops and reference
  photos are never sent.
- The token is only read from the environment, never printed, and masked in httpx logs;
  `vidette telegram check` prints counts, not chat ids.

## Budgets

| | |
|---|---|
| Compute | nothing measurable: a few HTTP requests per alert; the digest is one database query an hour |
| Storage | one row per message sent (`telegram_messages`); `labels.yaml` grows by one entry per answered clip |
| Network | an alert uploads its frames and clip (about 0.3 MB of JPEGs plus the clip, typically 2 to 15 MB); long polling holds one idle HTTPS request |
| Latency | the bot adds the upload time (seconds); the judge and the clip's length dominate |
