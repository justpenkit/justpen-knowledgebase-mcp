"""Bounded evidence inputs and explicit media classification, without sniffing."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .identity import EvidenceID, validate_evidence_id
from .models import ClosedModel, TargetRef

INLINE_LIMIT = 256 * 1024
Encoding = Literal["auto", "utf-8", "utf-16le", "utf-16be", "latin-1"]


def is_text_candidate(media_type: str) -> bool:
    """Classify declared media without decoding or normalizing aliases."""
    return (
        media_type.startswith("text/")
        or media_type
        in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-ndjson",
            "application/json-seq",
            "application/yaml",
            "application/x-yaml",
            "application/toml",
            "message/http",
        }
        or (media_type.startswith("application/") and media_type.endswith(("+json", "+xml")))
    )


class IngestRequest(ClosedModel):
    """Exactly one local source; an inline body never enters the durable job row."""

    path: str | None = None
    text: str | None = None
    base64: str | None = None
    media_type: str | None = None
    encoding: Encoding = "auto"
    source: str | None = None
    targets: list[TargetRef] = Field(default_factory=list[TargetRef], max_length=100)

    @model_validator(mode="after")
    def source_contract(self) -> Self:
        """Validate source presence, strict base64, decoded bounds, and media."""
        supplied = self.model_fields_set & {"path", "text", "base64"}
        if len(supplied) != 1 or getattr(self, next(iter(supplied))) is None:
            raise ValueError("exactly one non-null source required")
        if self.path == "":
            raise ValueError("empty path")
        if self.inline_size > INLINE_LIMIT:
            raise ValueError("inline decoded byte limit")
        if self.media_type is not None and not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", self.media_type):
            raise ValueError("bare lowercase media type required")
        if not self.text_candidate and self.encoding != "auto":
            raise ValueError("encoding requires text media")
        if self.source is not None and len(self.source.encode("utf-8")) > 256:
            raise ValueError("source exceeds 256 bytes")
        for target in self.targets:
            UUID(target.id)
        return self

    @property
    def effective_media_type(self) -> str:
        """Apply source-specific defaults without extension/content inference."""
        return self.media_type or ("text/plain" if self.text is not None else "application/octet-stream")

    @property
    def text_candidate(self) -> bool:
        """Return declaration-based text eligibility; decoding may still fail."""
        return is_text_candidate(self.effective_media_type)

    @property
    def warnings(self) -> list[str]:
        """Expose defaulted raw-only import as a bounded operational warning."""
        return ["MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED"] if self.media_type is None and self.text is None else []

    def inline_bytes(self) -> bytes:
        """Decode a bounded inline source with strict base64 validation."""
        if self.text is not None:
            return self.text.encode("utf-8")
        if self.base64 is None:
            return b""
        if len(self.base64) > ((INLINE_LIMIT + 2) // 3) * 4:
            raise ValueError("inline decoded byte limit")
        try:
            return base64.b64decode(self.base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64") from exc

    @property
    def inline_size(self) -> int:
        """Return decoded byte size for admission, never encoded character count."""
        return len(self.inline_bytes())


class ReadEvidenceRequest(ClosedModel):
    """An exact bounded range in source bytes."""

    evidence_id: EvidenceID
    offset: Annotated[int, Field(ge=0)] = 0
    length: Annotated[int, Field(ge=0, le=65536)] = 16384
    format: Literal["text", "base64"] = "text"

    @model_validator(mode="after")
    def identity(self) -> Self:
        """Preserve the shared evidence identity boundary at raw read ingress."""
        validate_evidence_id(self.evidence_id)
        return self


class ReturnedRange(ClosedModel):
    """Returned bounds are source-byte positions, never character indices."""

    offset: Annotated[int, Field(ge=0)]
    length: Annotated[int, Field(ge=0, le=65536)]


class EvidenceReadResult(ClosedModel):
    """Content and public integrity metadata without a managed file locator."""

    evidence_id: EvidenceID
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    total_size: Annotated[int, Field(ge=0)]
    returned_range: ReturnedRange
    format: Literal["text", "base64"]
    content: Annotated[str, Field(max_length=87384)]
