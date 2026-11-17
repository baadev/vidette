# Known people: who the camera saw, told to the judge

A policy like "the household and friends are fine in the yard, strangers are not" needs
the judge to know who it is looking at. Known people is local face recognition for the
people a household enrolls: each incident's frames go through a face check on your own
machine, and the judge gets one line of facts, for example

```
Local face check over the whole clip: recognized Sam (0.71); faces matching nobody
enrolled: 0; no clear face in 21 of 24 frames.
An unrecognized or unseen face is not evidence of a stranger; judge what people do.
```

Then you confirm or correct what was recognized, and those answers become references.
Design and guardrails: [ADR-0012](architecture/adr/0012-known-people.md).

Status: 🚧 every path is built and tested with a fake engine (CLI, label drafts, eval, the
live worker, the web app); enrolling, `scan`, `confirm` and the judge's line were run on real
clips with a local VLM. No accuracy figure exists yet: it needs
labelled clips and `vidette eval --people`.

## The rules it keeps

- **Opt-in and local.** Off by default; the models run on your machine; nothing is sent
  anywhere. Enroll only people who agreed to it.
- **Enrolled people only.** A face that matches nobody is "unknown", never identified. Its
  crop stays with the clip (the footage already shows it) but no embedding of it is kept,
  unless you turn on [anonymous identities](#strangers-who-come-back) (`track_unknown`,
  off by default): then it is kept as a numbered stranger, never named, for 30 days.
- **Unclear is not unknown.** A face too small, blurred or turned away to tell is counted
  as "no clear face", never as a stranger. Security cameras see many such faces.
- **Names go to a local judge only.** A cloud judge hears "a person the household enrolled
  as known". No judge ever receives reference photos, the list of enrolled people, or the
  nearest name of a face that did not match.
- **Uncertain matches never quiet anything.** Below the threshold, or tied between two
  people, a face is not named.
- **Biometric data stays out of git.** The people folder (and every face crop) is refused
  inside a git work tree; folders are 0700 and files 0600.

## Set up

```bash
cd ~/development/vidette/server
uv sync --extra people          # or: pip install 'vidette[people]'  (OpenCV, numpy)
uv run vidette people models    # once: YuNet (MIT) + SFace (Apache-2.0), 39 MB, sha256-checked
```

The models come from the OpenCV model zoo at a pinned commit and go to
`~/.cache/vidette/models` (`people.models_dir` to change it). Command-line tools fetch them
on first use and say so; `vidette serve` never downloads anything.

```yaml
people:
  enabled: true
  dir: ~/vidette-data/people     # default: $VIDETTE_DATA/people, else ~/vidette-data/people
  threshold: 0.4                 # SFace cosine similarity for a match
  min_face_px: 40                # smaller faces are "unclear"
  frames_per_second: 2           # sampled from each clip, at full resolution
  max_frames: 24
  max_faces_per_person: 60

policies:
  - name: yard
    description: Tell me about strangers in the yard. The household and friends are fine.
    known_people: quiet          # people recognized as known never need an alert by themselves
```

