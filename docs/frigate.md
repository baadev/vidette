# Running Vidette with Frigate

Since v0.2, Vidette does not talk to cameras. [Frigate](https://frigate.video) records,
detects and groups activity into review items; Vidette reads those items and (from week 2
of the v0.2 plan) decides which ones are worth a notification under your policy. Why:
[ADR-0009](architecture/adr/0009-intent-layer-over-frigate.md).

## Supported Frigate versions

| Frigate | Status |
|---|---|
| 0.18 | Target. The parser is built against the payloads documented for 0.18. |
| 0.17 | Expected to work; adds the `genai` review message that `frigate-genai` scores. |
| 0.14 to 0.16 | Expected to work (review items exist since 0.14); no GenAI review summaries. |
| older than 0.14 | Not supported: there is no `reviews` topic. `vidette frigate check` says so. |

"Expected" is honest wording: the test fixtures are hand-built from Frigate's documented
payloads, not captured from each version. Unknown fields are ignored and optional fields
default, so additive changes in new Frigate releases do not break parsing. If a message does
not parse, Vidette counts it, reports `frigate.payload_invalid` and keeps going; please
record a sample (below) and open an issue.

## What Frigate needs to have on

```yaml
# in Frigate's config
mqtt:
  enabled: true
  host: mqtt              # the broker Vidette will read from too
  # topic_prefix: frigate # default; Vidette's frigate.mqtt.topic_prefix must match

snapshots:
  enabled: true           # Vidette caches one snapshot per event for its Events page

record:
  enabled: true           # clips on Vidette's Events page and, from week 2, the frames
                          # the VLM judges; without it Vidette falls back to snapshots
```

Review items are what Vidette consumes, so Frigate's own review settings decide what
reaches it. By default Frigate marks a review `alert` for a person or car and `detection`
for other tracked objects; Vidette takes both unless `frigate.severities` says otherwise.

Optional: Frigate's GenAI review summaries (0.17+). Vidette stores them next to its own
events and `vidette eval` scores them as the `frigate-genai` baseline.

## Vidette's side

```yaml
# vidette.yaml
frigate:
  url: http://frigate:5000      # Frigate's internal API port
  mqtt:
    host: mqtt
    port: 1883
    username: vidette
    password: ${FRIGATE_MQTT_PASSWORD}
    topic_prefix: frigate
  cameras: all                  # or [front_door, back_yard]: Frigate camera keys
  severities: [alert, detection]
  tracked_objects: true         # also read <prefix>/events
```

Full reference: [configuration.md](configuration.md).

**Port 5000 has no authentication.** It is Frigate's internal port for services on the
same Docker network. Keep it off the LAN and the internet: Vidette should reach it over the
compose network, not over a published port. Frigate's authenticated port (8971) is not
supported yet.

## Network layouts

**Same compose project as Frigate (simplest).** Add a service to the file that already runs
Frigate and the broker:

```yaml
services:
  # frigate: …   (yours)
  # mqtt: …      (yours)
  vidette:
    build:
      context: ../vidette              # your Vidette checkout
      dockerfile: deploy/Dockerfile
    restart: unless-stopped
    ports: ["8642:8642"]
    volumes:
      - ./vidette:/config              # vidette.yaml + database
      - vidette-media:/media/vidette
    environment:
      - FRIGATE_MQTT_PASSWORD=${FRIGATE_MQTT_PASSWORD}
volumes:
  vidette-media:
```

Then `frigate.url: http://frigate:5000` and `frigate.mqtt.host: mqtt` (the service names).

**Separate compose project.** Join Frigate's network from `deploy/docker-compose.yml`:

```yaml
services:
  vidette:
    networks: [default, frigate]
networks:
  frigate:
    external: true
    name: frigate_default    # `docker network ls` shows the real name
```

## Check it, watch it, record it

```bash
vidette frigate check --config vidette.yaml
# Frigate 0.18.2-… at http://frigate:5000
# cameras: back_yard, front_door

vidette frigate tail --config vidette.yaml --record session.jsonl
# 21:03:04 incident.opened      front_door     detection person@path  frigate:1731…
# 21:03:11 incident.updated     front_door     alert     person@path,porch  frigate:1731…
# 21:03:37 incident.closed      front_door     alert     person@path,porch  frigate:1731…

vidette frigate replay session.jsonl     # same output, offline, no broker
```

`--record` appends every raw MQTT message as a JSON line. That file is a test fixture and a
bug report at the same time; it contains camera names and zone names, and nothing else
about your home (no images).

## What Vidette does with each message

| Frigate topic | Message | Vidette bus topic |
|---|---|---|
| `<prefix>/reviews` | `new` (first one passing the filters) | `incident.opened` |
| `<prefix>/reviews` | `new` / `update` after that | `incident.updated` |
| `<prefix>/reviews` | `end` | `incident.closed` |
| `<prefix>/reviews` | `genai` (0.17+) | `incident.summarized` |
| `<prefix>/events` | `new` / `update` / `end`, false positives skipped | `object.opened/updated/closed` |
| `<prefix>/available` | `offline`, `stopped`, then `online` again | system events `frigate.unavailable` / `frigate.available` |

Each incident becomes one event row in state `observed` (snapshot cached, Frigate's
severity and summary kept). `observed` events show up in the Events page and never notify
anyone; verdicts that can confirm or dismiss them arrive in week 2.

Vidette only reads from Frigate: it never acknowledges reviews, retains events or changes
Frigate's config.
