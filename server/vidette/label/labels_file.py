"""Editing `labels.yaml` without destroying what a person wrote in it.

A YAML library would round-trip the values and drop every comment. Instead, this module
edits text: it appends entry blocks under `entries:` and replaces whole blocks that the
tool itself wrote (entries with a review `number`), and it leaves every other byte alone.
Every edit is validated by loading the result as a dataset before the file is replaced
(atomically, with the previous version kept as `labels.yaml.bak`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.eval.dataset import LABELS_FILE, Entry, load_dataset

_ITEM_RE = re.compile(r"^(?P<indent>\s*)- id:\s*(?P<id>[^\s#]+)\s*(?:#.*)?$")
_ENTRIES_RE = re.compile(r"^entries:\s*(?P<rest>.*)$")
_OBSERVED_MANAGED_RE = re.compile(r"^(#\s*)?observed_hours:.*#\s*vidette-managed.*$")
MANAGED_NOTE = "# vidette-managed: watched hours from vidette collect"

TEMPLATE = """\
# Labels for `vidette eval` (format: docs/eval.md). Created by `vidette label`.
#
# Entries with `status: draft` were proposed by a model and are NOT ground truth until
# you review them (contact sheets in review/, then `vidette label apply`). Never commit
# this folder: it is real footage.

version: 1
name: {name}
description: Clips collected by vidette collect; review the drafts before trusting a number.
license: private, never publish
{observed}

scenarios:
  passer_by: someone walks past on the street or sidewalk and never approaches
  courier_at_door: a delivery; walks up, leaves a parcel, leaves
  visitor_at_door: rings or knocks and waits
  door_handle_attempt: tries the door handle, a door or a window
  lingering_at_door: stays at the door or a window with no visible errand, peers in
  household_member: someone who lives here
  cat: a cat
  dog: a dog
  animal: another animal
  car_passing: a car drives by
  car_lights: only headlights sweep the scene; no person
  night_noise: infrared noise, insects, rain or shadows; nothing real

policies:
  entry-interest:
    description: >
      Alert me when someone shows interest in getting in: tries the door handle, lingers
      at the door or windows, peers inside. Ignore people passing by on the street,
      couriers dropping off a parcel, animals and car headlights.
    alert_on: [door_handle_attempt, lingering_at_door]

# Extra words for reviewing by number ("3 courier"); common ones are built in.
aliases: {{}}

entries: []
"""


def observed_line(hours: float | None) -> str:
    if hours is None or hours <= 0:
        return f"# observed_hours: 0  {MANAGED_NOTE}"
    return f"observed_hours: {hours:.2f}  {MANAGED_NOTE}"


def create(root: Path, *, observed_hours: float | None) -> Path:
    path = root / LABELS_FILE
    path.write_text(
        TEMPLATE.format(name=_yaml_str(root.name), observed=observed_line(observed_hours)),
        encoding="utf-8",
    )
    return path


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


_PLAIN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_./-]*|\d{8}-\d{6}(?:-\d+)?)$")
_YAML_WORDS = {"true", "false", "yes", "no", "on", "off", "null", "y", "n"}


def _text(value: str) -> str:
    """Plain when YAML would read it back as the same string, quoted otherwise."""
    if _PLAIN_RE.match(value) and value.lower() not in _YAML_WORDS:
        return value
    return _yaml_str(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return _text(str(value))


def render_entry(entry: Mapping[str, Any], indent: str = "  ") -> str:
    """One entry as a YAML list item, house style: scalars inline, media as a flow list,
    `draft` as a nested mapping. Keys in a stable order; None values are left out."""
    order = ["id", "scenario", "status", "number", "camera", "at", "duration_s", "media",
             "upstream", "notes", "reason", "draft"]  # fmt: skip
    keys = [key for key in order if key in entry] + [key for key in entry if key not in order]
    lines: list[str] = []
    for key in keys:
        value = entry[key]
        if value is None:
            continue
        prefix = f"{indent}- " if not lines else f"{indent}  "
        if isinstance(value, list):
            items = ", ".join(_text(str(item)) for item in value)
            lines.append(f"{prefix}{key}: [{items}]")
        elif isinstance(value, Mapping):
            lines.append(f"{prefix}{key}:")
            for sub_key, sub_value in value.items():
                if sub_value is None:
                    continue
                if isinstance(sub_value, list):
                    items = ", ".join(_text(str(item)) for item in sub_value)
                    lines.append(f"{indent}    {sub_key}: [{items}]")
                else:
                    lines.append(f"{indent}    {sub_key}: {_scalar(sub_value)}")
        else:
            lines.append(f"{prefix}{key}: {_scalar(value)}")
    return "\n".join(lines) + "\n"


def entry_dict(entry: Entry) -> dict[str, Any]:
    data: dict[str, Any] = entry.model_dump(mode="python", exclude_none=True)
    if entry.camera == "camera":
        data.pop("camera", None)
    if not data.get("media"):
        data.pop("media", None)
    return data


def _entries_line(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if _ENTRIES_RE.match(line):
            return index
    raise ValueError("labels.yaml has no top-level `entries:` key")


def item_spans(text: str) -> dict[str, tuple[int, int]]:
    """Entry id → (first line, line after the block), for block-style entries."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        match = _ITEM_RE.match(line.rstrip("\n"))
        if match:
            starts.append((index, match.group("id"), len(match.group("indent"))))
    spans: dict[str, tuple[int, int]] = {}
    for position, (start, entry_id, indent) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            line_indent = len(lines[index]) - len(lines[index].lstrip())
            if stripped and not stripped.startswith("#") and line_indent <= indent:
                end = index  # a dedent: the next top-level key
                break
        while end - 1 > start and not lines[end - 1].strip():
            end -= 1  # trailing blank lines stay outside the block
        spans[entry_id.strip("'\"")] = (start, end)
    return spans


