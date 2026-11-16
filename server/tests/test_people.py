"""Known people: the store, matching, the report, the judge's line, confirmations, the CLI.

Everything runs on the fake engine in `fakes/faces.py`: no OpenCV, no model files and no
picture of a real person anywhere in the test suite. One test at the end loads the real
models when they are already cached on the machine, and only checks that an image with
no face in it gives no face.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fakes.faces import FakeFaceEngine, identity_vector, photo

from vidette.core.config import VidetteConfig, designed_feature_warnings
from vidette.core.incidents import Incident
from vidette.label.people_reply import resolve_faces, split_reply
from vidette.people import cli as people_cli
from vidette.people import models
from vidette.people.check import PeopleCheck, build_report, confirm, crop_writer
from vidette.people.engine import DetectedFace
from vidette.people.report import STRANGER, UNSURE, PeopleReport, people_line
from vidette.people.sidecar import load_report, save_report
from vidette.people.store import (
    NewFace,
    PeopleStore,
    StoreError,
    person_id,
)
from vidette.people.vectors import cosine, decode, encode, normalize
from vidette.verdicts.base import Case

NOW = datetime(2026, 11, 14, 21, 0).astimezone()


def new_face(identity: str, variant: str, source: str = "test") -> NewFace:
    return NewFace(
        identity_vector(identity, variant), f"crop:{identity}/{variant}".encode(), 100, source
    )


def enrolled(tmp_path: Path, **people: int) -> PeopleStore:
    """A store with each person enrolled from `count` fake faces."""
    store = PeopleStore(tmp_path / "people")
    for name, count in people.items():
        person = store.create(name.capitalize())
        store.add_faces(person.id, [new_face(name, str(i)) for i in range(count)], kind="enrolled")
    return store


# --- vectors -----------------------------------------------------------------------------


def test_vectors_round_trip_and_compare() -> None:
    vector = identity_vector("alice", "1")
    assert decode(encode(vector)) == pytest.approx(vector, abs=1e-6)
    assert cosine(vector, vector) == pytest.approx(1.0)
    assert normalize([0.0, 0.0]) == (0.0, 0.0)
    same = cosine(identity_vector("alice", "1"), identity_vector("alice", "2"))
    other = cosine(identity_vector("alice", "1"), identity_vector("bob", "1"))
    assert 0.4 < same < 0.92 and abs(other) < 0.3


# --- the store ---------------------------------------------------------------------------


def test_store_refuses_a_folder_inside_a_git_work_tree(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(StoreError, match="git work tree"):
        PeopleStore(tmp_path / "repo" / "people")


def test_store_files_are_owner_only(tmp_path: Path) -> None:
    os.umask(0o022)
    store = enrolled(tmp_path, alice=3)
    for path in [store.root, *store.root.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, f"{path} is {oct(mode)}"
    assert store.permission_warnings() == []
    (store.root / "alice").chmod(0o755)
    assert "chmod -R go-rwx" in store.permission_warnings()[0]


def test_people_are_found_by_name_or_id_and_named_once(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people")
    store.create("sam")
    store.create("Sam")  # the same person, spelled as they prefer
    assert [p.name for p in store.people()] == ["Sam"]
    assert store.find("SAM") is not None and store.find("sam") is not None
    assert person_id("Anna Maria") == "anna-maria" and person_id("Мария").startswith("person-")
    with pytest.raises(StoreError, match="enrolled: Sam"):
        store.require("bob")


def test_adding_faces_skips_duplicates_others_and_outliers(tmp_path: Path) -> None:
    store = enrolled(tmp_path, bob=3)
    alice = store.create("Alice")
    batch = [
        new_face("alice", "1"),
        new_face("alice", "1"),  # the same crop twice
        new_face("alice", "2"),
        new_face("alice", "3"),
        new_face("bob", "9"),  # bob, already enrolled
        new_face("carol", "1"),  # someone nobody enrolled, unlike every other face
    ]
    result = store.add_faces(alice.id, batch, kind="enrolled")
    assert len(result.added) == 3 and result.duplicates == 1
    assert [name for _source, name, _score in result.elsewhere] == ["Bob"]
    assert len(result.outliers) == 1
    view = store.view(store.require("alice"))
    assert view.enrolled == 3 and view.confirmed == 0 and view.threshold == 0.4


def test_the_face_limit_keeps_enrolled_faces_and_recycles_confirmed_ones(
    tmp_path: Path,
) -> None:
    store = PeopleStore(tmp_path / "people", max_faces=5)
    alice = store.create("Alice")
    store.add_faces(alice.id, [new_face("alice", str(i)) for i in range(4)], kind="enrolled")
    first = store.add_faces(alice.id, [new_face("alice", "c1")], kind="confirmed")
    assert first.added and store.view(alice).confirmed == 1
    second = store.add_faces(alice.id, [new_face("alice", "c2")], kind="confirmed")
    assert second.added and second.dropped_for_room == 1
    full = store.add_faces(alice.id, [new_face("alice", "e9")], kind="enrolled")
    assert full.refused_full == 1 and not full.added
    sources = [f.source for f in store.faces(alice.id)]
    assert len(sources) == 5


def test_delete_removes_what_the_store_wrote_and_keeps_files_put_there_by_hand(
    tmp_path: Path,
) -> None:
    store = enrolled(tmp_path, alice=2)
    hand = store.root / "alice" / "selfie.jpg"
    hand.write_bytes(b"not ours")
    store.add_photo("alice", b"a photo", ".jpg")
    assert store.foreign_files("alice") == ["selfie.jpg"]
    store.delete("Alice")
    assert hand.read_bytes() == b"not ours"
    assert sorted(p.name for p in (store.root / "alice").iterdir()) == ["selfie.jpg"]
    assert store.people() == [] and store.not_enrolled() == ["alice"]


def test_a_confirmed_wrong_match_raises_that_persons_threshold(tmp_path: Path) -> None:
    store = enrolled(tmp_path, alice=3)
    assert store.reject("alice", 0.47, "clip:x") == pytest.approx(0.49)
    assert store.reject("alice", 0.42, "clip:y") == pytest.approx(0.49)  # the highest wins
    assert store.gallery().candidates[0].threshold == pytest.approx(0.49)


# --- matching and the report ------------------------------------------------------------


def test_gallery_matches_above_the_threshold_and_refuses_ties(tmp_path: Path) -> None:
    store = enrolled(tmp_path, alice=4, bob=4)
    gallery = store.gallery()
    hit = gallery.match(identity_vector("alice", "new"))
    assert hit.person == "alice" and hit.score >= 0.4
    miss = gallery.match(identity_vector("carol", "1"))
    assert miss.person is None and miss.closest in ("alice", "bob")
    both = normalize(
        a + b
        for a, b in zip(identity_vector("alice", "x"), identity_vector("bob", "x"), strict=True)
    )
    tie = gallery.match(both)
    assert tie.person is None and tie.ambiguous


def faces(*tokens: str) -> list[DetectedFace]:
    return FakeFaceEngine().faces(photo(*tokens)) if tokens else []


def test_report_groups_faces_into_people_and_counts_unclear_frames(tmp_path: Path) -> None:
    store = enrolled(tmp_path, alice=4)
    written: dict[int, bytes] = {}

    def crops(n: int, data: bytes) -> str:
        written[n] = data
        return f"faces/clip/{n}.jpg"

    frames = [
        faces("face:alice/a"),
        faces("face:alice/b", "face:carol/1"),
        faces("face:carol/2"),  # the same stranger again: one unknown person
        faces("small"),
        faces("side", "small"),
        faces(),
    ]
    report = build_report(
        frames, store.gallery(), engine="fake", threshold=0.4, crops=crops, now=NOW
    )
    assert report.frames_checked == 6
    assert report.frames_with_readable_faces == 3
    assert report.unclear_frames == 2 and report.frames_without_readable_face == 3
    assert [(p.status, p.name, p.faces) for p in report.seen] == [
        ("known", "Alice", 2),
        ("unknown", None, 2),
        ("unclear", None, 2),  # the small face in frames 4 and 5, followed by place
    ]
    assert report.seen[0].frames == [0, 1] and report.seen[1].frames == [1, 2]
    assert report.seen[2].frames == [3, 4]  # the turned-away face, seen once, is noise
    assert sorted(written) == [1, 2, 3] and report.seen[0].crop == "faces/clip/1.jpg"
    assert report.summary().startswith("Alice 0.") and "1 unknown" in report.summary()


def test_an_unmatched_face_like_a_matched_one_joins_that_person(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people", threshold=0.95)  # nothing clears this
    alice = store.create("Alice")
    store.add_faces(alice.id, [new_face("alice", "a")], kind="enrolled")
    report = build_report(
        [faces("face:alice/a"), faces("face:alice/a")],
        store.gallery(),
        engine="f",
        threshold=0.95,
        now=NOW,
    )
    assert len(report.seen) == 1 and report.seen[0].status == "known"


# --- what the judge is told ---------------------------------------------------------------


def report_with(*seen: dict[str, Any], frames: int = 10, readable: int = 3) -> PeopleReport:
    return PeopleReport.model_validate(
        {
            "checked_at": NOW.isoformat(),
            "engine": "fake",
            "threshold": 0.4,
            "enrolled": 3,
            "frames_checked": frames,
            "frames_with_readable_faces": readable,
            "unclear_frames": 2,
            "seen": list(seen),
        }
    )


SAM = {"n": 1, "status": "known", "person": "sam", "name": "Sam", "score": 0.83}
UNKNOWN = {"n": 2, "status": "unknown", "score": 0.21, "closest": "robin"}


def test_the_judge_hears_names_only_from_a_local_check_and_never_who_is_enrolled() -> None:
    report = report_with(SAM, UNKNOWN)
    local = people_line(report, names=True)
    assert "recognized Sam (0.83)" in local
    assert "matching nobody enrolled: 1" in local and "no clear face in 7 of 10 frames" in local
    assert "robin" not in local.lower()  # the nearest enrolled name of a stranger: never
    assert "not evidence of a stranger" in local
    cloud = people_line(report, names=False)
    assert "Sam" not in cloud and "1 person the household enrolled as known" in cloud
    assert "never need an alert" not in local
    assert "never need an alert" in people_line(report, names=True, quiet=True)
    assert "never need an alert" not in people_line(report_with(UNKNOWN), names=True, quiet=True)
    assert people_line(None, names=True) == ""
    assert people_line(report_with(frames=0, readable=0), names=True) == ""


async def test_the_vlm_judge_puts_the_line_in_its_prompt() -> None:
    import httpx
    from fakes.openai_api import FakeVlmApi
    from test_vlm import SAMPLE, cloud_endpoint, local_endpoint

    from vidette.eval.dataset import load_dataset
    from vidette.verdicts.base import PolicyText
    from vidette.verdicts.vlm import VlmJudge
    from vidette.vlm.budget import MemoryLedger
    from vidette.vlm.client import VlmClient

    dataset = load_dataset(SAMPLE)
    case = dataset.case(dataset.labels.entries[0])
    case = Case(incident=case.incident, media=case.media, people=report_with(SAM))
    policy = PolicyText("yard", "Strangers in the yard.", known_people="quiet")
    texts = {}
    for name, endpoint in (("local", local_endpoint()), ("cloud", cloud_endpoint())):
        api = FakeVlmApi()
        client = VlmClient(
            endpoint, ledger=MemoryLedger(), http=httpx.AsyncClient(transport=api.transport())
        )
        await VlmJudge(client=client).judge(case, policy)
        texts[name] = api.requests[0]["body"]["messages"][1]["content"][0]["text"]
    assert "recognized Sam (0.83)" in texts["local"] and "never need an alert" in texts["local"]
    assert "Sam" not in texts["cloud"] and "enrolled as known" in texts["cloud"]


# --- the check and confirmations -----------------------------------------------------------


async def test_the_check_reads_key_frames_without_a_clip(tmp_path: Path) -> None:
    store = enrolled(tmp_path, alice=3)
    frames = []
    for index, content in enumerate(["face:alice/z", "small", "nothing"]):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(photo(content))
        frames.append(path)
    case = Case(Incident(source="t", source_id="1", camera="yard", started_at=0.0), tuple(frames))
    engine = FakeFaceEngine()
    report = await PeopleCheck(store, engine).check(case, crop_writer(tmp_path, "crops"))
    assert report.known[0].name == "Alice" and report.frames_checked == 3
    assert (tmp_path / "crops" / "1.jpg").read_bytes() == b"crop:alice/z"


def test_confirmations_teach_the_store(tmp_path: Path) -> None:
    store = enrolled(tmp_path, alice=4, bob=4)
    media = tmp_path / "media"
    write = crop_writer(media, "faces/ev1")
    report = build_report(
        [faces("face:alice/new", "face:dave/1", "face:bob/x")],
        store.gallery(),
        engine="fake",
        threshold=0.4,
        crops=write,
        now=NOW,
    )
    by_name = {p.name: p.n for p in report.known}
    unknown = report.unknown[0].n
    engine = FakeFaceEngine()

    done = confirm(
        report, unknown, "Alice", store=store, engine=engine, media_root=media, where="event:ev1"
    )
    assert "added to Alice" in done.message and report.get(unknown).confirmed == "alice"
    assert store.view(store.require("alice")).confirmed == 1

    done = confirm(
        report,
        by_name["Bob"],
        STRANGER,
        store=store,
        engine=None,
        media_root=media,
        where="event:ev1",
    )
    assert "threshold is now" in done.message
    assert store.view(store.require("bob")).threshold > 0.4
    assert store.view(store.require("bob")).confirmed == 0  # nothing of the stranger kept

    done = confirm(
        report,
        by_name["Alice"],
        UNSURE,
        store=store,
        engine=None,
        media_root=media,
        where="event:ev1",
    )
    assert report.get(by_name["Alice"]).confirmed == UNSURE
    with pytest.raises(StoreError, match="no face 9"):
        confirm(report, 9, UNSURE, store=store, engine=None, media_root=media, where="x")


# --- the model files ------------------------------------------------------------------------


def test_models_are_checked_before_use_and_after_download(tmp_path: Path) -> None:
    content = b"pretend onnx"
    import hashlib

    spec = models.ModelFile(
        "m.onnx",
        "https://example.test/m.onnx",
        hashlib.sha256(content).hexdigest(),
        len(content),
        "MIT",
        "m",
    )
    with pytest.raises(models.ModelError, match="vidette people models"):
        models.ensure_model(spec, tmp_path, download=False)

    def bad(url: str, target: Path) -> None:
        target.write_bytes(b"something else")

    with pytest.raises(models.ModelError, match="does not match"):
        models.ensure_model(spec, tmp_path, download=True, say=lambda _: None, fetch=bad)
    assert not (tmp_path / "m.onnx").exists() and not list(tmp_path.glob("*.part"))

    def good(url: str, target: Path) -> None:
        target.write_bytes(content)

    path = models.ensure_model(spec, tmp_path, download=True, say=lambda _: None, fetch=good)
    assert path.read_bytes() == content
    path.write_bytes(b"tampered!!!!")
    with pytest.raises(models.ModelError, match="checksum"):
        models.ensure_model(spec, tmp_path, download=False)


def test_pinned_models_are_permissively_licensed_and_pinned_to_a_commit() -> None:
    for spec in models.MODELS:
        assert spec.license in ("MIT", "Apache-2.0")
        assert models.ZOO_COMMIT in spec.url and len(spec.sha256) == 64


# --- config ---------------------------------------------------------------------------------


def test_people_config_defaults_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDETTE_DATA", str(tmp_path / "data"))
    config = VidetteConfig.model_validate(
        {"policies": [{"name": "yard", "description": "x", "known_people": "quiet"}]}
    )
    assert config.people.enabled is False
    assert config.people.dir == tmp_path / "data" / "people"
    warnings = designed_feature_warnings(config)
    assert any("known_people: quiet has no effect" in w for w in warnings)
    on = VidetteConfig.model_validate({"people": {"enabled": True, "dir": "~/x/people"}})
    assert on.people.dir == Path("~/x/people").expanduser()
    assert any("does not exist yet" in w for w in designed_feature_warnings(on))


# --- the command line and the contact-sheet reply ------------------------------------------------


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`vidette people` with the fake engine and a config whose people.dir is in tmp."""
    from vidette.label import cli as label_cli

    engine = FakeFaceEngine()
    for module in (people_cli, label_cli):
        monkeypatch.setattr(module, "engine_for", lambda *_a, **_k: engine)
        monkeypatch.setattr(
            module,
            "check_for",
            lambda config, **_k: PeopleCheck(people_cli.store_for(config), engine),
        )
    config = tmp_path / "vidette.yaml"
    config.write_text(f"people:\n  enabled: true\n  dir: {tmp_path / 'people'}\n")
    monkeypatch.setenv("VIDETTE_CONFIG", str(config))
    return tmp_path


