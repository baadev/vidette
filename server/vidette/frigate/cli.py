"""`vidette frigate check | tail | replay`: look at a Frigate install through Vidette's eyes.

- `check`  asks Frigate's API for its version and cameras (read-only)
- `tail`   subscribes to Frigate's MQTT topics and prints what Vidette makes of each
           message; `--record FILE` also saves the raw messages as JSON lines, which is
           how test fixtures and bug reports are captured
- `replay` does the same offline from such a file, no broker needed
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from vidette.core.config import ConfigError, FrigateConfig, load_config
from vidette.core.events import InProcessEventBus, Subscription
from vidette.frigate.client import FrigateClient, FrigateError
from vidette.frigate.source import FrigateSource, record_line, replay

MIN_FRIGATE = (0, 14)


def _load_frigate(config_path: Path | None) -> FrigateConfig | None:
    if config_path is None:
        return None
    config, _warnings = load_config(config_path)
    return config.frigate


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("-")[0].split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def describe(topic: str, payload: dict[str, Any]) -> str:
    """One human line per bus message."""
    at = payload.get("ended_at") or payload.get("started_at")
    clock = datetime.fromtimestamp(at).strftime("%H:%M:%S") if isinstance(at, float) else "--:--:--"
    camera = payload.get("camera", "?")
    if topic.startswith("incident."):
        objects = ",".join(payload.get("objects") or []) or "-"
        zones = ",".join(payload.get("zones") or [])
        where = f"@{zones}" if zones else ""
        summary = payload.get("upstream_summary") or {}
        extra = ""
        if summary:
            extra = f"  threat={summary.get('threat_level')} {summary.get('title') or ''}".rstrip()
        return (
            f"{clock} {topic:<20} {camera:<14} {payload.get('severity') or '-':<9} "
            f"{objects}{where}  frigate:{payload.get('source_id')}{extra}"
        )
    zones = ",".join(payload.get("current_zones") or [])
    return (
        f"{clock} {topic:<20} {camera:<14} {payload.get('label', '?')}"
        f"{'@' + zones if zones else ''}  stationary={payload.get('stationary')}"
    )


def _print_bus(bus: InProcessEventBus, out: TextIO) -> asyncio.Task[None]:
    """Subscribe now (eagerly, so nothing published from here on is missed) and print
    from a background task."""
    subscriptions = [
        bus.subscribe("incident.*"),
        bus.subscribe("object.*"),
        bus.subscribe("system.*"),
    ]

    async def pump(subscription: Subscription) -> None:
        while True:
            topic, payload = await subscription.get()
            if topic.startswith("system."):
                print(f"{'':8} {topic:<20} {payload.get('message', payload)}", file=out)
            else:
                print(describe(topic, payload), file=out)
            out.flush()

    async def run() -> None:
        await asyncio.gather(*(pump(subscription) for subscription in subscriptions))

    return asyncio.create_task(run(), name="frigate-cli:printer")


async def _emit_to_bus(bus: InProcessEventBus, kind: str, payload: dict[str, Any]) -> None:
    await bus.publish(f"system.{kind}", payload)


def cmd_check(config_path: Path | None, url: str | None) -> int:
    frigate = _load_frigate(config_path)
    base = url or (frigate.url if frigate else None)
    if base is None:
        print("error: pass --url http://frigate:5000 or --config vidette.yaml", file=sys.stderr)
        return 2

    async def run() -> int:
        async with FrigateClient(base) as client:
            version = await client.version()
            cameras = await client.cameras()
        print(f"Frigate {version} at {base}")
        print(f"cameras: {', '.join(cameras) or '(none)'}")
        if _version_tuple(version) < MIN_FRIGATE:
            print("✗ Vidette needs Frigate 0.14 or newer (review items); see docs/frigate.md")
            return 1
        missing = []
        if frigate is not None and frigate.cameras != "all":
            missing = [name for name in frigate.cameras if name not in cameras]
        for name in missing:
            print(f"✗ frigate.cameras lists '{name}', which Frigate does not have")
        return 1 if missing else 0

    try:
        return asyncio.run(run())
    except FrigateError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def cmd_tail(config_path: Path, record: Path | None) -> int:
    try:
        frigate = _load_frigate(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if frigate is None:
        print(f"error: {config_path} has no frigate: section (docs/frigate.md)", file=sys.stderr)
        return 2

    async def run() -> None:
        bus = InProcessEventBus()
        sink_file = record.open("a", encoding="utf-8") if record is not None else None

        def sink(topic: str, raw: bytes) -> None:
            if sink_file is not None:
                sink_file.write(record_line(topic, raw, at=time.time()) + "\n")
                sink_file.flush()

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            await _emit_to_bus(bus, kind, payload)

        source = FrigateSource(frigate, bus, emit=emit, raw_sink=sink)
        printer = _print_bus(bus, sys.stdout)
        print(f"listening on {frigate.mqtt.host}:{frigate.mqtt.port} {source.topics}; Ctrl-C stops")
        if record is not None:
            print(f"recording raw messages to {record}")
        source.start()
        try:
            await asyncio.Event().wait()
        finally:
            await source.stop()
            printer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await printer
            if sink_file is not None:
                sink_file.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


def cmd_replay(path: Path, config_path: Path | None, prefix: str) -> int:
    frigate = _load_frigate(config_path) or FrigateConfig.model_validate(
        {"url": "http://frigate.invalid", "mqtt": {"host": "replay", "topic_prefix": prefix}}
    )

    async def run() -> int:
        bus = InProcessEventBus()

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            await _emit_to_bus(bus, kind, payload)

        source = FrigateSource(frigate, bus, emit=emit)
        printer = _print_bus(bus, sys.stdout)
        count = await replay(source, path)
        await asyncio.sleep(0.05)  # let the printer drain
        printer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await printer
        status = source.status()
        print(
            f"{count} messages, {status.incidents_total} incidents opened, "
            f"{status.invalid_total} invalid"
        )
        return 0 if status.invalid_total == 0 else 1

    try:
        return asyncio.run(run())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
