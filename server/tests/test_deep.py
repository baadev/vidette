"""Deep analysis (ADR-0019): the shared slot, the queue, look-then-decide, live vs deep,
review drafts and the digest. A fake OpenAI-compatible server plays the model and its
`/slots`; no model, no network, no footage."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes.openai_api import FakeVlmApi
from test_clipfolder import collected_clip

from vidette.core.config import VidetteConfig, VlmBackend
from vidette.db import Database, EventRow
from vidette.deep.analyst import DeepAnalyst
from vidette.deep.compare import Side, compare, worst
from vidette.deep.worker import P_ALERT, P_PERSON, P_UNJUDGED, P_VEHICLE, DeepWorker, classify
from vidette.eval.dataset import load_dataset
from vidette.vlm.budget import MemoryLedger
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import endpoint_for
from vidette.vlm.gate import PREEMPTED, SlotGate, slot_state, was_preempted

LEVELS: dict[str, Any] = {
    "critical": {"wake": "always", "scenarios": {"door_or_gate_attempt": "tries the gate"}},
    "elevated": {"wake": "unless_known", "scenarios": {"person_at_house": "at the gate"}},
    "none": {
        "wake": "never",
        "scenarios": {"passer_by": "walks past", "car_passing": "a car drives by"},
    },
}


# --- the shared slot -------------------------------------------------------------------


async def test_a_live_call_takes_the_slot_back_from_a_background_one() -> None:
    gate = SlotGate()
    started = asyncio.Event()

    async def background() -> None:
        async with gate.background(poll_s=0.01):
            started.set()
            await asyncio.sleep(10)

    task: asyncio.Task[None] = asyncio.create_task(background())
    await started.wait()
    async with gate.live():
        assert gate.busy
    await asyncio.wait({task})
    assert was_preempted(task)  # type: ignore[arg-type]
    assert gate.preemptions == 1 and gate.live_calls == 1 and not gate.busy


async def test_a_background_call_waits_for_live_ones_and_shutdown_is_not_preemption() -> None:
    gate = SlotGate()
    order: list[str] = []

    async def live() -> None:
        async with gate.live():
            await asyncio.sleep(0.05)
            order.append("live")

    async def background() -> None:
        async with gate.background(poll_s=0.01):
            order.append("background")

    first = asyncio.create_task(live())
    await asyncio.sleep(0)
    await asyncio.gather(background(), first)
    assert order == ["live", "background"]

    async def forever() -> None:
        async with gate.background(poll_s=0.01):
            await asyncio.sleep(10)

    task: asyncio.Task[None] = asyncio.create_task(forever())
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled() and not was_preempted(task)  # type: ignore[arg-type]
    assert PREEMPTED


async def test_slot_state_reads_llama_cpp_slots_with_the_key() -> None:
    fake = FakeVlmApi(api_key="k3y", slots=[True, False, True])
    config = VidetteConfig.model_validate(
        {
            "understanding": {
                "vlm": {"local": {"base_url": "http://llama.test/v1", "api_key": "k3y"}}
            }
        }
    )
    endpoint = endpoint_for(config, VlmBackend.local)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        state = await slot_state(endpoint, http)
        assert state is not None and (state.total, state.busy, state.idle) == (3, 2, 1)
        fake.slots = None
        assert await slot_state(endpoint, http) is None  # Ollama, a cloud API: unknown


# --- deciding what to look at and how to compare --------------------------------------


def row(
    state: str, verdicts: list[dict[str, Any]], people: dict[str, Any] | None = None
) -> EventRow:
    intent: dict[str, Any] = {"verdicts": verdicts} if verdicts else {}
    if people is not None:
        intent["people"] = people
    return EventRow(
        id="e",
        camera="street",
        started_at=0,
        ended_at=1,
        state=state,
        kinds=[],
        zones=[],
        geometry={},
        summary=None,
        intent=intent or None,
        policy=None,
        feedback=None,
        snapshot_path=None,
        clip_path=None,
    )


def test_classify_puts_alerts_first_and_skips_the_empty_street() -> None:
    assert classify(row("confirmed", [{"alert": True}]), include_unjudged=True) == (
        P_ALERT,
        "alert",
    )
    person = {"label": "passer_by", "persons": 1, "alert": False}
    assert classify(row("dismissed", [person]), include_unjudged=True) == (P_PERSON, "person")
    car = {"label": "car_passing", "persons": 0, "alert": False}
    assert classify(row("dismissed", [car]), include_unjudged=True) == (P_VEHICLE, "vehicle")
    animal = {"label": "animal", "persons": 0, "alert": False}
    assert classify(row("dismissed", [animal]), include_unjudged=True) is None
    assert classify(row("observed", []), include_unjudged=True) == (P_UNJUDGED, "never judged")
    assert classify(row("observed", []), include_unjudged=False) is None


def test_compare_names_where_the_judges_part() -> None:
    live = Side(True, False, "none", "passer_by", 0.9)
    assert compare(live, Side(True, False, "none", "car_passing", 0.8), min_confidence=0.6)[0] == (
        "agree"
    )
    assert (
        compare(live, Side(True, True, "elevated", "person_at_house", 0.8), min_confidence=0.6)[0]
        == "disputed"
    )
    assert compare(live, Side(True, False, "none", "passer_by", 0.4), min_confidence=0.6)[0] == (
        "uncertain"
    )
    assert compare(live, Side(False, False, None, None, None), min_confidence=0.6)[0] == (
        "uncertain"
    )
    nothing = Side(False, False, None, None, None)
    assert compare(nothing, Side(True, True, "critical", "x", 0.9), min_confidence=0.6)[0] == (
        "disputed"
    )
    assert compare(nothing, Side(True, False, "none", "x", 0.9), min_confidence=0.6)[0] == (
        "only_deep"
    )
    assert worst(["agree", "uncertain", "disputed"]) == "disputed"


def test_the_window_wraps_midnight_and_is_validated() -> None:
    deep = VidetteConfig.model_validate(
        {"understanding": {"deep": {"window": "22:00-06:00"}}}
    ).understanding.deep
    assert deep.in_window(23 * 60) and deep.in_window(5 * 60) and not deep.in_window(12 * 60)
    with pytest.raises(ValueError, match="window"):
        VidetteConfig.model_validate({"understanding": {"deep": {"window": "1-7"}}})
    with pytest.raises(ValueError, match="cloud"):
        VidetteConfig.model_validate(
            {"understanding": {"deep": {"enabled": True, "backend": "cloud"}}}
        )


# --- the worker, end to end on a fake model --------------------------------------------


def answer(text: str, images: list[bytes], body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("response_format", {}).get("json_schema", {}).get("name")
    if name == "deep_look":
        assert images and "Policy" not in text  # look: frames, no policy
        return {
            "observations": "A person walks to the gate and stands there.",
            "persons": 1,
            "vehicles": 0,
            "subject_frames": [1, 2],
        }
    assert not images and "A person walks to the gate" in text  # decide: words, no frames
    return {
        "scenario": "person_at_house",
        "description": "Человек подошёл к калитке и ждал.",
        "persons": 1,
        "first_person_frame": 1,
        "subject_frames": [1],
        "confidence": 0.8,
    }


def deep_config(tmp_path: Path, **deep: Any) -> VidetteConfig:
    dataset = tmp_path / "street"
    dataset.mkdir(exist_ok=True)
    return VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "clip_folders": [{"path": str(dataset), "camera": "street"}],
            "understanding": {
                "vlm": {
                    "backend": "local",
                    "language": "Russian",
                    "local": {"base_url": "http://llama.test/v1", "model": "fake-vlm"},
                },
                "deep": {"enabled": True, "window": "00:00-00:00", **deep},
            },
            "policies": [{"name": "home", "description": "A house.", "levels": LEVELS}],
        }
    )


async def make_worker(
    tmp_path: Path, fake: FakeVlmApi, clock: list[float], **deep: Any
) -> tuple[DeepWorker, Database, SlotGate, list[float]]:
    config = deep_config(tmp_path, **deep)
    db = Database(config.storage.database)
    await db.connect()
    http = httpx.AsyncClient(transport=fake.transport())
    endpoint = endpoint_for(config, VlmBackend.local)
    client = VlmClient(endpoint, ledger=MemoryLedger(), http=http)
    gate = SlotGate()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        fake.slots = [False, False]  # the server frees up while we wait

    async def emit(_kind: str, _payload: dict[str, Any]) -> None:
        return None

    worker = DeepWorker(
        config,
        db,
        DeepAnalyst(client, frames=4, frames_per_call=4, language="Russian"),
        gate,
        media_dir=config.storage.media_dir,
        emit=emit,
        endpoint=endpoint,
        http=http,
        clock=lambda: clock[0],
        sleep=sleep,
    )
    return worker, db, gate, slept


async def insert(
    db: Database,
    root: Path,
    clip_id: str,
    *,
    started: float,
    verdict: dict[str, Any] | None,
    state: str,
) -> str:
    clip = collected_clip(
        root, clip_id, camera="street", started=datetime.fromtimestamp(started).astimezone()
    )
    intent = {"verdicts": [verdict]} if verdict else None
    await db.insert_event(
        f"ev-{clip_id}",
        "street",
        started,
        state,
        [],
        [],
        {},
        ended_at=started + 8,
        intent=intent,
        clip_path=str(clip),
        source="clip-folder",
        source_ref=str(clip),
    )
    return f"ev-{clip_id}"


PASSER = {
    "policy": "home",
    "alert": False,
    "model_said_alert": False,
    "abstained": False,
    "label": "passer_by",
    "level": "none",
    "persons": 1,
    "confidence": 0.9,
}


async def test_a_disputed_clip_gets_a_deep_verdict_a_review_draft_and_a_digest_line(
    tmp_path: Path,
) -> None:
    fake = FakeVlmApi(answer=answer, slots=[True, True])  # busy at first: back off once
    now = time.time()
    clock = [now]
    worker, db, _gate, slept = await make_worker(tmp_path, fake, clock)
    event_id = await insert(
        db,
        tmp_path / "street",
        "20261114-210304",
        started=now - 7200,
        verdict=PASSER,
        state="dismissed",
    )
    car = await insert(
        db,
        tmp_path / "street",
        "20261114-210400",
        started=now - 7100,
        verdict={**PASSER, "label": "animal", "persons": 0},
        state="dismissed",
    )

    assert await worker.refill(force=True) == 1  # the animal clip is not worth a look
    assert await worker.step() == 0.0
    stored = await db.get_event(event_id)
    assert stored is not None and stored.deep is not None
    assert stored.intent == {"verdicts": [PASSER]}  # the live verdict is untouched
    deep = stored.deep
    assert deep["agreement"] == "disputed" and deep["verdicts"][0]["label"] == "person_at_house"
    assert deep["description"] == "Человек подошёл к калитке и ждал."
    assert deep["looks"][0]["observations"].startswith("A person walks")
    assert deep["review"] == {"dataset": "street", "number": 1}
    assert slept and slept[0] == 15.0 and worker.status.backoffs == 1

    [job] = await db.deep_jobs(state="done")
    assert job.event_id == event_id and job.stats["slots"] == "0/2"
    assert [c["kind"] for c in job.stats["calls"]] == ["look", "decide"]

    entry = load_dataset(tmp_path / "street" / "labels.yaml", include_drafts=True)
    [draft] = [e for e in entry.labels.entries if e.status == "draft"]
    assert draft.draft is not None and draft.draft.scenario == "person_at_house"
    assert draft.notes is not None and "live passer_by" in draft.notes

    assert await worker.refill(force=True) == 0  # idempotent: once per event
    assert await db.get_event(car) is not None
    await db.close()


async def test_the_daily_cap_and_the_window_hold_the_queue(tmp_path: Path) -> None:
    fake = FakeVlmApi(answer=answer, slots=[False])
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    clock = [noon]
    worker, db, _gate, _slept = await make_worker(
        tmp_path, fake, clock, window="01:00-07:00", outside_window="off", max_clips_per_day=1
    )
    await insert(
        db,
        tmp_path / "street",
        "20261114-110000",
        started=noon - 7200,
        verdict=PASSER,
        state="dismissed",
    )
    wait = await worker.step()
    assert wait > 0 and worker.status.waiting_for == "window 01:00-07:00"
    assert fake.requests == []

    worker.deep.outside_window = "trickle"
    assert await worker.step() == 0.0  # one clip now
    await insert(
        db,
        tmp_path / "street",
        "20261114-110100",
        started=noon - 7100,
        verdict=PASSER,
        state="dismissed",
    )
    worker._last_refill = 0
    assert await worker.step() > 0 and worker.status.waiting_for.startswith("daily cap")
    await db.close()


async def test_a_live_call_puts_the_deep_job_back_with_its_looks(tmp_path: Path) -> None:
    fake = FakeVlmApi(answer=answer, slots=[False], latency_s=0.3)
    now = time.time()
    worker, db, gate, _slept = await make_worker(tmp_path, fake, [now])
    event_id = await insert(
        db,
        tmp_path / "street",
        "20261114-210304",
        started=now - 7200,
        verdict=PASSER,
        state="dismissed",
    )
    await worker.refill(force=True)
    job = await db.deep_next(since_ts=0, max_attempts=3)
    assert job is not None
    run = asyncio.create_task(worker.run_job(job))
    while not gate.busy:
        await asyncio.sleep(0.01)
    async with gate.live():  # the live judge needs the slot now
        pass
    await run
    [queued] = await db.deep_jobs(state="queued")
    assert queued.event_id == event_id and queued.stats["preempted"] == 1
    assert queued.attempts == 0 and worker.status.preempted == 1
    stored = await db.get_event(event_id)
    assert stored is not None and stored.deep is None
    await db.close()


async def test_running_jobs_are_queued_again_after_a_restart(tmp_path: Path) -> None:
    config = deep_config(tmp_path)
    db = Database(config.storage.database)
    await db.connect()
    await db.deep_enqueue([("e1", "street", time.time(), 3, "person")])
    await db.deep_update("e1", state="running", taken_at=time.time())
    assert await db.deep_requeue_running() == 1
    assert (await db.deep_counts()) == {"queued": 1}
    await db.deep_enqueue([("e1", "street", time.time(), 0, "alert")])  # better priority wins
    [job] = await db.deep_jobs()
    assert (job.priority, job.reason) == (0, "alert")
    assert json.dumps(job.stats) == "{}"
    await db.close()


async def test_a_daytime_turn_is_not_cancelled_and_live_calls_wait_for_it(
    tmp_path: Path,
) -> None:
    gate = SlotGate()
    order: list[str] = []

    async def turn() -> None:
        async with gate.background(preemptible=False):
            await asyncio.sleep(0.05)
            order.append("turn")

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.01)
    async with gate.live():
        order.append("live")
    await task
    assert order == ["turn", "live"] and gate.preemptions == 0


async def test_outside_the_window_the_worker_takes_a_turn_while_the_live_judge_works(
    tmp_path: Path,
) -> None:
    fake = FakeVlmApi(answer=answer, slots=[False])
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    worker, db, _gate, _slept = await make_worker(
        tmp_path, fake, [noon], window="01:00-07:00", trickle_every="10m"
    )
    worker._live_busy = lambda: True  # a busy day: the live judge never rests
    await insert(
        db,
        tmp_path / "street",
        "20261114-110000",
        started=noon - 7200,
        verdict=PASSER,
        state="dismissed",
    )
    assert await worker.step() == 0.0
    [job] = await db.deep_jobs(state="done")
    assert job.stats["mode"] == "trickle"
    assert await worker.step() > 0  # the next one waits for the trickle
    await db.close()
