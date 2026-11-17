"""Everything the bot says, one catalog per language (`notifications.telegram.language`).

English is the reference text; every other catalog has the same keys and the same
`{placeholders}` (a test holds them to it). The code never writes a user-facing phrase
itself: it asks for a key, `t("digest.no_alerts")`, and `n("n.clips", 3)` for a count with
its noun in the language's plural form ("3 clips", «3 ролика»). Forms are separated by
`|`: one|other in English, one|few|many in Russian.

The language is process-wide (one bot per server): `use()` is called when the bot, the
runtime or a command-line tool reads its config. Free text a model writes (the judge's
description, deep analysis, the assistant's answers) follows the same setting through
the prompt (`model_language`), not through this catalog.
"""

from __future__ import annotations

from typing import Literal, get_args

Language = Literal["en", "ru"]
LANGUAGES: tuple[str, ...] = get_args(Language)
MODEL_LANGUAGE: dict[str, str] = {"en": "English", "ru": "Russian"}

EN: dict[str, str] = {
    # answers and buttons under an alert
    "answer.right": "right",
    "answer.wrong": "false alarm",
    "answer.unsure": "not sure",
    "answer.ours": "one of us",
    "answer.marked": "marked",
    "kb.right": "👍 right",
    "kb.wrong": "👎 false alarm",
    "kb.unsure": "Not sure",
    "kb.unsure_done": "🤷 not sure",
    "kb.who": "Who is it?",
    "kb.no_reason": "No reason",
    "kb.face": "Face {n}: {word}",
    "kb.back": "← Back",
    "kb.ours": "One of us: {name}",
    "kb.familiar": "Familiar: {name}",
    "kb.new_familiar": "New familiar…",
    "kb.new_identity": "New identity…",
    "kb.stranger": "Stranger",
    "kb.undo_face": "Take it back",
    "kb.other_cameras": "📹 Other cameras",
    "kb.cal_undo": "↩️ Undo",
    "kb.cal_undo_n": "↩️ {n}",
    "face.small": "small",
    "face.unknown": "unknown",
    # why 👎: the reasons (vidette.telegram.reasons)
    "reason.passing.button": "Walked past",
    "reason.passing.short": "walked past",
    "reason.car_use.button": "Got into a car, theirs",
    "reason.car_use.short": "their own car",
    "reason.ours.button": "One of us or familiar",
    "reason.ours.short": "ours",
    "reason.animal.button": "An animal",
    "reason.animal.short": "animal",
    "reason.vehicle.button": "A car drove past",
    "reason.vehicle.short": "a car",
    "reason.light.button": "Light, shadow, rain",
    "reason.light.short": "light, shadow",
    "reason.stalled.button": "The recording froze",
    "reason.stalled.short": "recording froze",
    "reason.camera.button": "Wrong camera",
    "reason.camera.short": "wrong camera",
    "reason.unimportant.button": "Right, but not important",
    "reason.unimportant.short": "not important",
    "reason.other.button": "Other: I'll write",
    "reason.other.short": "other",
    # who was seen
    "who.small_face": "small face",
    "who.stranger": "stranger",
    "who.named": "{name} (#{id})",
    "who.suspicious": "⚠️ suspicious #{id}",
    "who.stranger_n": "stranger #{id}",
    "who.small_suffix": " (small face)",
    "who.first_time": "first time",
    "who.nth_today": "{nth} time today",
    "who.nth_over_days": "{nth} time in {days}",
    "who.cameras_hour": " ({cameras} within the hour)",
    "who.joined": "{who}, {count}",
    "who.no_people": "no people in view",
    "who.unchecked": "faces not checked",
    "who.ours_named": "one of us: {names}",
    "who.ours_one": "one of us",
    "who.ours_many": "our people",
    "who.familiar_named": "familiar: {names}",
    "who.familiar_one": "familiar",
    "who.familiar_many": "familiar people",
    "who.strangers": "strangers: {n}",
    "who.strangers_list": "strangers: {n} ({list})",
    "who.and": " and ",
    "who.no_face": "no face found",
    "who.raised": " · level raised: not the first visit",
    # the alert
    "alert.test": "🧪 <b>Channel test</b> (a real clip, the real judge)",
    "alert.title": "{icon} <b>Alert: {level}</b>",
    "alert.not_alert": "🔔 <b>An event, not an alert</b>",
    "alert.more_here": "More on this camera: {n}",
    "alert.upgrade": "⬆️ <b>Level raised: {level}</b>",
    "alert.no_description": "no description",
    "alert.no_verdict": "No verdict: {why}",
    "alert.judge_unsure": "The judge is not sure: {why}",
    "alert.label_failed": "the label was not written to the dataset: {problem}",
    "alert.frames_failed": " The frames were not sent.",
    "alert.late": "⏳ Sent late: Telegram could not be reached at {time}.",
    "late.dropped_one": "⏳ The alert at {time} was not sent: Telegram could not be "
    "reached for too long. /status shows the day.",
    "late.dropped_many": "⏳ {alerts} from {first} to {last} were not sent: Telegram "
    "could not be reached for too long. /status shows the day.",
    "note.early": "From the first seconds of the recording; the whole clip will follow in "
    "this message.",
    "note.clip_not_alert": "Checked against the whole clip: not an alert.",
    "note.clip_unjudged": "The judge did not rate the whole clip; the alert is from the first "
    "seconds.",
    "note.clip_refined": "Checked against the whole clip.",
    "video.too_big_whole": "🎞 The whole clip is {mb} MB, over the limit: ",
    "video.too_big": "🎞 The clip is {mb} MB, over the limit: ",
    "video.watch_home": "watch at home",
    "video.watch_app": "watch in Vidette",
    "why.no_verdict": "Without the test this would have gone to the digest: the judge gave "
    "no verdict.",
    "why.not_alert": "Without the test this would have gone to the digest: the judge does not "
    "think it is an alert.",
    "why.quiet": "Without the test this would have gone to the digest: quiet is on.",
    "why.known": "Without the test this would have gone to the digest: only our own people "
    "were recognized.",
    "why.plain": "Without the test this would have gone to the digest.",
    "why.calibrated": "Without the test this would have gone to the digest: your 👍/👎 made "
    "this quieter (/tuning).",
    "level.by_policy": "by the policy",
    "level.none": "none",
    "scenario.quiet": "quiet",
    "scenario.other": "other",
    # marks
    "mark.done": "✅ Marked: {word}",
    "mark.ours_person": "one of us ({name})",
    "face.undone": "↩️ The decision about the face was taken back",
    "face.number": " (#{id})",
    "face.stranger": "stranger{number}",
    "face.unsure": "not sure",
    "face.familiar_named": "familiar “{name}”{number}",
    "face.familiar": "familiar{number}",
    "face.suspicious": "suspicious{number}",
    "face.identity": "identity #{id}",
    "face.identity_named": " “{name}”",
    "face.ours": "one of us",
    "face.ours_named": "one of us: {name}",
    # timing
    "span.seconds": "{n} s",
    "latency.title": "⏱ <b>Where the time went</b>",
    "latency.from_person": "From the person in view to the alert: {span}",
    "latency.from_frame": "From the first frame to the alert: {span}",
    "stage.requested": "camera woke up",
    "stage.motion": "motion",
    "stage.first_frame": "first frame",
    "stage.person_seen": "person in view",
    "stage.early": "first seconds recorded",
    "stage.early_picked_up": "the server took them",
    "stage.early_judge_started": "the judge started",
    "stage.early_judge_finished": "the judge answered",
    "stage.closed": "clip ready",
    "stage.picked_up": "the server took the clip",
    "stage.judge_started": "the judge started",
    "stage.judge_finished": "the judge answered",
    "stage.sent": "alert sent",
    "stream.interrupted": "The recording paused",
    "stream.up_to": "up to {n} s",
    # the other cameras
    "place.left": "left",
    "place.right": "right",
    "place.top": "top",
    "place.bottom": "bottom",
    "place.centre": "in the middle",
    "mosaic.title": "📹 <b>All cameras, {start}–{end}</b>",
    "mosaic.place": "{place}: {name}",
    "mosaic.approximate": "{names}: approximate time, this camera delivers its recording "
    "late and its picture may lag behind the others",
    "mosaic.none": "📹 Around {time} the other cameras recorded nothing.",
    "mosaic.failed": "Could not stitch the video: {error}",
    "mosaic.part": "Part {n} of {total}",
    "linked.around": "about {span}",
    "linked.this_event": "this event",
    "linked.same_person": "the same person by face",
    "linked.person_no_face": "a person in view, no face to compare",
    "linked.nothing": "{names}: nothing recorded at that time (the camera slept or nothing moved)",
    # the incident across cameras (ADR-0022)
    "incident.face": "🔗 The same person (by face): {camera}, {time}",
    "incident.look": "🔗 Probably the same person (clothes and timing): {camera}, {time}",
    "incident.time": "🔗 At the same time a person: {camera}, {time}",
    "incident.missed": " (the judge had let it pass)",
    "incident.approx": "about {time}",
    "incident.digest": "🔗 With it: {parts}",
    "event.deleted": "The event is already deleted.",
    "event.deleted_short": "The event is deleted",
    # the digest
    "months": "January|February|March|April|May|June|July|August|September|October|"
    "November|December",
    "digest.evening": "🌙 <b>The day, {month} {day}</b>",
    "digest.so_far": "📋 <b>Today until {time}</b>",
    "digest.false_n": "false {n}",
    "digest.right_n": "right {n}",
    "digest.unanswered_n": "unanswered {n}",
    "digest.by_marks": " By your marks: {list}.",
    "digest.alerts": "Alerts: {n}.{marks}",
    "digest.no_alerts": "No alerts.",
    "digest.common_false": "False most often: {list}.",
    "digest.waiting_line": "{waiting} for an answer, the last at {time} ({camera}). The "
    "buttons are under the alert.",
    "digest.look": "No alert, but worth a look:",
    "digest.and_more": "…and {n} more",
    "digest.total": "All day: {clips}",
    "digest.unjudged": "Not checked by the judge: {n} ({why}).",
    "digest.deep": "Second look: {clips}; {disputed} to labelling.",
    "digest.second_look": " (second look)",
    "digest.calibrated": " · in the digest by your marks",
    "n.days": "day|days",
    "n.clips": "clip|clips",
    "n.events": "event|events",
    "n.pauses": "pause|pauses",
    "n.waiting": "alert waits|alerts wait",
    "n.disputed": "disputed clip went|disputed clips went",
    "n.times": "time|times",
    "n.alerts": "alert|alerts",
    "unjudged.queue": "the queue did not reach it",
    "unjudged.none": "no verdict",
    "unjudged.context": "the frames do not fit the judge's model",
    "unjudged.timeout": "the judge ran out of time",
    "unjudged.network": "the judge is unreachable",
    "unjudged.budget": "the judge's budget is spent",
    "unjudged.auth": "the judge refused the key",
    "unjudged.rate_limited": "the judge is busy",
    "unjudged.server": "the judge failed",
    "unjudged.not_found": "the model was not found",
    "unjudged.bad_answer": "the judge's answer made no sense",
    "unjudged.unsure": "the judge is not sure",
    "unjudged.no_frames": "no frames",
    # commands
    "cmd.status": "cameras, today's events, the digest",
    "cmd.live": "live video (coming in the Vidette app)",
    "cmd.quiet": "quiet for an hour; /quiet off to end it",
    "cmd.help": "what the bot can do",
    "cmd.tuning": "what your 👍/👎 changed, and undo",
    # calibration (vidette.calibration, ADR-0020)
    "cal.what": "{scenario} ({camera})",
    "cal.why_one": "{times} 👎 “{reason}”",
    "cal.why_mixed": "{times} 👎 ({reasons})",
    "cal.no_reason": "no reason",
    "cal.bar": "🎚 {what} now wakes you only when the judge is {bar} % sure or more: {why}.",
    "cal.digest": "🎚 {what} is now in the digest only: {why}.",
    "cal.restored": "🎚 {what} wakes you again as the policy says: you marked 👍 one it had "
    "kept in the digest.",
    "cal.undone": "↩️ Taken back: {what} now wakes you {how}.",
    "cal.how.written": "as the policy says",
    "cal.how.bar": "only from {bar} % sure",
    "cal.how.digest": "only in the digest",
    "cal.list_title": "🎚 <b>What your 👍/👎 changed</b>",
    "cal.list_item": "{n}. {what}: {how} since {since} ({why})",
    "cal.list_hint": "A numbered button takes that change back. Critical alerts never go to "
    "the digest.",
    "cal.list_empty": "🎚 Your 👍/👎 have not changed anything yet. After {marks} 👎 and no 👍 "
    "on one kind of alert from one camera, it wakes you less often; critical alerts never go "
    "to the digest.",
    "cal.off": "🎚 Calibration from 👍/👎 is off (calibration.enabled: false).",
    "status.judge": "Judge: {judge}. Today {judged} with a verdict, {unjudged} without",
    "status.by_camera": "<b>Today by camera</b>",
    "status.last": ", last {time}",
    "status.alerts": ", alerts {n}",
    "status.last_alert": "Last alert: {what}",
    "status.no_alert": "none",
    "status.quiet": "🔕 Quiet until {time}",
    "live": "📹 Live video will be right in the Vidette app, no other software. Until then: "
    "the latest events and the digest with /status.",
    "quiet.off": "🔔 Quiet is off: alerts arrive as usual.",
    "quiet.on": "🔕 Quiet until {time}. Critical alerts still come, the rest waits for the "
    "digest. To end it: /quiet off",
    "help": "I send alerts from the cameras and, in the evening, a summary of the day when "
    "there is something to look at.\n"
    "/status: cameras, today's events, the last alert and the digest\n"
    "/live: live video (coming in the Vidette app)\n"
    "/quiet: quiet for an hour (critical alerts still come), /quiet off: end it\n"
    "/tuning: what your 👍/👎 changed, and undo\n"
    "Under an alert: 👍 right, 👎 false alarm (I'll ask why), Who is it?, Not sure, "
    "📹 Other cameras. Tap an answer to take it back.\n"
    "You can just ask: “where else was this person?” or “send the other cameras” in reply "
    "to an alert, “what happened at the gate after 9?”, “digest”.",
    "judge.off": "off (backend: none)",
    "judge.line": "{backend} {model}; since start {judged}, old ones skipped {skipped}",
    "judge.error": "; error: {error}",
    "judge.triage": (
        "; pre-filter: {now} first in line, {later} only when idle ({expired} waited too "
        "long), {skip} not judged"
    ),
    # taps and replies
    "toast.gone": "This alert is no longer available",
    "toast.who": "Who is it?",
    "toast.why_false": "Why false? Choose a reason",
    "toast.reply_what": "Reply to the bot's message: what was it",
    "toast.face_who": "Face {n}: who is it?",
    "toast.reply_name": "Reply to the bot's message with a name",
    "toast.collecting": "Collecting video from the other cameras…",
    "toast.marked": "Marked: {what}",
    "toast.answer_undone": "Answer taken back",
    "toast.face_gone": "The event or the face is no longer available",
    "toast.person_gone": "The event, the face or the person is no longer available",
    "toast.failed": "Did not work: {error}",
    "toast.cal_undone": "Taken back",
    "toast.cal_gone": "Already taken back",
    "toast.decision_undone": "Decision taken back",
    "toast.no_alert_here": "this chat got no alert for this event; mark it in the web app",
    "prompt.what": "What was it? Reply to this message briefly.",
    "prompt.what_hint": "For example: the neighbours parking",
    "prompt.name": "What should I call {who} (face {face})? Reply to this message with a word "
    "or a phrase; “-” for no name.",
    "prompt.who_familiar": "this familiar person",
    "prompt.who_identity": "this identity",
    "prompt.hint_familiar": "Neighbour",
    "prompt.hint_name": "Name",
    "done": "Done: {what}",
    "ask.wait": "⏳ Looking at the recordings, the local model answers within a couple of minutes…",
    # the assistant
    "assistant.can_do": "I can: send video from every camera at the moment of an alert "
    "(“send the other cameras” in reply to an alert), find where else a person was seen by "
    "face (“where else was this person?”), find events (“what happened on the street after "
    "12:00?”, “today's alerts”), tell the details of an event and the day's digest, mark an "
    "alert right or false.",
    "assistant.off": "Free questions are off (assistant.backend: none). ",
    "assistant.model_failed": "The model did not answer ({error}). Try later. ",
    "assistant.all_cameras": "All cameras at that moment, in one video:",
    "assistant.time_approx": " (approximate time)",
    "assistant.no_video": "No video: only this camera recorded at that moment.",
    "assistant.face_ours_or_small": "The face here was recognized as one of us or is too "
    "small: nothing to compare.",
    "assistant.no_face": "No face was found in this event, so the person cannot be compared "
    "with other recordings. I do not compare people by clothing or height. I can send what "
    "the other cameras recorded at that moment.",
    "assistant.number": "#{id}",
    "assistant.first_seen": "{head}: no other sightings this week, this is the first.",
    "assistant.seen": "{head}: {total} in {days} in all; other sightings: {listed}{tail}.",
    "assistant.and_more": " and {n} more",
    "assistant.no_answer": "Found nothing to answer. ",
    "assistant.too_slow": "Did not manage to answer. ",
    "assistant.err.limit": "the daily limit of questions is spent",
    "assistant.err.timeout": "it ran out of time",
    "assistant.err.unreachable": "it is unreachable: {name}",
    "assistant.err.context": "the question and what was found did not fit the model",
    "assistant.err.unclear": "its answer made no sense",
    "tool.bad_time": "did not understand the time “{text}”: use HH:MM, YYYY-MM-DD HH:MM or -2h",
    "tool.unknown": "there is no tool {name}",
    "tool.no_event": "no event given: reply to an alert or give its id",
    "tool.event_missing": "there is no event {id}",
    "tool.identities_off": "identities by face are off (people.track_unknown)",
    "tool.identity_number": "an identity number is needed, for example 12",
    "tool.identity_missing": "there is no identity #{n}",
    "tool.mark_chat_only": "marking works only from the chat with the bot",
    "tool.answer_values": "answer: right, wrong or unsure",
    "tool.digest_chat_only": "the digest is only available in the chat with the bot",
}

