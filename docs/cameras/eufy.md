# Eufy

> **The one honest sentence first:** Eufy integration is possible **only** through the
> camera's built-in **NAS (RTSP)** feature, and **only on models that have it**. There is no
> working cloud/P2P bridge — the community reverse-engineered client stopped being viable
> when Anker migrated its backend (see [history](#why-there-is-no-bridge)). If your model
> has no NAS (RTSP) option, Vidette cannot integrate it today, and neither can anything
> else self-hosted. Not affiliated with Anker/Eufy; trademarks belong to their owners.

Eufy hardware is generally well-regarded; the app experience and the closed ecosystem are
why this project exists. The good news: on RTSP-capable models the integration is the most
durable kind there is — a plain local stream with no vendor cloud in the path.

## The path: NAS (RTSP)

Eufy's "NAS storage" feature exposes an RTSP stream (it was built so a Synology-class NAS
could record the camera — Vidette plays the NAS's role). Enable it per camera:

1. Eufy app → your camera → **Settings → General → Storage → NAS (RTSP)** → enable.
   (Menu naming varies slightly by model and app version; on some models the toggle only
   appears after a firmware update.)
2. Set the RTSP username/password the app asks for.
3. Note the URL the app shows (typically `rtsp://<user>:<password>@<camera-ip>/live0`;
   HomeBase-attached cameras stream via the HomeBase's address on some generations —
   verify with `ffprobe`).
4. Give the camera (or HomeBase) a static IP / DHCP reservation.
5. Add it to Vidette as a **plain `rtsp` camera** — there is no special Eufy adapter,
   because none is needed:

```yaml
cameras:
  backyard:
    adapter: rtsp
    name: "Backyard (eufyCam)"
    source:
      main: rtsp://user:${EUFY_RTSP_PASSWORD}@10.0.20.30/live0
```

Verify from the Vidette host before configuring:

```bash
ffprobe -rtsp_transport tcp "rtsp://user:PASSWORD@10.0.20.30/live0"
```

## Which models have it

Availability is decided by Anker per model line and firmware — **check your exact model in
the app** (the NAS (RTSP) menu item either exists or it doesn't). As commonly reported by
the community at the time of writing:

| Family | NAS (RTSP) | Notes |
|---|---|---|
| eufyCam 3 / 3C / E330 (Professional) | ✓ commonly reported | via HomeBase 3 generation |
| Older eufyCam 2 / 2C / 2 Pro lines | varies by firmware | many report it working via HomeBase 2 |
| Wired indoor cams (Indoor Cam family) | ✓ commonly reported | direct from the camera |
| Wired doorbells | varies | some expose RTSP, some never got it |
| Battery doorbells, SoloCams, some newer lines | often ✗ | no NAS (RTSP) menu → no integration path |

Treat this table as a starting point, not a promise — firmware updates have both added and
removed capabilities in this ecosystem. **Please report what your model does** via the
[camera support template](https://github.com/baadev/vidette/issues/new?template=camera_support.yml);
verified reports replace "commonly reported" rows.

## Reality checks

- **Battery cameras.** RTSP is a continuous protocol; a battery camera either sleeps (the
  stream drops) or streams and eats its battery. Vidette handles the sleep honestly: after
  repeated silent connections it backs off up to 5 minutes (instead of keeping the camera
  awake), the camera card says it is probably sleeping, and recording resumes automatically
  when the camera answers — e.g. after its own motion wake. Mains-powered and solar-topped
  models are the realistic candidates for true continuous recording.
- **Resolution caps.** Some models cap the RTSP stream below the sensor's native recording
  resolution. What `ffprobe` shows is what you get.
- **No vendor events.** The RTSP path carries video only — doorbell presses and Eufy's
  own detections stay inside the Eufy app. Vidette's own cascade (M2) replaces them.
- **One consumer.** Some firmwares handle exactly one RTSP client well — which is fine:
  go2rtc connects once and fans out to everything ([ADR-0002](../architecture/adr/0002-stream-gateway-go2rtc.md)).

## <a id="why-there-is-no-bridge"></a>Why there is no bridge

For years the community used [bropat's `eufy-security-client`](https://github.com/bropat/eufy-security-client)
(and its `eufy-security-ws` wrapper) — a reverse-engineered client for Eufy's cloud auth and
P2P streaming, and genuinely heroic work. Anker then migrated its backend to the new
platform generation and began removing the legacy APIs that client was built on; the
project itself warns that functionality degrades and will stop as the shutdown completes.
As of mid-2026 the bridge path is not something a product can be built on, so **Vidette
does not ship a Eufy bridge adapter** — a `cameras.*.adapter: eufy` entry gets a validator
hint pointing here instead. If the community ever rebuilds a client on the new platform,
a bridge adapter can return as a sidecar ([the pattern survives](../architecture/plugins.md#the-sidecar-bridge-pattern));
we track that as 🔭 exploring, gated on upstream reality, in [ROADMAP.md](../../ROADMAP.md).

This episode is the project's thesis in one story: **closed ecosystems can revoke your
integration at any moment; the recording layer should belong to you.** A vendor pivot cost
this ecosystem its bridge — your archive, living in Vidette as plain MP4 + SQLite, is the
part no vendor can take away.

## Buying advice

If you're adding cameras to a Vidette setup: prefer hardware that speaks **RTSP/ONVIF
locally without a cloud account** ([generic guide](onvif-rtsp.md)) — it will outlive every
vendor pivot. Within Eufy, that means: verify the NAS (RTSP) menu exists on the exact model
*before* buying, and prefer mains/solar power if you want continuous recording.
