"""Strict parser for inbound Exotel AgentStream media events."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any


class ExotelProtocolError(ValueError):
    """An event cannot safely be accepted into this stream session."""


@dataclass(frozen=True)
class ExotelMediaFrame:
    account_sid: str
    call_sid: str
    stream_sid: str
    sequence_number: int
    chunk: int
    stream_offset_ms: int
    pcm: bytes
    missing_sequences: range
    missing_chunks: range
    speaker: str = "UNKNOWN"


@dataclass(frozen=True)
class ExotelLifecycleEvent:
    event: str
    stream_sid: str
    sequence_number: int
    missing_sequences: range
    missing_chunks: range


class ExotelSession:
    """Bind one trusted call generation to a verified Exotel event stream."""

    MAX_ENVELOPE_BYTES = 150_000
    MAX_FRAME_BYTES = 100_000
    MAX_STREAM_BYTES = 100_000_000
    MAX_DURATION_MS = 7_200_000
    MAX_SEQUENCE_NUMBER = 2_147_483_647

    def __init__(self, *, account_sid: str, call_sid: str, generation: int):
        if not account_sid or not call_sid or type(generation) is not int or generation < 0:
            raise ValueError("trusted account, call, and generation are required")
        self.account_sid = account_sid
        self.call_sid = call_sid
        self.generation = generation
        self.connected = False
        self.started = False
        self.stopped = False
        self.stream_sid: str | None = None
        self._last_sequence: int | None = None
        self._last_chunk = 0
        self._last_timestamp = -1
        self._total_bytes = 0

    @staticmethod
    def _object(value: Any, name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ExotelProtocolError(f"{name} must be an object")
        return value

    @staticmethod
    def _identity(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ExotelProtocolError(f"invalid {name}")
        return value

    def _event(self, raw: str | bytes) -> dict[str, Any]:
        if not isinstance(raw, (str, bytes)):
            raise ExotelProtocolError("event must be bounded JSON text")
        if len(raw) > self.MAX_ENVELOPE_BYTES:
            raise ExotelProtocolError("event envelope exceeds limit")
        try:
            encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        except UnicodeEncodeError as exc:
            raise ExotelProtocolError("malformed JSON event") from exc
        if len(encoded) > self.MAX_ENVELOPE_BYTES:
            raise ExotelProtocolError("event envelope exceeds limit")
        try:
            event = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ExotelProtocolError("malformed JSON event") from exc
        return self._object(event, "event")

    def _sequence(self, event: dict[str, Any]) -> tuple[int, range]:
        value = event.get("sequence_number")
        if isinstance(value, str):
            if not value.isascii() or not value.isdigit() or len(value) > 10:
                raise ExotelProtocolError("sequence_number must be a bounded positive integer")
            value = int(value)
        if type(value) is not int or not 1 <= value <= self.MAX_SEQUENCE_NUMBER:
            raise ExotelProtocolError("sequence_number must be a bounded positive integer")
        if self._last_sequence is not None and value <= self._last_sequence:
            raise ExotelProtocolError("duplicate or out-of-order sequence")
        gaps = range(self._last_sequence + 1, value) if self._last_sequence is not None else range(1, value)
        return value, gaps

    def accept(self, raw: str | bytes, *, generation: int) -> ExotelMediaFrame | ExotelLifecycleEvent | None:
        if type(generation) is not int or generation != self.generation:
            raise ExotelProtocolError("stale session generation")
        if self.stopped:
            raise ExotelProtocolError("event after stop")
        event = self._event(raw)
        kind = event.get("event")
        if kind == "connected":
            if self.connected or self.started or event.keys() != {"event"}:
                raise ExotelProtocolError("invalid connected event")
            self.connected = True
            return None
        if not self.connected:
            raise ExotelProtocolError("connected event required first")
        if kind not in {"start", "media", "stop"}:
            raise ExotelProtocolError("unsupported Exotel event")
        sequence, gaps = self._sequence(event)

        if kind == "start":
            if self.started or set(event) != {"event", "sequence_number", "stream_sid", "start"}:
                raise ExotelProtocolError("invalid start lifecycle")
            outer_sid = self._identity(event["stream_sid"], "stream SID")
            start = self._object(event["start"], "start")
            stream_sid = self._identity(start.get("stream_sid"), "stream SID")
            if stream_sid != outer_sid or start.get("call_sid") != self.call_sid or start.get("account_sid") != self.account_sid:
                raise ExotelProtocolError("start identity does not match trusted session")
            allowed_start_fields = {"stream_sid", "call_sid", "account_sid", "media_format", "from", "to", "custom_parameters"}
            if not start.keys() <= allowed_start_fields:
                raise ExotelProtocolError("unexpected start fields")
            for party_field in ("from", "to"):
                if party_field in start and (not isinstance(start[party_field], str) or len(start[party_field]) > 256):
                    raise ExotelProtocolError("invalid bounded start party field")
            custom = start.get("custom_parameters", {})
            if not isinstance(custom, dict) or len(custom) > 3 or any(
                    not isinstance(key, str) or not isinstance(value, str) for key, value in custom.items()
            ) or sum(len(key) + len(value) for key, value in custom.items()) > 256:
                raise ExotelProtocolError("invalid custom parameters")
            fmt = self._object(start.get("media_format"), "media format")
            if not fmt.keys() <= {"encoding", "sample_rate", "bit_rate", "channels"}:
                raise ExotelProtocolError("unexpected media format fields")
            sample_rate = fmt.get("sample_rate")
            if isinstance(sample_rate, str) and sample_rate.isascii() and sample_rate.isdigit() and len(sample_rate) <= 5:
                sample_rate = int(sample_rate)
            if (fmt.get("encoding") not in ("raw", "slin", "base64") or sample_rate != 8000
                    or fmt.get("channels", 1) not in (1, "1")
                    or fmt.get("bit_rate", 16) not in (16, "16", "128kbps")):
                raise ExotelProtocolError("unsupported media format")
            self.stream_sid = stream_sid
            self.started = True
            self._last_sequence = sequence
            return ExotelLifecycleEvent("start", stream_sid, sequence, gaps, range(0))

        if not self.started:
            raise ExotelProtocolError("start event required before media or stop")
        if event.get("stream_sid") != self.stream_sid:
            raise ExotelProtocolError("stream identity mismatch")

        if kind == "stop":
            if set(event) != {"event", "sequence_number", "stream_sid", "stop"}:
                raise ExotelProtocolError("invalid stop event")
            stop = self._object(event["stop"], "stop")
            if stop.get("call_sid") != self.call_sid or stop.get("account_sid") != self.account_sid:
                raise ExotelProtocolError("stop identity does not match trusted session")
            self._last_sequence = sequence
            self.stopped = True
            return ExotelLifecycleEvent("stop", self.stream_sid, sequence, gaps, range(0))

        if set(event) != {"event", "sequence_number", "stream_sid", "media"}:
            raise ExotelProtocolError("invalid media envelope")
        media = self._object(event["media"], "media")
        if set(media) != {"chunk", "timestamp", "payload"}:
            raise ExotelProtocolError("invalid media fields")
        chunk = media.get("chunk")
        stamp = media.get("timestamp")
        if type(chunk) is not int or not 1 <= chunk <= self.MAX_SEQUENCE_NUMBER or chunk <= self._last_chunk:
            raise ExotelProtocolError("duplicate or out-of-order media chunk")
        if not (isinstance(stamp, (str, int)) and not isinstance(stamp, bool)
                and str(stamp).isascii() and str(stamp).isdigit() and len(str(stamp)) <= 7):
            raise ExotelProtocolError("timestamp must be integer milliseconds")
        offset_ms = int(stamp)
        if offset_ms < self._last_timestamp or offset_ms > self.MAX_DURATION_MS:
            raise ExotelProtocolError("invalid or stale media timestamp")
        payload = media.get("payload")
        if not isinstance(payload, str) or len(payload) > ((self.MAX_FRAME_BYTES + 2) // 3) * 4:
            raise ExotelProtocolError("invalid or oversized base64 payload")
        try:
            pcm = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            raise ExotelProtocolError("invalid base64 payload") from exc
        if not pcm or len(pcm) > self.MAX_FRAME_BYTES or len(pcm) % 2:
            raise ExotelProtocolError("PCM payload must be bounded, nonempty 16-bit audio")
        if offset_ms * 16 + len(pcm) > self.MAX_DURATION_MS * 16:
            raise ExotelProtocolError("media frame extends beyond stream duration limit")
        if self._total_bytes + len(pcm) > self.MAX_STREAM_BYTES:
            raise ExotelProtocolError("stream byte limit exceeded")

        # ponytail: bounded in-memory session accounting; durable stream quotas belong at the intake service boundary.
        chunk_gaps = range(self._last_chunk + 1, chunk)
        self._last_sequence, self._last_chunk, self._last_timestamp = sequence, chunk, offset_ms
        self._total_bytes += len(pcm)
        return ExotelMediaFrame(self.account_sid, self.call_sid, self.stream_sid, sequence,
                                chunk, offset_ms, pcm, gaps, chunk_gaps)
