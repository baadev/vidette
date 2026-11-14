# ADR-0012: Known people: local face recognition that informs the judge

- **Status:** accepted
- **Date:** 2026-11-14
- **Amends:** the out-of-scope row "Face recognition: Frigate does it" in
  [ROADMAP.md](../../../ROADMAP.md) for incidents that do not come from Frigate (clip
  folders, [ADR-0010](0010-clip-folders.md)), and gives the designed "trusted faces"
  feature ([faq](../../faq.md#what-about-face-recognition),
  [ai-pipeline](../ai-pipeline.md#trusted-faces-suppression--m4)) its first code. Every
  guardrail written there stands; this ADR records how each one is kept.

## Context

- The VLM judge sees a person in the garden and cannot know whether it is someone who lives
  there. A policy such as "people of the household and friends are fine in the garden,
  strangers are not" is the kind of rule Vidette exists for, and the judge cannot follow
  it without being told who it saw.
- Frigate (0.16+) recognizes faces and publishes them as sub-labels, which the incident
  model already carries. Without Frigate, incidents come from
  clip folders filled by `vidette collect`, so nothing upstream names anyone.
- Households want to enroll themselves and then confirm or correct what the camera
  recognized, from the web app and from the phone review of contact sheets.
- Faces are biometric data. The project's lines are fixed: opt-in, local only, enrolled
  people only, no stranger identification, no cloud biometrics, uncertain matches never
  quiet an alert ([principles](../../project/principles.md#4-behavior-first-identity-only-by-consent)).
- Vidette is Apache-2.0 and keeps its default install free of non-commercial or AGPL
  parts ([ADR-0006](0006-license.md)). Most popular face embedders are not usable:
  InsightFace's model packs (buffalo_l and friends, ArcFace weights) are licensed for
  non-commercial research only.

## Decision

1. **Detector and embedder: YuNet + SFace from the OpenCV model zoo**, through
   `cv2.FaceDetectorYN` and `cv2.FaceRecognizerSF` in `opencv-python-headless`
   (Apache-2.0). YuNet is MIT, SFace Apache-2.0. The two files (0.2 MB and 39 MB) are
   downloaded on first use by a command-line tool into `~/.cache/vidette/models`, from a
   pinned opencv_zoo commit, and checked against a pinned sha256 before every load; a
   file that fails the check is never loaded. `vidette serve` never downloads anything:
   `vidette people models` does it once, loudly.
2. **An opt-in extra, not a core dependency:** `pip install 'vidette[people]'`. Without
   it nothing changes: no import of OpenCV, no face check, no People page content.
   `people.enabled` is false by default.
3. **The people folder** (`people.dir`, default `$VIDETTE_DATA/people`, else
   `~/vidette-data/people`) holds, per person, the photos given to enroll them, aligned
   112×112 face crops, their embeddings and later confirmations. Folders are 0700, files
   0600, written atomically under a lock. The store refuses a folder inside a git work
   tree, like `vidette collect`. Deleting a person removes everything the store wrote.
4. **What the judge is told is one line of facts**, computed before the VLM call:
   who was recognized and how sure, how many readable faces matched nobody, and in how
   many frames no face could be told ("Local face check: recognized Sam (0.83); faces
   that match nobody enrolled: 1; no clear face in 15 of 18 frames"). Names go only to a
   local model; a cloud model hears "a person the household enrolled as known". The
   judge never receives reference photos, never the list of enrolled people, and never
   the nearest name of a face that did not match. The line says explicitly that an
   unseen or unrecognized face is not evidence of a stranger.
5. **Three kinds of face**, so that "too small to tell" is never reported as "a
   stranger": *known* (readable, at or above the person's threshold, not tied with
   another person), *unknown* (readable, matching nobody), *unclear* (too small, below
   40 px by default, uncertain, turned away, or with a weak embedding). Unclear faces are
   counted per frame only.
6. **Policies may say "known people do not wake"**: `known_people: quiet` on a policy
   (or `--known-people quiet` for `vidette eval`) adds that rule to the judge's prompt.
   The VLM still decides, because it sees what the face check cannot: a second person
   whose face never turns to the camera. Vidette does not override an `alert` verdict
   because a known face was in the clip; that would trade the fail-toward-alerting rule
   for convenience.
7. **Confirmations teach the matcher without storing strangers.** "This is Robin" adds
   the face crop as one of Robin's references (duplicates dropped, at most 60 faces a
   person, confirmed faces recycled oldest first). "A stranger" on a face that had
   matched someone raises that person's own threshold just above the wrong score; the
   stranger's face is not stored in the people folder. "Not sure" is recorded only.
   Answers come from the event card in the web app, `vidette people confirm`, and the
   contact-sheet reply (`5 Robin`, `5.2 stranger`).
8. **Every face check is recorded where its incident lives:** in the clip's sidecar
   (`people`) with crops under the dataset's `faces/` folder, or in the event's
   `intent.people` with crops under `media_dir/faces/`. Unknown faces keep a crop (the
   footage already contains them) but never an embedding.

## Consequences

- ✅ A plain-language policy can distinguish the household from strangers on cameras
  without Frigate, and `vidette eval --people` measures whether the line helps.
- ✅ No non-commercial weights, no cloud call, no new daemon; the default install is
  unchanged.
- ⚠️ **Security cameras see small faces.** On 1920×1080 cameras, faces at the far end of the picture were 15 to 25 px tall and could
  not be told; faces of 90 px and more were recognized at 0.56 to 0.66, while phone-screenshot
  references of the same people matched each other at no more than 0.29. Most clips have
  no readable face at all, and the report says so instead of guessing.
  These are early observations, not a measured rate; a rate needs labelled
  clips and `vidette eval`.
- ⚠️ Compute: about 1.3 s per clip on an Apple-silicon Mac (24 frames at 1920×1080,
  each searched at full and half size, using about 3.5 cores), before a VLM call that
  takes 5 to 60 s. Storage: 39 MB of models; about 5 KB per face crop.
- ⚠️ The SFace weights are Apache-2.0 as published, but like every public face embedder
  they were trained on web-scraped face datasets. That provenance is recorded here, not
  resolved.
- ⚠️ Not done yet: encryption at rest (the guardrail in the FAQ). Today the protection is
  file permissions plus the disk's own encryption. Tracked in the roadmap.
- ⚠️ Matching is per face, not per person track: two people with similar faces, twins or a
  person whose references are all from one angle will produce errors; ties between two
  enrolled people are reported as unknown.
- **Tripwire:** if the people folder ever needs a server, a sync, an import from a
  third-party identity source, or matching of faces nobody enrolled, stop: that is
  identification, which this project does not build. If Frigate's own face recognition
  runs for a camera, prefer its sub-labels for that camera over a second matcher.

## Alternatives considered

- **InsightFace buffalo_l / ArcFace (ResNet50/100):** better accuracy on small faces;
  model weights licensed for non-commercial research only. Not shippable here.
- **AuraFace (fal.ai, Apache-2.0, ResNet100 trained on permissively licensed data):** the
  most promising permissive upgrade for accuracy; about 250 MB and slower on CPU. Worth an
  evaluation on the labelled set before switching; the engine boundary
  (`vidette.people.engine.FaceEngine`) is where it would plug in.
- **dlib / `face_recognition`:** permissive, but needs a native dlib build and its
  embedder is weaker than SFace on low-resolution faces.
- **Ask the VLM to recognize people from reference photos:** sends biometric references
  to the judge (to a vendor, for a cloud judge), costs a call's worth of images per
  person, and is not measurable as a matcher. Rejected on principle.
- **Frigate's face recognition only:** right answer where Frigate runs; no answer for
  clip folders.
