# The pre-filter: who waits for the judge

The live judge is a vision model, one verdict at a time, about a minute each on a shared
server. A camera that records two thousand clips a day gives it more than it can answer,
and the live queue answers the newest and drops what is older than 15 minutes: which
clips go unjudged is then chance. The pre-filter looks at every clip first, on this
machine and without a model call, and puts it in one of three classes, so the judge's
time goes to the clips that might matter. Design:
[ADR-0021](architecture/adr/0021-pre-filter-before-the-judge.md).

Status: 🚧 tested against fakes and real clips; replayed over a test deployment's stored
events ([measured](#measured-on-a-test-deployment)); running live there. Clip-folder
cameras only: a Frigate review item has no clip to look at and goes to the judge as
before.

## The three classes

| Class | What goes there | What happens |
|---|---|---|
| **now** | a person; a weaker person candidate; a vehicle that moves; a lamp switching on while things move; anything the detector could not check | judged first, newest first: people (and what was not checked, and the lamp) before weak candidates and moving vehicles with nobody seen; each waits 15 minutes, as without the pre-filter |
| **later** | an animal; something moved and nothing was recognised | judged only while the model's slot is idle; after `later_max_age` (10 minutes) it leaves the live queue, and deep analysis may still take it |
| **skip** | the detector found nobody and what changed was a light change, tiny motion, or only the usual movement of that camera; an early window with nobody in it yet | never judged; the event stays *observed* with "not judged: tiny motion" (or the reason), and the reason is kept on the event |

The rules, first match wins:

1. The detector did not look (no model, no frames, an unreadable clip, an error): *now*,
   or *later* when almost nothing changed. **Nothing is skipped without a detector
   check**, on any camera and whatever the policy says, a `wake: always` level included.
2. A person (detector score from `person`, 0.35): *now*.
3. A weaker person candidate (from `possible_person`, 0.2): *now*. Unclear goes to the
   judge, unless it is a person-like shape that is always there (below): a clip whose
   only candidates are such shapes is *later* at most, never skipped.
4. A vehicle that moves: *now* (`vehicles: later` makes it *later*). Parked cars are in
   every frame at the same place and do not count.
5. An animal: *later*.
6. The light changed (a lamp switched on) while things moved, and nobody was found:
   *now*. A floodlight's own sensor saw something warm, and the detector can miss a
   person over-exposed at the frame's edge.
7. An early window ([telegram.md](telegram.md#early-alerts-before-the-clip-ends)) with
   none of the above: *skip*. The whole clip follows and is looked at again; an early
   window *with* someone makes its whole clip *now*, even if the person has left the
   frame by then.
8. Fewer than three frames looked at: *later*.
9. Light change, the picture brightened or darkened and little else changed: *skip*.
10. Tiny motion, almost nothing changed and the collector's motion gate agrees: *skip*.
11. The usual movement: everything that changed is where things always move on this
    camera (below): *skip*.
12. Anything else: *later*.

During [incident mode](incidents.md#incident-mode) (the minutes after an alert), a whole
clip that would be *later* or *skip* is *now* instead, judged right after the clips with
people; the class it would have had is kept (`intent.triage.incident`).

## What it looks at

- **Up to `frames` (8) frames where the picture changed**, chosen exactly as the judge's
  frames are ([vlm.md](vlm.md#what-the-judge-sees-and-answers)), and the key frames the
  collector already wrote next to the clip (spread over it, nothing to decode). When
  nothing changed on the activity map, the clip is long and nobody was seen, also **a
  frame every 4 s** (at most 24, one ffmpeg run at 640 px): the map cannot see a person
  far away, and eight frames fifteen seconds apart over a two-minute wake period miss
  one walking past.
- **An object detector** on each of them: YOLOX-S (Apache-2.0, trained on COCO) from the
  OpenCV model zoo, through OpenCV's DNN module on the CPU. Only people, vehicles (car,
  truck, bus, motorcycle, bicycle) and animals count.
- **The activity map** the frames were chosen from: the clip's key frames as 64×36 grey
  thumbnails. How much of the picture changed, where, and how much of the change was
  global brightness (a light, a cloud, infrared switching on).
- **The sidecar** of a `vidette collect` clip ([collect.md](collect.md#what-lands-on-disk)):
  the motion gate's largest changed area, for continuous cameras.

**The usual movement** is learned per camera from the clips in which the detector found
nobody: a cell of the 64×36 grid that changed in at least a fifth of the last 300 such
clips is "usual" (a tree, an awning, a flag). A clip whose changes all fall inside the
usual cells (three cells of slack) is skipped. A clip with a person is never learned
from, so a person never becomes "usual".

**Person-like shapes that are always there** are learned the same way: a weak person
candidate (score 0.2 to 0.35) at a spot where one was found in at least 6 of the
camera's last 150 clips (a plant, a bag hanging on a wall, at night) no longer makes a
clip *now*. Only weak candidates are remembered and discounted; a confident person never
is, and a clip whose only candidates are such shapes is never skipped. After a restart
both memories are rebuilt from the decisions kept on the last day's events.

## Turn it on

```bash
pip install 'vidette[people]'    # OpenCV, as for the face check
vidette triage models             # YOLOX-S, 35.9 MB, pinned by sha256, into ~/.cache/vidette/models
```

```yaml
understanding:
  triage:
    enabled: true
    detector: yolox        # yolox | none (none: order by motion only, skip nothing)
    frames: 8              # where the picture changed, per clip
    key_frames: true       # also the collector's key frames
    threads: 2             # CPU threads for the detector
    skip: true             # false: only the order (now / later), nothing skipped
    vehicles: now          # now | later: a vehicle that moves, nobody seen
    later_max_age: 10m     # a later clip waits this long for an idle slot
    usual_movement: true   # learn where things always move on each camera
    person: 0.35           # detector scores
    possible_person: 0.2
    vehicle: 0.45
    animal: 0.35
```

`vidette serve` never downloads the model. Without it the pre-filter starts anyway,
orders clips by motion and skips none, and a start-up warning says how to fetch it.

## Seeing what it did

- **On every event**, `intent.triage`: the class, the reason in a few words, the facts
  (best person score, moving vehicles, frames looked at, how much changed) and how long
  the look took. A skipped event's summary is "not judged: …".
- **The bot's `/status`** line counts them since the start: first in line, only when idle (and how
  many waited too long), not judged.
- **`vidette policy dry-run`** adds a line: of the events it looked at, how many were
  *now*, *later* or skipped ([calibration.md](calibration.md#dry-run-what-a-policy-would-have-done)).
- **`vidette triage check CLIP…`** says what it would do with clips, and why:

  ```
  20261117-170321.mp4  skip   tiny motion  · person 0.00, moving vehicle 0.00, animal 0.00, 10 frames, changed 0.3 %, light 0.1 %, 4.1 s
  ```

## Measuring it before and after: `vidette triage replay`

```bash
vidette triage --config ~/.config/vidette/serve.yaml replay --days 3 --sample 0.25 --jobs 4
```

Every stored event of the last days with anything known about it is looked at again: an
alert, a 👍/👎, a reviewed label, a face, a person the live judge or deep analysis
counted, a verdict at a waking level. Of the rest, a random `--sample` (weighted per day
and camera, so a run stopped half way still estimates the day). The database is opened
read-only and no model is called; observations are cached (`--cache`), so trying other
thresholds later costs nothing. The report:

- **per day**, how many clips each class would get (estimated);
- **why**, how many clips each rule caught;
- **checks**, for each kind of evidence, how many events have it, how many would have
  been *later* and how many skipped, each skipped one listed. The rows marked *must be 0*
  are alerts the household confirmed or did not answer, reviewed labels of a waking
  scenario, verdicts of a waking scenario by the live judge or deep analysis, and readable
  faces;
- **the live judge's queue, simulated**: one slot, the measured seconds per verdict in
  turn, deep analysis' daytime turns, the queue's rules (newest first, 15 minutes; with
  the pre-filter *now* first and *later* for 10 minutes, early windows only for clips
  with a person). Next to it what the service really judged those days, so the model's
  error is visible.

`vidette eval --judge triage` does the same for a labelled set: it "alerts" when the
pre-filter would let a clip through, so its recall is the share of the set's alerts that
would not have been skipped (`triage-now`: the share judged first) ([eval.md](eval.md)).

## Measured on a test deployment

Three clip-folder cameras (one battery camera that wakes on motion, two streaming ones,
one with its own floodlight), four days in November 2026, about 2,000 clips a day, the
live judge a
27B vision model on a shared llama.cpp server (one slot, a median 43 s per verdict on
the last day). `vidette triage replay` looked at 2,257 of the 5,936 stored clips: all
1,099 with anything known about them and a 25 % sample of the rest, weighted per day and
camera. Default settings; the usual movement learned in time order as the service would
have. No model call.

**Clips a day per class** (estimated; 17 November up to 18:00):

| day | clips | now | later | skip |
|---|---|---|---|---|
| 15 Nov | 1,975 | 973 (49 %) | 388 (20 %) | 614 (31 %) |
| 16 Nov | 2,011 | 975 (48 %) | 577 (29 %) | 459 (23 %) |
| 17 Nov | 1,748 | 804 (46 %) | 430 (25 %) | 514 (29 %) |

Over the three days, of the *now* clips about two in five had a person (1,061) or a lamp
switching on with motion (11), and three in five a weak person candidate (418) or only a
moving car (1,299). The skips were tiny motion (1,558) and the usual movement (60); 17
clips went to *later* as person-like shapes that are always there. Both memories are
cold at the start of a replay, and a replay that looks at a sample of the clips learns
them more slowly than a service that sees all of them.

**What it would have skipped of what is known** (the rows that must be 0 are 0):

| what is known | clips | later | skipped |
|---|---|---|---|
| alerts the household marked 👍 | 1 | 0 | **0** |
| alerts nobody answered | 4 | 0 | **0** |
| alerts marked 👎 | 11 | 0 | 0 |
| reviewed labels of a scenario that wakes someone | 2 | 0 | **0** |
| a waking scenario by the live judge | 19 | 0 | **0** |
| a waking scenario by deep analysis | 4 | 0 | **0** |
| readable faces | 72 | 0 | **0** |
| faces the face check could not read | 274 | 22 | 17 |
| people counted by the live judge | 431 | 10 | 3 |
| people counted by deep analysis | 146 | 2 | 0 |

The 17 unreadable "faces" that would be skipped are patterns (a window, a box, a red
blur) on clips the judges called empty. The 3 clips with a person by the live judge are a
passer-by far off, a head in a doorway at the far end of the picture, and a driver seen
through a gate: none of them a scenario that wakes anyone. `vidette eval --judge
triage` on the 33 clips the household reviewed (3 that should alert): recall 1.00 (3 of
3, 95 % CI 0.44 to 1.00), one clip skipped (a cat).

**A scripted test** recorded one evening before the service ran (a person acting out the
critical scenarios: peering into windows, trying a door and a gate, touching equipment on
a wall; 80 clips on the three cameras, `vidette triage check`): 48 *now*, 19 *later*,
13 *skip*. A separate clip of someone touching equipment on the wall is *now* (a person at
0.86). Every one of the 13 skipped
clips was looked at frame by frame (a frame every 1.2 s): nobody in any of them, twice a
passing car's lights far off.

Two misses found on the way and fixed: a person walking past far down the street in a
two-minute clip in which nothing else changed was between the eight spread frames (now a
frame every 4 s for such clips), and a person at a gate at night, over-exposed at the
frame's edge while the floodlight switched on, was not found at all (now "the light
changed and something moved" is *now*).

**The judge's queue** (simulated: one slot, the last day's measured verdict times in
turn, deep analysis' daytime turns, early windows; next to what the service really did):

| day | judged then | simulated as it ran | with the pre-filter | clips with a person, then | with the pre-filter: person / weak candidate or car / later |
|---|---|---|---|---|---|
| 15 Nov | 38 % | 53 % | 48 % | 48 % | 98 % / 76 % / 30 % |
| 16 Nov | 33 % | 37 % | 37 % | 40 % | 93 % / 41 % / 20 % |
| 17 Nov | 39 % | 32 % | 37 % | 50 % | 95 % / 58 % / 15 % |

The share of all clips the judge reaches hardly changes: the slot was busy before and is
busy after. What changes is which clips it spends itself on: clips with a person went
from about half reached (and which half was chance) to 93 % or more, and the judge no
longer spends about 30 % of its verdicts on clips with nobody in them (41 % of the clips
the pre-filter skips had been judged). 15 November's simulation is off by 15 points because
its verdicts were slower (a longer prompt that day); the other days are within 7.

## Budgets

| | |
|---|---|
| Compute | per clip, choosing frames (ffmpeg, about 1 s) and the detector on 8 to 16 frames; measured on an Apple-silicon laptop that also runs three collectors: median 4.4 s, about 5 CPU-seconds. At 2,000 clips a day about 3 CPU-hours a day, on two threads |
| Storage | about 1 kB of JSON per event (`intent.triage`); the frames it looked at are deleted at once |
| Latency | the look before a clip waits for the judge, about 2 to 5 s; a clip that the old queue would have judged at once now waits for the look first |
| Memory | about 150 MB for the detector |
| Threads | `threads` (2) is OpenCV's setting for the whole process, so the face check runs on as many |
| Money | none: nothing leaves the machine |

## Limits

- A person the detector misses in every frame of a quiet clip can be skipped: dark,
  far, mostly hidden or motion-blurred, on a clip where little changed. Busy clips with no
  detection go to *later*, not to *skip*. The checks above are how this is watched.
- The usual movement could, in principle, learn a spot where people also pass by often; it
  only ever skips clips in which the detector found nobody.
- The early windows of the past are pruned after an hour, so a replay cannot look at
  them: a clip whose person was only in its early window shows up there as skipped, while
  live it would have been judged (rule 6).
- Thresholds were chosen on one deployment's clips (three cameras, one week). Check
  yours with `vidette triage replay` before trusting the defaults.
