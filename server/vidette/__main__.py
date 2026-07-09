"""CLI: `vidette serve` and `vidette validate <config>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vidette import __version__


def _cmd_validate(path: Path) -> int:
    from vidette.core.config import validate_config_text

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    report = validate_config_text(text)
    if not report.valid:
        print(f"✗ {path} is invalid:")
        for error in report.errors:
            print(f"  - {error}")
        return 1
    print(f"✓ {path} is valid")
    for warning in report.warnings:
        print(f"  ⚠ {warning}")
    return 0


def _cmd_serve(host: str, port: int, config: Path | None) -> int:
    import os

    import uvicorn

    if config is not None:
        os.environ["VIDETTE_CONFIG"] = str(config)
    config_path = os.environ.get("VIDETTE_CONFIG", "/config/vidette.yaml")
    print(f"vidette v{__version__} — M1 · http://{host}:{port} · config: {config_path}")
    uvicorn.run("vidette.api.app:create_app", factory=True, host=host, port=port)
    return 0


def _cmd_discover(timeout_s: float) -> int:
    import asyncio

    from vidette.adapters.onvif import discover

    devices = asyncio.run(discover(timeout_s=timeout_s))
    if not devices:
        print(
            "no ONVIF cameras answered — check that this host shares the cameras' "
            "network/VLAN (WS-Discovery is multicast and does not cross routers)"
        )
        return 1
    print(f"found {len(devices)} device(s):")
    for device in devices:
        print(f"  {device.address}  {device.xaddr}")
        for scope in device.scopes:
            print(f"    {scope}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vidette",
        description="Self-hosted video security that understands intent — not just motion.",
    )
    parser.add_argument("--version", action="version", version=f"vidette {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the API + web app")
    serve.add_argument("--host", default="0.0.0.0")  # container default
    serve.add_argument("--port", type=int, default=8642)
    serve.add_argument("--config", type=Path, default=None, help="config file path")

    validate = subparsers.add_parser("validate", help="validate a config file")
    validate.add_argument("config", type=Path)

    discover = subparsers.add_parser("discover", help="find ONVIF cameras on the LAN")
    discover.add_argument("--timeout", type=float, default=3.0)

    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args.config)
    if args.command == "discover":
        return _cmd_discover(args.timeout)
    return _cmd_serve(args.host, args.port, args.config)


if __name__ == "__main__":
    raise SystemExit(main())
