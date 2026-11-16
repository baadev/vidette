"""What the bot says, as Telegram HTML, in the bot's language (vidette.telegram.i18n holds
every phrase, English and Russian). Pure functions: the service passes facts in and sends
the strings out, so every message has a test.

House rules for messages read on a phone: the camera and the time first, then what the
judge saw, then who; short lines; no dashes as punctuation; names only when the config
allows them (`names`), otherwise "one of us".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Literal

from pydantic import ValidationError

from vidette.people.report import IdentityBrief
from vidette.telegram import reasons
from vidette.telegram.i18n import month, n, ordinal, t

MAX_TEXT = 4096  # Telegram's limit for a message
LEVEL_ICONS = {0: "🚨", 1: "⚠️"}
Answer = Literal["right", "wrong", "unsure", "ours"]


def answer_word(answer: str) -> str:
    """The word for an answer about a verdict: right, false alarm, not sure, one of us."""
    return (
        t(f"answer.{answer}")
        if answer in ("right", "wrong", "unsure", "ours")
        else t("answer.marked")
    )


def clock(ts: float, *, now: float | None = None, seconds: bool = False) -> str:
    """`17:01` today, `13.11 17:01` another day (local time)."""
    at = datetime.fromtimestamp(ts)
    same_day = now is not None and datetime.fromtimestamp(now).date() == at.date()
    stamp = at.strftime("%H:%M:%S" if seconds else "%H:%M")
    return stamp if same_day or now is None else f"{at:%d.%m} {stamp}"


def plural(count: int, key: str) -> str:
    """`count` with the catalog noun `key` in the right form ("3 clips")."""
    return n(key, count)


def _brief(raw: object) -> IdentityBrief | None:
    try:
        return IdentityBrief.model_validate(raw)
    except ValidationError:
        return None


def stranger_phrase(
    raw: object,
    *,
    names: bool,
    camera_name: Callable[[str], str] | None = None,
    small: bool = False,
) -> str:
    """One face that is not enrolled: "stranger" or, when it belongs to an anonymous
    identity (ADR-0014), "stranger #12, 3rd time today (Street, Yard within the hour)". A
    name someone gave the identity is shown only with `names`; `small`: a face too small
    to compare ("small face")."""
    brief = _brief(raw)
    if brief is None:
        return t("who.small_face") if small else t("who.stranger")
    if names and brief.name:
        who = t("who.named", name=escape(brief.name), id=brief.id)
    elif brief.status == "suspicious":
        who = t("who.suspicious", id=brief.id)
    else:
        who = t("who.stranger_n", id=brief.id)
    if small:
        who += t("who.small_suffix")
    today = brief.today()
    if brief.sightings <= 1:
        count = t("who.first_time")
    elif today > 1:
        count = t("who.nth_today", nth=ordinal(today))
    else:
        count = t(
            "who.nth_over_days", nth=ordinal(brief.sightings), days=plural(brief.days, "n.days")
        )
    cameras = brief.cameras_within(3600.0)
    if len(cameras) > 1:
        named = ", ".join(escape(camera_name(c) if camera_name else c) for c in cameras)
        count += t("who.cameras_hour", cameras=named)
    return t("who.joined", who=who, count=count)


def _familiar(person: Mapping[str, Any]) -> bool:
    brief = _brief(person.get("identity"))
    return brief is not None and brief.status == "familiar"


def who_line(
    people: Mapping[str, Any] | None,
    persons: int | None,
    *,
    names: bool,
    camera_name: Callable[[str], str] | None = None,
) -> str:
    """ "one of us: Sam" / "familiar: Neighbour" / "stranger #12, 3rd time today" /
    "no face found", from the event's face report."""
    if people is None or "seen" not in people:
        if persons == 0:
            return t("who.no_people")
        return t("who.unchecked")
    seen = [p for p in people.get("seen") or [] if isinstance(p, Mapping)]
    known = [p for p in seen if p.get("status") == "known"]
    familiar = [p for p in seen if p.get("status") != "known" and _familiar(p)]
    unknown = [p for p in seen if p.get("status") == "unknown" and not _familiar(p)]
    unclear = [p for p in seen if p.get("status") == "unclear" and not _familiar(p)]
    parts: list[str] = []
    if known:
        if names:
            listed = ", ".join(escape(str(p.get("name") or p.get("person"))) for p in known)
            parts.append(t("who.ours_named", names=listed))
        else:
            parts.append(t("who.ours_many") if len(known) > 1 else t("who.ours_one"))
    if familiar:
        named = [b.name for p in familiar if (b := _brief(p.get("identity"))) and b.name]
        if names and named:
            parts.append(t("who.familiar_named", names=escape(", ".join(named))))
        else:
            parts.append(t("who.familiar_many") if len(familiar) > 1 else t("who.familiar_one"))
    others = [(p, False) for p in unknown] + [(p, True) for p in unclear]
    if others:
        phrases = [
            stranger_phrase(p.get("identity"), names=names, camera_name=camera_name, small=small)
            for p, small in others
        ]
        if len(others) == 1:
            parts.append(phrases[0])
        elif all(phrase in (t("who.stranger"), t("who.small_face")) for phrase in phrases):
            parts.append(t("who.strangers", n=len(others)))
        else:
            parts.append(t("who.strangers_list", n=len(others), list="; ".join(phrases)))
    if parts:
        return t("who.and").join(parts)
    if persons == 0:
        return t("who.no_people")
    return t("who.no_face")


