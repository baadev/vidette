"""Labelled clip sets for `vidette eval` (format: docs/eval.md).

A dataset is a directory with a `labels.yaml` and the media it points at:

    my-set/
      labels.yaml
      clips/2026-11-14_2103_porch.mp4
      frames/2026-11-14_2103_porch/001.jpg …

`labels.yaml` says what happened in each entry (`scenario`, from the set's own
vocabulary), which scenarios each policy should alert on (`policies.<name>.alert_on`, the
ground truth), and optionally what the upstream NVR reported (`upstream`), so that the
Frigate baselines can be scored on the same entries.

Validation is strict, like Vidette's config: unknown keys, unknown scenarios, duplicate
ids and missing or escaping media paths are errors with the entry id in the message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from vidette.core.incidents import Incident, UpstreamSummary
from vidette.verdicts.base import Case, PolicyText

LABELS_FILE = "labels.yaml"
UNLABELLED = "unlabelled"  # reserved scenario: present in the set, not scored yet
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})
MEDIA_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"}) | VIDEO_SUFFIXES

_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class DatasetError(Exception):
    """The dataset cannot be loaded; the message says what to fix."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetPolicy(_Strict):
    description: str  # what a judge is told, in plain language
    alert_on: list[str] = Field(min_length=1)  # ground truth: scenarios that should alert
    known_people: Literal["judge", "quiet"] = "judge"  # with --people (docs/people.md)


class UpstreamFacts(_Strict):
    """What the upstream NVR said about the entry, if it saw it (all optional)."""

    severity: Literal["alert", "detection"] | None = None
    objects: list[str] = Field(default_factory=list)
    zones: list[str] = Field(default_factory=list)
    sub_labels: list[str] = Field(default_factory=list)
    threat_level: int | None = Field(default=None, ge=0, le=2)  # Frigate GenAI, 0.17+
    summary: str | None = None


class DraftInfo(_Strict):
    """What a machine proposed for an entry (`vidette label`), kept after review so a
    report can say how many truth labels are model drafts accepted unchanged."""

    by: str  # judge and model, e.g. "vlm-local qwen2.5vl:7b"
    at: datetime | None = None
    scenario: str  # the proposal: a scenario, or "unclear"
    alert: bool | None = None  # under the set's first policy, for the reviewer's eye
    description: str | None = None  # one line about what is visible
    confidence: float | None = Field(default=None, ge=0, le=1)


class Entry(_Strict):
    id: str = Field(pattern=_ID_RE)
    scenario: str
    # draft: written by a machine, not ground truth until a person reviews it;
    # reviewed: a person confirmed or corrected it. Absent: written by hand.
    status: Literal["draft", "reviewed"] | None = None
    draft: DraftInfo | None = None
    number: int | None = Field(default=None, ge=1)  # on the review contact sheets
    camera: str = "camera"
    at: datetime | None = None  # when it happened; naive values are read as UTC
    duration_s: float | None = Field(default=None, ge=0)
    media: list[str] = Field(default_factory=list)  # paths relative to the dataset dir
    upstream: UpstreamFacts | None = None
    notes: str | None = None
    # why a person said an alert was wrong or not worth it (a code from the bot's list,
    # e.g. passing, light, stalled, unimportant); not scored, read by the analysis
    reason: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,30}$")


class Labels(_Strict):
    version: Literal[1] = 1
    name: str
    description: str | None = None
    license: str  # say it out loud: "Apache-2.0 (synthetic)", "private, never publish", …
    observed_hours: float | None = Field(default=None, gt=0)  # wall-clock span the set covers
    scenarios: dict[str, str] = Field(min_length=1)  # vocabulary: label → what it means
    policies: dict[str, DatasetPolicy] = Field(min_length=1)
    # Extra words for reviewing by number ("3 courier"); built-ins cover the starter set.
    aliases: dict[str, str] = Field(default_factory=dict)
    entries: list[Entry] = Field(default_factory=list)

    @field_validator("entries", mode="before")
    @classmethod
    def _bare_entries_key(cls, value: object) -> object:
        return [] if value is None else value  # `entries:` with nothing under it yet

    @model_validator(mode="after")
    def _consistent(self) -> Labels:
        if UNLABELLED in self.scenarios:
            raise ValueError(f"'{UNLABELLED}' is reserved; do not list it under scenarios")
        known = set(self.scenarios)
        for name, policy in self.policies.items():
            if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
                raise ValueError(f"policy name '{name}' must match [a-z0-9-]")
            unknown = [label for label in policy.alert_on if label not in known]
            if unknown:
                raise ValueError(
                    f"policy '{name}' alert_on names unknown scenarios {unknown}; add them "
                    "under scenarios"
                )
        for alias, scenario in self.aliases.items():
            if scenario not in known:
                raise ValueError(f"alias '{alias}' points to unknown scenario '{scenario}'")
        seen: set[str] = set()
        numbers: set[int] = set()
        for entry in self.entries:
            if entry.id in seen:
                raise ValueError(f"entry id '{entry.id}' is used twice")
            seen.add(entry.id)
            if entry.number is not None:
                if entry.number in numbers:
                    raise ValueError(f"review number {entry.number} is used twice")
                numbers.add(entry.number)
            if entry.scenario != UNLABELLED and entry.scenario not in known:
                raise ValueError(
                    f"entry '{entry.id}': scenario '{entry.scenario}' is not under scenarios "
                    f"(or use '{UNLABELLED}' until you have looked at it)"
                )
        return self


