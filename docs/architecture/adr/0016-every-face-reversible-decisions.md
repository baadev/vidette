# ADR-0016: Every face gets an identity, its sharpest moment, and decisions that can be taken back

- **Status:** accepted
- **Date:** 2026-11-15
- **Amends:** [ADR-0014](0014-anonymous-identities.md): decision 4 (only faces of 64 px
  and more, and none close to someone enrolled, became identities), decision 5's
  "ignored" status, and decision 8 (one-way answers). Its guardrails stand: anonymous,
  local only, opt-in, retention, no appearance attribute, conservative automatic matching.

## Context

The first day with anonymous identities on a test deployment showed three problems:

- **An answer could not be changed.** "A stranger" on an event card was final, and it
  did nothing useful either: the face stayed unknown and was not tracked.
- **Most people never got an identity.** Only readable faces of 64 px and more counted,
  and on security cameras most faces are smaller or blurred in the frames the check
  happened to sample. A familiar face seen at 43 px could not be marked at all, so
  nobody could say "this one is fine, do not wake me for them".
- **The previews were blurry**, while the clip had frames where the same face was
  sharp: the check looked at 2 frames a second and kept the largest face, not the best
  picture of it.

## Decision

1. **Every person whose face the check finds gets an identity**, unless they are
   enrolled. Readable faces of any readable size (40 px, the engine's own floor) join
   the identity they clearly match (`match` 0.50, `margin` 0.06, unchanged) or start
   one. Faces too small or blurred to compare are followed from frame to frame by where
   they are (a face near the same place a moment later is the same person; at least two
   frames) and start a **weak** identity: it keeps its crop and its embeddings as hints
   for suggesting merges, and nothing ever joins it automatically. A person with no face
   found gets nothing, and the event card and the bot say so («лицо не найдено»).
2. **Duplicates are expected and cheap to fix**: the Identities view lists pairs that
   may be one person (best face similarity from 0.35, weak ones marked as such) with a
   one-tap merge, merges a selection in bulk, and moves one sighting to another identity
   or a known person. Automatic matching stays as strict as before, so two strangers are
   not merged by the system; small faces only make more singletons.
3. **Close to someone enrolled is a hint, not a gate.** A face within `near_known`
   (0.30) of an enrolled person now gets an identity too, marked as probably that person:
   such an identity never raises a level by recurrence, the judge hears "possibly a
   household member", and the event card offers that person first.
4. **The sharpest moment.** Wherever the first pass found a face, the clip is looked at
   again around it (±0.6 s, 6 frames a second, at most 24 more frames). Every face gets
   a score, detector confidence × sharpness (variance of the Laplacian of the aligned
   crop, full at 150) × size (full at 112 px) × frontalness (full at an eye distance of
   0.45 of the face width), readable faces first; the best one is the crop the report
   keeps, the crop and the embeddings an identity keeps, and the preview. The report
   says when in the clip that moment is (`best_at_s`), for the judge's frame selection
   to use later.
5. **Every decision about a face is reversible.** A decision ("this is Sam", "a
   stranger", "not sure", "identity #12", "a new identity named … with a disposition")
   records each change it made on the face: a reference added to Sam, a raised
   threshold, a promotion, a sighting added or moved. Taking it back reverses exactly
   those, newest first; a new answer takes the previous one back first. Answers given
   before this change are taken back from what they always wrote (the reference's source
   and the rejection's place). "A stranger" now keeps the face as an identity. A face
   of any size can be put in an identity by hand; one under 64 px says it may not be
   recognized next time.
6. **Familiar** replaces "ignored": a person the household names and marks «знакомый, не
   тревожить» (a neighbour, the postman). For routing, familiar people count like the
   household: an event where everyone recognized is household or familiar goes to the
   digest at an `unless_known` level, a `known_people: quiet` policy tells the judge they
   need no alert, and they never raise a level by recurrence; `always` levels (the
   critical list) still wake, exactly as for the household. A familiar identity is kept
   until deleted.
7. **Telegram**: «Кто это?» under an alert opens a menu per face: Свой (enrolled people,
   with `names: true`), Знакомый (named familiar identities), Новый знакомый…, Новая
   личность…, Чужой, Отменить решение. A name is given by replying to the bot's question
   (a forced reply, only in an allowed chat). No identity crop is sent anywhere; the
   frames and the clip already go to the household's own chat.

## Measured

Ten clips of a test deployment with faces in them, the first pass alone ("before", the
largest readable face kept) against the first pass plus the second look ("after", the
best-scored face kept):

| | before | after |
|---|---|---|
| readable face detections | 40 | 142 |
| median sharpness of the kept crop | 193 | 297 |
| clips whose kept crop got sharper | | 8 of 10 |
| frames looked at | 237 | 469 (232 more, about 23 a clip with a face) |

The second look costs about as much again as the first pass, only for clips with a
face: on an Apple-silicon Mac about 1 to 2 s more before the judge. Where the kept crop
got less sharp (2 of 10), a more frontal face of the same size won; both were sharper
than the score's full mark.

## Consequences

- ✅ Every face the household can see on a card can be named, marked familiar, merged or
  taken back, and a person marked familiar stops waking anyone (outside the critical
  list) when their face is found again.
- ✅ Better crops, better embeddings, more readable faces from the same clips.
- ⚠️ **Many more identities**, mostly singletons from small faces, and more biometric
  data of passers-by on the machine (still pruned after 30 days unless named or
  flagged). The list's suggestions and bulk merge are the answer; the retention promise
  is unchanged.
- ⚠️ A small face matches its own identity less often next time (in ADR-0014's
  measurement, 72 % of same-person pairs reached 0.50 with faces from 40 px, 87 % from
  64 px), so marking someone seen only from afar helps less until a clearer face of
  them is added.
- ⚠️ **Familiar quiets, so a wrong join quiets too.** A stranger matched to a familiar
  identity at 0.50 or more would route like the household at an `unless_known` level.
  The join threshold is stricter than known people's (0.40), and the critical list
  always wakes; that is the bet.
- ⚠️ Latency: the second look adds 1 to 2 s to a verdict for clips with a face.
- **Tripwire:** unchanged from ADR-0014; and if familiar people ever need to quiet the
  critical list, stop: that is a policy change for the household to make in its levels,
  not a property of a face.

## Alternatives considered

- **Every detected face, no tracking:** one person walking through a clip would make a
  dozen weak identities.
- **Lower the automatic join threshold for small faces:** more joins, and joins of
  different people, which invent visits and, for familiar identities, quiet alerts.
- **Decode every frame of the clip:** five to ten times the cost for little more than
  the second look around faces gives.
- **Ask the judge which frame shows the face best:** sends faces to the judge, and the
  sharpness of a crop is cheaper to measure than to ask about.
