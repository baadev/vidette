"""Face embeddings as plain floats: normalise, compare, serialise.

An SFace embedding is 128 numbers; a person has tens of them. Comparing a face with every
reference is a few thousand multiplications, so plain Python is enough and the store,
the matching and the API need no numpy.
"""

from __future__ import annotations

import base64
import math
from array import array
from collections.abc import Iterable

Vector = tuple[float, ...]


def normalize(values: Iterable[float]) -> Vector:
    """Unit length (cosine similarity becomes a dot product). A zero vector stays zero."""
    items = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in items))
    if norm == 0.0:
        return tuple(items)
    return tuple(value / norm for value in items)


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two unit vectors, -1 to 1."""
    if len(a) != len(b):
        raise ValueError(f"embeddings differ in length ({len(a)} and {len(b)})")
    return sum(x * y for x, y in zip(a, b, strict=True))


def encode(vector: Vector) -> str:
    """Base64 of little-endian float32: about 170 characters for 128 numbers."""
    data = array("f", vector)
    if data.itemsize != 4:  # pragma: no cover - every supported platform has 4-byte floats
        raise RuntimeError("float32 arrays are not 4 bytes on this platform")
    raw = data.tobytes() if _little_endian() else _swapped(data).tobytes()
    return base64.b64encode(raw).decode("ascii")


def decode(text: str) -> Vector:
    data = array("f")
    data.frombytes(base64.b64decode(text.encode("ascii"), validate=True))
    if not _little_endian():  # pragma: no cover
        data = _swapped(data)
    return tuple(float(value) for value in data)


def _little_endian() -> bool:
    import sys

    return sys.byteorder == "little"


def _swapped(data: array[float]) -> array[float]:  # pragma: no cover - big-endian only
    copy = array("f", data)
    copy.byteswap()
    return copy
