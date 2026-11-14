"""Frigate HTTP API client: versions, review items, snapshots, recording frames, clips.

Endpoints used (all documented for Frigate 0.18; all present since 0.14):

- `GET /api/version`, `GET /api/config`
- `GET /api/review`, `GET /api/review/{id}`
- `GET /api/events/{id}/snapshot.jpg`              best snapshot of one tracked object
- `GET /api/{camera}/recordings/{ts}/snapshot.jpg`  one frame from the recordings
- `GET /api/{camera}/start/{start}/end/{end}/clip.mp4`

`url` is the internal, unauthenticated API port (5000) on the compose network; the
authenticated port (8971) needs a session token and is not supported yet. Nothing here
writes to Frigate.

Every failure becomes a `FrigateError` whose message says what to check; the URL in it
never carries credentials because the configured URL has none (port 5000 needs none).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from vidette.core.incidents import Incident
from vidette.frigate.models import FrigateReview

DEFAULT_TIMEOUT_S = 10.0
CLIP_TIMEOUT_S = 120.0  # Frigate assembles clips on request; long reviews take a while


class FrigateError(Exception):
    """A Frigate API call failed; the message says what to check."""


class FrigateNotFoundError(FrigateError):
    """Frigate answered 404: the item, snapshot or recording does not exist (anymore)."""


@dataclass(frozen=True)
class Frame:
    """One still image of an incident, with where it came from."""

    at: float  # unix epoch seconds of the frame (best effort for event snapshots)
    jpeg: bytes
    origin: str  # "recording" | "event_snapshot"


def frame_times(started_at: float, ended_at: float | None, count: int) -> list[float]:
    """`count` evenly spaced instants inside the incident, ends excluded.

    An open incident is sampled over its first 10 s: what is already on disk.
    """
    if count < 1:
        return []
    end = ended_at if ended_at is not None and ended_at > started_at else started_at + 10.0
    step = (end - started_at) / (count + 1)
    return [round(started_at + step * (index + 1), 3) for index in range(count)]


class FrigateClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api",
            timeout=timeout_s,
            transport=transport,
            headers={"accept": "application/json, image/jpeg, video/mp4"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FrigateClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- plumbing ------------------------------------------------------------------------------

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None, timeout_s: float | None = None
    ) -> httpx.Response:
        url = f"{self.base_url}/api{path}"
        try:
            response = await self._http.get(
                path,
                params=params,
                timeout=timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as exc:
            raise FrigateError(
                f"Frigate did not answer {url} in time ({type(exc).__name__}); check that "
                "Frigate is running and not overloaded"
            ) from exc
        except httpx.HTTPError as exc:
            raise FrigateError(
                f"cannot reach Frigate at {url} ({exc}); check frigate.url and that Vidette "
                "shares a network with Frigate (docs/frigate.md)"
            ) from exc
        if response.status_code == 404:
            raise FrigateNotFoundError(f"Frigate has no {path} (404)")
        if response.status_code in (401, 403):
            raise FrigateError(
                f"Frigate refused {url} ({response.status_code}); point frigate.url at the "
                "internal port 5000, the authenticated port 8971 is not supported yet"
            )
        if response.is_error:
            raise FrigateError(f"Frigate answered {response.status_code} for {url}")
        return response

    async def _json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = await self._get(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FrigateError(f"Frigate returned non-JSON for {path}") from exc

    # --- metadata --------------------------------------------------------------------------------

    async def version(self) -> str:
        response = await self._get("/version")
        return response.text.strip().strip('"')

    async def config(self) -> dict[str, Any]:
        data = await self._json("/config")
        if not isinstance(data, dict):
            raise FrigateError("Frigate /api/config is not a JSON object")
        return data

    async def cameras(self) -> list[str]:
        """Camera keys from Frigate's config (what Vidette's camera names refer to)."""
        cameras = (await self.config()).get("cameras")
        return sorted(cameras) if isinstance(cameras, dict) else []

    # --- review items ----------------------------------------------------------------------------

    async def review(self, review_id: str) -> FrigateReview:
        data = await self._json(f"/review/{quote(review_id, safe='')}")
        return FrigateReview.model_validate(data)

    async def reviews(
        self,
        *,
        cameras: list[str] | None = None,
        severity: str | None = None,
        after: float | None = None,
        before: float | None = None,
        limit: int | None = None,
    ) -> list[FrigateReview]:
        """Past review items, newest first (for dry runs over history)."""
        params: dict[str, Any] = {}
        if cameras:
            params["cameras"] = ",".join(cameras)
        if severity is not None:
            params["severity"] = severity
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if limit is not None:
            params["limit"] = limit
        data = await self._json("/review", params=params)
        if not isinstance(data, list):
            raise FrigateError("Frigate /api/review did not return a list")
        return [FrigateReview.model_validate(item) for item in data]

    # --- media -----------------------------------------------------------------------------------

    async def event_snapshot(
        self, event_id: str, *, crop: bool = False, height: int | None = None
    ) -> bytes:
        """Best snapshot of one tracked object (needs `snapshots: enabled` in Frigate)."""
        params: dict[str, Any] = {}
        if crop:
            params["crop"] = 1
        if height is not None:
            params["height"] = height
        response = await self._get(
            f"/events/{quote(event_id, safe='')}/snapshot.jpg", params=params
        )
        return response.content

    async def recording_frame(self, camera: str, at: float, *, height: int | None = None) -> bytes:
        """One frame from the recordings at `at` (needs `record: enabled` in Frigate)."""
        params = {"height": height} if height is not None else None
        response = await self._get(
            f"/{quote(camera, safe='')}/recordings/{at}/snapshot.jpg", params=params
        )
        return response.content

    async def incident_frames(
        self, incident: Incident, *, count: int = 4, height: int | None = 480
    ) -> list[Frame]:
        """Frames spread over an incident: recording frames first, event snapshots as the
        fallback when the camera does not record (or the recording is gone)."""
        frames: list[Frame] = []
        for at in frame_times(incident.started_at, incident.ended_at, count):
            try:
                jpeg = await self.recording_frame(incident.camera, at, height=height)
            except FrigateNotFoundError:
                continue
            frames.append(Frame(at=at, jpeg=jpeg, origin="recording"))
        if frames:
            return frames
        for event_id in incident.detections[:count]:
            try:
                jpeg = await self.event_snapshot(event_id, height=height)
            except FrigateNotFoundError:
                continue
            frames.append(Frame(at=incident.started_at, jpeg=jpeg, origin="event_snapshot"))
        return frames

    async def download_clip(self, camera: str, start: float, end: float, dest: Path) -> Path:
        """Stream a recording clip to `dest` (written atomically via a temporary file)."""
        path = f"/{quote(camera, safe='')}/start/{start}/end/{end}/clip.mp4"
        url = f"{self.base_url}/api{path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        try:
            async with self._http.stream("GET", path, timeout=CLIP_TIMEOUT_S) as response:
                if response.status_code == 404:
                    raise FrigateNotFoundError(f"Frigate has no recording for {path} (404)")
                if response.is_error:
                    raise FrigateError(f"Frigate answered {response.status_code} for {url}")
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            raise FrigateError(f"clip download from {url} failed ({exc})") from exc
        partial.replace(dest)
        return dest
