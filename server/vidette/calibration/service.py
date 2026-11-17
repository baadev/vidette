"""Calibration in `vidette serve` (ADR-0020): after every 👍/👎, look again; store,
announce and take back steps.

    a mark (Telegram buttons, the web page) ─▶ review(): the marks of the window and the
        steps in force ─▶ rules.propose ─▶ new steps / steps taken back, stored
        (`calibration` table) ─▶ `calibration.changed` system event ─▶ the bot says it
        in one line, with a button that takes it back

    the verdict worker, for each alert ─▶ effect(): does a step in force send it to the
        digest? ─▶ `intent.calibration` on the event ─▶ the bot's routing reads it

The steps are read from the database for every alert (one small query), so a step taken
back from the command line or the web page counts for the next alert. Nothing here
writes the config: the policy file stays exactly as its author wrote it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from vidette.calibration.rules import (
    Effect,
    KeyState,
    Rules,
    effect,
    marks_from,
    propose,
    state_from,
)
from vidette.core.config import PolicyConfig, VidetteConfig
from vidette.db import CalibrationRow, Database

logger = logging.getLogger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class Change:
    """What one review did: a new step, or a step taken back by a 👍 on what it hid."""

    kind: Literal["step", "restored"]
    row: CalibrationRow
    evidence: dict[str, Any]


Announce = Callable[[list[Change]], Awaitable[None]]


async def _no_emit(_kind: str, _payload: dict[str, Any]) -> None:
    return None


class Calibrator:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        *,
        emit: EmitCallback = _no_emit,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = config.calibration.enabled
        self.rules = Rules.from_config(config.calibration)
        self.policies: dict[str, PolicyConfig] = {p.name: p for p in config.policies}
        self.db = db
        self._emit = emit
        self._clock = clock
        self._lock = asyncio.Lock()
        self.announce: Announce | None = None  # the bot, when there is one

    async def states(self) -> dict[tuple[str, str, str], KeyState]:
        return state_from(await self.db.calibration_steps())

    async def effect(
        self,
        policy: PolicyConfig,
        verdict: Mapping[str, Any],
        camera: str,
    ) -> Effect | None:
        """Whether an alert of `policy` naming `verdict.label` on `camera` goes to the
        digest because of a step in force. Never raises: a broken lookup wakes."""
        if not self.enabled or not policy.levels:
            return None
        scenario = verdict.get("label")
        if not isinstance(scenario, str):
            return None
        try:
            states = await self.states()
        except Exception:
            logger.exception("calibration: steps not read; the alert wakes as written")
            return None
        confidence = verdict.get("confidence")
        level = verdict.get("level")
        return effect(
            states.get((policy.name, scenario, camera)),
            policy,
            scenario,
            level=level if isinstance(level, str) else None,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
            rules=self.rules,
        )

    async def review(self) -> list[Change]:
        """Look at the marks again and make the steps they ask for. Called after every
        mark; cheap (the marked events of the window and the steps)."""
        if not self.enabled:
            return []
        changes: list[Change] = []
        async with self._lock:
            now = self._clock()
            rows = await self.db.feedback_events(since_ts=now - self.rules.window_s - 86400)
            marks = marks_from(rows)
            states = await self.states()
            for proposal in propose(marks, states, self.rules, self.policies, now=now):
                if proposal.undo is not None:
                    taken = await self.db.revert_calibration_step(proposal.undo, "mark")
                    if taken:
                        changes.append(Change("restored", taken[0], proposal.evidence))
                    continue
                row = await self.db.add_calibration_step(
                    proposal.key, proposal.step_from, proposal.step_to,
                    bar=proposal.bar, evidence=proposal.evidence,
                )  # fmt: skip
                changes.append(Change("step", row, proposal.evidence))
        for change in changes:
            await self._emit("calibration.changed", _payload(change.kind, change.row))
            logger.info(
                "calibration: %s %s/%s on %s: step %d -> %d",
                change.kind, *change.row.key, change.row.step_from,
                change.row.step_to if change.kind == "step" else change.row.step_from,
            )  # fmt: skip
        if changes and self.announce is not None:
            try:
                await self.announce(changes)
            except Exception:
                logger.exception("calibration: the change was stored but not announced")
        return changes

    async def revert(self, step_id: int, by: str) -> list[CalibrationRow]:
        """Take a step back (and the later steps of its key); `by` says who: web,
        telegram, cli. Empty when it was not in force."""
        async with self._lock:
            taken = await self.db.revert_calibration_step(step_id, by)
        for row in taken:
            await self._emit("calibration.changed", {**_payload("reverted", row), "by": by})
        return taken


def _payload(kind: str, row: CalibrationRow) -> dict[str, Any]:
    return {
        "change": kind,
        "id": row.id,
        "policy": row.policy,
        "scenario": row.scenario,
        "camera": row.camera,
        "step_from": row.step_from,
        "step_to": row.step_to,
        "bar": row.bar,
    }


async def safe_review(calibrator: Calibrator | None) -> list[Change]:
    """`review()` for callers that must never fail because of it (a button, a request)."""
    if calibrator is None:
        return []
    try:
        return await calibrator.review()
    except Exception:
        logger.exception("calibration: review failed; nothing changed")
        return []
