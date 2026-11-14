"""Contact sheets: drafts laid out for review on a phone.

One PNG per 8 (up to 12) clips, two columns. Each cell shows a large review number, the
proposed scenario with ALERT / silent and the model's confidence, four key frames in
time order (a 2×2 grid, or one row of four tall tiles when the frames are portrait, as
from a camera turned with `--rotate`), the time, length and camera, and the model's
one-line description. A cell without a draft says so, and the number still works for
labelling by hand.

The reply format is printed at the bottom of every sheet: `3 ok`, `5 courier`,
`7 alert`, `rest ok` (see `vidette label apply`). When the clip had a face check (known
people, docs/people.md), the cell's last line lists the faces by number, so a reply can
say who one is: `5 Robin`, `5.2 stranger`.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vidette.ffmpeg import MediaToolError, probe, rgb_tile
from vidette.label.canvas import RGB, Canvas, text_width, wrap
from vidette.people.report import PeopleReport
from vidette.verdicts.base import Case
from vidette.verdicts.frames import frame_files

WIDTH = 1080
MARGIN = 12
COLUMNS = 2
CELL_W = (WIDTH - MARGIN * (COLUMNS + 1)) // COLUMNS  # 522
TILE_W, TILE_H = CELL_W // 2, (CELL_W // 2) * 9 // 16  # 261 x 146
HEADER_H = 72
FOOTER_H = 70
CELL_H = HEADER_H + 2 * TILE_H + FOOTER_H
TITLE_H = 44
INSTRUCTIONS_H = 94

BG: RGB = (11, 14, 20)
CELL: RGB = (21, 26, 35)
INK: RGB = (232, 235, 241)
MUTED: RGB = (143, 151, 166)
ALERT: RGB = (229, 72, 77)
SILENT: RGB = (64, 72, 89)
UNCLEAR: RGB = (240, 166, 60)
TILE_BG: RGB = (27, 31, 39)


@dataclass(frozen=True)
class SheetItem:
    number: int
    entry_id: str
    case: Case  # media: the entry's clip and key frames
    proposal: str | None  # proposed scenario; None without a draft; "unclear" possible
    alert: bool | None
    confidence: float | None
    description: str | None
    at: datetime | None
    duration_s: float | None
    camera: str
    faces: str | None = None  # "faces: 1 Sam .83  2 ? .21" (sheet_faces)


def _short(score: float) -> str:
    return f"{score:.2f}".removeprefix("0")


def sheet_faces(report: PeopleReport) -> str:
    """One line for a cell: numbered faces, names only for known people."""
    if not report.seen:
        if report.unclear_frames:
            return f"faces: too small or unclear ({report.unclear_frames} fr)"
        return "faces: no clear face"
    parts = []
    for seen in report.seen:
        who = (seen.name or seen.person or "?") if seen.status == "known" else "?"
        mark = " ok" if seen.confirmed else ""
        parts.append(f"{seen.n} {who} {_short(seen.score)}{mark}")
    return "faces: " + "  ".join(parts)


def _badge(item: SheetItem) -> tuple[RGB, str, str]:
    """(badge colour, first line, second line) of a cell's header."""
    if item.proposal is None:
        return UNCLEAR, "?", "no draft: name it"
    if item.alert is None:
        return UNCLEAR, "unclear", "model unsure: name it"
    confidence = "" if item.confidence is None else f" {item.confidence * 100:.0f}%"
    if item.alert:
        return ALERT, item.proposal, f"ALERT{confidence}"
    return SILENT, item.proposal, f"silent{confidence}"


