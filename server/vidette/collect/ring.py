"""The ring: the last few hours of a continuous stream, kept only to cut clips from.

    ring/20261114T153742+0200.mkv   one segment, ~60 s, stream-copied, video only
    ring/20261114T153842+0200.mkv   …

ffmpeg's segment muxer writes the segments (Matroska, a new file at the first key frame
after each `--segment` seconds, named by the local time it opened, with the UTC offset so
the autumn clock change cannot reuse a name). This module lists them, learns each finished
segment's exact length once (ffprobe), deletes the oldest past the size or age limit, and
cuts a clip out of one or more segments without re-encoding.

A segment's start on the wall clock is its modification time minus its length: the muxer
closes a segment when the first packet of the next one arrives, so the last write is the
segment's end to within a frame. The name (one-second resolution) is the fallback.

The ring is not a recording anyone browses (ADR-0011): there is no index, no timeline and
no playback here. Its job is pre-roll, so a clip can start seconds before the motion that
triggered it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vidette.ffmpeg import MediaToolError, binary, probe, run

logger = logging.getLogger(__name__)

RING_DIR = "ring"
SEGMENT_SUFFIX = ".mkv"
SEGMENT_STRFTIME = "%Y%m%dT%H%M%S%z"
_NAME_SLACK_S = 5.0  # mtime − duration further than this from the name: trust the name
_CONTIGUOUS_S = 1.5  # a bigger hole between segments means the stream restarted


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float  # wall clock (epoch s) of its first frame
    duration_s: float | None  # None while ffmpeg is still writing it
    size_bytes: int
    ts_offset: float = 0.0  # the container's own start time (inpoints are in its timeline)

    @property
    def complete(self) -> bool:
        return self.duration_s is not None

    @property
    def end(self) -> float:
        return self.start + (self.duration_s or 0.0)


def name_time(name: str) -> float | None:
    """The wall clock encoded in a segment's file name, or None for a foreign file."""
    stem = name.removesuffix(SEGMENT_SUFFIX)
    try:
        return datetime.strptime(stem, SEGMENT_STRFTIME).timestamp()
    except ValueError:
        return None


def output_pattern(ring: Path) -> str:
    """The segment muxer's file name pattern; a `%` in the folder must not be expanded."""
    return str(ring).replace("%", "%%") + "/" + SEGMENT_STRFTIME + SEGMENT_SUFFIX


