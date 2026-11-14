# ADR-0009: Vidette becomes an intent layer over Frigate; the NVR core is frozen

- **Status:** accepted
- **Date:** 2026-11-14
- **Supersedes:** ADR-0002 (Vidette no longer manages a stream gateway), ADR-0004 (Vidette no
  longer records), ADR-0005 for camera adapters (notifier plugins are unaffected).
  **Amends** ADR-0003: tiers 0-2 are delegated to Frigate; tiers 3-4 stay Vidette's.
  ADR-0001, 0006, 0007 and 0008 stand; their go2rtc, live-view and sqlite-vec parts no
  longer apply to the code on this line.

## Context

A review of v0.1.3 (2026-11-14) counted where the effort went:

- **All shipped code re-implements an NVR.** Ingest via go2rtc, codec-copy recording,
  retention, live view, timeline, motion gate, YOLOX detector, IoU tracker, zones, ONVIF,
  MQTT/HA. Frigate (MIT, about 37k stars, weekly commits) already does every one of these,
  on more hardware, with years of field fixes.
- **The differentiator has zero code.** Plain-language policies and VLM verdicts about
  intent exist only as config schema: `policies[].description` is never read, and there is
  no VLM client.
- **Every field fix was RTSP session handling for one battery camera** (v0.1.1 to v0.1.3:
  reconnect loops draining a battery camera, single-client RTSP, keep-warm holders). None
  touched understanding.
- **No quality meter.** The reference clip set that gates the M2 "done" claim was never
  started, so there is no number for the one thing the project promises: fewer, better
  alerts.
- **The window is narrowing.** Frigate 0.17 added GenAI review summaries with a threat
  level (0/1/2) and a free-text `activity_context_prompt`; 0.18 added a chat with tools. What
  we did not find in Frigate's documentation (checked 2026-11-14, docs for 0.18): notifications
  gated by a user policy's verdict, a dry run of a policy over past events, calibration from
  thumbs up/down, or published false-alarm numbers on a labelled set.

Direction A was chosen on 2026-11-14 with two goals: be useful on real cameras, and build
a public open-source project with an audience.

## Decision

1. **Vidette runs beside Frigate, not instead of it.** It subscribes to Frigate's MQTT
   topics (`<prefix>/reviews` for incidents, `<prefix>/events` for tracked objects,
   `<prefix>/available` for liveness) and reads media through Frigate's HTTP API (event
   snapshots, recording frames, clips). Vidette opens no camera connection, records nothing,
   decodes nothing and runs no detector.
2. **Vidette owns the layer Frigate does not:** an incident (a Frigate review item) goes to
   a policy written in plain language, a budgeted VLM judge returns a structured verdict,
   and only a matching verdict produces a notification. The evaluation harness
   (`vidette eval`), the policy dry run over history and calibration from feedback are
   product features, not test tooling.
3. **Measure before claiming.** No accuracy or false-alarm claim appears in the README,
   docs or release notes without a `vidette eval` result on a named dataset.
4. **The NVR core is frozen at tag `nvr-core`** (= v0.1.3, commit a951765) and deleted on
   the `v0.2` line: `recording/`, `streams/` (go2rtc manager, keep-warm), `pipeline/` (decode,
   motion, detect, track, runner), `adapters/` (RTSP, ONVIF), the clip remuxer and the Tier-2
   event engine, their API routers and tests, and the Live, Review, Cameras and Setup web
   pages. Kept: `core/` (config, event bus), `db/`, `auth/`, `notify/`, the events API, the
   Events and System pages, CI.
5. **Frigate compatibility target:** reviews exist since Frigate 0.14; Vidette is built
   against the payloads documented for 0.18 and accepts the `genai` review message added in
   0.17. Parsing is lenient (unknown fields are ignored, missing optional fields default),
   because Frigate adds fields in minor releases. Details: [docs/frigate.md](../../frigate.md).

### Why delete on the branch instead of a `legacy/` directory

The tag already keeps the NVR byte-for-byte, with its own passing tests at that commit;
`git checkout nvr-core -- server/vidette/recording` restores any part. A `legacy/` copy would
add nothing the tag lacks and would cost a lot:

- it imports `vidette.core` and `vidette.db`, which change on this line, so it stops
  type-checking within days and becomes code nobody can run;
- it needs exclusions in five places (hatch build, pytest, ruff, mypy, Docker context) and
  still shows up in every grep, code search and contributor's first read;
- it invites issues and pull requests against code that will not be maintained.

Docs are treated differently: documents about frozen subsystems (storage, plugins, cameras,
hardware) stay with a "frozen" banner, because accepted ADRs link to them and the field
notes (for example Eufy's single-client RTSP behaviour) are useful to Frigate users too. A
document costs nothing to keep; dead code does.

## Consequences

- ✅ All effort goes into the part nobody else ships: policy verdicts, evaluation, dry runs.
- ✅ The audience already exists: people running Frigate, usually with Home Assistant. The
  install is one more container next to a working NVR; nobody has to switch.
- ✅ Camera quirks, recording durability and N100 decode budgets become Frigate's concern.
- ⚠️ Hard dependency on Frigate and an MQTT broker. Users without Frigate have no path until
  another source adapter exists (the incident model is source-agnostic on purpose).
- ⚠️ v0.2 is a breaking release for anyone running v0.1.x: no recording, no live view, a
  different config. Existing v0.1.x images stay published; v0.2 images ship only when the
  intent layer works end to end.
- ⚠️ A battery camera does not fit continuous RTSP in Frigate either. For battery cameras
  the evaluation set comes from clips, not from a live Frigate.
- ⚠️ Frigate may ship policy-gated notifications itself. **Tripwire:** if a Frigate release
  gates notifications on a user policy and reports precision on a labelled set, Vidette
  narrows to the evaluation and dry-run tool (useful across NVRs) or upstreams the policy
  layer into Frigate.
- Tripwire: if a Frigate release changes the `reviews` payload incompatibly, the recorded
  fixtures in `server/tests/fixtures/frigate/` fail first; refresh them with
  `vidette frigate tail --record`.

## Alternatives considered

- **Own NVR, trimmed to mains RTSP cameras (direction B)**: 4 to 6 weeks before the first
  differentiating feature, then a permanent maintenance tax, competing with Frigate,
  Scrypted and Viseron at once.
- **Measurement only, decide later (direction C)**: folded in as week 1 of this plan
  (`vidette eval`); on its own it gives users nothing to run.
- **Contribute the policy layer to Frigate directly**: possible later. A separate project
  keeps its own release pace and lets `vidette eval` act as a neutral benchmark across
  NVRs, including Frigate's own GenAI summaries.
- **Keep the NVR in a `legacy/` directory**: see "Why delete on the branch" above.
