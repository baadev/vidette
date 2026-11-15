"""What the bot says, in Russian, as Telegram HTML. Pure functions: the service passes
facts in and sends the strings out, so every message has a test.

House rules for messages read on a phone: the camera and the time first, then what the
judge saw, then who; short lines; no dashes as punctuation; names only when the config
allows them (`names`), otherwise "свой".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Literal

from pydantic import ValidationError

from vidette.people.report import IdentityBrief

MAX_TEXT = 4096  # Telegram's limit for a message
LEVEL_ICONS = {0: "🚨", 1: "⚠️"}
Answer = Literal["right", "wrong", "unsure", "ours"]
ANSWER_WORDS: dict[str, str] = {
    "right": "верно",
    "wrong": "ложная",
    "unsure": "не знаю",
    "ours": "свой",
}


def clock(ts: float, *, now: float | None = None, seconds: bool = False) -> str:
    """`17:01` today, `13.11 17:01` another day (local time)."""
    at = datetime.fromtimestamp(ts)
    same_day = now is not None and datetime.fromtimestamp(now).date() == at.date()
    stamp = at.strftime("%H:%M:%S" if seconds else "%H:%M")
    return stamp if same_day or now is None else f"{at:%d.%m} {stamp}"


def plural(count: int, one: str, few: str, many: str) -> str:
    n = abs(count) % 100
    if 10 < n < 20:
        return f"{count} {many}"
    n %= 10
    word = one if n == 1 else few if 2 <= n <= 4 else many
    return f"{count} {word}"


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
    """One face that is not enrolled: «незнакомец» or, when it belongs to an anonymous
    identity (ADR-0014), «незнакомец №12, 3-й раз за сегодня (Улица, Двор за час)». A
    name someone gave the identity is shown only with `names`; `small`: a face too small
    to compare («лицо мелкое»)."""
    brief = _brief(raw)
    if brief is None:
        return "лицо мелкое" if small else "незнакомец"
    if names and brief.name:
        who = f"{escape(brief.name)} (№{brief.id})"
    elif brief.status == "suspicious":
        who = f"⚠️ подозрительный №{brief.id}"
    else:
        who = f"незнакомец №{brief.id}"
    if small:
        who += " (лицо мелкое)"
    today = brief.today()
    if brief.sightings <= 1:
        count = "впервые"
    elif today > 1:
        count = f"{today}-й раз за сегодня"
    else:
        count = f"{brief.sightings}-й раз за {plural(brief.days, 'день', 'дня', 'дней')}"
    cameras = brief.cameras_within(3600.0)
    if len(cameras) > 1:
        named = ", ".join(escape(camera_name(c) if camera_name else c) for c in cameras)
        count += f" ({named} за час)"
    return f"{who}, {count}"


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
    """«свой: Имя» / «знакомый: Соседка» / «незнакомец №12, 3-й раз за сегодня» /
    «лицо не найдено», from the event's face report."""
    if people is None or "seen" not in people:
        if persons == 0:
            return "людей не видно"
        return "лица не проверялись"
    seen = [p for p in people.get("seen") or [] if isinstance(p, Mapping)]
    known = [p for p in seen if p.get("status") == "known"]
    familiar = [p for p in seen if p.get("status") != "known" and _familiar(p)]
    unknown = [p for p in seen if p.get("status") == "unknown" and not _familiar(p)]
    unclear = [p for p in seen if p.get("status") == "unclear" and not _familiar(p)]
    parts: list[str] = []
    if known:
        if names:
            listed = ", ".join(escape(str(p.get("name") or p.get("person"))) for p in known)
            parts.append(f"свой: {listed}")
        else:
            parts.append("свои" if len(known) > 1 else "свой")
    if familiar:
        named = [b.name for p in familiar if (b := _brief(p.get("identity"))) and b.name]
        if names and named:
            parts.append(f"знакомый: {escape(', '.join(named))}")
        else:
            parts.append("знакомые" if len(familiar) > 1 else "знакомый")
    others = [(p, False) for p in unknown] + [(p, True) for p in unclear]
    if others:
        phrases = [
            stranger_phrase(p.get("identity"), names=names, camera_name=camera_name, small=small)
            for p, small in others
        ]
        if len(others) == 1:
            parts.append(phrases[0])
        elif all(phrase in ("незнакомец", "лицо мелкое") for phrase in phrases):
            parts.append(f"незнакомцы: {len(others)}")
        else:
            parts.append(f"незнакомцы: {len(others)} ({'; '.join(phrases)})")
    if parts:
        return " и ".join(parts)
    if persons == 0:
        return "людей не видно"
    return "лицо не найдено"


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
    mark: str | None = None  # "✅ Отмечено: ложная"
    video_note: str | None = None  # a link when the clip was too big to send
    stream_note: str | None = None  # «запись прерывалась…» (stream_note())


