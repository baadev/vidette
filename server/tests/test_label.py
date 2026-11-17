"""`vidette label`: drafts that never pose as truth, sheets for a phone, replies by number."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes.openai_api import FakeVlmApi

from vidette.__main__ import main
from vidette.collect.clips import finalize, partial_path
from vidette.eval.dataset import load_dataset
from vidette.label import labels_file
from vidette.label.canvas import Canvas, ascii_only, wrap
from vidette.label.review import ReviewError, parse_reply, resolve
from vidette.label.sheet import tile_boxes

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)


def drafted(root: Path, proposals: list[str | None]) -> Path:
    """A labels.yaml with numbered draft entries (no media needed for parsing tests)."""
    labels_file.create(root, observed_hours=None)
    blocks = []
    for number, proposal in enumerate(proposals, start=1):
        entry: dict[str, Any] = {"id": f"c{number}", "scenario": "unlabelled", "number": number}
        if proposal is not None:
            entry.update(
                scenario=proposal if proposal != "unclear" else "unlabelled",
                status="draft",
                draft={"by": "vlm-local m", "scenario": proposal, "confidence": 0.7},
            )
        blocks.append(labels_file.render_entry(entry))
    text = labels_file.append_entries((root / "labels.yaml").read_text(), blocks)
    labels_file.write_validated(root, text)
    return root


# --- labels.yaml editing ---------------------------------------------------------------


def test_template_is_a_valid_empty_dataset_with_a_managed_observed_line(tmp_path: Path) -> None:
    labels_file.create(tmp_path, observed_hours=None)
    dataset = load_dataset(tmp_path)
    assert dataset.labels.entries == [] and dataset.labels.observed_hours is None
    text = (tmp_path / "labels.yaml").read_text()
    text, changed = labels_file.set_observed_hours(text, 41.25)
    assert changed and "observed_hours: 41.25  # vidette-managed" in text
    assert labels_file.set_observed_hours(text, 41.25) == (text, False)


def test_a_hand_set_observed_hours_is_never_overwritten() -> None:
    text = "observed_hours: 168   # one week, counted by me\n"
    assert labels_file.set_observed_hours(text, 3.0) == (text, False)


def test_appending_and_replacing_keep_every_hand_written_byte(tmp_path: Path) -> None:
    hand = (
        "# my notes about this camera\n"
        "version: 1\nname: porch\nlicense: private\n"
        "scenarios: {cat: a cat, passer_by: walks past}\n"
        "policies:\n  p:\n    description: x\n    alert_on: [cat]\n"
        "entries:\n"
        "  - id: mine  # labelled by hand, keep this comment\n"
        "    scenario: cat\n"
        "\n"
        "  # a comment between entries\n"
    )
    (tmp_path / "labels.yaml").write_text(hand)
    block = labels_file.render_entry({"id": "20261114-210304", "scenario": "unlabelled",
                                      "number": 1, "media": []})  # fmt: skip
    text = labels_file.append_entries(hand, [block])
    labels_file.write_validated(tmp_path, text)
    assert text.startswith(hand.rstrip("\n").rsplit("\n  # a comment", 1)[0])
    assert "# labelled by hand, keep this comment" in text
    replaced = labels_file.replace_entry(
        text, "20261114-210304",
        labels_file.render_entry({"id": "20261114-210304", "scenario": "cat", "number": 1}),
    )  # fmt: skip
    assert "# my notes about this camera" in replaced and "keep this comment" in replaced
    labels_file.write_validated(tmp_path, replaced)
    entries = {e.id: e for e in load_dataset(tmp_path).labels.entries}
    assert entries["20261114-210304"].scenario == "cat" and entries["mine"].scenario == "cat"
    assert (tmp_path / "labels.yaml.bak").exists()


def test_an_edit_that_breaks_the_dataset_is_refused(tmp_path: Path) -> None:
    labels_file.create(tmp_path, observed_hours=None)
    broken = labels_file.append_entries(
        (tmp_path / "labels.yaml").read_text(),
        [labels_file.render_entry({"id": "x", "scenario": "giraffe"})],
    )
    before = (tmp_path / "labels.yaml").read_text()
    with pytest.raises(Exception, match="giraffe"):
        labels_file.write_validated(tmp_path, broken)
    assert (tmp_path / "labels.yaml").read_text() == before


def test_rendered_scalars_read_back_as_the_same_strings() -> None:
    block = labels_file.render_entry(
        {"id": "123", "scenario": "yes", "notes": "a, b: c", "media": ["clips/a b.mp4"]}
    )
    (item,) = yaml.safe_load("entries:\n" + block)["entries"]
    assert item == {"id": "123", "scenario": "yes", "notes": "a, b: c", "media": ["clips/a b.mp4"]}


# --- replies by number ---------------------------------------------------------------


PROPOSALS = ["courier_at_door", "passer_by", "door_handle_attempt", "cat", "car_lights",
             "unclear", "lingering_at_door", None]  # fmt: skip


def changes_for(tmp_path: Path, reply: str) -> dict[int, tuple[str, bool]]:
    labels = load_dataset(drafted(tmp_path, PROPOSALS)).labels
    return {c.number: (c.new, c.accepted_draft) for c in resolve(labels, reply)}


def test_a_russian_reply_without_commas(tmp_path: Path) -> None:
    assert changes_for(tmp_path, "3 ок 5 курьер 6 кот 7 тревога 8 ?") == {
        3: ("door_handle_attempt", True),
        5: ("courier_at_door", False),
        6: ("cat", False),
        7: ("lingering_at_door", True),  # the draft already alerts: accepted
        8: ("unlabelled", False),
    }


def test_rest_ok_accepts_only_unmentioned_drafts_with_a_proposal(tmp_path: Path) -> None:
    changes = changes_for(tmp_path, "#5 courier, 2 тихо; остальные ок")
    assert changes[5] == ("courier_at_door", False)
    assert changes[2] == ("passer_by", True)  # silent and the draft is silent: accepted
    assert set(changes) == {1, 2, 3, 4, 5, 7}  # 6 is unclear, 8 has no draft: left alone


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        ("1 тревога", "fits several scenarios"),  # truth is a scenario, never a bare flag
        ("6 ok", "no draft to accept"),
        ("8 ok", "no draft to accept"),
        ("42 cat", "no entry has that number"),
        ("4 жираф", "not a scenario or alias"),
        ("3 ok, 3 cat", "mentioned twice"),
        ("looks fine", "start with a number"),
    ],
)
def test_unclear_replies_change_nothing(tmp_path: Path, reply: str, problem: str) -> None:
    labels = load_dataset(drafted(tmp_path, PROPOSALS)).labels
    with pytest.raises(ReviewError, match=problem):
        resolve(labels, f"2 ok, {reply}")


def test_dataset_aliases_and_dashed_numbers(tmp_path: Path) -> None:
    root = drafted(tmp_path, PROPOSALS)
    text = (root / "labels.yaml").read_text().replace("aliases: {}", "aliases: {мурка: cat}")
    labels_file.write_validated(root, text)
    items, rest, problems = parse_reply("5-мурка", load_dataset(root).labels)
    assert [(i.number, i.scenario) for i in items] == [(5, "cat")] and not rest and not problems


# --- the contact sheet canvas ---------------------------------------------------------


def test_canvas_writes_a_real_png_and_never_chokes_on_text() -> None:
    canvas = Canvas(40, 20, (0, 0, 0))
    canvas.rect(-5, -5, 100, 3, (255, 0, 0))  # clipped, not an error
    canvas.text(1, 5, "Кот 12%", scale=1, color=(255, 255, 255))
    canvas.paste_rgb(30, 15, 20, 10, bytes(20 * 10 * 3))  # partly off-canvas
    png = canvas.png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (40, 20)
    assert ascii_only("Кот — 12%") == "??? - 12%"
    assert wrap("one two three four five", 9, 2) == ["one two", "three..."]


def test_portrait_frames_get_one_row_of_four_tall_tiles() -> None:
    landscape, portrait = tile_boxes(False), tile_boxes(True)
    assert len(landscape) == len(portrait) == 4
    # Same frames area, but a 9:16 frame fills 130×231 px instead of 82×146.
    assert {box[3] for box in portrait} == {2 * landscape[0][3]}
    assert [box[0] for box in portrait] == [0, 130, 260, 390]
    assert all(box[0] + box[2] <= 522 for box in portrait + landscape)


# --- the commands, end to end --------------------------------------------------------


async def make_clips(root: Path, count: int) -> None:
    for index in range(count):
        started = time.time() - 3600 + index * 300
        part = partial_path(root, started)
        part.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=320x180:rate=10", "-t", str(2 + index), "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-f", "matroska", str(part),
        )  # fmt: skip
        assert await proc.wait() == 0
        meta = await finalize(part, root, camera="porch", started_at=started,
                              stop_reason="stream ended", min_seconds=1)  # fmt: skip
        assert meta is not None


def scripted(proposals: list[str]) -> Any:
    calls = iter(proposals)

    def answer(text: str, images: list[bytes], body: dict[str, Any]) -> dict[str, Any]:
        assert body["response_format"]["json_schema"]["name"] == "draft_label"
        assert "courier_at_door:" in text  # the dataset's vocabulary, with meanings
        return {"scenario": next(calls), "description": "Something happens.", "confidence": 0.8}

    return answer


@needs_ffmpeg
def test_draft_sheets_apply_and_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "front-door"
    root.mkdir()
    asyncio.run(make_clips(root, 3))
    (root / ".vidette").mkdir(exist_ok=True)
    (root / ".vidette" / "collect-state.json").write_text(json.dumps({"watched_seconds": 7200}))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    api = FakeVlmApi(answer=scripted(["courier_at_door", "door_handle_attempt", "unclear"]))
    with api.serve() as url:
        config = tmp_path / "vidette.yaml"
        config.write_text(
            f"storage: {{database: {tmp_path / 'none.db'}}}\n"
            f"understanding: {{vlm: {{local: {{base_url: '{url}', model: fake-vlm}}}}}}\n"
        )
        assert main(["label", "draft", str(root), "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "added 3 entries (#1-#3)" in out and "Frames stay on this machine" in out

    dataset = load_dataset(root)
    assert dataset.labels.observed_hours == 2.0
    assert [e.status for e in dataset.labels.entries] == ["draft"] * 3
    assert [e.scenario for e in dataset.labels.entries][2] == "unlabelled"  # unclear
    assert dataset.scored_entries == []  # drafts are not truth
    assert all(len(api.requests[i]["body"]["messages"][1]["content"]) == 5 for i in range(3))
    (sheet,) = (root / "review").glob("sheet-*.png")
    assert sheet.name == "sheet-0001-0003.png"
    assert struct.unpack(">II", sheet.read_bytes()[16:24])[0] == 1080

    # Redrawing the sheets of pending entries works on its own (a local name used to shadow `pending`).
    sheet.unlink()
    assert main(["label", "sheets", str(root)]) == 0
    assert "3 entries to review on 1 contact sheet(s)" in capsys.readouterr().out
    assert (root / "review" / "sheet-0001-0003.png").exists()

    assert main(["label", "apply", str(root), "1 ok, 3 кот, 2 ok"]) == 0
    reviewed = load_dataset(root)
    assert [e.scenario for e in reviewed.scored_entries] == [
        "courier_at_door", "door_handle_attempt", "cat"]  # fmt: skip
    assert reviewed.accepted_unchanged_count == 2
    assert main(["eval", str(root), "--judge", "always"]) == 0
    assert "2 of 3 truth labels are model drafts a reviewer accepted unchanged" in (
        capsys.readouterr().out
    )

    # Nothing new: a second draft run does nothing and calls nobody.
    assert main(["label", "draft", str(root), "--no-vlm"]) == 0
    assert "no new clips" in capsys.readouterr().out


@needs_ffmpeg
def test_without_a_reachable_model_clips_still_get_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "set"
    root.mkdir()
    asyncio.run(make_clips(root, 2))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = tmp_path / "vidette.yaml"
    config.write_text(
        f"storage: {{database: {tmp_path / 'none.db'}}}\n"
        "understanding: {vlm: {local: {base_url: 'http://127.0.0.1:9/v1'}}}\n"
    )
    assert main(["label", "draft", str(root), "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "ollama serve" in out and "stopping drafts" in out
    entries = load_dataset(root).labels.entries
    assert [(e.number, e.status, e.draft) for e in entries] == [(1, None, None), (2, None, None)]
    assert main(["label", "apply", str(root), "1 cat, 2 passer"]) == 0
    assert len(load_dataset(root).scored_entries) == 2
    assert os.path.exists(root / "review" / "sheet-0001-0002.png")
