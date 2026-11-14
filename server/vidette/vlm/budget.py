"""Daily call budget and per-minute pace for VLM calls.

The budget is a hard stop, not a hint: when a backend has made `max_calls_per_day` calls
today (local calendar day), the next judgment abstains with "daily VLM budget reached"
instead of calling. Abstentions never notify, so a runaway loop costs at most one day's
budget. The ledger also keeps the day's estimated spend for the System page.

Ledgers: `FileLedger` for the command-line tools (shared by `label` and `eval`, default
`~/.local/state/vidette/vlm-usage.json`), `MetaLedger` for the server (the database's meta
table), `MemoryLedger` for tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

KEEP_DAYS = 31


def default_ledger_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "vidette" / "vlm-usage.json"


@dataclass(frozen=True)
class DayUsage:
    calls: int = 0
    usd: float = 0.0


class UsageLedger(Protocol):
    async def today(self, backend: str) -> DayUsage: ...

    async def add(self, backend: str, *, calls: int, usd: float) -> None: ...


def _today(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock()).date().isoformat()


def _prune(days: dict[str, Any], today: str) -> dict[str, Any]:
    cutoff = date.fromisoformat(today).toordinal() - KEEP_DAYS
    return {
        day: value
        for day, value in days.items()
        if date.fromisoformat(day).toordinal() > cutoff
    }


class MemoryLedger:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.days: dict[str, dict[str, dict[str, float]]] = {}

    async def today(self, backend: str) -> DayUsage:
        entry = self.days.get(_today(self._clock), {}).get(backend, {})
        return DayUsage(calls=int(entry.get("calls", 0)), usd=float(entry.get("usd", 0.0)))

    async def add(self, backend: str, *, calls: int, usd: float) -> None:
        entry = self.days.setdefault(_today(self._clock), {}).setdefault(backend, {})
        entry["calls"] = entry.get("calls", 0) + calls
        entry["usd"] = entry.get("usd", 0.0) + usd


class FileLedger:
    """A small JSON file; concurrent CLI runs can lose an increment, never corrupt it."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def today(self, backend: str) -> DayUsage:
        entry = self._read().get(_today(self._clock), {}).get(backend, {})
        return DayUsage(calls=int(entry.get("calls", 0)), usd=float(entry.get("usd", 0.0)))

    async def add(self, backend: str, *, calls: int, usd: float) -> None:
        async with self._lock:
            today = _today(self._clock)
            data = _prune(self._read(), today)
            entry = data.setdefault(today, {}).setdefault(backend, {})
            entry["calls"] = int(entry.get("calls", 0)) + calls
            entry["usd"] = round(float(entry.get("usd", 0.0)) + usd, 6)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)


class MetaStore(Protocol):
    async def get_meta(self, key: str) -> str | None: ...

    async def set_meta(self, key: str, value: str) -> None: ...


class MetaLedger:
    """The server's ledger: one meta row per day (`vlm.usage.<YYYY-MM-DD>`)."""

    def __init__(self, store: MetaStore, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._lock = asyncio.Lock()

    def _key(self) -> str:
        return f"vlm.usage.{_today(self._clock)}"

    async def _read(self) -> dict[str, Any]:
        raw = await self._store.get_meta(self._key())
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def today(self, backend: str) -> DayUsage:
        entry = (await self._read()).get(backend, {})
        return DayUsage(calls=int(entry.get("calls", 0)), usd=float(entry.get("usd", 0.0)))

    async def add(self, backend: str, *, calls: int, usd: float) -> None:
        async with self._lock:
            data = await self._read()
            entry = data.setdefault(backend, {})
            entry["calls"] = int(entry.get("calls", 0)) + calls
            entry["usd"] = round(float(entry.get("usd", 0.0)) + usd, 6)
            await self._store.set_meta(self._key(), json.dumps(data, sort_keys=True))


class Pace:
    """At most `per_minute` calls in any 60 s window; callers wait their turn."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._recent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> float:
        """Returns how long it waited."""
        waited = 0.0
        async with self._lock:
            while True:
                now = self._clock()
                while self._recent and now - self._recent[0] >= 60.0:
                    self._recent.popleft()
                if len(self._recent) < self._per_minute:
                    self._recent.append(now)
                    return waited
                delay = 60.0 - (now - self._recent[0]) + 0.01
                waited += delay
                await asyncio.sleep(delay)