@dataclass(frozen=True)
class AlertFacts:
    camera: str  # display name
    started_at: float
    level_rank: int  # 0 the highest level
    level_title: str
    description: str | None
    who: str
    alert: bool = True  # False only for a test of an event that is not an alert
    test: bool = False
    note: str | None = None  # e.g. why a test went to the alerts topic
    extra: tuple[str, ...] = ()  # collapsed later events
    mark: str | None = None  # "✅ Marked: false alarm"
    video_note: str | None = None  # a link when the clip was too big to send
    stream_note: str | None = None  # "The recording paused…" (stream_note())
    late: float | None = None  # sent late: when it was due, and Telegram could not be reached


def alert_text(facts: AlertFacts, *, now: float) -> str:
    icon = LEVEL_ICONS.get(facts.level_rank, "🔔")
    lines = []
    if facts.test:
        lines.append(t("alert.test"))
    if facts.alert:
        lines.append(t("alert.title", icon=icon, level=escape(facts.level_title)))
    else:
        lines.append(t("alert.not_alert"))
    lines.append(f"📍 {escape(facts.camera)}, {clock(facts.started_at, now=now, seconds=True)}")
    if facts.late is not None:
        lines.append(f"<i>{t('alert.late', time=clock(facts.late, now=now, seconds=True))}</i>")
    lines.append(f"👤 {facts.who}")
    if facts.description:
        lines.append(f"💬 {escape(facts.description)}")
    if facts.stream_note:
        lines.append(f"📼 {escape(facts.stream_note)}")
    if facts.video_note:
        lines.append(facts.video_note)
    if facts.note:
        lines.append(f"<i>{escape(facts.note)}</i>")
    if facts.extra:
        lines.append(t("alert.more_here", n=len(facts.extra)))
        lines += [f"＋ {line}" for line in facts.extra[-5:]]
    if facts.mark:
        lines.append(facts.mark)
    return truncate("\n".join(lines))


def upgrade_text(facts: AlertFacts, *, now: float) -> str:
    """The whole clip raised the level of an early alert: one more message that buzzes."""
    lines = [
        t("alert.upgrade", level=escape(facts.level_title)),
        f"📍 {escape(facts.camera)}, {clock(facts.started_at, now=now, seconds=True)}",
    ]
    if facts.description:
        lines.append(f"💬 {escape(facts.description)}")
    return truncate("\n".join(lines))


# Stages of one alert, in order, as the event's `intent.timeline` names them (the words:
# `stage.<name>` in the catalog).
LATENCY_STAGES: tuple[str, ...] = (
    "requested", "motion", "first_frame", "person_seen", "early", "early_picked_up",
    "early_judge_started", "early_judge_finished", "closed", "picked_up", "judge_started",
    "judge_finished", "sent",
)  # fmt: skip


