"""Human-friendly sizes and durations on the command line: `5GB`, `10GiB`, `90s`, `2m`.

Config files use the same duration spelling (`3d`, `12h`); sizes are decimal (`GB` is
10^9 bytes, like the Finder and `df -H`) unless the binary unit is spelled out (`GiB`).
"""

from __future__ import annotations

import re

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgGtT]?)(i?)([bB]?)\s*$")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_PREFIX_POWER = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}


def parse_size(text: str) -> int:
    """`5GB` → 5_000_000_000, `512MiB` → 536_870_912, `1000` → 1000 bytes."""
    match = _SIZE_RE.match(text)
    if match is None:
        raise ValueError(f"'{text}' is not a size; use e.g. 500MB, 5GB or 10GiB")
    number, prefix, binary, _ = match.groups()
    base = 1024 if binary else 1000
    return int(float(number) * base ** _PREFIX_POWER[prefix.lower()])


def parse_duration(text: str) -> float:
    """`90s` → 90.0, `2m` → 120.0, `1.5h` → 5400.0; a bare number means seconds."""
    match = _DURATION_RE.match(text)
    if match is None:
        raise ValueError(f"'{text}' is not a duration; use e.g. 500ms, 8s, 2m or 1h")
    number, unit = match.groups()
    return float(number) * _DURATION_UNITS[unit or "s"]


def format_size(size: float) -> str:
    """Decimal units, three significant digits: 5.00 GB, 731 MB, 12.4 kB."""
    value = float(size)
    for unit in ("B", "kB", "MB", "GB"):
        if abs(value) < 1000:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.3g} {unit}"
        value /= 1000
    return f"{value:.3g} TB"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"