def run_cli(*args: str) -> int:
    from vidette.__main__ import main

    return main(["people", *args])


def test_cli_add_list_and_remove(fake_cli: Path, capsys: pytest.CaptureFixture[str]) -> None:
    grid = fake_cli / "grid.png"
    grid.write_bytes(photo("face:alice/1", "face:alice/2", "face:alice/3", "small"))
    assert run_cli("add", "Alice", str(grid)) == 0
    out = capsys.readouterr().out
    assert "4 face(s), 3 readable, 1 too small or unclear" in out and "added 3 face(s)" in out
    assert run_cli("list") == 0
    assert (
        "Alice (alice): 3 enrolled + 0 confirmed faces from 1 photo(s)" in capsys.readouterr().out
    )
    assert run_cli("remove", "alice", "--yes") == 0
    assert run_cli("list") == 0 and "nobody enrolled" in capsys.readouterr().out


def make_dataset(root: Path, clip_id: str, frame_tokens: list[str]) -> Path:
    """A dataset with one collected clip whose key frames are fake photos."""
    (root / "clips").mkdir(parents=True)
    clip = root / "clips" / f"{clip_id}.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    frames = []
    for index, token in enumerate(frame_tokens, start=1):
        frame = root / "frames" / clip_id / f"{index}.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(photo(token))
        frames.append(f"frames/{clip_id}/{index}.jpg")
    sidecar = {
        "version": 1,
        "id": clip_id,
        "camera": "yard",
        "clip": f"clips/{clip_id}.mp4",
        "frames": frames,
        "started_at": NOW.isoformat(),
        "ended_at": NOW.isoformat(),
        "duration_s": 8.0,
        "stop_reason": "motion",
    }
    clip.with_suffix(".json").write_text(json.dumps(sidecar))
    (root / "labels.yaml").write_text(
        "version: 1\nname: yard\nlicense: private\nscenarios:\n  household_member: lives here\n"
        "  passer_by: walks past\npolicies:\n  yard:\n    description: strangers\n"
        f"    alert_on: [passer_by]\nentries:\n  - id: {clip_id}\n    scenario: unlabelled\n"
        f"    number: 5\n    media: [clips/{clip_id}.mp4]\n"
    )
    return clip


