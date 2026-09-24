"""Bounded opaque binary frames for a not-yet-enabled TeleCMI stream."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


class TeleCMIProtocolError(ValueError):
    """A frame exceeds the local safety bounds for this stream."""


@dataclass(frozen=True)
class TeleCMIAudioFrame:
    payload: bytes
    speaker: str = "UNKNOWN"


class TeleCMIStream:
    """Pass through bounded binary messages without guessing their encoding."""

    MAX_FRAME_BYTES = 100_000
    MAX_STREAM_BYTES = 100_000_000
    MAX_SESSION_SECONDS = 7_200

    def __init__(self, *, started_at: float | None = None) -> None:
        self.started_at = time.monotonic() if started_at is None else started_at
        if (type(self.started_at) not in (int, float) or not math.isfinite(self.started_at)
                or self.started_at < 0):
            raise ValueError("started_at must be a finite monotonic time")
        self._total_bytes = 0

    def accept(self, message: bytes, *, now: float | None = None) -> TeleCMIAudioFrame:
        """Validate local size and elapsed-time limits; do not decode or label audio."""
        received_at = time.monotonic() if now is None else now
        if (type(received_at) not in (int, float) or not math.isfinite(received_at)
                or received_at < self.started_at):
            raise TeleCMIProtocolError("invalid stream time")
        if received_at - self.started_at > self.MAX_SESSION_SECONDS:
            raise TeleCMIProtocolError("stream duration limit exceeded")
        if not isinstance(message, bytes) or not message or len(message) > self.MAX_FRAME_BYTES:
            raise TeleCMIProtocolError("binary frame must be nonempty and bounded")
        if self._total_bytes + len(message) > self.MAX_STREAM_BYTES:
            raise TeleCMIProtocolError("stream byte limit exceeded")

        # ponytail: opaque frames have no trusted duration metadata; elapsed time and total bytes cap exposure until the vendor contract is confirmed.
        self._total_bytes += len(message)
        return TeleCMIAudioFrame(message)
