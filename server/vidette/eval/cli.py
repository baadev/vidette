"""`vidette eval DATASET`: score judges on a labelled dataset (docs/eval.md)."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from vidette.core.config import ConfigError
from vidette.eval.dataset import (
    LABELS_FILE,
    Dataset,
    DatasetError,
    load_dataset,
    scaffold_entries,
)
from vidette.eval.metrics import CostModel
from vidette.eval.report import render_json, render_text
from vidette.eval.runner import Enrich, run_eval
from vidette.verdicts.base import Case, Judge
from vidette.verdicts.baselines import BASELINES, baseline
from vidette.vlm.endpoint import VlmUnavailableError
from vidette.vlm.setup import JUDGE_NAMES, cli_config, make_judge


def _judges(names: list[str], config_path: Path | None, dataset: Dataset) -> list[Judge]:
    """Baselines by name; `vlm-local` / `vlm-cloud` built from the config. VLM judges run
    only when named: nobody gets a hosted-model bill from a plain `vidette eval`."""
    judges: list[Judge] = []
    for name in names:
        if name in JUDGE_NAMES:
            config, _source = cli_config(config_path)
            judges.append(
                make_judge(
                    config,
                    JUDGE_NAMES[name],
                    cache_dir=dataset.root / ".vidette" / "frames",
                )
            )
        else:
            judges.append(baseline(name))
    return judges


def _face_check(config_path: Path | None) -> Enrich:
    """The face check for every entry, once per entry however many judges run."""
    from vidette.people.setup import check_for

    config, _source = cli_config(config_path)
    check = check_for(config.people, download=True)
    if not check.store.people():
        raise ValueError(f"--people: nobody is enrolled in {check.store.root}")
    cache: dict[str, Case] = {}

    async def enrich(entry_id: str, case: Case) -> Case:
        if entry_id not in cache:
            cache[entry_id] = replace(case, people=await check.check(case))
        return cache[entry_id]

    return enrich


def cmd_eval(
    dataset_path: Path,
    *,
    policies: list[str] | None,
    judges: list[str] | None,
    as_json: bool,
    cost: CostModel,
    config_path: Path | None = None,
    include_drafts: bool = False,
    people: bool = False,
    known_people: str | None = None,
) -> int:
    from vidette.people.engine import EngineUnavailableError
    from vidette.people.models import ModelError
    from vidette.people.store import StoreError

    try:
        dataset = load_dataset(dataset_path, include_drafts=include_drafts)
        chosen = _judges(judges or list(BASELINES), config_path, dataset)
        policy_names = policies or list(dataset.labels.policies)
        enrich = _face_check(config_path) if people else None
        runs = asyncio.run(
            run_eval(dataset, policy_names, chosen, enrich=enrich, known_people=known_people)
        )
    except (
        DatasetError, ValueError, ConfigError, VlmUnavailableError, StoreError, ModelError,
        EngineUnavailableError,
    ) as exc:  # fmt: skip
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(render_json(dataset, runs, cost), indent=2))
    else:
        print(render_text(dataset, runs, cost), end="")
    return 0


def cmd_scaffold(dataset_path: Path) -> int:
    root = dataset_path if dataset_path.is_dir() else dataset_path.parent
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    snippets = scaffold_entries(root)
    if not snippets:
        print(f"# every clip under {root} already has an entry in {LABELS_FILE}")
        return 0
    print(f"# {len(snippets)} clip(s) without an entry; paste under `entries:` in {LABELS_FILE}")
    print("".join(snippets), end="")
    return 0
