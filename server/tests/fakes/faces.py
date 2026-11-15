"""A fake face engine: no OpenCV, no models, no real faces.

A test "photo" is plain text naming the faces in it, one token each:

    b"face:alice/1 face:alice/2 small face:bob/1"

- `face:<identity>/<variant>` is a readable face. Its embedding is a fixed random
  direction for the identity plus noise for the variant, so two variants of one
  identity score about 0.75 and two identities about 0.
- `small` is a face too small to tell; `side` one turned away. Like the real engine,
  they come with a crop and an embedding (`readable=False`); `small` faces sit in one
  corner of the picture and `side` ones in another, so the check can follow them from
  frame to frame by where they are.

Crops are tiny text blobs (`crop:<identity>/<variant>`) that `embed_crop` reads back.
Nothing here is an image of a person.
"""

from __future__ import annotations

import math
import random

from vidette.people.engine import DetectedFace
from vidette.people.vectors import Vector, normalize

DIM = 128
NOISE = 0.9


def identity_vector(identity: str, variant: str) -> Vector:
    base = random.Random(f"id:{identity}")
    noise = random.Random(f"noise:{identity}/{variant}")
    return normalize(
        base.gauss(0, 1) + NOISE * noise.gauss(0, 1) / math.sqrt(1.0) for _ in range(DIM)
    )


def photo(*tokens: str) -> bytes:
    return " ".join(tokens).encode()


class FakeFaceEngine:
    name = "fake-engine"

    def __init__(self) -> None:
        self.calls = 0

    def faces(self, image: bytes, *, max_side: int | None = None) -> list[DetectedFace]:
        self.calls += 1
        try:
            text = image.decode()
        except UnicodeDecodeError as exc:
            raise ValueError("not a fake photo") from exc
        if not text.strip() and image:
            return []
        out: list[DetectedFace] = []
        for index, token in enumerate(text.split()):
            box = (10 * index, 10, 80, 100)
            if token in ("small", "side"):
                small = token == "small"
                corner = (
                    (600 + 10 * index, 600, 20, 20) if small else (300 + 10 * index, 300, 80, 90)
                )
                out.append(
                    DetectedFace(
                        corner,
                        0.9,
                        20 if small else 90,
                        readable=False,
                        unclear_reason="too small" if small else "turned away",
                        embedding=identity_vector(token, str(self.calls)),
                        crop=f"crop:{token}/{self.calls}".encode(),
                        sharpness=20.0,
                        frontal=0.1 if not small else 0.4,
                    )
                )
            elif token.startswith("face:"):
                identity, _, variant = token.removeprefix("face:").partition("/")
                out.append(
                    DetectedFace(
                        box,
                        0.95,
                        100,
                        readable=True,
                        embedding=identity_vector(identity, variant or "0"),
                        crop=f"crop:{identity}/{variant or '0'}".encode(),
                        sharpness=200.0,
                        frontal=0.45,
                    )
                )
            elif token == "nothing":
                continue
            else:
                raise ValueError(f"unknown fake token {token!r}")
        return out

    def embed_crop(self, crop: bytes) -> Vector:
        identity, _, variant = crop.decode().removeprefix("crop:").partition("/")
        return identity_vector(identity, variant)