`known_people: quiet` adds that rule to the judge's prompt; the judge still decides, because
it sees what the face check cannot (a second person whose face never turns to the camera).
Vidette does not overrule an `alert` because a known face was in the clip. A policy with
[levels](telegram.md#levels-who-wakes-for-what) can say it for one level, `wake:
unless_known`: such an alert goes to the digest instead of waking anyone only when the
face check recognized everyone the judge counted ([ADR-0013](architecture/adr/0013-telegram-bot.md)).

## Enroll

```bash
uv run vidette people add Sam selfie.jpg grid.png
#   Sam (sam) in /Users/you/vidette-data/people
#     grid.png: 27 face(s), 25 readable, 2 too small or unclear
#     selfie.jpg: 1 face(s), 1 readable
#   added 26 face(s); 0 duplicate(s) skipped; Sam now has 26 reference faces
uv run vidette people sheet Sam /tmp/sam-faces.jpg   # look at what was enrolled
uv run vidette people list
```

Every readable face in a photo becomes a reference, so a screenshot of many faces works
(for example a camera app's own face gallery). A face much closer to someone else already
enrolled, or (in a batch) unlike every other face, is skipped and named in the output.
Large photos are shrunk to 1280 px first, because the detector is trained on faces up to
about 300 px.

The **People** page in the web app does the same: add a person, upload photos, see and
remove reference faces, delete a person (everything stored for them goes; files you put in
the folder by hand stay).

## Check clips

```bash
uv run vidette people match clip.mp4 frames/ a.jpg     # prints, writes nothing
uv run vidette people scan ~/vidette-data/porch       # every clip without a report yet
#   20261114-210304: Sam 0.63, 2 unknown  (24 frames, 10 with a readable face, …)
#     1. Sam 0.63; 6 face(s) in frames 16,17,18,20,21,24; up to 457 px  faces/…/1.jpg
#     2. unknown 0.28, nearest sam; 3 face(s) in frames 13,14,15; up to 95 px  faces/…/2.jpg
```

`scan` writes the report into the clip's sidecar (`clips/<id>.json`, key `people`; other
clips get `faces/<id>/people.json`) and the best crop of each person seen to
`faces/<id>/<n>.jpg`. `--all` checks clips again, `--since 20261114-2100` and `--clip ID`
narrow it down. The "nearest" name is for you only; it is never sent to a model.

`vidette label draft --people` (or `people.enabled` in the config) runs the check before
each draft, so the labeller hears who was recognized; `vidette eval --people` does it for
every entry, and `--known-people quiet` tries the rule without editing the dataset.
Live, `vidette serve` checks every incident on a camera with a policy before judging it,
and keeps the report with the event.

## Teach it

| Where | "This is Sam" | "A stranger" | "Not sure" |
|---|---|---|---|
| Web app, event card | the **This is Sam** button | **Stranger** | **Not sure** |
| Telegram alert ([telegram.md](telegram.md)) | **Who is it?** → **One of us: Sam** | **Stranger** | |
| Contact-sheet reply | `5 Sam` (`5.2 Sam` for face 2) | `5 stranger` (`чужой`) | `5.2 ?` |
| Command line | `vidette people confirm CLIP 2 Sam` | `… 2 stranger` | `… 2 unsure` |

- "This is Sam" adds that face crop to Sam's references (duplicates are dropped; at most
  60 faces a person, confirmed faces are recycled oldest first). A face confirmed that way
  scores 1.00 if the same clip is checked again: it is now a reference. Judge the gain on
  other clips.
- "A stranger" on a face that had matched someone raises that person's own threshold just
  above the wrong score. Nothing about the stranger is stored in the people folder.
- On a contact sheet, the last line of a cell lists the faces by number
  (`faces: 1 Sam .63  2 ? .28`); `5 ?` alone still means "leave the scenario unlabelled".

## Strangers who come back

A person who walks past once and a person who walks past four times in an afternoon,
stops at the gate and later shows up in the yard look the same to the judge, clip by
clip. With `track_unknown`, everyone whose face the check finds and who is not enrolled
gets an anonymous identity: "stranger #12", seen 3 times today, on two cameras. Nobody
is named; you may give an identity a name, mark it familiar or suspicious, merge or split
it, or say it is someone you know, and take any of that back. Design and guardrails:
[ADR-0014](architecture/adr/0014-anonymous-identities.md),
[ADR-0016](architecture/adr/0016-every-face-reversible-decisions.md).

Status: 🚧 built and tested with a fake engine; backfilled over real
clips. No accuracy figure: there is no labelled identity set yet.

**Read [privacy and the law](#privacy-and-the-law) before you turn it on.** It keeps face
crops and embeddings of passers-by who never agreed to anything.

```yaml
people:
  enabled: true
  track_unknown: true              # off by default
  identities:
    dir: ~/vidette-data/identities # default $VIDETTE_DATA/identities; never in a git tree
    match: 0.5                     # SFace cosine for a face to join an identity
    margin: 0.06                   # ...and no other identity this close to it
    min_face_px: 40                # readable faces smaller than this start a weak identity
    near_known: 0.3                # this close to someone enrolled: marked, never raises
    retention_days: 30             # unnamed, unflagged identities go after this long
  extra_fps: 6                     # a second look around every face, for its best moment
  extra_frames: 24                 # at most this many more frames a clip (0: off)

policies:
  - name: home
    levels: {critical: …, elevated: …, none: …}   # docs/telegram.md#levels-who-wakes-for-what
    recurrence:                    # optional: repeated presence raises the level
      raise_to: elevated
      sightings: 3                 # the same face 3 times...
      within: 24h                  # ...within a day, this time included
      cameras: 2                   # or on 2 cameras...
      cameras_within: 10m          # ...within 10 minutes (cameras: null turns it off)
```

### How a face becomes an identity

- Wherever the check finds a face, it looks at the clip again around it (6 frames a
  second, at most 24 more frames) and keeps the best moment of each person: detector
  confidence × sharpness × size × frontalness. That moment is the crop you see, the crop
  and the embedding an identity keeps. On ten real clips with faces this found 142
  readable faces instead of 40, and the kept crop got sharper in 8 of 10.
- Known people come first: a face that matched someone enrolled is theirs.
- A readable face (40 px and more, clear and frontal enough) joins the identity it
  resembles at `match` (0.50) or above, when no other identity is within `margin` (0.06);
  close to two identities, it joins neither. Otherwise it starts a new identity. A face
  within `near_known` (0.30) of someone enrolled gets one too, marked "close to someone
  enrolled": it never raises a level, and the card offers that person first.
- A face too small or blurred to compare is followed from frame to frame by where it is
  and starts a **weak** identity (marked "small face"): nothing joins it automatically;
  you merge it by hand. A person whose face was never found gets no identity, and the
  card says "No face found".
- The thresholds are strict on purpose. Two strangers in one identity invent visits that
  never happened; one person split in two only undercounts, and a merge fixes it. On the
  real clips the same person scored 0.56 to 0.66 with faces of 90 px and more, two
  different people at most 0.29; small faces score lower, so a person seen only from
  afar mostly gets a new number each time. Expect duplicates; the Identities view
  suggests them.
- Nothing about how anyone looks is used: no age, gender, skin colour or clothing, not in
  matching, not in the judge's facts, not in rules. Only the face embedding, the time and
  the camera. One opt-in exception, outside face matching: `incident.look` compares the
  colours of the clothes (never the head) between an alert and the other cameras' events
  of the same minutes, only to say "probably the same person" next to the alert; never
  stored, never a reason to alert ([incidents.md](incidents.md#the-clothing-cue-incidentlook-off-by-default)).

### Change your mind

Every answer about a face can be changed or taken back, on the event card (**Change**,
**Undo**) and in Telegram (**Take it back**): the answer remembers what it changed (a
reference added to Sam, a raised threshold, a sighting added or moved, an identity
promoted) and takes back exactly that.

| Answer | What happens |
|---|---|
| This is Sam | the face becomes one of Sam's references; its identity, if any, becomes Sam |
| Stranger | the face is kept as an identity (a new one if it had none) |
| Not sure | recorded only |
| Familiar… | a new identity with the name you type, marked familiar |
| New identity… | a new identity, a name optional |
| Add to… | into an identity you pick |

A face of any size can be put in an identity by hand; one under 64 px says it may not
be recognized next time, because small faces rarely match even the same person.

**Familiar** ("familiar, do not alert") is for people who are neither household nor
strangers: a neighbour, the postman. For alerts they count like the household: an event
where everyone recognized is household or familiar goes to the digest at an
`unless_known` level, a `known_people: quiet` policy tells the judge they need no alert,
and they never raise a level by recurrence. The critical list (`wake: always`) still
wakes, exactly as for the household.

### What the judge and the bot hear

The face check's line gets one more sentence:

```
Unknown faces against earlier clips (anonymous, by face only): unknown face #12: this is
the 3rd time today, 5 times in total on 2 days; cameras in the last hour: street,
front_door.
Coming back, or looking at one place then another, deserves more attention than a single
pass; alone it proves no intent.
```

A cloud judge hears the same counts and camera names, never a crop, an embedding or a
name you gave. A policy's `recurrence` rule raises a verdict that named a scenario of a
lower level (a `passer_by` at a `never` level, say) to `raise_to` when the same identity
was seen `sightings` times within `within`, or on `cameras` cameras within
`cameras_within`. An identity you marked suspicious raises on any sighting; familiar and
known ones never do, nor one close to someone enrolled. `other`, an abstention and anything already at or above `raise_to`
stay as the judge said; nothing is lowered. `vidette eval` does not apply the rule.

The Telegram alert says who: `👤 stranger #12, 3rd time today (Street, Yard within the
hour)`, `familiar: Neighbour`, `stranger #40 (small face)`, `no face found`, and `· level
raised: not the first visit` when the rule raised it. **Who is it?** under the alert
opens a menu per face: **One of us: …** (an enrolled person: the whole identity becomes
them), **Familiar: …** (a familiar identity), **New familiar…** and **New identity…** (the
bot asks for a name: reply to its message), **Stranger**, **Take it back**. With
`language: ru` the same reads «незнакомец №12, 3-й раз за сегодня», «Кто это?». Identity crops
are never sent to Telegram or anywhere else.

### The Identities view

People → **Identities** in the web app lists every identity with its best face, how many
times and on how many days it was seen, when last and on which cameras; a filter by
status (suspicious, unknown, familiar, known). **Maybe the same person** shows pairs
whose faces are alike enough to ask (0.35 and more, small faces marked) with **Same
person** to merge them in one tap; **Select…** merges several at once into the one seen
most. An identity's page shows every sighting with its face and its clip, and identities
that look alike. From there: a name (only you and a local judge see it), **Familiar:
don't alert**, **Mark suspicious**, **Same person** (merge), **Split…** (select the
sightings that are someone else), **Move to…** on one sighting (another identity, a new
one, or a known person), **Known** (an enrolled person or a new one; **Not … : take
back** undoes it), **Delete identity**. Reading needs `read:config`, changes
`write:config`, like the People page.

### The merge pass: one person, found later

Live matching decides once, when a face arrives. The merge pass looks at the whole store
afterwards and joins identities that are certainly one person, every merge audited and
reversible ([ADR-0018](architecture/adr/0018-identity-merge-pass.md)):

- **the same moment**: faces at least 0.85 alike on one camera within 10 minutes (the same
  pixels cut into two clips score 1.0; two people seen at once scored at most 0.74);
- **clear faces**: a best pair of 0.5 or more (the live join threshold), no pair of their
  clear faces below 0.4, each the other's closest by 0.06.

Never merged: two identities seen in one clip, sightings you kept apart (a split, an
undone merge), two identities you both named or marked, one close to someone enrolled
with one that is not, a known person. A group keeps the number, name and status of the
one you marked (else of the one seen most); what you named or marked is never moved
away. Small faces at other times or on other cameras are never merged: blurred faces of
different people look alike.

```bash
uv run vidette people --config ~/.config/vidette/serve.yaml merge          # the plan, dry run
#   clear-face pairs, best score:  n=2145  >=0.45:21 >=0.5:4 >=0.55:0 …
#   one camera, close in time:     n=1872  >=0.7:32 >=0.85:9 >=0.95:5 …
#   not merged: clear: another candidate within the margin: 4
#   plan: 7 merge(s) in 6 group(s)
uv run vidette people --config ~/.config/vidette/serve.yaml merge --apply  # backup, then merge
uv run vidette people --config ~/.config/vidette/serve.yaml merge --log
uv run vidette people --config ~/.config/vidette/serve.yaml merge --undo 20261116-153000
uv run vidette people --config ~/.config/vidette/serve.yaml merge --unmerge 12
```

`vidette serve` runs it after new sightings, at most every 30 minutes
(`people.identities.merge`, `auto: false` to leave it to you), and copies the database to
`identities/backups/` (0600, the last five) before any pass that merges. An undone merge
brings the identity back with its number and keeps its sightings apart from the rest
from then on. A number that a merge removed (in an old alert, on an event card) opens the
identity it went into.

### Fill it from the clips you have

```bash
uv run vidette people --config ~/.config/vidette/serve.yaml \
    --env-file ~/.config/vidette/telegram.env identities backfill
