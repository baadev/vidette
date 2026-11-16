# Getting started

> **Status: v0.2 development line.** Vidette reads Frigate's review items over MQTT (or
> clips from a folder), judges each one with a VLM under your policy, and notifies only
> for events the judge confirmed; `vidette eval` measures policies on labelled clips. No
> accuracy numbers exist yet ([ROADMAP.md](../ROADMAP.md)). Published images are still the
> v0.1 NVR line, so everything below builds from a checkout. Without Frigate, with a camera
> of your own and Telegram: the [README's quickstart](../README.md#your-own-camera-without-frigate).

There are two ways in. You can do the first one on a laptop in five minutes, without a
camera.

## A. Measure first: `vidette eval` (no camera, no Frigate)

Needs [uv](https://docs.astral.sh/uv/) and a checkout:

```bash
git clone https://github.com/baadev/vidette && cd vidette/server
uv sync --locked
uv run vidette eval ../datasets/synthetic-sample
```

You get precision, recall and false alarms per day for four baselines on a five-entry
synthetic set (its numbers mean nothing; it shows the format). Then build a set from your
own camera's clips: [eval.md](eval.md#adding-your-own-clips-battery-cameras-and-friends).

## B. Run it next to Frigate

What you need:

- **Frigate 0.14 or newer**, with MQTT enabled, and ideally `snapshots` and `record` on
  ([frigate.md](frigate.md)). Frigate handles cameras, recording and detection; size the
  box for Frigate, Vidette itself is one small Python process.
- **The MQTT broker** Frigate publishes to.
- **Docker** with the compose plugin.

1. Write `vidette.yaml` from the [annotated example](../deploy/config.example.yaml); the
   part that matters is the `frigate:` section. Check it:

   ```bash
   cd server && uv run vidette validate ../path/to/vidette.yaml
   uv run vidette frigate check --config ../path/to/vidette.yaml
   ```

2. Watch what Vidette makes of Frigate's messages before running the service:

   ```bash
   uv run vidette frigate tail --config ../path/to/vidette.yaml
   ```

3. Run the service on the same Docker network as Frigate (layouts in
   [frigate.md](frigate.md#network-layouts)), or standalone from the checkout:

   ```bash
   docker compose -f deploy/docker-compose.yml up -d --build
   ```

   Put `vidette.yaml` in the `/config` volume (or bind-mount `./config`; on Linux
   `chown 1000:1000 ./config`).

4. Open **http://localhost:8642**. The first run makes you create the admin account; there
   are no default credentials. The **Events** page fills as Frigate reports review items;
   the **System** page shows whether the broker is connected and how many messages arrived.

Notifications (webhooks, Apprise, web push, MQTT) are configured as in
[events-and-automations.md](events-and-automations.md), the Telegram bot with buttons as in
[telegram.md](telegram.md). They fire only for events the judge confirmed
([vlm.md](vlm.md)): `observed` events never notify.

## Remote access

Vidette ships **no cloud relay**. In order of preference:

1. **Tailscale/WireGuard**: zero exposed ports, about ten minutes of setup.
2. A reverse proxy with TLS (Caddy, Traefik, NPM) if you know exactly what you are doing.
3. Never plain port-forwarding to the internet.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `cameras: this is a v0.1 NVR section` | A v0.1 config. Cameras now live in Frigate; replace the section with `frigate:` ([configuration.md](configuration.md)) |
| System page: Frigate not connected, `frigate.mqtt_connection_failed` events | Broker host, port or credentials differ from Frigate's own `mqtt:` section, or Vidette is not on a network that reaches the broker |
| Connected, but no events | Frigate produces no review items (check its Review page), or `frigate.cameras` / `frigate.severities` filter them out; `vidette frigate tail` shows what arrives |
| `frigate.payload_invalid` events | Frigate older than 0.14, or a payload change; record a sample with `vidette frigate tail --record` and open an issue |
| Events without a snapshot | `snapshots` disabled in Frigate, or Frigate was unreachable over HTTP (`frigate.url`) |
| Clip says "no recording" | `record` disabled for that camera in Frigate, or its retention removed the footage |
| `vidette validate` complains about `${VAR}` | The env var is not set for the container; add it to compose `environment:` or a `.env` file |

Stuck? [Open an issue](https://github.com/baadev/vidette/issues) or write alex@baadev.com.
