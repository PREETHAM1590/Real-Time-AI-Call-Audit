"""Short-lived, identity-bound capabilities for analyst audio playback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.auth import Scope


def issue_audio_capability(scope: Scope, call_id: str, audio_id: str, secret: str, *, now: int | None = None) -> str:
    payload = {
        "org": scope.organisation_id,
        "user": scope.user_id,
        "call": call_id,
        "audio": audio_id,
        "purpose": "audio-playback",
        "exp": (int(time.time()) if now is None else now) + 60,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=")
    signature = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
    return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def verify_audio_capability(token: str, scope: Scope, call_id: str, secret: str, *, now: int | None = None) -> str | None:
    if not isinstance(token, str) or len(token) > 2048 or token.count(".") != 1:
        return None
    encoded_text, signature_text = token.split(".", 1)
    try:
        encoded = encoded_text.encode("ascii")
        supplied = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        payload = json.loads(base64.urlsafe_b64decode(encoded_text + "=" * (-len(encoded_text) % 4)))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    expected = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
    current_time = int(time.time()) if now is None else now
    if not hmac.compare_digest(supplied, expected):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("purpose") != "audio-playback"
        or payload.get("org") != scope.organisation_id
        or payload.get("user") != scope.user_id
        or payload.get("call") != call_id
        or isinstance(payload.get("exp"), bool)
        or not isinstance(payload.get("exp"), int)
        or payload["exp"] <= current_time
        or payload["exp"] > current_time + 60
        or not isinstance(payload.get("audio"), str)
        or len(payload["audio"]) > 64
    ):
        return None
    return payload["audio"]
