"""The pre-filter (docs/prefilter.md, ADR-0021): the rules, the look at a clip, the
worker's queue, deep analysis' use of it, and the replay's checks and simulation."""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from vidette.core.config import TriageConfig, VidetteConfig, VlmBackend
from vidette.core.events import InProcessEventBus
from vidette.core.incidents import Incident
from vidette.db import Database, EventRow
from vidette.deep.worker import P_PERSON, P_UNJUDGED
from vidette.deep.worker import classify as deep_classify
from vidette.events.intake import EventIntake
from vidette.triage.detector import Detection
from vidette.triage.observe import Observation, SeenFrame, observe, pack_cells, unpack_cells
from vidette.triage.replay import Arrival, simulate, stratified_weights
from vidette.triage.rules import (
    RANK_LATER,
    RANK_PEOPLE,
    RANK_SECOND,
    Decision,
    Facts,
    TriageClass,
    UsualMovement,
    classify,
    rank,
)
from vidette.triage.service import Triager
from vidette.verdicts.base import Case, PolicyText, Verdict
from vidette.verdicts.frames import activity_map
from vidette.verdicts.worker import VerdictWorker

SETTINGS = TriageConfig(enabled=True)
PARKED = ["car", 0.9, [0.1, 0.1, 0.2, 0.1]]


def det(label: str, score: float, box: list[float] | None = None) -> list[Any]:
    return [label, score, box or [0.4, 0.4, 0.1, 0.2]]


def seen(*frames: list[list[Any]]) -> list[SeenFrame]:
    return [SeenFrame(src="activity", t=float(i), dets=dets) for i, dets in enumerate(frames)]


def obs(*frames: list[list[Any]], **facts: Any) -> Observation:
    defaults: dict[str, Any] = {
        "detector": "fake", "activity_peak": 0.05, "activity_active": 3, "light": 0.0,
        "cells": pack_cells({100, 101, 102, 103, 104}),
    }  # fmt: skip
    defaults.update(facts)
    return Observation(frames=seen(*frames) if frames else seen([], [], [], []), **defaults)


# --- the rules -------------------------------------------------------------------------------


def test_a_person_or_a_weak_person_candidate_is_judged_now() -> None:
    assert classify(obs([det("person", 0.8)], [], [], []), SETTINGS).reason == "person"
    weak = classify(obs([det("person", 0.25)], [], [], []), SETTINGS)
    assert weak.klass is TriageClass.now and weak.reason == "possible_person"
    noise = classify(obs([det("person", 0.1)], [], [], []), SETTINGS)
    assert noise.klass is TriageClass.later  # below possible_person: something moved
    assert rank(TriageClass.now, "person") == RANK_PEOPLE
    assert rank(TriageClass.now, "possible_person") == rank(TriageClass.now, "vehicle")
    assert rank(TriageClass.now, "possible_person") < rank(TriageClass.later, "motion")
    assert rank(TriageClass.now, "early_possible_person") == rank(TriageClass.now, "vehicle")
    assert rank(TriageClass.now, "early_person") == RANK_PEOPLE


def test_parked_cars_do_not_count_and_a_moving_one_does() -> None:
    parked = classify(obs([PARKED], [PARKED], [PARKED], [PARKED]), SETTINGS)
    assert parked.reason == "motion" and parked.facts.vehicles_still == 1
    driving = [["car", 0.8, [0.1 + 0.15 * i, 0.5, 0.2, 0.1]] for i in range(4)]
    moving = classify(obs(*[[PARKED, d] for d in driving]), SETTINGS)
    assert moving.klass is TriageClass.now and moving.reason == "vehicle"
    later = TriageConfig(enabled=True, vehicles="later")
    assert classify(obs(*[[d] for d in driving]), later).klass is TriageClass.later


def test_animals_wait_and_early_windows_without_anyone_are_skipped() -> None:
    assert classify(obs([det("cat", 0.7)], [], [], []), SETTINGS).klass is TriageClass.later
    early = classify(obs(), SETTINGS, partial=True)
    assert early.klass is TriageClass.skip and early.reason == "early"
    person_early = classify(obs([det("person", 0.9)], [], [], []), SETTINGS, partial=True)
    assert person_early.klass is TriageClass.now


