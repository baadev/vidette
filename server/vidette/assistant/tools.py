"""What the assistant can look up and do, one function per tool (ADR-0017).

The same tools serve the Telegram conversation and the MCP endpoint. Each returns a short
JSON-able result for the model (times as local HH:MM:SS, camera ids, scenario titles,
identity numbers) and, for `send_clips`, the videos to send. Results are small on
purpose: a shared local model has about 2000 tokens a conversation.

Privacy: names (of enrolled people and of identities someone named) appear in results
only with `names=True`, which the caller sets for a local model with the household's
`notifications.telegram.names` on. A cloud model or an MCP client gets "one of us" and
"#12". Values are plain English for the model; errors a person may read are in the bot's
language (vidette.telegram.i18n). No tool returns a face crop, an embedding or a frame;
videos go to the chat, never to the model.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vidette.core.config import OTHER_SCENARIO, PolicyConfig, VidetteConfig
from vidette.db import Database, EventRow
from vidette.ffmpeg import MediaToolError
from vidette.linked import Linked, describe, find_around, find_linked, moment_of, mosaic
from vidette.people.identities import IdentityStore
from vidette.telegram import text as say
from vidette.telegram.i18n import t
from vidette.telegram.routing import alerting_verdict

MAX_EVENTS = 12
MAX_SIGHTINGS = 12
CURRENT = "current"  # an event id meaning "the event this conversation is about"

MarkCallback = Callable[[str, str, str | None], Awaitable[str]]
DigestCallback = Callable[[], Awaitable[str]]


@dataclass
class Media:
    path: Path
    caption: str
    size: tuple[int, int] | None = None


@dataclass
class Result:
    data: dict[str, Any]  # what the model reads
    media: list[Media] = field(default_factory=list)  # what the chat receives


class ToolError(Exception):
    """A tool could not do what was asked; the message is for the model and the person."""


# The tool list as the model sees it (OpenAI function calling). Short descriptions: every
# token here is spent on every call.
SPECS: list[dict[str, Any]] = [
    {
        "name": "find_events",
        "description": "Events in a period: time, camera, verdict, alert, faces.",
        "parameters": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "HH:MM, YYYY-MM-DD HH:MM or -3h"},
                "until": {"type": "string"},
                "camera": {"type": "string"},
                "scenario": {"type": "string"},
                "alerts_only": {"type": "boolean"},
                "identity": {"type": "integer", "description": "face number"},
            },
        },
    },
    {
        "name": "event_detail",
        "description": "One event: verdict, description, faces, owner's answer.",
        "parameters": {
            "type": "object",
            "properties": {"event_id": {"type": "string", "description": "id or current"}},
            "required": ["event_id"],
        },
    },
    {
        "name": "clips_at",
        "description": "What every camera recorded around a moment.",
        "parameters": {
            "type": "object",
            "properties": {
                "time": {"type": "string", "description": "HH:MM[:SS] or current"},
                "cameras": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["time"],
        },
    },
    {
        "name": "identity_sightings",
        "description": "Where and when a face number was seen.",
        "parameters": {
            "type": "object",
            "properties": {
                "identity": {"type": "integer"},
                "days": {"type": "integer"},
            },
            "required": ["identity"],
        },
    },
    {
        "name": "send_clips",
        "description": "Send one video of all cameras at an event or a time.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "id or current"},
                "time": {"type": "string", "description": "HH:MM[:SS], instead of an event"},
            },
        },
    },
    {
        "name": "mark_event",
        "description": "Mark an event like the alert buttons do; test: right, staged by us.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "answer": {"type": "string", "enum": ["right", "wrong", "unsure", "test"]},
                "reason": {"type": "string", "description": "for wrong: passing, car_use, …"},
            },
            "required": ["event_id", "answer"],
        },
    },
    {
        "name": "today",
        "description": "Today's digest.",
        "parameters": {"type": "object", "properties": {}},
    },
]
READ_ONLY = ("find_events", "event_detail", "clips_at", "identity_sightings", "today")


def parse_when(text: str, *, now: float) -> float:
    """`12:01`, `12:01:13`, `2026-11-15 12:01`, `-2h`, `-30m` → epoch seconds (local)."""
    raw = text.strip().lower()
    today = datetime.fromtimestamp(now)
    rel = re.fullmatch(r"-(\d+)\s*([mhd])", raw)
    if rel:
        unit = {"m": 60, "h": 3600, "d": 86400}[rel.group(2)]
        return now - int(rel.group(1)) * unit
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).timestamp()
        except ValueError:
            continue
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?", raw)
    if match:
        hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
        at = today.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if at.timestamp() > now + 600:  # "23:10" asked after midnight: yesterday
            at -= timedelta(days=1)
        return at.timestamp()
    raise ToolError(t("tool.bad_time", text=text))


def hms(ts: float, *, now: float | None = None) -> str:
    at = datetime.fromtimestamp(ts)
    if now is not None and datetime.fromtimestamp(now).date() != at.date():
        return at.strftime("%d.%m %H:%M:%S")
    return at.strftime("%H:%M:%S")


class Tools:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        *,
        identities: IdentityStore | None = None,
        camera_name: Callable[[str], str] | None = None,
        names: bool = False,
        mark: MarkCallback | None = None,
        digest: DigestCallback | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.db = db
        self.identities = identities
        self.camera_name = camera_name or (lambda camera: camera)
        self.names = names
        self._mark = mark
        self._digest = digest
        self._clock = clock
        self.current: str | None = None  # the event the conversation is about

    def specs(self, *, read_only: bool = False) -> list[dict[str, Any]]:
        wanted = [s for s in SPECS if not read_only or s["name"] in READ_ONLY]
        if self._mark is None:
            wanted = [s for s in wanted if s["name"] != "mark_event"]
        return wanted

    async def call(self, name: str, args: Mapping[str, Any]) -> Result:
        handlers: dict[str, Callable[[Mapping[str, Any]], Awaitable[Result]]] = {
            "find_events": self.find_events,
            "event_detail": self.event_detail,
            "clips_at": self.clips_at,
            "identity_sightings": self.identity_sightings,
            "send_clips": self.send_clips,
            "mark_event": self.mark_event,
            "today": self.today,
            "incident_clips": self.incident_clips,  # not offered to the model
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(t("tool.unknown", name=name))
        return await handler(args)

    # --- helpers -----------------------------------------------------------------------

    def _policy(self, name: str | None) -> PolicyConfig | None:
        return next((p for p in self.config.policies if p.name == name), None)

    def _title(self, row: EventRow) -> str:
        verdict = alerting_verdict(row.intent, row.policy)
        label = verdict.get("label")
        if not label:
            return "no verdict"
        if label == OTHER_SCENARIO:
            return "none of the rules"
        policy = self._policy(row.policy or verdict.get("policy"))
        found = policy.scenario_configs().get(str(label)) if policy is not None else None
        return found.title if found is not None and found.title else str(label)

    def _faces(self, row: EventRow) -> list[str]:
        people = (row.intent or {}).get("people")
        seen = people.get("seen") if isinstance(people, Mapping) else None
        out: list[str] = []
        for face in seen if isinstance(seen, list) else []:
            if not isinstance(face, Mapping):
                continue
            brief = face.get("identity")
            if face.get("status") == "known":
                name = face.get("name") if self.names else None
                out.append(f"one of us: {name}" if name else "one of us")
            elif isinstance(brief, Mapping) and isinstance(brief.get("id"), int):
                status = brief.get("status")
                word = {"familiar": "familiar", "suspicious": "suspicious"}.get(
                    str(status), "stranger"
                )
                name = brief.get("name") if self.names else None
                out.append(f"{word} #{brief['id']}" + (f" ({name})" if name else ""))
            else:
                out.append("small face")
        return out

    async def _event(self, event_id: object) -> EventRow:
        wanted = self.current if event_id in (None, "", CURRENT) else str(event_id)
        if not wanted:
            raise ToolError(t("tool.no_event"))
        row = await self.db.get_event(wanted)
        if row is None:
            raise ToolError(t("tool.event_missing", id=wanted))
        return row

    def _brief(self, row: EventRow) -> dict[str, Any]:
        verdict = alerting_verdict(row.intent, row.policy)
        review = (row.intent or {}).get("review")
        brief: dict[str, Any] = {
            "id": row.id,
            "t": hms(row.started_at, now=self._clock()),
            "cam": self.camera_name(row.camera),
            "what": self._title(row),
            "alert": row.state == "confirmed",
        }
        if isinstance(verdict.get("persons"), int):
            brief["persons"] = verdict["persons"]
        faces = self._faces(row)
        if faces:
            brief["faces"] = faces
        if row.feedback:
            brief["owner"] = "right" if row.feedback == "up" else "false alarm"
            if isinstance(review, Mapping) and review.get("reason"):
                brief["owner"] += f" ({review['reason']})"
        return brief

    # --- the tools ---------------------------------------------------------------------

    async def find_events(self, args: Mapping[str, Any]) -> Result:
        now = self._clock()
        since = parse_when(str(args["since"]), now=now) if args.get("since") else now - 86400
        until = parse_when(str(args["until"]), now=now) if args.get("until") else now
        camera = args.get("camera")
        cameras = {f.camera for f in self.config.clip_folders}
        if isinstance(camera, str) and camera not in cameras:
            by_name = {self.camera_name(c).lower(): c for c in cameras}
            camera = by_name.get(camera.lower(), camera)
        rows = await self.db.list_events(
            camera=camera if isinstance(camera, str) else None, since_ts=since, limit=2000
        )
        rows = [r for r in rows if r.started_at <= until]
        if args.get("alerts_only"):
            rows = [r for r in rows if r.state == "confirmed"]
        scenario = args.get("scenario")
        if isinstance(scenario, str) and scenario:
            rows = [
                r for r in rows
                if alerting_verdict(r.intent, r.policy).get("label") == scenario
                or scenario.lower() in self._title(r).lower()
            ]  # fmt: skip
        identity = args.get("identity")
        if isinstance(identity, int):
            mark = re.compile(rf"#{identity}\b")
            rows = [r for r in rows if any(mark.search(f) for f in self._faces(r))]
        total = len(rows)
        listed = [self._brief(r) for r in rows[:MAX_EVENTS]]
        return Result({"total": total, "shown": len(listed), "events": listed})

    async def event_detail(self, args: Mapping[str, Any]) -> Result:
        row = await self._event(args.get("event_id"))
        verdict = alerting_verdict(row.intent, row.policy)
        detail = self._brief(row)
        detail["description"] = verdict.get("rationale") or row.summary
        detail["level"] = verdict.get("level")
        stream = (row.intent or {}).get("stream")
        if isinstance(stream, Mapping) and stream.get("interrupted"):
            detail["recording"] = "interrupted"
        detail["clip"] = bool(row.clip_path and Path(row.clip_path).is_file())
        links = await self._incident_links(row)
        if links is not None:
            detail["incident"] = links
        return Result(detail)

    async def _incident_links(self, row: EventRow) -> list[dict[str, Any]] | None:
        """The other cameras' events tied to this one (ADR-0022), each with the plain line
        the alert said; None when the event has no incident record."""
        incident = (row.intent or {}).get("incident")
        if not isinstance(incident, Mapping):
            return None
        if incident.get("role") == "linked":  # this one was linked to an alert: that alert
            alert = await self.db.get_event(str(incident.get("alert")))
            if alert is None:
                return []
            record = {
                "event_id": alert.id, "camera": alert.camera,
                "at": moment_of(alert), "evidence": incident.get("evidence") or "time",
                "label": alerting_verdict(alert.intent, alert.policy).get("label"),
                "rationale": alerting_verdict(alert.intent, alert.policy).get("rationale"),
                "state": alert.state,
            }  # fmt: skip
            incident = {"role": "alert", "links": [record]}
        policy_of = self._policy
        lines = say.incident_lines(
            incident, camera_name=self.camera_name,
            titles=lambda label: self._label_title(policy_of(row.policy), label),
            now=self._clock(), limit=6,
        )  # fmt: skip
        links = [link for link in incident.get("links") or [] if isinstance(link, Mapping)]
        return [
            {
                "line": line, "event_id": link.get("event_id"),
                "cam": self.camera_name(str(link.get("camera"))),
                "same_person": link.get("evidence") in ("face", "look"),
            }
            for line, link in zip(lines, links, strict=False)
        ]  # fmt: skip

    def _label_title(self, policy: PolicyConfig | None, label: str | None) -> str:
        if not label:
            return ""
        found = policy.scenario_configs().get(label) if policy is not None else None
        return found.title if found is not None and found.title else label.replace("_", " ")

    async def incident_clips(self, args: Mapping[str, Any]) -> Result:
        """The clips of the other cameras' events tied to this one by a face or the
        clothes (at most two), for the chat; nothing for the model."""
        row = await self._event(args.get("event_id"))
        links = await self._incident_links(row) or []
        media: list[Media] = []
        for link in links:
            if not link.get("same_person") or len(media) >= 2:
                continue
            other = await self.db.get_event(str(link.get("event_id")))
            if other is None or not other.clip_path or not Path(other.clip_path).is_file():
                continue
            media.append(Media(Path(other.clip_path), str(link["line"])))
        return Result({"clips": len(media)}, media)

    def _linked_data(self, linked: Linked) -> dict[str, Any]:
        now = self._clock()
        clips = []
        for clip in linked.clips:
            item: dict[str, Any] = {
                "cam": self.camera_name(clip.camera),
                "from": hms(clip.started_at, now=now),
                "to": hms(clip.ended_at, now=now),
            }
            if clip.own:
                item["this_event"] = True
            if clip.same_person:
                item["same_person_by_face"] = True
            elif clip.persons:
                item["person"] = "yes, no face to compare"
            if clip.approximate:
                item["time"] = "approximate"
            if clip.event_id:
                item["event_id"] = clip.event_id
            clips.append(item)
        configured = [f.camera for f in self.config.clip_folders]
        return {
            "window": f"{hms(linked.window.start, now=now)}–{hms(linked.window.end, now=now)}",
            "clips": clips,
            "nothing_from": [self.camera_name(c) for c in linked.silent_cameras(configured)],
        }

    async def _linked(self, args: Mapping[str, Any]) -> Linked:
        when = args.get("time")
        cameras = args.get("cameras")
        wanted = [str(c) for c in cameras] if isinstance(cameras, list) and cameras else None
        if when in (None, "", CURRENT) or args.get("event_id"):
            row = await self._event(args.get("event_id"))
            return await find_linked(row, self.config, self.db, cameras=wanted)
        moment = parse_when(str(when), now=self._clock())
        return await find_around(moment, self.config, self.db, cameras=wanted)

    async def clips_at(self, args: Mapping[str, Any]) -> Result:
        return Result(self._linked_data(await self._linked(args)))

    async def identity_sightings(self, args: Mapping[str, Any]) -> Result:
        if self.identities is None:
            raise ToolError(t("tool.identities_off"))
        number = args.get("identity")
        if not isinstance(number, int):
            raise ToolError(t("tool.identity_number"))
        current_id = await asyncio.to_thread(self.identities.resolve, number)  # after merges
        row = (
            None if current_id is None else await asyncio.to_thread(self.identities.get, current_id)
        )
        if row is None:
            raise ToolError(t("tool.identity_missing", n=number))
        number = row.id
        days = args.get("days") if isinstance(args.get("days"), int) else 7
        since = self._clock() - float(days or 7) * 86400
        sightings = [
            s for s in await asyncio.to_thread(self.identities.sightings, number) if s.at >= since
        ]
        now = self._clock()
        current = await self.db.get_event(self.current) if self.current else None
        here = current.source_ref if current is not None else None
        shown = []
        for s in sightings[:MAX_SIGHTINGS]:
            item: dict[str, Any] = {
                "t": hms(s.at, now=now), "cam": self.camera_name(s.camera), "face_px": s.face_px,
            }  # fmt: skip
            if here is not None and s.source_id == here:
                item["this_event"] = True
            shown.append(item)
        data: dict[str, Any] = {
            "identity": number,
            "status": row.status,
            "sightings_total": row.sightings,
            "days_seen": row.days,
            "shown": shown,
        }
        if self.names and row.name:
            data["name"] = row.name
        return Result(data)

    async def send_clips(self, args: Mapping[str, Any]) -> Result:
        linked = await self._linked(args)
        data = self._linked_data(linked)
        try:
            made = await mosaic(linked, self.config)
        except (MediaToolError, OSError, ValueError) as exc:
            data["video"] = f"not stitched: {exc}"[:200]
            return Result(data)
        if made is None:
            data["video"] = "nothing to stitch: the other cameras recorded nothing then"
            return Result(data)
        names = [self.camera_name(c) for c in made.cameras]
        data["video"] = f"sent: {', '.join(names)}"
        places = [made.place(i) for i in range(len(made.cameras))]
        approximate = {self.camera_name(c.camera) for c in linked.clips if c.approximate}
        configured = [f.camera for f in self.config.clip_folders]
        caption = say.mosaic_caption(
            linked.window.start, linked.window.end, list(zip(names, places, strict=True)),
            describe(linked, camera_name=self.camera_name, configured=configured), approximate,
        )  # fmt: skip
        media = [
            Media(path, caption if n == 1 else t("mosaic.part", n=n, total=len(made.files)),
                  (made.width, made.height))
            for n, path in enumerate(made.files, start=1)
        ]  # fmt: skip
        return Result(data, media)

    async def mark_event(self, args: Mapping[str, Any]) -> Result:
        if self._mark is None:
            raise ToolError(t("tool.mark_chat_only"))
        row = await self._event(args.get("event_id"))
        answer = str(args.get("answer") or "")
        if answer not in ("right", "wrong", "unsure", "test"):
            raise ToolError(t("tool.answer_values"))
        reason = args.get("reason")
        said = await self._mark(row.id, answer, str(reason) if reason else None)
        return Result({"done": said})

    async def today(self, _args: Mapping[str, Any]) -> Result:
        if self._digest is None:
            raise ToolError(t("tool.digest_chat_only"))
        return Result({"digest": await self._digest()})
