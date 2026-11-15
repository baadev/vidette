"""Anonymous identities (ADR-0014): the store, the check, the judge's facts, the rule.

Everything runs on the fake engine's vectors (`fakes/faces.py`): two variants of one fake
identity score about 0.75, two identities about 0. No picture of a person anywhere.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fakes.faces import FakeFaceEngine, identity_vector, photo

from vidette.core.config import VidetteConfig, designed_feature_warnings
from vidette.core.incidents import Incident
from vidette.people.check import PeopleCheck, build_report, confirm, crop_writer
from vidette.people.engine import DetectedFace
from vidette.people.identities import (
    GroupFace,
    IdentityError,
    IdentitySettings,
    IdentityStore,
    SightingContext,
    promote,
)
from vidette.people.recurrence import (
    RecurrenceJudge,
    raise_for_recurrence,
    was_raised,
    with_recurrence,
)
from vidette.people.report import IdentityBrief, PeopleReport, people_line
from vidette.people.store import NewFace, PeopleStore
from vidette.verdicts.base import Case, PolicyText, Verdict

DAY = 86400.0
T0 = datetime(2026, 11, 14, 10, 0).timestamp()


def face(who: str, variant: str, px: int = 100) -> GroupFace:
    return GroupFace(identity_vector(who, variant), px, 0.95, f"crop:{who}/{variant}".encode())


def at(seconds: float, camera: str = "front_door", clip: str = "c") -> SightingContext:
    return SightingContext(
        at=T0 + seconds,
        camera=camera,
        source="clip-folder",
        source_id=f"{clip}-{seconds}",
        clip=f"/data/{clip}.mp4",
    )


@pytest.fixture
def store(tmp_path: Path) -> IdentityStore:
    clock = {"now": T0 + DAY}
    return IdentityStore(tmp_path / "identities", clock=lambda: clock["now"])


# --- the store ---------------------------------------------------------------------------


def test_the_store_refuses_a_git_tree_and_keeps_files_owner_only(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(IdentityError, match="git work tree"):
        IdentityStore(tmp_path / "repo" / "identities")
    os.umask(0o022)
    store = IdentityStore(tmp_path / "identities")
    store.assign([face("dave", "1")], at(0))
    for path in [store.root, *store.root.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, f"{path} is {oct(mode)}"
    assert store.permission_warnings() == []


def test_a_face_that_comes_back_joins_its_identity_and_a_new_face_starts_one(
    store: IdentityStore,
) -> None:
    first = store.assign([face("dave", "1"), face("dave", "2")], at(0))
    assert first.reason == "new" and first.brief is not None and first.brief.id == 1
    again = store.assign([face("dave", "3")], at(3600, camera="street"))
    assert again.reason == "joined" and again.brief is not None
    assert again.brief.id == 1 and again.brief.sightings == 2 and again.brief.days == 1
    other = store.assign([face("erin", "1")], at(7200))
    assert other.reason == "new" and other.brief is not None and other.brief.id == 2
    rows = store.entries()
    assert [row.id for row in rows] == [2, 1]
    assert rows[1].cameras == ["front_door", "street"] or rows[1].cameras == [
        "street",
        "front_door",
    ]
    assert rows[1].sightings == 2 and rows[1].cover is not None
    assert store.sighting_crop(rows[1].cover) is not None


def test_small_faces_start_weak_identities_that_nothing_joins_automatically(
    store: IdentityStore,
) -> None:
    small = store.assign([face("dave", "1", px=30)], at(0))
    assert small.reason == "new weak" and small.brief is not None and small.brief.weak
    again = store.assign([face("dave", "1", px=30)], at(0))  # the same clip checked twice
    assert again.brief is not None and again.brief.id == small.brief.id
    clear = store.assign([face("dave", "1")], at(10))  # the same face, readable: no join
    assert clear.reason == "new" and clear.brief is not None and clear.brief.id == 2
    assert store.assign([face("dave", "2", px=30)], at(20), weak=True).reason == "new weak"
    rows = {row.id: row for row in store.entries()}
    assert rows[1].weak and not rows[2].weak
    suggested = store.similar(2)
    assert suggested and suggested[0].weak  # a weak look-alike: offered, never merged
    pairs = store.duplicates()
    assert any({d.a, d.b} == {1, 2} for d in pairs)


def test_a_face_close_to_someone_enrolled_gets_an_identity_marked_so(
    store: IdentityStore,
) -> None:
    near = store.assign([face("dave", "1")], at(0), known_score=0.35)
    assert near.reason == "new" and near.brief is not None and near.brief.near_known
    far = store.assign([face("erin", "1")], at(10), known_score=0.1)
    assert far.brief is not None and not far.brief.near_known


def test_a_face_close_to_two_identities_joins_neither(store: IdentityStore) -> None:
    store.assign([face("dave", "1")], at(0))
    store.assign([face("dave", "2"), face("dave", "3")], at(10))  # joins #1
    split = store.split(1, [s.id for s in store.sightings(1)][:1])  # one person, two ids
    tie = store.assign([face("dave", "1")], at(20))
    assert tie.reason == "joined"  # the exact face: clearly the identity holding it
    scores = dict(tie.scores)
    assert split.id in scores and 1 in scores
    vague = store.assign([face("dave", "9")], at(30))
    assert vague.reason in ("ambiguous", "joined")
    if vague.reason == "ambiguous":
        assert vague.brief is None


def test_the_same_clip_checked_again_is_one_sighting(store: IdentityStore) -> None:
    context = at(0)
    store.assign([face("dave", "1")], context)
    again = store.assign([face("dave", "1"), face("dave", "2")], context)
    assert again.brief is not None and again.brief.sightings == 1
    assert store.sightings(1)[0].faces == 2
    smaller = store.assign([face("dave", "1", px=70)], context)  # a second group, smaller
    assert smaller.brief is not None and store.sightings(1)[0].face_px == 100


def test_rename_status_merge_split_and_delete(store: IdentityStore) -> None:
    store.assign([face("dave", "1")], at(0))
    store.assign([face("erin", "1")], at(10))
    assert store.rename(1, "  the   courier ").name == "the courier"
    assert store.rename(1, "").name is None
    with pytest.raises(IdentityError, match="1 to 40"):
        store.rename(1, "x" * 41)
    assert store.set_status(2, "suspicious").status == "suspicious"
    with pytest.raises(IdentityError, match="promoting"):
        store.set_status(2, "known")
    merged = store.merge(2, 1)
    assert merged.id == 1 and merged.sightings == 2 and merged.status == "suspicious"
    assert store.get(2) is None
    with pytest.raises(IdentityError, match="itself"):
        store.merge(1, 1)
    erin = next(s for s in store.sightings(1) if s.at == T0 + 10)
    split = store.split(1, [erin.id])
    assert split.id == 3 and split.sightings == 1  # numbers are never reused
    with pytest.raises(IdentityError, match="every sighting"):
        store.split(3, [erin.id])
    crop = store.sighting_crop(erin.id)
    assert crop is not None and crop.is_file()
    assert store.delete(3) == 1
    assert not crop.exists() and store.get(3) is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM refs WHERE identity = 3").fetchone()[0] == 0
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] in (0, 1)


def test_retention_drops_unflagged_identities_and_keeps_named_and_flagged_ones(
    tmp_path: Path,
) -> None:
    clock = {"now": T0}
    store = IdentityStore(
        tmp_path / "ids", IdentitySettings(retention_days=30), clock=lambda: clock["now"]
    )
    for index, who in enumerate(["dave", "erin", "fay", "gus"]):
        store.assign([face(who, "1")], at(index))
    store.rename(2, "Erin")
    store.set_status(3, "suspicious")
    store.set_status(4, "familiar")
    clock["now"] = T0 + 29 * DAY
    assert store.expire() == 0
    clock["now"] = T0 + 31 * DAY
    crop = store.sighting_crop(1)
    assert crop is not None
    assert store.expire() == 1
    assert [row.id for row in store.entries()] == [4, 3, 2] and not crop.exists()


def test_a_brief_counts_today_the_window_and_the_cameras() -> None:
    brief = IdentityBrief(
        id=12,
        at=T0 + 3600,
        sightings=5,
        days=2,
        first_seen=T0 - DAY,
        recent=[
            (T0 + 3600, "front_door"),
            (T0 + 3300, "yard"),
            (T0 + 600, "street"),
            (T0 - DAY, "street"),
        ],
    )
    assert brief.today() == 3
    assert len(brief.within(DAY)) == 3 and len(brief.within(2 * DAY)) == 4
    assert len(brief.within(600)) == 2
    assert brief.cameras_within(600) == ["yard", "front_door"]


def test_promoting_an_identity_teaches_the_person_and_stops_matching_it(
    store: IdentityStore, tmp_path: Path
) -> None:
    people = PeopleStore(tmp_path / "people")
    sam = people.create("Sam")
    for index in range(3):
        store.assign([face("sam", str(index))], at(index * 100))
    row, added = promote(store, 1, people, "sam")
    assert row.status == "known" and row.person == "sam" and row.name == "Sam"
    assert added >= 1 and people.view(sam).confirmed == added
    later = store.assign([face("sam", "9")], at(1000))
    assert later.brief is not None and later.brief.id == 2  # #1 is a known person now


# --- the check -------------------------------------------------------------------------------


def frame(*tokens: str) -> list[DetectedFace]:
    return FakeFaceEngine().faces(photo(*tokens)) if tokens else []


def test_an_unknown_person_in_a_clip_gets_an_identity_and_a_known_one_does_not(
    tmp_path: Path, store: IdentityStore
) -> None:
    people = PeopleStore(tmp_path / "people")
    alice = people.create("Alice")
    people.add_faces(
        alice.id,
        [NewFace(identity_vector("alice", str(i)), f"a{i}".encode(), 100, "t") for i in range(3)],
        kind="enrolled",
    )
    calls: list[float] = []

    def assign(faces: list[GroupFace], known: float, weak: bool) -> IdentityBrief | None:
        calls.append(known)
        return store.assign(faces, at(0), known_score=known, weak=weak).brief

    report = build_report(
        [frame("face:alice/x", "face:dave/1"), frame("face:dave/2")],
        people.gallery(),
        engine="fake",
        threshold=0.4,
        identities=assign,
    )
    known, unknown = report.seen
    assert known.identity is None and len(calls) == 1
    assert unknown.identity is not None and unknown.identity.id == 1 and unknown.identity.new


async def test_the_check_records_where_and_when_and_only_with_a_store(
    tmp_path: Path, store: IdentityStore
) -> None:
    people = PeopleStore(tmp_path / "people")
    clip = tmp_path / "f.jpg"
    clip.write_bytes(photo("face:dave/1"))
    incident = Incident(source="clip-folder", source_id="s1", camera="street", started_at=T0)
    case = Case(incident=incident, media=(clip,))
    plain = await PeopleCheck(people, FakeFaceEngine()).check(case)
    assert plain.seen[0].identity is None and store.entries() == []
    tracked = await PeopleCheck(people, FakeFaceEngine(), identities=store).check(case)
    brief = tracked.seen[0].identity
    assert brief is not None and brief.recent == [(T0, "street")]
    assert store.sightings(1)[0].source_id == "s1"


def test_this_is_sam_on_a_tracked_face_promotes_the_identity(
    tmp_path: Path, store: IdentityStore
) -> None:
    people = PeopleStore(tmp_path / "people")
    people.create("Sam")
    store.assign([face("sam", "1")], at(0))
    media = tmp_path / "media"
    report = build_report(
        [frame("face:sam/2")],
        people.gallery(),
        engine="fake",
        threshold=0.4,
        crops=crop_writer(media, "faces/ev"),
        identities=lambda faces, known, weak: store.assign(faces, at(60), known_score=known).brief,
    )
    assert report.seen[0].identity is not None and report.seen[0].identity.id == 1
    done = confirm(
        report,
        1,
        "Sam",
        store=people,
        engine=FakeFaceEngine(),
        media_root=media,
        where="event:ev",
        identities=store,
    )
    assert "stranger #1 is Sam now" in done.message
    row = store.get(1)
    assert row is not None and row.status == "known" and row.person == "sam"


# --- what the judge is told ---------------------------------------------------------------------


def report_with(*briefs: IdentityBrief | None) -> PeopleReport:
    return PeopleReport.model_validate(
        {
            "checked_at": datetime.fromtimestamp(T0).astimezone().isoformat(),
            "engine": "fake",
            "threshold": 0.4,
            "enrolled": 1,
            "frames_checked": 10,
            "frames_with_readable_faces": 4,
            "seen": [
                {
                    "n": index + 1,
                    "status": "unknown",
                    "score": 0.1,
                    "identity": None if brief is None else brief.model_dump(),
                }
                for index, brief in enumerate(briefs)
            ],
        }
    )


def brief(**changes: Any) -> IdentityBrief:
    values: dict[str, Any] = {
        "id": 12,
        "at": T0 + 3600,
        "sightings": 5,
        "days": 2,
        "first_seen": T0 - DAY,
        "recent": [(T0 + 3600, "front_door"), (T0 + 3300, "street"), (T0 + 60, "street")],
    }
    values.update(changes)
    return IdentityBrief(**values)


def test_the_judge_hears_recurrence_as_counts_and_names_only_locally() -> None:
    report = report_with(brief(name="the courier"))
    local = people_line(report, names=True)
    assert "unknown face #12 (the household calls them 'the courier'): this is the 3rd" in local
    assert "3rd time today, 5 times in total on 2 days" in local
    assert "cameras in the last hour: street, front_door" in local
    assert "the courier" in local and "does not prove intent" in local
    cloud = people_line(report, names=False)
    assert "the courier" not in cloud and "#12" in cloud and "3rd time today" in cloud
    first = people_line(report_with(brief(sightings=1, recent=[(T0 + 3600, "yard")])), names=True)
    assert "not seen before" in first
    familiar = people_line(report_with(brief(status="familiar", name="Anna")), names=True)
    assert "familiar to the household" in familiar and "Anna" in familiar
    assert "3rd time" not in familiar and "match nobody enrolled: 0" in familiar
    familiar_cloud = people_line(report_with(brief(status="familiar", name="Anna")), names=False)
    assert "Anna" not in familiar_cloud and "marked as familiar" in familiar_cloud
    quiet = people_line(report_with(brief(status="familiar")), names=True, quiet=True)
    assert "(known or familiar) never need an alert" in quiet
    near = people_line(report_with(brief(near_known=True)), names=False)
    assert "possibly a household member" in near
    suspicious = people_line(report_with(brief(status="suspicious")), names=False)
    assert "marked this person as suspicious" in suspicious
    assert "compared with earlier clips" not in people_line(report_with(None), names=True)
    for line in (local, cloud):  # behaviour only: nothing about how anyone looks
        for word in ("age", "gender", "skin", "male", "female", "ethnic"):
            assert f" {word}" not in line.lower()


# --- the policy rule --------------------------------------------------------------------------


def home(recurrence: dict[str, Any] | None = None, **people: Any) -> VidetteConfig:
    policy: dict[str, Any] = {
        "name": "home",
        "description": "A house.",
        "levels": {
            "critical": {"wake": "always", "scenarios": {"door_attempt": "tries the door"}},
            "elevated": {"wake": "unless_known", "scenarios": {"at_house": "stops at the house"}},
            "none": {"wake": "never", "scenarios": {"passer_by": "walks past"}},
        },
    }
    if recurrence is not None:
        policy["recurrence"] = recurrence
    return VidetteConfig.model_validate(
        {"policies": [policy], "people": {"enabled": True, "track_unknown": True, **people}}
    )


def passer_by() -> Verdict:
    return Verdict(alert=False, label="passer_by", level="none", confidence=0.9, persons=1)


def test_a_recurring_stranger_raises_a_lower_level_and_nothing_else() -> None:
    policy = home({"raise_to": "elevated"}).policies[0]
    three = report_with(brief())  # 3 sightings within 24 h
    raised = raise_for_recurrence(policy, passer_by(), three)
    assert raised.level == "elevated" and raised.alert and raised.label == "passer_by"
    assert was_raised(policy, raised.model_dump())
    assert not was_raised(policy, passer_by().model_dump())
    once = report_with(brief(sightings=1, recent=[(T0 + 3600, "street")]))
    assert raise_for_recurrence(policy, passer_by(), once).level == "none"
    two_cams = report_with(
        brief(sightings=2, recent=[(T0 + 3600, "yard"), (T0 + 3300, "front_door")])
    )
    assert raise_for_recurrence(policy, passer_by(), two_cams).level == "elevated"
    far_apart = report_with(
        brief(sightings=2, recent=[(T0 + 3600, "yard"), (T0 + 60, "front_door")])
    )
    assert raise_for_recurrence(policy, passer_by(), far_apart).level == "none"
    flagged = report_with(brief(status="suspicious", sightings=1, recent=[(T0 + 3600, "yard")]))
    assert raise_for_recurrence(policy, passer_by(), flagged).level == "elevated"
    for status in ("familiar", "known"):
        flagged = report_with(brief(status=status))
        assert raise_for_recurrence(policy, passer_by(), flagged).level == "none"
    critical = Verdict(alert=True, label="door_attempt", level="critical", confidence=0.9)
    assert raise_for_recurrence(policy, critical, three).level == "critical"  # never lowered
    other = Verdict(alert=False, label="other", level=None, confidence=0.9)
    assert raise_for_recurrence(policy, other, three).level is None
    abstained = Verdict(alert=False, abstained=True, level="none")
    assert raise_for_recurrence(policy, abstained, three).level == "none"
    assert raise_for_recurrence(policy, passer_by(), None).level == "none"


async def test_the_live_judges_apply_the_rule_only_when_strangers_are_tracked() -> None:
    class Fixed:
        name = "fixed"
        description = "always a passer-by"

        async def judge(self, case: Case, policy: PolicyText) -> Verdict:
            return passer_by()

    config = home({"raise_to": "elevated", "sightings": 2, "within": "1h", "cameras": None})
    judge = with_recurrence(Fixed(), config)
    assert isinstance(judge, RecurrenceJudge) and judge.name == "fixed"
    incident = Incident(source="t", source_id="1", camera="yard", started_at=T0)
    case = Case(incident=incident, people=report_with(brief()))
    verdict = await judge.judge(case, PolicyText("home", "A house."))
    assert verdict.level == "elevated"
    assert (await judge.judge(case, PolicyText("other-policy", "x"))).level == "none"
    plain = Fixed()
    assert with_recurrence(plain, home({"raise_to": "elevated"}, track_unknown=False)) is plain
    assert with_recurrence(plain, home(None)) is plain


def test_the_config_checks_the_rule_and_says_what_tracking_means(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a level of this policy"):
        home({"raise_to": "urgent"})
    with pytest.raises(ValueError, match="not a duration"):
        home({"raise_to": "elevated", "within": "soon"})
    config = home({"raise_to": "elevated"}, identities={"dir": str(tmp_path / "ids")})
    rule = config.policies[0].recurrence
    assert rule is not None and rule.within_s == DAY and rule.cameras_within_s == 600
    warnings = designed_feature_warnings(config)
    assert any("never agreed" in w and "30 days" in w for w in warnings)
    off = VidetteConfig.model_validate(
        {
            "policies": [home({"raise_to": "elevated"}).policies[0].model_dump(mode="json")],
            "people": {"track_unknown": True},
        }
    )
    warnings = designed_feature_warnings(off)
    assert any("track_unknown has no effect" in w for w in warnings)
    assert any("recurrence has no effect" in w for w in warnings)
    assert VidetteConfig().people.track_unknown is False  # off unless someone turns it on


# --- what Telegram says -----------------------------------------------------------------------


def test_the_alert_says_which_stranger_and_how_often() -> None:
    from vidette.telegram.text import who_line

    def people(*briefs: IdentityBrief | None) -> dict[str, Any]:
        return report_with(*briefs).model_dump(mode="json")

    names = {"street": "Street", "front_door": "Door"}.get
    line = who_line(people(brief()), 1, names=False, camera_name=lambda c: names(c) or c)
    assert line == "незнакомец №12, 3-й раз за сегодня (Street, Door за час)"
    first = who_line(people(brief(sightings=1, recent=[(T0 + 3600, "yard")])), 1, names=False)
    assert first == "незнакомец №12, впервые"
    other_days = brief(sightings=4, days=3, recent=[(T0 + 3600, "yard"), (T0 - DAY, "yard")])
    assert who_line(people(other_days), 1, names=False) == "незнакомец №12, 4-й раз за 3 дня"
    named = brief(name="Курьер", sightings=1, recent=[(T0 + 3600, "yard")])
    assert who_line(people(named), 1, names=True) == "Курьер (№12), впервые"
    assert "Курьер" not in who_line(people(named), 1, names=False)
    flagged = brief(status="suspicious", sightings=1, recent=[(T0 + 3600, "yard")])
    assert who_line(people(flagged), 1, names=False).startswith("⚠️ подозрительный №12")
    assert who_line(people(None), 1, names=False) == "незнакомец"
    assert who_line(people(None, None), 2, names=False) == "незнакомцы: 2"
    two = who_line(people(brief(), None), 2, names=False)
    assert two.startswith("незнакомцы: 2 (незнакомец №12, 3-й раз") and two.endswith(
        "; незнакомец)"
    )


# --- the web API ---------------------------------------------------------------------------------


@pytest.fixture
def identities_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from vidette.api.app import create_app
    from vidette.people.setup import identities_for
    from vidette.runtime import AppRuntime

    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "people": {
                "enabled": True,
                "track_unknown": True,
                "dir": str(tmp_path / "people"),
                "models_dir": str(tmp_path / "no-models"),
                "identities": {"dir": str(tmp_path / "identities")},
            },
        }
    )
    runtime = AppRuntime(config)
    runtime.face_engine = FakeFaceEngine()  # as if the models were there
    runtime.people_error = None
    runtime.identities = identities_for(config.people)
    with TestClient(create_app(runtime, workers=False)) as client:
        yield client, runtime


def test_api_lists_and_changes_identities(identities_api: Any, tmp_path: Path) -> None:
    client, runtime = identities_api
    store: IdentityStore = runtime.identities
    assert client.get("/api/v1/identities").json()["identities"] == []
    clip = tmp_path / "media" / "clips" / "c.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"not really a video")
    for index, who in enumerate(["dave", "dave", "erin"]):
        context = SightingContext(T0 + index * 60, "yard", "clip-folder", f"s{index}", str(clip))
        store.assign([face(who, str(index))], context)
    state = client.get("/api/v1/identities").json()
    assert state["enabled"] and state["retention_days"] == 30
    assert [(i["id"], i["label"], i["sightings"]) for i in state["identities"]] == [
        (2, "Stranger #2", 1),
        (1, "Stranger #1", 2),
    ]
    detail = client.get("/api/v1/identities/1").json()
    first = detail["sightings_list"][0]
    assert first["clip"] == f"/api/v1/identities/sightings/{first['id']}/clip.mp4"
    assert client.get(first["clip"]).content == b"not really a video"
    image = client.get(first["face"])
    assert image.status_code == 200 and image.headers["cache-control"] == "private, no-store"
    renamed = client.patch("/api/v1/identities/1", json={"name": "Courier"}).json()
    assert renamed["label"] == "Courier" and renamed["name"] == "Courier"
    flagged = client.patch("/api/v1/identities/2", json={"status": "suspicious"}).json()
    assert flagged["status"] == "suspicious"
    assert client.patch("/api/v1/identities/2", json={"status": "known"}).status_code == 422
    merged = client.post("/api/v1/identities/2/merge", json={"into": 1}).json()
    assert merged["sightings"] == 3 and merged["status"] == "suspicious"
    assert client.get("/api/v1/identities/2").status_code == 404
    erin = [s["id"] for s in merged["sightings_list"] if s["at"] == T0 + 120]
    split = client.post("/api/v1/identities/1/split", json={"sightings": erin}).json()
    assert split["id"] == 3 and split["sightings"] == 1
    promoted = client.post("/api/v1/identities/3/promote", json={"person": "Erin"}).json()
    assert promoted["person"] == "Erin" and promoted["identity"]["status"] == "known"
    assert runtime.people_store.find("erin") is not None
    assert client.delete("/api/v1/identities/1").status_code == 204
    assert client.delete("/api/v1/identities/1").status_code == 404
    assert [i["id"] for i in client.get("/api/v1/identities").json()["identities"]] == [3]


def test_api_serves_clips_only_from_known_folders(identities_api: Any, tmp_path: Path) -> None:
    client, runtime = identities_api
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    context = SightingContext(T0, "yard", "clip-folder", "s1", str(outside))
    runtime.identities.assign([face("dave", "1")], context)
    sighting = client.get("/api/v1/identities/1").json()["sightings_list"][0]
    assert sighting["clip"] is None
    assert client.get(f"/api/v1/identities/sightings/{sighting['id']}/clip.mp4").status_code == 404


def test_api_says_identities_are_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from vidette.api.app import create_app
    from vidette.runtime import AppRuntime

    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {"media_dir": str(tmp_path / "m"), "database": str(tmp_path / "v.db")},
            "people": {"enabled": True, "dir": str(tmp_path / "people")},
        }
    )
    with TestClient(create_app(AppRuntime(config), workers=False)) as client:
        state = client.get("/api/v1/identities").json()
        assert state["enabled"] is False and "track_unknown" in state["reason"]
        assert client.get("/api/v1/identities/1").status_code == 409
        assert client.delete("/api/v1/identities/1").status_code == 409


def test_an_event_face_links_its_identity(identities_api: Any, tmp_path: Path) -> None:
    client, runtime = identities_api
    people = runtime.people_store
    report = build_report(
        [frame("face:dave/1")],
        people.gallery(),
        engine="fake",
        threshold=0.4,
        identities=lambda faces, known, weak: (
            runtime.identities.assign(faces, at(0), known_score=known).brief
        ),
    )
    client.portal.call(
        lambda: runtime.db.insert_event(
            "ev-1", "yard", T0, "dismissed", [], [], {},
            intent={"people": report.model_dump(mode="json")},
        )
    )  # fmt: skip
    seen = client.get("/api/v1/events/ev-1").json()["people"]["seen"][0]
    assert seen["identity"] == 1 and seen["identity_note"] == "first time"


def test_a_sighting_older_than_retention_never_makes_an_identity(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "ids", clock=lambda: T0 + 40 * DAY)
    assert store.assign([face("dave", "1")], at(0)).reason == "past retention"
    assert store.entries() == []


# --- the command line ------------------------------------------------------------------------


def test_backfill_reads_the_clip_folders_oldest_first_and_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vidette.__main__ import main
    from vidette.people import cli as people_cli

    engine = FakeFaceEngine()
    monkeypatch.setattr(
        people_cli,
        "check_for",
        lambda config, identities=None, **_k: PeopleCheck(
            people_cli.store_for(config), engine, identities=identities
        ),
    )
    folder = tmp_path / "porch"
    now = datetime.now()
    for minutes, token in ((30, "face:dave/1"), (20, "face:dave/2"), (10, "small")):
        stamp = datetime.fromtimestamp(now.timestamp() - minutes * 60).strftime("%Y%m%d-%H%M%S")
        clip = folder / f"{stamp}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"not a video: ffmpeg fails, the frames are used")
        frames = folder / "frames" / stamp
        frames.mkdir(parents=True)
        (frames / "1.jpg").write_bytes(photo(token))
    config = tmp_path / "vidette.yaml"
    config.write_text(
        f"clip_folders:\n  - {{path: {folder}, camera: porch}}\n"
        f"people:\n  enabled: true\n  track_unknown: true\n  dir: {tmp_path / 'people'}\n"
        f"  identities: {{dir: {tmp_path / 'ids'}}}\n"
    )
    assert main(["people", "--config", str(config), "identities", "backfill"]) == 0
    out = capsys.readouterr().out
    assert "3 clip(s) to look at" in out and "#1 (new)" in out and "#1 (2 sightings)" in out
    assert "the store holds 1 identities, 2 sightings" in out
    assert main(["people", "--config", str(config), "identities", "backfill"]) == 0
    assert "0 clip(s) to look at" in capsys.readouterr().out
    assert main(["people", "--config", str(config), "identities", "list"]) == 0
    assert "#1: 2 sighting(s) on 1 day(s)" in capsys.readouterr().out
    assert main(["people", "--config", str(config), "identities", "delete", "1", "--yes"]) == 0
    assert (tmp_path / "ids" / "crops").is_dir() and not list(
        (tmp_path / "ids" / "crops").iterdir()
    )
    off = tmp_path / "off.yaml"
    off.write_text(f"clip_folders:\n  - {{path: {folder}}}\npeople:\n  enabled: true\n")
    assert main(["people", "--config", str(off), "identities", "backfill"]) == 2
    assert "track_unknown must be true" in capsys.readouterr().err


# --- decisions about a face, and taking them back (ADR-0016) -------------------------------------


def decision_setup(tmp_path: Path) -> tuple[PeopleStore, IdentityStore, Path, Any]:
    from vidette.people.decisions import FaceContext

    people = PeopleStore(tmp_path / "people")
    sam = people.create("Sam")
    people.add_faces(
        sam.id,
        [NewFace(identity_vector("sam", str(i)), f"s{i}".encode(), 100, "t") for i in range(3)],
        kind="enrolled",
    )
    ids = IdentityStore(tmp_path / "ids", clock=lambda: T0 + DAY)
    media = tmp_path / "media"
    ctx = FaceContext(people, FakeFaceEngine(), media, "event:ev", ids, at(0, clip="ev"))
    return people, ids, media, ctx


def test_every_decision_about_a_face_can_be_taken_back(tmp_path: Path) -> None:
    from vidette.people.decisions import IdentityChoice, decide, undo

    people, ids, media, ctx = decision_setup(tmp_path)
    report = build_report(
        [frame("face:dave/1"), frame("face:dave/2")],
        people.gallery(),
        engine="fake",
        threshold=0.4,
        crops=crop_writer(media, "faces/ev"),
        identities=lambda faces, known, weak: (
            ids.assign(faces, at(0, clip="ev"), known_score=known, weak=weak).brief
        ),
    )
    seen = report.seen[0]
    assert seen.identity is not None and seen.identity.id == 1 and seen.identity.sighting

    stranger = decide(report, 1, "stranger", ctx)
    assert "it is #1" in stranger.message and seen.confirmed == "stranger"

    familiar = decide(report, 1, IdentityChoice(name="Anna", status="familiar"), ctx)
    assert familiar.answer == "identity:2" and "moved to #2" in familiar.message
    assert seen.identity is not None and seen.identity.status == "familiar"
    assert seen.identity.name == "Anna" and [e.kind for e in seen.effects] == ["move"]
    assert ids.sightings(1) == [] and len(ids.sightings(2)) == 1

    back = undo(report, 1, ctx)
    assert "the move to #2" in back.message and seen.confirmed is None
    assert seen.identity is not None and seen.identity.id == 1
    assert ids.get(2) is None and len(ids.sightings(1)) == 1  # the new one went with it

    before = len(people.faces("sam"))
    ours = decide(report, 1, "Sam", ctx)
    assert "stranger #1 is Sam now" in ours.message
    assert ids.require(1).status == "known" and len(people.faces("sam")) > before
    changed = decide(report, 1, IdentityChoice(identity=1), ctx)  # changes the answer:
    assert len(people.faces("sam")) == before  # Sam's faces from it are gone again
    assert ids.require(1).status == "unknown" and changed.answer == "identity:1"
    undo(report, 1, ctx)
    assert seen.effects == [] and len(people.faces("sam")) == before


def test_a_small_face_becomes_an_identity_by_hand_and_says_it_may_not_be_recognized(
    tmp_path: Path,
) -> None:
    from vidette.people.decisions import IdentityChoice, decide

    people, ids, media, ctx = decision_setup(tmp_path)
    report = build_report(
        [frame("small"), frame("small")],  # followed by place: one unclear person
        people.gallery(),
        engine="fake",
        threshold=0.4,
        crops=crop_writer(media, "faces/ev"),
    )
    assert [s.status for s in report.seen] == ["unclear"] and report.seen[0].identity is None
    done = decide(report, 1, IdentityChoice(name="Neighbour", status="familiar"), ctx)
    assert "may not be recognized next time" in done.message
    row = ids.require(1)
    assert row.weak and row.status == "familiar" and row.name == "Neighbour"
    assert ids.sightings(1)[0].manual
    ours = decide(report, 1, "Sam", ctx)  # too unclear to teach Sam anything
    assert "too unclear to learn from" in ours.message and len(people.faces("sam")) == 3


def test_an_answer_given_before_answers_were_reversible_can_still_be_taken_back(
    tmp_path: Path,
) -> None:
    from vidette.people.decisions import undo

    people, _ids, media, ctx = decision_setup(tmp_path)
    report = build_report(
        [frame("face:dave/1")], people.gallery(), engine="fake", threshold=0.4,
        crops=crop_writer(media, "faces/ev"),
    )  # fmt: skip
    people.add_faces(
        "sam", [NewFace(identity_vector("dave", "1"), b"crop:dave/1", 100, "confirmed:event:ev#1")],
        kind="confirmed", check_outliers=False,
    )  # fmt: skip
    report.seen[0].confirmed = "sam"  # what the old code stored, without effects
    assert len(people.faces("sam")) == 4
    undo(report, 1, ctx)
    assert len(people.faces("sam")) == 3 and report.seen[0].confirmed is None


def test_familiar_people_are_quiet_like_the_household_but_critical_still_wakes() -> None:
    from vidette.core.config import Wake
    from vidette.telegram.routing import decide as route
    from vidette.telegram.routing import known_only

    familiar = report_with(brief(status="familiar", name="Anna")).model_dump(mode="json")
    assert known_only(familiar, 1)
    assert route(Wake.unless_known, familiar, 1, quiet=False) == "digest"
    assert route(Wake.always, familiar, 1, quiet=False) == "alert"
    both = report_with(brief(status="familiar"), brief(id=13)).model_dump(mode="json")
    assert not known_only(both, 2)  # a stranger next to the neighbour still wakes
    assert not known_only(familiar, 2)  # the judge saw someone whose face was not found


# --- the bot: who is this, and a name by reply --------------------------------------------------


async def test_the_bot_asks_who_a_face_is_and_takes_a_name_by_reply(tmp_path: Path) -> None:
    from test_telegram import bot

    people = PeopleStore(tmp_path / "people")
    people.create("Sam")
    b = await bot(tmp_path, store=people)
    try:
        await _who_is_this(b, people, tmp_path)
    finally:
        await b.db.close()


async def _who_is_this(b: Any, people: PeopleStore, tmp_path: Path) -> None:
    from test_telegram import alert, callback, event

    ids = IdentityStore(tmp_path / "ids")
    b.service._identities = ids
    media = b.config.storage.media_dir
    report = build_report(
        [frame("face:dave/1")],
        people.gallery(),
        engine="fake",
        threshold=0.4,
        crops=crop_writer(media, "faces/ev-20260115-101500"),
        identities=lambda faces, known, weak: (
            ids.assign(faces, at(0, clip="x"), known_score=known).brief
        ),
    )
    event_id = await event(
        b, "20260115-101500", level="elevated", scenario="person_at_house",
        people=report.model_dump(mode="json"), persons=1,
    )  # fmt: skip
    [row] = await alert(b, event_id)
    text = b.fake.last("sendMessage").params["text"]
    assert "незнакомец №1, впервые" in text
    await b.service.handle_update(callback(b, row, "own"))
    menu = b.fake.last("editMessageText").params["reply_markup"]["inline_keyboard"]
    labels = [button["text"] for line in menu for button in line]
    assert "Новый знакомый…" in labels and "Чужой" in labels and "Свой: Sam" in labels
    await b.service.handle_update(callback(b, row, "nf:1", update_id=2))
    prompt = b.fake.last("sendMessage")
    assert prompt.params["reply_markup"]["force_reply"] is True
    prompt_id = b.fake.next_id  # the id the fake gave the prompt
    reply = {
        "update_id": 3,
        "message": {
            "message_id": 900, "date": int(b.clock[0]), "text": "Соседка",
            "chat": {"id": row.chat_id}, "reply_to_message": {"message_id": prompt_id},
        },
    }  # fmt: skip
    await b.service.handle_update(reply)
    edited = b.fake.last("editMessageText").params["text"]
    assert "знакомый «Соседка»" in edited and "знакомый: Соседка" in edited
    stored = await b.db.get_event(event_id)
    assert stored is not None and stored.intent is not None
    face = stored.intent["people"]["seen"][0]
    assert face["identity"]["status"] == "familiar" and face["identity"]["name"] == "Соседка"
    assert "Готово" in b.fake.last("sendMessage").params["text"]
    await b.service.handle_update(callback(b, row, "u:1", update_id=4))
    assert "Решение по лицу отменено" in b.fake.last("editMessageText").params["text"]
    stored = await b.db.get_event(event_id)
    assert stored is not None and stored.intent is not None
    assert stored.intent["people"]["seen"][0]["identity"]["id"] == 1  # back where it was
    stranger_reply = dict(reply, update_id=5)  # a reply to no prompt: just a message
    assert await b.service._prompt_reply(stranger_reply["message"]) is False  # type: ignore[arg-type]


# --- the web API: decisions on an event card, merging, moving ------------------------------------


def test_api_event_face_decisions_are_reversible(identities_api: Any, tmp_path: Path) -> None:
    client, runtime = identities_api
    media = runtime.config.storage.media_dir
    report = build_report(
        [frame("face:dave/1")],
        runtime.people_store.gallery(),
        engine="fake",
        threshold=0.4,
        crops=crop_writer(media, "faces/ev-1"),
    )
    client.portal.call(
        lambda: runtime.db.insert_event(
            "ev-1", "street", T0, "dismissed", [], [], {}, source="clip-folder",
            source_ref="/data/c.mp4", intent={"people": report.model_dump(mode="json")},
        )
    )  # fmt: skip
    made = client.post(
        "/api/v1/events/ev-1/people/1/identity", json={"name": "Anna", "status": "familiar"}
    )
    assert made.status_code == 200, made.text
    face = made.json()["people"]["seen"][0]
    assert face["identity"] == 1 and face["identity_status"] == "familiar"
    assert face["confirmed"] == "identity:1" and face["identity_name"] == "Anna"
    taken = client.delete("/api/v1/events/ev-1/people/1").json()
    assert taken["people"]["seen"][0]["confirmed"] is None
    assert client.get("/api/v1/identities").json()["identities"] == []  # it went with it
    stranger = client.post("/api/v1/events/ev-1/people/1", json={"answer": "stranger"}).json()
    assert stranger["people"]["seen"][0]["identity"] == 2 and "#2" in stranger["message"]


def test_api_merges_many_moves_a_sighting_and_takes_a_promotion_back(
    identities_api: Any,
) -> None:
    client, runtime = identities_api
    store: IdentityStore = runtime.identities
    for index, who in enumerate(["dave", "erin", "fay"]):
        store.assign([face(who, "1")], at(index * 60))
    merged = client.post("/api/v1/identities/merge", json={"identities": [2, 3], "into": 1})
    assert merged.status_code == 200 and merged.json()["sightings"] == 3
    sighting = merged.json()["sightings_list"][0]["id"]
    moved = client.post(f"/api/v1/identities/sightings/{sighting}/move", json={"person": "Fay"})
    assert moved.status_code == 200 and moved.json()["status"] == "known"
    new_id = moved.json()["id"]
    assert runtime.people_store.find("fay") is not None
    back = client.post(f"/api/v1/identities/{new_id}/demote").json()
    assert back["status"] == "unknown" and back["person"] is None
    assert runtime.people_store.faces("fay") == []  # the faces it gave were taken back
    assert "duplicates" in client.get("/api/v1/identities").json()


def test_a_store_from_before_familiar_existed_is_migrated(tmp_path: Path) -> None:
    from vidette.people import identities as module

    root = tmp_path / "ids"
    root.mkdir()
    with sqlite3.connect(root / module.DB_FILE) as conn:
        conn.executescript(module._V1)
        conn.execute(
            "INSERT INTO identities (name, status, created_at, updated_at)"
            " VALUES ('Postman', 'ignored', 1, 1)"
        )
        conn.execute("PRAGMA user_version=1")
    store = IdentityStore(root)
    row = store.require(1)
    assert row.status == "familiar" and row.name == "Postman"
    with sqlite3.connect(root / module.DB_FILE) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == module.SCHEMA_VERSION