def test_a_lamp_switching_on_while_things_move_is_judged_now() -> None:
    lamp = classify(obs(light=0.3, activity_peak=0.6), SETTINGS)
    assert lamp.klass is TriageClass.now and lamp.reason == "light_motion"
    assert rank(lamp.klass, lamp.reason) == RANK_PEOPLE
    cat = classify(obs([det("cat", 0.8)], [], [], [], light=0.3, activity_peak=0.6), SETTINGS)
    assert cat.reason == "animal", "an animal explains the lamp"


def test_light_change_and_tiny_motion_are_skipped_more_motion_waits() -> None:
    light = classify(obs(light=0.2, activity_peak=0.01), SETTINGS)
    assert light.klass is TriageClass.skip and light.reason == "light"
    tiny = classify(obs(activity_peak=0.002, activity_active=0), SETTINGS)
    assert tiny.klass is TriageClass.skip and tiny.reason == "tiny"
    gate_saw_more = classify(
        obs(activity_peak=0.002, activity_active=0, motion_peak=0.05), SETTINGS
    )
    assert gate_saw_more.klass is TriageClass.later, "the motion gate saw more than the map"
    moved = classify(obs(activity_peak=0.08), SETTINGS)
    assert moved.klass is TriageClass.later and moved.reason == "motion"


def test_nothing_is_skipped_without_a_detector_check() -> None:
    for unchecked in (
        obs(detector=None, activity_peak=0.0),
        obs(error="frames: cannot read", activity_peak=0.0),
        Observation(detector="fake", activity_peak=0.0),  # no frames at all
    ):
        decision = classify(unchecked, SETTINGS)
        assert decision.klass is not TriageClass.skip and decision.reason == "unchecked"
    assert classify(obs(detector=None, activity_peak=0.3), SETTINGS).klass is TriageClass.now
    few = obs([], [], activity_peak=0.0)
    assert classify(few, SETTINGS).klass is TriageClass.later, "two frames are too few"
    no_skips = TriageConfig(enabled=True, skip=False)
    assert classify(obs(activity_peak=0.0), no_skips).klass is TriageClass.later


def test_the_usual_movement_is_learned_from_empty_clips_and_skips_only_inside_it() -> None:
    memory = UsualMovement(minimum=3, keep=10, share=0.5)
    tree = frozenset(range(200, 260))
    assert memory.usual("yard") is None
    for _ in range(3):
        memory.learn("yard", tree)
    usual = memory.usual("yard")
    assert usual == tree and memory.learned("yard") == 3
    in_tree = obs(cells=pack_cells(set(range(210, 240))))
    assert classify(in_tree, SETTINGS, usual=usual).reason == "usual"
    beyond = obs(cells=pack_cells(set(range(210, 240)) | set(range(900, 920))))
    assert classify(beyond, SETTINGS, usual=usual).reason == "motion"
    person_in_tree = obs([det("person", 0.9)], [], [], [], cells=pack_cells(set(range(210, 240))))
    assert classify(person_in_tree, SETTINGS, usual=usual).klass is TriageClass.now
    for _ in range(10):
        memory.learn("yard", frozenset())
    assert memory.usual("yard") == frozenset(), "old clips are forgotten"


def test_cells_pack_small_and_read_back_old_hex() -> None:
    cells = {0, 5, 2303, 1000}
    packed = pack_cells(cells)
    assert unpack_cells(packed) == frozenset(cells) and len(packed) < 80
    hex_bits = bytearray(288)
    hex_bits[0] = 0b100001
    assert unpack_cells(bytes(hex_bits).hex()) == frozenset({0, 5})
    assert unpack_cells("not packed!") == frozenset() and unpack_cells(None) == frozenset()


