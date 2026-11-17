"""`vidette collect DATASET`: run the collector in the foreground (docs/collect.md)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import sys
from pathlib import Path

from vidette.collect.collector import (
    Collector,
    CollectSettings,
    DatasetLockedError,
    tcp_reachable,
)
from vidette.collect.continuous import (
    CLIP_RESERVE_BYTES,
    ContinuousCollector,
    ContinuousSettings,
    default_hwaccel,
)
from vidette.collect.disk import DiskLimits, dir_size, measure
from vidette.collect.motion import OVERLAY_MASK, MotionSettings, Region, parse_fraction
from vidette.collect.prune import (
    PRUNE_AT,
    PRUNE_TO,
    ProtectionError,
    Pruner,
    PruneSettings,
    default_serve_config,
    serve_stores,
)
from vidette.collect.ring import RING_DIR
from vidette.collect.target import RtspTarget, TargetError, load_target
from vidette.ffmpeg import ROTATIONS, MediaToolError, binary
from vidette.incident.mode import incident_path
from vidette.units import format_duration, format_size, parse_duration, parse_size

logger = logging.getLogger("vidette.collect")


class _Scrub(logging.Filter):
    """Last line of defence: no log record leaves with the camera's credentials in it."""

    def __init__(self, target: RtspTarget) -> None:
        super().__init__()
        self._target = target

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg, record.args = self._target.scrub(message), None
        return True


