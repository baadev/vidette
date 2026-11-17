"""The Telegram bot inside `vidette serve` (docs/telegram.md, ADR-0013).

    event.confirmed ─▶ routing (level's wake rule, face check, quiet hours)
                          ├─ alert ─▶ album (key frames + clip) + a text with buttons
                          │           in the chat's alerts topic, once per event and chat;
                          │           one camera's alerts within `collapse` edit the first
                          └─ digest ─▶ (nothing now: the hourly digest reads the database)

    getUpdates (long poll) ─▶ only `allowed_chats`
        /status /live /quiet /help    commands, answered where they were asked
        button presses                👍 verdict right, 👎 false alarm, "This is …", "?":
                                      the event's feedback, a reviewed label in the clip's
                                      dataset, people confirm; the alert's text is edited

Guards, as everywhere: nothing here can crash or stall the verdict worker. Failures become
rate-limited `telegram.*` system events (never with the token, never with names). An
event without a verdict never alerts; it is counted in the digest as "no verdict".

When Telegram cannot be reached (no connection, 429, 5xx: `TelegramError.transient`):
    an alert ─▶ its row stays, `data.pending` ─▶ sent when the poller hears Telegram
                again (or after a growing pause), marked late, for `late_alerts_for`;
                older ones: one line saying how many, the digest lists them
    an edit of an alert (collapse, the whole clip) ─▶ the same queue, redone then
    the evening digest ─▶ the day is marked `<day>:unsent` and tried again until
                `digest_until`; one that may have arrived is never sent twice
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vidette.assistant.agent import Assistant
from vidette.assistant.tools import Tools
from vidette.calibration.service import Calibrator, Change, safe_review
from vidette.clipfolder import clip_incident
from vidette.core.config import (
    OTHER_SCENARIO,
    ClipFolderConfig,
    PolicyConfig,
    TelegramConfig,
    VidetteConfig,
    Wake,
)
from vidette.core.events import EventState, InProcessEventBus, Subscription
from vidette.db import Database, EventRow, TelegramMessageRow
from vidette.ffmpeg import MediaToolError
from vidette.label.feedback import FeedbackError, LiveVerdict, Recorded, record, restore
from vidette.linked import describe, find_linked, mosaic
from vidette.people import decisions
from vidette.people.decisions import FaceContext, IdentityChoice
from vidette.people.engine import FaceEngine
from vidette.people.identities import IdentityError, IdentityStore, SightingContext
from vidette.people.recurrence import was_raised
from vidette.people.report import STRANGER, PeopleReport
from vidette.people.store import PeopleStore, StoreError
from vidette.telegram import i18n, reasons
from vidette.telegram import text as say
from vidette.telegram.api import TelegramApi, TelegramError, Upload
from vidette.telegram.i18n import t
from vidette.telegram.routing import alerting_verdict, decide, known_only, level_rank
from vidette.verdicts.frames import spread
from vidette.vlm.budget import MetaLedger
from vidette.vlm.endpoint import VlmEndpoint
from vidette.vlm.gate import SlotGate

logger = logging.getLogger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
OFFSET_KEY = "telegram.offset"
QUIET_KEY = "telegram.quiet_until:{chat}"
DIGEST_DAY_KEY = "telegram.digest_day:{chat}"  # the day the evening digest went out
DIGEST_UNSENT = ":unsent"  # after the day: Telegram never got that digest, it may go again
DIGEST_RETRY_S = (30.0, 600.0)  # a digest Telegram did not take: first pause, longest pause
RESEND_RETRY_S = (30.0, 300.0)  # the same for alerts waiting in the queue
_REDO_RANK = {"edit": 0, "refresh": 1, "send": 2}  # what waits in the queue: more covers less
COMMAND_MAX_AGE_S = 600.0  # commands sent while the bot was down longer ago are ignored
ALERT_TOPIC = "event.confirmed"
REFRESH_TOPICS = frozenset({"event.updated", "event.dismissed"})  # the whole clip, judged
EARLY_MARK = ".early"  # a clip path inside a dataset's early windows
MAX_DIGEST_EVENTS = 5000
_FAILURE_EMIT_EVERY = 10
COMMANDS = ("status", "live", "quiet", "tuning", "help")  # `cmd.<name>` in the catalog
QUIET_OFF_WORDS = frozenset({"off", "stop", "выкл", "стоп"})  # /quiet off, in any language
UNJUDGED_REASONS = (  # what a verdict's summary says → `unjudged.<key>` in the catalog
    ("context size", "context"),
    ("timeout", "timeout"),
    ("network", "network"),
    ("budget", "budget"),
    ("auth", "auth"),
    ("rate_limited", "rate_limited"),
    ("server", "server"),
    ("not_found", "not_found"),
    ("bad_answer", "bad_answer"),
    ("unsure", "unsure"),
    ("no frames", "no_frames"),
)


def judged_frames(row: EventRow) -> list[Path]:
    """The frames the judge saw for this event (`intent.frames`) that are still on disk."""
    listed = (row.intent or {}).get("frames")
    if not isinstance(listed, list):
        return []
    paths = [f.get("path") for f in listed if isinstance(f, Mapping)]
    return [Path(path) for path in paths if isinstance(path, str) and Path(path).is_file()]


def subject_frames(row: EventRow) -> list[Path]:
    """The frames chosen for the alert's album (`intent.album`, ADR-0017) still on disk."""
    listed = (row.intent or {}).get("album")
    if not isinstance(listed, list):
        return []
    paths = [f.get("path") for f in listed if isinstance(f, Mapping)]
    return [Path(path) for path in paths if isinstance(path, str) and Path(path).is_file()]


def _midnight(now: float) -> float:
    """Local midnight before `now`, as epoch seconds."""
    return (
        datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )


def unjudged_reason(summary: str | None) -> str:
    if not summary:
        return t("unjudged.queue")
    lowered = summary.lower()
    for found, key in UNJUDGED_REASONS:
        if found in lowered:
            return t(f"unjudged.{key}")
    return t("unjudged.none")


@dataclass
class TelegramStatus:
    alerts_sent: int = 0
    collapsed: int = 0
    digests_sent: int = 0
    answers: int = 0
    failures: int = 0
    last_error: str | None = None
    polling: bool = False


