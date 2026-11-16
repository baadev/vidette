"""What the bot says, in Russian, as Telegram HTML. Pure functions: the service passes
facts in and sends the strings out, so every message has a test.

House rules for messages read on a phone: the camera and the time first, then what the
judge saw, then who; short lines; no dashes as punctuation; names only when the config
allows them (`names`), otherwise "свой".
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


def answer_button(data: Mapping[str, Any]) -> str:
    """The one button left under an alert after an answer about its verdict."""
    answer = data.get("answer")
    if answer == "right":
        return "👍 верно"
    if answer == "unsure":
        return "🤷 не знаю"
    reason = reasons.BY_CODE.get(str(data.get("reason") or ""))
    if answer == "wrong":
        return f"👎 {reason.short}" if reason else "👎 ложная"
    return "✅ " + ANSWER_WORDS.get(str(answer), "отмечено")


_ACROSS = {"left": "слева", "right": "справа", "middle": ""}
_DOWN = {"top": "сверху", "bottom": "снизу", "full": ""}


def mosaic_caption(
    start: float,
    end: float,
    tiles: list[tuple[str, tuple[str, str]]],
    lines: list[str],
    approximate: set[str],
) -> str:
    """The message under a mosaic: which tile is which camera, then what each recorded."""
    head = [f"📹 <b>Все камеры, {clock(start, seconds=True)}–{clock(end, seconds=True)}</b>"]
    if len(tiles) > 1:
        where = []
        for name, (across, down) in tiles:
            place = " ".join(w for w in (_ACROSS[across], _DOWN[down]) if w) or "по центру"
            where.append(f"{place}: {escape(name)}")
        legend = "; ".join(where)
        head.append(legend[:1].upper() + legend[1:])
    body = [f"• {escape(line)}" for line in lines]
    if approximate:
        names = ", ".join(sorted(approximate))
        body.append(
            f"<i>{escape(names)}: время примерное, эта камера отдаёт запись с задержкой, "
            "её картинка может отставать от остальных</i>"
        )
    return truncate("\n".join(head + body), 1024)


def no_mosaic_text(moment: float, lines: list[str]) -> str:
    head = f"📹 Около {clock(moment, seconds=True)} другие камеры ничего не записали."
    return truncate("\n".join([head, *(f"• {escape(line)}" for line in lines)]))


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

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
    "октября", "ноября", "декабря",
)  # fmt: skip


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
    """Plain short Russian lines, no codes: what happened, what was wrong, what waits."""
    date = datetime.fromtimestamp(now)
    if evening:
        lines = [f"🌙 <b>Итог дня, {date.day} {MONTHS[date.month - 1]}</b>"]
    else:
        lines = [f"📋 <b>Сегодня к {clock(now, now=now)}</b>"]
    if day.alerts:
        answers = []
        if day.wrong:
            answers.append(f"ложных {day.wrong}")
        if day.right:
            answers.append(f"верных {day.right}")
        if day.unanswered:
            answers.append(f"без ответа {day.unanswered}")
        said = f" По вашим отметкам: {', '.join(answers)}." if answers else ""
        lines.append(f"Тревог: {day.alerts}.{said}")
    else:
        lines.append("Тревог не было.")
    if day.reasons:
        common = ", ".join(f"{escape(word)} ({n})" for word, n in day.reasons.most_common(3))
        lines.append(f"Ложные чаще всего: {common}.")
    if day.unanswered and day.last_unanswered is not None:
        at, camera = day.last_unanswered
        waiting = plural(day.unanswered, "тревога ждёт", "тревоги ждут", "тревог ждут")
        lines.append(
            f"{waiting} ответа, последняя в {clock(at, now=now)} ({escape(camera)}). "
            "Кнопки под тревогой."
        )
    if day.notable:
        lines.append("Без тревоги, но стоит взглянуть:")
        for item in day.notable[-6:]:
            stamp = clock(item.started_at, now=now)
            text = f"• {stamp} {escape(item.camera)}: {escape(item.title)}"
            if item.description:
                text += f". {escape(item.description)}"
            lines.append(text)
        if len(day.notable) > 6:
            lines.append(f"…и ещё {len(day.notable) - 6}")
    if day.events:
        clips = plural(day.events, "ролик", "ролика", "роликов")
        common = ", ".join(f"{escape(t)} {n}" for t, n in day.ordinary.most_common(4))
        lines.append(f"Всего за день {clips}" + (f": {common}." if common else "."))
    if day.unjudged:
        why = day.unjudged_why.most_common(1)[0][0] if day.unjudged_why else "не дошла очередь"
        lines.append(f"Не проверено судьёй: {day.unjudged} ({escape(why)}).")
    if day.deep_review:
        lines.append(
            f"Перепроверка: {plural(day.deep_checked, 'ролик', 'ролика', 'роликов')}; "
            f"{plural(day.deep_review, 'спорный ушёл', 'спорных ушли', 'спорных ушли')} "
            "на разметку."
        )
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
    "Я присылаю тревоги с камер и вечером итог дня, если есть что посмотреть.\n"
    "/status: камеры, события за сегодня, последняя тревога и сводка\n"
    "/live: живое видео (будет в приложении Vidette)\n"
    "/quiet: тишина на час (критичное всё равно придёт), /quiet off: снять\n"
    "Под тревогой: 👍 верно, 👎 ложная (спрошу почему), Кто это?, Не знаю, 📹 Другие камеры. "
    "Ответ можно отменить, нажав на него.\n"
    "Можно просто спросить: «где ещё был этот человек?» или «пришли другие камеры» в ответ "
    "на тревогу, «что было у калитки после 9?», «сводка»."
)
