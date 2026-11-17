"""One model slot, shared by everything in this process that talks to the same server.

A shared llama.cpp server gives each client a number of parallel slots; Vidette may be
allowed exactly one (ADR-0019). The live judge, the assistant and the deep analysis all
want it. `SlotGate` hands it out:

- **live** callers (a verdict someone may be woken by, a question someone is waiting
  for) take it in arrival order;
- a **background** caller (deep analysis) takes it only when it is free and no live call
  waits, and gives it back the moment one arrives: the gate cancels the background
  request (`PREEMPTED`), the caller puts its job back and tries later. A live verdict then
  waits for nothing but the server;
- a **turn** (a background request with `preemptible=False`, deep analysis's daytime
  trickle) queues in arrival order like a live call and is not cancelled: when the live
  judge never rests, it waits for one such request now and then instead of starving it.

`slot_state` asks a llama.cpp server how many of its slots are busy right now (`GET
/slots`, with the API key when there is one), so a background call is sent only when one
of them is idle: never into another tenant's share. Servers without the endpoint
(Ollama, hosted APIs) answer None, and the caller decides.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from vidette.vlm.endpoint import VlmEndpoint

logger = logging.getLogger(__name__)

PREEMPTED = "preempted by a live call"


class SlotGate:
    """See the module docstring. Not thread-safe: one event loop."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._live_waiting = 0
        self._background: asyncio.Task[object] | None = None
        self.live_calls = 0
        self.background_calls = 0
        self.preemptions = 0

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def live_waiting(self) -> int:
        return self._live_waiting

    @property
    def idle(self) -> bool:
        """Nobody holds the slot and no live call waits for it (the pre-filter's `later`
        clips are judged only then)."""
        return not self._lock.locked() and not self._live_waiting

    @contextlib.asynccontextmanager
    async def live(self) -> AsyncIterator[None]:
        self._live_waiting += 1
        try:
            task = self._background
            if task is not None and not task.done():
                task.cancel(PREEMPTED)
                self.preemptions += 1
            await self._lock.acquire()
        finally:
            self._live_waiting -= 1
        try:
            self.live_calls += 1
            yield
        finally:
            self._lock.release()

    @contextlib.asynccontextmanager
    async def background(
        self, *, poll_s: float = 0.5, preemptible: bool = True
    ) -> AsyncIterator[None]:
        """Wait until the slot is free and no live call waits, then hold it. The holding
        task is cancelled with `PREEMPTED` when a live call arrives.

        `preemptible=False` is a turn of its own (deep analysis's daytime trickle): it
        queues for the slot in arrival order like a live call and is not cancelled, so a
        busy live judge waits for one background request instead of starving it."""
        if not preemptible:
            await self._lock.acquire()
            try:
                self.background_calls += 1
                yield
            finally:
                self._lock.release()
            return
        while self._live_waiting or self._lock.locked():
            await asyncio.sleep(poll_s)
        await self._lock.acquire()  # free: returns at once, no other task runs in between
        self._background = asyncio.current_task()
        try:
            self.background_calls += 1
            yield
        finally:
            self._background = None
            self._lock.release()


def was_preempted(task: asyncio.Task[object]) -> bool:
    """Whether a finished task was cancelled by `SlotGate.live` (not by shutdown)."""
    if not task.cancelled():
        return False
    try:
        task.result()
    except asyncio.CancelledError as exc:
        return PREEMPTED in str(exc.args[0] if exc.args else "")
    return False


@dataclass(frozen=True)
class SlotState:
    total: int
    busy: int

    @property
    def idle(self) -> int:
        return self.total - self.busy


async def slot_state(
    endpoint: VlmEndpoint, http: httpx.AsyncClient, *, timeout_s: float = 10.0
) -> SlotState | None:
    """How many of a llama.cpp server's slots are processing now; None when the server
    has no such endpoint or cannot be asked (the reason is logged, never the key)."""
    base = endpoint.base_url.rstrip("/")
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    headers = {"authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    try:
        response = await http.get(f"{root}/slots", headers=headers, timeout=timeout_s)
    except httpx.HTTPError as exc:
        logger.info("slots of %s unknown: %s", endpoint.host, type(exc).__name__)
        return None
    if not response.is_success:
        logger.info("slots of %s unknown: HTTP %d", endpoint.host, response.status_code)
        return None
    try:
        slots = response.json()
    except ValueError:
        return None
    if not isinstance(slots, list) or not slots:
        return None
    busy = sum(1 for slot in slots if isinstance(slot, dict) and slot.get("is_processing"))
    return SlotState(total=len(slots), busy=busy)
