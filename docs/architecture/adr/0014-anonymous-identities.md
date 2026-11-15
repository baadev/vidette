# ADR-0014: Anonymous identities: unknown faces that come back, counted for the judge

- **Status:** accepted
- **Date:** 2026-11-15
- **Amends:** [ADR-0012](0012-known-people.md), its tripwire ("matching of faces nobody
  enrolled … is identification, which this project does not build"), principle 4 of
  [principles.md](../../project/principles.md#4-behavior-first-identity-only-by-consent)
  ("no stranger identification") and the FAQ's "suppression-only" guardrail, for one
  opt-in feature described below. Every other guardrail of ADR-0012 stands: nobody is
  ever named from any source, nothing biometric leaves the machine, uncertain matches of
  known people never quiet an alert.

## Context

- The judge sees one clip at a time. A person who walks past the house once and a person
  who walks past four times in an afternoon, stops at the gate and later shows up in the
  yard look the same to it, clip by clip. The second is what a household wants to hear
  about, and nothing inside one clip says so.
- Known people (ADR-0012) already finds faces, computes their embeddings and decides that
  a readable face matches nobody enrolled. It then throws the embedding away, so nothing
  can tell that today's unknown face is yesterday's.
- What the engine can tell apart, measured on real clips (ADR-0012): the same
  person scored 0.56 to 0.66 against their own references with faces of 90 px
  and more, two different people no more than 0.29, and faces of 15 to 25 px at
  the far end of a picture are unreadable. Backfilling that deployment's clips
  (see [Measured](#measured)) adds what the cameras actually deliver: most readable faces
  are small, and few clips have one at all.
- The faces are those of passers-by, couriers and neighbours, who never agreed to
  anything. Comparing face embeddings to tell people apart is processing of biometric
  data; in the EU that is a special category (GDPR Art. 9), and a camera that covers
  public space, even partly, is not a "purely personal or household activity" (CJEU,
  C‑212/13 *Ryneš*, 2014), so the household exemption does not apply to it. Other places
  have their own rules (Illinois' BIPA, for one). The project's earlier line, "no
  stranger identification", existed for two reasons: never name a stranger from any
  source, and never build a biometric database of people who did not consent. This
  feature crosses the second for a narrow purpose, and says so.

## Decision

1. **Opt-in and off by default:** `people.track_unknown: false`. It needs
   `people.enabled`, and `vidette validate` says plainly what it keeps, for how long and
   where. The repository's example config leaves it off.
2. **Anonymous.** An identity is a number ("stranger #12", never reused). No name is
   ever looked up, from any source. A household member may type a name in the web app;
   it stays local: it goes to a local judge only, and to Telegram only with
   `notifications.telegram.names: true`, like the names of known people.
3. **What is kept, where:** `people.identities.dir` (default `~/vidette-data/identities`,
   refused inside a git work tree, folder 0700, files 0600): one SQLite file with the
   identities, their sightings (time, camera, clip path, face size) and up to three
   embeddings per sighting, and one aligned 112×112 face crop per sighting. It is a
   separate file from the event database ([ADR-0008](0008-database.md)) on purpose:
   biometric data stays in one folder that can be excluded from backups and wiped by
   deleting it. SQLite runs with `secure_delete`, and the write-ahead log is truncated
   after every delete.
4. **Matching is conservative**, because two strangers merged into one identity invent a
   recurrence that never happened, while one person split in two only undercounts and is
   fixed with one tap:
   - only faces the engine already calls readable (detector score, frontal enough,
     embedding quality, ADR-0012), and of those only faces at least
     `identities.min_face_px` tall (64; the engine's own floor is 40);
   - known people first: an unknown face within `near_known` (0.30) of someone enrolled
     is left alone, it is probably that person on a bad frame;
   - a sighting joins an identity at `match` (0.50) or above, and only when no other
     identity is within `margin` (0.06); close to two, it joins neither and creates
     nothing; below 0.50 it starts a new identity;
   - the web app merges two identities and splits sightings off one.
5. **Retention.** An identity nobody named or flagged is deleted, with every embedding,
   crop and sighting it holds, `retention_days` (30) after it was last seen. Named,
   suspicious, ignored and promoted identities stay until someone deletes them.
   Deleting an identity removes everything it holds; the clips are not touched.
6. **How it reaches a decision**, two ways, neither of them a verdict by itself:
   - **facts for the judge**: one more sentence in the face check's line, e.g. "unknown
     face #12: this is the 3rd time today, 5 times in total on 2 days; cameras in the
     last hour: street, front_door", followed by "Someone who keeps coming back, or looks
     at one place and then another, deserves more attention than a single pass; that
     alone does not prove intent". A cloud judge gets the counts and camera names; never
     a crop, an embedding or a name;
   - **a policy rule**: `recurrence:` on a policy with levels raises a verdict that named
     a scenario of a lower level to `raise_to` when the same identity was seen
     `sightings` (3) times within `within` (24 h), or on `cameras` (2) cameras within
     `cameras_within` (10 min). An identity marked suspicious raises on any sighting;
     ignored or promoted ones never do. `other`, an abstention and a verdict already at
     or above `raise_to` stay as the judge said, and nothing is ever lowered.
7. **Behaviour only.** No appearance attribute exists anywhere: no age, gender, skin
   colour, ethnicity or clothing is computed, stored, sent or used in matching, in the
   judge's facts or in a rule. The only inputs are the face embedding, time and camera.
8. **The household corrects it**: the Identities view (under People in the web app) lists
   every identity with its crop, count, days, last time and cameras, and per identity
   every sighting with its clip; rename, mark suspicious or ignored, merge, split, delete,
   or promote to a known person. "Это свой" on a Telegram alert, and "This is …" on an
   event card, promote the identity: its best faces become that person's references.
   Telegram alerts show the number and the count («незнакомец №12, 3-й раз за сегодня»);
   identity crops are never sent anywhere.
9. **Backfill** (`vidette people identities backfill`) runs the face check over the
   configured clip folders' existing clips, oldest first, so the view starts with the
   history the household already has. It requires `track_unknown` and writes only to
   the identities folder.

## Measured

On real clip folders, with the engine settings of ADR-0012 (2 frames a
second, at most 24 per clip, full resolution):

- **What the cameras deliver.** 590 clips gave 490 detected faces, of which 59 (12 %)
  were readable; the rest were too small (210), uncertain (181), of low quality (25) or
  turned away (15). Readable faces had a median height of 77 px, and 23 of the 59 were
  under 60 px.
- **The same person, two clips** (an enrolled person, the best pair among up to three
  faces of each clip, faces of 64 px and more, which is what matching an identity does):
  median 0.59, 13 of 15 pairs at 0.50 or above, the lowest 0.37.
- **Two different people:** never above 0.23 over 81 pairs of faces of 40 px and more,
  0.17 at 64 px and more (and 0.29 in ADR-0012's measurement).
- **Unknown faces against the enrolled references:** at most 0.38, median 0.28; the faces
  between 0.30 and 0.38 are left alone (`near_known`) as probably someone enrolled.
- So `match` 0.50 sits between the highest score two different people reached (0.23 to
  0.29) and the median of one person (0.59): it gives up some same-person joins (2 of the 15
  pairs fell below it) for a wide gap on the side that invents visits.
- **The backfill** over all 912 clips of the same folders (the 585 that the measurement
  above had already shown to hold no candidate face were not checked twice) found 7
  groups of unknown faces in total: 2 started an identity, 3 were within 0.33 to 0.36 of
  someone enrolled and were left alone, 2 were under 64 px. On these cameras the feature
  mostly waits: a stranger's face big and clear enough to count is rare, so it will
  undercount long before it invents anything.


These are observations on one test deployment, not a measured error rate: there is no
labelled identity set yet.

## Consequences

- ✅ A household can say "a stranger who keeps coming back is worth waking for", and the
  judge hears how often a face has been seen instead of judging every clip alone.
- ✅ Local only, no new dependency, no new process; off unless someone turns it on.
- ⚠️ **Biometric data of people who never agreed sits on the machine** for up to
  `retention_days`, longer for identities someone named or flagged. Whether running this
  is lawful where the cameras are is the operator's question; the docs say so plainly and
  the feature is off by default.
- ⚠️ No accuracy figure. Thresholds come from one deployment's measurements; small faces
  (the common case on security cameras) mostly start a new identity each time, so
  recurrence is undercounted rather than invented.
- ⚠️ The rule raises `level` and `alert` on the stored verdict; the scenario label stays
  the judge's, and that mismatch is how the bot and the web app recognize a raised
  verdict. `model_said_alert` records the raised value. `vidette eval` does not apply
  the rule: it measures judges.
- ⚠️ Compute: matching is plain Python over at most 40 references per identity, about
  20 to 35 ms per sighting (database write included) at a few hundred identities on a
  busy Apple-silicon Mac; it grows linearly with identities, which retention keeps few.
  Storage is about 5 KB of crop and 1.5 KB of embeddings per sighting.
- **Tripwire:** if identities ever need to leave the machine (sync, a cloud judge seeing
  crops, sharing between households), a name from any source other than a household
  member typing it, an appearance attribute of any kind, or a retention longer than the
  household chose, stop: that is identification and profiling, which this project does
  not build.

## Alternatives considered

- **Keep the embeddings of unknown faces in each event's report:** spreads biometric data
  across clip sidecars and the event database, with no retention and no single place to
  wipe.
- **Offline clustering (e.g. HDBSCAN) once a night:** better clusters, but no recurrence
  at the moment an alert is decided. A candidate later, for suggesting merges.
- **Identities in the event database:** one database, as ADR-0008 prefers, but faces of
  strangers would then be in every backup of the events and could not be removed by
  deleting one folder.
- **Frigate's face library:** keeps unknown faces in its own training folder; for
  cameras under Frigate, its sub-labels are still preferred for known people. Clip
  folders have no Frigate.
- **Ask the VLM whether two clips show the same person:** sends faces to the judge (and
  to a vendor, for a cloud judge), and is not measurable as a matcher.
