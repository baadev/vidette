# Getting started

> **Status: M0.** Today the stack brings up the app shell, API and config validator. Streaming
> and recording land in **M1** — this page is written for that flow and marks what is not live
> yet. Onboarding is a product surface for us: if any step below takes you more than a few
> minutes or a single retry, [that's a bug — report it](https://github.com/baadev/vidette/issues).

## What you need

- **A box.** Anything from a Raspberry Pi 5 to a mini-PC. The sweet spot is an Intel N100
  (~$150, ~7 W idle). Sizing guide: [hardware.md](hardware.md).
- **Docker** with the compose plugin.
- **A camera.** Any RTSP/ONVIF camera works natively; Eufy and other closed ecosystems go
  through [adapters](cameras/README.md).
- **Storage.** Budget roughly 20–45 GB per camera per day for continuous 1080p–2K recording
  before compaction — the [storage math](architecture/storage.md#sizing) helps you pick a disk.

## 1. Launch

```bash
git clone https://github.com/baadev/vidette.git && cd vidette
docker compose -f deploy/docker-compose.yml up -d --build
```

Open **http://localhost:8642**. You'll see the shell and the system status.

*(M1: the first-run wizard forces you to create the admin account before anything is served —
there are no default credentials, ever.)*

## 2. Configure your first camera

Copy the annotated example and edit:

```bash
mkdir -p config
cp deploy/config.example.yaml config/vidette.yaml
```

The minimum viable camera is four lines:

```yaml
cameras:
  front-door:
    adapter: rtsp
    source:
      main: rtsp://user:${CAM_PASSWORD}@10.0.20.11:554/stream1
      sub:  rtsp://user:${CAM_PASSWORD}@10.0.20.11:554/stream2   # low-res twin, used for AI
```

Don't know your camera's RTSP URL? See [cameras/onvif-rtsp.md](cameras/onvif-rtsp.md) for
per-vendor URL patterns and how to find them. Eufy owners: [cameras/eufy.md](cameras/eufy.md).

Validate before applying — this works **today**:

```bash
docker compose -f deploy/docker-compose.yml exec vidette vidette validate /config/vidette.yaml
```

You'll get either a clean bill, or exact errors with paths, plus warnings for any configured
feature that is still design-stage (so the config never silently lies to you).

## 3. Watch, record, review *(M1)*

- **Live wall** at `/live` — every camera, one screen, sub-second WebRTC.
- **Timeline** at `/review` — scrub like a video editor; motion and events are marked.
- **Export** — drag a range, get an MP4 (remuxed, instant, no re-encode).

## 4. Turn on understanding *(M2–M3)*

Draw zones on each camera (`door`, `street`, …), pick their kind (`entry`, `public`, `object`,
`private`), and enable the detector. The `public` kind is the passers-by killer: people who
only transit it never alert. Then, optionally, attach a VLM (local Ollama profile ships in the
compose file: `--profile vlm`) and write your first plain-language policy
([events-and-automations.md](events-and-automations.md)).

## Remote access

Vidette deliberately ships **no cloud relay**. The recommended patterns, in order:

1. **Tailscale/WireGuard** — zero exposed ports, ~10 minutes of setup.
2. Reverse proxy with TLS (Caddy/Traefik/NPM) if you know exactly what you're doing.
3. Never plain port-forwarding to the internet.

## Troubleshooting quickies

| Symptom | Likely cause |
|---|---|
| `vidette validate` complains about `${VAR}` | The env var isn't set for the container — add it to compose `environment:` or an `.env` file |
| Stream plays in VLC but not in the browser | H.265 camera + browser without HEVC support — enable the H.264 substream or let go2rtc transcode (costs CPU); see [cameras/onvif-rtsp.md](cameras/onvif-rtsp.md#h265) |
| Port 8642 taken | Change the published port in the compose file — the internal one stays 8642 |
| Container up, UI empty | Check `docker compose logs vidette` — the log tells you what to do next; if it doesn't, that's a bug |

Stuck? [Open an issue](https://github.com/baadev/vidette/issues) or write alex@baadev.com.