def git_work_tree(path: Path) -> Path | None:
    """The git work tree containing `path`, if any (a `.git` dir or file up the tree)."""
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _setup_logging(target: RtspTarget) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(_Scrub(target))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dataset", type=Path, help="dataset folder, outside any git repository")
    parser.add_argument("--camera", default="camera", help="camera name for the entries")
    parser.add_argument(
        "--url-file",
        type=Path,
        default=None,
        help="file with the RTSP URL (default ~/.config/vidette/rtsp-url; "
        "VIDETTE_RTSP_URL wins over both)",
    )
    parser.add_argument("--min", default="3s", help="drop clips shorter than this")
    parser.add_argument("--max", default="2m", help="cut a clip at this length")
    parser.add_argument(
        "--probe-every",
        default=None,
        help="check while it sleeps (default 5s; 2s with --base-station, whose 404 costs "
        "the camera nothing)",
    )
    parser.add_argument("--cooldown", default="10s", help="pause after each clip")
    parser.add_argument("--silence", default="8s", help="no video this long ends a clip")
    parser.add_argument("--max-size", default="5GB", help="hard cap on the dataset folder")
    parser.add_argument("--min-free", default="10GB", help="stop when the disk has less free")
    parser.add_argument(
        "--prune-oldest",
        action="store_true",
        help=f"past {PRUNE_AT:.0%} of --max-size, delete the oldest clips (with their key "
        f"frames) down to {PRUNE_TO:.0%} instead of stopping; never labelled clips, clips "
        "vidette serve alerted on or that were marked, the other cameras at that moment, "
        "clips with faces, or clips younger than --prune-min-age".replace("%", "%%"),
    )
    parser.add_argument(
        "--prune-min-age", default="24h", help="--prune-oldest never deletes a younger clip"
    )
    parser.add_argument(
        "--serve-config",
        type=Path,
        default=None,
        help="vidette serve's config, for --prune-oldest: its database and people folders "
        "say which clips to keep (default $VIDETTE_CONFIG, then "
        "~/.config/vidette/vidette.yaml if it exists)",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the TCP port check (if the camera's port is always open)",
    )
    parser.add_argument(
        "--base-station",
        action="store_true",
        help="the URL is a base station's (Eufy HomeBase): it answers 404 while the camera "
        "sleeps, so 404 means asleep: check every --probe-every instead of backing off",
    )
    parser.add_argument(
        "--wake-min",
        default="0.5s",
        help="with --base-station: a woken stream this long is kept even below --min (the "
        "camera's own motion sensor woke it: that is evidence)",
    )
    parser.add_argument(
        "--base-reconnect",
        default="1s",
        help="with --base-station: look again this soon after a woken stream ends (never "
        "grows while the camera keeps waking)",
    )
    parser.add_argument(
        "--incident-file",
        type=Path,
        default=None,
        help="vidette serve's incident-mode file: after an alert every camera keeps what "
        "it sees for a while (default: next to the database named by --serve-config)",
    )
    parser.add_argument(
        "--wallclock-timestamps",
        action="store_true",
        help="stamp packets with their arrival time instead of the camera's clock. Not "
        "recommended: delivery jitter becomes freezes in the clip. A restreamer's splices "
        "(go2rtc) are repaired without it",
    )
    parser.add_argument(
        "--early",
        default="0s",
        help="also write the first N seconds of each recording (or motion event, with "
        "--continuous) to .early/ while it goes on, so `vidette serve` can judge and alert "
        "before the clip ends (e.g. 8s; default 0s: off)",
    )
    parser.add_argument("--once", action="store_true", help="exit after the first clip kept")
    parser.add_argument(
        "--keep-awake",
        action="store_true",
        help="macOS: hold an idle-sleep assertion (caffeinate) while collecting",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="show address (redacted), port state and disk budget; open no RTSP session",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        default=0,
        choices=ROTATIONS,
        help="clockwise degrees that make the picture upright (a camera on its side): "
        "key frames are turned, the MP4 is tagged, the pixels stay as sent",
    )
    parser.add_argument(
        "--allow-inside-git", action="store_true", help=argparse.SUPPRESS
    )

    continuous = parser.add_argument_group(
        "continuous cameras (--continuous)",
        "For a camera that streams all the time: a ring of segments and a motion gate "
        "that cuts clips out of it (docs/collect.md#continuous-mode).",
    )
    continuous.add_argument(
        "--continuous",
        action="store_true",
        help="record a continuous stream into a ring and cut a clip around each motion",
    )
    continuous.add_argument("--segment", default="60s", help="length of one ring segment")
    continuous.add_argument(
        "--ring-size", default="3GB", help="cap on the ring (the clips have --max-size)"
    )
    continuous.add_argument(
        "--ring-keep", default="24h", help="delete ring segments older than this"
    )
    continuous.add_argument(
        "--ring-min-age",
        default="30m",
        help="never delete younger ring segments for --ring-size (the free-space floor "
        "still wins): the last half hour can always be cut out again",
    )
    continuous.add_argument("--preroll", default="3s", help="video kept before the motion")
    continuous.add_argument("--postroll", default="3s", help="video kept after the motion")
    continuous.add_argument(
        "--motion-fps", type=float, default=4.0, help="frames per second the gate looks at"
    )
    continuous.add_argument(
        "--motion-threshold",
        type=int,
        default=25,
        help="grey levels (of 255) a pixel must change by to count",
    )
    continuous.add_argument(
        "--motion-area",
        default="0.1%",
        help="smallest changed share of the frame that is motion",
    )
    continuous.add_argument(
        "--motion-max-area",
        default="40%",
        help="a larger change at once is light (clouds, headlights, infrared), not motion",
    )
    continuous.add_argument(
        "--motion-min", default="1s", help="motion this long opens an event (drops blips)"
    )
    continuous.add_argument(
        "--motion-gap", default="2s", help="quiet this long ends an event"
    )
    continuous.add_argument(
        "--mask",
        action="append",
        default=[],
        metavar="X,Y,W,H",
        help="ignore a region, as fractions of the frame as sent (before --rotate); "
        f"repeatable. The date/time overlay ({OVERLAY_MASK}) is masked by default",
    )
    continuous.add_argument(
        "--no-overlay-mask",
        action="store_true",
        help="do not mask the top-right date/time overlay",
    )
    continuous.add_argument(
        "--hwaccel",
        default="auto",
        help="decoder for the motion gate: auto (VideoToolbox on macOS), none, or an "
        "ffmpeg -hwaccel name",
    )


def settings_from_args(args: argparse.Namespace) -> CollectSettings:
    return CollectSettings(
        dataset=Path(args.dataset).expanduser().resolve(),
        camera=args.camera,
        min_seconds=parse_duration(args.min),
        max_seconds=parse_duration(args.max),
        probe_every_s=parse_duration(
            args.probe_every or ("2s" if args.base_station else "5s")
        ),
        cooldown_s=parse_duration(args.cooldown),
        silence_s=parse_duration(args.silence),
        limits=DiskLimits(
            max_dataset_bytes=parse_size(args.max_size),
            min_free_bytes=parse_size(args.min_free),
        ),
        tcp_probe=not args.no_probe,
        once=args.once,
        rotation=args.rotate,
        base_station=args.base_station,
        wallclock_timestamps=args.wallclock_timestamps,
        early_s=parse_duration(args.early),
        prune=prune_from_args(args),
        wake_min_seconds=parse_duration(args.wake_min),
        base_reconnect_s=parse_duration(args.base_reconnect),
        incident_file=incident_file_from_args(args),
    )


def incident_file_from_args(args: argparse.Namespace) -> Path | None:
    """--incident-file, else next to the database of --serve-config (when given or found);
    None when there is no serve to say anything (incident mode is then off)."""
    if args.incident_file is not None:
        return Path(args.incident_file).expanduser()
    config = args.serve_config
    path = config.expanduser().resolve() if config is not None else default_serve_config()
    if path is None:
        return None
    try:
        stores = serve_stores(path)
    except ProtectionError as exc:
        logger.warning("no incident mode: %s", exc)
        return None
    return None if stores.database is None else incident_path(stores.database)


def prune_from_args(args: argparse.Namespace) -> PruneSettings | None:
    if not args.prune_oldest:
        return None
    config = args.serve_config
    return PruneSettings(
        serve_config=(
            config.expanduser().resolve() if config is not None else default_serve_config()
        ),
        min_age_s=parse_duration(args.prune_min_age),
        span_s=parse_duration(args.max),
    )


def continuous_from_args(args: argparse.Namespace) -> ContinuousSettings:
    masks = [] if args.no_overlay_mask else [OVERLAY_MASK]
    masks += [Region.parse(text) for text in args.mask]
    motion = MotionSettings(
        fps=args.motion_fps,
        pixel_threshold=args.motion_threshold,
        min_area=parse_fraction(args.motion_area),
        max_area=parse_fraction(args.motion_max_area),
        min_motion_s=parse_duration(args.motion_min),
        gap_s=parse_duration(args.motion_gap),
        masks=tuple(masks),
    )
    hwaccel = {"auto": default_hwaccel(), "none": None}.get(args.hwaccel, args.hwaccel)
    if hwaccel is not None and not re.match(r"^[a-z0-9_]+$", hwaccel):
        raise ValueError("--hwaccel takes auto, none or an ffmpeg -hwaccel name")
    settings = ContinuousSettings(
        segment_s=parse_duration(args.segment),
        ring_max_bytes=parse_size(args.ring_size),
        ring_max_age_s=parse_duration(args.ring_keep),
        ring_min_age_s=parse_duration(args.ring_min_age),
        preroll_s=parse_duration(args.preroll),
        postroll_s=parse_duration(args.postroll),
        motion=motion,
        hwaccel=hwaccel,
    )
    if not 2 <= settings.segment_s <= 600:
        raise ValueError("--segment must be between 2 s and 10 min")
    if settings.preroll_s > 30 or settings.postroll_s > 30:
        raise ValueError("--preroll and --postroll are at most 30 s")
    return settings


def run(args: argparse.Namespace) -> int:
    continuous: ContinuousSettings | None = None
    try:
        settings = settings_from_args(args)
        if args.continuous:
            continuous = continuous_from_args(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not settings.min_seconds < settings.max_seconds:
        print("error: --min must be shorter than --max", file=sys.stderr)
        return 2
    if not re.match(r"^[A-Za-z0-9_-]+$", settings.camera):
        print("error: --camera takes letters, digits, '_' and '-'", file=sys.stderr)
        return 2
    try:
        target, warnings = load_target(url_file=args.url_file)
    except TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dataset = settings.dataset
    repo = git_work_tree(dataset)
    if repo is not None and not args.allow_inside_git:
        print(
            f"error: {dataset} is inside the git work tree {repo}; real footage must never "
            "be committed. Collect into a folder outside any repository, for example "
            "~/vidette-data/front-door",
            file=sys.stderr,
        )
        return 2
    try:
        binary("ffmpeg")
        binary("ffprobe")
    except MediaToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(target)
    for warning in warnings:
        logger.warning("warning: %s", warning)
    dataset.mkdir(parents=True, exist_ok=True)

    if args.check:
        return asyncio.run(_check(target, settings, continuous))

    collector = (
        Collector(target, settings)
        if continuous is None
        else ContinuousCollector(target, settings, continuous)
    )

    async def main() -> int:
        if args.keep_awake:
            await _keep_awake()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            stats = await collector.run(stop)
        except DatasetLockedError as exc:
            logger.error("error: %s", exc)
            return 2
        logger.info(
            "stopped · %d clip(s) in the dataset, %d session(s) this run, %s recorded, "
            "%s watched in total",
            stats.clips,
            stats.sessions,
            format_size(stats.bytes_recorded),
            format_duration(stats.watched_s),
        )
        return 0

    return asyncio.run(main())


async def _keep_awake() -> None:
    """`caffeinate -i -w <our pid>`: no idle sleep until this process exits, however it
    exits (the assertion dies with the process it watches)."""
    caffeinate = shutil.which("caffeinate")
    if caffeinate is None:
        logger.warning("--keep-awake: caffeinate not found (macOS only); the machine may sleep")
        return
    await asyncio.create_subprocess_exec(
        caffeinate,
        "-i",
        "-w",
        str(os.getpid()),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.info("keeping the machine awake while collecting (caffeinate -i)")


async def _check(
    target: RtspTarget, settings: CollectSettings, continuous: ContinuousSettings | None
) -> int:
    """Setup check that never opens an RTSP session: address, port, disk budget."""
    skip = (RING_DIR,) if continuous is not None else ()
    disk = measure(settings.dataset, settings.limits, skip=skip)
    print(f"camera   {target.redacted}  (from {target.origin})")
    reachable = await tcp_reachable(target.host, target.port)
    if settings.base_station:
        port_state = (
            "open: the base station answers (it says nothing about the camera; --check "
            "sends no RTSP request)"
            if reachable
            else "closed: the base station is off or the address is wrong"
        )
    else:
        port_state = (
            "open: the camera is awake (or a base station answers)"
            if reachable
            else "closed or silent: asleep, or the address is wrong"
        )
    print(f"port     {port_state}")
    print(f"dataset  {settings.dataset}")
    print(f"disk     {disk.describe()}")
    blocked = disk.blocked_reason
    if blocked:
        print(f"         not recording: {blocked}")
    else:
        print(f"         next clip may take up to {format_size(disk.clip_allowance_bytes)}")
    if settings.prune is not None:
        pruner = Pruner(settings.dataset, settings.prune)
        print(f"prune    {pruner.preview(disk)}")
    if continuous is None:
        print(
            f"timing   probe every {format_duration(settings.probe_every_s)}, clips "
            f"{format_duration(settings.min_seconds)} to "
            f"{format_duration(settings.max_seconds)}, "
            f"silence limit {format_duration(settings.silence_s)}"
        )
        return 0 if blocked is None else 1
    ring_bytes = dir_size(settings.dataset / RING_DIR)
    room = ring_bytes + disk.free_bytes - settings.limits.min_free_bytes - CLIP_RESERVE_BYTES
    print(
        f"ring     {format_size(ring_bytes)} of {format_size(continuous.ring_max_bytes)}, "
        f"segments of {format_duration(continuous.segment_s)}, kept at most "
        f"{format_duration(continuous.ring_max_age_s)}"
    )
    if room < continuous.ring_max_bytes:
        print(
            f"         free space allows only {format_size(max(0, room))}: the ring will be "
            "smaller (clips come first)"
        )
    motion = continuous.motion
    print(
        f"motion   {motion.fps:g} fps at {motion.width}x{motion.height}, changed area "
        f"{motion.min_area * 100:g}% to {motion.max_area * 100:g}%, for "
        f"{format_duration(motion.min_motion_s)}; masks: "
        + (", ".join(str(mask) for mask in motion.masks) or "none")
    )
    print(
        f"clips    {format_duration(settings.min_seconds)} to "
        f"{format_duration(settings.max_seconds)}, pre-roll "
        f"{format_duration(continuous.preroll_s)}, post-roll "
        f"{format_duration(continuous.postroll_s)}, rotate {settings.rotation}°, decoder "
        f"{continuous.hwaccel or 'software'}"
    )
    return 0 if blocked is None else 1