def span(seconds: float) -> str:
    """ "7 s", "1:58", "1:02:03"."""
    whole = max(0, round(seconds))
    if whole < 60:
        return t("span.seconds", n=whole)
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def latency_text(timeline: Mapping[str, Any], *, sent_at: float | None) -> str | None:
    """ "Where the time went" for one alert: each stage after the previous one, then the total
    from the person (or the first frame) to the alert. None with fewer than two stages.

    Stages run on the clock of the machine that records; a camera's stream can lag behind
    the camera itself, which no stage here can see."""
    stamps = {k: float(v) for k, v in timeline.items() if isinstance(v, int | float)}
    if sent_at is not None:
        stamps["sent"] = sent_at
    if stamps.get("early_judge_finished") and sent_at is not None:
        # an alert sent from the early window: the clip's own stages came later
        for late in ("closed", "picked_up", "judge_started", "judge_finished"):
            if stamps.get(late, 0) > sent_at:
                stamps.pop(late)
    present = [(key, t(f"stage.{key}")) for key in LATENCY_STAGES if key in stamps]
    if len(present) < 2:
        return None
    lines = [t("latency.title")]
    previous_key, previous_words = present[0]
    lines.append(f"{previous_words}: {clock(stamps[previous_key], seconds=True)}")
    for key, words in present[1:]:
        lines.append(f"→ {words}: +{span(stamps[key] - stamps[previous_key])}")
        previous_key = key
    start = stamps.get("person_seen") or stamps.get("first_frame")
    if start is not None and "sent" in stamps:
        lines.append(
            t("latency.from_person", span=span(stamps["sent"] - start))
            if "person_seen" in stamps
            else t("latency.from_frame", span=span(stamps["sent"] - start))
        )
    return "\n".join(lines)


def interrupted(stream: object) -> bool:
    """Whether an event's stream health (`intent.stream`) says the recording paused."""
    return isinstance(stream, Mapping) and stream.get("interrupted") is True


def stream_note(stream: object) -> str | None:
    """ "The recording paused: 38 pauses, up to 13 s" for an interrupted recording."""
    if not isinstance(stream, Mapping) or not interrupted(stream):
        return None
    stalls = stream.get("stalls")
    longest = stream.get("longest_stall_s")
    parts = []
    if isinstance(stalls, int) and stalls > 0:
        parts.append(plural(stalls, "n.pauses"))
    if isinstance(longest, int | float) and longest >= 1:
        parts.append(t("stream.up_to", n=f"{longest:.0f}"))
    return t("stream.interrupted") + (": " + ", ".join(parts) if parts else "")


def dropped_text(times: list[float], *, now: float) -> str:
    """Alerts that waited for Telegram longer than `late_alerts_for` and were not sent."""
    first, last = min(times), max(times)
    if len(times) == 1:
        return t("late.dropped_one", time=clock(first, now=now))
    return t(
        "late.dropped_many", alerts=plural(len(times), "n.alerts"),
        first=clock(first, now=now), last=clock(last, now=now),
    )  # fmt: skip


def extra_line(started_at: float, description: str | None, *, now: float) -> str:
    text = escape(description) if description else t("alert.no_description")
    return f"{clock(started_at, now=now, seconds=True)} {text}"


def mark_line(answer: Answer, *, person: str | None = None, problem: str | None = None) -> str:
    word = answer_word(answer)
    if answer == "ours" and person:
        word = t("mark.ours_person", name=escape(person))
    line = t("mark.done", word=word)
    if problem:
        line += f"\n<i>{t('alert.label_failed', problem=escape(problem))}</i>"
    return line


def answer_button(data: Mapping[str, Any]) -> str:
    """The one button left under an alert after an answer about its verdict."""
    answer = data.get("answer")
    if answer == "right":
        return t("kb.right")
    if answer == "unsure":
        return t("kb.unsure_done")
    reason = reasons.BY_CODE.get(str(data.get("reason") or ""))
    if answer == "wrong":
        return f"👎 {reason.short}" if reason else t("kb.wrong")
    return "✅ " + answer_word(str(answer))


