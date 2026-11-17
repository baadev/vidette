# Collecting clips from a camera: `vidette collect`

A labelled set of real clips is what turns "fewer, better alerts" into a number
([eval.md](eval.md)). A battery camera cannot feed Frigate continuously, but it does
stream while it is awake, and those wake periods are exactly the moments a policy has to
judge. `vidette collect` waits for them and keeps each one as a dataset clip. For a camera
that streams all the time, [continuous mode](#continuous-mode) keeps a short ring of the
stream and cuts a clip around each motion instead.

It is a dataset tool, not a recorder ([ADR-0010](architecture/adr/0010-clip-folders.md),
[ADR-0011](architecture/adr/0011-continuous-collect.md)): it keeps clips and, in
continuous mode, a few hours of lookback that are overwritten; it never runs inside
`vidette serve`, and it stops at hard disk limits (with
[`--prune-oldest`](#pruning-the-oldest-clips---prune-oldest), the oldest clips nothing
needs make room under the folder's cap first).

Status: ✅ wake-period mode tested against a fake RTSP camera that sleeps, wakes, turns
away a second client and holds dead sessions the way a battery camera did in v0.1;
run against real battery cameras. ✅ Continuous mode tested against a fake
continuous camera and run against a real camera behind a base station
([measured](#measured-on-a-real-camera)).

## Setup (macOS, a battery camera with NAS RTSP)

1. **Enable NAS (RTSP)** in the Eufy app (camera → Settings → Storage → NAS (RTSP)), set
   its username and password, note the URL, and give the camera (or its HomeBase) a fixed
   address in your router. Details and caveats: [cameras/eufy.md](cameras/eufy.md).
2. **Store the URL outside the repository**, readable only by you:

   ```bash
   mkdir -p ~/.config/vidette
   printf '%s\n' 'rtsp://USER:PASSWORD@192.0.2.50/live0' > ~/.config/vidette/rtsp-url
   chmod 600 ~/.config/vidette/rtsp-url
   ```

   Or export `VIDETTE_RTSP_URL` (it wins over the file); `--url-file` points elsewhere.
   The URL is never accepted as a command-line argument.
3. **Check without touching the camera's RTSP server:**

   ```bash
   cd ~/development/vidette/server
   uv run vidette collect ~/vidette-data/front-door --camera front_door --check
   # camera   rtsp://***@192.0.2.50:554/live0  (from /Users/you/.config/vidette/rtsp-url)
   # port     closed or silent: asleep, or the address is wrong
   # dataset  /Users/you/vidette-data/front-door
   # disk     dataset 0 B of 5 GB, 21.3 GB free (floor 10 GB)
   #          next clip may take up to 2.5 GB
   ```

   Walk in front of the camera and run it again: the port should show `open`.
4. **Run it in the foreground once** (`--once` exits after the first clip):

   ```bash
   uv run vidette collect ~/vidette-data/front-door --camera front_door --once
   ```
5. **Run it in the background** with launchd: the template is
   [deploy/macos/dev.vidette.collect.plist](../deploy/macos/dev.vidette.collect.plist),
   with the install commands in its header comment. Logs go to
   `~/Library/Logs/vidette-collect.log`.

The dataset folder must live outside any git repository; the collector refuses a path
inside one. Real footage shows neighbours and couriers: publish numbers, never clips.

## What it does with the camera

```mermaid
stateDiagram-v2
    [*] --> Asleep
    Asleep --> Asleep: port closed (TCP check every 5 s)
    Asleep --> Recording: port open → one RTSP session
    Recording --> Cooldown: stream ends / 2 min cap / stop
    Cooldown --> Asleep: 10 s
    Recording --> Backoff: 404, no video, 401
    Backoff --> Asleep: 30 s … 5 min (401: 10 min)
```

Every rule comes from the v0.1 field fixes (tag `nvr-core`, CHANGELOG 0.1.1 to 0.1.3):

| Rule | Why |
|---|---|
| While the camera sleeps, only a bare TCP connect to the RTSP port every 5 s, closed at once; no RTSP request | An RTSP session is something the camera has to serve and time out; v0.1 reconnecting every ~45 s kept a battery camera awake and drained it |
| One ffmpeg at a time; a lock file allows one collector per dataset | Eufy serves exactly one RTSP client; a second one gets `404 Stream Not Found` |
| Every stop is clean: `q` to ffmpeg (TEARDOWN), SIGTERM after 5 s, SIGKILL last (and logged) | After an unclean hangup the camera holds the dead session and answers 404 to everyone for minutes |
| `404` backs off 30 s, 60 s, … up to 5 min | That is the dead-session or second-client case; retrying fast only prolongs it |
| A session that opens but sends no video for 8 s ends; repeats back off 15 s … 5 min | A silent session means the camera is not really awake; hammering it was the v0.1 battery bug |
| `401` retries every 10 min and says which file holds the URL | Wrong credentials will not fix themselves |
| Input probed for up to 10 s | A camera that just woke sends its codec parameters late; with ffmpeg's default window the recording died ("dimensions not set") |
| 10 s pause after each clip | Lets the camera finish going back to sleep |

Do not point VLC, Home Assistant's RTSP integration or another NVR at the camera while
the collector runs: whoever connects second gets nothing.

### Cameras behind a base station (Eufy HomeBase)

A HomeBase keeps its RTSP port open for all its cameras and answers `404 Stream Not
Found` for a camera that is asleep, the same words a camera uses for "another client has
the stream". Measured on 2026-11-14: the answer comes in about 30 ms, 14 requests in 14
seconds did not wake the camera, and the stream appears by itself when the camera's own
motion sensor wakes it. Without help, the collector reads those 404s as "busy" and backs
off to 5 minutes, so it misses most wake periods. Tell it what the address is:

```bash
uv run vidette collect ~/vidette-data/front-door --camera front_door --base-station
```

With `--base-station`, a 404 means asleep: no TCP check (the port tells nothing), a
request every `--probe-every` (2 s with `--base-station`) without backoff, one log line per
sleep. A woken stream is evidence (the camera's own sensor fired): it is kept down to
`--wake-min` (0.5 s) even below `--min`, and after it ends the next look comes after
`--base-reconnect` (1 s), never a growing backoff. A real
second client (VLC, the Home Assistant RTSP integration) now looks like a camera that
never wakes, so keep the one-client rule.

A camera whose port stays open while it sleeps without answering 404 shows up as repeated
"accepted a session but sent no video" lines with growing pauses: the collector still
works but reacts to motion more slowly. `--no-probe` skips the port check.

Options: `--min 3s` (drop shorter clips), `--max 2m` (cut longer ones), `--probe-every 5s`,
`--cooldown 10s`, `--silence 8s`, `--camera NAME`, `--rotate 90|180|270`
([rotation](#a-camera-on-its-side---rotate)), `--base-station`, `--wake-min 0.5s`,
`--base-reconnect 1s`, `--early 8s`, `--incident-file PATH`.

### Incident mode

After an alert, `vidette serve` writes `incident-mode.json` next to its database
([incidents.md](incidents.md#incident-mode)). A collector started with `--serve-config`
finds it there (or give `--incident-file`); until it expires, every woken stream is kept
whole whatever its length, and in continuous mode the stretches the motion gate did not
cut are kept in 30 s pieces (trigger `wake`, the reason in `stop_reason`). The log says
`incident mode until …` once per incident.

### Early windows: `--early 8s`

A wake period lasts until the camera sleeps again, often a minute or two after the person
has gone, and the clip exists only then. With `--early 8s`, once a recording has 8 s of
video, its first 8 s are copied (no re-encode) to `.early/<id>.mp4` with a sidecar
`.early/<id>.json`, written last and atomically. The clip that follows gets the same id,
so `vidette serve` judges the early window at once and the whole clip later under one
event ([docs/telegram.md](telegram.md#early-alerts-before-the-clip-ends)). The part file
is written in half-second Matroska clusters, flushed at once, so it can be read while it
grows. Early windows are pruned after an hour. In continuous mode the window is the
motion event's first seconds with its pre-roll, cut from the ring once the event has run
that long.

### Timestamps, and a restreamer in front of the camera

A clip keeps the camera's own clock (its RTP timestamps): the time each frame was taken.
The time a frame *arrives* is a worse clock. Measured through a restreamer (go2rtc) on a
Wi-Fi camera, 25 s of the same packets: the camera's timestamps were 376 frames 61 to
73 ms apart; their arrival times came in bursts (47 % less than 30 ms apart) with eight
pauses of up to 0.7 s. On a camera with a weak radio link the pauses reach 7 to 13 s,
after which the backlog arrives at once. A clip stamped on arrival plays that as a frozen
picture followed by fast-forward, and a judge reads a person walking past as a person
standing still.

A restreamer has one trap of its own: when the camera drops and comes back while a reader
is attached, go2rtc splices the new camera session into the same RTSP session, clock and
all, so the timestamps jump by up to hours. The collector repairs that on the input
(`setts` jump guard): a packet more than 10 s after the previous one, or not after it at
all, continues one frame later; real pauses of the camera up to 10 s stay as they are.
The clip's length limit (`--max`) is the collector's own, not ffmpeg's `-t`, which would
read the timestamps before the repair.

`--wallclock-timestamps` stamps packets on arrival instead. It is there for a source whose
clock cannot be trusted at all; it is not needed behind go2rtc any more. Each clip's
sidecar carries its [stream health](#stream-health) either way.

## What lands on disk

```
~/vidette-data/front-door/
  clips/20261114-210304.mp4       the wake period, stream-copied, video only
  clips/20261114-210304.json      when, how long, codec, size, why it stopped
  frames/20261114-210304/1.jpg    4 to 8 key frames spread over the clip (≤ 1280 px)
  .partial/…                      the recording in progress (adopted on the next start
                                  if the collector was killed)
  .early/20261114-210304.mp4      with --early: its first seconds, while it records
  ring/20261114T210304+0200.mkv   continuous mode only: the lookback ring, 60 s a file
  .vidette/collect-state.json     watched hours, clip count
```

The clip id is the local time the video started. No file contains the camera's address
or credentials.

### Stream health

Every sidecar has a `health` block, measured from the clip's packet times (no decoding):
how many frames, how many pauses longer than 0.5 s and the time they cover, the longest
one and where it starts, the share of frames that came in a heap after a pause
(`burst_share`), and the longest pauses as `[start, end]` seconds into the clip. A clip
with a pause of 2 s or more, or pauses covering a tenth of it, counts as interrupted: the
judge is told where the video stalled, and the bot says "the recording paused". Sound is not recorded: the judge does not use it, and recording
conversations is a legal minefield in many places.

## Disk limits

Two hard limits, checked before every recording and once a second during it:

| Limit | Default | What happens |
|---|---|---|
| Dataset folder size | `--max-size 5GB` | nothing new is recorded; a recording is capped by ffmpeg's `-fs` so it cannot cross the limit. With [`--prune-oldest`](#pruning-the-oldest-clips---prune-oldest), the oldest clips nothing needs go first |
| Free space on the disk | `--min-free 10GB` | nothing is recorded below it; a running recording stops cleanly |

The folder never exceeds `--max-size`: each recording may use at most half of what is
left (finalizing briefly stores a clip twice). How far 5 GB goes depends on the camera's
bitrate; at 2 Mbit/s a 30 s clip is about 8 MB with its frames, so roughly 600 clips. When
a limit is reached, the log says so once and the collector checks again every minute.
The log itself grows by a few lines per clip.

### Pruning the oldest clips: `--prune-oldest`

A camera that sees a lot fills any cap within days, and a full folder is a blind camera.
With `--prune-oldest`, a folder past 95 % of `--max-size` loses its oldest clips until it
is back under 90 %: one prune frees about a twentieth of the cap, so it runs now and then,
not after every clip. A clip goes with everything made from it: its sidecar, its key
frames (`frames/<id>/`), its face crops (`faces/<id>/`) and its early window
(`.early/<id>.*`). The continuous ring has its own cap and is never touched; contact
sheets in `review/` show labelled entries, which are kept.

A clip is never pruned when something needs it:

| Kept | How it is found |
|---|---|
| labelled | an entry of the folder's `labels.yaml`, whatever its status: a reviewed label, a model's draft waiting for review, a number on a contact sheet |
| alert or mark | an event in `vidette serve`'s database whose verdict alerted (or the model said alert and a rule kept it quiet), or that was confirmed, sent to Telegram, answered with 👍/👎 or a reason, or starred |
| near an alert | starts from 1 min before to 3 min after such an event, on any camera: the [other cameras](other-cameras.md) of the alert, with room for a camera whose video arrives late |
| faces | a sighting of an anonymous identity, or a face decision about a known person that names the clip or its event ([people.md](people.md)): decisions can be taken back only while the clip is there |
| young | younger than `--prune-min-age` (24 h): time for a verdict, an answer in Telegram or a label to arrive |

`vidette serve`'s database and people folders come from its config:
`--serve-config PATH` (default `$VIDETTE_CONFIG`, then `~/.config/vidette/vidette.yaml`
if it exists). Only its `storage` and `people` sections are read, and the databases are
opened read-only; secrets elsewhere in the file, such as a bot token from the environment,
are not needed. Without a serve config, only labels and the default people folders
(`~/vidette-data/people`, `~/vidette-data/identities`) keep clips, and the start-up line
says that alerts are not known.

It fails safe. When anything that says what to keep cannot be read (a `labels.yaml` that
does not parse, a database that is missing or will not open), nothing is pruned and the
collector stops at the cap as it does without the flag. When every clip old enough is
kept for a reason, it stops too. Either way the "not recording" line says why, and the
next try comes 10 minutes later. A prune is one line in the log:

```
pruned 41 oldest clip(s) up to 2026-01-17 09:58, 760 MB freed · dataset 14.3 GB → 13.5 GB of 15 GB (--max-size) · oldest kept 2026-01-16 18:40 · kept: 3 alert or mark, 20 faces, 12 labelled, 6 near an alert, 410 younger than 24.0 h
not recording: the dataset folder holds 15 GB, the cap is 15 GB (--max-size); nothing left to prune (kept: 3 alert or mark, 20 faces, 900 labelled, 6 near an alert, 410 younger than 24.0 h); checking again every 1.0 min
```

`--check` shows what a prune would find now and deletes nothing:

```
prune    on: past 14.2 GB the oldest clips go until 13.5 GB; 301 clip(s), 1.53 GB could go now · kept: 3 alert or mark, 20 faces, 12 labelled, 6 near an alert, 410 younger than 24.0 h
```

`vidette serve` keeps its events of pruned clips; their clip link answers "Clip not
available", as for any clip gone from a clip folder. A pruned clip is gone: label what you
want to keep for `vidette eval` while it is in the folder.

Budgets: a prune lists `clips/`, reads `labels.yaml`, the people folder and the two
databases (read-only), and deletes files; measured on an Apple-silicon machine, finding
what to keep and what could go takes about 0.1 s for 1,300 clips and 2,500 events. It
runs in a thread, so a continuous camera's stream is not held up.

## Watched hours

`.vidette/collect-state.json` counts the time the collector was running and allowed to
record. `vidette label` writes it into the dataset's `observed_hours`, which is what
false alarms per day are computed from. As with exported clips, recall is measured
against what the camera caught: a person its own motion trigger missed is invisible here.

## Credentials

The URL lives in one file you own or in the environment. Everything the collector prints
or writes passes through a scrubber that removes the URL, its user part and the password
(raw and percent-encoded), including ffmpeg's own error lines, which echo the input URL.
One thing cannot be hidden: ffmpeg receives the URL as an argument, so another user
account on the same Mac could see it with `ps` while a recording runs.

## Continuous mode

For a camera that streams all the time (a mains camera), `--continuous` keeps a ring of
the stream and cuts a clip around each motion, with a few seconds before and after it. It
also works for a camera that streams only while awake: each wake period goes into the
ring and the gate cuts the motion out of it. With `--base-station`, a wake the gate cut
nothing out of is kept whole (trigger `wake`, "camera woke; the motion gate saw nothing"):
the camera's own sensor fired, which is evidence the gate cannot see. Without
`--continuous` the whole wake period is kept anyway.

```bash
uv run vidette collect ~/vidette-data/street --camera street \
  --url-file ~/.config/vidette/rtsp-url-live1 --continuous --base-station --rotate 270 --check
uv run vidette collect ~/vidette-data/street --camera street \
  --url-file ~/.config/vidette/rtsp-url-live1 --continuous --base-station --rotate 270 --once
```

```mermaid
flowchart LR
    cam[camera RTSP] --> ff[one ffmpeg]
    ff -- stream copy --> ring["ring/ 60 s segments<br/>≤ --ring-size, ≤ --ring-keep"]
    ff -- "4 fps, 256×144 grey,<br/>frame difference" --> gate[motion gate]
    gate -- "event ± 3 s" --> cutter[cut from the ring]
    ring --> cutter
    cutter --> clip["clips/&lt;id&gt;.mp4 + key frames + json"]
```

- **One connection.** ffmpeg reads the camera once and writes two outputs: the ring
  (stream copy, no re-encode) and a small grey copy for the gate. A camera that serves one
  client at a time is never asked twice.
- **The ring** is `ring/<local time with UTC offset>.mkv`, one file per `--segment` (60 s,
  split on key frames). It is trimmed to `--ring-size` (3 GB) and `--ring-keep` (24 h),
  never deletes for size what is younger than `--ring-min-age` (30 min: the last half
  hour can always be cut out again; the free-space floor still wins),
  keeps at least its newest three segments and whatever a waiting clip still needs, and
  is not a recording: nothing indexes or plays it, and it is overwritten within hours.
- **The gate** decodes the stream a second time at 4 fps (VideoToolbox on a Mac,
  `--hwaccel none` to force software), shrinks it to 256×144 grey and compares each frame
  with the previous one. A frame has motion when between `--motion-area` (0.1 %) and
  `--motion-max-area` (40 %) of the unmasked picture changed by more than
  `--motion-threshold` (25 of 255 grey levels); more than 40 % at once is light (a cloud,
  headlights on the wall, the switch to infrared), not motion. An event opens after
  `--motion-min` (1 s) of such frames and closes after `--motion-gap` (2 s) without them.
  A leaf flicking once never opens one; leaves in a steady wind will, and the VLM draft
  and the person labelling throw those clips away (the gate errs on keeping).
- **The clip** is the event plus `--preroll` (3 s) and `--postroll` (3 s), between
  `--min` and `--max`. It is cut as soon as the ring holds its end (the open segment is
  written in half-second clusters, flushed at once, so the clip need not wait up to a
  minute for it to finish), by stream copy (so it starts on the key frame at or before the
  pre-roll: a little more lead, never less) and gets key frames and a sidecar exactly like
  a wake-period clip, plus `"trigger": "motion"` and what the gate saw (`motion.active_s`,
  `motion.peak_area`). `vidette label` and the [clip-folder source](clip-folders.md) read
  it unchanged. Motion longer than `--max` gives one clip of `--max`, then the gate waits
  for 2 s of quiet before it opens another.

### Masks

Cameras burn the date and time into the picture; the seconds change every second and
would be "motion" forever. The default mask covers the top-right strip where Eufy draws
it (`0.6,0,0.4,0.1`: x, y, width, height as fractions of the picture **as the camera sends
it**, before `--rotate`). Add regions with `--mask` (repeatable, `60%,0,40%,10%` works
too), for example a tree that waves all day; `--no-overlay-mask` drops the default. Masks
in the camera's own frame keep working when you change `--rotate`.

### A camera on its side: `--rotate`

`--rotate 90|180|270` is the clockwise turn that makes the picture stand upright (both
modes). The key frames, which are what contact sheets and the VLM see, are turned
explicitly; the MP4 keeps the pixels as sent and carries the turn as display metadata,
so QuickTime and browsers show it upright. The date overlay is part of the picture, so it
turns with it and reads sideways in a corner: the real time is in the clip id, the
sidecar and the contact sheet's caption, and the judge is not asked to read clocks.
Re-drawing the overlay level would put a second, smaller time stamp on top of the scene
for no gain.

To find the turn, look at one frame both ways and keep the one where cars stand on their
wheels:

```bash
ffmpeg -i clips/<id>.mp4 -noautorotate -frames:v 1 -vf transpose=clock cw.jpg
ffmpeg -i clips/<id>.mp4 -noautorotate -frames:v 1 -vf transpose=cclock ccw.jpg
# cw.jpg upright → --rotate 90; ccw.jpg upright → --rotate 270
```

### Disk in continuous mode

| Limit | Default | What happens |
|---|---|---|
| Free space on the disk | `--min-free 10GB` | the ring gives up all but its last three segments; if that is not enough, recording stops cleanly and resumes when space comes back |
| Clips (the folder without `ring/`) | `--max-size 5GB` | a motion is logged as "not kept"; the ring keeps recording. With `--prune-oldest`, the oldest clips nothing needs go first |
| The ring | `--ring-size 3GB`, `--ring-keep 24h` | oldest segments are deleted; the ring also never grows into the last 512 MB above the free-space floor, so clips always have room before it |

Priority, highest first: the floor, the clips, the ring. Two collectors on one Mac share
the floor; the ring yields first.

### Budgets

| | Measured on a 1920×1080 H.264 camera at 15 fps, Apple-silicon machine |
|---|---|
| Compute | the gate's decode: about 5 % of one core with VideoToolbox, about 20 % in software; cutting a clip is a stream copy plus 4 to 8 JPEGs |
| Storage | the stream is about 2.6 Mbit/s, 1.2 GB an hour: a 1 GB ring holds about 50 minutes, 3 GB about 2.5 hours. A 15 s clip is about 5 MB with its frames |
| Disk writes | everything the camera streams passes through the ring once: up to about 28 GB a day for a camera that never sleeps |
| Latency | a clip lands up to one segment (60 s) plus the post-roll after the motion ends; this is a dataset tool, not an alarm |

### Measured on a real camera

A battery camera behind a base station (1920×1080 H.264 at 15 fps, about 2.6 to 2.8 Mbit/s),
five minutes of `--continuous --base-station`:

- **It streamed only while awake**: short wake periods, the base station answering 404 in between.
- **Every clip was real**; wake periods whose cause had passed before the stream arrived gave no clip.
- **No false clips from the clock**: the ticking date overlay changes 0.02 to 0.06 % of the frame
  once a second, under the default threshold, but enough to keep a lower threshold busy.
- **Ring**: a 60 s segment of steady stream is about 20 MB.
- **Timing**: the clip's start agreed with the camera's burned-in clock to within a second.
- **Cost**: ffmpeg at 1 to 5 % of a core while streaming (VideoToolbox), nothing while
  the camera sleeps but one RTSP request every 5 s.

Five minutes is a smoke test, not a quality figure: how many events the gate misses or
invents per day is a `vidette eval` question for the labelled set.

### Running it in the background

The template [deploy/macos/dev.vidette.collect.camera2.plist](../deploy/macos/dev.vidette.collect.camera2.plist)
runs a second camera next to the first camera's agent: its own dataset, URL file and log
(`~/Library/Logs/vidette-collect-camera2.log`), `--continuous --base-station`,
a 1 GB ring. The log has one line per clip, one per sleep and wake of the camera, and an
hourly status line (events, clips kept and dropped, share of frames with motion and with
light changes, ring size, free space): a gate that fires on most frames, or many
light changes, is the hint to add a `--mask` or raise `--motion-area`.

Next step: [label the clips](label.md).
