"""Tenant-scoped Exotel AgentStream credentials and recording helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import wave


def _verifier(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << 14, r=8, p=1, dklen=32)


def integration_credentials() -> tuple[str, str, bytes, bytes]:
    username = "ex_" + secrets.token_urlsafe(18)
    password = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(16)
    return username, password, salt, _verifier(password, salt)


def verify_integration_secret(password: str, salt: bytes, verifier: bytes) -> bool:
    try:
        candidate = _verifier(password, salt)
    except (UnicodeEncodeError, ValueError):
        candidate = bytes(32)
    return hmac.compare_digest(candidate, verifier)


def parse_basic_authorization(header: str | None) -> tuple[str, str] | None:
    if not isinstance(header, str) or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator or not username or not password or len(decoded) > 1024:
        return None
    return username, password


def build_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) > 100_000_000 or len(pcm) % 2:
        raise ValueError("PCM stream must be bounded, nonempty, 16-bit audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(pcm)
    return output.getvalue()


def make_audio_references(organisation_id: str, account_sid: str, call_sid: str) -> tuple[str, str]:
    digest = hashlib.sha256()
    for part in (organisation_id, account_sid, call_sid):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    value = digest.hexdigest()
    return f"exotel:{value}", f"exotel:{value}"
