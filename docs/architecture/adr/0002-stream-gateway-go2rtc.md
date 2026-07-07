# ADR-0002: go2rtc as the stream gateway — integrate, don't reimplement

- **Status:** accepted
- **Date:** 2026-07-07

## Context

Every consumer of camera video (recorder, analysis, N browser tabs) must not multiply
connections to the camera — cheap cameras fall over at 2–3 RTSP clients. Browsers need
WebRTC/MSE with codec negotiation. Exotic sources (HomeKit, WebRTC cams, bridge outputs)
need normalization. This is a solved problem: go2rtc (MIT, Go, actively maintained, also
embedded by Frigate) does exactly this and nothing else.

## Decision

go2rtc runs as a **sidecar container**; Vidette manages its configuration (generated from
`cameras:`), monitors its health, and consumes: one camera connection fanned out to recorder
(RTSP), pipeline (RTSP substream), and browsers (WebRTC/MSE). We pin minor versions, never
fork, and contribute fixes upstream.

## Consequences

- ✅ Years of streaming edge cases (codec negotiation, transport fallbacks, exotic sources)
  for free; one camera connection total; sub-second browser latency.
- ✅ A future ecosystem bridge that can push bytes to go2rtc is automatically a Vidette source.
- ⚠️ External dependency on a largely single-maintainer project — mitigated by version
  pinning, MIT license (fork-in-anger is possible but a last resort), and the adapter layer
  isolating Vidette from gateway API details.
- ⚠️ One more container — accepted; compose hides it.

## Alternatives considered

- **Embed go2rtc binary in our image** (Frigate-style) — fewer moving parts for users, less
  clean upgrade/restart isolation; revisit at M1 packaging (either is compatible with this ADR).
- **PyAV/GStreamer in-process fan-out** — reimplements the hardest, least differentiating
  layer; guaranteed inferior to upstream within a year.
- **MediaMTX** — capable RTSP server, weaker consumer-camera-quirk and WebRTC-to-browser
  story than go2rtc for this use case.
