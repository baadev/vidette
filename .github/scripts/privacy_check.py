#!/usr/bin/env python3
"""Keep private data out of this public repository (stdlib only; CI and pre-commit).

    python3 .github/scripts/privacy_check.py            # every tracked file
    python3 .github/scripts/privacy_check.py FILE…      # these files (pre-commit passes the staged ones)

What it refuses, and what it lets through so the existing examples stay valid:

1. **Credentials in camera URLs**: `rtsp://user:password@host`. Allowed only when the
   password is a placeholder (`${VAR}`, `{var}` in code, `***`, `PASSWORD`, `pass`, `p`, …)
   or the host is a documentation or test host (192.0.2.x, 198.51.100.x, 203.0.113.x,
   10.0.20.x, 127.0.0.1, localhost, `cam`, `IP`, `*.test`, `example.*`).
2. **Private addresses** (10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16) outside the
   documentation ranges above: a real LAN address says where someone's cameras are.
3. **Images, video and model files** outside the folders allowed to hold them
   (`docs/`, `web/`, `datasets/synthetic-sample/`, `server/tests/fixtures/`), and anywhere
   under a `faces/` or `people/` folder. Model weights (`.onnx`, `.pt`, `.npy`, …) never.
4. **Email addresses** other than the project's own contact and example/no-reply ones.

A line can opt out with `privacy-check: allow` in a comment on it; use that only for an
example someone reviewed. Exit status 1 lists every finding as `path:line: reason`.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOW_MARK = "privacy-check: allow"

DOC_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "10.0.20.0/24", "127.0.0.0/8")
]
PRIVATE_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")  # privacy-check: allow
]
PLACEHOLDER_PASSWORDS = re.compile(
    r"^(\*+|x+|p|pass|password|passwd|secret|changeme|pw|PASSWORD|PASS|SECRET|USER_PASSWORD"
    r"|p%40ss%3Aword)$",
    re.IGNORECASE,
)
TEST_HOSTS = re.compile(
    r"^(localhost|cam|camera|ip|host|nvr|frigate|go2rtc|[\w.-]+\.(test|example|invalid|local)"
    r"|example\.(com|org|net))$",
    re.IGNORECASE,
)
URL_CREDS = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s:@'\"`<>]+):([^/\s@'\"`<>]+)@([^/\s:'\"`<>]+)")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAILS = re.compile(
    r"^(alex@baadev\.com|noreply@anthropic\.com|git@github\.com|[^@]+@example\.(com|org|net)"
    r"|[^@]+@users\.noreply\.github\.com|noreply@[^@]+)$",
    re.IGNORECASE,
)

MEDIA = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif",
    ".avif", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts",
}  # fmt: skip
WEIGHTS = {".onnx", ".pt", ".pth", ".npy", ".npz", ".tflite", ".pb", ".safetensors", ".bin"}
MEDIA_ALLOWED = ("docs/", "web/", "datasets/synthetic-sample/", "server/tests/fixtures/")
NEVER_UNDER = {"faces", "people"}
SKIP_TEXT = {"server/uv.lock", "web/package-lock.json"}
TS_SOURCE = {"web/src", "server"}  # .ts is TypeScript there, not a video


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [path for path in out.split("\0") if path]


def _doc_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return bool(TEST_HOSTS.match(host))
    return any(address in network for network in DOC_NETWORKS)


def _placeholder(password: str) -> bool:
    return (
        "${" in password
        or "{" in password
        or password.startswith("$")
        or bool(PLACEHOLDER_PASSWORDS.match(password))
    )


def check_media(path: str) -> list[str]:
    suffix = Path(path).suffix.lower()
    parts = set(Path(path).parts[:-1])
    if suffix in WEIGHTS:
        return [f"{path}: model weights do not belong in the repository"]
    if suffix == ".ts" and path.startswith(tuple(TS_SOURCE)):
        return []
    if suffix not in MEDIA:
        return []
    if parts & NEVER_UNDER:
        return [f"{path}: an image or video under a faces/ or people/ folder (biometric data)"]
    if not path.startswith(MEDIA_ALLOWED):
        allowed = ", ".join(MEDIA_ALLOWED)
        return [f"{path}: images and video are allowed only under {allowed}"]
    return []


def check_text(path: str, text: str) -> list[str]:
    findings: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARK in line:
            continue
        where = f"{path}:{number}"
        for _user, password, host in URL_CREDS.findall(line):
            if not _placeholder(password) and not _doc_host(host):
                findings.append(f"{where}: a URL with a real-looking password ({host})")
        for candidate in IPV4.findall(line):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if any(address in n for n in PRIVATE_NETWORKS) and not any(
                address in n for n in DOC_NETWORKS
            ):
                findings.append(
                    f"{where}: private address {candidate}; use 192.0.2.x or 10.0.20.x in "
                    "examples"
                )
        for email in EMAIL.findall(line):
            if not ALLOWED_EMAILS.match(email):
                findings.append(f"{where}: e-mail address {email}; keep personal ones out")
    return findings


def check(paths: list[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        findings += check_media(path)
        if path in SKIP_TEXT or Path(path).suffix.lower() in MEDIA | WEIGHTS:
            continue
        file = ROOT / path
        try:
            data = file.read_bytes()
        except OSError:
            continue  # deleted in this commit
        if b"\0" in data[:4096]:
            continue
        findings += check_text(path, data.decode("utf-8", errors="replace"))
    return findings


def main(argv: list[str]) -> int:
    paths = [Path(p).resolve().relative_to(ROOT).as_posix() for p in argv] or tracked()
    findings = check(paths)
    for finding in findings:
        print(finding)
    if findings:
        print(f"privacy check: {len(findings)} finding(s); see .github/scripts/privacy_check.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