RU: dict[str, str] = {
    "answer.right": "верно",
    "answer.wrong": "ложная",
    "answer.unsure": "не знаю",
    "answer.ours": "свой",
    "answer.marked": "отмечено",
    "kb.right": "👍 верно",
    "kb.wrong": "👎 ложная",
    "kb.unsure": "Не знаю",
    "kb.unsure_done": "🤷 не знаю",
    "kb.who": "Кто это?",
    "kb.no_reason": "Без причины",
    "kb.face": "Лицо {n}: {word}",
    "kb.back": "← Назад",
    "kb.ours": "Свой: {name}",
    "kb.familiar": "Знакомый: {name}",
    "kb.new_familiar": "Новый знакомый…",
    "kb.new_identity": "Новая личность…",
    "kb.stranger": "Чужой",
    "kb.undo_face": "Отменить решение",
    "kb.other_cameras": "📹 Другие камеры",
    "kb.cal_undo": "↩️ Отменить",
    "kb.cal_undo_n": "↩️ {n}",
    "face.small": "мелкое",
    "face.unknown": "незнакомое",
    "reason.passing.button": "Прошёл мимо",
    "reason.passing.short": "прошёл мимо",
    "reason.car_use.button": "Сел в машину, своя",
    "reason.car_use.short": "своя машина",
    "reason.ours.button": "Свой или знакомый",
    "reason.ours.short": "свои",
    "reason.animal.button": "Животное",
    "reason.animal.short": "животное",
    "reason.vehicle.button": "Проехала машина",
    "reason.vehicle.short": "машина",
    "reason.light.button": "Свет, тень, дождь",
    "reason.light.short": "свет, тень",
    "reason.stalled.button": "Запись зависла",
    "reason.stalled.short": "запись зависла",
    "reason.camera.button": "Не та камера",
    "reason.camera.short": "не та камера",
    "reason.unimportant.button": "Верно, но неважно",
    "reason.unimportant.short": "неважно",
    "reason.other.button": "Другое: напишу",
    "reason.other.short": "другое",
    "who.small_face": "лицо мелкое",
    "who.stranger": "незнакомец",
    "who.named": "{name} (№{id})",
    "who.suspicious": "⚠️ подозрительный №{id}",
    "who.stranger_n": "незнакомец №{id}",
    "who.small_suffix": " (лицо мелкое)",
    "who.first_time": "впервые",
    "who.nth_today": "{nth} раз за сегодня",
    "who.nth_over_days": "{nth} раз за {days}",
    "who.cameras_hour": " ({cameras} за час)",
    "who.joined": "{who}, {count}",
    "who.no_people": "людей не видно",
    "who.unchecked": "лица не проверялись",
    "who.ours_named": "свой: {names}",
    "who.ours_one": "свой",
    "who.ours_many": "свои",
    "who.familiar_named": "знакомый: {names}",
    "who.familiar_one": "знакомый",
    "who.familiar_many": "знакомые",
    "who.strangers": "незнакомцы: {n}",
    "who.strangers_list": "незнакомцы: {n} ({list})",
    "who.and": " и ",
    "who.no_face": "лицо не найдено",
    "who.raised": " · уровень поднят: приходит не первый раз",
    "alert.test": "🧪 <b>Проверка канала</b> (настоящий клип, настоящий судья)",
    "alert.title": "{icon} <b>Тревога: {level}</b>",
    "alert.not_alert": "🔔 <b>Событие, не тревога</b>",
    "alert.more_here": "Ещё на этой камере: {n}",
    "alert.upgrade": "⬆️ <b>Уровень выше: {level}</b>",
    "alert.no_description": "без описания",
    "alert.no_verdict": "Вердикта нет: {why}",
    "alert.judge_unsure": "Судья не уверен: {why}",
    "alert.label_failed": "метка в датасет не записана: {problem}",
    "alert.frames_failed": " Кадры не отправились.",
    "alert.late": "⏳ С опозданием: в {time} Telegram был недоступен.",
    "late.dropped_one": "⏳ Тревога в {time} не отправлена: Telegram слишком долго был "
    "недоступен. День целиком: /status.",
    "late.dropped_many": "⏳ {alerts} с {first} до {last} не отправлены: Telegram слишком "
    "долго был недоступен. День целиком: /status.",
    "note.early": "По первым секундам записи; весь ролик пришлю в это сообщение.",
    "note.clip_not_alert": "Уточнено по всему ролику: это не тревога.",
    "note.clip_unjudged": "Весь ролик судья не оценил; тревога по первым секундам.",
    "note.clip_refined": "Уточнено по всему ролику.",
    "video.too_big_whole": "🎞 Весь ролик {mb} МБ, больше лимита: ",
    "video.too_big": "🎞 Ролик {mb} МБ, больше лимита: ",
    "video.watch_home": "смотреть дома",
    "video.watch_app": "смотреть в Vidette",
    "why.no_verdict": "Без проверки это ушло бы в сводку: судья не вынес вердикт.",
    "why.not_alert": "Без проверки это ушло бы в сводку: судья не считает это тревогой.",
    "why.quiet": "Без проверки это ушло бы в сводку: включена тишина.",
    "why.known": "Без проверки это ушло бы в сводку: узнаны только свои.",
    "why.plain": "Без проверки это ушло бы в сводку.",
    "why.calibrated": "Без проверки это ушло бы в сводку: так настроили ваши 👍/👎 (/tuning).",
    "level.by_policy": "по политике",
    "level.none": "нет",
    "scenario.quiet": "тихо",
    "scenario.other": "другое",
    "mark.done": "✅ Отмечено: {word}",
    "mark.ours_person": "свой ({name})",
    "face.undone": "↩️ Решение по лицу отменено",
    "face.number": " (№{id})",
    "face.stranger": "чужой{number}",
    "face.unsure": "не знаю",
    "face.familiar_named": "знакомый «{name}»{number}",
    "face.familiar": "знакомый{number}",
    "face.suspicious": "подозрительный{number}",
    "face.identity": "личность №{id}",
    "face.identity_named": " «{name}»",
    "face.ours": "свой",
    "face.ours_named": "свой: {name}",
    "span.seconds": "{n} с",
    "latency.title": "⏱ <b>Где ушло время</b>",
    "latency.from_person": "От человека в кадре до тревоги: {span}",
    "latency.from_frame": "От первого кадра до тревоги: {span}",
    "stage.requested": "камера проснулась",
    "stage.motion": "движение",
    "stage.first_frame": "первый кадр",
    "stage.person_seen": "человек в кадре",
    "stage.early": "первые секунды записаны",
    "stage.early_picked_up": "сервер взял их",
    "stage.early_judge_started": "судья начал",
    "stage.early_judge_finished": "судья ответил",
    "stage.closed": "ролик готов",
    "stage.picked_up": "сервер взял ролик",
    "stage.judge_started": "судья начал",
    "stage.judge_finished": "судья ответил",
    "stage.sent": "тревога ушла",
    "stream.interrupted": "Запись прерывалась",
    "stream.up_to": "до {n} с",
    "place.left": "слева",
    "place.right": "справа",
    "place.top": "сверху",
    "place.bottom": "снизу",
    "place.centre": "по центру",
    "mosaic.title": "📹 <b>Все камеры, {start}–{end}</b>",
    "mosaic.place": "{place}: {name}",
    "mosaic.approximate": "{names}: время примерное, эта камера отдаёт запись с задержкой, "
    "её картинка может отставать от остальных",
    "mosaic.none": "📹 Около {time} другие камеры ничего не записали.",
    "mosaic.failed": "Склеить видео не получилось: {error}",
    "mosaic.part": "Часть {n} из {total}",
    "linked.around": "около {span}",
    "linked.this_event": "это событие",
    "linked.same_person": "тот же человек по лицу",
    "linked.person_no_face": "человек в кадре, но лицо не сравнить",
    "linked.nothing": "{names}: в это время ничего не записано (камера спала или движения не было)",
    # происшествие на нескольких камерах (ADR-0022)
    "incident.face": "🔗 Тот же человек (по лицу): {camera}, {time}",
    "incident.look": "🔗 Похоже, тот же человек (одежда и время): {camera}, {time}",
    "incident.time": "🔗 В это же время человек: {camera}, {time}",
    "incident.missed": " (судья это пропустил)",
    "incident.approx": "около {time}",
    "incident.digest": "🔗 Вместе с ним: {parts}",
    "event.deleted": "Событие уже удалено.",
    "event.deleted_short": "Событие удалено",
    "months": "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря",
    "digest.evening": "🌙 <b>Итог дня, {day} {month}</b>",
    "digest.so_far": "📋 <b>Сегодня к {time}</b>",
    "digest.false_n": "ложных {n}",
    "digest.right_n": "верных {n}",
    "digest.unanswered_n": "без ответа {n}",
    "digest.by_marks": " По вашим отметкам: {list}.",
    "digest.alerts": "Тревог: {n}.{marks}",
    "digest.no_alerts": "Тревог не было.",
    "digest.common_false": "Ложные чаще всего: {list}.",
    "digest.waiting_line": "{waiting} ответа, последняя в {time} ({camera}). Кнопки под тревогой.",
    "digest.look": "Без тревоги, но стоит взглянуть:",
    "digest.and_more": "…и ещё {n}",
    "digest.total": "Всего за день {clips}",
    "digest.unjudged": "Не проверено судьёй: {n} ({why}).",
    "digest.deep": "Перепроверка: {clips}; {disputed} на разметку.",
    "digest.second_look": " (перепроверка)",
    "digest.calibrated": " · в сводке по вашим отметкам",
    "n.days": "день|дня|дней",
    "n.clips": "ролик|ролика|роликов",
    "n.events": "событие|события|событий",
    "n.pauses": "пауза|паузы|пауз",
    "n.waiting": "тревога ждёт|тревоги ждут|тревог ждут",
    "n.disputed": "спорный ушёл|спорных ушли|спорных ушли",
    "n.times": "раз|раза|раз",
    "n.alerts": "тревога|тревоги|тревог",
    "unjudged.queue": "не дошла очередь",
    "unjudged.none": "нет вердикта",
    "unjudged.context": "кадры не помещаются в модель судьи",
    "unjudged.timeout": "судья не успел",
    "unjudged.network": "судья недоступен",
    "unjudged.budget": "бюджет судьи исчерпан",
    "unjudged.auth": "судья не принял ключ",
    "unjudged.rate_limited": "судья занят",
    "unjudged.server": "ошибка судьи",
    "unjudged.not_found": "модель не найдена",
    "unjudged.bad_answer": "непонятный ответ судьи",
    "unjudged.unsure": "судья не уверен",
    "unjudged.no_frames": "нет кадров",
    "cmd.status": "камеры, события за сегодня, сводка",
    "cmd.live": "живое видео (будет в приложении Vidette)",
    "cmd.quiet": "тишина на час; /quiet off снять",
    "cmd.help": "что умеет бот",
    "cmd.tuning": "что изменили ваши 👍/👎, и отмена",
    "cal.what": "{scenario} ({camera})",
    "cal.why_one": "{times} 👎 «{reason}»",
    "cal.why_mixed": "{times} 👎 ({reasons})",
    "cal.no_reason": "без причины",
    "cal.bar": "🎚 {what} теперь будит, только если судья уверен на {bar} % и больше: {why}.",
    "cal.digest": "🎚 {what} теперь только в сводке: {why}.",
    "cal.restored": "🎚 {what} снова будит, как в правилах: вы отметили 👍 то, что было "
    "только в сводке.",
    "cal.undone": "↩️ Отменено: {what} теперь будит {how}.",
    "cal.how.written": "как в правилах",
    "cal.how.bar": "только от {bar} % уверенности",
    "cal.how.digest": "только в сводке",
    "cal.list_title": "🎚 <b>Что изменили ваши 👍/👎</b>",
    "cal.list_item": "{n}. {what}: {how} с {since} ({why})",
    "cal.list_hint": "Кнопка с номером отменяет изменение. Критичное никогда не уходит в сводку.",
    "cal.list_empty": "🎚 Ваши 👍/👎 пока ничего не изменили. После {marks} 👎 без единого 👍 "
    "на одном виде тревог одной камеры он будит реже; критичное никогда не уходит в сводку.",
    "cal.off": "🎚 Настройка по 👍/👎 выключена (calibration.enabled: false).",
    "status.judge": "Судья: {judge}. Сегодня вердиктов {judged}, без вердикта {unjudged}",
    "status.by_camera": "<b>Сегодня по камерам</b>",
    "status.last": ", последнее {time}",
    "status.alerts": ", тревог {n}",
    "status.last_alert": "Последняя тревога: {what}",
    "status.no_alert": "не было",
    "status.quiet": "🔕 Тишина до {time}",
    "live": "📹 Живое видео будет прямо в приложении Vidette, без отдельных программ. Пока его "
    "нет: последние события и сводка по команде /status.",
    "quiet.off": "🔔 Тишина выключена: тревоги приходят как обычно.",
    "quiet.on": "🔕 Тишина до {time}. Критичные тревоги всё равно придут, остальное копится "
    "в сводку. Снять: /quiet off",
    "help": "Я присылаю тревоги с камер и вечером итог дня, если есть что посмотреть.\n"
    "/status: камеры, события за сегодня, последняя тревога и сводка\n"
    "/live: живое видео (будет в приложении Vidette)\n"
    "/quiet: тишина на час (критичное всё равно придёт), /quiet off: снять\n"
    "/tuning: что изменили ваши 👍/👎, и отмена\n"
    "Под тревогой: 👍 верно, 👎 ложная (спрошу почему), Кто это?, Не знаю, 📹 Другие камеры. "
    "Ответ можно отменить, нажав на него.\n"
    "Можно просто спросить: «где ещё был этот человек?» или «пришли другие камеры» в ответ "
    "на тревогу, «что было у калитки после 9?», «сводка».",
    "judge.off": "выключен (backend: none)",
    "judge.line": "{backend} {model}; с запуска {judged}, пропущено старых {skipped}",
    "judge.error": "; ошибка: {error}",
    "judge.triage": (
        "; предфильтр: {now} в первую очередь, {later} только в свободное время "
        "({expired} не дождались), {skip} не отправлены судье"
    ),
    "toast.gone": "Эта тревога уже недоступна",
    "toast.who": "Кто это?",
    "toast.why_false": "Почему ложная? Выберите причину",
    "toast.reply_what": "Ответьте на сообщение бота: что это было",
    "toast.face_who": "Лицо {n}: кто это?",
    "toast.reply_name": "Ответьте именем на сообщение бота",
    "toast.collecting": "Собираю видео с других камер…",
    "toast.marked": "Отмечено: {what}",
    "toast.answer_undone": "Ответ отменён",
    "toast.face_gone": "Событие или лицо уже недоступно",
    "toast.person_gone": "Событие, лицо или человек уже недоступны",
    "toast.failed": "Не вышло: {error}",
    "toast.cal_undone": "Отменено",
    "toast.cal_gone": "Уже отменено",
    "toast.decision_undone": "Решение отменено",
    "toast.no_alert_here": "по этому событию в этом чате не было тревоги; отметьте его в "
    "веб-приложении",
    "prompt.what": "Что это было? Ответьте на это сообщение коротко.",
    "prompt.what_hint": "Например: соседи паркуются",
    "prompt.name": "Как назвать {who} (лицо {face})? Ответьте на это сообщение одним словом "
    "или фразой; «-» без имени.",
    "prompt.who_familiar": "знакомого",
    "prompt.who_identity": "эту личность",
    "prompt.hint_familiar": "Соседка",
    "prompt.hint_name": "Имя",
    "done": "Готово: {what}",
    "ask.wait": "⏳ Смотрю записи, локальная модель отвечает до пары минут…",
    "assistant.can_do": "Я умею: присылать видео со всех камер в момент тревоги («пришли "
    "другие камеры» в ответ на тревогу), искать, где ещё был человек по лицу («где ещё был "
    "этот человек?»), находить события («что было на улице после 12:00?», «тревоги за "
    "сегодня»), рассказывать подробности события и сводку дня, отмечать тревогу верной или "
    "ложной.",
    "assistant.off": "Свободные вопросы выключены (assistant.backend: none). ",
    "assistant.model_failed": "Модель не ответила ({error}). Попробуйте позже. ",
    "assistant.all_cameras": "Все камеры в этот момент, одним видео:",
    "assistant.time_approx": " (время примерное)",
    "assistant.no_video": "Видео нет: в этот момент записала только эта камера.",
    "assistant.face_ours_or_small": "Лицо здесь распознано как своё или слишком мелкое: "
    "сравнивать не с чем.",
    "assistant.no_face": "В этом событии лицо не найдено, поэтому сравнить человека с другими "
    "записями нельзя. По одежде или росту я людей не сравниваю. Могу прислать, что записали "
    "другие камеры в этот момент.",
    "assistant.number": "№{id}",
    "assistant.first_seen": "{head}: других встреч за неделю нет, это первая.",
    "assistant.seen": "{head}: всего {total} за {days}; другие встречи: {listed}{tail}.",
    "assistant.and_more": " и ещё {n}",
    "assistant.no_answer": "Не нашёл, что ответить. ",
    "assistant.too_slow": "Не успел ответить. ",
    "assistant.err.limit": "дневной лимит вопросов исчерпан",
    "assistant.err.timeout": "не уложилась во время",
    "assistant.err.unreachable": "недоступна: {name}",
    "assistant.err.context": "вопрос и найденное не поместились в модель",
    "assistant.err.unclear": "непонятный ответ",
    "tool.bad_time": "не понял время «{text}»: нужно HH:MM, YYYY-MM-DD HH:MM или -2h",
    "tool.unknown": "нет инструмента {name}",
    "tool.no_event": "событие не указано: ответьте на сообщение с тревогой или дайте id",
    "tool.event_missing": "нет события {id}",
    "tool.identities_off": "личности по лицам выключены (people.track_unknown)",
    "tool.identity_number": "нужен номер личности, например 12",
    "tool.identity_missing": "нет личности №{n}",
    "tool.mark_chat_only": "отмечать можно только из чата с ботом",
    "tool.answer_values": "answer: right, wrong или unsure",
    "tool.digest_chat_only": "сводка доступна только в чате с ботом",
}

