"""Thin async wrappers around the ffmpeg/ffprobe binaries for offline tools.

Used by `vidette collect` (finalize clips, key frames), `vidette label` (contact sheets)
and the VLM judge (frames from a clip). The `vidette serve` container does not ship
ffmpeg (ADR-0009: Vidette decodes nothing live); these helpers raise `MediaToolError`
with an install hint when the binary is missing instead of failing obscurely.

Every command is argument-vector only (no shell), reads nothing from stdin and has a
timeout, so a broken file can never hang a tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 60.0


class MediaToolError(Exception):
    """ffmpeg/ffprobe is missing or failed; the message says what to do."""


def binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MediaToolError(
            f"{name} not found on PATH; install ffmpeg (macOS: `brew install ffmpeg`, "
            "Debian/Ubuntu: `apt install ffmpeg`)"
        )
    return path


async def run(
    args: list[str], *, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[int, bytes, str]:
    """Run to completion; returns (returncode, stdout, stderr). Kills on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise MediaToolError(f"{Path(args[0]).name} timed out after {timeout:.0f} s") from None
    return proc.returncode or 0, stdout, stderr.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class VideoInfo:
    duration_s: float
    codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    size_bytes: int
    start_s: float = 0.0  # the container's first timestamp (seek points are relative to it)


ROTATIONS = (0, 90, 180, 270)


def rotation_filter(rotation: int) -> str | None:
    """ffmpeg filter that turns a picture `rotation` degrees clockwise (None for 0)."""
    filters = {0: None, 90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}
    if rotation not in filters:
        raise ValueError(f"rotation must be one of {ROTATIONS}, not {rotation}")
    return filters[rotation]


def display_rotation(rotation: int) -> int:
    """`-display_rotation` takes counter-clockwise degrees; ours are clockwise."""
    return {0: 0, 90: -90, 180: 180, 270: 90}[rotation]


def _fps(rate: str | None) -> float | None:
    if not rate or "/" not in rate:
        return None
    num, _, den = rate.partition("/")
    try:
        value = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if value > 0 and math.isfinite(value) else None


