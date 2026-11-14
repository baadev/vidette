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
`vidette serve`, and it stops at hard disk limits.

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
request every `--probe-every` (5 s) without backoff, one log line per sleep. A real
second client (VLC, the Home Assistant RTSP integration) now looks like a camera that
never wakes, so keep the one-client rule.

A camera whose port stays open while it sleeps without answering 404 shows up as repeated
"accepted a session but sent no video" lines with growing pauses: the collector still
works but reacts to motion more slowly. `--no-probe` skips the port check.

Options: `--min 3s` (drop shorter clips), `--max 2m` (cut longer ones), `--probe-every 5s`,
`--cooldown 10s`, `--silence 8s`, `--camera NAME`, `--rotate 90|180|270`
([rotation](#a-camera-on-its-side---rotate)), `--base-station`.

## What lands on disk

```
~/vidette-data/front-door/
  clips/20261114-210304.mp4       the wake period, stream-copied, video only
  clips/20261114-210304.json      when, how long, codec, size, why it stopped
  frames/20261114-210304/1.jpg    4 to 8 key frames spread over the clip (≤ 1280 px)
  .partial/…                      the recording in progress (adopted on the next start
                                  if the collector was killed)
  ring/20261114T210304+0200.mkv   continuous mode only: the lookback ring, 60 s a file
  .vidette/collect-state.json     watched hours, clip count
```

The clip id is the local time the video started. No file contains the camera's address
or credentials. Sound is not recorded: the judge does not use it, and recording
conversations is a legal minefield in many places.

## Disk limits

Two hard limits, checked before every recording and once a second during it:

| Limit | Default | What happens |
|---|---|---|
| Dataset folder size | `--max-size 5GB` | nothing new is recorded; a recording is capped by ffmpeg's `-fs` so it cannot cross the limit |
| Free space on the disk | `--min-free 10GB` | nothing is recorded below it; a running recording stops cleanly |

The folder never exceeds `--max-size`: each recording may use at most half of what is
left (finalizing briefly stores a clip twice). How far 5 GB goes depends on the camera's
bitrate; at 2 Mbit/s a 30 s clip is about 8 MB with its frames, so roughly 600 clips. When
a limit is reached, the log says so once and the collector checks again every minute.
The log itself grows by a few lines per clip.

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
ring and the gate cuts the motion out of it. Motion that is over within the first seconds
of a wake, before the stream reaches the collector, is not seen; without `--continuous`
the whole wake period is kept instead.

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
  `--min` and `--max`. It waits until the segment holding its end is finished (up to one
  segment, a minute), is cut by stream copy (so it starts on the key frame at or before the
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
| Clips (the folder without `ring/`) | `--max-size 5GB` | a motion is logged as "not kept"; the ring keeps recording |
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