@dataclass(frozen=True)
class Dataset:
    root: Path
    labels: Labels
    include_drafts: bool = False  # score machine drafts as if they were truth (loudly)

    @property
    def scored_entries(self) -> list[Entry]:
        return [
            entry
            for entry in self.labels.entries
            if entry.scenario != UNLABELLED
            and (self.include_drafts or entry.status != "draft")
        ]

    @property
    def unlabelled_count(self) -> int:
        return sum(1 for entry in self.labels.entries if entry.scenario == UNLABELLED)

    @property
    def draft_count(self) -> int:
        """Machine drafts with a scenario, not reviewed yet."""
        return sum(
            1
            for entry in self.labels.entries
            if entry.status == "draft" and entry.scenario != UNLABELLED
        )

    @property
    def accepted_unchanged_count(self) -> int:
        """Scored entries whose truth is a model draft a reviewer accepted as it was."""
        return sum(
            1
            for entry in self.scored_entries
            if entry.status == "reviewed"
            and entry.draft is not None
            and entry.draft.scenario == entry.scenario
        )

    def policy(self, name: str) -> DatasetPolicy:
        try:
            return self.labels.policies[name]
        except KeyError:
            known = ", ".join(sorted(self.labels.policies))
            raise DatasetError(f"no policy '{name}' in {self.root}; available: {known}") from None

    def policy_text(self, name: str) -> PolicyText:
        policy = self.policy(name)
        return PolicyText(
            name=name, description=policy.description, known_people=policy.known_people
        )

    def expected_alert(self, entry: Entry, policy_name: str) -> bool:
        return entry.scenario in self.policy(policy_name).alert_on

    def case(self, entry: Entry) -> Case:
        return Case(
            incident=entry_incident(entry),
            media=tuple(self.root / item for item in entry.media),
        )


def entry_incident(entry: Entry) -> Incident:
    """The same Incident a live Frigate review would produce, built from the labels."""
    started = 0.0
    if entry.at is not None:
        at = entry.at if entry.at.tzinfo is not None else entry.at.replace(tzinfo=UTC)
        started = at.timestamp()
    upstream = entry.upstream or UpstreamFacts()
    summary = None
    if upstream.threat_level is not None or upstream.summary is not None:
        summary = UpstreamSummary(text=upstream.summary, threat_level=upstream.threat_level)
    return Incident(
        source="dataset",
        source_id=entry.id,
        camera=entry.camera,
        started_at=started,
        ended_at=None if entry.duration_s is None else started + entry.duration_s,
        severity=upstream.severity,
        objects=list(upstream.objects),
        sub_labels=list(upstream.sub_labels),
        zones=list(upstream.zones),
        upstream_summary=summary,
    )


def _check_media(root: Path, labels: Labels) -> None:
    resolved_root = root.resolve()
    problems: list[str] = []
    for entry in labels.entries:
        for item in entry.media:
            path = (root / item).resolve()
            if not path.is_relative_to(resolved_root):
                problems.append(f"entry '{entry.id}': media '{item}' points outside the dataset")
            elif not path.is_file():
                problems.append(f"entry '{entry.id}': media '{item}' does not exist")
            elif path.suffix.lower() not in MEDIA_SUFFIXES:
                problems.append(
                    f"entry '{entry.id}': media '{item}' is not an image or video "
                    f"({', '.join(sorted(MEDIA_SUFFIXES))})"
                )
    if problems:
        raise DatasetError("\n  - ".join([f"{root / LABELS_FILE} has media problems:", *problems]))


def load_dataset(path: Path, *, include_drafts: bool = False) -> Dataset:
    """Load `path` (a dataset directory or its labels.yaml); raises DatasetError."""
    labels_path = path / LABELS_FILE if path.is_dir() else path
    root = labels_path.parent
    try:
        raw = yaml.safe_load(labels_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"cannot read {labels_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise DatasetError(f"{labels_path} is not valid YAML: {exc}") from exc
    try:
        labels = Labels.model_validate(raw if raw is not None else {})
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(part) for part in issue['loc']) or '<root>'}: {issue['msg']}"
            for issue in exc.errors()
        ]
        raise DatasetError("\n  - ".join([f"{labels_path} is invalid:", *issues])) from exc
    _check_media(root, labels)
    return Dataset(root=root, labels=labels, include_drafts=include_drafts)


def scaffold_entries(root: Path, labels_path: Path | None = None) -> list[str]:
    """Video clips under `root` not referenced by any entry, as YAML entry snippets.

    For labelling a folder of exported clips: each new clip becomes an `unlabelled`
    entry to edit by hand. Still images are left alone (they usually sit in per-entry
    frame folders). Returns the snippets; nothing is written.
    """
    labels_file = labels_path or root / LABELS_FILE
    referenced: set[str] = set()
    if labels_file.is_file():
        raw = yaml.safe_load(labels_file.read_text(encoding="utf-8")) or {}
        for entry in raw.get("entries") or []:
            if isinstance(entry, dict):
                referenced.update(str(item) for item in entry.get("media") or [])
    snippets: list[str] = []
    used_ids: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        if relative in referenced:
            continue
        base_id = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-.") or "entry"
        entry_id, n = base_id, 2
        while entry_id in used_ids:
            entry_id, n = f"{base_id}-{n}", n + 1
        used_ids.add(entry_id)
        snippets.append(
            f"  - id: {entry_id}\n"
            f"    scenario: {UNLABELLED}\n"
            f"    media: [{json.dumps(relative)}]\n"
            f"    # at: 2026-11-14T21:03:00+02:00\n"
            f"    # notes: what you see in the clip\n"
        )
    return snippets