_ACROSS = {"left": "place.left", "right": "place.right", "middle": ""}
_DOWN = {"top": "place.top", "bottom": "place.bottom", "full": ""}


def mosaic_caption(
    start: float,
    end: float,
    tiles: list[tuple[str, tuple[str, str]]],
    lines: list[str],
    approximate: set[str],
) -> str:
    """The message under a mosaic: which tile is which camera, then what each recorded."""
    head = [t("mosaic.title", start=clock(start, seconds=True), end=clock(end, seconds=True))]
    if len(tiles) > 1:
        where = []
        for name, (across, down) in tiles:
            words = (t(key) for key in (_ACROSS[across], _DOWN[down]) if key)
            place = " ".join(words) or t("place.centre")
            where.append(t("mosaic.place", place=place, name=escape(name)))
        legend = "; ".join(where)
        head.append(legend[:1].upper() + legend[1:])
    body = [f"• {escape(line)}" for line in lines]
    if approximate:
        names = ", ".join(sorted(approximate))
        body.append(f"<i>{t('mosaic.approximate', names=escape(names))}</i>")
    return truncate("\n".join(head + body), 1024)


def no_mosaic_text(moment: float, lines: list[str]) -> str:
    head = t("mosaic.none", time=clock(moment, seconds=True))
    return truncate("\n".join([head, *(f"• {escape(line)}" for line in lines)]))


def face_mark_line(seen: Mapping[str, Any] | None, *, names: bool) -> str:
    """What was decided about one face (ADR-0016): "✅ Marked: familiar “Neighbour” (#5)",
    "✅ Marked: stranger (#12)", or that the decision was taken back."""
    if seen is None or not seen.get("confirmed"):
        return t("face.undone")
    brief = _brief(seen.get("identity"))
    number = t("face.number", id=brief.id) if brief is not None else ""
    name = brief.name if brief is not None and brief.name and names else None
    confirmed = str(seen.get("confirmed"))
    if confirmed == "stranger":
        word = t("face.stranger", number=number)
    elif confirmed == "unsure":
        word = t("face.unsure")
    elif confirmed.startswith("identity:") and brief is not None:
        if brief.status == "familiar":
            word = (
                t("face.familiar_named", name=escape(name), number=number)
                if name
                else t("face.familiar", number=number)
            )
        elif brief.status == "suspicious":
            word = t("face.suspicious", number=number)
        else:
            word = t("face.identity", id=brief.id) + (
                t("face.identity_named", name=escape(name)) if name else ""
            )
    else:
        word = t("face.ours")
    return t("mark.done", word=word)


def mark_prefix() -> str:
    """ "✅ Marked: " as the language writes it (what the service strips for a button)."""
    return t("mark.done", word="")


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 2].rsplit("\n", 1)[0]
    return cut + "\n…"


# --- digest -------------------------------------------------------------------------------


@dataclass
class DigestItem:
    camera: str  # display name
    started_at: float
    title: str  # the level, and who it was when only known people were seen
    description: str | None = None


@dataclass
class DayDigest:
    """One chat's day in numbers a person reads in ten seconds (`day_digest_text`)."""

    since: float
    events: int = 0
    alerts: int = 0  # alerts this chat got
    right: int = 0  # of those, answered 👍 (by anyone)
    wrong: int = 0  # answered 👎
    unanswered: int = 0
    last_unanswered: tuple[float, str] | None = None  # (time, camera name)
    reasons: Counter[str] = field(default_factory=Counter)  # the reasons' short words
    notable: list[DigestItem] = field(default_factory=list)  # confirmed, did not wake here
    ordinary: Counter[str] = field(default_factory=Counter)  # titles of ordinary scenarios
    unjudged: int = 0
    unjudged_why: Counter[str] = field(default_factory=Counter)
    deep_checked: int = 0  # looked at again by deep analysis (ADR-0019)
    deep_review: int = 0  # where it and the live judge part: in the next labelling batch
    deep_found: int = 0  # of the notable items, the ones only deep analysis flagged

    @property
    def substance(self) -> bool:
        """Worth a message on its own: something did not wake anyone but was confirmed, or
        an alert still waits for an answer."""
        return bool(self.notable) or self.unanswered > 0


