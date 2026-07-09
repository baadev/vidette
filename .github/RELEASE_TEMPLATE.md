## Vidette v0.1.0 — Watch + Detect

The first tagged release. Vidette records your cameras and turns motion into **events you
actually care about** — a person approaching the door, not every passing car — delivered
wherever you want them. All local, all on your hardware.

### Run it

```bash
curl -fsSLO https://raw.githubusercontent.com/baadev/vidette/main/deploy/docker-compose.yml
docker compose up -d
# http://localhost:8642 → create your admin account → add a camera in the UI
```

Published images: `ghcr.io/baadev/vidette:0.1.0` and `:latest` (`linux/amd64`, `linux/arm64`).

### What works

- **Watch** — RTSP/ONVIF ingest via a managed go2rtc gateway, codec-copy recording (no
  transcode) with an SQLite index, a sub-second WebRTC live wall, an hour-strip review UI
  with scrub previews, and MP4 range export (remux). Eufy works through its NAS (RTSP)
  feature on [supported models](docs/cameras/eufy.md).
- **Detect** — the understanding cascade, tiers 0–2: motion gate → YOLOX object detection
  (ONNX Runtime, Apache-2.0 model) → trajectory geometry + zone algebra. `public` zones
  suppress passers-by *before any alert logic runs*.
- **Notify** — signed webhooks (HMAC), Apprise (Telegram/Discord/100+), web push (VAPID),
  and MQTT with Home Assistant discovery. Live event stream over WebSocket; Prometheus
  `/metrics`.
- **Manage** — add, edit and delete cameras and draw detection zones from the UI, *or* keep
  them as code in `vidette.yaml`. The file stays the source of truth; the UI never rewrites it.
- **Trust** — first-run admin bootstrap (no default credentials), scoped API tokens, a
  retention + disk-health janitor, and zero telemetry.

### Honest limits

- The **VLM/intent tier (M3)** and **plain-language policies (M4)** are not here yet — event
  promotion currently uses geometric heuristics scaled by a sensitivity preset.
- Reference N100 budgets are measured on dev hardware, not yet on the reference box.
- ONVIF is **beta** (streams + discovery; events/PTZ to come). ByteTrack-proper tracking and
  a public accuracy clip set are tracked in the [roadmap](ROADMAP.md).

Full detail: [CHANGELOG](CHANGELOG.md) · [ROADMAP](ROADMAP.md). Feedback: open an issue or
email **alex@baadev.com**.

---
