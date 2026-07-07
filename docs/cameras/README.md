# Camera support

Vidette's core is vendor-neutral; every ecosystem connects through an
[adapter](../architecture/plugins.md). This page is the support matrix; each vendor gets its
own page written to be *the* guide for pairing that hardware with open software — including
the parts the vendor would rather not document.

## Support tiers

| Tier | Meaning |
|---|---|
| **A — native/verified** | Core adapter, tested against real hardware in CI-adjacent smoke tests |
| **B — bridge** | Works through a maintained community sidecar (pinned version, documented risks) |
| **C — generic** | Standard protocols (RTSP/ONVIF) — works, vendor-specific extras vary |
| **D — requested** | Demand recorded; no adapter yet — [add your voice](https://github.com/baadev/vidette/issues/new?template=camera_support.yml) |

## Matrix

| Ecosystem | Path | Tier | Status | Docs |
|---|---|---|---|---|
| Any RTSP camera | native | C→A | 📐 M1 | [onvif-rtsp.md](onvif-rtsp.md) |
| ONVIF (discovery, PTZ, events) | native | C→A | 📐 M1–M2 | [onvif-rtsp.md](onvif-rtsp.md) |
| **Eufy** | bridge: eufy-security-ws | B | 📐 M2 preview → M3 stable | [eufy.md](eufy.md) |
| Reolink | native RTSP/HTTP | C | 📐 M1 | [onvif-rtsp.md](onvif-rtsp.md) |
| Hikvision / Dahua / Amcrest | native RTSP/ONVIF | C | 📐 M1 | [onvif-rtsp.md](onvif-rtsp.md) |
| TP-Link Tapo | native RTSP/ONVIF | C | 📐 M1 | [onvif-rtsp.md](onvif-rtsp.md) |
| UniFi Protect | bridge | B | 🔭 | — |
| Ring | bridge: ring-mqtt | B | 🔭 | — |
| Wyze | bridge: docker-wyze-bridge | B | 🔭 | — |
| HomeKit cameras | via go2rtc source | B | 🔭 | — |
| Nest/Google | (API constraints under review) | D | 🔭 | — |

**Buying advice** (the short version): if you're choosing hardware *for* Vidette, buy cameras
that speak RTSP/ONVIF locally without a cloud account — they'll outlive every vendor pivot.
The per-vendor pages call out which models in each ecosystem do.

## Contributing a camera

1. File a [camera support request](https://github.com/baadev/vidette/issues/new?template=camera_support.yml) —
   even without code, stream URLs / ONVIF quirks / vendor API notes move an ecosystem up the
   backlog (it is sorted by these requests).
2. Writing an adapter: start from [template.md](template.md) +
   [plugins.md](../architecture/plugins.md). Non-Python ecosystem clients integrate via the
   sidecar-bridge pattern — don't port, wrap.
