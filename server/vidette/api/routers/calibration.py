"""Calibration on the web app's System page (ADR-0020, docs/calibration.md).

`GET /api/v1/calibration` (read:config): the rule's numbers and every step the marks made,
newest first, with what is in force now. `POST /api/v1/calibration/{id}/revert`
(write:config): take a step back (and the later steps of the same scenario and camera);
`404` when it is not in force. Each change is a `calibration.changed` system event.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.auth.service import Principal
from vidette.calibration.rules import protected
from vidette.db import CalibrationRow
from vidette.runtime import AppRuntime

router = APIRouter(prefix="/api/v1/calibration", tags=["calibration"])

_REQUIRE_READ_CONFIG = require_scope("read:config")
_REQUIRE_WRITE_CONFIG = require_scope("write:config")
_SHOWN = 50


class RuleOut(BaseModel):
    enabled: bool
    marks: int
    window: str
    bar: float
    protected_bar: float
    protected: list[str]  # scenarios that never leave the alerts


class StepOut(BaseModel):
    id: int
    at: float
    policy: str
    scenario: str
    scenario_title: str
    camera: str
    camera_name: str
    step_from: int
    step_to: int
    bar: float | None
    evidence: dict[str, Any]
    in_force: bool  # the step that decides this scenario on this camera now
    reverted_at: float | None
    reverted_by: str | None


class CalibrationOut(BaseModel):
    rule: RuleOut
    steps: list[StepOut]


class RevertOut(BaseModel):
    reverted: list[int]


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _title(rt: AppRuntime, policy: str, scenario: str) -> str:
    config = next((p for p in rt.config.policies if p.name == policy), None)
    found = None if config is None else config.scenario_configs().get(scenario)
    return found.title if found is not None and found.title else scenario.replace("_", " ")


def _camera_name(rt: AppRuntime, camera: str) -> str:
    telegram = rt.config.notifications.telegram
    return telegram.camera_names.get(camera, camera) if telegram is not None else camera


def _step(rt: AppRuntime, row: CalibrationRow, in_force: set[int]) -> StepOut:
    return StepOut(
        id=row.id, at=row.at, policy=row.policy, scenario=row.scenario,
        scenario_title=_title(rt, row.policy, row.scenario), camera=row.camera,
        camera_name=_camera_name(rt, row.camera), step_from=row.step_from,
        step_to=row.step_to, bar=row.bar, evidence=row.evidence, in_force=row.id in in_force,
        reverted_at=row.reverted_at, reverted_by=row.reverted_by,
    )  # fmt: skip


@router.get("")
async def get_calibration(
    request: Request, principal: Annotated[Principal, Depends(_REQUIRE_READ_CONFIG)]
) -> CalibrationOut:
    rt = _runtime(request)
    config = rt.config.calibration
    states = await rt.calibration.states()
    in_force = {s.step_id for s in states.values() if s.step_id is not None}
    rows = await rt.db.calibration_steps(limit=_SHOWN)
    shielded = sorted(
        {
            scenario
            for policy in rt.config.policies
            for scenario in policy.scenario_configs()
            if protected(policy, scenario, rt.calibration.rules.protect)
        }
    )
    return CalibrationOut(
        rule=RuleOut(
            enabled=config.enabled,
            marks=config.marks,
            window=config.window,
            bar=config.bar,
            protected_bar=config.protected_bar,
            protected=shielded,
        ),
        steps=[_step(rt, row, in_force) for row in reversed(rows)],
    )


@router.post("/{step_id}/revert")
async def revert_step(
    step_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_REQUIRE_WRITE_CONFIG)],
) -> RevertOut:
    taken = await _runtime(request).calibration.revert(step_id, "web")
    if not taken:
        raise problem(
            404, "Not in force", "this step was already taken back, or there is no such step"
        )
    return RevertOut(reverted=[row.id for row in taken])
