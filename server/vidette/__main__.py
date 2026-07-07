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


def _cmd_serve(host: str, port: int) -> int:
    import uvicorn

    print(f"vidette v{__version__} — M0 design preview · http://{host}:{port}")
    uvicorn.run("vidette.api.app:create_app", factory=True, host=host, port=port)
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

    validate = subparsers.add_parser("validate", help="validate a config file")
    validate.add_argument("config", type=Path)

    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args.config)
    return _cmd_serve(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
