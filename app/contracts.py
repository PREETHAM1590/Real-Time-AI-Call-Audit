from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
OffsetMilliseconds = Annotated[int, Field(strict=True, ge=0)]


class Utterance(BaseModel):
    """Redacted transcript content with final/provisional state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: Identifier
    role: Literal["AGENT", "CUSTOMER", "UNKNOWN", "IVR"]
    start_ms: OffsetMilliseconds
    end_ms: OffsetMilliseconds
    text_redacted: Annotated[str, Field(max_length=10_000)]
    is_final: bool = True

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> "Utterance":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class PersistedUtterance(Utterance):
    """Canonical persisted utterance envelope with local model provenance."""

    schema_version: Literal[1] = 1
    organisation_id: Identifier
    call_id: Identifier
    revision: Annotated[int, Field(strict=True, ge=1)]
    segment_id: Identifier
    speaker_id: Identifier
    model_version: Identifier
    confidence: FiniteFloat = Field(ge=0, le=1)