class Ring:
    """Lists, measures and trims the segments in one ring folder."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._probed: dict[str, tuple[int, int, float, float]] = {}  # name → mtime, size, …

    async def scan(self, *, writing: bool) -> list[Segment]:
        """Segments oldest first. While `writing`, the newest one is still open."""
        entries: list[tuple[float, Path]] = []
        with contextlib.suppress(FileNotFoundError):
            for path in self.directory.iterdir():
                stamp = name_time(path.name)
                if stamp is not None and path.is_file():
                    entries.append((stamp, path))
        entries.sort()
        segments: list[Segment] = []
        for index, (stamp, path) in enumerate(entries):
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if writing and index == len(entries) - 1:
                segments.append(Segment(path, stamp, None, st.st_size))
                continue
            known = self._probed.get(path.name)
            if known is not None and known[:2] == (st.st_mtime_ns, st.st_size):
                duration, offset = known[2], known[3]
            else:
                try:
                    info = await probe(path)
                except MediaToolError as exc:
                    logger.warning(
                        "ring segment %s is unreadable (%s); deleting it", path.name, exc
                    )
                    path.unlink(missing_ok=True)
                    continue
                duration, offset = info.duration_s, info.start_s
                self._probed[path.name] = (st.st_mtime_ns, st.st_size, duration, offset)
            start = st.st_mtime - duration
            if abs(start - stamp) > _NAME_SLACK_S:
                start = stamp
            segments.append(Segment(path, start, duration, st.st_size, offset))
        live = {segment.path.name for segment in segments}
        for name in list(self._probed):
            if name not in live:
                del self._probed[name]
        return segments

    def trim(
        self,
        segments: list[Segment],
        *,
        max_bytes: int,
        max_age_s: float,
        now: float,
        keep_from: float | None = None,
        min_keep: int = 3,
    ) -> list[Segment]:
        """Delete the oldest segments until the ring fits `max_bytes` and `max_age_s`.

        Never deletes the newest `min_keep` segments, an open one, or one that ends after
        `keep_from` (a clip waiting to be cut still needs it). Returns what was deleted;
        `segments` is not modified.
        """
        total = sum(segment.size_bytes for segment in segments)
        deletable = len(segments) - min_keep
        deleted: list[Segment] = []
        for segment in segments[: max(0, deletable)]:
            too_big = total > max_bytes
            too_old = segment.complete and now - segment.end > max_age_s
            if not (too_big or too_old):
                break
            if not segment.complete:
                break
            if keep_from is not None and segment.end >= keep_from:
                break
            segment.path.unlink(missing_ok=True)
            self._probed.pop(segment.path.name, None)
            total -= segment.size_bytes
            deleted.append(segment)
        return deleted


def total_bytes(segments: Iterable[Segment]) -> int:
    return sum(segment.size_bytes for segment in segments)


@dataclass(frozen=True)
class CutPiece:
    segment: Segment
    inpoint: float | None  # in the segment's own timeline; None: from its first frame
    outpoint: float | None  # None: to its last frame

    @property
    def wall_start(self) -> float:
        if self.inpoint is None:
            return self.segment.start
        return self.segment.start + self.inpoint - self.segment.ts_offset

    @property
    def wall_end(self) -> float:
        if self.outpoint is None:
            return self.segment.end
        return self.segment.start + self.outpoint - self.segment.ts_offset

    @property
    def estimated_bytes(self) -> int:
        duration = self.segment.duration_s or 0.0
        if duration <= 0:
            return self.segment.size_bytes
        share = max(0.0, self.wall_end - self.wall_start) / duration
        return int(self.segment.size_bytes * min(1.0, share))


def plan_cut(segments: list[Segment], start: float, end: float) -> list[CutPiece]:
    """The finished segments covering [start, end] and where to cut each one.

    Only one contiguous run of segments is used, the one holding the end of the window
    (that is where the motion was): pre-roll never reaches across a stream restart.
    """
    if end <= start:
        return []
    covering = [s for s in segments if s.complete and s.end > start and s.start < end]
    if not covering:
        return []
    run = [covering[-1]]
    for segment in reversed(covering[:-1]):
        if run[0].start - segment.end > _CONTIGUOUS_S:
            break
        run.insert(0, segment)
    pieces: list[CutPiece] = []
    for index, segment in enumerate(run):
        inpoint = outpoint = None
        if index == 0 and start > segment.start:
            inpoint = round(start - segment.start + segment.ts_offset, 3)
        if index == len(run) - 1 and end < segment.end:
            outpoint = round(end - segment.start + segment.ts_offset, 3)
        pieces.append(CutPiece(segment, inpoint, outpoint))
    return pieces


def _ffconcat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


async def cut(pieces: list[CutPiece], out: Path) -> None:
    """Copy the pieces into one Matroska file (no re-encode). A stream copy can only
    start on a key frame, so the clip starts at the key frame at or before the first
    inpoint: a little more pre-roll, never less."""
    if not pieces:
        raise MediaToolError("nothing to cut: the ring holds no video for that moment")
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_name(out.name + ".ffconcat")
    lines = ["ffconcat version 1.0"]
    for piece in pieces:
        lines.append(f"file {_ffconcat_quote(piece.segment.path.resolve())}")
        if piece.inpoint is not None:
            lines.append(f"inpoint {piece.inpoint:.3f}")
        if piece.outpoint is not None:
            lines.append(f"outpoint {piece.outpoint:.3f}")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        rc, _, stderr = await run(
            [
                binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-f", "matroska", str(out),
            ],
            timeout=120,
        )  # fmt: skip
    finally:
        listing.unlink(missing_ok=True)
    if rc != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        raise MediaToolError(f"cannot cut a clip from the ring: {stderr.strip()[-200:]}")
