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
states, so per-state counts can shrink. A gauge whose source is not known yet (janitor
before its first run) keeps its HELP/TYPE headers but emits no sample line — never NaN.
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
    pipelines = sorted(runtime.pipeline.status().items())
    recorders = sorted(runtime.recorder.status().items())
    janitor = runtime.janitor.status()
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
    lines += _family(
        "vidette_detector_ready",
        "gauge",
        "1 when the Tier-1 detector is loaded and ready, else 0.",
        [({}, int(runtime.detector_state == "ready"))],
    )
    lines += _family(
        "vidette_pipeline_up",
        "gauge",
        "1 when the camera's analysis pipeline is running.",
        [({"camera": camera}, int(s.state == "running")) for camera, s in pipelines],
    )
    lines += _family(
        "vidette_pipeline_frames_total",
        "counter",
        "Substream frames pulled by the camera's pipeline.",
        [({"camera": camera}, s.frames_total) for camera, s in pipelines],
    )
    lines += _family(
        "vidette_pipeline_motion_frames_total",
        "counter",
        "Frames the Tier-0 motion gate let through.",
        [({"camera": camera}, s.motion_frames) for camera, s in pipelines],
    )
    lines += _family(
        "vidette_pipeline_detect_calls_total",
        "counter",
        "Tier-1 detector invocations.",
        [({"camera": camera}, s.detect_calls) for camera, s in pipelines],
    )
    lines += _family(
        "vidette_recorder_up",
        "gauge",
        "1 when the camera's recorder is writing segments.",
        [({"camera": camera}, int(s.state == "recording")) for camera, s in recorders],
    )
    lines += _family(
        "vidette_recorder_restarts_total",
        "counter",
        "Recorder ffmpeg restarts.",
        [({"camera": camera}, s.restarts) for camera, s in recorders],
    )
    lines += _family(
        "vidette_disk_total_bytes",
        "gauge",
        "Capacity of the filesystem holding media_dir.",
        _known(janitor.disk_total_bytes),
    )
    lines += _family(
        "vidette_disk_free_bytes",
        "gauge",
        "Free space on the filesystem holding media_dir.",
        _known(janitor.disk_free_bytes),
    )
    lines += _family(
        "vidette_media_bytes",
        "gauge",
        "Recorded media tracked in the database.",
        _known(janitor.media_bytes),
    )
    lines += _family(
        "vidette_janitor_expired_deleted_total",
        "counter",
        "Segments deleted because their retention window expired.",
        [({}, janitor.expired_deleted_total)],
    )
    lines += _family(
        "vidette_janitor_pressure_deleted_total",
        "counter",
        "Segments deleted early to relieve disk pressure.",
        [({}, janitor.pressure_deleted_total)],
    )
    lines += _family(
        "vidette_storage_probe_ok",
        "gauge",
        "1 when the janitor's last media_dir write probe succeeded, else 0.",
        _known(None if janitor.last_probe_ok is None else int(janitor.last_probe_ok)),
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