async def probe(path: Path) -> VideoInfo:
    """Duration and first video stream of a media file; MediaToolError if unreadable."""
    rc, stdout, stderr = await run(
        [
            binary("ffprobe"), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=30,
    )  # fmt: skip
    if rc != 0:
        raise MediaToolError(f"ffprobe cannot read {path.name}: {stderr.strip()[-200:]}")
    data = json.loads(stdout or b"{}")
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    rate = None if video is None else video.get("avg_frame_rate") or video.get("r_frame_rate")
    duration = data.get("format", {}).get("duration")
    if duration is None and video is not None:
        duration = video.get("duration")
    try:
        seconds = float(duration) if duration is not None else 0.0
    except ValueError:
        seconds = 0.0
    try:
        start = float(data.get("format", {}).get("start_time", 0.0))
    except (TypeError, ValueError):
        start = 0.0
    return VideoInfo(
        duration_s=seconds if math.isfinite(seconds) else 0.0,
        codec=None if video is None else video.get("codec_name"),
        width=None if video is None else video.get("width"),
        height=None if video is None else video.get("height"),
        fps=_fps(rate),
        size_bytes=path.stat().st_size,
        start_s=start if math.isfinite(start) else 0.0,
    )


@dataclass(frozen=True)
class Packets:
    """The video packets of a file, in presentation order: each one is one frame."""

    times: list[float]  # presentation time, seconds, in the file's own timeline
    keys: list[bool]  # key frame (decodable on its own)


async def packets(path: Path, *, timeout: float = 30.0) -> Packets:
    """Every video packet's time and key flag, read from the container (no decoding).

    About 0.1 s for a two-minute 1080p clip. Packets without a time are skipped."""
    rc, stdout, stderr = await run(
        [
            binary("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path),
        ],
        timeout=timeout,
    )  # fmt: skip
    if rc != 0:
        raise MediaToolError(f"ffprobe cannot list packets of {path.name}: {stderr.strip()[-200:]}")
    rows: list[tuple[float, bool]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        time_text, _, flags = line.strip().partition(",")
        try:
            value = float(time_text)
        except ValueError:
            continue
        if math.isfinite(value):
            rows.append((value, "K" in flags))
    rows.sort(key=lambda row: row[0])
    return Packets([row[0] for row in rows], [row[1] for row in rows])


async def packet_times(path: Path) -> list[float]:
    """Presentation times of every video packet, sorted (see `packets`)."""
    return (await packets(path)).times


def frame_times(duration_s: float, count: int) -> list[float]:
    """`count` instants spread evenly over the clip, centred in equal slices."""
    if count <= 0 or duration_s <= 0:
        return []
    return [round(duration_s * (index + 0.5) / count, 3) for index in range(count)]


def scale_filter(max_side: int) -> str:
    """Shrink so the longer side is at most `max_side`, keep the aspect, even dimensions."""
    return (
        f"scale=w='if(gte(iw,ih),min(iw,{max_side}),-2)':"
        f"h='if(gte(iw,ih),-2,min(ih,{max_side}))'"
    )


async def extract_frame(
    video: Path, at_s: float, out: Path, *, max_side: int = 1280, rotation: int = 0
) -> Path:
    """One JPEG at `at_s` seconds (accurate seek), at most `max_side` px on its long side.

    `rotation` (clockwise degrees) turns the picture explicitly and ignores rotation
    metadata in the file, so the result is the same whatever the container carries."""
    out.parent.mkdir(parents=True, exist_ok=True)
    turn = rotation_filter(rotation)
    filters = scale_filter(max_side) if turn is None else f"{turn},{scale_filter(max_side)}"
    no_autorotate = [] if turn is None else ["-noautorotate"]
    rc, _, stderr = await run(
        [
            binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            *no_autorotate, "-ss", f"{max(0.0, at_s):.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", filters, "-q:v", "3", str(out),
        ],
        timeout=30,
    )  # fmt: skip
    if rc != 0 or not out.is_file():
        raise MediaToolError(f"cannot extract a frame at {at_s:.1f} s: {stderr.strip()[-200:]}")
    return out


async def extract_frames(
    video: Path, times: list[float], out_dir: Path, *, max_side: int = 1280, rotation: int = 0
) -> list[Path]:
    """Numbered JPEGs (1.jpg, 2.jpg, …) at the given instants."""
    return [
        await extract_frame(
            video, at, out_dir / f"{index}.jpg", max_side=max_side, rotation=rotation
        )
        for index, at in enumerate(times, start=1)
    ]


async def sample_frames(
    video: Path, out_dir: Path, *, fps: float, max_frames: int, max_side: int = 1920
) -> list[Path]:
    """Frames at `fps` over the whole clip (fewer per second when that would exceed
    `max_frames`), upright as players show them, as numbered JPEGs. One ffmpeg run."""
    info = await probe(video)
    if info.duration_s <= 0 or max_frames <= 0:
        return []
    rate = min(fps, max_frames / info.duration_s)
    out_dir.mkdir(parents=True, exist_ok=True)
    rc, _, stderr = await run(
        [
            binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-vf", f"fps={rate:.4f},{scale_filter(max_side)}",
            "-frames:v", str(max_frames), "-q:v", "2", str(out_dir / "%03d.jpg"),
        ],
        timeout=120,
    )  # fmt: skip
    frames = sorted(out_dir.glob("*.jpg"))
    if rc != 0 and not frames:
        raise MediaToolError(f"cannot sample frames from {video.name}: {stderr.strip()[-200:]}")
    return frames


async def remux_mp4(src: Path, dst: Path, *, hevc: bool, rotation: int = 0) -> bool:
    """Copy the streams into a standard, fast-start MP4 (no re-encode).

    H.265 gets the `hvc1` tag, without which Apple players refuse to open the file.
    `rotation` (clockwise degrees) is written as display metadata: the pixels stay as
    the camera sent them, players show them turned. Returns False when this ffmpeg
    cannot write that metadata (older than 6.0) and the MP4 was written without it.
    """
    tmp = dst.with_name(dst.name + ".tmp")
    tag = ["-tag:v", "hvc1"] if hevc else []
    attempts: list[list[str]] = [[]]
    if rotation:
        attempts.insert(0, ["-display_rotation:v:0", str(display_rotation(rotation))])
    stderr = ""
    for turn in attempts:
        rc, _, stderr = await run(
            [
                binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                *turn, "-i", str(src), "-map", "0", "-c", "copy", *tag,
                "-movflags", "+faststart", "-f", "mp4", str(tmp),
            ],
            timeout=120,
        )  # fmt: skip
        if rc == 0 and tmp.is_file():
            tmp.replace(dst)
            return bool(turn) or not rotation
        tmp.unlink(missing_ok=True)
    raise MediaToolError(f"cannot remux {src.name} to MP4: {stderr.strip()[-200:]}")


async def cut_head(
    src: Path, dst: Path, seconds: float, *, hevc: bool, rotation: int = 0
) -> None:
    """The first `seconds` of `src` (it may still be growing) as a fast-start MP4, stream
    copy, written to a temporary name and renamed: a reader never sees half a file."""
    tmp = dst.with_name(dst.name + ".tmp")
    tag = ["-tag:v", "hvc1"] if hevc else []
    turn = ["-display_rotation:v:0", str(display_rotation(rotation))] if rotation else []
    rc, _, stderr = await run(
        [
            binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            *turn, "-i", str(src), "-map", "0:v:0", "-t", f"{seconds:g}", "-c", "copy", *tag,
            "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ],
        timeout=60,
    )  # fmt: skip
    if rc != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise MediaToolError(
            f"cannot cut the first {seconds:g} s of {src.name}: {stderr.strip()[-200:]}"
        )
    tmp.replace(dst)


async def jpeg(src: Path, *, max_side: int) -> bytes:
    """An image (or a video's first frame) as JPEG bytes, shrunk to `max_side`."""
    rc, stdout, stderr = await run(
        [
            binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-frames:v", "1", "-vf", scale_filter(max_side),
            "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        ],
        timeout=30,
    )  # fmt: skip
    if rc != 0 or not stdout:
        raise MediaToolError(f"cannot read {src.name} as an image: {stderr.strip()[-200:]}")
    return stdout


async def rgb_tile(sources: list[Path], *, width: int, height: int) -> bytes:
    """Each source scaled into a `width`×`height` box (letterboxed), as raw RGB24 rows,
    stacked vertically: len(sources) * width * height * 3 bytes."""
    out = bytearray()
    for src in sources:
        rc, stdout, stderr = await run(
            [
                binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(src), "-frames:v", "1",
                "-vf", (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x1b1f27"
                ),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            ],
            timeout=30,
        )  # fmt: skip
        if rc != 0 or len(stdout) != width * height * 3:
            raise MediaToolError(f"cannot decode {src.name}: {stderr.strip()[-200:]}")
        out += stdout
    return bytes(out)
