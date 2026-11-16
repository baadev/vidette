# ADR-0018: A merge pass joins identities that are certainly one person, audited and reversible

- **Status:** accepted, amends 0014 and 0016
- **Date:** 2026-11-16

## Context

Live matching (ADR-0014, ADR-0016) decides once, when a face arrives, against the
identities that exist at that moment. On a test store of 572 identities after two days,
565 had one sighting each: "the same stranger again" never happened. The reasons only
show later:

- the same pixels arrive twice: an early window and the whole clip, or two overlapping
  clips of a camera that streams all the time (identical crops, cosine 1.0);
- most faces on street cameras are too small or blurred to compare (502 of the 572
  identities had no clear face at all) and start weak identities that nothing joins;
- an identity can get its first clear face after the stranger's first pass.

Two strangers in one identity invent a visit that never happened and, through the
recurrence rule, an alert; one person split in two only undercounts. So a merge must be
conservative, and every merge must be undoable.

Measured on that store (SFace cosine, best pair of faces between two identities):

| Pairs | n | at or above 0.5 | 0.7 | 0.85 | max |
|---|---|---|---|---|---|
| different people in one frame (cannot be one person) | 39 | 3 | 2 | 0 | 0.74 |
| of those, both faces unclear | 26 | 1 | 0 | 0 | 0.55 |
| enrolled people's reference photos, person A vs B | 840 | 0 | 0 | 0 | 0.33 |
| clear faces of two identities, every pair | 2145 | 4 | 0 | 0 | 0.53 |
| one camera, within 10 min, any faces (not one clip) | 1250 | 52 | 10 | 8 | 1.0 |
| more than an hour apart or another camera, any faces | 154779 | 2990 | 36 | 2 | 0.88 |

Unclear faces of different people score as high as one person's (a blur looks like a
blur), and a static face-like pattern scored 0.88 against itself seven hours later. Near
in time, on one camera, the scores split into a cluster at 0.85 to 1.0 and the rest at
0.74 or below, which is also the highest score of two people seen at once.

## Decision

We add a merge pass (`vidette.people.merge`) with two rules:

1. **Same moment**: two identities with faces at least 0.85 alike, on one camera, within
   10 minutes. Unclear faces count here and only here.
2. **Clear faces**: identities with clear faces whose best pair scores 0.5 (the live join
   threshold) or more, no pair of their clear faces below 0.4, and each the other's
   closest candidate by a margin of 0.06.

Groups grow by complete linkage (every member with every other), never chains. Never
merged: two identities seen in one clip; sightings a person kept apart (a split, an
undone merge, recorded in a new `apart` table); two named or marked identities; one close
to someone enrolled with one that is not; a known (promoted) identity. A group keeps the
number, name and status of its marked member, else of the one seen most; a named or
marked identity is never moved away.

Every merge is a row in a new `merges` table with the removed identity as it was and the
sightings it moved; `unmerge` and `undo_run` restore the number, the fields and exactly
those sightings, and mark them apart. An old number (in an alert, an event card, the
assistant) resolves to the identity it was merged into. `vidette people merge` shows the
plan and the score distribution (dry run by default), `--apply` copies the database
first; `vidette serve` runs the pass after new sightings, at most every 30 minutes
(`people.identities.merge`), copying the database before any pass that merges.

## Consequences

- On the test store the first pass merged 7 identities (572 → 565): five same-pixel
  duplicates and one person over three overlapping clips. Four clear pairs at 0.50 to
  0.53 were left alone, each having another candidate within the margin. The pass cannot
  make "the same stranger again" work for faces of 20 to 40 pixels; that needs more
  pixels on faces (camera placement, a narrower lens), not a looser rule.
- The thresholds rest on a few hundred pairs from one place; they are design values to
  revisit with a labelled identity set, not measured rates.
- Tripwire: an undone automatic merge (the audit shows `undone_at`) means a rule let
  through two people; tighten it before anything else.

## Alternatives considered

- **Single-linkage clustering at the SFace reference threshold (0.363):** chains blurred
  faces into a few huge identities; the same-clip pairs alone reached 0.455.
- **Merging small faces by time and place only:** two passers-by a minute apart are
  common on a street; it would invent recurrences.
- **Lowering the live `match`:** changes every future decision instead of fixing the past
  ones that can be shown to be duplicates, and is not reversible.
