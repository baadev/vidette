"""A long clip with a person is judged in windows around the person (ADR-0022), and
incident mode keeps the pre-filter from skipping anything."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from test_incident_policy import POLICY
from test_triage import FakeDetector, item, make_clip, needs_ffmpeg, triage_setup

from vidette.core.config import VidetteConfig
from vidette.triage.detector import Detection
from vidette.triage.rules import TriageClass
from vidette.triage.track import Sighting, appearances, pack, stored_sightings, unpack
from vidette.verdicts.base import Case, PolicyText, Verdict
from vidette.verdicts.windows import crop_for, plan, real_times

# --- windows around the person --------------------------------------------------------------


def walk(start: float, end: float, *, x0: float, x1: float, h: float = 0.3) -> list[Sighting]:
    out = []
    t = start
    while t <= end:
        x = x0 + (x1 - x0) * (t - start) / max(1.0, end - start)
        out.append(Sighting(t, 0.9, (x, 0.4, 0.08, h)))
        t += 1.0
    return out


def test_a_track_splits_into_appearances_and_windows_end_at_the_last_sighting() -> None:
    track = walk(36, 39, x0=0.6, x1=0.62, h=0.08) + walk(44, 74, x0=0.3, x1=0.85, h=0.5)
    assert [len(a) for a in appearances(track)] == [4, 31]
    windows = plan(track, frames=4, window_s=20)
    first = windows[0]
    assert first.last and first.end == 74.0 and first.times[-1] == 74.0, "where they ended up"
    assert first.times[0] == first.start and len(first.times) == 4
    far = next(w for w in windows if w.start == 36.0)
    assert far.crop is not None, "a small person far away is enlarged"
    assert windows[1] is far, "then the window the usual frames cannot show"
    assert far.crop[2] == far.crop[3] and 0.4 <= far.crop[2] < 0.85


def test_a_sighting_inside_a_stall_is_the_frame_before_it() -> None:
    track = [Sighting(72.0, 0.9, (0.8, 0.5, 0.1, 0.5)), Sighting(76.0, 0.9, (0.8, 0.5, 0.1, 0.5))]
    real = real_times(track, [(73.67, 78.07)])
    assert [s.t for s in real] == [72.0, 73.62]
    assert plan(track, frames=4, gaps=[(73.67, 78.07)])[0].end == 73.62


def test_a_large_person_away_from_the_edges_is_not_cropped() -> None:
    assert crop_for([(0.3, 0.2, 0.2, 0.6)]) is None
    assert crop_for([(0.9, 0.6, 0.1, 0.4)]) is not None, "at the edge"


def test_sightings_are_kept_compactly_and_read_back() -> None:
    track = walk(0, 3, x0=0.1, x1=0.2)
    packed = pack(track)
    back = unpack(packed)
    assert [s.t for s in back] == [s.t for s in track]
    assert all(abs(a.box[0] - b.box[0]) < 1e-4 for a, b in zip(back, track, strict=True))
    assert stored_sightings({"triage": {"observed": {"persons": packed}}})[0].t == 0.0
    assert stored_sightings({"windows": {"track": packed[:1]}, "triage": {}})[0].t == 0.0


@dataclass
class WindowJudge:
    """Alerts only when it is shown the frames at the very start (a car tried there)."""

    seen: list[list[float | None]] = field(default_factory=list)
    name: str = "vlm-local"
    description: str = "windows"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        assert case.frames is not None
        times = list(case.frames.times)
        self.seen.append(times)
        if times and times[0] is not None and times[0] < 3:
            return Verdict(alert=True, label="car_tampering", level="critical", confidence=0.9,
                           persons=1, subject_at_s=[t for t in times if t is not None][:1])
        return Verdict(alert=False, label="passer_by", level="none", confidence=0.9, persons=1)


@needs_ffmpeg
async def test_a_long_clip_is_judged_in_windows_and_one_that_wakes_for_anyone_decides(
    tmp_path: Path,
) -> None:
    worker, _judge, triager, db, intake = await triage_setup(tmp_path)
    worker._config = worker._config.model_copy(
        update={
            "policies": [VidetteConfig.model_validate({"policies": [POLICY]}).policies[0]],
            "understanding": worker._config.understanding.model_copy(
                update={"windows": worker._config.understanding.windows.model_copy(
                    update={"long_clip": "5s", "window": "4s"})}
            ),
        }
    )  # fmt: skip
    judge = WindowJudge()
    worker._judges = lambda _b: judge  # type: ignore[assignment,return-value]
    person = Detection("person", "person", 0.9, (0.4, 0.3, 0.1, 0.4))
    triager.detector = FakeDetector(found=[person])
    clip = await make_clip(tmp_path / "long.mp4", "testsrc2=size=320x240:rate=10", seconds=12)
    incident = item("cam-now").model_copy(update={"media": [str(clip)], "source_id": str(clip)})
    await intake.handle("incident.closed", incident.model_dump(mode="json"))
    row = await db.get_event_by_source(incident.source, incident.source_id)
    assert row is not None
    await db.update_event(row.id, intent={"triage": {"facts": {"person": 0.9}}})
    judged = await worker.handle(incident)
    assert judged is not None and judged.state == "confirmed", judged
    windows = (judged.intent or {})["windows"]
    assert windows["plan"][0]["last"] is True, "the last sighting is judged first"
    assert windows["plan"][-1]["verdict"]["label"] == "car_tampering"
    assert windows["decided_by"] == len(windows["plan"]) - 1 and windows["track"]
    assert windows["plan"][1].get("activity") is True, "the usual frames come second"
    assert judged.intent is not None and judged.intent["verdicts"][0]["label"] == "car_tampering"
    assert len(judge.seen) == len(windows["plan"]), "no window after the one that decided"
    await db.close()


async def test_incident_mode_judges_what_the_pre_filter_would_skip(tmp_path: Path) -> None:
    worker, _judge, _triager, db, intake = await triage_setup(tmp_path)

    class Active:
        def active(self, at: float | None = None) -> object:
            return object()

        def context(self, camera: str, at: float) -> str | None:
            return "Context: an alert."

    worker._incidents = Active()  # type: ignore[assignment]
    skipped = item("cam-skip")
    await intake.handle("incident.closed", skipped.model_dump(mode="json"))
    assert await worker.triage(skipped) is TriageClass.now
    row = await db.get_event_by_source(skipped.source, skipped.source_id)
    assert row is not None and row.intent is not None
    assert row.intent["triage"]["reason"] == "incident"
    assert row.intent["triage"]["incident"]["class"] == "skip" and row.summary is None
    await db.close()


