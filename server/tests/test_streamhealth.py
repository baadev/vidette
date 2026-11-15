"""Stream health from packet times."""

from __future__ import annotations

import pytest

from vidette.streamhealth import measure


def even(start: float, end: float, fps: float = 15.0) -> list[float]:
    count = round((end - start) * fps)
    return [round(start + i / fps, 4) for i in range(count)]


def test_an_even_stream_is_healthy() -> None:
    health = measure(even(0, 10), 15.0)
    assert health.frames == 150 and health.stalls == 0 and health.missing_s == 0
    assert health.burst_share == 0 and not health.interrupted
    assert health.fps == pytest.approx(15.0, abs=0.1)


def test_a_stall_then_a_burst_is_measured_and_listed() -> None:
    # frames until 3.6 s, nothing until 10.9 s, then the backlog 10 ms apart
    times = [*even(0, 3.6), *[10.9 + i * 0.01 for i in range(60)], *even(11.5, 20)]
    health = measure(times, 15.0)
    assert health.interrupted and health.stalls >= 1
    assert health.longest_stall_s == pytest.approx(7.3, abs=0.1)
    assert health.longest_at_s == pytest.approx(3.53, abs=0.1)
    assert health.gaps[0][0] == pytest.approx(3.53, abs=0.1)
    assert health.gaps[0][1] == pytest.approx(10.9, abs=0.01)
    assert health.burst_share > 0.15  # the backlog arrived in a heap
    assert health.gaps_between(3.0, 11.0) == health.gaps[:1]
    assert health.gaps_between(12.0, 19.0) == []


def test_small_pauses_covering_a_tenth_count_as_interrupted() -> None:
    times: list[float] = []
    for second in range(20):  # 0.6 s of every second missing: 12 s of 20
        times += even(second, second + 0.4)
    health = measure(times, 15.0)
    assert health.longest_stall_s < 2 and health.interrupted
    assert health.gaps == []  # none of them is a second long


def test_one_frame_is_no_stream() -> None:
    assert measure([1.0]).frames == 1 and not measure([1.0]).interrupted
