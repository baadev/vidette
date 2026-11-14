"""Frigate integration: the first (and for now only) incident source (ADR-0009).

- `models`  typed, lenient views of Frigate's MQTT and HTTP payloads + mapping to incidents
- `source`  MQTT subscriber that publishes incidents and tracked objects on the bus
- `client`  HTTP client for review items, snapshots, recording frames and clips
- `cli`     `vidette frigate check | tail | replay`
"""
