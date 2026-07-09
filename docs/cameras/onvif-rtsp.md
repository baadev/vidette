# Generic RTSP / ONVIF cameras

> **Status:** manual RTSP ✅ (M1, works today) · ONVIF streams + probe 🚧 (beta) ·
> ONVIF events, PTZ 📐 → M2.
> This is Vidette's native tongue: if your camera speaks RTSP, it needs no vendor, no cloud,
> no bridge — and it will still work in ten years.

## Find your RTSP URLs

Every camera exposes a **main** (full-res) and usually a **sub** stream (low-res twin —
configure both; the substream is what the AI tiers decode). Common vendor patterns:

| Vendor | Main | Sub |
|---|---|---|
| Hikvision | `rtsp://u:p@IP:554/Streaming/Channels/101` | `.../Channels/102` |
| Dahua / Amcrest | `rtsp://u:p@IP:554/cam/realmonitor?channel=1&subtype=0` | `...&subtype=1` |
| Reolink | `rtsp://u:p@IP:554/h264Preview_01_main` | `.../h264Preview_01_sub` |
| TP-Link Tapo | `rtsp://u:p@IP:554/stream1` | `.../stream2` |
| Ubiquiti (RTSP enabled) | from Protect UI per camera | idem |
| Generic ONVIF | discovered automatically ([`adapter: onvif`](#onvif)) | idem |

Verify before configuring (from the Vidette host):

```bash
ffprobe -rtsp_transport tcp "rtsp://user:pass@10.0.20.11:554/stream1"
```

If `ffprobe` plays but the browser won't later: see [H.265](#h265).

## Configure

```yaml
cameras:
  driveway:
    adapter: rtsp
    source:
      main: rtsp://cam:${CAM_PASSWORD}@10.0.20.12:554/stream1
      sub:  rtsp://cam:${CAM_PASSWORD}@10.0.20.12:554/stream2
```

### ONVIF

If the camera speaks ONVIF (Profile S), skip the URL hunt and let Vidette ask the camera
itself:

```yaml
cameras:
  driveway:
    adapter: onvif
    options:
      host: 10.0.20.12
      # port: 80          # ONVIF device service port, if not the default
      username: cam
      password: ${CAM_PASSWORD}
```

Profiles and RTSP URLs are discovered over SOAP: the highest-resolution profile becomes
`main`, the lowest becomes `sub`. Credentials are embedded into the resolved RTSP URLs for
the stream gateway — set `inject_credentials: false` if your camera already puts them there.
`probe` tells you *specifically* what failed — host unreachable vs. credentials rejected vs.
a typo in the options — instead of a generic error, and on success lists the discovered
profiles with their resolutions.

Network scanning (WS-Discovery) is implemented in the adapter; the `vidette discover` CLI
wiring lands with integration. Events and PTZ are M2.

## Camera-side settings that matter

- **Substream on**, 640–720p, 5–10 fps — the analysis workhorse.
- **Constant bitrate or capped VBR** on main — storage sizing becomes predictable
  ([math](../architecture/storage.md#sizing)).
- **H.264 on the substream** even if main is H.265 — maximizes browser/inference compatibility.
- Keyframe (I-frame) interval ≤ 2× fps — affects clip-start latency and seek precision.
- Camera clock: NTP on, or let ONVIF set it — Vidette timelines trust timestamps.

## <a id="h265"></a>H.265/HEVC and browsers

Recording H.265 is great (smaller archives, codec-copy regardless). *Live view* in browsers
is the catch: HEVC support varies (Safari best, Chromium improving, Firefox no). go2rtc
negotiates what it can; when the browser can't take HEVC, options are: use the H.264
substream for live, or enable a transcode fallback (CPU cost — off by default). The UI
surfaces which path a session is using instead of leaving you guessing.

## Hardening

Change default credentials (camera-side), give cameras static IPs/DHCP reservations, and put
them on an egress-blocked VLAN — [security model](../architecture/security-model.md) has the
checklist. RTSP credentials go into config via `${ENV}`, never literals.
