# Known people: who the camera saw, told to the judge

A policy like "the household and friends are fine in the yard, strangers are not" needs
the judge to know who it is looking at. Known people is local face recognition for the
people a household enrolls: each incident's frames go through a face check on your own
machine, and the judge gets one line of facts, for example

```
Local face check (a separate model on this machine, over frames of the whole clip):
recognized Sam (0.71); faces that match nobody enrolled: 0; no clear face in 21 of 24
frames (too small, turned away or absent).
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
  crop stays with the clip (the footage already shows it) but no embedding of it is kept.
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
Vidette does not overrule an `alert` because a known face was in the clip.

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

## Privacy notes

- The people folder holds biometric data: back it up like a password store, not like
  photos. Encryption at rest is not implemented yet; file permissions and your disk's own
  encryption (FileVault, LUKS) are what protect it today.
- Face crops of everyone seen (also strangers) are kept next to the clips or in
  `media_dir/faces/` for the confirm buttons; they go when the clip or the media folder
  goes.
- Pointing cameras at public space and recognizing faces is regulated in many places;
  enroll only people who agreed, and check local rules.
