"""Fail-closed local PII redaction. No text or detected values are logged."""

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.artifacts import verified_model_directory

MAX_TRANSCRIPT_TEXT = 10_000
REDACTION = "[REDACTED]"


class RedactionError(RuntimeError):
    """The transcript could not be safely redacted."""


class Analyzer(Protocol):
    def analyze(self, *, text: str, language: str): ...


@dataclass(frozen=True)
class _Span:
    start: int
    end: int


# Structured recognisers supplement Presidio's local NLP recognisers.
_STRUCTURED_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)"),
    re.compile(r"(?i)\b(?:account|policy|member|customer)\s*(?:number|no\.?|#)\s*[:#-]?\s*[A-Z0-9-]{5,}\b"),
    re.compile(r"(?i)\b(?:my name is|this is|I am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})"),
    re.compile(r"(?i)\b(?:address is|lives? at|located at)\s+([0-9]{1,6}\s+[A-Z0-9][A-Za-z0-9 .'-]{2,50})"),
)


def _local_analyzer() -> Analyzer:
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        model_dir = verified_model_directory(
            os.environ.get("PRESIDIO_SPACY_MODEL_PATH"),
            os.environ.get("PRESIDIO_SPACY_MODEL_SHA256"),
        )
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": str(model_dir)}],
            }
        )
        engine = provider.create_engine()
        return AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
    except Exception as error:
        # Do not preserve dependency/model exception messages near transcript data.
        raise RedactionError("Local PII recognizer is unavailable") from None


@lru_cache(maxsize=1)
def _configured_analyzer() -> Analyzer:
    return _local_analyzer()


def _merge_spans(spans: list[_Span], length: int) -> list[_Span]:
    normalized = sorted(
        (_Span(max(0, s.start), min(length, s.end)) for s in spans if s.end > s.start),
        key=lambda span: (span.start, span.end),
    )
    merged: list[_Span] = []
    for span in normalized:
        if merged and span.start <= merged[-1].end:
            merged[-1] = _Span(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    return merged


def redact_text(text: str, analyzer: Analyzer | None = None, *, language: str = "en") -> str:
    if not isinstance(text, str) or len(text) > MAX_TRANSCRIPT_TEXT:
        raise RedactionError("Transcript text is invalid")
    if not text:
        return ""
    if language.lower().split("-", 1)[0] != "en":
        raise RedactionError("No local PII recognizer is configured for this language")
    recognizer = analyzer if analyzer is not None else _configured_analyzer()
    try:
        spans = [
            _Span(match.start(1) if match.lastindex else match.start(), match.end(1) if match.lastindex else match.end())
            for pattern in _STRUCTURED_PATTERNS
            for match in pattern.finditer(text)
        ]
        for result in recognizer.analyze(text=text, language="en"):
            spans.append(_Span(int(result.start), int(result.end)))
        merged = _merge_spans(spans, len(text))
    except Exception:
        raise RedactionError("Transcript redaction failed") from None
    output: list[str] = []
    offset = 0
    for span in merged:
        output.extend((text[offset:span.start], REDACTION))
        offset = span.end
    output.append(text[offset:])
    redacted = "".join(output)
    if len(redacted) > MAX_TRANSCRIPT_TEXT:
        raise RedactionError("Redacted transcript text exceeds limit")
    return redacted