def test_the_activity_map_tells_a_light_change_from_movement() -> None:
    background = bytes([50]) * 100
    brighter = bytes([90]) * 100
    someone = bytearray(background)
    someone[10:20] = bytes([200]) * 10
    mapped = activity_map([background, background, brighter, bytes(someone), background])
    assert mapped.light > 0.1 and mapped.shares[2] == 0
    assert mapped.peak == pytest.approx(0.1) and mapped.cells == frozenset(range(10, 20))


# --- looking at a real clip ------------------------------------------------------------------


@dataclass
class FakeDetector:
    found: list[Detection] = field(default_factory=list)
    calls: int = 0
    name: str = "fake-detector"

    def detect(self, image: bytes) -> list[Detection]:
        self.calls += 1
        return list(self.found)


async def make_clip(path: Path, source: str, *, vf: str | None = None, seconds: int = 6) -> Path:
    args = ["-vf", vf] if vf else []
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", source,
        "-t", str(seconds), "-g", "5", "-pix_fmt", "yuv420p", *args, str(path),
    )  # fmt: skip
    assert await proc.wait() == 0
    return path


def case_of(clip: Path, *frames: Path) -> Case:
    incident = Incident(
        source="clip-folder", source_id=str(clip), camera="porch", started_at=time.time() - 20,
        ended_at=time.time() - 5, media=[str(clip), *(str(f) for f in frames)],
    )  # fmt: skip
    return Case(incident=incident, media=tuple(Path(m) for m in incident.media))


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@needs_ffmpeg
async def test_a_look_finds_frames_where_it_moved_and_runs_the_detector_on_them(
    tmp_path: Path,
) -> None:
    clip = await make_clip(tmp_path / "moving.mp4", "testsrc2=size=320x180:rate=15")
    key = tmp_path / "key.jpg"
    key.write_bytes((tmp_path / "moving.mp4").read_bytes()[:10])  # unreadable for a model
    person = Detection("person", "person", 0.9, (0.4, 0.3, 0.1, 0.3))
    detector = FakeDetector([person])
    observation = await observe(case_of(clip, key), detector, frames=6)
    assert observation.checked and observation.error is None
    assert [f.src for f in observation.frames].count("activity") == 6
    assert [f.src for f in observation.frames].count("key") == 1 and detector.calls == 7
    assert observation.activity_peak and observation.activity_peak > 0.01
    assert observation.frames[0].detections() == [person]
    assert classify(observation, SETTINGS).reason == "person"


@needs_ffmpeg
async def test_a_still_picture_is_tiny_motion_and_a_light_change_is_light(tmp_path: Path) -> None:
    still = await make_clip(tmp_path / "still.mp4", "color=c=0x606060:s=320x180:r=15")
    quiet = await observe(case_of(still), FakeDetector(), frames=6)
    assert classify(quiet, SETTINGS).reason == "tiny"
    dusk = await make_clip(
        tmp_path / "dusk.mp4", "color=c=0xc0c0c0:s=320x180:r=15", vf="fade=t=out:st=1:d=4"
    )
    light = await observe(case_of(dusk), FakeDetector(), frames=6)
    assert light.light is not None and light.light > 0.1
    assert classify(light, SETTINGS).reason == "light"


@needs_ffmpeg
async def test_a_long_clip_where_nothing_changed_is_looked_at_densely(tmp_path: Path) -> None:
    still = await make_clip(tmp_path / "wake.mp4", "color=c=0x606060:s=320x180:r=15", seconds=40)
    detector = FakeDetector()
    observation = await observe(case_of(still), detector, frames=8)
    dense = [f for f in observation.frames if f.src == "dense"]
    assert observation.dense and len(dense) == 10 and dense[1].t == 4.0
    assert detector.calls == 18 and classify(observation, SETTINGS).reason == "tiny"
    walker = Detection("person", "person", 0.6, (0.8, 0.1, 0.02, 0.06))
    seen_once = FakeDetector([walker])
    busy = await observe(case_of(still), seen_once, frames=8)
    assert not busy.dense, "a person already seen: no dense look"


