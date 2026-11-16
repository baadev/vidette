# ADR-0019: Deep analysis: a second look at the day's clips, on one shared slot, inside serve

- **Status:** accepted, amends 0013 (the digest) and 0017
- **Date:** 2026-11-16

## Context

The live judge answers about four frames within minutes, because someone may be woken.
On a shared model server it does not keep up: on a busy day of a test deployment two
thirds of the clips were never judged, and its one-line descriptions are what the evening
digest quoted. The same server's owner reserved one of its parallel slots for Vidette;
the other slots belong to another workload that must never wait for us.

A second, slower look by the same big model can describe what happens over more frames
and judge from that description. It can run at night, when few clips arrive. To be
worth it, it must not delay a live verdict, must not take another tenant's slot, must
survive restarts without repeating or losing work, and its disagreements with the live
judge should become labelling work, so a person reviews only where the judges part.

## Decision

- **Look, then decide** (`vidette.deep.analyst`): one request with up to eight frames of
  the whole clip and no policy (the frames get the slot's room, the model describes before
  it judges: who is where, what their hands do, which vehicles move), then one text-only
  request with the policy, its levels, the clip's facts and the description, answered in
  the live judge's schema with a 2-to-4-sentence description in the owner's language. A
  look refused for context size is split in half.
- **Next to the live verdict, never over it**: `events.deep` (JSON) holds the deep verdicts,
  the looks, timings and the comparison: agree, disputed (the alert decision or the level
  differs, or no live verdict and deep says alert), uncertain (either side unsure or below
  `min_confidence`), only_deep.
- **Disputed and uncertain clips become review drafts** in the clip's dataset
  (`labels.yaml`, `status: draft`, the deep scenario proposed, both judges in `notes`), so
  the next contact sheets show only those. Reviewed entries are never touched.
- **A persistent queue** (`deep_jobs`, one row per event, in the server's database): the
  last 36 hours of events with people, faces, vehicles, alerts, or never judged, by
  priority (alerts first, never-judged last), newest first. Running jobs go back to the
  queue at start; a job fails after three attempts; at most `max_clips_per_day`.
- **One slot for everything** (`vidette.vlm.gate.SlotGate`): the live judge and the
  assistant take the process's slot in turn; deep analysis takes it only when it is free
  and nothing live waits, and a live call that arrives cancels the deep request (the job
  goes back, its finished looks kept). Before each request the worker reads the server's
  `GET /slots` and sends only when a slot is idle, backing off from 15 s to 5 min.
- **When**: back to back inside `window` (01:00-07:00 by default), yielding to live calls
  as above; outside it one clip every `trickle_every` (10 min) as a turn of its own (in
  line with live calls, not cancelled: on a busy day the live judge never rests and a
  yielding trickle would never run), or nothing (`outside_window: off`).
- **Inside `vidette serve`**, not a separate agent: only one process can see the live
  judge, the assistant and deep analysis together and let the live ones go first; the
  event database has one writer (ADR-0008); the digest that quotes the deep descriptions
  lives there. Every failure stays in the worker (retries, a rate-limited `deep.failed`
  system event).
- **The digest** uses the deep description when there is one, lists events the deep look
  flagged and the live judge did not (also from last night, after yesterday's digest),
  and says how many disputed clips went to labelling. It still goes once a day and only
  when something needs a look.

## Consequences

- At night live verdicts wait for nothing new: a deep request can be thrown away by a live
  one. By day a live verdict may wait for one deep request once per `trickle_every`; on a
  test deployment the live judge kept the slot busy all day, so without the turn deep
  analysis would only ever run at night.
- Deep calls are counted apart from the live judge's (`deep-local` in the usage ledger);
  the clip cap is the real limit.
- More frames than the live judge are only as useful as the slot's context allows: with
  2048 tokens per slot eight 640-pixel frames fit in one look only because the policy is
  left out of it.
- Whether the deep verdict is better than the live one is not measured; the disputes it
  sends to labelling are how it will be.
- Tripwire: preemptions that outnumber finished clips for a week mean the window is too
  busy; move it, or give deep analysis its own slot.

## Alternatives considered

- **A separate launchd/systemd agent:** cannot see the live judge's queue, so it would
  either hold the slot while a live verdict waits or need a lock protocol between
  processes; and it would be a second writer of the event database.
- **Replace the live verdict with the deep one:** the live verdict is what woke someone;
  rewriting it would make the history and the labels lie about what happened.
- **Send every clip to labelling:** what the owner asked to stop; only disagreements carry
  information a person must add.
