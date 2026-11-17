# Incidents across cameras

🚧 Built and tested ([ADR-0022](architecture/adr/0022-incidents-across-cameras.md));
running on a test deployment.

One person on a property is usually on two or three cameras within a few minutes. An
alert is one clip of one camera; this page is about what Vidette does with the rest:

- the alert says where else the person was (one line per camera), with that clip;
- for some minutes after an alert every camera keeps everything it sees (**incident mode**);
- a long clip is judged around the person, so a moment at its very end is not lost.

## What the alert says

```
🚨 Alert: critical
📍 Street, 22:14:05
👤 stranger #12 (small face)
💬 A person walks along the wall, stops at the door and reaches for it.
🔗 Probably the same person (clothes and timing): Front door, about 22:13: A person at a parked car (the judge had let it pass)
```

When an alert goes out, the events of the other cameras that had a person within
`incident.link_within` (3 min) of it are linked, each with the strongest evidence there is:

| Evidence | The line | Needs |
|---|---|---|
| `face` | The same person (by face) | the same enrolled person or anonymous identity in both ([people.md](people.md)) |
| `look` | Probably the same person (clothes and timing) | `incident.look: true`, no face to compare, neighbouring cameras, similar clothes |
| `time` | At the same time a person | nothing more: someone was there then |

Only `face` and `look` are "the same person". The same person's best frame and clip
follow the alert as replies. Times of a camera whose video arrives late
(`cross_camera.lag_up_to`) are said as "about". When the links take longer than 8 s, the
alert goes without them and its text is edited when they are ready.

The links are kept on both events (`intent.incident`): the web app's event card shows
"With this alert" or "Part of an alert", the evening digest names the same-person links,
and the bot's answer to "what about the other camera?" reads them. A linked event the
judge had called ordinary is marked *reopened* when the alert was one that wakes for
anyone; it is not judged again.

### The clothing cue (`incident.look`, off by default)

Without a face, "the same person" can only come from how someone looks, which Vidette
otherwise never uses ([people.md](people.md#guardrails)). With `look: true`, the colours of
the clothes are compared, narrowly:

- the torso and legs only (rows 30 to 95 % of the detector's person box, its middle 70 %);
  the head (face, hair, skin) is left out, though hands may remain;
- only between an alert and the other cameras' events within minutes, on cameras in
  `neighbours` (all of them when none are declared);
- never stored (only the similarity goes into the link), never compared with anyone
  else's, never a reason to alert, raise a level or suspect anyone.

It is weak: at night in infrared only brightness counts (the bar is then 0.95 instead of
0.88), and dark clothes look alike. Measured on a test deployment: 0.91 for the same person
on two cameras, up to 0.89 for other people the same evening.

## Incident mode

An alert (not one a [calibration](calibration.md) step sent to the digest) makes
`vidette serve` write `incident-mode.json` next to its database, valid for
`incident.keep_for` (10 min; a second alert extends it). Until then:

- every collector started with `--serve-config` (or `--incident-file`) keeps every woken
  stream whole, however short, and a continuous camera keeps the stretches its motion gate
  did not cut, in 30 s pieces ([collect.md](collect.md#incident-mode));
- the [pre-filter](prefilter.md) skips nothing: what it would skip or leave for an idle
  slot is judged right after the clips with people;
- the judge is told, in one line, that a camera alerted minutes before, and to judge only
  what its frames show.

A missing or unreadable file means no incident: incident mode can only keep more.

## Long clips are judged around the person

Four frames chosen where the picture changed cover a short clip. In an 80 s clip where
headlights and animals keep the picture changing, they may miss the person entirely. For a
clip longer than `understanding.windows.long_clip` (30 s) in which the pre-filter saw a
person:

1. the detector looks at a frame a second (about 7 s of CPU for 80 s of video);
2. the person's track is cut into windows of at most `window` (20 s), each ending at a
   sighting; a sighting inside a stall gets the time of the frame before it;
3. the window with the **last** sighting is judged first (where the person ended up), then
   the frames the clip would have been judged on anyway (where the picture changed: the
   detector misses people half out of the picture, and nothing those frames show is lost),
   then the windows where the person is small or at the edge, then where they stayed
   longest in one place, at most `max_windows` (3) windows around the person; while other
   clips with people wait, a clip stops after three calls;
4. a small person, or one at the frame's edge, is enlarged: frame 1 is the whole picture,
   the others are cropped around them, in the picture's shape (no extra tokens);
5. a window whose verdict wakes for anyone decides at once; otherwise the highest
   alerting level wins; when nothing alerts, the clip keeps the verdict of the usual
   frames.

The track, the windows and each window's verdict are kept (`intent.windows`). On a test
deployment, an 81 s clip judged ordinary on four activity frames became a critical
alert: a window enlarged on a person 36 to 39 s in showed them at a parked car's door. Its
first window, with the person at the frame's very edge, was still judged as walking
past.

## Configure

```yaml
incident:
  enabled: true
  keep_for: 10m            # incident mode after an alert; 0s: off
  link_within: 3m
  neighbours: {}           # camera → [cameras a person can walk to]; empty: all
  look: false              # the clothing cue (above)
  look_threshold: 0.88
  look_threshold_grey: 0.95
  # mode_file: …           # default: incident-mode.json next to storage.database

understanding:
  windows:
    enabled: true          # needs the pre-filter's detector (understanding.triage)
    long_clip: 30s
    window: 20s
    max_windows: 3         # windows around the person; the usual frames are judged too
    track_every: 1s
```

## Budgets

- Compute: a person track per long clip with a person; three to five frames extracted
  per linked event when the clothing cue is on; nothing for short clips.
- Model calls: up to `max_windows` + 1 per long clip with a person instead of one (three
  while other clips with people wait; one when the first window decides). On a test
  deployment about 55 to 70 clips a day were long and had a person, most on one battery
  camera: up to about 150 to 220 more calls a day.
- Storage: incident mode keeps every woken stream for 10 minutes after an alert.
- Latency: the alert's text waits at most 8 s for its links.

## Limits

- Battery cameras sleep, stream late and stall; their times are the collector's, not the
  camera's on-screen clock, and a camera that did not wake recorded nothing to link.
- A person seen only by the detector, with no face and the clothing cue off, is linked
  only as "at the same time".
- A window still judges four frames: an act shorter than a second between two of them
  can be missed.