def alert_text(facts: AlertFacts, *, now: float) -> str:
    icon = LEVEL_ICONS.get(facts.level_rank, "🔔")
    lines = []
    if facts.test:
        lines.append("🧪 <b>Проверка канала</b> (настоящий клип, настоящий судья)")
    if facts.alert:
        lines.append(f"{icon} <b>Тревога: {escape(facts.level_title)}</b>")
    else:
        lines.append("🔔 <b>Событие, не тревога</b>")
    lines.append(f"📍 {escape(facts.camera)}, {clock(facts.started_at, now=now, seconds=True)}")
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
        lines.append(f"Ещё на этой камере: {len(facts.extra)}")
        lines += [f"＋ {line}" for line in facts.extra[-5:]]
    if facts.mark:
        lines.append(facts.mark)
    return truncate("\n".join(lines))


def upgrade_text(facts: AlertFacts, *, now: float) -> str:
    """The whole clip raised the level of an early alert: one more message that buzzes."""
    lines = [
        f"⬆️ <b>Уровень выше: {escape(facts.level_title)}</b>",
        f"📍 {escape(facts.camera)}, {clock(facts.started_at, now=now, seconds=True)}",
    ]
    if facts.description:
        lines.append(f"💬 {escape(facts.description)}")
    return truncate("\n".join(lines))


# Stages of one alert, in order, as the event's `intent.timeline` names them.
LATENCY_STAGES: tuple[tuple[str, str], ...] = (
    ("requested", "камера проснулась"),
    ("motion", "движение"),
    ("first_frame", "первый кадр"),
    ("person_seen", "человек в кадре"),
    ("early", "первые секунды записаны"),
    ("early_picked_up", "сервер взял их"),
    ("early_judge_started", "судья начал"),
    ("early_judge_finished", "судья ответил"),
    ("closed", "ролик готов"),
    ("picked_up", "сервер взял ролик"),
    ("judge_started", "судья начал"),
    ("judge_finished", "судья ответил"),
    ("sent", "тревога ушла"),
)


def span(seconds: float) -> str:
    """«7 с», «1:58», «1:02:03»."""
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole} с"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def latency_text(timeline: Mapping[str, Any], *, sent_at: float | None) -> str | None:
    """«Где ушло время» for one alert: each stage after the previous one, then the total
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
    present = [(key, words) for key, words in LATENCY_STAGES if key in stamps]
    if len(present) < 2:
        return None
    lines = ["⏱ <b>Где ушло время</b>"]
    previous_key, previous_words = present[0]
    lines.append(f"{previous_words}: {clock(stamps[previous_key], seconds=True)}")
    for key, words in present[1:]:
        lines.append(f"→ {words}: +{span(stamps[key] - stamps[previous_key])}")
        previous_key = key
    start = stamps.get("person_seen") or stamps.get("first_frame")
    if start is not None and "sent" in stamps:
        lines.append(
            f"От человека в кадре до тревоги: {span(stamps['sent'] - start)}"
            if "person_seen" in stamps
            else f"От первого кадра до тревоги: {span(stamps['sent'] - start)}"
        )
    return "\n".join(lines)


def interrupted(stream: object) -> bool:
    """Whether an event's stream health (`intent.stream`) says the recording paused."""
    return isinstance(stream, Mapping) and stream.get("interrupted") is True


def stream_note(stream: object) -> str | None:
    """«Запись прерывалась: 38 пауз, до 13 с» for an interrupted recording, else None."""
    if not isinstance(stream, Mapping) or not interrupted(stream):
        return None
    stalls = stream.get("stalls")
    longest = stream.get("longest_stall_s")
    parts = []
    if isinstance(stalls, int) and stalls > 0:
        parts.append(plural(stalls, "пауза", "паузы", "пауз"))
    if isinstance(longest, int | float) and longest >= 1:
        parts.append(f"до {longest:.0f} с")
    return "Запись прерывалась" + (": " + ", ".join(parts) if parts else "")


def extra_line(started_at: float, description: str | None, *, now: float) -> str:
    text = escape(description) if description else "без описания"
    return f"{clock(started_at, now=now, seconds=True)} {text}"


def mark_line(answer: Answer, *, person: str | None = None, problem: str | None = None) -> str:
    word = ANSWER_WORDS[answer]
    if answer == "ours" and person:
        word = f"свой ({escape(person)})"
    line = f"✅ Отмечено: {word}"
    if problem:
        line += f"\n<i>метка в датасет не записана: {escape(problem)}</i>"
    return line


def face_mark_line(seen: Mapping[str, Any] | None, *, names: bool) -> str:
    """What was decided about one face (ADR-0016): «✅ Отмечено: знакомый «Соседка»
    (№5)», «✅ Отмечено: чужой (№12)», or that the decision was taken back."""
    if seen is None or not seen.get("confirmed"):
        return "↩️ Решение по лицу отменено"
    brief = _brief(seen.get("identity"))
    number = f" (№{brief.id})" if brief is not None else ""
    name = brief.name if brief is not None and brief.name and names else None
    confirmed = str(seen.get("confirmed"))
    if confirmed == "stranger":
        word = f"чужой{number}"
    elif confirmed == "unsure":
        word = "не знаю"
    elif confirmed.startswith("identity:") and brief is not None:
        if brief.status == "familiar":
            word = f"знакомый «{escape(name)}»{number}" if name else f"знакомый{number}"
        elif brief.status == "suspicious":
            word = f"подозрительный{number}"
        else:
            word = f"личность №{brief.id}" + (f" «{escape(name)}»" if name else "")
    else:
        word = "свой"
    return f"✅ Отмечено: {word}"


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
    kind: Literal["notable", "ordinary", "unjudged"]
    title: str  # scenario title (ordinary), reason (notable), why (unjudged)
    description: str | None = None
    interrupted: bool = False  # the recording paused (stream health)