def day_digest_text(day: DayDigest, *, now: float, evening: bool) -> str:
    """Plain short lines, no codes: what happened, what was wrong, what waits."""
    date = datetime.fromtimestamp(now)
    if evening:
        lines = [t("digest.evening", day=date.day, month=month(date.month))]
    else:
        lines = [t("digest.so_far", time=clock(now, now=now))]
    if day.alerts:
        answers = []
        if day.wrong:
            answers.append(t("digest.false_n", n=day.wrong))
        if day.right:
            answers.append(t("digest.right_n", n=day.right))
        if day.unanswered:
            answers.append(t("digest.unanswered_n", n=day.unanswered))
        said = t("digest.by_marks", list=", ".join(answers)) if answers else ""
        lines.append(t("digest.alerts", n=day.alerts, marks=said))
    else:
        lines.append(t("digest.no_alerts"))
    if day.reasons:
        common = ", ".join(f"{escape(word)} ({k})" for word, k in day.reasons.most_common(3))
        lines.append(t("digest.common_false", list=common))
    if day.unanswered and day.last_unanswered is not None:
        at, camera = day.last_unanswered
        lines.append(
            t("digest.waiting_line", waiting=plural(day.unanswered, "n.waiting"),
              time=clock(at, now=now), camera=escape(camera))
        )  # fmt: skip
    if day.notable:
        lines.append(t("digest.look"))
        for item in day.notable[-6:]:
            stamp = clock(item.started_at, now=now)
            text = f"• {stamp} {escape(item.camera)}: {escape(item.title)}"
            if item.description:
                text += f". {escape(item.description)}"
            lines.append(text)
        if len(day.notable) > 6:
            lines.append(t("digest.and_more", n=len(day.notable) - 6))
    if day.events:
        clips = plural(day.events, "n.clips")
        common = ", ".join(f"{escape(title)} {k}" for title, k in day.ordinary.most_common(4))
        lines.append(t("digest.total", clips=clips) + (f": {common}." if common else "."))
    if day.unjudged:
        why = day.unjudged_why.most_common(1)[0][0] if day.unjudged_why else t("unjudged.queue")
        lines.append(t("digest.unjudged", n=day.unjudged, why=escape(why)))
    if day.deep_review:
        lines.append(
            t("digest.deep", clips=plural(day.deep_checked, "n.clips"),
              disputed=plural(day.deep_review, "n.disputed"))
        )  # fmt: skip
    return truncate("\n".join(lines))


# --- commands -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraToday:
    name: str
    events: int
    alerts: int
    last_at: float | None


def status_text(
    *,
    now: float,
    judge: str,
    judged: int,
    unjudged: int,
    cameras: list[CameraToday],
    last_alert: str | None,
    quiet_until: float | None,
    latency: str | None = None,
) -> str:
    lines = [f"📊 <b>Vidette</b> · {clock(now, now=now)}"]
    lines.append(t("status.judge", judge=escape(judge), judged=judged, unjudged=unjudged))
    if cameras:
        lines.append(t("status.by_camera"))
        for cam in cameras:
            last = "" if cam.last_at is None else t("status.last", time=clock(cam.last_at, now=now))
            alerts = t("status.alerts", n=cam.alerts) if cam.alerts else ""
            events = plural(cam.events, "n.events")
            lines.append(f"• {escape(cam.name)}: {events}{alerts}{last}")
    what = escape(last_alert) if last_alert else t("status.no_alert")
    lines.append(t("status.last_alert", what=what))
    if latency:
        lines.append(latency)
    if quiet_until is not None and quiet_until > now:
        lines.append(t("status.quiet", time=clock(quiet_until, now=now)))
    return truncate("\n".join(lines))


def live_text() -> str:
    return t("live")


def quiet_text(until: float | None, *, now: float) -> str:
    if until is None or until <= now:
        return t("quiet.off")
    return t("quiet.on", time=clock(until, now=now))


def help_text() -> str:
    return t("help")
