# ADR-0021: A pre-filter before the live judge: a local detector decides what waits for the model

- **Status:** accepted, amends 0019 (who gets the shared slot) and the verdict worker's
  queue
- **Date:** 2026-11-17

## Context

The live judge is a vision model on a shared server, one request at a time, about 40 to
60 s per verdict. On a test deployment with three clip-folder cameras, about 2,000 clips
arrived a day and the judge reached about a third of them: the queue is newest first and
drops what is older than 15 minutes, so in a busy hour most clips were never judged. Which
ones were dropped was chance. A clip of someone at the gate was as likely to age out as a
clip of a branch in the wind, and deep analysis (ADR-0019) inherited the same backlog as
"never judged".

Most of those clips have nobody in them: one camera's clips are mostly an awning and a
plant moving, the battery camera wakes for nothing, light changes at dusk. Telling
"nobody there" from "somebody there" does not need a 27B model. A small object detector
on the CPU does it in a fraction of a second per frame, and the clip's own motion facts
(the collector's sidecar, an activity map of tiny grey key frames) say whether anything
changed at all.

The risk is the obvious one: a clip that is thrown away is never judged, and a missed
alert is worse than a late one.

## Decision

- **Three classes, decided once per clip before it waits for the judge**, kept on the
  event (`intent.triage`: class, a short reason, the facts it rests on):
  - *now*: a person, a weaker person candidate, a vehicle that moves, a lamp switching
    on while things move, or anything the detector could not check. Judged first,
    newest first, as before, with people (and what was not checked, and the lamp) ahead
    of weak candidates and moving vehicles with nobody seen: on a test deployment cars
    driving past were half of the *now* clips, and nine in ten of those and of the
    weak candidates are ordinary traffic or no person at all.
  - *later*: an animal, or something moved and nothing was recognised. Judged only when
    the model's slot is idle; it waits 10 minutes, then leaves the live queue. Deep
    analysis may still take it.
  - *skip*: the detector looked and found nobody, and what changed was a light change,
    tiny motion, or only the usual movement of that camera; or an early window with
    nobody in it (the whole clip follows and is looked at again). Never judged; the event
    stays observed with "not judged: …".
- **No skip without a detector check.** A clip the detector did not look at (no model,
  no frames, an unreadable clip, an error) is never skipped, on any camera and under any
  policy, a `wake: always` level included. Without the model the pre-filter still orders
  (motion first) but skips nothing.
- **The detector is YOLOX-S (Apache-2.0, COCO) from the OpenCV model zoo**, run by
  OpenCV's DNN module, which the opt-in `vidette[people]` extra already installs: no new
  dependency. The 36 MB model is pinned by commit and sha256 like the face models,
  downloaded only by `vidette triage models`, never by `vidette serve`. Two CPU threads.
- **What it looks at:** up to 8 frames where the picture changed (the judge's own frame
  choice) plus the key frames the collector already wrote, the activity map those frames
  were chosen from (64×36 grey thumbnails of the key frames: where and how much changed,
  and how much of it was a global brightness change), and the sidecar's motion facts.
  When the map saw nothing on a long clip and nobody was found, a frame every 4 s as
  well: on a test deployment a person walking past far down the street was between the
  eight spread frames of a two-minute clip. Parked cars are in every frame at the same
  place and do not count.
- **The usual movement is learned per camera** from the clips the detector found empty:
  cells of the activity grid that changed in at least a fifth of the last 300 such clips.
  A clip whose changes are all inside them is skipped. A clip with a person is never
  learned from. The memory is rebuilt after a restart from the decisions stored on the
  last day's events.
- **An early window with someone in it carries over:** its whole clip is judged now even
  when the person has left the frame by then.
- **Deep analysis reads the class:** a clip the live judge never reached is ranked by
  what the pre-filter saw (a person as "person"), and a skipped clip is not queued.
  Deep analysis waits only for *now* clips, not for *later* ones.
- **Measured before it is switched on, and auditable after:** `vidette triage replay`
  runs it over a service's stored events (read-only, no model call) and reports what it
  would have skipped of everything known (alerts, marks, reviewed labels, faces, people
  the judges counted), and a simulation of the queue; `vidette eval --judge triage` gives
  the share of a labelled set's alerts it lets through; `vidette policy dry-run` shows
  what it skipped per day.
- **Off by default** (`understanding.triage.enabled`); `skip: false` keeps the ordering
  without skipping anything.

## Alternatives considered

- **Frigate's own detection.** Under Frigate, a review item already means a person or a
  car in a zone, and the pre-filter has no clip to look at (Frigate events pass as *now*).
  Clip folders have no detector of their own; this is for them.
- **NanoDet-Plus (3.8 MB, same zoo).** A tenth of the size, but weaker on small and
  distant people, which is exactly where a street camera's people are.
- **YOLOv8/YOLO11 nano.** Good and small, but AGPL-3.0: not something an Apache-2.0
  project should make its users download by default.
- **onnxruntime or Core ML.** Faster, but a new native dependency; OpenCV is already there
  for the face check.
- **A smaller live model.** Cheaper per call, but it would still be asked about every
  clip and would be a second judge with its own errors; the pre-filter only decides
  whether to ask.
- **Raising the model's throughput.** Not ours to raise: the server is shared, and
  Vidette has one slot of it.

## Consequences

- The slot goes to clips with people and moving vehicles first; clips with nobody in them
  stop taking it. How much the judged share of *now* clips rises, and what it skipped of
  the known alerts, is measured on a test deployment in docs/prefilter.md.
- About 4 to 5 CPU-seconds per clip on the machine that runs `vidette serve` (frame
  choice plus the detector), and about 2 s added before a *now* clip reaches the judge.
- A person the detector misses in every frame of a quiet clip (dark, far, mostly hidden,
  motion-blurred) can be skipped; on busy clips such a miss only costs priority
  (*later*), since skips need little change or only usual change. The replay's checks are
  how this is watched, and the reason on every skipped event is how it is audited.
- The usual-movement memory makes the same camera's quiet clips cheaper over time, and it
  could, in principle, learn a region where people also pass; it only ever skips clips in
  which the detector found nobody.
