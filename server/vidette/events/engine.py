"""Event engine: Tier-2 geometry in, confirmed/dismissed events out (M2 skeleton).

Per camera the engine owns an `IouTracker` built from the camera's zones. Every detection
batch runs the tracker, then the promotion rules from `CascadeSpec` — scaled by each
matching policy's sensitivity preset — decide whether a track becomes an event:

- **suppression first** (the passers-by rule, docs/architecture/ai-pipeline.md): a track
  whose zones are only `public` — or empty while the camera has a public zone at all —
  never promotes, no matter what else fires;
- a promotion that satisfies a matching policy → **confirmed**: persisted, snapshot saved
  (best effort, retried while the event stays open), the canonical payload published as
  ``event.confirmed``;
- a promotion no policy wants → **dismissed**: persisted, searchable, silent.

One open event per camera at a time; further promotions extend it (kinds/zones union,
geometry maxima). An open event closes once its tracks have been gone for
`CLOSE_AFTER_ABSENT_S`; confirmed ones then get their footage upgraded to the ``event``
retention class (docs/architecture/storage.md) and publish ``event.ended``.

Crash containment: recording is sacred — every public entry point catches and logs;
nothing here may raise into the pipeline. Policy evaluation is the M2 *geometric
skeleton*: plain-language interpretation is M4, sensitivity presets stand in until then.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vidette.core.config import PolicyConfig, Sensitivity, VidetteConfig, ZoneKind
from vidette.core.events import Event, EventState, GeometryFacts, InProcessEventBus
from vidette.db import Database
from vidette.pipeline.base import CascadeSpec, Detection, MotionRegion, TrackState
from vidette.pipeline.track import IouTracker

logger = logging.getLogger(__name__)

CLOSE_AFTER_ABSENT_S = 10.0

# A confirmed event's promotion can race the stream gateway's warmup: the first snapshot
# then fails and, without retries, the event would stay snapshotless forever (observed
# live). While the event is open, missing snapshots are retried on every detection/tick
# pass — at most every SNAPSHOT_RETRY_AFTER_S seconds, SNAPSHOT_MAX_ATTEMPTS total tries.
SNAPSHOT_RETRY_AFTER_S = 5.0
SNAPSHOT_MAX_ATTEMPTS = 5

# Pre/post-roll seconds of footage pulled along when an event's segments change retention
# class (close → `event`; the events API reuses this pad when starring → `favorite`).
EVENT_FOOTAGE_PAD_S = 5.0

_TARGET_KINDS = (ZoneKind.entry, ZoneKind.object)

DEFAULT_POLICY = PolicyConfig(
    name="default",
    description="built-in policy applied when none are configured — balanced sensitivity",
)


# --- pure promotion rules ------------------------------------------------------------------


def is_suppressed(
    track: TrackState, zone_kinds: Mapping[str, ZoneKind], has_public_zone: bool
) -> bool:
    """The passers-by rule: only-public tracks never promote.

    A track in no zone at all is treated as public passage *when the camera has a public
    zone configured* — un-zoned street around the marked sidewalk is still the street.
    """
    if not track.zones:
        return has_public_zone
    return all(zone_kinds.get(name) is ZoneKind.public for name in track.zones)


def promotion_reason(
    track: TrackState,
    zone_kinds: Mapping[str, ZoneKind],
    spec: CascadeSpec,
    sensitivity: Sensitivity,
) -> str | None:
    """Why (if at all) this track promotes under `spec` at the given sensitivity.

    relaxed  — only touch, or entry/object presence combined with dwell;
    balanced — the CascadeSpec defaults;
    paranoid — balanced with the dwell threshold halved.
    """
    in_target = any(zone_kinds.get(name) in _TARGET_KINDS for name in track.zones)
    if sensitivity is Sensitivity.relaxed:
        if spec.promote_on_touch and track.touch:
            return "touch"
        if spec.promote_on_entry_zone and in_target and track.dwell_s > spec.promote_dwell_s:
            return "entry_dwell"
        return None
    dwell_threshold = spec.promote_dwell_s * (0.5 if sensitivity is Sensitivity.paranoid else 1.0)
    if spec.promote_on_touch and track.touch:
        return "touch"
    if spec.promote_on_entry_zone and in_target:
        return "entry_zone"
    if track.dwell_s > dwell_threshold:
        return "dwell"
    if spec.promote_on_loiter and track.loiter:
        return "loiter"
    if spec.promote_on_repeat_pass and track.repeat_pass >= spec.promote_on_repeat_pass:
        return "repeat_pass"
    return None


# --- canonical payload -----------------------------------------------------------------------


def _iso_utc(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_payload(event: Event, topic: str) -> dict[str, Any]:
    """The canonical event shape shared by API/WebSocket/webhooks/MQTT (docs/events-…md)."""
    return {
        "event": topic,
        "id": event.id,
        "camera": event.camera,
        "started_at": _iso_utc(event.started_at),
        "ended_at": _iso_utc(event.ended_at) if event.ended_at is not None else None,
        "kinds": list(event.kinds),
        "zones": list(event.zones),
        "geometry": {
            "approach": event.geometry.approach,
            "dwell_s": event.geometry.dwell_s,
            "touch": event.geometry.touch,
            "loiter": event.geometry.loiter,
            "repeat_pass": event.geometry.repeat_pass,
        },
        "summary": None,  # Tier 3 (M3) fills these in; geometry alerts stand on their own
        "intent": None,
        "policy": event.policy,
        "media": {
            "snapshot": (
                f"/api/v1/events/{event.id}/snapshot.jpeg" if event.media.snapshot_path else None
            ),
            "clip": f"/api/v1/events/{event.id}/clip.mp4",
        },
    }


# --- engine ----------------------------------------------------------------------------------


@dataclass
class _OpenEvent:
    event: Event
    track_ids: set[int]
    last_seen_ts: float
    snapshot_attempts: int = 0
    snapshot_last_attempt_ts: float = 0.0


@dataclass
class _CameraState:
    tracker: IouTracker
    zone_kinds: dict[str, ZoneKind]
    has_public: bool
    open_event: _OpenEvent | None = None


class EventEngine:
    """Owns the Tier-2 trackers and the T2→event promotion/policy step.

    Wire-up (integration): the pipeline supervisor calls `on_detections` per analyzed
    frame and `tick` on its idle timer; `snapshot_fn` is `gateway.snapshot`.
    """

    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        bus: InProcessEventBus,
        *,
        snapshot_fn: Callable[[str], Awaitable[bytes]],
        media_dir: Path,
        spec: CascadeSpec | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._bus = bus
        self._snapshot_fn = snapshot_fn
        self._media_dir = media_dir
        self._spec = spec if spec is not None else CascadeSpec()
        self._clock = clock or time.time
        self._cameras: dict[str, _CameraState] = {}
        self._unknown_warned: set[str] = set()

    async def on_detections(
        self,
        camera_id: str,
        ts: float,
        detections: list[Detection],
        motion_regions: Sequence[MotionRegion] | None = None,
    ) -> None:
        """Run tracking + promotion for one detection batch. Never raises."""
        del motion_regions  # reserved for best-shot selection (Tier 3, M3)
        try:
            state = self._camera_state(camera_id)
            if state is None:
                return
            tracks = state.tracker.update(ts, detections)

            open_event = state.open_event
            if open_event is not None and any(
                track.track_id in open_event.track_ids for track in tracks
            ):
                open_event.last_seen_ts = ts

            policies = self._policies_for(camera_id)
            for track in tracks:
                if is_suppressed(track, state.zone_kinds, state.has_public):
                    continue
                confirmed_policy: str | None = None
                for policy in policies:
                    if promotion_reason(track, state.zone_kinds, self._spec, policy.sensitivity):
                        confirmed_policy = policy.name
                        break
                if confirmed_policy is not None:
                    await self._promote(camera_id, state, ts, track, policy=confirmed_policy)
                elif promotion_reason(track, state.zone_kinds, self._spec, Sensitivity.balanced):
                    # Promoted on the geometry, wanted by no policy → kept, searchable, silent.
                    await self._promote(camera_id, state, ts, track, policy=None)
            await self._retry_missing_snapshots(ts)
            await self._close_absent(ts)
        except Exception:
            logger.exception(
                "event engine failed on detections for camera '%s' — pipeline continues",
                camera_id,
            )

    async def tick(self, ts: float) -> None:
        """Retry missing snapshots + close events whose tracks are gone. Never raises."""
        try:
            await self._retry_missing_snapshots(ts)
            await self._close_absent(ts)
        except Exception:
            logger.exception("event engine tick failed — pipeline continues")

    # --- internals ---------------------------------------------------------------------------

    def _camera_state(self, camera_id: str) -> _CameraState | None:
        state = self._cameras.get(camera_id)
        if state is not None:
            return state
        camera = self._config.cameras.get(camera_id)
        if camera is None:
            if camera_id not in self._unknown_warned:
                self._unknown_warned.add(camera_id)
                logger.warning(
                    "event engine: ignoring detections for unconfigured camera '%s'", camera_id
                )
            return None
        zone_kinds = {name: zone.kind for name, zone in camera.zones.items()}
        state = _CameraState(
            tracker=IouTracker(camera.zones, clock=self._clock),
            zone_kinds=zone_kinds,
            has_public=any(kind is ZoneKind.public for kind in zone_kinds.values()),
        )
        self._cameras[camera_id] = state
        return state

    def _policies_for(self, camera_id: str) -> list[PolicyConfig]:
        if not self._config.policies:
            return [DEFAULT_POLICY]
        return [
            policy
            for policy in self._config.policies
            if policy.cameras == "all" or camera_id in policy.cameras
        ]

    async def _promote(
        self,
        camera_id: str,
        state: _CameraState,
        ts: float,
        track: TrackState,
        *,
        policy: str | None,
    ) -> None:
        confirmed = policy is not None
        open_event = state.open_event
        if open_event is None:
            event = Event(
                camera=camera_id,
                started_at=datetime.fromtimestamp(ts, tz=UTC),
                state=EventState.confirmed if confirmed else EventState.dismissed,
                kinds=[track.label],
                zones=list(track.zones),
                geometry=_facts(track),
                policy=policy,
            )
            opened = _OpenEvent(event=event, track_ids={track.track_id}, last_seen_ts=ts)
            state.open_event = opened
            await self._db.insert_event(
                event.id,
                event.camera,
                event.started_at.timestamp(),
                event.state.value,
                list(event.kinds),
                list(event.zones),
                event.geometry.model_dump(),
                policy=event.policy,
            )
            if confirmed:
                await self._attach_snapshot(opened, ts)
                await self._bus.publish(
                    "event.confirmed", canonical_payload(event, "event.confirmed")
                )
            return

        # Extend the open event: union kinds/zones, keep the geometry maxima. (The DB row
        # keeps its promotion-time facts — `update_event` is deliberately partial at M2.)
        event = open_event.event
        if track.label not in event.kinds:
            event.kinds.append(track.label)
        for zone in track.zones:
            if zone not in event.zones:
                event.zones.append(zone)
        event.geometry = _merge_geometry(event.geometry, track)
        open_event.track_ids.add(track.track_id)
        open_event.last_seen_ts = ts

        if confirmed and event.state is not EventState.confirmed:
            event.state = EventState.confirmed
            event.policy = policy
            await self._db.update_event(event.id, state=EventState.confirmed.value)
            await self._attach_snapshot(open_event, ts)
            await self._bus.publish("event.confirmed", canonical_payload(event, "event.confirmed"))

    async def _attach_snapshot(self, open_event: _OpenEvent, ts: float) -> None:
        """Best effort: a missing snapshot must never block or kill the event.

        Each call counts as one attempt; failures are retried from
        `_retry_missing_snapshots` while the event stays open.
        """
        event = open_event.event
        open_event.snapshot_attempts += 1
        open_event.snapshot_last_attempt_ts = ts
        try:
            data = await self._snapshot_fn(event.camera)
            path = self._media_dir / event.camera / "events" / event.id / "snapshot.jpeg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            event.media.snapshot_path = str(path)
            await self._db.update_event(event.id, snapshot_path=str(path))
        except Exception as exc:
            logger.warning(
                "snapshot for event %s (camera '%s') failed (attempt %d/%d): %s — event "
                "proceeds without it",
                event.id,
                event.camera,
                open_event.snapshot_attempts,
                SNAPSHOT_MAX_ATTEMPTS,
                exc,
            )

    async def _retry_missing_snapshots(self, ts: float) -> None:
        """Re-attempt failed snapshots of open confirmed events (gateway warmup race).

        Runs on every detection/tick pass; throttled to one attempt per
        `SNAPSHOT_RETRY_AFTER_S`, capped at `SNAPSHOT_MAX_ATTEMPTS` total. Failures stay
        contained in `_attach_snapshot`.
        """
        for state in self._cameras.values():
            open_event = state.open_event
            if open_event is None:
                continue
            event = open_event.event
            if (
                event.state is not EventState.confirmed
                or event.media.snapshot_path is not None
                or open_event.snapshot_attempts >= SNAPSHOT_MAX_ATTEMPTS
                or ts - open_event.snapshot_last_attempt_ts < SNAPSHOT_RETRY_AFTER_S
            ):
                continue
            await self._attach_snapshot(open_event, ts)

    async def _close_absent(self, ts: float) -> None:
        """Close events whose tracks are gone; confirmed ones keep their footage.

        Only *confirmed* events get their segments upgraded to the ``event`` retention
        class (± `EVENT_FOOTAGE_PAD_S` of pre/post-roll). Dismissed events are deliberately
        excluded: they stay persisted and searchable, but their footage ages out on the
        continuous/motion schedule — "kept, silent" must not cost 90 days of disk.
        """
        for state in self._cameras.values():
            open_event = state.open_event
            if open_event is None or ts - open_event.last_seen_ts <= CLOSE_AFTER_ABSENT_S:
                continue
            state.open_event = None
            event = open_event.event
            event.ended_at = datetime.fromtimestamp(ts, tz=UTC)
            await self._db.update_event(event.id, ended_at=ts)
            if event.state is EventState.confirmed:
                try:
                    await self._db.upgrade_segments_class(
                        event.camera,
                        event.started_at.timestamp() - EVENT_FOOTAGE_PAD_S,
                        ts + EVENT_FOOTAGE_PAD_S,
                        "event",
                    )
                except Exception:
                    # Notification delivery outranks bookkeeping: still publish the end.
                    logger.exception(
                        "retention upgrade for event %s (camera '%s') failed — its footage "
                        "stays on the continuous/motion schedule",
                        event.id,
                        event.camera,
                    )
                await self._bus.publish("event.ended", canonical_payload(event, "event.ended"))


def _facts(track: TrackState) -> GeometryFacts:
    return GeometryFacts(
        approach=track.approach,
        dwell_s=track.dwell_s,
        touch=track.touch,
        loiter=track.loiter,
        repeat_pass=track.repeat_pass,
    )


def _merge_geometry(current: GeometryFacts, track: TrackState) -> GeometryFacts:
    approaches = [a for a in (current.approach, track.approach) if a is not None]
    dwells = [d for d in (current.dwell_s, track.dwell_s) if d is not None]
    return GeometryFacts(
        approach=max(approaches) if approaches else None,
        dwell_s=max(dwells) if dwells else None,
        touch=current.touch or track.touch,
        loiter=current.loiter or track.loiter,
        repeat_pass=max(current.repeat_pass, track.repeat_pass),
    )