def test_scan_writes_the_report_into_the_sidecar_and_a_reply_confirms_a_face(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = enrolled(fake_cli, alice=4)
    dataset = fake_cli / "yard"
    clip = make_dataset(dataset, "20261114-210304", ["face:dave/1", "face:alice/q", "small"])

    async def no_video(*_a: Any, **_k: Any) -> Any:
        from vidette.ffmpeg import MediaToolError

        raise MediaToolError("the test clip is not a real video")

    monkeypatch.setattr("vidette.people.check.probe", no_video)
    assert run_cli("scan", str(dataset)) == 0
    out = capsys.readouterr().out
    assert "Alice 0." in out and "1 unknown" in out
    sidecar = json.loads(clip.with_suffix(".json").read_text())
    assert sidecar["people"]["seen"][0]["name"] == "Alice" and sidecar["stop_reason"] == "motion"
    report = load_report(clip)
    assert report is not None and len(report.seen) == 2
    crops = dataset / "faces" / clip.stem
    assert stat.S_IMODE(crops.stat().st_mode) == 0o700

    from vidette.eval.dataset import load_dataset

    labels = load_dataset(dataset).labels
    answers, rest = split_reply("5 ok, 5.2 Alice, 7 ?", store)
    assert rest == "5 ok, 7 ?" and [(a.number, a.face, a.answer) for a in answers] == [
        (5, 2, "alice")
    ]
    targets, problems = resolve_faces(labels, dataset, split_reply("5 Alice", store)[0])
    assert not targets and "say which: 5.1, 5.2" in problems[0]

    from vidette.__main__ import main

    assert main(["label", "apply", str(dataset), "5.2 alice, 5 household"]) == 0
    out = capsys.readouterr().out
    assert "added to Alice's references" in out and "set household_member" in out
    after = load_report(clip)
    assert after is not None and after.get(2) is not None and after.get(2).confirmed == "alice"
    assert store.view(store.require("alice")).confirmed == 1
    save_report(clip, after)  # idempotent
    assert run_cli("confirm", str(clip), "1", "stranger") == 0
    assert "Alice's threshold is now" in capsys.readouterr().out


# --- the real engine, only when its models are already on this machine ----------------------


def test_the_real_engine_finds_no_face_in_a_gradient() -> None:
    pytest.importorskip("cv2")
    folder = models.default_models_dir()
    if not all(models.verified(folder / spec.name, spec) for spec in models.MODELS):
        pytest.skip("face models not cached; `vidette people models` fetches them")
    import cv2
    import numpy as np

    from vidette.people.engine import load_engine

    engine = load_engine(folder, download=False)
    gradient = np.tile(np.linspace(0, 255, 640, dtype=np.uint8), (480, 1))
    ok, data = cv2.imencode(".jpg", cv2.cvtColor(gradient, cv2.COLOR_GRAY2BGR))
    assert ok and engine.faces(bytes(data.tobytes())) == []
    with pytest.raises(ValueError, match="not an image"):
        engine.faces(b"not an image")


# --- the web API ----------------------------------------------------------------------------


@pytest.fixture
def people_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from vidette.api.app import create_app
    from vidette.runtime import AppRuntime

    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "people": {
                "enabled": True,
                "dir": str(tmp_path / "people"),
                "models_dir": str(tmp_path / "no-models"),
            },
        }
    )
    runtime = AppRuntime(config)
    assert runtime.people_error is not None and "vidette people models" in runtime.people_error
    runtime.face_engine = FakeFaceEngine()  # as if the models were there
    runtime.people_error = None
    with TestClient(create_app(runtime, workers=False)) as client:
        yield client, runtime


