"""Prometheus metrics: `GET /metrics` (docs/api.md).

Hand-rolled text exposition (format version 0.0.4): the metric set is small and moves
with the code, so the format's few escaping rules cost less than a client-library
dependency and a second registry of truth.

The endpoint is guarded by the `read:events` scope, which personal access tokens can
carry — so it is scrapeable with `Authorization: Bearer vd_…` in the Prometheus scrape
config:

    scrape_configs:
      - job_name: "vidette"
        metrics_path: /metrics
        authorization:
          type: Bearer
          credentials: "vd_…"   # a settings-created token carrying read:events

With `server.auth.mode: none` no header is needed, like the rest of the API.

Conventions: counters are process-lifetime (they reset on restart, which Prometheus
handles); `vidette_events_total` is a gauge on purpose — events migrate between lifecycle
states, so per-state counts can shrink. A gauge whose source is not known yet keeps its
HELP/TYPE headers but emits no sample line, never NaN. The v0.1 recorder, pipeline and
janitor families went with the NVR core (ADR-0009, tag `nvr-core`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from vidette import __version__
from vidette.auth.deps import require_scope
from vidette.core.events import EventState
from vidette.runtime import AppRuntime

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_scope("read:events"))])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_Sample = tuple[dict[str, str], int | float]


def _escape_label(value: str) -> str:
    """Escape a label value per the text exposition format (backslash first, then " and LF)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _family(name: str, kind: str, help_text: str, samples: Iterable[_Sample]) -> list[str]:
    """Render one metric family: HELP/TYPE headers plus zero or more samples."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        if labels:
            body = ",".join(f'{key}="{_escape_label(val)}"' for key, val in labels.items())
            lines.append(f"{name}{{{body}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return lines


def _known(value: int | float | None) -> list[_Sample]:
    """One unlabeled sample — or none at all when the source value is unknown."""
    return [] if value is None else [({}, value)]


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus scrape endpoint. Requires `read:events`; see the module docstring for
    the `Authorization: Bearer vd_…` scrape configuration."""
    runtime = cast(AppRuntime, request.app.state.runtime)
    notifier = runtime.notifier.status()
    event_counts = {state.value: 0 for state in EventState}
    event_counts.update(await runtime.db.count_events_by_state())

    lines: list[str] = []
    lines += _family(
        "vidette_info",
        "gauge",
        "Build information; the value is always 1.",
        [({"version": __version__}, 1)],
    )
    source = runtime.frigate_source.status() if runtime.frigate_source is not None else None
    lines += _family(
        "vidette_frigate_connected",
        "gauge",
        "1 while connected to Frigate's MQTT broker (no sample without a frigate: section).",
        _known(None if source is None else int(source.connected)),
    )
    lines += _family(
        "vidette_frigate_messages_total",
        "counter",
        "MQTT messages received from Frigate.",
        _known(None if source is None else source.messages_total),
    )
    lines += _family(
        "vidette_frigate_invalid_total",
        "counter",
        "Frigate messages that did not match the expected payload.",
        _known(None if source is None else source.invalid_total),
    )
    lines += _family(
        "vidette_frigate_incidents_total",
        "counter",
        "Incidents opened from Frigate review items.",
        _known(None if source is None else source.incidents_total),
    )
    lines += _family(
        "vidette_notifications_delivered_total",
        "counter",
        "Notifications delivered across all channels.",
        [({}, notifier.delivered_total)],
    )
    lines += _family(
        "vidette_notifications_failed_total",
        "counter",
        "Notification deliveries that failed.",
        [({}, notifier.failed_total)],
    )
    lines += _family(
        "vidette_bus_dropped_total",
        "counter",
        "Bus messages dropped because a subscriber queue was full (drop-oldest).",
        [({}, runtime.bus.dropped)],
    )
    lines += _family(
        "vidette_events_total",
        "gauge",
        "Events in the store by lifecycle state (states migrate, so counts can shrink).",
        [({"state": state}, count) for state, count in sorted(event_counts.items())],
    )
    return PlainTextResponse("\n".join(lines) + "\n", media_type=CONTENT_TYPE)
