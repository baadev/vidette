# ADR-0020: Calibration: 👍/👎 move a scenario on a camera a step quieter, never past a floor

- **Status:** accepted, amends 0013 (who is woken)
- **Date:** 2026-11-17

## Context

Since ADR-0013 every 👍/👎 under an alert becomes feedback on the event and a reviewed
label, and a 👎 asks why. Nothing else changes: the next alert of the same kind wakes the
same person the same way. On a test deployment most alerts of one scenario on one camera
were marked false, again and again, and the only remedy was rewording the policy by hand.

The marks are a running record of what the household wants to be woken for. Using them
has two risks: silencing something dangerous because of a few careless taps, and changing
behaviour nobody can see or undo. The policy file is the person's own words; a program
that edits it takes them away.

## Decision

- **A key is one scenario of one policy on one camera.** The same scenario can be noise
  on one camera and essential on another.
- **A ladder of steps, one at a time:** 0 as written; 1 a confidence bar (the alert
  wakes only when the judge is at least `bar`, 90 %, sure); 2 the digest only. A step
  changes routing only: the event stays confirmed, the evening digest lists it with "in
  the digest by your marks", and the verdict, the labels and `vidette eval` are untouched.
- **Quieter after `marks` (4) counted 👎 and no 👍** on the key, since its last change and
  within `window` (14 days). The reasons "The recording froze", "Wrong camera" and "One of
  us or familiar" do not count: the camera or the face check are at fault, and a stricter
  bar for a person at the house would hide strangers too. When most counted marks say
  "Right, but not important", the bar is skipped (the judge saw right; its confidence is
  not the problem).
- **Back at once:** a 👍 on an event a step kept out of the alerts takes that step back.
  Quieting needs four marks, waking again one.
- **A floor no mark can pass:** every scenario of a `wake: always` level, and those named
  in `calibration.protect`, gets at most a bar of `protected_bar` (75 %) and never the
  digest. The limit is applied again for every alert, so a later policy edit that protects
  a scenario caps an older step.
- **Stored, never written into the config:** a `calibration` table, one row per step with
  the evidence (counts, reasons, event ids); a step taken back keeps when and by whom
  (`telegram`, `web`, `cli`, `mark`). The steps are read from the database for every
  alert, so a step taken back anywhere counts for the next alert.
- **Said and undoable where the marks were given:** one silent line per step to every
  recipient in the alerts topic, in the bot's language, with an Undo button; `/tuning`
  lists what is in force; the System page has a Calibration card; `vidette policy
  calibration [undo ID]` on the command line.
- **The dry run replays it:** `vidette policy dry-run` routes stored verdicts of the last
  days through the policy (as written, or a proposed one) and calibration (off, the steps
  in force, or replayed from the marks in the order they were given), so the effect of a
  rule is visible before and after it acts.
- **On by default.** Every step is said and can be undone with one tap, and the floor
  holds; `calibration.enabled: false` keeps the ADR-0013 behaviour.

## Consequences

- Noise from one scenario on one camera fades after a few marks, without anyone touching
  the policy, and comes back with one 👍.
- The person decides what counts as noise; a household that never marks never sees a
  change. A careless series of 👎 can quiet a non-critical scenario on one camera; the
  bot says so the moment it happens, with the button that undoes it.
- What wakes someone is now the policy plus a short list of steps. Both are visible (the
  policy file, `/tuning`, the System page); the dry run shows their sum over real days.
- Webhooks, Apprise, web push and MQTT still see every confirmed event; the steps only
  change the Telegram routing, like `wake` rules do.
- Tripwire: if the steps pile up per camera (many keys quieted), the policy itself is
  wrong for that camera, and a per-camera policy is the better fix than more steps.

## Alternatives considered

- **Edit the policy text or its levels automatically:** takes the person's words away and
  turns a reversible preference into an irreversible rewrite.
- **A global per-policy threshold:** one noisy scenario on one camera would quiet every
  scenario everywhere, critical ones included.
- **Learn a classifier from the marks:** a few dozen marks a week is not training data,
  and its decisions could not be said in one line or undone with a tap.
- **Change the judge's prompt with the marks (few-shot examples):** costs tokens on a
  2048-token slot on every call, and its effect cannot be shown before it acts.
