"""Bounded local faster-whisper adapter and redacted utterance preparation."""

import hashlib
import io
import math
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ConfigDict, FiniteFloat

from app.artifacts import verified_model_directory
from app.contracts import Utterance
from app.db import connect
from app.ingest import MAX_AUDIO_BYTES, MAX_DURATION_MS
from app.privacy import redact_text
from app.storage import LocalPrivateStorage

MAX_TRANSCRIPTION_WORKERS = 1
MAX_STT_SEGMENTS = 50_000
MAX_STT_TEXT_CHARS = 1_000_000
_MODEL_GATE = threading.BoundedSemaphore(MAX_TRANSCRIPTION_WORKERS)
_MODEL_LOCK = threading.Lock()
_MODEL: Any | None = None
_MODEL_VERSION: str | None = None


class TranscriptionError(RuntimeError):
    """Safe stage failure; never includes transcript or model output."""


class PreparedUtterance(Utterance):
    model_config = ConfigDict(extra="forbid", strict=True)

    segment_id: str
    speaker_id: str
    confidence: FiniteFloat | None = None


def _field(segment: Any, name: str, default=None):
    if isinstance(segment, Mapping):
        return segment.get(name, default)
    return getattr(segment, name, default)


def normalise_segments(
    response: dict,
    channel_roles: Mapping[int, str],
    *,
    channel_index: int | None = None,
) -> list[dict]:
    """Convert local model segments to bounded call-relative values.

    Channel metadata is trusted only when supplied by the authenticated media
    adapter. A mono stream or unmapped channel stays UNKNOWN.
    """
    source = response.get("segments", ()) if isinstance(response, dict) else ()
    result: list[dict] = []
    total_chars = 0
    for index, segment in enumerate(source):
        text = _field(segment, "text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        total_chars += len(text)
        if len(result) >= MAX_STT_SEGMENTS or total_chars > MAX_STT_TEXT_CHARS:
            raise TranscriptionError("Local model output exceeds configured resource bounds")
        try:
            start, end = float(_field(segment, "start")), float(_field(segment, "end"))
        except (TypeError, ValueError):
            raise TranscriptionError("Local model returned invalid segment timing") from None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or end * 1000 > MAX_DURATION_MS:
            raise TranscriptionError("Local model returned invalid segment timing")
        if channel_index is not None:
            candidate_role = channel_roles.get(channel_index, "UNKNOWN")
            speaker_id = f"channel-{channel_index}"
            source_segment_id = _field(segment, "id", f"segment-{index}")
            segment_id = f"channel-{channel_index}:{source_segment_id}"
        else:
            candidate_role = "UNKNOWN"
            speaker_id = "mono-unknown"
            segment_id = str(_field(segment, "id", f"segment-{index}"))
        role = candidate_role if candidate_role in {"AGENT", "CUSTOMER", "IVR"} else "UNKNOWN"
        confidence = _field(segment, "confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
            if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
                confidence = None
        result.append({
            "segment_id": segment_id,
            "speaker_id": speaker_id[:128],
            "role": role,
            "start_ms": round(start * 1000),
            "end_ms": round(end * 1000),
            "text": text,
            "confidence": confidence,
        })
    return result


def _decode_audio_channels(audio: bytes, channels: int):
    """Decode mono/stereo audio to separate 16 kHz float32 streams using local PyAV."""
    if channels not in (1, 2):
        raise TranscriptionError("Only mono or stereo recordings are supported")
    try:
        import av
        import numpy as np

        with av.open(io.BytesIO(audio), mode="r") as container:
            if not container.streams.audio:
                raise TranscriptionError("Recording has no audio stream")
            stream = container.streams.audio[0]
            layout = stream.codec_context.layout
            source_channels = layout.nb_channels if layout is not None else stream.codec_context.channels
            if source_channels != channels:
                raise TranscriptionError("Recording channel metadata does not match media")
            output_layout = "mono" if channels == 1 else "stereo"
            resampler = av.AudioResampler(format="fltp", layout=output_layout, rate=16000)
            decoded: list[list[Any]] = [[] for _ in range(channels)]
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    planes = converted.to_ndarray()
                    if planes.ndim != 2 or planes.shape[0] != channels:
                        raise TranscriptionError("Local decoder returned an unexpected channel layout")
                    for index in range(channels):
                        decoded[index].append(planes[index].astype(np.float32, copy=False))
            for converted in resampler.resample(None):
                planes = converted.to_ndarray()
                if planes.ndim != 2 or planes.shape[0] != channels:
                    raise TranscriptionError("Local decoder returned an unexpected channel layout")
                for index in range(channels):
                    decoded[index].append(planes[index].astype(np.float32, copy=False))
            if any(not values for values in decoded):
                raise TranscriptionError("Local decoder returned an empty channel")
            return [np.concatenate(values) for values in decoded]
    except TranscriptionError:
        raise
    except Exception:
        raise TranscriptionError("Local audio channel decoding failed") from None


def prepare_utterances(segments: list[dict], redact: Callable[[str], str]) -> list[PreparedUtterance]:
    """Redact the whole batch before returning any persistence-ready item."""
    prepared: list[PreparedUtterance] = []
    seen: set[str] = set()
    total_chars = 0
    try:
        for segment in segments:
            segment_id = str(segment["segment_id"])
            if not segment_id or len(segment_id) > 128 or segment_id in seen:
                raise TranscriptionError("Local model returned duplicate or invalid segment IDs")
            seen.add(segment_id)
            source = segment["text"]
            if not isinstance(source, str) or not source.strip():
                continue
            total_chars += len(source)
            if len(prepared) >= MAX_STT_SEGMENTS or total_chars > MAX_STT_TEXT_CHARS:
                raise TranscriptionError("Transcript exceeds configured resource bounds")
            redacted = redact(source)
            if not isinstance(redacted, str):
                raise TranscriptionError("Redactor returned invalid output")
            stable_id = hashlib.sha256(segment_id.encode("utf-8")).hexdigest()[:32]
            prepared.append(PreparedUtterance(
                id=stable_id,
                role=segment["role"],
                start_ms=segment["start_ms"],
                end_ms=segment["end_ms"],
                text_redacted=redacted,
                is_final=True,
                segment_id=segment_id,
                speaker_id=segment["speaker_id"],
                confidence=segment.get("confidence"),
            ))
    except TranscriptionError:
        raise
    except Exception:
        # Keep PII and exception messages out of worker error paths.
        raise TranscriptionError("Transcript preparation or redaction failed") from None
    return prepared


def _load_model():
    global _MODEL, _MODEL_VERSION
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL, _MODEL_VERSION
        path = verified_model_directory(
            os.environ.get("FASTER_WHISPER_MODEL_PATH"),
            os.environ.get("FASTER_WHISPER_MODEL_SHA256"),
        )
        version = os.environ.get("FASTER_WHISPER_MODEL_VERSION", "").strip()
        if not version or len(version) > 128:
            raise TranscriptionError("Local transcription model version is not configured")
        try:
            from faster_whisper import WhisperModel

            _MODEL = WhisperModel(
                str(path),
                device=os.environ.get("FASTER_WHISPER_DEVICE", "cpu"),
                compute_type=os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "int8"),
                cpu_threads=2,
                num_workers=1,
                local_files_only=True,
            )
        except Exception:
            raise TranscriptionError("Pinned local transcription model is unavailable") from None
        _MODEL_VERSION = version
        return _MODEL, _MODEL_VERSION


def transcribe_recording(
    private_key: str,
    language: str,
    *,
    storage: LocalPrivateStorage | None = None,
    model: Any | None = None,
    duration_ms: int | None = None,
    expected_sha256: str | None = None,
    channels: int = 1,
    channel_roles: Mapping[int, str] | None = None,
    audio_decoder: Callable[[bytes, int], list[Any]] = _decode_audio_channels,
) -> list[dict]:
    if not language or len(language) > 32:
        raise TranscriptionError("Unsupported language value")
    if duration_ms is not None and not 0 < duration_ms <= MAX_DURATION_MS:
        raise TranscriptionError("Recording duration exceeds local inference limit")
    if channels not in (1, 2):
        raise TranscriptionError("Only mono or stereo recordings are supported")
    if not _MODEL_GATE.acquire(blocking=False):
        raise TranscriptionError("Local transcription capacity is busy")
    try:
        storage = storage or LocalPrivateStorage(os.environ.get("AUDIO_STORAGE_PATH", "./private-audio"))
        try:
            audio = storage.get(private_key, max_bytes=MAX_AUDIO_BYTES)
        except Exception:
            raise TranscriptionError("Private audio object is unavailable") from None
        if not audio:
            raise TranscriptionError("Private audio object is empty")
        if expected_sha256 is not None and hashlib.sha256(audio).hexdigest() != expected_sha256.lower():
            raise TranscriptionError("Private audio checksum verification failed")
        if model is None:
            model, _model_version = _load_model()
        try:
            decoded_channels = audio_decoder(audio, channels)
            if len(decoded_channels) != channels:
                raise TranscriptionError("Local decoder returned an unexpected channel count")
            all_segments: list[dict] = []
            for channel_index, pcm in enumerate(decoded_channels):
                segments, _info = model.transcribe(
                    pcm,
                    language=None if language == "und" else language,
                    word_timestamps=False,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    beam_size=5,
                )
                all_segments.extend(normalise_segments(
                    {"segments": segments},
                    channel_roles or {},
                    channel_index=channel_index if channels == 2 else None,
                ))
            # Preserve overlap and channel identity while ordering by call offsets.
            return sorted(all_segments, key=lambda item: (item["start_ms"], item["end_ms"], item["speaker_id"], item["segment_id"]))
        except TranscriptionError:
            raise
        except Exception:
            raise TranscriptionError("Local transcription failed") from None
    finally:
        _MODEL_GATE.release()


def make_transcription_processor(
    *,
    storage: LocalPrivateStorage | None = None,
    transcriber: Callable[..., list[dict]] = transcribe_recording,
    redact: Callable[[str], str] = redact_text,
    model_version: str | None = None,
):
    """Build a TRANSCRIBE stage handler; output contains redacted text only."""
    def process(job: dict) -> dict:
        with connect() as connection:
            details = connection.execute(
                "SELECT c.language,a.private_key,a.duration_ms,a.checksum,a.channels FROM calls c "
                "JOIN audio_objects a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
                "JOIN jobs j ON j.organisation_id=c.organisation_id AND j.call_id=c.id "
                "WHERE c.organisation_id=%s AND c.id=%s AND j.id=%s AND j.stage='TRANSCRIBE' "
                "AND c.tombstoned_at IS NULL",
                (job["organisation_id"], job["call_id"], job["id"]),
            ).fetchone()
        if details is None:
            raise TranscriptionError("Transcription input is unavailable")
        language, private_key, duration_ms, expected_sha256, channels = details
        raw_segments = transcriber(
            private_key,
            language,
            storage=storage,
            duration_ms=duration_ms,
            expected_sha256=expected_sha256,
            channels=channels,
        )
        if redact is redact_text:
            redactor = lambda text: redact(text, language=language)
        else:
            redactor = redact
        prepared = prepare_utterances(raw_segments, redactor)
        version = model_version or os.environ.get("FASTER_WHISPER_MODEL_VERSION", "")
        if not version or len(version) > 128:
            raise TranscriptionError("Transcription model version is not configured")
        return {
            "processing_state": "ANALYSING" if prepared else "NEEDS_REVIEW",
            "model_version": version,
            "utterances": [item.model_dump() for item in prepared],
        }
    return process
