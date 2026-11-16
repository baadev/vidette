"""`vidette collect --prune-oldest`: room under `--max-size` by deleting the oldest clips
nobody needs, instead of going blind.

Without it, a dataset folder that reaches its cap stops the collector until someone makes
room. With it, once the folder is past `PRUNE_AT` (95 %) of the cap, the oldest clips go,
each with everything made from it, until the folder is down to `PRUNE_TO` (90 %): one
prune frees about a twentieth of the cap, so it runs now and then, not after every clip.

A clip is never pruned when any of these needs it:

- **labels**: an entry of the dataset's `labels.yaml`, whatever its status (a reviewed
  label, a model's draft waiting for review, a numbered entry on a contact sheet);
- **an alert or a mark** in `vidette serve`'s database: the verdict alerted (or the model
  said alert and a rule kept it quiet), the event was confirmed, sent to Telegram, given
  a 👍/👎 or a reason, or starred;
- **the moment of such an event on any camera**: clips that start within
  `NEAR_BEFORE_S` before to `NEAR_AFTER_S` after it (the other cameras of an alert,
  docs/other-cameras.md, with room for a camera whose video arrives late);
- **faces**: a sighting of an anonymous identity (ADR-0014), or a face decision about a
  known person that names the clip or its event (ADR-0016, they can be taken back);
- **age**: clips younger than `--prune-min-age` (24 h), so a verdict, an answer in
  Telegram or a label has time to arrive.

Fail safe: when something that says what to keep cannot be read (a `labels.yaml` that does
not parse, a database that will not open), nothing is pruned and the collector stops at
the cap as it did before, saying why. A pruned clip goes with its sidecar, key frames
(`frames/<id>/`), face crops (`faces/<id>/`) and early window (`.early/<id>.*`); the
continuous ring and contact sheets are never touched (a sheet shows labelled entries,
which are kept). `vidette serve` keeps its event row: its clip link then answers "Clip not
available", as for any clip gone from a clip folder.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vidette.collect.clips import CLIPS_DIR, EARLY_DIR, FRAMES_DIR
from vidette.collect.disk import FRAMES_RESERVE_BYTES, MIN_CLIP_BYTES, DiskState
from vidette.core.config import PeopleConfig, StorageConfig, _interpolate_node
from vidette.eval.dataset import LABELS_FILE
from vidette.units import format_duration, format_size

logger = logging.getLogger(__name__)

PRUNE_AT = 0.95  # share of --max-size that starts a prune
PRUNE_TO = 0.90  # a prune goes down to this share
DEFAULT_MIN_AGE_S = 24 * 3600.0
RETRY_S = 600.0  # after a prune that could free nothing, the next try waits this long
NEAR_BEFORE_S = 60.0  # clips this long before an alert's start are its other cameras
NEAR_AFTER_S = 180.0  # … and this long after its end (45 s window plus a late camera)
FACES_DIR = "faces"  # vidette.people.sidecar.FACES_DIR (not imported: the face extra)
IDENTITIES_DB = "identities.db"  # vidette.people.identities.DB_FILE
SERVE_CONFIG_DEFAULT = Path("~/.config/vidette/vidette.yaml")

LABELLED = "labelled"
ALERT = "alert or mark"
NEAR_ALERT = "near an alert"
FACES = "faces"

_STAMP = re.compile(r"^(\d{8}-\d{6})")


class ProtectionError(Exception):
    """What must be kept cannot be read; the message says what to fix."""


# --- what is in the dataset -----------------------------------------------------------


@dataclass(frozen=True)
class StoredClip:
    id: str
    files: tuple[Path, ...]  # everything in clips/ with this id: video, sidecar
    started_at: float  # the local time in the id, else the oldest file's mtime


def stored_clips(dataset: Path) -> list[StoredClip]:
    """Every clip in `clips/`, oldest first."""
    groups: dict[str, list[Path]] = {}
    try:
        entries = list(os.scandir(dataset / CLIPS_DIR))
    except FileNotFoundError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
            continue
        clip_id = entry.name.split(".", 1)[0]
        if clip_id:
            groups.setdefault(clip_id, []).append(Path(entry.path))
    clips = []
    for clip_id, files in groups.items():
        started = _stamp(clip_id)
        if started is None:
            started = min(_mtime(path) for path in files)
        # the video before its sidecar: a reader that lists videos never sees half a clip
        files.sort(key=lambda path: (path.name.endswith((".json", ".tmp")), path.name))
        clips.append(StoredClip(clip_id, tuple(files), started))
    clips.sort(key=lambda clip: (clip.started_at, clip.id))
    return clips


def _stamp(clip_id: str) -> float | None:
    match = _STAMP.match(clip_id)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return time.time()


def clip_parts(dataset: Path, clip: StoredClip) -> list[Path]:
    """The clip's files and everything made from it, in deletion order."""
    return [
        *clip.files,
        dataset / FRAMES_DIR / clip.id,
        dataset / FACES_DIR / clip.id,
        dataset / EARLY_DIR / f"{clip.id}.mp4",
        dataset / EARLY_DIR / f"{clip.id}.json",
    ]


