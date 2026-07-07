# Generic RTSP / ONVIF cameras

> **Status: 📐 M1** (manual RTSP, ONVIF discovery) → M2 (ONVIF events, PTZ). This is
> Vidette's native tongue: if your camera speaks RTSP, it needs no vendor, no cloud, no
> bridge — and it will still work in ten years.

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
| Generic ONVIF | discovered automatically (M1) | idem |

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

ONVIF (M1) reduces this to `adapter: onvif` + host/credentials: streams, profiles, PTZ
capabilities and event topics are discovered. Vidette's `probe` tells you *specifically* what
failed (auth vs. network vs. disabled service) instead of a generic error.

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
