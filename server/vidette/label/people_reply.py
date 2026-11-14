"""Who a face is, answered in the same reply as the scenarios: "5 Robin, 7.2 чужой".

- `N Name` (a person enrolled in the people folder): the face in entry N is that person;
- `N stranger` (чужой, незнакомец, …): it is nobody enrolled;
- `N.K …` picks face K of entry N when the clip shows several (the sheet numbers them);
  `N.K ?` (не знаю) records "unsure".

Without a face number, only names and stranger words count as answers about faces, so
"5 ?" still means "leave the scenario unlabelled" as before. Everything else in the reply
is handed to the scenario parser unchanged, and nothing is written unless both parts
make sense.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from vidette.eval.dataset import VIDEO_SUFFIXES, Labels
from vidette.people.cli import answer_word
from vidette.people.report import UNSURE, PeopleReport
from vidette.people.sidecar import load_report
from vidette.people.store import PeopleStore

_NUMBER_RE = re.compile(r"^#?(\d+)(?:[.:](\d+))?[-:.)]?$")


@dataclass(frozen=True)
class FaceAnswer:
    number: int  # the entry's review number
    face: int | None  # K in "N.K"; None: the only face
    answer: str  # a person id, "stranger" or "unsure"
    raw: str


@dataclass(frozen=True)
class FaceTarget:
    answer: FaceAnswer
    entry_id: str
    clip: Path
    report: PeopleReport
    face: int


def split_reply(text: str, store: PeopleStore | None) -> tuple[list[FaceAnswer], str]:
    """(answers about faces, the rest of the reply for the scenario parser)."""
    answers: list[FaceAnswer] = []
    rest: list[str] = []
    for segment in re.split(r"[,;\n]+", text):
        groups: list[tuple[str, int | None, int | None, list[str]]] = []
        for token in segment.split():
            match = _NUMBER_RE.match(token)
            if match:
                face = int(match.group(2)) if match.group(2) else None
                groups.append((token, int(match.group(1)), face, []))
            elif groups:
                groups[-1][3].append(token)
            else:
                groups.append(("", None, None, [token]))
        for head, number, face, tokens in groups:
            phrase = " ".join(tokens).strip(" .!")
            word = answer_word(phrase, store) if store is not None and phrase else None
            if number is not None and word is not None and (face is not None or word != UNSURE):
                answers.append(
                    FaceAnswer(number=number, face=face, answer=word, raw=f"{head} {phrase}")
                )
            else:
                rest.append(" ".join(part for part in (head, phrase) if part))
    return answers, ", ".join(part for part in rest if part)


def resolve_faces(
    labels: Labels, root: Path, answers: list[FaceAnswer]
) -> tuple[list[FaceTarget], list[str]]:
    """(targets, problems): which face of which clip each answer is about."""
    by_number = {entry.number: entry for entry in labels.entries if entry.number is not None}
    targets: list[FaceTarget] = []
    problems: list[str] = []
    for answer in answers:
        entry = by_number.get(answer.number)
        if entry is None:
            problems.append(f"{answer.raw}: no entry has number {answer.number}")
            continue
        clip = next(
            (root / item for item in entry.media if Path(item).suffix.lower() in VIDEO_SUFFIXES),
            None,
        )
        report = None if clip is None else load_report(clip)
        if clip is None or report is None:
            problems.append(
                f"{answer.raw}: #{answer.number} has no face check yet (vidette people scan {root})"
            )
            continue
        if answer.face is not None:
            face = answer.face
            if report.get(face) is None:
                problems.append(f"{answer.raw}: #{answer.number} has no face {face}")
                continue
        elif len(report.seen) == 1:
            face = report.seen[0].n
        elif not report.seen:
            problems.append(f"{answer.raw}: no readable face was found in #{answer.number}")
            continue
        else:
            numbers = ", ".join(f"{answer.number}.{s.n}" for s in report.seen)
            problems.append(
                f"{answer.raw}: #{answer.number} shows several faces; say which: {numbers}"
            )
            continue
        targets.append(FaceTarget(answer, entry.id, clip, report, face))
    return targets, problems