@dataclass
class DigestCamera:
    name: str
    items: list[DigestItem] = field(default_factory=list)


def _collapse(items: list[DigestItem], window_s: float) -> list[tuple[DigestItem, DigestItem, int]]:
    """Notable items of one camera close together → (first, last, count)."""
    groups: list[tuple[DigestItem, DigestItem, int]] = []
    for item in sorted(items, key=lambda i: i.started_at):
        if groups:
            first, last, count = groups[-1]
            if item.title == last.title and item.started_at - last.started_at <= window_s:
                groups[-1] = (first, item, count + 1)
                continue
        groups.append((item, item, 1))
    return groups


def digest_text(
    cameras: Iterable[DigestCamera],
    *,
    since: float,
    until: float,
    now: float,
    collapse_s: float,
    title: str = "Сводка",
) -> str | None:
    """None when nothing happened: an empty digest is not sent."""
    cams = [cam for cam in cameras if cam.items]
    if not cams:
        return None
    lines = [f"📋 <b>{escape(title)} {clock(since, now=now)}–{clock(until, now=now)}</b>"]
    reasons: Counter[str] = Counter()
    for cam in cams:
        ordinary = Counter(i.title for i in cam.items if i.kind == "ordinary")
        unjudged = [i for i in cam.items if i.kind == "unjudged"]
        notable = [i for i in cam.items if i.kind == "notable"]
        parts = [plural(len(cam.items), "событие", "события", "событий")]
        parts += [f"{escape(name)} {count}" for name, count in ordinary.most_common(6)]
        rest = sum(ordinary.values()) - sum(c for _n, c in ordinary.most_common(6))
        if rest:
            parts.append(f"другое {rest}")
        if unjudged:
            parts.append(f"без вердикта {len(unjudged)}")
            reasons.update(i.title for i in unjudged)
        broken = sum(1 for i in cam.items if i.interrupted)
        if broken:
            parts.append(f"запись прерывалась {broken}")
        lines.append(f"<b>{escape(cam.name)}</b>: " + " · ".join(parts))
        for first, last, count in _collapse(notable, collapse_s)[:8]:
            span = clock(first.started_at, now=now)
            if count > 1:
                span += f"–{clock(last.started_at, now=now)} ×{count}"
            text = f"• {span} {escape(first.title)}"
            if last.description:
                text += f": {escape(last.description)}"
            if last.interrupted:
                text += " (запись прерывалась)"
            lines.append(text)
    if reasons:
        listed = ", ".join(f"{escape(r)} ×{n}" for r, n in reasons.most_common(3))
        lines.append(f"<i>Без вердикта: {listed}</i>")
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
    lines.append(f"Судья: {escape(judge)}. Сегодня вердиктов {judged}, без вердикта {unjudged}")
    if cameras:
        lines.append("<b>Сегодня по камерам</b>")
        for cam in cameras:
            last = "" if cam.last_at is None else f", последнее {clock(cam.last_at, now=now)}"
            alerts = f", тревог {cam.alerts}" if cam.alerts else ""
            events = plural(cam.events, "событие", "события", "событий")
            lines.append(f"• {escape(cam.name)}: {events}{alerts}{last}")
    lines.append(f"Последняя тревога: {escape(last_alert) if last_alert else 'не было'}")
    if latency:
        lines.append(latency)
    if quiet_until is not None and quiet_until > now:
        lines.append(f"🔕 Тишина до {clock(quiet_until, now=now)}")
    return truncate("\n".join(lines))


LIVE_TEXT = (
    "📹 Живое видео будет прямо в приложении Vidette, без отдельных программ. Пока его нет: "
    "последние события и сводка по команде /status."
)


def quiet_text(until: float | None, *, now: float) -> str:
    if until is None or until <= now:
        return "🔔 Тишина выключена: тревоги приходят как обычно."
    return (
        f"🔕 Тишина до {clock(until, now=now)}. Критичные тревоги всё равно придут, "
        "остальное копится в сводку. Снять: /quiet off"
    )


HELP_TEXT = (
    "Я присылаю тревоги с камер и раз в час сводку.\n"
    "/status: камеры, события за сегодня, последняя тревога и накопившаяся сводка\n"
    "/live: живое видео (будет в приложении Vidette)\n"
    "/quiet: тишина на час (критичное всё равно придёт), /quiet off: снять\n"
    "Под тревогой кнопки: 👍 верно, 👎 ложная, Это свой, Не знаю. Ответы учат систему."
)
