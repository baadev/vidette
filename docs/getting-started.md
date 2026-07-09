# Getting started

> **Status: v0.1.0 (alpha).** Everything below works today: record, watch live, review,
> export, and turn motion into events with notifications. The VLM/intent tier and
> plain-language policies (M3–M4) aren't here yet. Onboarding is a product surface for us:
> if any step takes more than a few minutes or a single retry,
> [that's a bug — report it](https://github.com/baadev/vidette/issues).

## What you need

- **A box.** Anything from a Raspberry Pi 5 to a mini-PC. The sweet spot is an Intel N100
  (~$150, ~7 W idle). Sizing guide: [hardware.md](hardware.md).
- **Docker** with the compose plugin.
- **A camera.** Any RTSP/ONVIF camera works natively — including Eufy models with the
  built-in NAS (RTSP) feature ([which ones](cameras/eufy.md)); other ecosystems:
  [support matrix](cameras/README.md).
- **Storage.** Budget roughly 20–45 GB per camera per day for continuous 1080p–2K recording —
  the [storage math](architecture/storage.md#sizing) helps you pick a disk.

## 1. Launch

Pull the published images (no build):

```bash
curl -fsSLO https://raw.githubusercontent.com/baadev/vidette/main/deploy/docker-compose.yml
docker compose up -d
```

Or build from a checkout: `git clone … && docker compose -f deploy/docker-compose.yml up -d --build`.

Open **http://localhost:8642**. The first-run wizard makes you create the admin account —
there are no default credentials, ever.

## 2. Add your first camera

Two ways, use whichever fits — the same cameras, the same features.

**From the UI (easiest).** Open the **Cameras** page → *Add camera*. Enter an RTSP URL, or
click *Discover (ONVIF)* to find cameras on your network. *Probe* checks reachability before
you save; on save, recording and detection start within a few seconds. Don't know your
camera's RTSP URL? [Per-vendor patterns](cameras/onvif-rtsp.md). Eufy owners:
[cameras/eufy.md](cameras/eufy.md).

**As code (infrastructure-as-code).** Bind-mount a config directory (see the commented
`./config` volume in the compose file; on Linux `chown 1000:1000 ./config` so the container
can write to it), drop in a `vidette.yaml`:

```yaml
cameras:
  front-door:
    adapter: rtsp
    source:
      main: rtsp://user:${CAM_PASSWORD}@10.0.20.11:554/stream1
      sub:  rtsp://user:${CAM_PASSWORD}@10.0.20.11:554/stream2   # low-res twin, used for AI
```

Validate any time:

```bash
docker compose exec vidette vidette validate /config/vidette.yaml
```

You get a clean bill or exact errors with paths, plus warnings for any design-stage feature —
the config never silently lies. File-defined cameras hot-apply on change; the UI shows them as
read-only (`CONFIG FILE`) and never rewrites your file. Cameras added in the UI live in the
database and merge in alongside; if an id appears in both, the file wins.

## 3. Watch, record, review

- **Live** (`#/live`) — every camera on one screen over WebRTC; keys `1–9` focus a camera,
  `Esc` returns to the grid. If WebRTC can't get through, tiles fall back to refreshing
  snapshots and say so.
- **Review** (`#/review`) — pick a day, scan the 24-hour strip, hover-scrub the tiny preview
  strip, click an hour, play segments.
- **Export** — set a range, get an MP4 in seconds (remuxed, no re-encode; precision is
  segment/keyframe granularity).

## 4. Turn motion into events

Draw **zones** on each camera (Cameras → *Zones*), picking each one's kind:

| Kind | Use it for |
|---|---|
| `entry` | doors, gates, windows — approach/dwell/touch here is high-signal |
| `object` | protected things (wall equipment, a bike) — interaction detection |
| `private` | yard, porch — presence is notable, transit is not |
| `public` | sidewalk, street — **tracks that never leave it are suppressed** (the passers-by killer) |

With the detector on (default), a person who approaches your door and lingers becomes a
**confirmed event** with a snapshot and a clip; someone cutting across the sidewalk does not.
Review events on the **Events** page (👍/👎 tunes nothing yet but is recorded; ★ pins an event
so its footage is kept). [How the cascade decides](architecture/ai-pipeline.md).

> The plain-language policy (*"alert me only when someone seems interested in getting in"*) and
> VLM scene descriptions are the M3–M4 north star — today, promotion uses zone geometry scaled
> by a `sensitivity` preset.

## 5. Get notified

Add channels in your config (or the notification section — see
[events-and-automations.md](events-and-automations.md)):

- **Signed webhooks** — HMAC-signed JSON with snapshot + clip links, for your own automations.
- **Apprise** — one URL string reaches Telegram, Discord, ntfy, Pushover, email and 100+ more.
- **Web push** — enable it on the **System** page; notifications hit your phone via the PWA
  (install to the home screen on iOS first).
- **MQTT + Home Assistant** — turn on `integrations.mqtt`; Vidette announces camera/person/
  event entities via HA discovery.

## Remote access

Vidette deliberately ships **no cloud relay**. In order of preference:

1. **Tailscale/WireGuard** — zero exposed ports, ~10 minutes of setup.
2. Reverse proxy with TLS (Caddy/Traefik/NPM) if you know exactly what you're doing.
3. Never plain port-forwarding to the internet.

## Troubleshooting quickies

| Symptom | Likely cause |
|---|---|
| `manifest unknown` on `go2rtc` | Old compose pinned a non-existent tag — pull the current `deploy/docker-compose.yml` (go2rtc is pinned to a full version like `1.9.14`) |
| Permission errors writing `/config` (Linux, bind mount) | The bind-mounted dir is root-owned — `chown 1000:1000 ./config`, or use the default named volume |
| `vidette validate` complains about `${VAR}` | The env var isn't set for the container — add it to compose `environment:` or a `.env` file |
| Stream plays in VLC but not the browser | H.265 camera + browser without HEVC — use the H.264 substream, or let go2rtc transcode (CPU cost); see [onvif-rtsp.md](cameras/onvif-rtsp.md#h265) |
| Live tile says `live · mse` instead of `webrtc` | Normal: WebRTC needs a reachable ICE candidate, which a containerized gateway doesn't have by default. MSE works everywhere; for sub-second WebRTC set `VIDETTE_WEBRTC_CANDIDATES=<host-LAN-IP>:8555` (or `server.webrtc_candidates` in YAML) |
| Battery camera (e.g. Eufy) shows `backoff` with a "probably sleeping" note | Expected with `power_profile: battery`: the camera sleeps, Vidette retries with growing backoff (up to 5 min) instead of keeping it awake; recording resumes when it answers |
| Mains camera keeps dropping / `wrong response on DESCRIBE` in go2rtc logs | The camera serves one RTSP client at a time (typical Eufy) and something else is also connected — disconnect other clients; Vidette itself uses one held-open connection (`power_profile: mains`, the default). Details: [eufy.md](cameras/eufy.md#reality-checks) |
| Port 8642 taken | Change the published port in the compose file — the internal one stays 8642 |
| No events, only recordings | Detection needs `detect.enabled` (default on) **and** zones; a camera with no zones records but won't promote events |

Stuck? [Open an issue](https://github.com/baadev/vidette/issues) or write alex@baadev.com.