def test_api_people_enroll_upload_and_delete(people_api: Any) -> None:
    client, _runtime = people_api
    state = client.get("/api/v1/people").json()
    assert state["enabled"] and state["available"] and state["people"] == []
    assert client.post("/api/v1/people", json={"name": "Alice"}).status_code == 201
    upload = client.post(
        "/api/v1/people/alice/photos",
        content=photo("face:alice/1", "face:alice/2", "small"),
        headers={"content-type": "image/jpeg"},
    ).json()
    assert upload["faces_found"] == 3 and upload["added"] == 2 and upload["unclear"] == 1
    face = upload["person"]["faces"][0]
    image = client.get(face["url"])
    assert image.status_code == 200 and image.headers["cache-control"] == "private, no-store"
    refused = client.post(
        "/api/v1/people/alice/photos", content=b"x", headers={"content-type": "text/plain"}
    )
    assert refused.status_code == 415
    assert client.delete(f"/api/v1/people/alice/faces/{face['id']}").status_code == 204
    assert len(client.get("/api/v1/people").json()["people"][0]["faces"]) == 1
    assert client.delete("/api/v1/people/alice").status_code == 204
    assert client.get("/api/v1/people").json()["people"] == []


def test_api_confirms_a_face_seen_in_an_event(people_api: Any) -> None:
    client, runtime = people_api
    store = runtime.people_store
    alice = store.create("Alice")
    store.add_faces(alice.id, [new_face("alice", str(i)) for i in range(3)], kind="enrolled")
    media = runtime.config.storage.media_dir
    report = build_report(
        [faces("face:alice/z", "face:dave/1")],
        store.gallery(),
        engine="fake",
        threshold=0.4,
        crops=crop_writer(media, "faces/ev-1"),
        now=NOW,
    )
    client.portal.call(
        lambda: runtime.db.insert_event(
            "ev-1",
            "yard",
            1.0,
            "dismissed",
            [],
            [],
            {},
            intent={"people": report.model_dump(mode="json")},
        )
    )
    event = client.get("/api/v1/events/ev-1").json()
    seen = event["people"]["seen"]
    assert [s["status"] for s in seen] == ["known", "unknown"]
    assert seen[1]["crop"] == "/api/v1/events/ev-1/people/2.jpg"
    assert client.get(seen[1]["crop"]).content == b"crop:dave/1"
    answer = client.post("/api/v1/events/ev-1/people/2", json={"answer": "Alice"}).json()
    assert "added to Alice" in answer["message"]
    assert answer["people"]["seen"][1]["confirmed"] == "alice"
    stored = client.get("/api/v1/events/ev-1").json()["people"]["seen"][1]["confirmed"]
    assert stored == "alice" and store.view(alice).confirmed == 1
    nobody = client.post("/api/v1/events/ev-1/people/2", json={"answer": "Zed"})
    assert nobody.status_code == 422