def _bytes(path: Path) -> int:
    try:
        st = path.lstat()
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return st.st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def clip_bytes(dataset: Path, clip: StoredClip) -> int:
    return sum(_bytes(part) for part in clip_parts(dataset, clip))


def delete_clip(dataset: Path, clip: StoredClip) -> None:
    for part in clip_parts(dataset, clip):
        if part.is_dir() and not part.is_symlink():
            shutil.rmtree(part)
        else:
            part.unlink(missing_ok=True)


# --- what must be kept ----------------------------------------------------------------


@dataclass(frozen=True)
class ServeStores:
    """Where `vidette serve` keeps what refers to clips."""

    config: Path | None  # None: no serve config, only the default people folders
    database: Path | None
    people_dir: Path
    identities_dir: Path


def default_serve_config() -> Path | None:
    """$VIDETTE_CONFIG, else ~/.config/vidette/vidette.yaml when it exists."""
    env = os.environ.get("VIDETTE_CONFIG")
    if env:
        return Path(env).expanduser()
    path = SERVE_CONFIG_DEFAULT.expanduser()
    return path if path.is_file() else None


def serve_stores(config: Path | None) -> ServeStores:
    """Read only `storage` and `people` of serve's config (secrets elsewhere in it, such
    as a bot token from the environment, are not needed and not read)."""
    if config is None:
        people = PeopleConfig()
        return ServeStores(None, None, people.dir, people.identities.dir)
    try:
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProtectionError(f"cannot read vidette serve's config {config}: {exc}") from None
    if not isinstance(raw, dict):
        raise ProtectionError(f"{config} is not a vidette config (no mapping at the top)")
    picked = {"storage": raw.get("storage") or {}, "people": raw.get("people") or {}}
    missing: set[str] = set()
    interpolated = _interpolate_node(picked, os.environ, missing)
    if missing:
        raise ProtectionError(
            f"{config}: storage or people use unset environment variables: "
            + ", ".join(sorted(missing))
        )
    assert isinstance(interpolated, dict)
    try:
        storage = StorageConfig.model_validate(interpolated["storage"])
        people = PeopleConfig.model_validate(interpolated["people"])
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ProtectionError(f"{config}: {where}: {first['msg']}") from None
    return ServeStores(config, storage.database.expanduser(), people.dir, people.identities.dir)


class _Places:
    """Which paths name a clip of this dataset: `<dataset>/clips/<id>.*` or
    `<dataset>/.early/<id>.*`, by the real path or, failing that, by the folder names
    (a path written by a process with another working directory or `~` still counts;
    a false match only keeps a clip longer)."""

    def __init__(self, dataset: Path) -> None:
        self._real = {
            os.path.realpath(dataset / CLIPS_DIR),
            os.path.realpath(dataset / EARLY_DIR),
        }
        self._name = dataset.name
        self._cache: dict[str, bool] = {}

    def clip_id(self, raw: object) -> str | None:
        if not isinstance(raw, str) or not raw:
            return None
        parent, name = os.path.split(raw)
        mine = self._cache.get(parent)
        if mine is None:
            folder, sub = os.path.split(parent)
            mine = os.path.realpath(os.path.expanduser(parent)) in self._real or (
                sub in (CLIPS_DIR, EARLY_DIR) and os.path.basename(folder) == self._name
            )
            self._cache[parent] = mine
        clip_id = name.split(".", 1)[0]
        return clip_id if mine and clip_id else None