#   412 clip(s) to look at, oldest first; identities in /Users/you/vidette-data/identities
#     2026-11-02 18:02 front_door: #1 (new)
#   done: 412 clip(s), 31 unknown face group(s) (joined 6, new 9, too small 16, …)
uv run vidette people identities list
uv run vidette people identities expire          # retention now (serve does it hourly)
uv run vidette people identities delete 12
```

The backfill reads the config's `clip_folders`, oldest clip first, and writes only to the
identities folder: no report, crop or label next to the clips. It needs `track_unknown`
on, so it never builds a store that nothing prunes, and skips clips older than
`retention_days`, which would be deleted at once. A clip it looked at is not looked at
again (`--all` does); `--verbose` says for every unknown face why it joined an identity,
started one or neither (too small, close to someone enrolled, close to two identities).

### Privacy and the law

- **These are biometric data of people who never agreed to anything.** Comparing face
  embeddings to tell people apart is processing biometric data; in the EU that is a
  special category under GDPR Art. 9, and a camera that covers a street, a sidewalk or a
  neighbour's property is not "purely personal or household activity" (CJEU, C-212/13
  *Ryneš*), so the household exemption does not cover it. Other places have their own
  rules (Illinois' BIPA, for example). Whether you may run this is your question, not the
  software's; if in doubt, leave it off, or point it only at your own property.
- **Local only.** Matching runs on your machine, the identities stay in
  `people.identities.dir` (0700, files 0600, refused inside a git repository), and
  nothing is sent anywhere. A cloud judge gets counts and camera names only.
- **Retention.** An identity nobody named or flagged is deleted 30 days
  (`retention_days`) after it was last seen, with all its crops and embeddings. Named,
  suspicious, familiar and known identities stay until you delete them; deleting removes
  everything the identity holds (SQLite `secure_delete`, the log truncated). The folder
  can be wiped as a whole: `rm -r ~/vidette-data/identities`.
- **Keep it out of backups you do not control**, like the people folder.

## What to expect from security cameras

Recognition needs a face of roughly 60 px or more turned towards the camera. On 1920×1080
cameras in evening light:

- a person at the far end of the picture had a face of 15 to 25 px: not readable, reported as "no clear face", not as a stranger;
- faces of 90 to 560 px (someone walking up to the camera) were recognized at 0.56 to
  0.66 against references from a phone app's face gallery, while references of
  different people matched each other at no more than 0.29;
- a face seen from steeply above (the top of a head) is caught by the quality gate
  (`min_quality`) only part of the time and may show up as "unknown".

Those are early observations, not rates. What helps: a camera at face height
near the door, more pixels on the entrance (a narrower lens or a second stream), light on
faces at night, and confirmations from the camera itself (references from the same camera
and angle match much better than selfies).

## Budgets

| | |
|---|---|
| Compute | about 1.3 s per clip on an Apple-silicon Mac (24 frames at 1920×1080, full and half size, about 3.5 cores); faces are matched in plain Python |
| Storage | 39 MB of models; about 5 KB per face crop; a person with 60 faces and two photos is under 1 MB |
| Latency | the check runs before the VLM call and adds 1 to 2 s to a verdict |
| Identities (`track_unknown`) | matching in plain Python over at most 40 references per identity: about 20 to 35 ms per sighting (database write included) at a few hundred identities, measured on a busy Apple-silicon Mac; about 5 KB of crop and 1.5 KB of embeddings per sighting; a backfill costs one face check per clip (about 1.3 s) |
| Second look (`extra_frames`) | for a clip with a face, about 23 more frames (232 over ten clips with faces): roughly as much again as the first pass, 1 to 2 s more before the judge; nothing for clips without a face |

## Privacy notes

- Anonymous identities of strangers have their own rules: [privacy and the
  law](#privacy-and-the-law).
- The people folder holds biometric data: back it up like a password store, not like
  photos. Encryption at rest is not implemented yet; file permissions and your disk's own
  encryption (FileVault, LUKS) are what protect it today.
- Face crops of everyone seen (also strangers) are kept next to the clips or in
  `media_dir/faces/` for the confirm buttons; they go when the clip or the media folder
  goes.
- Pointing cameras at public space and recognizing faces is regulated in many places;
  enroll only people who agreed, and check local rules.
