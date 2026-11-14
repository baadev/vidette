"""EventIntake: Frigate incidents end up as one observed event row each.

End to end without a broker: a recorded Frigate session is replayed through the real
FrigateSource onto a real bus, the intake writes to a real (tmp) SQLite store.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conftest import FIXTURES

from vidette.core.config import FrigateConfig
from vidette.core.events import InProcessEventBus, Message
from vidette.core.incidents import Incident, as_payload
from vidette.db import Database
from vidette.events.intake import SNAPSHOT_MAX_ATTEMPTS, EventIntake
from vidette.frigate.source import FrigateSource, replay

SESSION = FIXTURES / "frigate" / "session-front-door.jsonl"
JPEG = (FIXTURES / "frigate" / "api" / "snapshot.jpg").read_bytes()


class FakeSnapshots:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def event_snapshot(
        self, event_id: str, *, crop: bool = False, height: int | None = None
    ) -> bytes:
        self.calls.append(event_id)
        if len(self.calls) <= self.failures:
            raise RuntimeError("no snapshot yet")
        return JPEG


async def noop_emit(kind: str, payload: dict[str, Any]) -> None:
    return None


async def wait_for(condition: Callable[[], object], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


def collect(bus: InProcessEventBus, pattern: str) -> list[Message]:
    subscription = bus.subscribe(pattern)
    out: list[Message] = []
    while not subscription._queue.empty():
        out.append(subscription._queue.get_nowait())
    return out


async def test_recorded_frigate_session_becomes_one_observed_event(
    db: Database, media_dir: Path
) -> None:
    bus = InProcessEventBus()
    observed = bus.subscribe("event.observed")
    snapshots = FakeSnapshots(failures=1)  # Frigate has no snapshot at the very start
    intake = EventIntake(db, bus, media_dir=media_dir, snapshots=snapshots)
    source = FrigateSource(
        FrigateConfig.model_validate({"url": "http://frigate:5000", "mqtt": {"host": "b"}}),
        bus,
        emit=noop_emit,
    )
    intake.start()
    try:
        await replay(source, SESSION)
        await wait_for(lambda: observed._queue.qsize() == 4)
    finally:
        await intake.stop()

    [row] = await db.list_events()
    assert row.state == "observed"
    assert row.source == "frigate"
    assert row.source_ref == "1763107200.802000-m2p7xd"
    assert row.camera == "front_door"
    assert row.kinds == ["person"]
    assert row.zones == ["path", "porch"]
    assert row.ended_at is not None
    assert row.upstream is not None
    assert row.upstream["severity"] == "alert"
    assert row.upstream["summary"]["threat_level"] == 1
    assert row.snapshot_path is not None
    assert Path(row.snapshot_path).read_bytes() == JPEG
    assert Path(row.snapshot_path).is_relative_to(media_dir)
    assert snapshots.calls == ["1763107200.412000-k3v9qa"] * 2  # one failure, one success
    assert intake.observed_total == 1

    topics = [observed._queue.get_nowait() for _ in range(4)]
    assert {payload["id"] for _, payload in topics} == {row.id}
    last = topics[-1][1]
    assert last["state"] == "observed"
    assert last["media"]["snapshot"] == f"/api/v1/events/{row.id}/snapshot.jpeg"
    assert last["media"]["clip"] == f"/api/v1/events/{row.id}/clip.mp4"


def incident(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "frigate",
        "source_id": "r1",
        "camera": "porch",
        "started_at": 100.0,
        "objects": ["person"],
        "detections": ["e1"],
    }
    base.update(overrides)
    return as_payload(Incident.model_validate(base))


async def test_a_later_phase_never_erases_the_upstream_summary(
    db: Database, media_dir: Path
) -> None:
    intake = EventIntake(db, InProcessEventBus(), media_dir=media_dir)
    summary = {"title": "t", "text": "x", "threat_level": 0, "confidence": 0.9, "concerns": []}
    await intake.handle("incident.summarized", incident(upstream_summary=summary))
    await intake.handle("incident.updated", incident(objects=["person", "dog"]))
    [row] = await db.list_events()
    assert row.kinds == ["person", "dog"]
    assert row.upstream is not None and row.upstream["summary"]["text"] == "x"


async def test_snapshot_attempts_are_capped(db: Database, media_dir: Path) -> None:
    snapshots = FakeSnapshots(failures=99)
    intake = EventIntake(db, InProcessEventBus(), media_dir=media_dir, snapshots=snapshots)
    for _ in range(SNAPSHOT_MAX_ATTEMPTS + 2):
        await intake.handle("incident.updated", incident())
    assert len(snapshots.calls) == SNAPSHOT_MAX_ATTEMPTS
    [row] = await db.list_events()
    assert row.snapshot_path is None


async def test_unsafe_camera_names_never_become_paths(db: Database, media_dir: Path) -> None:
    intake = EventIntake(db, InProcessEventBus(), media_dir=media_dir, snapshots=FakeSnapshots())
    row = await intake.handle("incident.opened", incident(camera="../../etc"))
    assert row is not None and row.snapshot_path is not None
    assert Path(row.snapshot_path).resolve().is_relative_to(media_dir.resolve())
    assert "unknown-camera" in row.snapshot_path


async def test_bad_payloads_are_ignored_not_fatal(db: Database, media_dir: Path) -> None:
    intake = EventIntake(db, InProcessEventBus(), media_dir=media_dir)
    assert await intake.handle("incident.opened", {"camera": "porch"}) is None
    assert await db.list_events() == []
    assert collect(InProcessEventBus(), "event.*") == []
