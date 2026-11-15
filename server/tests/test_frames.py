"""Which frames the judge sees: where the picture changed, two per episode."""

from __future__ import annotations

import pytest

from vidette.verdicts.frames import activity, centred_indices, choose, episodes


def test_activity_finds_the_changed_thumbnail_and_ignores_a_light_change() -> None:
    background = bytes([50]) * 100
    brighter = bytes([90]) * 100  # the whole picture lit up: no motion
    someone = bytearray(background)
    someone[10:20] = bytes([200]) * 10  # a tenth of the picture changed
    scores = activity([background, background, brighter, bytes(someone), background])
    assert scores[0] == scores[1] == scores[2] == 0
    assert scores[3] == pytest.approx(0.1)


def test_choose_gives_each_episode_two_frames_longest_first() -> None:
    # candidates every 15 frames; a long episode (5 candidates), a short one (1), quiet
    candidates = list(range(0, 300, 15))
    scores = [0.0] * len(candidates)
    for i in (3, 4, 5, 6, 7):
        scores[i] = 0.02
    scores[12] = 0.6  # a car's headlights: big but brief
    runs = episodes(300, candidates, scores)
    assert [(len(frames), peak) for frames, peak in runs] == [(75, 0.02), (15, 0.6)]
    chosen, method = choose(300, candidates, scores, 4)
    assert method == "activity"
    assert len(chosen) == 4 and chosen == sorted(chosen)
    assert sum(45 <= i < 120 for i in chosen) == 2 and sum(180 <= i < 195 for i in chosen) == 2
    # two frames for three episodes: only the longest is shown
    scores[16] = 0.01
    chosen, _ = choose(300, candidates, scores, 2)
    assert all(45 <= i < 120 for i in chosen)


def test_choose_spreads_over_the_clip_when_nothing_moved() -> None:
    chosen, method = choose(100, [0, 50], [0.0, 0.001], 4)
    assert method == "spread" and chosen == centred_indices(100, 4) == [12, 37, 62, 87]


def test_the_judge_is_told_frame_times_and_the_stall_between_them() -> None:
    from pathlib import Path

    from vidette.streamhealth import measure
    from vidette.verdicts.base import FrameSet
    from vidette.verdicts.vlm import timing_text

    times = [*[i / 15 for i in range(54)], *[10.93 + i / 15 for i in range(150)]]
    frames = FrameSet(
        [Path(f"{n}.jpg") for n in (1, 2, 3, 4)], [1.0, 3.5, 11.0, 12.5], "activity", 21.0,
        measure(times, 15.0),
    )  # fmt: skip
    text = timing_text(frames)
    assert text is not None
    assert "1: 1.0 s, 2: 3.5 s, 3: 11.0 s, 4: 12.5 s (picked where the picture changed)" in text
    assert "stalled between frames 2 and 3 (3.5 to 10.9 s)" in text
    assert "interrupted: 1 stall(s), up to 7.4 s" in text
    assert timing_text(FrameSet([Path("a.jpg")])) is None  # images without times
