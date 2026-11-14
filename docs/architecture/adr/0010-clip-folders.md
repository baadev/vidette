# ADR-0010: Clip folders as an incident source, and a dataset collector for wake-on-motion cameras

- **Status:** accepted
- **Date:** 2026-11-14
- **Amends:** [ADR-0009](0009-intent-layer-over-frigate.md), decision 1 ("Vidette opens no
  camera connection, records nothing") with two bounded exceptions described below.
  Everything else in ADR-0009 stands: Frigate stays the primary source, there is no
  recorder, live view, detector or retention on this line.

## Context

- ADR-0009 made Frigate the only incident source and named the cost: "users without
  Frigate have no path until another source adapter exists", and "a battery camera does
  not fit continuous RTSP in Frigate either".
- Battery cameras (for example Eufy) sleep, wake on their own PIR
  motion sensor, serve RTSP only while awake, serve one client at a time and hold dead
  sessions for minutes (v0.1 field notes, [cameras/eufy.md](../../cameras/eufy.md)).
- The v0.2 plan needs 30 to 50 labelled real clips. Exporting them one by one from the
  camera's app does not scale; clips should be collected and drafted by machine and only
  checked by a person.
- Many cameras outside Frigate already produce clips: battery cameras through their own
  motion trigger, IP cameras that upload motion clips to FTP or SMB, phone exports. A
  clip is a complete incident: the camera already decided that something happened.

## Decision

1. **A clip folder is an incident source.** `clip_folders:` in the config lists folders;
   each settled video file in one becomes an incident (`incident.opened`, then
   `incident.closed`, source `clip-folder`) with its clip and key frames attached as local
   media. The intake, the events API, the judges and `vidette eval` are unchanged, which
   is what the source-agnostic incident model (ADR-0009) was for. Vidette only reads the
   folder: it never moves, deletes or writes files there.
2. **`vidette collect` is a dataset tool for wake-on-motion cameras.** A separate
   command, never started by `vidette serve`: it waits for the camera's RTSP stream to
   appear (a bare TCP port check while it sleeps), copies each wake period into a clip
   folder (`clips/<YYYYMMDD-HHMMSS>.mp4`, key frames, a JSON sidecar), and stops at hard
   disk limits (default 5 GB for the folder, nothing below 10 GB free). It records only
   what the camera's own motion trigger decided to stream.
3. Together they give a Frigate-free pipeline for a battery camera: collector → folder →
   `vidette serve` (clip folder source) → events → verdicts. The same folder is also the
   `vidette eval` dataset, labelled with `vidette label`.

### Why this does not reopen the NVR

The collector keeps none of what ADR-0009 froze: no continuous recording, no segment
index, no retention classes, no live view, no restream, no detector, no keep-warm
holders. It is one ffmpeg stream copy per wake period plus a disk guard. The camera
handling it needs (single client, clean TEARDOWN, backoff) is a few hundred lines in
`vidette/collect/`, not a subsystem.

## Consequences

- ✅ People without Frigate (battery cameras included) can run the
  whole intent layer on clips their cameras already make.
- ✅ The real clip set for `vidette eval` can be gathered unattended; the same files feed
  labelling, evaluation and the live source.
- ⚠️ Clip-folder incidents arrive after the clip ends, so a verdict is at best a clip's
  length late. Fine for "tell me about the courier", too slow for "the door is being
  forced right now". Frigate remains the answer for live latency.
- ⚠️ A clip carries no object labels, zones or tracker data: judges see frames only, and
  Frigate-based baselines abstain on these incidents.
- ⚠️ The collector does open a camera connection. It is bounded (a separate command,
  wake periods only, hard disk caps) and documented in [collect.md](../../collect.md).
- **Tripwire:** if the collector starts to need retention, a timeline, continuous
  recording or a restream for a second consumer, stop: that is the NVR again, and the
  answer is Frigate (or go2rtc in front of the camera), not more code here.

## Alternatives considered

- **Export clips from the Eufy app by hand**: documented in [eval.md](../../eval.md) and
  still valid, but dozens of manual exports a week is the reason labelling never started.
- **Collector only, no clip-folder source**: the collector's output would be usable by
  `vidette eval` but not by the live service; the source costs about 200 lines and makes
  the folder a real input.
- **A Eufy cloud/P2P bridge**: not viable since Anker's backend migration
  ([cameras/eufy.md](../../cameras/eufy.md#why-there-is-no-bridge)).
- **Run the battery camera under Frigate**: Frigate expects a stream that is there; a
  camera that is gone 99 % of the time produces reconnect noise and the same battery drain
  v0.1 fixed.