CATALOGS: dict[str, dict[str, str]] = {"en": EN, "ru": RU}
_current: list[str] = ["en"]


def use(language: str) -> None:
    """Speak `language` from now on (the bot's setting; English for anything unknown)."""
    _current[0] = language if language in CATALOGS else "en"


def language() -> str:
    return _current[0]


def model_language(code: str | None = None) -> str:
    """The language's English name, for prompts: "Answer in Russian"."""
    return MODEL_LANGUAGE.get(code or language(), "English")


def t(key: str, **values: object) -> str:
    """The phrase for `key` in the current language, `{placeholders}` filled in."""
    template = CATALOGS[language()].get(key)
    if template is None:
        template = EN[key]  # a KeyError here is a missing key: the catalog test finds it
    return template.format(**values) if values else template


def forms(key: str) -> list[str]:
    return t(key).split("|")


def n(key: str, count: int) -> str:
    """`count` with its noun: "1 clip", "3 clips"; «1 ролик», «3 ролика», «5 роликов»."""
    words = forms(key)
    if language() == "ru" and len(words) == 3:
        m = abs(count) % 100
        if 10 < m < 20:
            word = words[2]
        else:
            m %= 10
            word = words[0] if m == 1 else words[1] if 2 <= m <= 4 else words[2]
    else:
        word = words[0] if abs(count) == 1 else words[-1]
    return f"{count} {word}"


def ordinal(count: int) -> str:
    """ "3rd" in English, «3-й» in Russian."""
    if language() == "ru":
        return f"{count}-й"
    teen = 10 <= count % 100 <= 20
    suffix = "th" if teen else {1: "st", 2: "nd", 3: "rd"}.get(count % 10, "th")
    return f"{count}{suffix}"


def month(index: int) -> str:
    """Month 1..12 as a digest's date uses it (genitive in Russian)."""
    return forms("months")[index - 1]