def test_api_says_known_people_are_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from vidette.api.app import create_app
    from vidette.runtime import AppRuntime

    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {"media_dir": str(tmp_path / "m"), "database": str(tmp_path / "v.db")},
        }
    )
    with TestClient(create_app(AppRuntime(config), workers=False)) as client:
        state = client.get("/api/v1/people").json()
        assert state["enabled"] is False and "people.enabled" in state["reason"]
        assert client.post("/api/v1/people", json={"name": "A"}).status_code == 409


# --- the live verdict worker ------------------------------------------------------------------


async def test_the_worker_checks_faces_once_and_keeps_the_report(tmp_path: Path) -> None:
    from test_verdicts_worker import observed, setup

    from vidette.verdicts.base import PolicyText, Verdict

    store = enrolled(tmp_path, alice=3)
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(photo("face:alice/q"))
    check = PeopleCheck(store, FakeFaceEngine())
    verdict = Verdict(alert=False, label="household", confidence=0.9, rationale="Alice.")
    worker, judge, db, _bus, intake = await setup(tmp_path, verdict)
    worker._people = check
    item = Incident(
        source="clip-folder",
        source_id="c1",
        camera="porch",
        started_at=judge_now() - 20,
        ended_at=judge_now() - 10,
        media=[str(frame)],
    )
    try:
        await observed(intake, item)
        row = await worker.handle(item)
        assert row is not None and row.intent is not None
        people = row.intent["people"]
        assert people["seen"][0]["name"] == "Alice"
        assert (tmp_path / "media" / people["seen"][0]["crop"]).is_file()
        seen_case, seen_policy = judge.seen[0]
        assert seen_case.people is not None and isinstance(seen_policy, PolicyText)
    finally:
        await db.close()


def judge_now() -> float:
    import time

    return time.time()