async def test_a_clip_that_cannot_be_read_is_never_skipped(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    observation = await observe(case_of(broken), FakeDetector(), frames=4)
    assert not observation.checked and observation.error
    assert classify(observation, SETTINGS).klass is TriageClass.now


# --- the worker's queue ----------------------------------------------------------------------


class ScriptedTriager(Triager):
    """Decides by the incident's camera name: cam-now, cam-later, cam-skip."""

    def __init__(self) -> None:
        super().__init__(TriageConfig(enabled=True), None)
        self.looked: list[tuple[str, bool]] = []

    async def decide(
        self, case: Case, *, camera: str, partial: bool = False
    ) -> tuple[Decision, Observation]:
        self.looked.append((case.incident.source_id, partial))
        if camera.endswith("now") or (partial and "person" in case.incident.source_id):
            klass, reason = TriageClass.now, "person"
        elif partial:
            klass, reason = TriageClass.skip, "early"
        else:
            klass = TriageClass(camera.split("-")[1])
            reason = {"later": "motion", "skip": "tiny"}[klass.value]
        decision = Decision(klass, f"scripted {reason}", reason, Facts())
        return decision, Observation(detector="fake", frames=seen([]))


@dataclass
class CountingJudge:
    seen: list[str] = field(default_factory=list)
    name: str = "vlm-local"
    description: str = "counting"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        self.seen.append(case.incident.source_id)
        return Verdict(alert=False, confidence=0.9, rationale="nothing", label="other")


async def _emit(kind: str, payload: dict[str, Any]) -> None:
    return None


async def triage_setup(
    tmp_path: Path, *, idle: bool = True
) -> tuple[VerdictWorker, CountingJudge, ScriptedTriager, Database, EventIntake]:
    cfg = VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "understanding": {"triage": {"enabled": True, "later_max_age": "2m"}},
            "policies": [{"name": "home", "description": "the house"}],
        }
    )
    db = Database(cfg.storage.database)
    await db.connect()
    bus = InProcessEventBus()
    intake = EventIntake(db, bus, media_dir=cfg.storage.media_dir)
    judge = CountingJudge()
    triager = ScriptedTriager()

    async def backend() -> VlmBackend:
        return VlmBackend.local

    worker = VerdictWorker(
        cfg, bus, db, emit=_emit, backend=backend, judges=lambda _b: judge,
        media_dir=cfg.storage.media_dir, triage=triager, slot_idle=lambda _b: idle,
    )  # fmt: skip
    return worker, judge, triager, db, intake


def item(camera: str, *, ended_ago: float = 5.0, partial: bool = False, name: str = "") -> Incident:
    now = time.time()
    return Incident(
        source="clip-folder", source_id=f"clips/{camera}-{name}-{now}.mp4", camera=camera,
        started_at=now - ended_ago - 10, ended_at=now - ended_ago, partial=partial,
    )  # fmt: skip


async def test_a_skipped_clip_is_never_judged_and_says_why(tmp_path: Path) -> None:
    worker, judge, _triager, db, intake = await triage_setup(tmp_path)
    skipped = item("cam-skip")
    await intake.handle("incident.closed", skipped.model_dump(mode="json"))
    assert await worker.triage(skipped) is TriageClass.skip
    row = await db.get_event_by_source(skipped.source, skipped.source_id)
    assert row is not None and row.state == "observed"
    assert row.summary == "not judged: scripted tiny"
    assert row.intent is not None and row.intent["triage"]["class"] == "skip"
    assert row.intent["triage"]["phase"] == "final" and judge.seen == []
    await db.close()


async def test_now_before_later_newest_first_and_later_only_when_idle(tmp_path: Path) -> None:
    worker, _judge, _triager, db, _intake = await triage_setup(tmp_path)
    pending: dict[str, Incident] = {}
    old_now, new_now, later = item("cam-now", ended_ago=60), item("cam-now"), item("cam-later")
    car = item("cam-now", ended_ago=1, name="car")
    for incident, order in ((later, RANK_LATER), (old_now, RANK_PEOPLE), (car, RANK_SECOND),
                            (new_now, RANK_PEOPLE)):  # fmt: skip
        pending[incident.key] = incident
        worker._ranks[incident.key] = order
    assert worker.next_incident(pending) is new_now
    assert worker.next_incident(pending) is old_now, "people before a newer moving car"
    assert worker.next_incident(pending) is car
    assert worker.next_incident(pending, idle=False) is None, "later waits for an idle slot"
    assert worker.next_incident(pending, idle=True) is later
    stale = item("cam-later", ended_ago=200)  # later_max_age is 2m
    pending[stale.key] = stale
    worker._ranks[stale.key] = RANK_LATER
    assert worker.next_incident(pending) is None and worker.status.later_expired == 1
    await db.close()


