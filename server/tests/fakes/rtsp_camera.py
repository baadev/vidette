"""A fake battery camera that speaks just enough RTSP for ffmpeg, on 127.0.0.1.

It behaves like a battery camera's NAS (RTSP) endpoint as observed in v0.1 (tag
`nvr-core`, docs/cameras/eufy.md):

- **asleep**: the port is closed, a connection attempt is refused;
- **awake** for a while after "motion": OPTIONS/DESCRIBE/SETUP/PLAY over TCP-interleaved
  RTP, H.264 at 10 fps, then the camera falls asleep again and drops the connection;
- **one client only**: a second client gets `404 Stream Not Found` on DESCRIBE;
- **ghost sessions**: after a client vanishes without TEARDOWN, every DESCRIBE gets the
  same 404 for `ghost_s` seconds;
- optional **silent** mode (session accepted, no media) and **Basic auth** (401).

Media is real: an H.264 Annex-B stream encoded once by ffmpeg (lavfi testsrc2), split
into access units and packetized per RFC 6184 (single NAL units and FU-A), so the
collector's ffmpeg records a real, decodable clip.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import random
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_MTU_PAYLOAD = 1300
_FPS = 10


def encode_test_stream(path: Path, seconds: int = 4) -> Path:
    """H.264 Annex-B, 320x240 @ 10 fps, an IDR (with SPS/PPS) every second, AUD per frame."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for the fake camera")
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={_FPS}",
            "-t", str(seconds),
            "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            "-g", str(_FPS), "-bf", "0",
            "-x264-params", "aud=1:repeat-headers=1",
            "-f", "h264", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def encode_street_stream(
    path: Path, *, seconds: int = 12, motion: tuple[float, float] = (4.0, 7.0)
) -> Path:
    """Like `encode_test_stream`, but a still scene for a continuous camera: grey, a white
    box crossing it between `motion` seconds, and a "clock" (a small box blinking once a
    second) in the top-right corner where cameras burn in the date and time."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for the fake camera")
    first, last = motion
    speed = 360 / max(0.1, last - first)
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=0x505050:s=320x240:r={_FPS}",
            "-f", "lavfi", "-i", f"color=c=white:s=40x40:r={_FPS}",
            "-f", "lavfi", "-i", f"color=c=white:s=48x12:r={_FPS}",
            "-filter_complex",
            f"[0][1]overlay=x='if(between(t,{first},{last}),(t-{first})*{speed:.1f}-40,-100)'"
            f":y=110:eval=frame[a];"
            "[a][2]overlay=x=264:y=6:enable='lt(mod(t,2),1)'",
            "-t", str(seconds),
            "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            "-g", str(_FPS), "-bf", "0",
            "-x264-params", "aud=1:repeat-headers=1",
            "-f", "h264", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _split_nals(data: bytes) -> list[bytes]:
    nals: list[bytes] = []
    i, n = 0, len(data)
    starts: list[int] = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for index, start in enumerate(starts):
        end = starts[index + 1] - 3 if index + 1 < len(starts) else n
        nal = data[start:end].rstrip(b"\x00")
        if nal:
            nals.append(nal)
    return nals


def access_units(data: bytes) -> list[list[bytes]]:
    """Group NAL units into frames, splitting on access unit delimiters (type 9)."""
    units: list[list[bytes]] = []
    current: list[bytes] = []
    for nal in _split_nals(data):
        if nal[0] & 0x1F == 9:
            if current:
                units.append(current)
            current = []
            continue
        current.append(nal)
    if current:
        units.append(current)
    return units


def _parameter_sets(units: list[list[bytes]]) -> tuple[bytes, bytes]:
    sps = pps = b""
    for unit in units:
        for nal in unit:
            if nal[0] & 0x1F == 7 and not sps:
                sps = nal
            if nal[0] & 0x1F == 8 and not pps:
                pps = nal
    if not sps or not pps:
        raise RuntimeError("test stream has no SPS/PPS")
    return sps, pps


def _rtp_payloads(nal: bytes) -> list[bytes]:
    if len(nal) <= _MTU_PAYLOAD:
        return [nal]
    header, body = nal[0], nal[1:]
    indicator = (header & 0xE0) | 28  # FU-A
    kind = header & 0x1F
    chunks = [body[i : i + _MTU_PAYLOAD] for i in range(0, len(body), _MTU_PAYLOAD)]
    out = []
    for index, chunk in enumerate(chunks):
        fu_header = kind
        if index == 0:
            fu_header |= 0x80
        if index == len(chunks) - 1:
            fu_header |= 0x40
        out.append(bytes([indicator, fu_header]) + chunk)
    return out


@dataclass
class CameraStats:
    connections: int = 0
    describes: int = 0
    plays: int = 0
    teardowns: int = 0
    rejected_busy: int = 0
    rejected_auth: int = 0
    unclean_disconnects: int = 0
    max_concurrent_sessions: int = 0
    frames_sent: int = 0
    auth_headers: list[str] = field(default_factory=list)


class FakeRtspCamera:
    def __init__(
        self,
        stream: Path,
        *,
        path: str = "live0",
        credentials: tuple[str, str] | None = None,
        ghost_s: float = 0.0,
        silent: bool = False,
    ) -> None:
        self._units = access_units(stream.read_bytes())
        self._sps, self._pps = _parameter_sets(self._units)
        self.path = path
        self.credentials = credentials
        self.ghost_s = ghost_s
        self.silent = silent
        self.port = 0
        self.stats = CameraStats()
        self._server: asyncio.Server | None = None
        self._awake_until = 0.0
        self._sessions: set[asyncio.StreamWriter] = set()
        self._ghost_until = 0.0
        self._sleep_task: asyncio.Task[None] | None = None
        self._busy_forever = False

    # --- control ------------------------------------------------------------------------

    async def reserve_port(self) -> int:
        """Pick a free port while staying asleep (closed), so URLs can be built up front."""
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        self.port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        return self.port

    def url(self, user: str | None = None, password: str | None = None) -> str:
        auth = f"{user}:{password}@" if user is not None else ""
        return f"rtsp://{auth}127.0.0.1:{self.port}/{self.path}"

    async def wake(self, seconds: float) -> None:
        """Motion: open the port and stream to whoever connects, then sleep again."""
        self._awake_until = time.monotonic() + seconds
        if self._server is None:
            if self.port == 0:
                await self.reserve_port()
            self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        if self._sleep_task is not None:
            self._sleep_task.cancel()
        self._sleep_task = asyncio.create_task(self._fall_asleep_after(seconds))

    def make_busy(self, busy: bool = True) -> None:
        """Answer every DESCRIBE with 404 Stream Not Found (another client holds it, or a
        base station answering for a camera that sleeps); `False` serves again."""
        self._busy_forever = busy

    async def sleep(self) -> None:
        """Close the port and drop every session, like the camera powering its radio down."""
        self._awake_until = 0.0
        if self._server is not None:
            self._server.close()
            for writer in list(self._sessions):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            self._server = None

    async def _fall_asleep_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self.sleep()

    # --- protocol -----------------------------------------------------------------------

    def _sdp(self) -> bytes:
        sprop = ",".join(base64.b64encode(nal).decode() for nal in (self._sps, self._pps))
        profile = self._sps[1:4].hex()
        return (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=fake battery camera\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "t=0 0\r\n"
            "a=control:*\r\n"
            "m=video 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 packetization-mode=1;profile-level-id={profile};"
            f"sprop-parameter-sets={sprop}\r\n"
            "a=control:trackID=0\r\n"
        ).encode()

    def _authorized(self, headers: dict[str, str]) -> bool:
        if self.credentials is None:
            return True
        header = headers.get("authorization", "")
        self.stats.auth_headers.append(header)
        expected = base64.b64encode(":".join(self.credentials).encode()).decode()
        return header == f"Basic {expected}"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.stats.connections += 1
        session_id = str(random.randint(10_000_000, 99_999_999))
        streaming: asyncio.Task[None] | None = None
        playing = False
        tore_down = False

        def respond(cseq: str, status: str, extra: list[str], body: bytes = b"") -> None:
            lines = [f"RTSP/1.0 {status}", f"CSeq: {cseq}", *extra]
            if body:
                lines.append(f"Content-Length: {len(body)}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":  # interleaved RTCP from the client: skip it
                    header = await reader.readexactly(3)
                    (length,) = struct.unpack(">H", header[1:3])
                    await reader.readexactly(length)
                    continue
                raw = first + await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("latin-1").split("\r\n")
                method = lines[0].split(" ", 1)[0]
                headers = {
                    key.strip().lower(): value.strip()
                    for key, _, value in (line.partition(":") for line in lines[1:] if line)
                }
                if int(headers.get("content-length", "0")):
                    await reader.readexactly(int(headers["content-length"]))
                cseq = headers.get("cseq", "0")
                if method == "OPTIONS":
                    respond(cseq, "200 OK", ["Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"])
                elif method == "DESCRIBE":
                    self.stats.describes += 1
                    if not self._authorized(headers):
                        self.stats.rejected_auth += 1
                        respond(cseq, "401 Unauthorized", ['WWW-Authenticate: Basic realm="cam"'])
                    elif (
                        self._busy_forever
                        or time.monotonic() < self._ghost_until
                        or any(s is not writer for s in self._sessions)
                    ):
                        self.stats.rejected_busy += 1
                        respond(cseq, "404 Stream Not Found", [])
                    else:
                        base = f"rtsp://127.0.0.1:{self.port}/{self.path}/"
                        respond(
                            cseq,
                            "200 OK",
                            ["Content-Type: application/sdp", f"Content-Base: {base}"],
                            self._sdp(),
                        )
                elif method == "SETUP":
                    self._sessions.add(writer)
                    self.stats.max_concurrent_sessions = max(
                        self.stats.max_concurrent_sessions, len(self._sessions)
                    )
                    respond(
                        cseq,
                        "200 OK",
                        [
                            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
                            f"Session: {session_id};timeout=60",
                        ],
                    )
                elif method == "PLAY":
                    self.stats.plays += 1
                    respond(cseq, "200 OK", [f"Session: {session_id}", "Range: npt=0.000-"])
                    playing = True
                    if not self.silent:
                        streaming = asyncio.create_task(self._stream(writer))
                elif method == "TEARDOWN":
                    self.stats.teardowns += 1
                    tore_down = True
                    respond(cseq, "200 OK", [f"Session: {session_id}"])
                    await writer.drain()
                    break
                else:
                    respond(cseq, "200 OK", [f"Session: {session_id}"])
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            if streaming is not None:
                streaming.cancel()
                with contextlib.suppress(BaseException):
                    await streaming
            awake = time.monotonic() < self._awake_until
            if playing and not tore_down and awake:
                # The client vanished while the camera was still serving it: Eufy keeps
                # the dead session and turns everyone away for a while.
                self.stats.unclean_disconnects += 1
                self._ghost_until = time.monotonic() + self.ghost_s
            self._sessions.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        seq = random.randint(0, 0xFFFF)
        ssrc = random.randint(0, 0xFFFFFFFF)
        timestamp = random.randint(0, 0xFFFFFFFF)
        step = 90_000 // _FPS
        index = 0
        while True:
            unit = self._units[index % len(self._units)]
            payloads = [payload for nal in unit for payload in _rtp_payloads(nal)]
            for number, payload in enumerate(payloads):
                marker = 0x80 if number == len(payloads) - 1 else 0
                header = struct.pack(
                    ">BBHII", 0x80, marker | 96, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc
                )
                packet = header + payload
                writer.write(b"$\x00" + struct.pack(">H", len(packet)) + packet)
                seq += 1
            await writer.drain()
            self.stats.frames_sent += 1
            timestamp += step
            index += 1
            await asyncio.sleep(1 / _FPS)
