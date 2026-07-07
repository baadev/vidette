# Storage design

> **Status: 📐 designed** (M1 recorder, M3 compaction/off-site). The retention planner is
> implemented and tested today (`server/vidette/recording/retention.py`). Numbers are design
> targets unless marked measured.

Storage is one of Vidette's three budgets, and the one users feel first: it decides how many
days of history a disk holds, and whether footage survives the day something actually happens.

## Principles

1. **Never transcode on ingest.** The camera already encoded the video; re-encoding 24/7 is
   the single biggest compute waste in naive NVRs. We write **codec-copy** (H.264/H.265
   passthrough) fMP4 segments via FFmpeg.
2. **Recording is sacred.** The recorder has priority over every other subsystem
   ([shedding ladder](overview.md#data-flow-and-backpressure)); segments are finalized and
   fsynced on close; a crash loses at most the current segment.
3. **Not all footage is equal.** Retention is per *class*, not one global dial.
4. **Failures are loud.** Disk pressure, write errors and gaps become system events that hit
   the same notification channels as security events. A security system that silently stops
   recording is worse than none.

## Layout

```
/media/vidette/<camera>/YYYY/MM/DD/HH/<epoch>.mp4   # 10-second fMP4 segments, codec-copy
/media/vidette/<camera>/events/<event-id>/clip.mp4  # remuxed pre/post-roll — no re-encode
/media/vidette/<camera>/events/<event-id>/snapshot.webp
/media/vidette/<camera>/previews/YYYY/MM/DD/HH.mp4  # scrub strip: ~1 fps, tiny, powers the timeline
```

- **10 s segments** balance seek granularity, per-file overhead, and worst-case loss window.
- **Scrub strips** are what make the timeline *feel* instant: the UI scrubs a tiny 1 fps
  preview and only opens full-rate segments on click. This is a UX feature purchased with
  ~1–2 % storage overhead.
- **Event clips are remuxed**, not re-encoded: pre-roll + post-roll copied from segments in
  well under a second, cheap enough to attach to a webhook.

## The index (SQLite, WAL)

Single database at `/config/vidette.db` ([ADR-0008](adr/0008-database.md)):

| Table | Contents |
|---|---|
| `segments` | camera, start/end, path, size, codec, motion density, class flags |
| `events` | lifecycle, zones, tracks, T2 features, T3 verdict, media refs, feedback |
| `embeddings` | sqlite-vec vectors for event keyframes (semantic search, M3) |
| `events_fts` | FTS5 over T3 summaries ("someone touched the gate") |
| `system` | disk health probes, gap records, schema migrations |

Nightly `VACUUM INTO` snapshot next to the media store; the snapshot is part of the off-site
backup set. Media files are the source of truth for video; the DB is rebuildable from a media
scan (recovery tool, M1).

## Retention classes

```yaml
storage:
  retention:
    continuous: 3d     # everything
    motion:     14d    # segments overlapping motion
    events:     90d    # segments referenced by events + event clips
    favorites:  forever
```

Enforcement (implemented, tested, pure function — `plan_deletions()`):

1. Delete segments whose *highest* class has expired (an event segment is never deleted on the
   continuous schedule).
2. Under disk pressure (low-watermark breach), delete oldest **continuous** first, then oldest
   **motion**; **events within retention and favorites are never pressure-deleted** — instead
   Vidette raises a loud `storage.pressure` system event telling you the disk is too small for
   your settings, with math.

## Compaction *(M3)*

Cold continuous footage (older than `compaction.after`) is re-encoded to HEVC/AV1 at archive
quality during idle hours, using hardware encoders when present (QSV/NVENC/VideoToolbox).
Target ≥ 60 % size reduction on typical static scenes. Events and favorites are exempt by
default (evidence stays original). This turns "3 days of history" into "10+ days" on the same
disk for the cost of idle-time compute the box wasn't using anyway.

## Off-site *(M3)*

The unreliable-storage pain has two halves: disks die, and burglars take the box. The answer:

- **Event backup** to any S3-compatible target (B2/R2/MinIO/Garage): event clips + snapshots +
  nightly DB snapshot. Events are small (MBs) — this is affordable off-site insurance for
  exactly the footage that matters.
- Full continuous off-site is out of scope (bandwidth economics); NAS-mounted media dirs are
  supported for local redundancy.

## Sizing

Rule of thumb per camera, continuous recording, codec-copy (storage = camera bitrate, we add
~1–2 % overhead):

| Camera setting | Bitrate | Per day | 3 days | 14 days |
|---|---|---|---|---|
| 1080p H.264 "balanced" | ~2 Mbps | ~21 GB | ~64 GB | ~300 GB |
| 2K H.264 | ~4 Mbps | ~43 GB | ~130 GB | ~600 GB |
| 2K H.265 | ~2.5 Mbps | ~27 GB | ~81 GB | ~380 GB |

Four 2K H.265 cameras with the default policy (3d continuous / 14d motion / 90d events) fit
comfortably in 2 TB with headroom; the [getting-started guide](../getting-started.md) links
here so nobody buys the wrong disk. Motion-only recording typically cuts these numbers 3–10×
depending on scene traffic.

## Reliability checklist (what the system itself monitors)

- Free-space watermarks (warn at 15 %, act at 10 %, loud event at 5 %).
- Periodic write-probe + read-back on the media volume.
- Segment gap detection (expected vs. present timeline) → `storage.gap` events.
- SQLite integrity check weekly; WAL checkpoint monitoring.
- Documented UPS + filesystem guidance (ext4/XFS/ZFS notes) in the deployment docs.
