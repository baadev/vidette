from __future__ import annotations

from pathlib import Path

from vidette.recording.segments import (
    build_record_command,
    camera_media_dir,
    parse_segment_list_line,
    segment_hour_dir,
)


def test_build_record_command_shape() -> None:
    cmd = build_record_command("rtsp://gw:8554/cam", Path("/media/cam"), 10)
    assert cmd == [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        "-i",
        "rtsp://gw:8554/cam",
        "-c",
        "copy",
        "-map",
        "0",
        "-f",
        "segment",
        "-segment_time",
        "10",
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-strftime_mkdir",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+faststart",
        "-segment_list",
        "pipe:1",
        "-segment_list_type",
        "csv",
        "/media/cam/%Y/%m/%d/%H/%s.mp4",
    ]


def test_build_record_command_custom_input_args() -> None:
    cmd = build_record_command(
        "/tmp/clip.mp4", Path("/m/c"), 2, input_args=("-re", "-stream_loop", "-1")
    )
    i = cmd.index("-i")
    expected = ["-nostdin", "-hide_banner", "-loglevel", "warning", "-re", "-stream_loop", "-1"]
    assert cmd[1:i] == expected
    assert "-rtsp_transport" not in cmd


def _make_segment_file(camera_dir: Path, epoch: int, size: int = 64) -> Path:
    hour_dir = segment_hour_dir(camera_dir, epoch)
    hour_dir.mkdir(parents=True, exist_ok=True)
    path = hour_dir / f"{epoch}.mp4"
    path.write_bytes(b"\0" * size)
    return path


def test_parse_segment_list_line_basename(tmp_path: Path) -> None:
    camera_dir = camera_media_dir(tmp_path, "cam")
    epoch = 1_783_430_183
    path = _make_segment_file(camera_dir, epoch, size=128)

    notice = parse_segment_list_line(f"{epoch}.mp4,0.000000,10.000000\n", camera_dir)
    assert notice is not None
    assert notice.path == path
    assert notice.start_ts == float(epoch)
    assert notice.end_ts == epoch + 10.0
    assert notice.size_bytes == 128


def test_parse_segment_list_line_duration_from_csv(tmp_path: Path) -> None:
    camera_dir = camera_media_dir(tmp_path, "cam")
    epoch = 1_783_430_193
    _make_segment_file(camera_dir, epoch)
    notice = parse_segment_list_line(f"{epoch}.mp4,10.000000,13.500000", camera_dir)
    assert notice is not None
    assert notice.end_ts - notice.start_ts == 3.5


def test_parse_segment_list_line_rejects_garbage(tmp_path: Path) -> None:
    camera_dir = camera_media_dir(tmp_path, "cam")
    epoch = 1_783_430_203
    _make_segment_file(camera_dir, epoch)
    assert parse_segment_list_line("", camera_dir) is None
    assert parse_segment_list_line("   \n", camera_dir) is None
    assert parse_segment_list_line(f"{epoch}.mp4,0.0", camera_dir) is None  # 2 fields
    assert parse_segment_list_line(f"{epoch}.mp4,a,b", camera_dir) is None  # non-numeric
    assert parse_segment_list_line(f"{epoch}.mp4,5.0,1.0", camera_dir) is None  # negative dur
    assert parse_segment_list_line("notanepoch.mp4,0.0,10.0", camera_dir) is None
    assert parse_segment_list_line("999999.mp4,0.0,10.0", camera_dir) is None  # missing file
    assert parse_segment_list_line(f"{epoch}.mp4,nan,inf", camera_dir) is None


def test_segment_hour_dir_is_zero_padded(tmp_path: Path) -> None:
    hour_dir = segment_hour_dir(tmp_path, 0.0)  # epoch 0 → 1970-01-01
    parts = hour_dir.relative_to(tmp_path).parts
    assert parts[0] == "1970"
    assert all(len(part) == 2 for part in parts[1:])
