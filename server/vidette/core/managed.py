"""UI-managed cameras: payload validation + merge into the effective config.

The hand-written YAML file is the IaC source of truth and is *never* rewritten by the UI.
Cameras created in the UI live in the `managed_cameras` table (vidette/db) and are merged
into the effective config at load/apply time. Two honesty rules govern the merge:

- on id collision the file wins, and the shadowed UI row is reported via a warning —
  never silently dropped;
- a stored row that no longer validates is skipped with a warning naming the camera and
  the error — a bad row must never prevent boot.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from vidette.core.config import CameraConfig, VidetteConfig
from vidette.db import ManagedCameraRow

# Same shape the config schema enforces for file cameras (ids end up in URLs and topics).
CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_camera_payload(camera_id: str, payload: dict[str, Any]) -> CameraConfig:
    """Validate a UI-supplied camera id + config dict into a CameraConfig.

    Raises ValueError with a single readable message (pydantic error locations joined
    dot-separated, the way core.config formats file errors) — the API surfaces it verbatim
    as a 422 problem, so it must stand on its own.
    """
    if not CAMERA_ID_RE.match(camera_id):
        raise ValueError(
            f"camera id '{camera_id}' must match [a-z0-9][a-z0-9-]* — lowercase letters, "
            "digits and dashes (it is used in URLs and topics)"
        )
    try:
        return CameraConfig.model_validate(payload)
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or 'config'}: {issue['msg']}"
            for issue in exc.errors()
        )
        raise ValueError(issues) from exc


def merge_managed_cameras(
    config: VidetteConfig, rows: Sequence[ManagedCameraRow]
) -> tuple[VidetteConfig, list[str]]:
    """Merge stored UI cameras into `config.cameras`; returns (merged config, warnings).

    `config` is not mutated. Never raises: a collision or an invalid stored row becomes a
    warning (the row is skipped), because a bad UI camera must not brick boot or shadow a
    working file config.
    """
    merged = dict(config.cameras)
    warnings: list[str] = []
    for row in rows:
        if row.id in config.cameras:
            warnings.append(
                f"cameras.{row.id}: defined in both the config file and the UI — the file "
                "wins; delete the UI copy to silence this"
            )
            continue
        try:
            merged[row.id] = validate_camera_payload(row.id, row.config)
        except ValueError as exc:
            warnings.append(
                f"cameras.{row.id}: stored UI camera is invalid and was skipped ({exc}) — "
                f"repair it via PUT /api/v1/config/cameras/{row.id} or delete it"
            )
    return config.model_copy(update={"cameras": merged}), warnings
