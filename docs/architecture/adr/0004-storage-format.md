# ADR-0004: Codec-copy fMP4 segments + SQLite index

- **Status:** accepted
- **Date:** 2026-07-07

## Context

Recording must satisfy all three budgets at once: near-zero compute (no transcode),
predictable storage, instant seek/export. It must survive crashes with bounded loss and
remain readable by standard tools if Vidette itself dies (user footage must never be held
hostage by our format).

## Decision

Record the camera's main stream **codec-copy** (H.264/H.265 passthrough) into **10-second
fMP4 segments** via FFmpeg, laid out `<camera>/YYYY/MM/DD/HH/`, indexed in SQLite. Event
clips and range exports are **remuxes** (stream copy), never re-encodes. Tiny 1 fps preview
strips are generated per hour for timeline scrubbing. Retention operates on segment classes
(continuous/motion/events/favorites) with pressure watermarks
([storage.md](../storage.md)).

## Consequences

- ✅ Recording CPU ≈ 0; storage = camera bitrate; export latency ≈ file copy.
- ✅ Segments are plain MP4: any player, any recovery scenario, no lock-in — the archive
  outlives the software.
- ✅ Crash loss bounded to one segment (≤ 10 s), fsync on finalize.
- ⚠️ Seek granularity is bounded by camera keyframe interval — documented camera-side setting
  (I-frame ≤ 2× fps).
- ⚠️ No inline encryption of segments (OS/disk-level encryption is the documented answer);
  revisit if demand shows (tripwire: repeated credible requests with a threat model that
  disk encryption doesn't cover).

## Alternatives considered

- **Continuous transcode to a house format** — burns the compute budget 24/7 for negative
  benefit; kills cheap-hardware viability.
- **One big file per day + index** — cheaper inodes, but crash recovery, retention deletion
  granularity and standard-tool readability all get worse.
- **MKV segments** — friendlier to exotic codecs, worse browser/remux ergonomics for clips;
  fMP4 wins on the export/webhook path.
