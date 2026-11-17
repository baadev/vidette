# ADR-0022: Incidents across cameras: nothing woken is dropped, long clips are judged around the person, an alert names the other cameras

- **Status:** accepted, amends 0011 (what the continuous collector keeps), 0013 (how an
  alert is sent), 0014 and 0017 (an opt-in clothing cue for one narrow use), 0021 (the
  pre-filter during an incident)
- **Date:** 2026-11-17

## Context

A staged test on a test deployment (one person, hood up, face hidden, two cameras within
two minutes) exposed five failures that were each reasonable on their own:

1. **One camera's 81 s clip was judged ordinary.** Four frames chosen where the picture
   changed (5, 16, 47, 64 s) spent themselves on headlights and an animal; the person
   small and far away at 36 to 39 s, and at the frame's edge at 72 to 74 s (then a
   stall), was never shown.
2. **The alert on the second camera said nothing about the first.** Links across cameras existed
   only on demand, only by face, and the face was hidden.
3. **The alert reached the first recipient 430 s after the verdict, the second 480 s after
   that.** Each recipient got an album (frames and the clip in one upload) before the text,
   one after the other, under one lock; a slow uplink held every text behind it.
4. **A reply in words got the model's echo of a tool result** ("[tool_result] {…}") as the
   answer: any text without a tool call was taken as the final answer.
5. **The person's next minutes were recorded by no camera.** A battery camera woke five
   times in those minutes; the continuous collector kept nothing of a woken
   stream its motion gate saw nothing in, and its reconnect wait grew to 20 s during the
   incident. On that camera 130 to 220 woken streams a day had been dropped like this.

Some of this is the cameras' doing (battery cameras behind a base station sleep, stream a
few seconds, deliver late and with stalls); the rest is ours. New cameras that stream all
the time are coming; what is the cameras' fault is re-checked on them, not engineered
around.

## Decision

- **A woken stream is evidence.** Behind a base station a camera streams only when its own
  sensor fired: a woken stream (up to `--max`) the motion gate cut nothing out of is kept
  whole as a clip (trigger `wake`), short wakes down to `--wake-min` (0.5 s). After a
  woken stream ends the next look comes after `--base-reconnect` (1 s), never a growing
  backoff. Ring segments younger than `--ring-min-age` (30 min) are not deleted for
  `--ring-size` (the free-space floor still wins).
- **Incident mode.** When an alert goes out (not one a calibration step sent to the
  digest), `vidette serve` writes `incident-mode.json` next to its database, valid for
  `incident.keep_for` (10 min). Every collector reads it (found through `--serve-config`):
  everything woken is kept, nothing is too short, a continuous stream's stretches the gate
  did not cut are kept in 30 s pieces. The pre-filter skips nothing from those minutes
  (they are judged right after the clips with people), and the judge gets one neutral
  line about the alert. A missing or unreadable file means no incident: it can only make
  the system keep more.
- **Long clips are judged in windows around the person.** For a clip longer than
  `understanding.windows.long_clip` (30 s) in which the pre-filter saw a person, the
  detector runs on a frame a second; the track is cut into windows of at most 20 s that
  end at a sighting; the window with the last sighting is judged first, then the frames
  the clip would have been judged on anyway (the detector misses people half out of the
  picture: nothing those frames show is lost), then the windows where the person is small
  or at the edge, then where they stayed longest, at most 3 (a clip stops after three
  calls while other clips with people wait). A small person or one at the edge is enlarged (frame 1 whole, the
  others cropped, in the frame's shape). A window whose verdict wakes for anyone decides;
  otherwise the highest alerting level wins; when nothing alerts the usual frames' verdict
  stands. Everything is kept on the event (`intent.windows`).
- **An alert names the other cameras.** Events of other cameras with a person within
  `incident.link_within` (3 min) are linked: **face** (the same enrolled person or
  anonymous identity), else **look** (opt-in, below), else **time** only. The alert gets
  one line per link and the same person's best frame and clip as replies; the links are
  stored on both events (`intent.incident`), shown in the web app and the digest; a
  linked event the judge had dismissed is marked `reopened` when the alert wakes for
  anyone.
- **The clothing cue, opt-in and narrow (`incident.look`, off by default).** The colours of
  the torso and legs (box rows 30 to 95 %, the middle 70 %, never the head) on a few frames
  of each clip, compared only between an alert and the other cameras' events within
  minutes, only on cameras declared neighbours (or all, when none are declared). Never
  stored (only the similarity goes into the link), never compared with anyone else's,
  never a reason to alert, raise a level or suspect anyone; it only lets the alert say
  "probably the same person". This is an appearance attribute, which ADR-0014 and the
  tripwire of ADR-0017 rule out; the exception is deliberate, limited to this use, and
  stays off unless a deployment turns it on.
- **The text goes first.** An alert's text with its buttons goes to every recipient at
  once; the best frame, then the other frames with the clip, follow as replies, off the
  alert path. One log line per alert and recipient says how long each step took.
- **The assistant never sends a tool transcript.** Copied tool calls and results, special
  tokens and bare JSON are stripped; a tool call written as text is run; an answer with
  nothing left is asked for once more, then said plainly from the facts. "It was me, a
  test" marks the alert right and as a test, never a false alarm for calibration.

## Consequences

- Clips with nothing in them are kept and judged more often: about 140 to 220 more short
  clips a day on a battery camera (about 10 s each, a few hundred MB a day, inside
  the clip cap and its pruning), most skipped by the pre-filter.
- A long clip with a person costs a detector track (about 7 s of CPU for 80 s) and up to
  four model calls instead of one (three while others wait; one when the first window
  decides): on a test deployment 55 to 70 such clips a day, up to about 150 to 220 more
  calls.
- During an incident nothing is skipped: the judge's queue is longer for those minutes.
- ⚠️ The clothing cue is weak: on the test deployment the staged person scored 0.91
  between two cameras, other people that evening up to 0.89. It never says more than
  "probably", and never acts on its own.
- ⚠️ Battery cameras still sleep, stream late and stall; their clips' times are the
  collector's, not the camera's. That is re-checked on cameras that stream all the time.
- **Tripwires:** if the clothing cue is ever wanted for anything but the line next to an
  alert (suspicion, a level, recurrence, storage), stop. If windows make long clips the
  judge's main cost, lower `max_windows` or raise `long_clip` before adding more frames
  per call. If incident mode keeps far more than an hour a day, it fires on alerts that
  are not real: fix those first.

## Alternatives considered

- More frames per call for long clips: the shared server's slot holds about 2000 tokens,
  four frames already take most of them.
- Linking only by face: correct but silent exactly when a face is hidden on purpose.
- Re-judging a linked event that was dismissed: one more model call per link, at the
  moment the slot is busiest; marking it as part of the incident shows the same.
- Lowering the motion gate's thresholds instead of keeping woken streams: the gate is
  right for cameras that stream all the time; a wake is a different, stronger signal.
