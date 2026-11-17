# The other cameras at the moment of an event

An event is one clip of one camera. A person walking along a street is usually on two or
three cameras within a minute, and each camera cuts its own clips. For any event, Vidette
finds what every clip-folder camera recorded around its moment, says which clips show the
same person (only when a face says so), and puts them side by side in one video. Design:
[ADR-0017](architecture/adr/0017-subject-frames-other-cameras-and-questions.md).

Status: 🚧 built and tested; run on real clips of a test deployment (a minute of three
cameras stitched in 2.3 s, 1.1 MB). Clip-folder cameras only: events from Frigate have no
neighbours yet.

## Where to find it

| Where | What |
|---|---|
| Web app, an event card | under the clip: every other camera's clips around the moment, «same person (face)» where a face matched, and «All cameras at once» |
| Telegram, under an alert | **📹 Other cameras**: the mosaic as a reply to the alert, with what each camera recorded |
| Telegram, a question | "send the other cameras" as a reply to an alert ([assistant.md](assistant.md)) |
| API | `GET /api/v1/events/{id}/linked` (the clips), `GET /api/v1/events/{id}/linked.mp4` (the mosaic) |

## Which clips count

The moment is when the judge first saw a person in the event's clip (else the clip's
start); the window is `before` (15 s) before it to `after` (45 s) after it. Every clip of
every configured clip folder that overlaps the window counts, the event's own camera
included (a person walking past often makes two clips on one camera). Found on demand from
the folders, never stored, so a clip that arrived after the alert is found too.

```yaml
cross_camera:
  before: 15s
  after: 45s
  lag_up_to:              # cameras whose video can reach the collector late
    porch: 2m
  max_video_mb: 19        # a longer mosaic is encoded smaller, then cut into parts
```

### Clocks

A clip's time is its collector's stamp on the camera's clock
([ADR-0015](architecture/adr/0015-truthful-timing-and-early-verdicts.md)). Compared with
the cameras' own on-screen clocks (12 clips per camera, read by hand):

| Camera | Its clips start after the moment on its screen by |
|---|---|
| a camera that streams all the time | 0.2 to 4.5 s |
| a battery camera that wakes through a base station | 0 to 100 s, different on every wake (median about 13 s) |

So cameras that stream all the time line up within a few seconds; a battery camera does not. List such a
camera under `lag_up_to`: its clips are searched that much later, and everything about it
says the time is approximate ("about 09:41:16–09:43:03"). Reading the on-screen clock to
place it exactly is 📐 designed, not built.

### The same person

Only by face: a clip shows "the same person by face" when an anonymous identity
([people.md](people.md#strangers-who-come-back)) or an enrolled person is in both the
event's and that clip's face reports. A clip whose event saw a person without a comparable
face says "a person in view, no face to compare"; one that was never judged says nothing about
people. Clothes, height or colour are never used here: that would be profiling. (An
alert's own [incident links](incidents.md) may use the colours of clothes when a
deployment turns `incident.look` on, only to say "probably the same person".)

## The video

One mosaic in real time: one tile per camera (up to four), each showing that camera's
clips at the moment they were recorded, dark grey where it recorded nothing. Landscape
cameras are stacked in a column; a camera mounted on its side stands next to them at full
height. Nothing is drawn on top: each camera's own on-screen clock is in its tile, and the
message under the video says which tile is which:

```
📹 All cameras, 09:40:59–09:41:59
Left top: Porch; left bottom: Garden; right: Path
• Path: 09:41:13–09:41:21, this event
• Porch: about 09:41:16–09:43:03
• Garden: 09:41:34–09:41:42, a person in view, no face to compare
Porch: approximate time, this camera delivers its recording late and its picture may lag behind the others
```

When no other camera recorded anything, the bot says so instead ("the other cameras recorded
nothing … the camera slept or nothing moved"): battery cameras record only while awake.

## Budgets

| | |
|---|---|
| Compute | finding the clips: a directory listing and a few sidecars (0.06 s for three folders of about a thousand clips); the mosaic: one ffmpeg run, about 2 to 3 s for a minute of three cameras on an Apple-silicon Mac |
| Storage | a mosaic is about 1 MB a minute, under `media_dir/linked/`, removed a day later |
| Network | the mosaic is uploaded to Telegram only when someone asks for it |
| Latency | seconds after the tap |
