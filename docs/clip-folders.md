# Clip folders: incidents without Frigate

A folder of finished clips can feed Vidette the way Frigate's review items do: each new
clip becomes one incident, then one event on the Events page, judged like any other
([ADR-0010](architecture/adr/0010-clip-folders.md)).

Good fits:

- a battery camera collected with [`vidette collect`](collect.md);
- an IP camera that uploads its motion clips to FTP or SMB;
- a folder you sync clips into from a phone.

Status: ✅ the source, local snapshots, clip playback in the Events page, and verdicts by
the VLM judge when a backend is selected ([vlm.md](vlm.md)).

## Configure

```yaml
clip_folders:
  - path: /clips/front-door   # inside the container: mount it (below)
    camera: front_door        # used when a clip has no vidette-collect sidecar
    poll_every: 10s           # how often the folder is scanned
    settle: 5s                # a file must be unchanged this long (uploads in progress)
    backfill: false           # true: clips already there on the first start count too
```

In compose, mount the folder read-only:

```yaml
services:
  vidette:
    volumes:
      - ~/vidette-data/front-door:/clips/front-door:ro
```

## What a clip becomes

| From | Incident field |
|---|---|
| `vidette collect` sidecar (`clips/<id>.json`) | camera, start, end, key frames |
| a stamp in the file name (`20261114-210304`, `2026-11-14_21-03-04`) | start |
| the file's modification time | end (and the start, without a stamp) |
| `frames/<clip name>/*.jpg` next to `clips/` | key frames, when there is no sidecar |
| the folder's `camera` | camera, when there is no sidecar |

The middle key frame becomes the event's snapshot (copied into `media_dir`); a clip
without key frames has no snapshot (the server image carries no ffmpeg to make one). The
clip is served from the folder in place. Files in hidden folders (`.partial`, `.vidette`) are
ignored, and Vidette never writes to the folder.

On the very first start only clips arriving after that moment count (unless `backfill`),
so pointing Vidette at a folder of 600 old clips does not create 600 events. Progress is
kept in the database: clips that arrive while Vidette is down are picked up when it
starts again.

## Limits

- An incident exists only once the clip is finished, so anything judged from a clip folder
  is at least a clip's length late. For "someone is at the door right now", use Frigate.
- Clips carry no object labels or zones; judges work from the frames alone, and the
  Frigate baselines (`frigate-severity`, `frigate-genai`) abstain on these incidents.