def append_entries(text: str, blocks: list[str]) -> str:
    if not blocks:
        return text
    lines = text.splitlines(keepends=True)
    index = _entries_line(lines)
    rest = _ENTRIES_RE.match(lines[index].rstrip("\n"))
    assert rest is not None
    if rest.group("rest").strip() == "[]":
        lines[index] = "entries:\n"
    elif rest.group("rest").strip() and not rest.group("rest").strip().startswith("#"):
        raise ValueError("`entries:` is written inline; turn it into a block list first")
    spans = item_spans("".join(lines))
    insert_at = max((end for _start, end in spans.values()), default=index + 1)
    # Entries normally end the file; if a key follows them, insert before it.
    new = "".join(blocks)
    lines[insert_at:insert_at] = [new if new.endswith("\n") else new + "\n"]
    out = "".join(lines)
    return out if out.endswith("\n") else out + "\n"


def replace_entry(text: str, entry_id: str, block: str) -> str:
    spans = item_spans(text)
    if entry_id not in spans:
        raise KeyError(entry_id)
    start, end = spans[entry_id]
    lines = text.splitlines(keepends=True)
    lines[start:end] = [block]
    return "".join(lines)


_SCENARIOS_RE = re.compile(r"^scenarios:\s*(?:#.*)?$")


def add_scenarios(text: str, scenarios: Mapping[str, str]) -> str:
    """Append scenarios to the `scenarios:` block (block style only); ones already there
    keep their wording. Every other byte stays as it was."""
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if _SCENARIOS_RE.match(line.rstrip("\n"))), None
    )
    if start is None:
        raise ValueError("labels.yaml has no block-style `scenarios:` key")
    end = start + 1
    indent: str | None = None
    existing: set[str] = set()
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[0].isspace():
            break  # the next top-level key or a top-level comment
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        own = line[: len(line) - len(line.lstrip())]
        if indent is None:
            indent = own
        if own == indent:  # a key; deeper lines continue a folded description
            existing.add(stripped.split(":", 1)[0].strip().strip("'\""))
        end = index + 1
    indent = indent or "  "
    new = [
        f"{indent}{key}: {_text(' '.join(value.split()))}\n"
        for key, value in scenarios.items()
        if key not in existing
    ]
    if not new:
        return text
    if end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines[end:end] = new
    return "".join(lines)


def set_observed_hours(text: str, hours: float) -> tuple[str, bool]:
    """Refresh the tool-managed `observed_hours` line; a hand-set value is left alone."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _OBSERVED_MANAGED_RE.match(line.rstrip("\n")):
            new = observed_line(hours) + "\n"
            if lines[index] == new:
                return text, False
            lines[index] = new
            return "".join(lines), True
    return text, False


def write_validated(root: Path, text: str) -> None:
    """Replace labels.yaml only if the new text loads as a valid dataset."""
    path = root / LABELS_FILE
    candidate = root / f".{LABELS_FILE}.new"
    candidate.write_text(text, encoding="utf-8")
    try:
        load_dataset(candidate)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    if path.exists():
        backup = root / f"{LABELS_FILE}.bak"
        backup.write_bytes(path.read_bytes())
    candidate.replace(path)