async def test_the_loops_judge_now_and_later_and_never_skip(tmp_path: Path) -> None:
    worker, judge, triager, db, intake = await triage_setup(tmp_path)
    worker.start()
    try:
        clips = [item("cam-skip", name="a"), item("cam-later", name="b"), item("cam-now", name="c")]
        for clip in clips:
            await intake.handle("incident.closed", clip.model_dump(mode="json"))
        for clip in clips:  # all at once: a burst
            await worker._bus.publish("incident.closed", clip.model_dump(mode="json"))
        for _ in range(200):
            if worker.status.judged == 2 and not worker.working:
                break
            await asyncio.sleep(0.02)
        assert judge.seen == [clips[2].source_id, clips[1].source_id], "now first, skip never"
        assert worker.status.later_judged == 1 and len(triager.looked) == 3
        assert worker.urgent == 0 and worker.backlog == 0
        judged = await db.get_event_by_source(clips[1].source, clips[1].source_id)
        assert judged is not None and judged.intent is not None
        assert judged.intent["triage"]["class"] == "later", "the verdict keeps the decision"
        assert judged.state == "dismissed"
    finally:
        await worker.stop()
        await db.close()


async def test_a_person_in_the_early_window_gets_the_whole_clip_judged(tmp_path: Path) -> None:
    worker, _judge, _triager, db, intake = await triage_setup(tmp_path)
    whole = item("cam-skip", name="person")
    early = whole.model_copy(update={"partial": True})
    await intake.handle("incident.updated", early.model_dump(mode="json"))
    assert await worker.triage(early) is TriageClass.now
    assert await worker.triage(whole) is TriageClass.now, "the early window had someone"
    row = await db.get_event_by_source(whole.source, whole.source_id)
    assert row is not None and row.intent is not None
    triage = row.intent["triage"]
    assert triage["reason"] == "early_person" and triage["early"]["class"] == "now"
    assert row.summary is None
    nobody = item("cam-skip", name="nobody")
    await intake.handle("incident.updated", nobody.model_copy(update={"partial": True})
                        .model_dump(mode="json"))  # fmt: skip
    assert await worker.triage(nobody.model_copy(update={"partial": True})) is TriageClass.skip
    assert await worker.triage(nobody) is TriageClass.skip
    await db.close()


# --- deep analysis and the replay ------------------------------------------------------------


def event(intent: dict[str, Any] | None, **extra: Any) -> EventRow:
    base: dict[str, Any] = {
        "id": "e1", "camera": "yard", "started_at": 1_800_000_000.0, "ended_at": None,
        "state": "observed", "kinds": [], "zones": [], "geometry": {}, "summary": None,
        "intent": intent, "policy": None, "feedback": None, "snapshot_path": None,
        "clip_path": None,
    }  # fmt: skip
    base.update(extra)
    return EventRow(**base)


def test_deep_analysis_leaves_out_what_was_skipped_and_ranks_what_had_a_person() -> None:
    skipped = event({"triage": {"class": "skip", "reason": "tiny"}})
    assert deep_classify(skipped, include_unjudged=True) is None
    person = event({"triage": {"class": "now", "reason": "person"}})
    assert deep_classify(person, include_unjudged=True) == (
        P_PERSON, "person (pre-filter), never judged"
    )  # fmt: skip
    waited = event({"triage": {"class": "later", "reason": "motion"}})
    assert deep_classify(waited, include_unjudged=True) == (P_UNJUDGED, "never judged")
    assert deep_classify(event(None), include_unjudged=True) == (P_UNJUDGED, "never judged")