@dataclass
class Protection:
    ids: dict[str, str] = field(default_factory=dict)  # clip id → why it is kept
    windows: list[tuple[float, float]] = field(default_factory=list)  # near an alert
    sources: list[str] = field(default_factory=list)  # what was read, for the log

    def add(self, clip_id: str | None, reason: str) -> None:
        if clip_id:
            self.ids.setdefault(clip_id, reason)

    def reason(self, clip: StoredClip, span_s: float) -> str | None:
        found = self.ids.get(clip.id)
        if found is not None:
            return found
        start, end = clip.started_at, clip.started_at + span_s
        for low, high in self.windows:
            if start <= high and end >= low:
                return NEAR_ALERT
        return None


def load_protection(dataset: Path, stores: ServeStores) -> Protection:
    """Everything that says which clips of `dataset` to keep. Raises ProtectionError when
    a source exists but cannot be read: then nothing may be pruned."""
    keep = Protection()  # a clip kept for several reasons is counted under the first
    places = _Places(dataset)
    event_refs, clip_refs = _people_refs(stores.people_dir, keep)
    if stores.database is not None:
        _serve_events(stores.database, places, keep, event_refs)
    elif event_refs:
        raise ProtectionError(
            f"face decisions in {stores.people_dir} name vidette serve events; pass "
            "--serve-config so their clips can be found"
        )
    _labels(dataset, keep)
    for clip_id in clip_refs:
        keep.add(clip_id, FACES)
    _sightings(stores.identities_dir / IDENTITIES_DB, places, keep)
    return keep


def _labels(dataset: Path, keep: Protection) -> None:
    path = dataset / LABELS_FILE
    if not path.exists():
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProtectionError(f"{path} cannot be read ({exc}); fix it first") from None
    entries = data.get("entries") if isinstance(data, dict) else None
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ProtectionError(f"{path}: `entries` is not a list; fix it first")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        keep.add(str(entry.get("id") or ""), LABELLED)
        media = entry.get("media")
        for item in media if isinstance(media, list) else []:
            if isinstance(item, str) and item.startswith(f"{CLIPS_DIR}/"):
                keep.add(item.removeprefix(f"{CLIPS_DIR}/").split(".", 1)[0], LABELLED)
    keep.sources.append(str(path))