class TelegramService:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        bus: InProcessEventBus,
        *,
        emit: EmitCallback,
        api: TelegramApi | None = None,
        people_store: PeopleStore | None = None,
        face_engine: FaceEngine | None = None,
        identities: IdentityStore | None = None,
        judge_status: Callable[[], Awaitable[str]] | None = None,
        assistant: Assistant | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gate_for: Callable[[VlmEndpoint], SlotGate] | None = None,
        calibration: Calibrator | None = None,
    ) -> None:
        telegram = config.notifications.telegram
        if telegram is None:
            raise ValueError("notifications.telegram is not configured")
        self.config = config
        self.tg: TelegramConfig = telegram
        i18n.use(telegram.language)  # every phrase the bot sends, from here on
        self.db = db
        self.bus = bus
        self._emit = emit
        self.api = api or TelegramApi(telegram.token.get_secret_value(), base_url=telegram.api_url)
        self._people_store = people_store
        self._face_engine = face_engine
        self.calibration = calibration  # 👍/👎 change who is woken (ADR-0020)
        if calibration is not None:
            calibration.announce = self.announce_calibration
        self._identities = identities
        # name prompts waiting for a reply: (chat, prompt message id) → (alert row, face,
        # status). In memory: after a restart the person taps the button again.
        self._prompts: dict[tuple[int, int], tuple[int, int, str]] = {}
        self._judge_status = judge_status
        self._clock = clock
        self._sleep = sleep
        self._subscription: Subscription | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._send_lock = asyncio.Lock()  # alerts one at a time: collapsing needs order
        self._work: set[asyncio.Task[None]] = set()  # slow answers in flight
        self._question_lock = asyncio.Lock()  # one question at a time, any chat
        self._asking_chat: int | None = None  # whose question is being answered
        self._assistant: Assistant | None = assistant
        self._online = asyncio.Event()  # cleared while the poller cannot reach Telegram
        self._online.set()
        self._queued = asyncio.Event()  # something was put in the resend queue
        self._kept = 0  # how many sends and edits went to the queue since the start
        self._dropped: dict[int, list[float]] = {}  # chat → when its too-late alerts were due
        if assistant is None and config.assistant.enabled:
            tools = Tools(
                config, db, identities=identities, camera_name=self.camera_name,
                names=telegram.names, mark=self._mark_from_question,
                digest=self._digest_for_question, clock=clock,
            )  # fmt: skip
            self._assistant = Assistant(
                config, tools, ledger=MetaLedger(db), clock=clock, gate_for=gate_for
            )
        self.status = TelegramStatus()

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._tasks:
            return
        self._subscription = self.bus.subscribe("event.*")
        self._tasks = [
            asyncio.create_task(self._alerts(self._subscription), name="telegram:alerts"),
            asyncio.create_task(self._poll(), name="telegram:poll"),
            asyncio.create_task(self._digests(), name="telegram:digest"),
            asyncio.create_task(self._resend(), name="telegram:resend"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = [*self._tasks, *self._work], []
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.api.aclose()
        if self._assistant is not None:
            await self._assistant.aclose()

    async def _failed(self, what: str, exc: BaseException) -> None:
        self.status.failures += 1
        self.status.last_error = f"{what}: {exc}"[:300]
        logger.warning("telegram %s failed: %s", what, exc)
        if self.status.failures == 1 or self.status.failures % _FAILURE_EMIT_EVERY == 0:
            with contextlib.suppress(Exception):
                await self._emit(
                    "telegram.failed",
                    {"what": what, "error": str(exc)[:300], "count": self.status.failures},
                )

    async def _pause(self, seconds: float) -> None:
        """Wait `seconds` before trying Telegram again; while the poller cannot reach it,
        only until it can (whichever comes first)."""
        if self._online.is_set():
            await self._sleep(seconds)
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        online = asyncio.ensure_future(self._online.wait())
        try:
            await asyncio.wait({sleeper, online}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, online):
                task.cancel()

    def _may_wait(self, test: bool = False) -> bool:
        """Whether what Telegram could not take waits in the queue (`late_alerts_for`)."""
        return not test and self.tg.seconds("late_alerts_for") > 0

    async def _keep(self, row_id: int, what: str, exc: TelegramError) -> None:
        """Telegram could not take an edit of an alert (`edit`: render it again; `refresh`:
        the whole clip's verdict): redo it when it answers again. What already waits and
        does more stays: a send renders everything, a refresh renders too."""
        message = await self.db.get_telegram_message(row_id)
        if message is None:
            return
        data = dict(message.data)
        waiting = data.get("pending")
        before = waiting.get("what") if isinstance(waiting, Mapping) else None
        if _REDO_RANK.get(str(before), -1) < _REDO_RANK[what]:
            data["pending"] = {"what": what, "at": self._clock()}
            await self.db.update_telegram_message(row_id, data=data)
        self._kept += 1
        self._queued.set()
        await self._failed(f"{what} (kept to redo)", exc)

    # --- settings and small lookups ------------------------------------------------------

    def camera_name(self, camera: str) -> str:
        return self.tg.camera_names.get(camera, camera)

    def _thread(self, chat: int, kind: str) -> int | None:
        topic = self.tg.topics.get(chat)
        return None if topic is None else getattr(topic, kind)

    def _policy(self, name: str | None) -> PolicyConfig | None:
        return next((p for p in self.config.policies if p.name == name), None)

    async def quiet_until(self, chat: int) -> float | None:
        raw = await self.db.get_meta(QUIET_KEY.format(chat=chat))
        try:
            return None if raw is None else float(raw)
        except ValueError:
            return None

    def _level_title(self, policy: PolicyConfig | None, level: str | None) -> str:
        if policy is None or not policy.levels:
            return t("level.by_policy")
        if level is None:
            return t("level.none")
        config = policy.levels.get(level)
        return (config.title if config and config.title else level) or level

    def _scenario_title(self, policy: PolicyConfig | None, scenario: str | None) -> str:
        if not scenario:
            return t("scenario.quiet")
        if scenario == OTHER_SCENARIO:
            return t("scenario.other")
        if policy is not None:
            found = policy.scenario_configs().get(scenario)
            if found is not None and found.title:
                return found.title
        return scenario.replace("_", " ")

    # --- alerts ------------------------------------------------------------------------

    async def _alerts(self, subscription: Subscription) -> None:
        while True:
            topic, payload = await subscription.get()
            event_id = payload.get("id")
            if not isinstance(event_id, str) or topic not in REFRESH_TOPICS | {ALERT_TOPIC}:
                continue
            try:
                row = await self.db.get_event(event_id)
                if row is None:
                    continue
                if topic == ALERT_TOPIC:
                    await self.notify_event(row)
                else:
                    await self.refresh_event(row)
            except Exception as exc:  # the loop survives anything
                await self._failed("alert", exc)

    async def refresh_event(self, row: EventRow) -> None:
        """The whole clip was judged after an early alert: edit that alert to the final
        verdict, attach the clip, and buzz once more only when the level went up."""
        async with self._send_lock:
            for message in await self.db.telegram_alerts_for_event(row.id):
                if message.chat_id not in self.tg.recipient_chats:
                    continue
                await self._refresh_or_keep(message, row)

    async def _refresh_or_keep(self, message: TelegramMessageRow, row: EventRow) -> None:
        try:
            await self._refresh_one(message, row)
        except Exception as exc:
            if isinstance(exc, TelegramError) and exc.transient and self._may_wait():
                await self._keep(message.id, "refresh", exc)  # redone when Telegram answers
            else:
                await self._failed("refresh", exc)

    async def _refresh_one(self, message: TelegramMessageRow, row: EventRow) -> None:
        target = message
        if message.kind == "collapsed":
            into = message.data.get("into")
            found = await self.db.get_telegram_message(into) if isinstance(into, int) else None
            if found is None:
                return
            target = found
        if target.message_id is None:
            return
        verdict = alerting_verdict(row.intent, row.policy)
        policy = self._policy(row.policy or verdict.get("policy"))
        rank = level_rank(policy, verdict.get("level"))
        data = dict(target.data)
        if message.kind == "alert":
            if row.state != EventState.confirmed.value:
                data["note"] = t("note.clip_not_alert")
            elif "final" in (row.intent or {}):  # the clip got no verdict
                data["note"] = t("note.clip_unjudged")
            else:
                data["note"] = t("note.clip_refined")
            if not data.get("clip_attached"):
                await self._attach_clip(target, row, data)
            data["updated_at"] = self._clock()
            await self.db.update_telegram_message(target.id, data=data)
            await self._render(target.id, keyboard="main")
        upgraded = row.state == EventState.confirmed.value and rank < int(data.get("rank", 99))
        if upgraded:
            facts = self._facts(row, verdict, policy)
            sent = await self.api.send_message(
                target.chat_id,
                say.upgrade_text(facts, now=self._clock()),
                thread_id=target.thread_id,
                reply_to=target.message_id,
            )
            await self.db.insert_telegram_message(
                target.chat_id, "upgrade", thread_id=target.thread_id,
                message_id=int(sent["message_id"]), event_id=row.id, camera=row.camera,
                level=verdict.get("level"), data={"into": target.id},
            )  # fmt: skip
            data = dict((await self.db.get_telegram_message(target.id) or target).data)
            data["rank"] = rank
            await self.db.update_telegram_message(target.id, data=data)

    async def _attach_clip(
        self, message: TelegramMessageRow, row: EventRow, data: dict[str, Any]
    ) -> None:
        """The whole clip under an early alert: the album's last item becomes the clip,
        or the clip answers the alert when there is no album. Too big: a link."""
        clip = Path(row.clip_path) if row.clip_path else None
        if clip is None or not clip.is_file() or EARLY_MARK in clip.parts:
            return
        size_mb = clip.stat().st_size / 1_000_000
        if size_mb > self.tg.max_video_mb:
            base = self.tg.clip_url or self.config.server.base_url
            link = f"{base.rstrip('/')}/api/v1/events/{row.id}/clip.mp4" if base else None
            data["video_note"] = t("video.too_big_whole", mb=f"{size_mb:.0f}") + (
                f'<a href="{link}">{t("video.watch_home")}</a>' if link else t("video.watch_app")
            )
            data["clip_attached"] = True
            return
        upload = (clip.name, await asyncio.to_thread(clip.read_bytes), "video/mp4")
        album = [m for m in data.get("album") or [] if isinstance(m, int)]
        assert message.message_id is not None
        if album:
            await self.api.edit_message_media(message.chat_id, album[-1], "video", upload)
        else:
            await self.api.send_reply_file(
                message.chat_id, "video", upload, reply_to=message.message_id,
                thread_id=message.thread_id,
            )  # fmt: skip
        data["clip_attached"] = True

    async def notify_event(self, row: EventRow, *, test: bool = False) -> list[TelegramMessageRow]:
        """Alert every recipient the routing says to wake. With `test`, every recipient
        gets it in the alerts topic, marked as a test, whatever the routing says."""
        verdict = alerting_verdict(row.intent, row.policy)
        policy = self._policy(row.policy or verdict.get("policy"))
        alert = row.state == EventState.confirmed.value
        wake = policy.wake_for(verdict.get("label")) if policy is not None else Wake.unless_quiet
        people = (row.intent or {}).get("people")
        people = people if isinstance(people, Mapping) and "seen" in people else None
        persons = verdict.get("persons") if isinstance(verdict.get("persons"), int) else None
        calibrated = say.calibrated(row.intent)
        sent: list[TelegramMessageRow] = []
        async with self._send_lock:
            for chat in self.tg.recipient_chats:
                quiet_until = await self.quiet_until(chat)
                quiet = quiet_until is not None and quiet_until > self._clock()
                route = decide(wake, people, persons, quiet=quiet) if alert else "digest"
                if route == "alert" and calibrated is not None:
                    route = "digest"  # a step of calibration (ADR-0020); the digest lists it
                note = None
                if test and route != "alert":
                    note = self._why_digest(alert, wake, people, persons, quiet, verdict)
                    if alert and calibrated is not None and note == t("why.plain"):
                        note = t("why.calibrated")
                elif route != "alert":
                    continue
                message = await self._alert(chat, row, verdict, policy, test=test, note=note)
                if message is not None:
                    sent.append(message)
        return sent

    @staticmethod
    def _why_digest(
        alert: bool,
        wake: Wake,
        people: Mapping[str, Any] | None,
        persons: int | None,
        quiet: bool,
        verdict: Mapping[str, Any],
    ) -> str:
        if not alert:
            if verdict.get("abstained"):
                return t("why.no_verdict")
            return t("why.not_alert")
        if quiet and wake is not Wake.always:
            return t("why.quiet")
        if wake is Wake.unless_known and known_only(people, persons):
            return t("why.known")
        return t("why.plain")

    def _facts(
        self,
        row: EventRow,
        verdict: Mapping[str, Any],
        policy: PolicyConfig | None,
        data: Mapping[str, Any] | None = None,
    ) -> say.AlertFacts:
        data = data or {}
        people = (row.intent or {}).get("people")
        persons = verdict.get("persons") if isinstance(verdict.get("persons"), int) else None
        description = verdict.get("rationale") or row.summary
        if verdict.get("abstained") or not verdict:
            description = t("alert.no_verdict", why=unjudged_reason(row.summary))
            rationale = str(verdict.get("rationale") or "")
            if rationale.startswith("unsure: "):
                description = t("alert.judge_unsure", why=rationale.removeprefix("unsure: "))
        return say.AlertFacts(
            camera=self.camera_name(row.camera),
            started_at=row.started_at,
            level_rank=level_rank(policy, verdict.get("level")),
            level_title=self._level_title(policy, verdict.get("level")),
            description=str(description) if description else None,
            who=say.who_line(
                people if isinstance(people, Mapping) else None,
                persons,
                names=self.tg.names,
                camera_name=self.camera_name,
            )
            + (t("who.raised") if was_raised(policy, verdict) else ""),
            alert=row.state == EventState.confirmed.value,
            test=bool(data.get("test")),
            note=data.get("note"),
            extra=tuple(data.get("extra") or ()),
            mark=data.get("mark")
            or (
                f"<i>{t('alert.label_failed', problem=escape(str(data['problem'])))}</i>"
                if data.get("problem")
                else None
            ),
            video_note=data.get("video_note"),
            stream_note=say.stream_note((row.intent or {}).get("stream")),
            late=float(data["late"]) if isinstance(data.get("late"), int | float) else None,
        )

    async def _alert(
        self,
        chat: int,
        row: EventRow,
        verdict: Mapping[str, Any],
        policy: PolicyConfig | None,
        *,
        test: bool,
        note: str | None,
    ) -> TelegramMessageRow | None:
        now = self._clock()
        rank = level_rank(policy, verdict.get("level"))
        if not test:
            since = now - self.tg.seconds("collapse")
            last = await self.db.last_telegram_alert(chat, row.camera, since=since, tests=False)
            if last is not None and last.event_id != row.id and last.data.get("rank", 99) <= rank:
                return await self._collapse(chat, last, row, verdict)
        thread = self._thread(chat, "alerts")
        data: dict[str, Any] = {"rank": rank, "test": test, "note": note}
        try:
            row_id = await self.db.insert_telegram_message(
                chat, "test" if test else "alert", thread_id=thread, event_id=row.id,
                camera=row.camera, level=verdict.get("level"), data=data,
            )  # fmt: skip
        except ValueError:
            return None  # this chat already has this event: one alert per event
        if (row.intent or {}).get("phase") == "early" and note is None:
            data["note"] = t("note.early")
        if not await self._deliver(row_id, chat, thread, row, verdict, policy, data):
            return None
        return await self.db.get_telegram_message(row_id)

    async def _deliver(
        self,
        row_id: int,
        chat: int,
        thread: int | None,
        row: EventRow,
        verdict: Mapping[str, Any],
        policy: PolicyConfig | None,
        data: dict[str, Any],
    ) -> bool:
        """Send one alert row: its album (unless that went out already), then the text
        with the buttons. When Telegram could not take it (`TelegramError.transient`), the
        row stays with `data.pending` and goes when Telegram answers again
        (`resend_pending`); any other failure deletes it, and the digest lists the event
        instead. True when it was sent."""
        may_wait = self._may_wait(bool(data.get("test")))
        try:
            if not isinstance(data.get("album"), list):
                data["album"] = await self._send_album(chat, thread, row, data, may_wait=may_wait)
            album = [m for m in data["album"] if isinstance(m, int)]
            facts = self._facts(row, verdict, policy, data)
            sent = await self.api.send_message(
                chat,
                say.alert_text(facts, now=self._clock()),
                thread_id=thread,
                reply_markup=self._keyboard(row_id, row),
                reply_to=album[0] if album else None,
            )
        except Exception as exc:
            if isinstance(exc, TelegramError) and exc.transient and may_wait:
                data["pending"] = {"what": "send", "at": self._clock()}
                await self.db.update_telegram_message(row_id, data=data)
                self._kept += 1
                self._queued.set()
                await self._failed("alert (kept to send late)", exc)
                return False
            await self.db.delete_telegram_message(row_id)  # the digest will count it instead
            await self._failed("alert", exc)
            return False
        data.pop("pending", None)
        data["sent_at"] = self._clock()
        clip = row.clip_path or ""
        data["early"] = EARLY_MARK in Path(clip).parts
        data["clip_attached"] = bool(clip) and not data["early"]
        await self.db.update_telegram_message(row_id, message_id=int(sent["message_id"]), data=data)
        self.status.alerts_sent += 1
        return True

    async def _collapse(
        self, chat: int, last: TelegramMessageRow, row: EventRow, verdict: Mapping[str, Any]
    ) -> TelegramMessageRow | None:
        try:
            await self.db.insert_telegram_message(
                chat, "collapsed", thread_id=last.thread_id, message_id=last.message_id,
                event_id=row.id, camera=row.camera, level=verdict.get("level"),
                data={"into": last.id},
            )  # fmt: skip
        except ValueError:
            return None
        data = dict(last.data)
        line = say.extra_line(row.started_at, verdict.get("rationale"), now=self._clock())
        data["extra"] = [*(data.get("extra") or []), line]
        await self.db.update_telegram_message(last.id, data=data)
        self.status.collapsed += 1
        await self._edit(last.id)
        return await self.db.get_telegram_message(last.id)

    async def _edit(self, row_id: int) -> None:
        """Render an alert again; when Telegram cannot be reached, it waits in the queue."""
        try:
            await self._render(row_id, keyboard="main")
        except TelegramError as exc:
            if not (exc.transient and self._may_wait()):
                raise
            await self._keep(row_id, "edit", exc)

    def _media_for(self, row: EventRow) -> tuple[list[Path], Path | None]:
        """The album's frames and the clip. Frames that show the subject (`intent.album`:
        the best moments of faces, the frames the judge said show what it is about);
        without those, the judged frames from the first one with a person in it; else
        the key frames from the clip's dataset sidecar, else the snapshot. At most
        `frames`: an album of an empty street tells nobody anything."""
        clip = Path(row.clip_path) if row.clip_path else None
        album = subject_frames(row)
        if album:
            return album[: self.tg.frames], clip if clip and clip.is_file() else None
        frames = judged_frames(row)
        verdict = alerting_verdict(row.intent, row.policy)
        seen_at = verdict.get("person_at_s")
        timed = [
            (Path(f["path"]), f.get("t"))
            for f in (row.intent or {}).get("frames") or []
            if isinstance(f, Mapping) and isinstance(f.get("path"), str)
        ]
        if isinstance(seen_at, int | float) and frames:
            later = [
                path for path, at in timed
                if isinstance(at, int | float) and at >= seen_at - 0.05 and path.is_file()
            ]  # fmt: skip
            if later:
                return later[: min(2, self.tg.frames)], clip if clip and clip.is_file() else None
        if frames:
            return spread(frames, min(2, self.tg.frames)), clip if clip and clip.is_file() else None
        if clip is not None and clip.is_file():
            root = clip.parent.parent if clip.parent.name == "clips" else clip.parent
            with contextlib.suppress(OSError, ValueError, ValidationError):
                incident = clip_incident(ClipFolderConfig(path=root, camera=row.camera), clip)
                frames = [Path(m) for m in incident.media[1:] if Path(m).is_file()]
        if not frames and row.snapshot_path and Path(row.snapshot_path).is_file():
            frames = [Path(row.snapshot_path)]
        return spread(frames, self.tg.frames), clip if clip and clip.is_file() else None

    async def _send_album(
        self,
        chat: int,
        thread: int | None,
        row: EventRow,
        data: dict[str, Any],
        *,
        may_wait: bool = False,
    ) -> list[int]:
        """The album above an alert; [] when there is nothing to show or it failed (the
        text then says the frames were not sent). With `may_wait`, a failure that says
        Telegram cannot be reached is raised instead: the whole alert waits for it."""
        frames, clip = self._media_for(row)
        media: list[dict[str, Any]] = []
        files: dict[str, Upload] = {}
        for index, frame in enumerate(frames):
            key = f"frame{index}"
            files[key] = (frame.name, await asyncio.to_thread(frame.read_bytes), "image/jpeg")
            media.append({"type": "photo", "media": f"attach://{key}"})
        if clip is not None:
            size_mb = clip.stat().st_size / 1_000_000
            if size_mb <= self.tg.max_video_mb:
                files["clip"] = (clip.name, await asyncio.to_thread(clip.read_bytes), "video/mp4")
                media.append(
                    {"type": "video", "media": "attach://clip", "supports_streaming": True}
                )
            else:
                base = self.tg.clip_url or self.config.server.base_url
                link = f"{base.rstrip('/')}/api/v1/events/{row.id}/clip.mp4" if base else None
                data["video_note"] = t("video.too_big", mb=f"{size_mb:.0f}") + (
                    f'<a href="{link}">{t("video.watch_home")}</a>'
                    if link
                    else t("video.watch_app")
                )
        if not media:
            return []
        try:
            if len(media) == 1:  # an album needs two items: one file goes alone
                key = next(iter(files))
                one = await self.api.send_file(
                    chat, media[0]["type"], files[key], thread_id=thread, silent=True
                )
                return [int(one["message_id"])]
            sent = await self.api.send_media_group(
                chat, media, files, thread_id=thread, silent=True
            )
        except TelegramError as exc:
            if exc.transient and may_wait:
                raise
            await self._failed("album", exc)
            data["note"] = (data.get("note") or "") + t("alert.frames_failed")
            return []
        return [int(message["message_id"]) for message in sent]

    def _keyboard(
        self,
        row_id: int,
        row: EventRow,
        *,
        keyboard: str = "main",
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The buttons under an alert. Before any answer: 👍 👎 / Who is it? Not sure. After
        one, every evaluation shrinks to one button that says what was chosen
        ("👎 walked past", "👤 familiar “Neighbour”"); tapping it takes the answer back and
        brings the full buttons again. No bulky keyboards stay on old alerts."""
        data = data or {}

        def button(text: str, code: str) -> dict[str, str]:
            return {"text": text, "callback_data": f"a:{row_id}:{code}"}

        report = self._report(row)
        faces = [] if report is None else report.seen[:3]
        if keyboard == "reasons":
            verdict = alerting_verdict(row.intent, row.policy)
            listed = [button(r.button, f"r:{r.code}") for r in reasons.likely(row.intent, verdict)]
            rows = [listed[i : i + 2] for i in range(0, len(listed), 2)]
            rows.append([button(t("kb.no_reason"), "rn")])
            return {"inline_keyboard": rows}
        if keyboard == "picker" and len(faces) == 1:
            keyboard = f"face:{faces[0].n}"
        if keyboard == "picker":
            rows = [
                [button(t("kb.face", n=face.n, word=self._face_word(face)), f"f:{face.n}")]
                for face in faces
            ]
            rows.append([button(t("kb.back"), "back")])
            return {"inline_keyboard": rows}
        if keyboard.startswith("face:") and report is not None:
            n = int(keyboard.removeprefix("face:"))
            face = report.get(n)
            people = self._people_store.people() if self._people_store is not None else []
            rows = []
            if self.tg.names and people:
                ours = [
                    button(t("kb.ours", name=p.name), f"p:{n}:{i}")
                    for i, p in enumerate(people[:6])
                ]
                rows += [ours[i : i + 2] for i in range(0, len(ours), 2)]
            if self._identities is not None:
                if self.tg.names:
                    familiar = [r for r in self._identities.entries(status="familiar") if r.name][
                        :4
                    ]
                    known = [
                        button(t("kb.familiar", name=r.name), f"i:{n}:{r.id}") for r in familiar
                    ]
                    rows += [known[i : i + 2] for i in range(0, len(known), 2)]
                rows.append(
                    [
                        button(t("kb.new_familiar"), f"nf:{n}"),
                        button(t("kb.new_identity"), f"ni:{n}"),
                    ]
                )
            rows.append([button(t("kb.stranger"), f"s:{n}")])
            if face is not None and face.confirmed:
                rows.append([button(t("kb.undo_face"), f"u:{n}")])
            rows.append([button(t("kb.back"), "back")])
            return {"inline_keyboard": rows}
        answered = data.get("answer")
        decided = {
            str(k): str(v) for k, v in (data.get("faces") or {}).items() if isinstance(v, str)
        }
        extra = self._extra_buttons(row_id, row)
        if not answered and not decided:
            second = [button(t("kb.unsure"), "unk")]
            if self._can_say_ours(row):
                second.insert(0, button(t("kb.who"), "own"))
            rows = [[button(t("kb.right"), "ok"), button(t("kb.wrong"), "no")], second]
            return {"inline_keyboard": rows + ([extra] if extra else [])}
        line: list[dict[str, str]] = []
        if answered:
            line.append(button(say.answer_button(data), "undo"))
        else:
            line += [button("👍", "ok"), button("👎", "no")]
        for number, word in sorted(decided.items())[:2]:
            line.append(button(f"👤 {word}", f"u:{number}"))
        if not decided and answered and self._can_say_ours(row):
            line.append(button(t("kb.who"), "own"))
        return {"inline_keyboard": [line] + ([extra] if extra else [])}

    def _extra_buttons(self, row_id: int, row: EventRow) -> list[dict[str, str]]:
        """Buttons that look, not answer: the other cameras at that moment (ADR-0017),
        when there are other clip-folder cameras to look at."""
        if not row.clip_path or len(self.config.clip_folders) < 2:
            return []
        return [{"text": t("kb.other_cameras"), "callback_data": f"a:{row_id}:x"}]

    @staticmethod
    def _face_word(face: Any) -> str:
        if face.status == "known":
            return str(face.name or face.person)
        if face.identity is not None:
            return face.identity.name or f"№{face.identity.id}"
        return t("face.small") if face.status == "unclear" else t("face.unknown")

    def _report(self, row: EventRow) -> PeopleReport | None:
        try:
            return PeopleReport.model_validate((row.intent or {}).get("people"))
        except ValidationError:
            return None

    def _can_say_ours(self, row: EventRow) -> bool:
        """ "Who is it?" when a face was found and there is something to say about it: an
        enrolled person (with `names`), or anonymous identities."""
        report = self._report(row)
        if report is None or not report.seen or self._people_store is None:
            return False
        return self._identities is not None or (self.tg.names and bool(self._people_store.people()))

    async def _render(self, row_id: int, *, keyboard: str = "main") -> None:
        message = await self.db.get_telegram_message(row_id)
        if message is None or message.message_id is None or message.event_id is None:
            return
        event = await self.db.get_event(message.event_id)
        if event is None:
            return
        verdict = alerting_verdict(event.intent, event.policy)
        policy = self._policy(event.policy or verdict.get("policy"))
        facts = self._facts(event, verdict, policy, message.data)
        await self.api.edit_message_text(
            message.chat_id,
            message.message_id,
            say.alert_text(facts, now=self._clock()),
            reply_markup=self._keyboard(row_id, event, keyboard=keyboard, data=message.data),
        )

    # --- updates: commands and buttons -----------------------------------------------------

    async def _poll(self) -> None:
        with contextlib.suppress(TelegramError):
            await self.api.set_my_commands([(name, t(f"cmd.{name}")) for name in COMMANDS])
        raw = await self.db.get_meta(OFFSET_KEY)
        offset = int(raw) if raw and raw.isdigit() else None
        delay = 1.0
        while True:
            try:
                updates = await self.api.get_updates(offset)
                self.status.polling = True
                self._online.set()  # what waits for Telegram goes now (_pause)
                delay = 1.0
            except TelegramError as exc:
                self.status.polling = False
                if exc.code is None or exc.transient:
                    self._online.clear()
                await self._failed("poll", exc)
                if exc.code in (401, 404):
                    delay = 600.0  # a revoked token will not come back by itself
                elif exc.code == 409:
                    delay = 30.0  # another getUpdates consumer or a webhook
                else:
                    delay = min(delay * 2, 60.0)
                await self._sleep(delay)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                try:
                    await self.handle_update(update)
                except Exception as exc:
                    await self._failed("update", exc)
            if updates and offset is not None:
                await self.db.set_meta(OFFSET_KEY, str(offset))
            await asyncio.sleep(0)  # an empty answer must never become a busy loop

    def allowed(self, chat: object) -> bool:
        return isinstance(chat, int) and chat in self.tg.allowed_chats

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            await self._button(callback)
            return
        message = update.get("message")
        if isinstance(message, Mapping):
            if await self._prompt_reply(message):
                return
            text = message.get("text")
            if isinstance(text, str) and not text.startswith("/"):
                await self._question(message)
                return
            await self._command(message)

    async def _command(self, message: Mapping[str, Any]) -> None:
        chat = (message.get("chat") or {}).get("id")
        text = message.get("text")
        if not self.allowed(chat) or not isinstance(text, str) or not text.startswith("/"):
            return
        assert isinstance(chat, int)
        if self._clock() - float(message.get("date") or 0) > COMMAND_MAX_AGE_S:
            return
        thread = message.get("message_thread_id")
        thread = thread if isinstance(thread, int) else None
        word, _sep, rest = text[1:].partition(" ")
        command = word.split("@", 1)[0].lower()
        now = self._clock()
        if command == "status":
            await self.api.send_message(chat, await self.status_text(), thread_id=thread)
            if chat in self.tg.recipient_chats:
                await self.send_digest(chat, thread=thread)
        elif command == "live":
            await self.api.send_message(chat, say.live_text(), thread_id=thread)
        elif command == "quiet":
            until = (
                None
                if rest.strip().lower() in QUIET_OFF_WORDS
                else now + (self.tg.seconds("quiet_for"))
            )
            await self.db.set_meta(QUIET_KEY.format(chat=chat), repr(until or 0.0))
            await self.api.send_message(chat, say.quiet_text(until, now=now), thread_id=thread)
        elif command == "tuning":
            await self.send_tuning(chat, thread=thread)
        elif command in ("start", "help"):
            await self.api.send_message(chat, say.help_text(), thread_id=thread)

    async def _button(self, callback: Mapping[str, Any]) -> None:
        message = callback.get("message")
        callback_id = str(callback.get("id") or "")
        if not isinstance(message, Mapping):
            return
        chat = (message.get("chat") or {}).get("id")
        if not self.allowed(chat):
            return  # never answer strangers, not even with an error
        parts = str(callback.get("data") or "").split(":")
        if len(parts) == 3 and parts[0] == "c" and parts[1].isdigit() and parts[2] == "undo":
            assert isinstance(chat, int)  # allowed() said so
            said = await self.undo_calibration(int(parts[1]), chat, message)
            await self.api.answer_callback_query(callback_id, said)
            return
        if len(parts) < 3 or parts[0] != "a" or not parts[1].isdigit():
            await self.api.answer_callback_query(callback_id)
            return
        row = await self.db.get_telegram_message(int(parts[1]))
        if (
            row is None
            or row.chat_id != chat
            or row.message_id != message.get("message_id")
            or row.event_id is None
        ):
            await self.api.answer_callback_query(callback_id, t("toast.gone"))
            return
        code = parts[2]
        toast: str | None = None
        if code == "own":
            await self._render(row.id, keyboard="picker")
            toast = t("toast.who")
        elif code == "back":
            await self._render(row.id, keyboard="main")
        elif code in ("ok", "unk"):
            toast = await self.answer(row, "right" if code == "ok" else "unsure")
        elif code == "no":
            await self.answer(row, "wrong", keyboard="reasons")
            toast = t("toast.why_false")
        elif code == "r" and len(parts) == 4 and parts[3] in reasons.BY_CODE:
            if parts[3] == "other":
                await self._ask(row, "reason")
                toast = t("toast.reply_what")
            else:
                toast = await self.answer(row, "wrong", reason=parts[3])
        elif code == "rn":
            await self._render(row.id, keyboard="main")
            await safe_review(self.calibration)  # a 👎 without a reason counts too
        elif code == "undo":
            toast = await self.take_back(row)
        elif code == "p" and len(parts) == 5 and parts[3].isdigit() and parts[4].isdigit():
            toast = await self.confirm_ours(row, int(parts[3]), int(parts[4]))
        elif code == "f" and len(parts) == 4 and parts[3].isdigit():
            await self._render(row.id, keyboard=f"face:{parts[3]}")
            toast = t("toast.face_who", n=parts[3])
        elif code in ("i", "s", "u") and len(parts) >= 4 and parts[3].isdigit():
            face = int(parts[3])
            choice: str | IdentityChoice | None = STRANGER if code == "s" else None
            if code == "i" and len(parts) == 5 and parts[4].isdigit():
                choice = IdentityChoice(identity=int(parts[4]))
            toast = await self.face_decision(row, face, choice)
        elif code in ("nf", "ni") and len(parts) == 4 and parts[3].isdigit():
            await self._ask(row, "familiar" if code == "nf" else "unknown", face=int(parts[3]))
            toast = t("toast.reply_name")
        elif code == "x":
            await self.api.answer_callback_query(callback_id, t("toast.collecting"))
            self._background(self.send_other_cameras(row), "other cameras")
            return
        await self.api.answer_callback_query(callback_id, toast)

    def _background(self, work: Awaitable[Any], what: str) -> None:
        """Slow work (stitching, a question to the model) off the update loop, so buttons
        and commands keep being answered meanwhile; failures are reported, never raised."""

        async def run() -> None:
            try:
                await work
            except Exception as exc:
                await self._failed(what, exc)

        task = asyncio.create_task(run(), name=f"telegram:{what}")
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    async def send_other_cameras(
        self,
        message: TelegramMessageRow,
        *,
        reply_to: int | None = None,
        at: float | None = None,
        event: EventRow | None = None,
    ) -> str:
        """The cameras at the moment of the alert's event (or `at`) as one mosaic video
        answering the alert, with what each camera recorded; said plainly when no other
        camera recorded anything. Returns what was said, for the conversation."""
        if event is None:
            assert message.event_id is not None
            event = await self.db.get_event(message.event_id)
        if event is None:
            return t("event.deleted")
        target = reply_to if reply_to is not None else message.message_id
        await self.api.send_chat_action(
            message.chat_id, "upload_video", thread_id=message.thread_id
        )
        linked = await find_linked(event, self.config, self.db, at=at)
        cameras = [folder.camera for folder in self.config.clip_folders]
        lines = describe(linked, camera_name=self.camera_name, configured=cameras)
        try:
            made = await mosaic(linked, self.config)
        except (MediaToolError, OSError, ValueError) as exc:
            logger.warning("mosaic for %s failed: %s", event.id, exc)
            made = None
            lines.append(t("mosaic.failed", error=str(exc)[:120]))
        if made is None:
            text = say.no_mosaic_text(linked.window.moment, lines)
            await self.api.send_message(
                message.chat_id, text, thread_id=message.thread_id, reply_to=target
            )
            return text
        names = [self.camera_name(camera) for camera in made.cameras]
        places = [made.place(i) for i in range(len(made.cameras))]
        approximate = {self.camera_name(c.camera) for c in linked.clips if c.approximate}
        caption = say.mosaic_caption(
            linked.window.start, linked.window.end, list(zip(names, places, strict=True)),
            lines, approximate,
        )  # fmt: skip
        for part, video in enumerate(made.files, start=1):
            text = caption if part == 1 else t("mosaic.part", n=part, total=len(made.files))
            upload = (video.name, await asyncio.to_thread(video.read_bytes), "video/mp4")
            assert target is not None
            await self.api.send_reply_file(
                message.chat_id, "video", upload, reply_to=target,
                thread_id=message.thread_id, caption=text, size=(made.width, made.height),
            )  # fmt: skip
        return caption

    # --- questions in plain words (ADR-0017) ------------------------------------------------

    async def _question(self, message: Mapping[str, Any]) -> None:
        """A free-text message from an allowed chat: a question for the assistant. A reply
        to an alert (or to its album, or to an answer about it) makes that event the
        context. Answered off the update loop: the local model can take minutes."""
        chat = (message.get("chat") or {}).get("id")
        text = str(message.get("text") or "").strip()
        if not self.allowed(chat) or not text or self._assistant is None:
            return
        assert isinstance(chat, int)
        if self._clock() - float(message.get("date") or 0) > COMMAND_MAX_AGE_S:
            return
        event_id = None
        replied = message.get("reply_to_message")
        if isinstance(replied, Mapping) and isinstance(replied.get("message_id"), int):
            found = await self.db.telegram_message_at(chat, int(replied["message_id"]))
            event_id = None if found is None else found.event_id
        thread = message.get("message_thread_id")
        asked = message.get("message_id")
        self._background(
            self._answer_question(
                chat, text, event_id,
                thread=thread if isinstance(thread, int) else None,
                asked=asked if isinstance(asked, int) else None,
            ),
            "question",
        )  # fmt: skip

    async def _answer_question(
        self, chat: int, text: str, event_id: str | None, *, thread: int | None, asked: int | None
    ) -> None:
        assert self._assistant is not None
        async with self._question_lock:  # one at a time: the model is shared anyway
            await self.api.send_chat_action(chat, "typing", thread_id=thread)
            self._asking_chat = chat
            work = asyncio.ensure_future(self._assistant.answer(text, event_id=event_id))
            placeholder: int | None = None
            done, _pending = await asyncio.wait({work}, timeout=4.0)
            if not done:
                sent = await self.api.send_message(
                    chat, t("ask.wait"),
                    thread_id=thread, reply_to=asked, silent=True,
                )  # fmt: skip
                placeholder = sent.get("message_id") if isinstance(sent, dict) else None
            reply = await work
            answer = await self.api.send_message(
                chat, say.truncate(escape(reply.text) if not reply.fast else reply.text),
                thread_id=thread, reply_to=asked,
            )  # fmt: skip
            if isinstance(placeholder, int):
                with contextlib.suppress(TelegramError):
                    await self.api.delete_message(chat, placeholder)
            target = answer.get("message_id")
            if isinstance(target, int):
                await self.db.insert_telegram_message(
                    chat, "answer", thread_id=thread, message_id=target, event_id=event_id,
                    data={"question": text[:200], "model_calls": reply.model_calls},
                )  # fmt: skip
            for media in reply.media:
                upload = (media.path.name, await asyncio.to_thread(media.path.read_bytes),
                          "video/mp4")  # fmt: skip
                assert isinstance(target, int)
                sent_video = await self.api.send_reply_file(
                    chat, "video", upload, reply_to=target, thread_id=thread,
                    caption=media.caption or None, size=media.size,
                )  # fmt: skip
                video_id = sent_video.get("message_id")
                if isinstance(video_id, int):
                    await self.db.insert_telegram_message(
                        chat, "answer", thread_id=thread, message_id=video_id,
                        event_id=event_id, data={"media": media.path.name},
                    )  # fmt: skip

    async def _mark_from_question(self, event_id: str, answer: str, reason: str | None) -> str:
        """`mark_event` from a question: the same as the alert's buttons in that chat."""
        chat = self._asking_chat
        rows = [m for m in await self.db.telegram_alerts_for_event(event_id) if m.chat_id == chat]
        alert = next((m for m in rows if m.kind == "alert"), None)
        if alert is None:
            return t("toast.no_alert_here")
        if answer == "wrong" and reason not in reasons.BY_CODE:
            reason = None
        verdict: say.Answer = "unsure"
        if answer in ("right", "wrong"):
            verdict = "right" if answer == "right" else "wrong"
        return await self.answer(alert, verdict, reason=reason)

    async def _digest_for_question(self) -> str:
        chat = self._asking_chat or self.tg.recipient_chats[0]
        text = say.day_digest_text(await self.day(chat), now=self._clock(), evening=False)
        return re.sub(r"<[^>]+>", "", text)

    # --- answers about the verdict --------------------------------------------------------

    async def answer(
        self,
        message: TelegramMessageRow,
        answer: say.Answer,
        *,
        reason: str | None = None,
        note: str | None = None,
        keyboard: str = "main",
    ) -> str:
        """Record one answer about the verdict: the event's feedback and review, a
        reviewed label (with the reason, when given), and the alert's buttons shrink to
        one that says what was chosen. A new answer replaces the previous one, which is
        taken back first. Returns the short toast shown on the phone."""
        assert message.event_id is not None
        event = await self.db.get_event(message.event_id)
        if event is None:
            return t("event.deleted_short")
        data = dict(message.data)
        same = data.get("answer") == answer and data.get("label_prev") is not None
        if data.get("answer") and not same:
            await self._unlabel(event, data)
        verdict = alerting_verdict(event.intent, event.policy)
        policy = self._policy(event.policy or verdict.get("policy"))
        await self.db.set_event_feedback(
            event.id, "up" if answer == "right" else "down" if answer == "wrong" else None
        )
        review: dict[str, Any] = {"answer": answer, "at": self._clock()}
        if reason:
            review["reason"] = reason
        if note:
            review["note"] = note
        intent = dict(event.intent or {})
        intent["review"] = review
        await self.db.update_event(event.id, intent=intent)
        data.pop("problem", None)
        if self.tg.labels and event.clip_path:
            found = reasons.BY_CODE.get(reason or "")
            recorded = await self._label(
                event, verdict, policy, answer, reason=reason,
                truth=found.truth if found else (), note=note,
            )  # fmt: skip
            if isinstance(recorded, str):
                data["problem"] = recorded
            elif not same:
                data["label_prev"] = recorded.previous
        data.update(answer=answer, reason=reason, reason_note=note)
        data.pop("mark", None)  # an answer from before the one-button keyboard
        await self.db.update_telegram_message(message.id, data=data)
        await self._render(message.id, keyboard=keyboard)
        self.status.answers += 1
        if answer in ("right", "wrong") and keyboard == "main":
            await safe_review(self.calibration)  # 👍/👎 may change who is woken
        return t("toast.marked", what=say.answer_button(data))

    async def take_back(self, message: TelegramMessageRow) -> str:
        """Undo the answer about the verdict: the event's feedback and review are
        cleared, the clip's label is put back the way it was, the full buttons return."""
        assert message.event_id is not None
        event = await self.db.get_event(message.event_id)
        data = dict(message.data)
        if event is not None:
            await self._unlabel(event, data)
            await self.db.set_event_feedback(event.id, None)
            intent = dict(event.intent or {})
            if intent.pop("review", None) is not None:
                await self.db.update_event(event.id, intent=intent)
        for key in ("answer", "reason", "reason_note", "label_prev", "problem", "mark"):
            data.pop(key, None)
        await self.db.update_telegram_message(message.id, data=data)
        await self._render(message.id, keyboard="main")
        return t("toast.answer_undone")

    async def _unlabel(self, event: EventRow, data: dict[str, Any]) -> None:
        previous = data.pop("label_prev", None)
        if not isinstance(previous, dict) or not event.clip_path:
            return
        try:
            await asyncio.to_thread(restore, Path(event.clip_path), previous)
        except FeedbackError as exc:
            logger.info("label of %s not restored: %s", event.id, exc)

    async def _label(
        self,
        event: EventRow,
        verdict: Mapping[str, Any],
        policy: PolicyConfig | None,
        answer: say.Answer,
        *,
        reason: str | None = None,
        truth: tuple[str, ...] = (),
        note: str | None = None,
    ) -> Recorded | str:
        """The answer as a reviewed label in the clip's dataset; a string says why not."""
        assert event.clip_path is not None
        scenarios = (
            {sid: s.description for sid, s in policy.scenario_configs().items()}
            if policy is not None
            else {}
        )
        judge = (event.intent or {}).get("judge") or "judge"
        live = LiveVerdict(
            by=f"{judge} {verdict.get('model') or ''} (live)".replace("  ", " "),
            scenario=verdict.get("label"),
            alert=event.state == EventState.confirmed.value,
            description=verdict.get("rationale"),
            confidence=verdict.get("confidence"),
            at=datetime.fromtimestamp(event.started_at).astimezone().replace(microsecond=0),
        )
        try:
            return await asyncio.to_thread(
                record, Path(event.clip_path), live, answer, scenarios=scenarios,
                reason=reason, truth=truth, note=note,
            )  # fmt: skip
        except FeedbackError as exc:
            return str(exc)

    # --- decisions about one face (ADR-0016) -----------------------------------------------

    def _face_context(self, event: EventRow) -> FaceContext:
        if self._people_store is None:
            raise StoreError("known people are off here")
        sighting = SightingContext(
            at=event.started_at,
            camera=event.camera,
            source=event.source or "event",
            source_id=event.source_ref or event.id,
            clip=event.clip_path,
        )
        return FaceContext(
            self._people_store,
            self._face_engine,
            self.config.storage.media_dir,
            f"event:{event.id}",
            self._identities,
            sighting,
        )

    async def face_decision(
        self, message: TelegramMessageRow, face: int, choice: str | IdentityChoice | None
    ) -> str:
        """A stranger, an identity (existing or new), or `None` to take the decision back;
        the face's button on the alert says what was decided. Returns the toast."""
        assert message.event_id is not None
        event = await self.db.get_event(message.event_id)
        report = None if event is None else self._report(event)
        if event is None or report is None:
            return t("toast.face_gone")
        try:
            ctx = self._face_context(event)
            if choice is None:
                done = await asyncio.to_thread(decisions.undo, report, face, ctx)
            else:
                done = await asyncio.to_thread(decisions.decide, report, face, choice, ctx)
        except (StoreError, IdentityError) as exc:
            return t("toast.failed", error=exc)[:190]
        intent = dict(event.intent or {})
        intent["people"] = report.model_dump(mode="json")
        await self.db.update_event(event.id, intent=intent)
        seen = report.get(face)
        mark = say.face_mark_line(
            None if seen is None else seen.model_dump(mode="json"), names=self.tg.names
        )
        data = dict(message.data)
        faces = {str(k): v for k, v in (data.get("faces") or {}).items()}
        labels = {str(k): v for k, v in (data.get("face_labels") or {}).items()}
        if done.answer:
            faces[str(face)] = mark.removeprefix(say.mark_prefix())
        else:
            faces.pop(str(face), None)
            previous = labels.pop(str(face), None)
            if isinstance(previous, dict) and event.clip_path:
                await self._unlabel(event, {"label_prev": previous})
        data["faces"], data["face_labels"] = faces, labels
        data.pop("mark", None)
        await self.db.update_telegram_message(message.id, data=data)
        await self._render(message.id, keyboard="main")
        self.status.answers += 1
        return mark.removeprefix("✅ ")[:190] if done.answer else t("toast.decision_undone")

    async def confirm_ours(self, message: TelegramMessageRow, face: int, person_index: int) -> str:
        """ "One of us: <name>": the face becomes that enrolled person's reference and, unless
        the level wakes for anyone, the clip is labelled as a household member."""
        assert message.event_id is not None
        event = await self.db.get_event(message.event_id)
        report = None if event is None else self._report(event)
        store = self._people_store
        people = store.people() if store is not None else []
        if event is None or report is None or not 0 <= person_index < len(people):
            return t("toast.person_gone")
        person = people[person_index]
        try:
            await asyncio.to_thread(
                decisions.decide, report, face, person.id, self._face_context(event)
            )
        except (StoreError, IdentityError) as exc:
            return t("toast.failed", error=exc)[:190]
        intent = dict(event.intent or {})
        intent["people"] = report.model_dump(mode="json")
        await self.db.update_event(event.id, intent=intent)
        verdict = alerting_verdict(event.intent, event.policy)
        policy = self._policy(event.policy or verdict.get("policy"))
        data = dict(message.data)
        faces = {str(k): v for k, v in (data.get("faces") or {}).items()}
        labels = {str(k): v for k, v in (data.get("face_labels") or {}).items()}
        faces[str(face)] = (
            t("face.ours_named", name=person.name) if self.tg.names else t("face.ours")
        )
        wakes_anyone = policy is not None and policy.wake_for(verdict.get("label")) is Wake.always
        if self.tg.labels and event.clip_path and not wakes_anyone and str(face) not in labels:
            recorded = await self._label(event, verdict, policy, "ours")
            if isinstance(recorded, str):
                data["problem"] = recorded
            else:
                labels[str(face)] = recorded.previous
        data["faces"], data["face_labels"] = faces, labels
        data.pop("mark", None)
        await self.db.update_telegram_message(message.id, data=data)
        await self._render(message.id, keyboard="main")
        self.status.answers += 1
        return t("toast.marked", what=faces[str(face)])

    async def _ask(self, message: TelegramMessageRow, what: str, *, face: int = 0) -> None:
        """A forced reply: a name for a face (`familiar`, `unknown`) or, after 👎 and
        "Other", what it was (`reason`). Read only as a reply to this question, in an
        allowed chat; in memory, so after a restart the button is tapped again."""
        if what == "reason":
            text = t("prompt.what")
            hint = t("prompt.what_hint")
        else:
            who = t("prompt.who_familiar") if what == "familiar" else t("prompt.who_identity")
            text = t("prompt.name", who=who, face=face)
            hint = t("prompt.hint_familiar") if what == "familiar" else t("prompt.hint_name")
        sent = await self.api.send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": hint},
        )
        prompt_id = sent.get("message_id")
        if isinstance(prompt_id, int):
            self._prompts[(message.chat_id, prompt_id)] = (message.id, face, what)

    async def _prompt_reply(self, message: Mapping[str, Any]) -> bool:
        """A reply to one of the bot's questions: a name for a face, or what a false
        alarm was."""
        chat = (message.get("chat") or {}).get("id")
        replied = message.get("reply_to_message")
        text = message.get("text")
        if not self.allowed(chat) or not isinstance(replied, Mapping) or not isinstance(text, str):
            return False
        assert isinstance(chat, int)
        key = (chat, replied.get("message_id"))
        if not isinstance(key[1], int) or key not in self._prompts:
            return False
        row_id, face, what = self._prompts.pop((chat, key[1]))
        row = await self.db.get_telegram_message(row_id)
        if row is None or row.event_id is None:
            return True
        thread = message.get("message_thread_id")
        if what == "reason":
            note = " ".join(text.split())[:200]
            toast = await self.answer(row, "wrong", reason="other", note=note or None)
        else:
            name = " ".join(text.split())[:40]
            choice = IdentityChoice(name=None if name in ("", "-") else name, status=what)
            toast = await self.face_decision(row, face, choice)
        await self.api.send_message(
            chat,
            t("done", what=escape(toast)),
            thread_id=thread if isinstance(thread, int) else None,
        )
        return True

    # --- what waits for Telegram to answer again ------------------------------------------

    async def _resend(self) -> None:
        """The queue's worker: idle until something is queued (or finds what a restart
        left), then tries after a growing pause, at once when the poller hears Telegram
        again, until nothing waits."""
        first, longest = RESEND_RETRY_S
        delay = first
        while True:
            self._queued.clear()  # before looking: a send queued meanwhile wakes us again
            try:
                waiting = bool(self._dropped or await self.db.pending_telegram_messages())
            except Exception as exc:
                await self._failed("resend", exc)
                waiting = False
            if not waiting:
                delay = first
                await self._queued.wait()
                continue
            await self._pause(delay)
            try:
                done = await self.resend_pending()
            except Exception as exc:
                await self._failed("resend", exc)
                done = False
            delay = first if done else min(delay * 2, longest)

    async def resend_pending(self) -> bool:
        """Everything that waits for Telegram, oldest first: alerts go out marked as late
        (the time they were due in the text), under the usual rules (one camera's alerts
        within `collapse` of each other, by the time they were due, become one message);
        edits of alerts are redone. An alert older than `late_alerts_for`, or whose event
        is no longer an alert, is not sent: the chat gets one line saying how many were
        not, the digest lists them. Stops at the first failure that says Telegram is still
        out of reach. True when nothing waits any more."""
        window = self.tg.seconds("late_alerts_for")
        async with self._send_lock:
            for message in await self.db.pending_telegram_messages():
                waiting = message.data.get("pending")
                what = str(waiting.get("what")) if isinstance(waiting, Mapping) else "send"
                at = waiting.get("at") if isinstance(waiting, Mapping) else None
                # claimed before it goes: a crash during the send never sends it twice
                data = {k: v for k, v in message.data.items() if k != "pending"}
                await self.db.update_telegram_message(message.id, data=data)
                event = (
                    None if message.event_id is None else await self.db.get_event(message.event_id)
                )
                now = self._clock()
                kept = self._kept
                if what == "send":
                    alert = event is not None and event.state == EventState.confirmed.value
                    if event is None or not alert or now - message.created_at > window:
                        await self.db.delete_telegram_message(message.id)
                        if alert:
                            logger.warning(
                                "telegram: the alert for %s waited longer than "
                                "late_alerts_for; not sent",
                                message.event_id,
                            )
                            self._dropped.setdefault(message.chat_id, []).append(message.created_at)
                        continue
                    try:
                        await self._send_late(message, event, data)
                    except Exception as exc:  # a collapse whose edit failed for good
                        await self._failed("late alert", exc)
                else:
                    since = float(at) if isinstance(at, int | float) else message.created_at
                    fresh = await self.db.get_telegram_message(message.id)
                    if event is None or fresh is None or now - since > window:
                        continue  # the message stays as it was
                    if what == "refresh":
                        await self._refresh_or_keep(fresh, event)
                    else:
                        try:
                            await self._edit(fresh.id)
                        except Exception as exc:
                            await self._failed("edit", exc)
                if self._kept != kept:
                    break  # still out of reach: the rest waits for the next try
            for chat, times in list(self._dropped.items()):
                if await self._say_dropped(chat, times):
                    del self._dropped[chat]
        return not self._dropped and not await self.db.pending_telegram_messages()

    async def _send_late(
        self, message: TelegramMessageRow, event: EventRow, data: dict[str, Any]
    ) -> None:
        verdict = alerting_verdict(event.intent, event.policy)
        policy = self._policy(event.policy or verdict.get("policy"))
        rank = level_rank(policy, verdict.get("level"))
        last = await self.db.last_telegram_alert(
            message.chat_id, event.camera, since=message.created_at - self.tg.seconds("collapse"),
            until=message.created_at, tests=False,
        )  # fmt: skip
        if last is not None and last.event_id != event.id and last.data.get("rank", 99) <= rank:
            await self.db.delete_telegram_message(message.id)
            await self._collapse(message.chat_id, last, event, verdict)
            return
        data["rank"] = rank
        data["late"] = message.created_at
        await self._deliver(
            message.id, message.chat_id, message.thread_id, event, verdict, policy, data
        )

    async def _say_dropped(self, chat: int, times: list[float]) -> bool:
        """One line about alerts that waited too long; False while Telegram cannot take it
        (it is tried again with the queue, until a restart)."""
        try:
            await self.api.send_message(
                chat,
                say.dropped_text(times, now=self._clock()),
                thread_id=self._thread(chat, "alerts"),
            )
        except TelegramError as exc:
            await self._failed("late alerts note", exc)
            return not exc.transient
        return True

    # --- calibration (ADR-0020) -------------------------------------------------------------

    def _calibration_what(self, policy: str, scenario: str, camera: str) -> str:
        return t(
            "cal.what",
            scenario=self._scenario_title(self._policy(policy), scenario),
            camera=self.camera_name(camera),
        )

    async def announce_calibration(self, changes: list[Change]) -> None:
        """One short line per change to every recipient, silently, in the alerts topic,
        with a button that takes it back."""
        for change in changes:
            row = change.row
            what = self._calibration_what(row.policy, row.scenario, row.camera)
            text = say.calibration_text(change.kind, what, row, change.evidence)
            markup = None
            if change.kind == "step":
                markup = {"inline_keyboard": [[
                    {"text": t("kb.cal_undo"), "callback_data": f"c:{row.id}:undo"}
                ]]}  # fmt: skip
            for chat in self.tg.recipient_chats:
                try:
                    await self.api.send_message(
                        chat, text, thread_id=self._thread(chat, "alerts"),
                        reply_markup=markup, silent=True, once=True,
                    )  # fmt: skip
                except TelegramError as exc:
                    await self._failed("calibration", exc)

    async def undo_calibration(
        self, step_id: int, chat: int, message: Mapping[str, Any]
    ) -> str | None:
        """The button under an announcement or in /tuning: take that step back."""
        if self.calibration is None:
            return t("toast.cal_gone")
        taken = await self.calibration.revert(step_id, "telegram")
        if not taken:
            return t("toast.cal_gone")
        row = taken[0]
        what = self._calibration_what(row.policy, row.scenario, row.camera)
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            states = await self.calibration.states()
            state = states.get(row.key)
            with contextlib.suppress(TelegramError):
                await self.api.edit_message_text(
                    chat, message_id,
                    say.calibration_undone_text(what, state.step if state else 0,
                                                state.bar if state else None),
                )  # fmt: skip
        return t("toast.cal_undone")

    async def send_tuning(self, chat: int, *, thread: int | None) -> None:
        """/tuning: what the marks changed and is in force, each with a button back."""
        steps = await self.calibration.db.calibration_steps() if self.calibration else []
        active = [row for row in steps if row.active]
        latest: dict[tuple[str, str, str], Any] = {}
        for row in active:
            latest[row.key] = row  # the newest step of each key is the one in force
        rows = sorted(latest.values(), key=lambda r: r.id)
        if self.calibration is None or not self.calibration.enabled:
            await self.api.send_message(chat, t("cal.off"), thread_id=thread)
            return
        if not rows:
            text = t("cal.list_empty", marks=self.calibration.rules.marks)
            await self.api.send_message(chat, text, thread_id=thread)
            return
        items = [
            say.TuningItem(
                what=self._calibration_what(row.policy, row.scenario, row.camera),
                step=row.step_to, bar=row.bar, since=row.at, evidence=row.evidence,
            )
            for row in rows
        ]  # fmt: skip
        buttons = [
            {"text": t("kb.cal_undo_n", n=n), "callback_data": f"c:{row.id}:undo"}
            for n, row in enumerate(rows, start=1)
        ]
        await self.api.send_message(
            chat, say.tuning_text(items, now=self._clock()), thread_id=thread,
            reply_markup={"inline_keyboard": [buttons[i : i + 4]
                                              for i in range(0, len(buttons), 4)]},
        )  # fmt: skip

    # --- digest and status ---------------------------------------------------------------

    async def _digests(self) -> None:
        """The day's digest at `digest_at`, once per chat and day. The day is written down
        before the message goes, so a restart never sends it twice; a service that was
        down at that time sends it when it is back. One Telegram did not take (no
        connection, 429, 5xx) is tried again after a growing pause, at once when the poller
        hears Telegram again. Late or retried, only until `digest_until`."""
        minutes = self.tg.digest_minutes
        if minutes is None:
            return
        first, longest = DIGEST_RETRY_S
        delay = first
        while True:
            now = self._clock()
            midnight = _midnight(now)
            due = midnight + minutes * 60
            until = midnight + self.tg.digest_until_minutes * 60
            if now < due:
                await self._sleep(max(1.0, due - now))
                continue
            owed = False
            if now < until:
                for chat in self.tg.recipient_chats:
                    try:
                        await self.evening_digest(chat)
                    except Exception as exc:
                        await self._failed("digest", exc)
                    owed = owed or await self._digest_owed(chat, now)
            else:
                await self._digest_too_late(now)
            if owed:
                await self._pause(min(delay, max(1.0, until - now)))
                delay = min(delay * 2, longest)
                continue
            delay = first
            await self._sleep(max(60.0, midnight + 86400 + 60 - self._clock()))  # next day

    async def _digest_owed(self, chat: int, now: float) -> bool:
        """Today's digest for `chat` has not reached Telegram and may still go."""
        today = datetime.fromtimestamp(now).date().isoformat()
        return await self.db.get_meta(DIGEST_DAY_KEY.format(chat=chat)) == today + DIGEST_UNSENT

    async def _digest_too_late(self, now: float) -> None:
        """Past `digest_until`: a digest still owed is not sent any more; said once."""
        today = datetime.fromtimestamp(now).date().isoformat()
        for chat in self.tg.recipient_chats:
            if await self._digest_owed(chat, now):
                await self.db.set_meta(DIGEST_DAY_KEY.format(chat=chat), today)
                logger.warning(
                    "telegram: today's digest did not reach Telegram by %s; not sent",
                    self.tg.digest_until or "midnight",
                )

    async def evening_digest(self, chat: int) -> bool:
        """Today's digest for `chat` once: nothing when it was already sent today or has
        nothing that needs a look (only ordinary events, every alert answered). When
        Telegram did not take it, the day is marked `<day>:unsent` so that it may go again;
        a digest that may have arrived (the answer was lost after it went out) stays
        marked as sent: never twice."""
        now = self._clock()
        today = datetime.fromtimestamp(now).date().isoformat()
        key = DIGEST_DAY_KEY.format(chat=chat)
        if await self.db.get_meta(key) == today:
            return False
        await self.db.set_meta(key, today)  # before sending: never twice
        midnight = _midnight(now)
        minutes = self.tg.digest_minutes or 0
        # deep analysis may find something in last night's clips after yesterday's digest
        day = await self.day(chat, late_since=midnight - 86400 + minutes * 60)
        if not day.substance:
            return False
        text = say.day_digest_text(day, now=now, evening=True)
        try:
            await self.api.send_message(
                chat, text, thread_id=self._thread(chat, "digest"), silent=True, once=True
            )
        except TelegramError as exc:
            if exc.transient:  # Telegram never got it: it may go again (_digests)
                await self.db.set_meta(key, today + DIGEST_UNSENT)
            raise
        self.status.digests_sent += 1
        return True

    async def send_digest(self, chat: int, *, thread: int | None) -> bool:
        """Today so far, on request: always answered, also when nothing happened."""
        now = self._clock()
        day = await self.day(chat)
        await self.api.send_message(
            chat, say.day_digest_text(day, now=now, evening=False), thread_id=thread
        )
        self.status.digests_sent += 1
        return True

    def _deep_finding(self, row: EventRow) -> say.DigestItem | None:
        """What deep analysis (ADR-0019) flagged that the live judge did not: an alert
        level on an event that did not alert, as a digest item in its own words."""
        deep = row.deep or {}
        if deep.get("agreement") != "disputed" or row.state == EventState.confirmed.value:
            return None
        verdict = next((v for v in deep.get("verdicts") or [] if v.get("alert")), None)
        if verdict is None:
            return None
        policy = self._policy(verdict.get("policy"))
        title = self._level_title(policy, verdict.get("level")) + t("digest.second_look")
        description = deep.get("description") or verdict.get("rationale")
        return say.DigestItem(
            self.camera_name(row.camera), row.started_at, title,
            str(description) if description else None,
        )  # fmt: skip

    async def day(
        self, chat: int, *, since: float | None = None, late_since: float | None = None
    ) -> say.DayDigest:
        """Today (from local midnight, or `since`) for one chat, from the database. With
        `late_since`, what deep analysis flagged between then and `since` comes first."""
        now = self._clock()
        if since is None:
            since = (
                datetime.fromtimestamp(now)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp()
            )
        rows = await self.db.list_events(since_ts=since, limit=MAX_DIGEST_EVENTS)
        alerted = await self.db.telegram_alerted_events(chat, since=since - 3600)
        day = say.DayDigest(since=since, events=len(rows))
        if late_since is not None and late_since < since:
            late = await self.db.list_events(since_ts=late_since, limit=MAX_DIGEST_EVENTS)
            for row in sorted(late, key=lambda r: r.started_at):
                flagged = self._deep_finding(row) if row.started_at <= since else None
                if flagged is not None and row.id not in alerted:
                    day.notable.append(flagged)
                    day.deep_found += 1
        for row in sorted(rows, key=lambda r: r.started_at):
            deep = row.deep or {}
            if deep:
                day.deep_checked += 1
                if deep.get("agreement") in ("disputed", "uncertain"):
                    day.deep_review += 1
            verdict = alerting_verdict(row.intent, row.policy)
            policy = self._policy(row.policy or verdict.get("policy"))
            review = (row.intent or {}).get("review")
            review = review if isinstance(review, Mapping) else {}
            if row.id in alerted:
                day.alerts += 1
                if row.feedback == "up":
                    day.right += 1
                elif row.feedback == "down":
                    day.wrong += 1
                    found = reasons.BY_CODE.get(str(review.get("reason") or ""))
                    if found is not None:
                        day.reasons[found.short] += 1
                elif not review:
                    day.unanswered += 1
                    day.last_unanswered = (row.started_at, self.camera_name(row.camera))
                continue
            if row.state == EventState.confirmed.value:
                people = (row.intent or {}).get("people")
                counted = verdict.get("persons")
                persons = counted if isinstance(counted, int) else None
                title = self._level_title(policy, verdict.get("level"))
                if known_only(people if isinstance(people, Mapping) else None, persons):
                    title += ", " + say.who_line(people, persons, names=self.tg.names)
                if say.calibrated(row.intent) is not None:
                    title += t("digest.calibrated")
                # the deep look's words when there are some: more frames, plainer prose
                description = deep.get("description") or verdict.get("rationale")
                day.notable.append(
                    say.DigestItem(
                        self.camera_name(row.camera), row.started_at, title,
                        str(description) if description else None,
                    )
                )  # fmt: skip
                continue
            flagged = self._deep_finding(row)
            if flagged is not None:
                day.notable.append(flagged)
                day.deep_found += 1
            elif row.state == EventState.dismissed.value:
                day.ordinary[self._scenario_title(policy, verdict.get("label"))] += 1
            elif deep.get("verdicts") and not deep["verdicts"][0].get("abstained"):
                first = deep["verdicts"][0]  # judged by the deep look only
                day.ordinary[self._scenario_title(self._policy(first.get("policy")),
                                                  first.get("label"))] += 1  # fmt: skip
            else:
                day.unjudged += 1
                day.unjudged_why[unjudged_reason(row.summary)] += 1
        return day

    async def status_text(self) -> str:
        now = self._clock()
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = await self.db.list_events(since_ts=midnight.timestamp(), limit=MAX_DIGEST_EVENTS)
        per: dict[str, list[EventRow]] = {}
        for row in rows:
            per.setdefault(row.camera, []).append(row)
        cameras = [f.camera for f in self.config.clip_folders]
        cameras += sorted(c for c in per if c not in cameras)
        today = [
            say.CameraToday(
                name=self.camera_name(camera),
                events=len(per.get(camera, [])),
                alerts=sum(1 for r in per.get(camera, []) if r.state == "confirmed"),
                last_at=max((r.started_at for r in per.get(camera, [])), default=None),
            )
            for camera in cameras
        ]
        last = await self.db.last_telegram_alert(None, tests=False)
        last_text = None
        latency = None
        if last is not None and last.event_id is not None:
            event = await self.db.get_event(last.event_id)
            if event is not None:
                timeline = (event.intent or {}).get("timeline")
                sent_at = last.data.get("sent_at")
                latency = say.latency_text(
                    timeline if isinstance(timeline, Mapping) else {},
                    sent_at=float(sent_at) if isinstance(sent_at, int | float) else None,
                )
                verdict = alerting_verdict(event.intent, event.policy)
                policy = self._policy(event.policy or verdict.get("policy"))
                level = self._level_title(policy, verdict.get("level"))
                phrase = verdict.get("rationale") or ""
                last_text = (
                    f"{say.clock(event.started_at, now=now)}, {self.camera_name(event.camera)}, "
                    f"{level}: {phrase}"
                ).rstrip(": ")
        quiet = None
        for chat in self.tg.recipient_chats[:1]:
            quiet = await self.quiet_until(chat)
        judged = sum(1 for r in rows if r.state in ("confirmed", "dismissed"))
        return say.status_text(
            now=now,
            judge=await self._judge_status() if self._judge_status else "?",
            judged=judged,
            unjudged=len(rows) - judged,
            cameras=today,
            last_alert=last_text,
            quiet_until=quiet,
            latency=latency,
        )