def test_the_simulated_queue_takes_now_first_and_lets_later_age_out() -> None:
    arrivals = [
        Arrival(ready=1.0, ended=1.0, klass=TriageClass.later),
        Arrival(ready=1.0, ended=1.0, klass=TriageClass.now),
        Arrival(ready=1.0, ended=1.0, klass=TriageClass.skip),
        Arrival(ready=1.0, ended=1.5, klass=TriageClass.now),
        Arrival(ready=1.0, ended=1.0, klass=TriageClass.now, early=True),
    ]
    result = simulate(arrivals, [50.0], max_age_s=900.0, later_max_age_s=60.0)
    assert result.finals == 4 and result.offered["skip"] == 1
    assert result.by_class["now"] == 2 and result.judged_early == 1
    assert result.by_class.get("later", 0) == 0, "the later clip was too old by its turn"
    assert result.share("now") == 1.0
    patient = simulate(arrivals, [50.0], max_age_s=900.0, later_max_age_s=900.0)
    assert patient.by_class["later"] == 1 and patient.judged_final == 3


def test_stratified_weights_stand_for_the_events_not_looked_at() -> None:
    rows = [event(None, id=f"e{i}", camera="yard") for i in range(8)]
    rows.append(event(None, id="alert", camera="yard", state="confirmed"))
    weights = stratified_weights(rows, ["e0", "e1", "alert"])
    assert weights == {"alert": 1.0, "e0": 4.0, "e1": 4.0}


def test_config_checks_the_pre_filter() -> None:
    assert TriageConfig().enabled is False and TriageConfig().later_max_age_s == 600
    with pytest.raises(ValueError, match="possible_person"):
        TriageConfig(person=0.3, possible_person=0.4)
    with pytest.raises(ValueError, match="later_max_age"):
        TriageConfig(later_max_age="10s")


# --- eval, the runtime and the command line --------------------------------------------------


@needs_ffmpeg
async def test_the_eval_judges_alert_for_what_gets_through(tmp_path: Path) -> None:
    from vidette.triage.judge import TriageJudge

    clip = await make_clip(tmp_path / "moving.mp4", "testsrc2=size=320x180:rate=15")
    still = await make_clip(tmp_path / "still.mp4", "color=c=0x606060:s=320x180:r=15")
    policy = PolicyText("home", "the house")
    cat = Detection("animal", "cat", 0.8, (0.1, 0.1, 0.1, 0.1))
    through = TriageJudge("triage", Triager(SETTINGS, FakeDetector([cat])))
    first = TriageJudge("triage-now", Triager(SETTINGS, FakeDetector([cat])))
    verdict = await through.judge(case_of(clip), policy)
    assert verdict.alert and verdict.label == "animal" and verdict.rationale == "later: an animal"
    assert not (await first.judge(case_of(clip), policy)).alert, "an animal is not judged first"
    nothing = TriageJudge("triage", Triager(SETTINGS, FakeDetector()))
    assert not (await nothing.judge(case_of(still), policy)).alert


def test_without_its_model_the_pre_filter_orders_but_never_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vidette.runtime import AppRuntime

    cfg = VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(tmp_path / "m"), "database": str(tmp_path / "v.db")},
            "understanding": {"triage": {"enabled": True, "models_dir": str(tmp_path / "none")}},
        }
    )
    runtime = AppRuntime(cfg)
    assert runtime.triage is not None and runtime.triage.detector is None
    assert any("vidette triage models" in w for w in runtime.config_warnings)
    unchecked = Observation(detector=None, activity_peak=0.0, error="no detector")
    assert classify(unchecked, cfg.understanding.triage).klass is TriageClass.later


def test_the_command_line_says_how_to_get_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    from vidette.__main__ import main

    config = tmp_path / "vidette.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "storage": {"media_dir": str(tmp_path / "m"), "database": str(tmp_path / "v.db")},
                "understanding": {"triage": {"models_dir": str(tmp_path / "none")}},
            }
        )
    )
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")
    assert main(["triage", "--config", str(config), "check", str(clip)]) == 1
    assert "vidette triage models" in capsys.readouterr().err
