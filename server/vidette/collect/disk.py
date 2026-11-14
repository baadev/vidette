"""Disk guard for the collector: a hard cap on the dataset folder and a free-space floor.

Two limits, both hard:

- the dataset folder never grows past `max_dataset_bytes` (default 5 GB): before each
  recording the collector computes how much one clip may still take and passes it to
  ffmpeg as `-fs`, so even a recording that runs away stops at the cap;
- nothing is recorded while the volume has less than `min_free_bytes` free (default
  10 GB), and a recording in progress is stopped when free space drops below it.

Finalizing a clip remuxes the partial recording into an MP4 next to it, so for a moment
a clip exists twice. The per-clip allowance is therefore half of what is left, minus a
reserve for key frames and the JSON sidecar.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from vidette.units import format_size

GB = 1_000_000_000
MB = 1_000_000
DEFAULT_MAX_DATASET_BYTES = 5 * GB
DEFAULT_MIN_FREE_BYTES = 10 * GB
FRAMES_RESERVE_BYTES = 4 * MB  # 8 frames at ≤ 1280 px plus a sidecar, generously
MIN_CLIP_BYTES = 8 * MB  # below this a recording is not worth starting


@dataclass(frozen=True)
class DiskLimits:
    max_dataset_bytes: int = DEFAULT_MAX_DATASET_BYTES
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES


@dataclass(frozen=True)
class DiskState:
    dataset_bytes: int
    free_bytes: int
    limits: DiskLimits

    @property
    def headroom_bytes(self) -> int:
        """How much more may be written before either limit is reached."""
        return min(
            self.limits.max_dataset_bytes - self.dataset_bytes,
            self.free_bytes - self.limits.min_free_bytes,
        )

    @property
    def clip_allowance_bytes(self) -> int:
        """The `-fs` cap for the next recording (it is briefly stored twice)."""
        return max(0, (self.headroom_bytes - FRAMES_RESERVE_BYTES) // 2)

    @property
    def blocked_reason(self) -> str | None:
        """Why nothing may be recorded right now, or None."""
        if self.free_bytes < self.limits.min_free_bytes:
            return (
                f"only {format_size(self.free_bytes)} free on the disk, the floor is "
                f"{format_size(self.limits.min_free_bytes)} (--min-free)"
            )
        if self.dataset_bytes >= self.limits.max_dataset_bytes:
            return (
                f"the dataset folder holds {format_size(self.dataset_bytes)}, the cap is "
                f"{format_size(self.limits.max_dataset_bytes)} (--max-size)"
            )
        if self.clip_allowance_bytes < MIN_CLIP_BYTES:
            return (
                f"less than {format_size(2 * MIN_CLIP_BYTES + FRAMES_RESERVE_BYTES)} left "
                "before a disk limit"
            )
        return None

    def describe(self) -> str:
        return (
            f"dataset {format_size(self.dataset_bytes)} of "
            f"{format_size(self.limits.max_dataset_bytes)}, "
            f"{format_size(self.free_bytes)} free (floor "
            f"{format_size(self.limits.min_free_bytes)})"
        )


def dir_size(path: Path, *, skip: tuple[str, ...] = ()) -> int:
    """Bytes in regular files under `path` (symlinks are not followed), leaving out the
    top-level folders named in `skip`."""
    total = 0
    for root, dirs, files in os.walk(path):
        if skip and root == str(path):
            dirs[:] = [name for name in dirs if name not in skip]
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue  # a file finalized and removed while we walked
            total += st.st_size
    return total


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def measure(dataset: Path, limits: DiskLimits, *, skip: tuple[str, ...] = ()) -> DiskState:
    """The dataset folder (without the `skip` folders, e.g. the continuous ring, which has
    its own cap) against the limits."""
    return DiskState(
        dataset_bytes=dir_size(dataset, skip=skip), free_bytes=free_bytes(dataset), limits=limits
    )
