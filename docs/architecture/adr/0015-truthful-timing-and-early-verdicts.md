# ADR-0015: The camera's clock for clips, frames by packet, and an early verdict per recording

- **Status:** accepted
- **Date:** 2026-11-15
- **Amends:** [ADR-0010](0010-clip-folders.md) (a clip folder may hold early windows of
  recordings still in progress, and one event can be judged twice) and
  [ADR-0013](0013-telegram-bot.md) (an alert can be edited after it went out, and an event
  can buzz a second time when its level goes up). Everything else in those ADRs stands.

## Context

- A false alert happened: a person walked past a camera while its stream
  stalled for 13 s. The collector stamped packets on arrival (needed so far behind a
  restreamer that splices a camera's reconnect into the session with the camera's clock),
  so the stall became a frozen picture in the clip. The judge got four frames spread
  evenly over the two-minute clip, one of them on the stall, and "the event lasted 113 s",
  and answered that the person lingered.
- Measured through the restreamer on one camera, 25 s of the same packets: the camera's
  RTP timestamps were 61 to 73 ms apart; arrival times came in bursts with pauses up to
  0.7 s. Clips recorded on arrival time had pauses over 0.5 s covering a median 18 to
  62 % of each clip, depending on the camera; with the camera's clock, the same cameras'
  clips had none.
- The alert arrived about four minutes after the person appeared: the stream itself
  lagged, a battery camera's clip ended only when the camera slept again, and a shared
  judge had a queue. Alerts should arrive within 20 to 30 seconds once the judge has a
  model of its own, and it should be visible where the time goes.

## Decision

1. **Clips keep the camera's clock.** The collector reads the camera's RTP timestamps and
   repairs a restreamer's splice on the input with a `setts` jump guard: a packet more
   than 10 s after the previous one, or not after it, continues one frame later. The clip
   length limit is the collector's own, from ffmpeg's progress. Arrival stamps stay as an
   option, not recommended.
2. **Every clip carries its stream health** (pauses, bursts, the longest pauses), measured
   from packet times without decoding, in its sidecar and in the judged event.
3. **The judge sees real frames, picked by packet, where the picture changed**, two per
   episode of change, and is told each frame's time and every stall between two frames.
   How long someone stayed is judged only from frames without a stall between them.
   Frames are chosen once per event and become the alert's album.
4. **One early verdict per recording.** The collector copies the first seconds of a
   wake-period recording, or of a continuous mode's motion event (with its pre-roll, from
   the open ring segment), to `.early/` while it records, under the id the clip will have.
   The clip folder publishes it as a partial incident of the same event; the worker
   judges it at once and an alert goes out. The whole clip is judged when it closes, also
   after an early alert, and its verdict replaces the early one; the bot edits the same
   message, and sends one more message only when the level went up. An early "nothing"
   never hides a later alert.
5. **Every event keeps when each stage happened**, and the bot shows it for the last alert.

## Consequences

- The clips' timeline is the camera's. A stall in delivery no longer shows; a camera that
  really drops frames still does (its clock has the gap), and a splice loses the time the
  camera was away (at most the silence limit).
- An event can be judged twice and an alert can change after it went out. The early
  verdict is less informed by construction; the edit says when the whole clip disagrees.
  Two judge calls per wake period with early windows on.
- Prompts are longer (frame times, stalls): on a shared 2048-token slot the margin for
  the answer is small, so a refusal for context size is retried once with smaller frames.
- Tripwires: if early alerts are wrong often enough that the edits annoy, raise the
  window or require the early verdict to be more confident; if a camera's own clock jumps
  (not a splice), revisit decision 1 for that camera.

## Alternatives considered

- **Keep arrival stamps, detect freezes by comparing frames.** Decoding every clip to find
  duplicate pictures costs CPU on every clip and still hides the real timing.
- **Read the camera's on-screen clock (OCR).** Fragile per camera and font; kept as a way
  to measure stream lag by hand, not in the pipeline.
- **A separate event for the early window.** Two events per recording, two alerts, and the
  digest and the labels would see duplicates.
- **Judge the stream continuously.** A live decoder per camera is the NVR this project
  stopped being (ADR-0009).
