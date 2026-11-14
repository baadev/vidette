# ADR-0011: `vidette collect --continuous`: a bounded ring and a motion gate, still a dataset tool

- **Status:** accepted
- **Date:** 2026-11-14
- **Amends:** [ADR-0010](0010-clip-folders.md), decision 2 and its tripwire ("if the
  collector starts to need … continuous recording …, stop"). Everything else in ADR-0010
  and [ADR-0009](0009-intent-layer-over-frigate.md) stands.

## Context

- Some cameras sit behind a base station, and some are mounted sideways, so their picture
  has to be turned. Users want both continuous recording and clips.
- ADR-0010 made `vidette collect` a wake-period copier and named continuous recording as
  the tripwire back to an NVR. A camera that streams all the time has no wake periods: the
  only way to turn it into dataset clips is to look at the stream and decide when
  something happened, and the only way to give such a clip the seconds *before* the
  motion is to have kept them.
- A base station answers `404` for a camera that is not streaming, and a battery camera
  behind it streams only while it is awake. For such a camera the continuous mode sees short
  wake periods; it is built for cameras that stream all the time.
- The v0.2 plan still needs real labelled clips, reviewed on a phone, also where Frigate
  is not running.

## Decision

1. **`--continuous` is a second mode of the same dataset tool**, never started by
   `vidette serve`. One ffmpeg, one RTSP session, two outputs: a **ring** of stream-copied
   60 s segments and a **motion branch** (4 fps, 256×144 grey, frame differences).
2. **The ring is a lookback buffer, not a recording.** It is capped by size (default
   3 GB) and age (default 24 h), gives way to clips and to the free-space floor first, and
   has no index, no timeline, no playback, no export and no API. Its only consumer is the
   clip cutter (pre-roll), and the files are plain Matroska a person can open by hand.
3. **The motion gate is a frame difference with masks, an area band and a minimum
   duration**, not a detector: no models, no objects, no tracking, no zones. It decides
   which minutes become dataset clips; it never notifies and never produces an incident by
   itself. Clips land in the same folder format, so the existing clip-folder source,
   `vidette label` and `vidette eval` are the only path from them to verdicts.
4. **`--rotate` turns key frames and tags the MP4, never re-encodes.**

### The tripwire, moved

The line ADR-0010 drew stays where it matters. Stop, and use Frigate (or go2rtc in front
of the camera), if any of these is ever needed: browsing or searching the ring, a
timeline, retention classes, keeping the ring for its own sake (longer than the clips
need), a restream for a second consumer, object detection, live alerts from the motion
gate, or running inside `vidette serve`.

## Consequences

- ✅ A streaming camera yields dataset clips that start before the motion; the same flags work
  for any continuous RTSP camera.
- ✅ Disk is bounded three ways (floor, clip cap, ring cap); the ring is the first to give
  space back.
- ⚠️ Compute: the motion branch decodes the full stream. Measured on an Apple-silicon Mac
  with that camera (1920×1080 H.264, 15 fps): about 5 % of one core with
  VideoToolbox, about 20 % in software.
- ⚠️ Storage: at the measured 2.6 Mbit/s a continuous stream is about 1.2 GB an hour, so a
  3 GB ring holds about 2.5 hours.
- ⚠️ Privacy: a ring of a camera that sees public space is footage of people who did not choose to be in a
  dataset. It stays local, is overwritten within hours, and the docs repeat the rule that
  numbers are published, clips never are.
- ⚠️ The gate is deliberately permissive (light, leaves, rain will produce some clips); the
  VLM draft and the person labelling throw them away. Its thresholds are not quality
  claims and are not measured by `vidette eval`.

## Alternatives considered

- **Run Frigate for that camera now**: the right answer for live alerts where it runs, but Frigate's own motion and object pipeline would still have to be
  exported clip by clip for the dataset.
- **Record continuously and cut clips later by hand**: hours of footage nobody watches;
  the reason labelling never started.
- **Use the camera's own motion clips only** (the wake-period mode): works today (it is
  how the second camera behaves now), but gives no pre-roll and nothing for a camera that
  does stream continuously.
- **OpenCV or numpy for the gate**: native wheels and tens of MB for what ffmpeg's `tblend`
  plus `bytes.count` do in C already.
