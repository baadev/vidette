"""`vidette eval DATASET…`: score judges on labelled datasets (docs/eval.md)."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from vidette.core.config import ConfigError, VidetteConfig, Wake
from vidette.eval.dataset import (
    LABELS_FILE,
    Dataset,
    DatasetError,
    load_dataset,
    scaffold_entries,
    union,
    with_policy,
)
from vidette.eval.metrics import CostModel
from vidette.eval.report import render_json, render_text
from vidette.eval.runner import Enrich, run_eval
from vidette.eval.stored import STORED_JUDGES, StoredJudge
from vidette.verdicts.base import Case, Judge
from vidette.verdicts.baselines import BASELINES, baseline
from vidette.vlm.endpoint import VlmUnavailableError
from vidette.vlm.setup import JUDGE_NAMES, cli_config, make_judge


def _judges(
    names: list[str], config_path: Path | None, dataset: Dataset, database: Path | None
) -> list[Judge]:
    """Baselines by name; `vlm-local` / `vlm-cloud` built from the config; `stored-*`
    read from a service's database (`--db`, else the config's). VLM judges run only when
    named: nobody gets a hosted-model bill from a plain `vidette eval`."""
    judges: list[Judge] = []
    for name in names:
        if name in STORED_JUDGES:
            path = database or cli_config(config_path)[0].storage.database
            judges.append(StoredJudge.from_database(name, path))
        elif name in JUDGE_NAMES:
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


ConfigPolicy = tuple[str, str, list[str], str]  # name, description, alert_on, known_people


def config_policies(config: VidetteConfig, names: list[str]) -> list[ConfigPolicy]:
    """A service's policies as dataset policies: (name, description, the scenarios of its
    levels that alert, known_people). Raises ValueError for an unknown name or a policy
    without levels (its alerts are not named by scenario)."""
    found: list[ConfigPolicy] = []
    for name in names:
        policy = next((p for p in config.policies if p.name == name), None)
        if policy is None:
            known = ", ".join(p.name for p in config.policies) or "none"
            raise ValueError(
                f"--policy-from-config {name}: the config has no such policy ({known})"
            )
        if not policy.levels:
            raise ValueError(
                f"--policy-from-config {name}: the policy has no levels, so its alerts are "
                "not named by scenario; write alert_on in labels.yaml instead"
            )
        alerting = [s for s in policy.scenario_configs() if policy.wake_for(s) is not Wake.never]
        found.append((name, policy.description, alerting, policy.known_people))
    return found


def cmd_eval(
    dataset_path: Path | list[Path],
    *,
    policies: list[str] | None,
    judges: list[str] | None,
    as_json: bool,
    cost: CostModel,
    config_path: Path | None = None,
    include_drafts: bool = False,
    people: bool = False,
    known_people: str | None = None,
    from_config: list[str] | None = None,
    database: Path | None = None,
) -> int:
    from vidette.people.engine import EngineUnavailableError
    from vidette.people.models import ModelError
    from vidette.people.store import StoreError

    try:
        paths = dataset_path if isinstance(dataset_path, list) else [dataset_path]
        sets = [load_dataset(path, include_drafts=include_drafts) for path in paths]
        if from_config:
            config, _source = cli_config(config_path)
            for name, description, alert_on, known in config_policies(config, from_config):
                sets = [with_policy(d, name, description, alert_on, known) for d in sets]
        dataset = union(sets)
        chosen = _judges(judges or list(BASELINES), config_path, dataset, database)
        policy_names = policies or (from_config or list(dataset.labels.policies))
        enrich = _face_check(config_path) if people else None
        runs = asyncio.run(
            run_eval(dataset, policy_names, chosen, enrich=enrich, known_people=known_people)
        )
    except (
        DatasetError, ValueError, ConfigError, VlmUnavailableError, StoreError, ModelError,
        EngineUnavailableError, FileNotFoundError,
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