def tile_boxes(portrait: bool) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) of the four frame tiles inside a cell, relative to the frames' area:
    a 2×2 grid of landscape boxes, or one row of four tall ones for portrait frames."""
    if portrait:
        width = CELL_W // 4
        return [(index * width, 0, width, 2 * TILE_H) for index in range(4)]
    return [((index % 2) * TILE_W, (index // 2) * TILE_H, TILE_W, TILE_H) for index in range(4)]


async def _portrait(path: Path) -> bool:
    try:
        info = await probe(path)
    except MediaToolError:
        return False
    return bool(info.width and info.height and info.height > info.width)


async def _tiles(item: SheetItem, work_dir: Path) -> tuple[list[bytes], bool]:
    paths = await frame_files(item.case, 4, work_dir=work_dir)
    if not paths:
        return [], False
    portrait = await _portrait(paths[0])
    _, _, width, height = tile_boxes(portrait)[0]
    data = await rgb_tile(paths, width=width, height=height)
    size = width * height * 3
    return [data[i * size : (i + 1) * size] for i in range(len(paths))], portrait


async def render_sheet(items: list[SheetItem], *, title: str) -> bytes:
    rows = math.ceil(len(items) / COLUMNS)
    height = TITLE_H + rows * (CELL_H + MARGIN) + INSTRUCTIONS_H
    canvas = Canvas(WIDTH, height, BG)
    canvas.text(MARGIN, 12, title, scale=3, color=INK)

    with tempfile.TemporaryDirectory(prefix="vidette-sheet-") as tmp:
        for index, item in enumerate(items):
            column, row = index % COLUMNS, index // COLUMNS
            x = MARGIN + column * (CELL_W + MARGIN)
            y = TITLE_H + row * (CELL_H + MARGIN)
            canvas.rect(x, y, CELL_W, CELL_H, CELL)
            color, headline, verdict = _badge(item)

            number = str(item.number)
            badge_w = max(96, text_width(number, 8) + 24)
            canvas.rect(x, y, badge_w, HEADER_H, color)
            canvas.text(x + (badge_w - text_width(number, 8)) // 2, y + 8, number,
                        scale=8, color=INK)  # fmt: skip
            label_x = x + badge_w + 12
            room = (CELL_W - badge_w - 18) // 18
            canvas.text(label_x, y + 10, headline[:room], scale=3, color=INK)
            canvas.text(label_x, y + 42, verdict[:room], scale=3, color=MUTED)

            tiles, portrait = await _tiles(item, Path(tmp) / item.entry_id)
            boxes = tile_boxes(portrait)
            for tile_index, (bx, by, bw, bh) in enumerate(boxes):
                tx, ty = x + bx, y + HEADER_H + by
                if tile_index < len(tiles):
                    canvas.paste_rgb(tx, ty, bw, bh, tiles[tile_index])
                else:
                    canvas.rect(tx, ty, bw, bh, TILE_BG)
            # Gutters, so four frames read as four moments, not one picture.
            if portrait:
                for bx, _, _, _ in boxes[1:]:
                    canvas.rect(x + bx - 1, y + HEADER_H, 3, 2 * TILE_H, CELL)
            else:
                canvas.rect(x + TILE_W - 1, y + HEADER_H, 3, 2 * TILE_H, CELL)
                canvas.rect(x, y + HEADER_H + TILE_H - 1, CELL_W, 3, CELL)

            fy = y + HEADER_H + 2 * TILE_H + 8
            meta = []
            if item.at is not None:
                meta.append(item.at.strftime("%d.%m %H:%M"))
            if item.duration_s:
                meta.append(f"{item.duration_s:.0f}s")
            meta.append(item.camera)
            canvas.text(x + 8, fy, "  ".join(meta), scale=2, color=MUTED)
            lines = 1 if item.faces else 2
            for line_index, line in enumerate(wrap(item.description or "", 42, lines)):
                canvas.text(x + 8, fy + 22 + line_index * 20, line, scale=2, color=INK)
            if item.faces:
                canvas.text(x + 8, fy + 42, item.faces[:42], scale=2, color=UNCLEAR)

    iy = TITLE_H + rows * (CELL_H + MARGIN) + 8
    canvas.text(MARGIN, iy, "reply: 3 ok, 5 courier, 7 alert, rest ok", scale=3, color=INK)
    canvas.text(
        MARGIN,
        iy + 36,
        "a scenario name or alias sets it; ok accepts the draft; ? leaves it unlabelled",
        scale=2,
        color=MUTED,
    )
    if any(item.faces and item.faces.startswith("faces: 1") for item in items):
        canvas.text(MARGIN, iy + 56, "faces: 5 Robin, 5.2 stranger, 5.1 ? (who a face is)",
                    scale=2, color=UNCLEAR)  # fmt: skip

    return canvas.png()
