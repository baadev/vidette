"""One video of several cameras at the same moment (ADR-0017, docs/other-cameras.md).

A mosaic in real time: one tile per camera, each showing that camera's clips at the
moment they were recorded, dark grey where the camera recorded nothing (asleep, or nothing
moved). The cameras' own on-screen clocks stay in the picture; nothing is drawn on top, so
no font is needed and a tile shows exactly what the camera recorded. Which tile is which
camera is said in the message that carries the video.

Landscape cameras are stacked in a column, portrait ones (a camera mounted on its side)
stand next to that column at its full height, so neither is shrunk to a sliver:

    two landscape, one portrait     one landscape, one portrait     three landscape
    +-----------+---+               +----------------+-----+        +-----------+
    | camera 1  |   |               |   camera 1     |  2  |        | camera 1  |
    +-----------+ 3 |               |                |     |        +-----------+
    | camera 2  |   |               +----------------+-----+        | camera 2  |
    +-----------+---+                                               +-----------+
                                                                    | camera 3  |
                                                                    +-----------+

15 frames a second, H.264. A video over `max_mb` is encoded again at a lower quality, and
if it is still too big it is cut into parts of equal length, each under the limit.
ffmpeg only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from vidette.ffmpeg import MediaToolError, binary, run

logger = logging.getLogger(__name__)

FPS = 15
CRF = 30
CRF_SMALLER = 36
GAP_COLOUR = "0x1a1a1a"
MAX_TILES = 4
MAX_SECONDS = 180.0  # never a mosaic longer than this
TIMEOUT_S = 240.0
COLUMN_W = 640  # landscape tiles: 640×360 each, stacked
ROW_H = 640  # one landscape tile or none: everything this tall, side by side


@dataclass(frozen=True)
class Piece:
    path: Path
    started_at: float  # epoch seconds of the clip's first frame
    ended_at: float


@dataclass
class Tile:
    camera: str
    pieces: list[Piece] = field(default_factory=list)
    portrait: bool = False  # taller than wide as shown (a camera mounted on its side)


@dataclass(frozen=True)
class Cell:
    x: int
    y: int
    w: int
    h: int


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def arrange(portrait: list[bool]) -> tuple[list[Cell], int, int]:
    """Where each tile goes, in the order given, and the canvas size."""
    landscape = [i for i, tall in enumerate(portrait) if not tall]
    upright = [i for i, tall in enumerate(portrait) if tall]
    cells: dict[int, Cell] = {}
    if len(landscape) >= 2:
        height = 360 * len(landscape)
        for row, i in enumerate(landscape):
            cells[i] = Cell(0, row * 360, COLUMN_W, 360)
        x = COLUMN_W
    else:
        height = ROW_H
        x = 0
        for i in landscape:
            cells[i] = Cell(0, 0, _even(ROW_H * 16 / 9), ROW_H)
            x = cells[i].w
    for i in upright:
        width = _even(height * 9 / 16)
        cells[i] = Cell(x, 0, width, height)
        x += width
    return [cells[i] for i in range(len(portrait))], x, height


@dataclass(frozen=True)
class Stitched:
    files: list[Path]  # one video, or its parts in order
    start: float
    end: float
    cameras: list[str]  # the tiles' cameras, in the order of `cells`
    cells: list[Cell]  # where each camera's tile is
    width: int
    height: int
    size_mb: float

    def place(self, index: int) -> tuple[str, str]:
        """Where tile `index` is: (left|right|middle, top|bottom|full)."""
        cell = self.cells[index]
        across = "left" if cell.x == 0 else "right"
        if len({c.x for c in self.cells}) == 1:
            across = "middle"
        down = "full" if cell.h >= self.height else "top" if cell.y == 0 else "bottom"
        return across, down


def _cell(label: str, cell: Cell) -> str:
    return (
        f"fps={FPS},scale=w={cell.w}:h={cell.h}:force_original_aspect_ratio=decrease,"
        f"pad={cell.w}:{cell.h}:(ow-iw)/2:(oh-ih)/2:color={GAP_COLOUR},setsar=1,"
        f"format=yuv420p[{label}]"
    )


def _gap(seconds: float, label: str, cell: Cell) -> str:
    return (
        f"color=c={GAP_COLOUR}:s={cell.w}x{cell.h}:r={FPS}:d={seconds:.3f},"
        f"setsar=1,format=yuv420p[{label}]"
    )


def plan(
    tiles: list[Tile], start: float, end: float
) -> tuple[list[str], str, list[str], list[Cell], int, int]:
    """ffmpeg input arguments, the filter graph and the tile order, for a mosaic of
    `tiles` over [start, end] (pure: no ffmpeg runs here). Tiles with nothing in the
    window are left out."""
    shown = [
        t for t in tiles
        if any(min(p.ended_at, end) - max(p.started_at, start) >= 0.2 for p in t.pieces)
    ][:MAX_TILES]  # fmt: skip
    cells, width, height = arrange([t.portrait for t in shown])
    inputs: list[str] = []
    graph: list[str] = []
    index = 0
    for number, (tile, cell) in enumerate(zip(shown, cells, strict=True)):
        parts: list[str] = []
        cursor = start
        for k, piece in enumerate(sorted(tile.pieces, key=lambda p: p.started_at)):
            begin, finish = max(piece.started_at, cursor), min(piece.ended_at, end)
            if finish - begin < 0.2:
                continue
            if begin - cursor >= 0.05:
                label = f"t{number}g{k}"
                graph.append(_gap(begin - cursor, label, cell))
                parts.append(f"[{label}]")
            inputs += [
                "-ss", f"{begin - piece.started_at:.3f}", "-t", f"{finish - begin:.3f}",
                "-i", str(piece.path),
            ]  # fmt: skip
            label = f"t{number}p{k}"
            graph.append(f"[{index}:v]setpts=PTS-STARTPTS," + _cell(label, cell))
            parts.append(f"[{label}]")
            index += 1
            cursor = finish
        if end - cursor >= 0.05:
            label = f"t{number}end"
            graph.append(_gap(end - cursor, label, cell))
            parts.append(f"[{label}]")
        graph.append(f"{''.join(parts)}concat=n={len(parts)}:v=1:a=0[tile{number}]")
    if not shown:
        return [], "", [], [], 0, 0
    if len(shown) == 1:
        graph.append("[tile0]null[out]")
    else:
        places = "|".join(f"{c.x}_{c.y}" for c in cells)
        stacked = "".join(f"[tile{i}]" for i in range(len(shown)))
        graph.append(
            f"{stacked}xstack=inputs={len(shown)}:layout={places}:fill={GAP_COLOUR}[out]"
        )
    return inputs, ";".join(graph), [t.camera for t in shown], cells, width, height


async def _encode(
    inputs: list[str], graph: str, out: Path, *, crf: int, duration: float
) -> None:
    args = [
        binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *inputs, "-filter_complex", graph, "-map", "[out]", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-an", str(out),
    ]  # fmt: skip
    code, _out, err = await run(args, timeout=TIMEOUT_S)
    if code != 0 or not out.is_file():
        raise MediaToolError(f"ffmpeg could not stitch the clips: {err.strip()[-300:]}")


async def _split(video: Path, parts: int, duration: float) -> list[Path]:
    files: list[Path] = []
    length = duration / parts
    for n in range(parts):
        out = video.with_name(f"{video.stem}-{n + 1}{video.suffix}")
        code, _o, err = await run(
            [
                binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{n * length:.3f}", "-t", f"{length:.3f}", "-i", str(video),
                "-c", "copy", "-movflags", "+faststart", str(out),
            ],
            timeout=TIMEOUT_S,
        )  # fmt: skip
        if code != 0 or not out.is_file():
            raise MediaToolError(f"ffmpeg could not cut the mosaic: {err.strip()[-200:]}")
        files.append(out)
    return files


async def stitch(
    tiles: list[Tile], start: float, end: float, out: Path, *, max_mb: float = 19.0
) -> Stitched:
    """Encode the mosaic of `tiles` over [start, end] to `out` (an .mp4). Raises
    MediaToolError when ffmpeg is missing or fails, ValueError when nothing to show."""
    end = min(end, start + MAX_SECONDS)
    inputs, graph, cameras, cells, width, height = plan(tiles, start, end)
    if not cameras:
        raise ValueError("no camera recorded anything in that window")
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    await _encode(inputs, graph, out, crf=CRF, duration=duration)
    size = out.stat().st_size / 1_000_000
    if size > max_mb:
        logger.info("mosaic %.1f MB over %.0f MB: encoding again smaller", size, max_mb)
        await _encode(inputs, graph, out, crf=CRF_SMALLER, duration=duration)
        size = out.stat().st_size / 1_000_000
    files = [out]
    if size > max_mb:
        parts = int(size // max_mb) + 1
        files = await _split(out, parts, duration)
    return Stitched(files, start, end, cameras, cells, width, height, round(size, 1))