def _people_refs(people_dir: Path, keep: Protection) -> tuple[set[str], set[str]]:
    """Clips and events named by face decisions (ADR-0016): a confirmed face's source
    `confirmed:clip:<id>#n` / `confirmed:event:<id>#n`, a rejection's `where`."""
    events: set[str] = set()
    clips: set[str] = set()
    if not people_dir.is_dir():
        return events, clips

    def note(where: object) -> None:
        if not isinstance(where, str):
            return
        kind, _, ident = where.partition(":")
        ident = ident.split("#", 1)[0]
        if kind == "clip" and ident:
            clips.add(ident)
        elif kind == "event" and ident:
            events.add(ident)

    try:
        folders = [child for child in people_dir.iterdir() if child.is_dir()]
    except OSError as exc:
        raise ProtectionError(f"cannot list {people_dir}: {exc}") from None
    for folder in folders:
        for name in ("faces.json", "person.json"):
            path = folder / name
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProtectionError(f"cannot read {path}: {exc}") from None
            if not isinstance(data, dict):
                continue
            for face in data.get("faces") or []:
                source = face.get("source") if isinstance(face, dict) else None
                if isinstance(source, str) and source.startswith("confirmed:"):
                    note(source.removeprefix("confirmed:"))
            for rejection in data.get("rejected") or []:
                if isinstance(rejection, dict):
                    note(rejection.get("where"))
    keep.sources.append(str(people_dir))
    return events, clips


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Read-only, never creating the file; waits for a writer instead of failing."""
    uri = path.expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def notable(row: sqlite3.Row | dict[str, Any], telegram: set[str]) -> bool:
    """An event someone was told about or said something about."""
    if row["state"] == "confirmed" or row["feedback"] or row["favorite"] or row["id"] in telegram:
        return True
    raw = row["intent"]
    if not raw:
        return False
    try:
        intent = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(intent, dict):
        return False
    if intent.get("review"):
        return True
    verdicts = intent.get("verdicts")
    return any(
        isinstance(verdict, dict)
        and (verdict.get("alert") is True or verdict.get("model_said_alert") is True)
        for verdict in (verdicts if isinstance(verdicts, list) else [])
    )


def _serve_events(database: Path, places: _Places, keep: Protection, wanted: set[str]) -> None:
    if not database.is_file():
        raise ProtectionError(
            f"vidette serve's database {database} does not exist; start vidette serve once "
            "or point --serve-config at the right config"
        )
    try:
        connection = _connect_ro(database)
    except sqlite3.Error as exc:
        raise ProtectionError(f"cannot open {database}: {exc}") from None
    try:
        tables = _tables(connection)
        if "events" not in tables:
            raise ProtectionError(f"{database} has no events table: not vidette's database")
        columns = _columns(connection, "events")
        pick = [
            "id", "started_at", "ended_at", "state", "feedback", "intent",
            "favorite" if "favorite" in columns else "0 AS favorite",
            "source_ref" if "source_ref" in columns else "NULL AS source_ref",
            "clip_path",
        ]  # fmt: skip
        telegram: set[str] = set()
        if "telegram_messages" in tables:
            telegram = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT event_id FROM telegram_messages WHERE event_id IS NOT NULL"
                )
            }
        # A loose filter in SQL (a superset), the exact rule in Python (`notable`).
        query = (
            f"SELECT {', '.join(pick)} FROM events WHERE state = 'confirmed' "
            "OR feedback IS NOT NULL OR instr(intent, 'true') > 0 "
            "OR instr(intent, '\"review\"') > 0"
            + (" OR favorite != 0" if "favorite" in columns else "")
        )
        rows = list(connection.execute(query))
        if telegram:
            marks = ",".join("?" * len(telegram))
            rows += connection.execute(
                f"SELECT {', '.join(pick)} FROM events WHERE id IN ({marks})", sorted(telegram)
            ).fetchall()
        for row in rows:
            if not notable(row, telegram):
                continue
            for raw in (row["source_ref"], row["clip_path"]):
                keep.add(places.clip_id(raw), ALERT)
            start = float(row["started_at"])
            end = float(row["ended_at"]) if row["ended_at"] is not None else start
            keep.windows.append((start - NEAR_BEFORE_S, end + NEAR_AFTER_S))
        if wanted:
            marks = ",".join("?" * len(wanted))
            for row in connection.execute(
                f"SELECT {', '.join(pick)} FROM events WHERE id IN ({marks})", sorted(wanted)
            ):
                for raw in (row["source_ref"], row["clip_path"]):
                    keep.add(places.clip_id(raw), FACES)
    except sqlite3.Error as exc:
        raise ProtectionError(f"cannot read {database}: {exc}") from None
    finally:
        connection.close()
    keep.sources.append(str(database))


def _sightings(database: Path, places: _Places, keep: Protection) -> None:
    """Clips with a sighting of an anonymous identity (ADR-0014); no file: no sightings."""
    if not database.is_file():
        return
    try:
        connection = _connect_ro(database)
        try:
            if "sightings" not in _tables(connection):
                return
            for row in connection.execute("SELECT source_id, clip FROM sightings"):
                keep.add(places.clip_id(row["source_id"]), FACES)
                keep.add(places.clip_id(row["clip"]), FACES)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ProtectionError(f"cannot read {database}: {exc}") from None
    keep.sources.append(str(database))


# --- the plan and the prune -----------------------------------------------------------


@dataclass
class PrunePlan:
    victims: list[tuple[StoredClip, int]]  # oldest first, with the bytes each frees
    protected: Counter[str]  # why → clips kept for it
    young: int  # clips too young to prune
    kept_oldest: StoredClip | None  # the oldest clip that stays

    @property
    def freed_bytes(self) -> int:
        return sum(size for _clip, size in self.victims)


def plan(
    dataset: Path,
    keep: Protection,
    *,
    free_bytes: float,
    now: float,
    min_age_s: float,
    span_s: float,
) -> PrunePlan:
    """The oldest prunable clips that free at least `free_bytes` (all of them for inf)."""
    victims: list[tuple[StoredClip, int]] = []
    protected: Counter[str] = Counter()
    young = 0
    freed = 0
    kept_oldest: StoredClip | None = None
    for clip in stored_clips(dataset):
        why = keep.reason(clip, span_s)
        if why is not None:
            protected[why] += 1
        elif now - clip.started_at < min_age_s:
            young += 1
        elif freed < free_bytes:
            size = clip_bytes(dataset, clip)
            victims.append((clip, size))
            freed += size
            continue
        if kept_oldest is None:
            kept_oldest = clip
    return PrunePlan(victims, protected, young, kept_oldest)


@dataclass(frozen=True)
class PruneSettings:
    serve_config: Path | None = None  # None: no serve database, default people folders
    min_age_s: float = DEFAULT_MIN_AGE_S
    span_s: float = 120.0  # the longest a clip can be (--max), for "near an alert"


@dataclass(frozen=True)
class PruneResult:
    pruned: int
    freed_bytes: int
    before_bytes: int
    cap_bytes: int
    newest_pruned: float | None
    kept_oldest: float | None
    protected: dict[str, int]
    young: int
    min_age_s: float
    error: str | None = None

    def kept_summary(self) -> str:
        parts = [f"{count} {why}" for why, count in sorted(self.protected.items())]
        parts.append(f"{self.young} younger than {format_duration(self.min_age_s)}")
        return ", ".join(parts)

    def line(self) -> str:
        after = self.before_bytes - self.freed_bytes
        return (
            f"pruned {self.pruned} oldest clip(s) up to {_when(self.newest_pruned)}, "
            f"{format_size(self.freed_bytes)} freed · dataset {format_size(self.before_bytes)}"
            f" → {format_size(after)} of {format_size(self.cap_bytes)} (--max-size) · oldest "
            f"kept {_when(self.kept_oldest)} · kept: {self.kept_summary()}"
        )

    def why_nothing(self) -> str:
        if self.error is not None:
            return f"nothing pruned: {self.error}"
        return f"nothing left to prune (kept: {self.kept_summary()})"


def _when(at: float | None) -> str:
    return "-" if at is None else datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")


class Pruner:
    """Decides when a dataset needs room and makes it; never raises."""

    def __init__(
        self,
        dataset: Path,
        settings: PruneSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.dataset = dataset
        self.settings = settings
        self._clock = clock
        self._futile_at: float | None = None
        self.note: str | None = None  # why the last try freed nothing, while it matters

    @staticmethod
    def wanted(disk: DiskState, need_bytes: int = 0) -> bool:
        """Is the folder close enough to its cap to prune?"""
        cap = disk.limits.max_dataset_bytes
        room = cap - disk.dataset_bytes
        return room < max(cap - int(cap * PRUNE_AT), 2 * MIN_CLIP_BYTES + FRAMES_RESERVE_BYTES,
                          need_bytes)  # fmt: skip

    def due(self) -> bool:
        return self._futile_at is None or self._clock() - self._futile_at >= RETRY_S

    def settle(self) -> None:
        """The folder is under the threshold again: forget an earlier futile try."""
        self._futile_at = None
        self.note = None

    def stores(self) -> ServeStores:
        return serve_stores(self.settings.serve_config)

    def describe(self, cap_bytes: int) -> str:
        try:
            stores = self.stores()
        except ProtectionError as exc:
            return f"pruning on, but it will not run until this is fixed: {exc}"
        serve = (
            f"clips vidette serve alerted on or that were marked, and the other cameras at "
            f"that moment ({stores.config})"
            if stores.config is not None
            else "no vidette serve config (--serve-config): its alerts are not known here"
        )
        return (
            f"pruning on: past {format_size(cap_bytes * PRUNE_AT)} the oldest clips go until "
            f"{format_size(cap_bytes * PRUNE_TO)}; kept: labelled clips, {serve}, clips "
            f"with faces, clips younger than {format_duration(self.settings.min_age_s)}"
        )

    def preview(self, disk: DiskState) -> str:
        """What a prune would see now (for --check); deletes nothing."""
        try:
            keep = load_protection(self.dataset, self.stores())
        except ProtectionError as exc:
            return f"on, but nothing can be pruned: {exc}"
        found = plan(self.dataset, keep, free_bytes=float("inf"), now=self._clock(),
                     min_age_s=self.settings.min_age_s, span_s=self.settings.span_s)  # fmt: skip
        result = self._result(found, disk, error=None)
        cap = disk.limits.max_dataset_bytes
        return (
            f"on: past {format_size(cap * PRUNE_AT)} the oldest clips go until "
            f"{format_size(cap * PRUNE_TO)}; {len(found.victims)} clip(s), "
            f"{format_size(found.freed_bytes)} could go now · kept: {result.kept_summary()}"
        )

    def run(self, disk: DiskState) -> PruneResult:
        """Prune down to PRUNE_TO of the cap. Blocking (call it in a thread)."""
        cap = disk.limits.max_dataset_bytes
        target = int(cap * PRUNE_TO)
        try:
            keep = load_protection(self.dataset, self.stores())
            found = plan(
                self.dataset,
                keep,
                free_bytes=disk.dataset_bytes - target,
                now=self._clock(),
                min_age_s=self.settings.min_age_s,
                span_s=self.settings.span_s,
            )
        except ProtectionError as exc:
            return self._done(self._result(None, disk, error=str(exc)))
        except Exception as exc:  # crash containment: a prune never stops the collector
            logger.exception("pruning failed")
            return self._done(self._result(None, disk, error=f"pruning failed: {exc}"))
        pruned: list[tuple[StoredClip, int]] = []
        for clip, size in found.victims:
            try:
                delete_clip(self.dataset, clip)
            except OSError as exc:
                logger.warning("could not delete clip %s: %s", clip.id, exc)
                found.kept_oldest = _older(found.kept_oldest, clip)
                continue
            pruned.append((clip, size))
        found.victims = pruned
        return self._done(self._result(found, disk, error=None))

    def _result(
        self, found: PrunePlan | None, disk: DiskState, *, error: str | None
    ) -> PruneResult:
        victims = found.victims if found is not None else []
        return PruneResult(
            pruned=len(victims),
            freed_bytes=sum(size for _clip, size in victims),
            before_bytes=disk.dataset_bytes,
            cap_bytes=disk.limits.max_dataset_bytes,
            newest_pruned=victims[-1][0].started_at if victims else None,
            kept_oldest=(
                found.kept_oldest.started_at
                if found is not None and found.kept_oldest is not None
                else None
            ),
            protected=dict(found.protected) if found is not None else {},
            young=found.young if found is not None else 0,
            min_age_s=self.settings.min_age_s,
            error=error,
        )

    def _done(self, result: PruneResult) -> PruneResult:
        if result.pruned:
            self._futile_at = None
            self.note = None
        else:
            self._futile_at = self._clock()
            self.note = result.why_nothing()
        return result


def _older(current: StoredClip | None, clip: StoredClip) -> StoredClip:
    return clip if current is None or clip.started_at < current.started_at else current
