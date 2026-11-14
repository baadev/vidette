# Frigate fixtures

Recorded-format inputs for the Frigate source and client tests. No live Frigate, broker or
camera is needed to run them.

**Provenance, honestly:** these files were written by hand on 2026-11-14 from the payload
examples and schemas documented for Frigate 0.18 (`docs/docs/integrations/mqtt.md` and
`docs/static/frigate-api.yaml` in the Frigate repository), including the `genai` review
message Frigate 0.17 added. They were **not** captured from a running Frigate. Replace or
extend them with real captures as soon as one is available:

```bash
vidette frigate tail --config vidette.yaml --record session.jsonl
```

| File | What it is |
|---|---|
| `session-front-door.jsonl` | A person walks up the path, stands at the porch, leaves. The review starts as `detection`, is upgraded to `alert`, ends, then gets a GenAI summary. |
| `session-street.jsonl` | A passer-by on the sidewalk (`detection` only), a false-positive dog, and Frigate going offline and back. |
| `malformed.jsonl` | A review without `camera`, a non-JSON payload, then a valid review: the source must survive the first two. |
| `api/review.json`, `api/reviews.json` | `GET /api/review/{id}` and `GET /api/review` answers. |
| `api/config.json` | A trimmed `GET /api/config` (cameras and zones only). |
| `api/version.txt` | `GET /api/version`. |
| `api/snapshot.jpg` | A 64×36 synthetic JPEG made with ffmpeg (a flat box, no footage). |

Format of the `.jsonl` files: one MQTT message per line,
`{"at": <epoch>, "topic": "frigate/reviews", "payload": <JSON payload or plain text>}`,
exactly what `vidette frigate tail --record` writes and `vidette frigate replay` reads.
