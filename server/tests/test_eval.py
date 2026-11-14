"""`vidette eval`: dataset format, runner, report and CLI, on the shipped synthetic sample
and on small datasets built in tmp dirs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from vidette.__main__ import main
from vidette.eval.dataset import DatasetError, entry_incident, load_dataset, scaffold_entries
from vidette.eval.metrics import CostModel
from vidette.eval.report import render_json, render_text
from vidette.eval.runner import run_eval
from vidette.verdicts.base import Case, PolicyText, Verdict
from vidette.verdicts.baselines import BASELINES, baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "datasets" / "synthetic-sample"


def labels(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "name": "tmp",
        "license": "test",
        "observed_hours": 24,
        "scenarios": {"passer_by": "walks past", "door_handle_attempt": "tries the door"},
        "policies": {
            "entry": {"description": "alert on door attempts", "alert_on": ["door_handle_attempt"]}
        },
        "entries": [
            {"id": "a", "scenario": "door_handle_attempt", "upstream": {"severity": "alert"}},
            {"id": "b", "scenario": "passer_by", "upstream": {"severity": "detection"}},
        ],
    }
    base.update(overrides)
    return base


def write_dataset(root: Path, data: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "labels.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


# --- the shipped sample -----------------------------------------------------------------------


def test_shipped_sample_loads_and_every_media_file_exists() -> None:
    dataset = load_dataset(SAMPLE)
    assert len(dataset.scored_entries) == 5
    assert dataset.labels.license.startswith("Apache-2.0")
    for entry in dataset.labels.entries:
        assert entry.media, entry.id
        for path in dataset.case(entry).media:
            assert path.stat().st_size < 4096  # tiny synthetic frames, never footage


async def test_shipped_sample_baseline_numbers() -> None:
    dataset = load_dataset(SAMPLE)
    runs = {
        run.judge: run.score
        for run in await run_eval(dataset, ["entry-interest"], list(BASELINES.values()))
    }
    assert (runs["always"].confusion.tp, runs["always"].confusion.fp) == (1, 4)
    assert runs["always"].false_alarms_per_day == pytest.approx(4.0)
    assert runs["never"].recall.value == 0.0
    assert runs["frigate-severity"].false_alarms == ["courier-day"]
    assert runs["frigate-genai"].abstained == 3
    assert runs["frigate-genai"].precision.value == 1.0


def test_entry_becomes_the_same_incident_shape_as_live_frigate() -> None:
    dataset = load_dataset(SAMPLE)
    entry = next(e for e in dataset.labels.entries if e.id == "door-handle-night")
    incident = entry_incident(entry)
    assert incident.key == "dataset:door-handle-night"
    assert incident.severity == "alert"
    assert incident.zones == ["path", "porch"]
    assert incident.duration_s == 33
    assert incident.upstream_summary is not None
    assert incident.upstream_summary.threat_level == 1


# --- format validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"entries": [{"id": "a", "scenario": "ufo"}]}, "not under scenarios"),
        ({"entries": [{"id": "a", "scenario": "passer_by"}] * 2}, "used twice"),
        (
            {"policies": {"p": {"description": "d", "alert_on": ["ufo"]}}},
            "unknown scenarios",
        ),
        ({"policies": {"P!": {"description": "d", "alert_on": ["passer_by"]}}}, "[a-z0-9-]"),
        ({"scenarios": {"unlabelled": "x"}}, "reserved"),
        ({"surprise": 1}, "Extra inputs"),
        ({"observed_hours": 0}, "greater than 0"),
    ],
)
def test_invalid_labels_are_rejected_with_a_reason(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    root = write_dataset(tmp_path / "set", labels(**change))
    with pytest.raises(DatasetError) as info:
        load_dataset(root)
    assert message in str(info.value)


def test_missing_and_escaping_media_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "outside.jpg").write_bytes(b"x")
    root = write_dataset(
        tmp_path / "set",
        labels(
            entries=[
                {"id": "a", "scenario": "passer_by", "media": ["frames/nope.jpg"]},
                {"id": "b", "scenario": "passer_by", "media": ["../outside.jpg"]},
            ]
        ),
    )
    with pytest.raises(DatasetError) as info:
        load_dataset(root)
    assert "'frames/nope.jpg' does not exist" in str(info.value)
    assert "points outside the dataset" in str(info.value)


def test_unlabelled_entries_are_kept_but_not_scored(tmp_path: Path) -> None:
    root = write_dataset(
        tmp_path / "set",
        labels(entries=[*labels()["entries"], {"id": "c", "scenario": "unlabelled"}]),
    )
    dataset = load_dataset(root)
    assert dataset.unlabelled_count == 1
    assert [entry.id for entry in dataset.scored_entries] == ["a", "b"]


async def test_machine_drafts_are_not_truth_until_reviewed(tmp_path: Path) -> None:
    draft = {"by": "vlm-local m", "scenario": "door_handle_attempt", "confidence": 0.8}
    data = labels(
        entries=[
            {"id": "a", "scenario": "door_handle_attempt", "status": "draft", "draft": draft},
            {"id": "b", "scenario": "door_handle_attempt", "status": "reviewed", "draft": draft},
            {"id": "c", "scenario": "passer_by", "status": "reviewed",
             "draft": {**draft, "scenario": "door_handle_attempt"}},
            {"id": "d", "scenario": "unlabelled", "status": "draft",
             "draft": {**draft, "scenario": "unclear"}},
        ]
    )  # fmt: skip
    root = write_dataset(tmp_path / "set", data)
    dataset = load_dataset(root)
    assert [e.id for e in dataset.scored_entries] == ["b", "c"]
    assert (dataset.draft_count, dataset.unlabelled_count) == (1, 1)
    assert dataset.accepted_unchanged_count == 1  # b: the model's label, accepted as is
    runs = await run_eval(dataset, ["entry"], [baseline("always")])
    text = render_text(dataset, runs, CostModel())
    assert "1 machine drafts awaiting review (not scored)" in text
    assert "1 of 2 truth labels are model drafts a reviewer accepted unchanged" in text
    loud = load_dataset(root, include_drafts=True)
    assert [e.id for e in loud.scored_entries] == ["a", "b", "c"]
    assert "DRAFTS INCLUDED" in render_text(loud, runs, CostModel())


def test_aliases_must_point_at_known_scenarios(tmp_path: Path) -> None:
    root = write_dataset(tmp_path / "set", labels(aliases={"cat-ish": "cat"}))
    with pytest.raises(DatasetError, match="alias 'cat-ish'"):
        load_dataset(root)


def test_naive_timestamps_are_utc(tmp_path: Path) -> None:
    root = write_dataset(
        tmp_path / "set",
        labels(entries=[{"id": "a", "scenario": "passer_by", "at": "2026-11-14T10:00:00"}]),
    )
    entry = load_dataset(root).labels.entries[0]
    assert entry_incident(entry).started_at == 1794650400.0


# --- runner -----------------------------------------------------------------------------------


class ExplodingJudge:
    name = "exploding"
    description = "raises on purpose"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        raise RuntimeError("model fell over")


class PeekingJudge:
    """Records what a judge is allowed to see: never the ground truth."""

    name = "peeking"
    description = "records its inputs"

    def __init__(self) -> None:
        self.seen: list[tuple[Case, PolicyText]] = []

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        self.seen.append((case, policy))
        return Verdict(alert=True, vlm_calls=1, images_sent=len(case.media))


async def test_a_failing_judge_abstains_instead_of_voiding_the_run(tmp_path: Path) -> None:
    dataset = load_dataset(write_dataset(tmp_path / "set", labels()))
    [run] = await run_eval(dataset, ["entry"], [ExplodingJudge()])
    assert run.score.abstained == 2
    assert run.score.misses == ["a"]


async def test_judges_see_policy_text_and_media_but_not_the_answer() -> None:
    dataset = load_dataset(SAMPLE)
    judge = PeekingJudge()
    [run] = await run_eval(dataset, ["entry-interest"], [judge])
    case, policy = judge.seen[0]
    assert policy == PolicyText(
        name="entry-interest", description=dataset.policy("entry-interest").description
    )
    assert not hasattr(policy, "alert_on")
    assert len(case.media) == 3
    assert (run.score.vlm_calls, run.score.images_sent) == (5, 15)


async def test_unknown_policy_fails_before_judging(tmp_path: Path) -> None:
    dataset = load_dataset(write_dataset(tmp_path / "set", labels()))
    judge = PeekingJudge()
    with pytest.raises(DatasetError, match="available: entry"):
        await run_eval(dataset, ["nope"], [judge])
    assert judge.seen == []


def test_unknown_judge_names_the_available_ones() -> None:
    with pytest.raises(ValueError, match="frigate-severity"):
        baseline("gpt-magic")


# --- report and CLI ---------------------------------------------------------------------------


async def test_reports_carry_intervals_cost_and_per_scenario_rows() -> None:
    dataset = load_dataset(SAMPLE)
    runs = await run_eval(dataset, ["entry-interest"], list(BASELINES.values()))
    cost = CostModel(usd_per_image=0.001, frames_per_call=4)
    text = render_text(dataset, runs, cost)
    assert "0.20 (0.04-0.62)" in text  # `always` precision with its Wilson interval
    assert "door_handle_attempt" in text and "should alert" in text
    assert "5 calls, 20 images, $0.02 on this set" in text
    assert "per 30 days" in text
    data = render_json(dataset, runs, cost)
    assert data["projection"] == {"calls": 5, "images": 20, "usd": pytest.approx(0.02)}
    always = next(run for run in data["runs"] if run["judge"] == "always")
    assert always["fp"] == 4 and always["precision"]["n"] == 5
    json.dumps(data)  # serializable as is


def test_cli_eval_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", str(SAMPLE), "--judge", "always", "--judge", "frigate-severity"]) == 0
    out = capsys.readouterr().out
    assert "frigate-severity" in out and "frigate-genai" not in out
    assert main(["eval", str(SAMPLE / "labels.yaml"), "--json", "--usd-per-call", "0.01"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {run["judge"] for run in data["runs"]} == set(BASELINES)


def test_cli_eval_reports_dataset_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["eval", str(tmp_path / "missing")]) == 2
    assert "cannot read" in capsys.readouterr().err
    assert main(["eval", str(SAMPLE), "--judge", "oracle"]) == 2
    assert "unknown judge" in capsys.readouterr().err


def test_scaffold_lists_clips_without_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "home"
    shutil.copytree(SAMPLE, root)
    (root / "clips").mkdir()
    (root / "clips" / "2026-11-14 21.03 porch.mp4").write_bytes(b"\x00")
    (root / "clips" / "2026-11-14 21.03 porch.MOV").write_bytes(b"\x00")
    snippets = scaffold_entries(root)
    assert len(snippets) == 2  # frames already referenced by entries are not repeated
    assert "id: 2026-11-14-21.03-porch\n" in snippets[0]
    assert "id: 2026-11-14-21.03-porch-2\n" in snippets[1]
    assert "scenario: unlabelled" in snippets[0]
    assert main(["eval", str(root), "--scaffold"]) == 0
    assert "2 clip(s) without an entry" in capsys.readouterr().out
