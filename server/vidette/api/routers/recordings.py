"""Recording timeline + range export (M1).

Recordings are read straight from the segment index; segment files stream as
`video/mp4`. Exports are asynchronous remux jobs owned by the ExportManager — this router
only validates, enqueues and serves results. Guarded by the `read:streams` scope.

Path safety: a segment/export file is only served when its resolved path stays under
`storage.media_dir` — rows come from our own DB, but defense in depth is house policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.recording.exporter import ExportError
from vidette.recording.previews import preview_path
from vidette.runtime import AppRuntime

MAX_QUERY_RANGE_S = 7 * 24 * 3600  # listing window; export has its own (tighter) clamp

router = APIRouter(
    prefix="/api/v1",
    tags=["recordings"],
    dependencies=[Depends(require_scope("read:streams"))],
)


class SegmentOut(BaseModel):
    id: int
    start_ts: float
    end_ts: float
    size_bytes: int


class HourBucketOut(BaseModel):
    hour_start_ts: float
    recorded_seconds: float
    bytes: int


class ExportRequest(BaseModel):
    camera: str
    from_ts: float
    to_ts: float


class ExportAccepted(BaseModel):
    id: str
    state: str
    error: str | None


class ExportStatus(ExportAccepted):
    size_bytes: int | None
    download: str | None


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _ensure_camera(runtime: AppRuntime, camera: str) -> None:
    if camera not in runtime.config.cameras:
        raise problem(
            404,
            "Camera not found",
            f"no camera '{camera}' is configured — list configured ids via GET /api/v1/cameras",
        )


_SEGMENT_GONE = (
    "the segment does not exist or its file is no longer on disk — pick a segment id "
    "from GET /api/v1/recordings"
)


@router.get("/recordings")
async def list_recordings(
    request: Request, camera: str, from_ts: float, to_ts: float
) -> list[SegmentOut]:
    runtime = _runtime(request)
    _ensure_camera(runtime, camera)
    if to_ts <= from_ts:
        raise problem(
            422,
            "Empty time range",
            "to_ts must be greater than from_ts — swap or widen the range and retry",
        )
    if to_ts - from_ts > MAX_QUERY_RANGE_S:
        raise problem(
            422,
            "Time range too large",
            "the recordings listing covers at most 7 days per request — narrow "
            "from_ts/to_ts and page through longer spans",
        )
    rows = await runtime.db.segments_between(camera, from_ts, to_ts)
    return [
        SegmentOut(id=row.id, start_ts=row.start_ts, end_ts=row.end_ts, size_bytes=row.size_bytes)
        for row in rows
    ]


@router.get("/recordings/summary")
async def recordings_summary(request: Request, camera: str, day: str) -> list[HourBucketOut]:
    runtime = _runtime(request)
    _ensure_camera(runtime, camera)
    try:
        day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise problem(
            422,
            "Invalid day",
            f"'{day}' is not a valid day — pass day=YYYY-MM-DD (UTC), e.g. day=2026-07-07",
        ) from None
    day_start_ts = day_start.timestamp()
    buckets = await runtime.db.hourly_summary(camera, day_start_ts, day_start_ts + 86400.0)
    return [
        HourBucketOut(
            hour_start_ts=bucket.hour_start_ts,
            recorded_seconds=bucket.recorded_seconds,
            bytes=bucket.bytes,
        )
        for bucket in buckets
    ]


@router.get("/recordings/segments/{segment_id}/file")
async def segment_file(segment_id: int, request: Request) -> FileResponse:
    runtime = _runtime(request)
    row = await runtime.db.get_segment(segment_id)
    if row is None:
        raise problem(404, "Recording not available", _SEGMENT_GONE)
    media_root = runtime.config.storage.media_dir.resolve()
    path = Path(row.path).resolve()
    if not path.is_relative_to(media_root) or not path.is_file():
        raise problem(404, "Recording not available", _SEGMENT_GONE)
    return FileResponse(path, media_type="video/mp4")


@router.get("/recordings/preview")
async def preview_file(request: Request, camera: str, hour_start_ts: float) -> FileResponse:
    runtime = _runtime(request)
    _ensure_camera(runtime, camera)
    media_root = runtime.config.storage.media_dir.resolve()
    path = preview_path(runtime.config.storage.media_dir, camera, hour_start_ts).resolve()
    if not path.is_relative_to(media_root) or not path.is_file():
        raise problem(
            404,
            "Preview not available",
            "preview not generated yet — previews cover completed hours and appear "
            "within ~5 minutes",
        )
    return FileResponse(path, media_type="video/mp4")


@router.post("/export", status_code=202)
async def create_export(body: ExportRequest, request: Request) -> ExportAccepted:
    runtime = _runtime(request)
    try:
        job = await runtime.exporter.create(body.camera, body.from_ts, body.to_ts)
    except ExportError as exc:
        raise problem(422, "Export rejected", str(exc)) from exc
    return ExportAccepted(id=job.id, state=job.state, error=job.error)


@router.get("/export/{job_id}")
async def get_export(job_id: str, request: Request) -> ExportStatus:
    job = _runtime(request).exporter.get(job_id)
    if job is None:
        raise problem(
            404,
            "Export not found",
            f"no export job '{job_id}' — start one with POST /api/v1/export "
            "(job records are removed ~24 h after completion)",
        )
    download = f"/api/v1/export/{job.id}/download" if job.state == "done" else None
    return ExportStatus(
        id=job.id, state=job.state, error=job.error, size_bytes=job.size_bytes, download=download
    )


@router.get("/export/{job_id}/download")
async def download_export(job_id: str, request: Request) -> FileResponse:
    runtime = _runtime(request)
    job = runtime.exporter.get(job_id)
    not_ready = problem(
        404,
        "Export not downloadable",
        f"export '{job_id}' is unknown, not finished, or already cleaned up — poll "
        f"GET /api/v1/export/{job_id} until state is 'done', then download promptly",
    )
    if job is None or job.state != "done" or job.path is None:
        raise not_ready
    media_root = runtime.config.storage.media_dir.resolve()
    path = Path(job.path).resolve()
    if not path.is_relative_to(media_root) or not path.is_file():
        raise not_ready
    return FileResponse(
        path, media_type="video/mp4", filename=f"vidette-{job.camera}-{job.id}.mp4"
    )
