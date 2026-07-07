from __future__ import annotations

from vidette.notify.signing import sign, verify

SECRET = "s3cret"
BODY = b'{"event":"event.confirmed","id":"01hxv7q8e9"}'
TS = 1_760_000_000


def test_sign_and_verify_roundtrip() -> None:
    signature = sign(SECRET, TS, BODY)
    assert signature.startswith("sha256=")
    assert verify(SECRET, TS, BODY, signature, now=TS + 10)


def test_tampered_body_fails() -> None:
    signature = sign(SECRET, TS, BODY)
    assert not verify(SECRET, TS, BODY + b" ", signature, now=TS + 10)


def test_wrong_secret_fails() -> None:
    signature = sign(SECRET, TS, BODY)
    assert not verify("other", TS, BODY, signature, now=TS + 10)


def test_stale_timestamp_fails_replay() -> None:
    signature = sign(SECRET, TS, BODY)
    assert not verify(SECRET, TS, BODY, signature, now=TS + 301)
    assert verify(SECRET, TS, BODY, signature, now=TS + 299)
