"""Server identity and strict UTC microsecond metadata codecs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import AfterValidator, Field

from .catalog import catalog_view, validate_record
from .errors import ExpectedValidationError
from .mutations import canonical_json

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _order_independent_hash(properties: dict[str, Any], rule: Mapping[str, Any]) -> tuple[str, str]:
    """Return one catalog-declared collection property as a canonical digest.

    `rule` is read from the shared catalog view, so its arrays arrive as tuples; the sequence checks
    accept either spelling so a rule taken from `catalog_manifest()` behaves identically.
    """
    field = cast("str", rule["property"])
    values = cast("list[Any]", properties[field])
    projection = rule.get("projection")
    if isinstance(projection, (list, tuple)):
        fields = cast("Sequence[str]", projection)
        normalized = [{name: value[name] for name in fields} for value in values]
        normalized.sort(key=lambda value: tuple(value[name] for name in cast("Sequence[str]", rule["sort"])))
    else:
        normalized = sorted(values)
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return field, digest


def identity_json(kind: str, type_name: str, properties: dict[str, Any], parent_id: str | None = None) -> str:
    """Return catalog-selected identity JSON, including a scoped parent UUID when declared."""
    validate_record(kind, type_name, properties)
    identity = cast("Mapping[str, Any]", catalog_view()[kind][type_name]["identity"])
    fields = cast("Sequence[str]", identity["properties"])
    selected = {field: properties[field] for field in fields}
    scope = identity.get("scope")
    if scope is not None:
        if type(parent_id) is not str:
            raise ExpectedValidationError("scoped identity requires a parent node id")
        try:
            parsed_parent = UUID(parent_id)
        except (TypeError, ValueError) as exc:
            raise ExpectedValidationError("scoped identity requires a valid parent node id") from exc
        if str(parsed_parent) != parent_id:
            raise ExpectedValidationError("scoped identity requires a canonical parent node id")
        selected["parent"] = parent_id
    order_independent = identity.get("order_independent")
    if isinstance(order_independent, Mapping):
        field, digest = _order_independent_hash(properties, cast("Mapping[str, Any]", order_independent))
        selected[field] = digest
    if any(type(value) not in (str, bool, int) for value in selected.values()):
        raise ValueError("identity only supports string, boolean and signed64")
    return canonical_json(selected)


def identity_key(kind: str, type_name: str, properties: dict[str, Any], parent_id: str | None = None) -> str:
    """Generate the read-only key from properties and any declared parent scope."""
    content = identity_json(kind, type_name, properties, parent_id)
    return "" if content == "{}" else hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_timestamp(value: str) -> int:
    """Check the public ASCII grammar before calendar and offset conversion."""
    if _TIMESTAMP.fullmatch(value) is None or value.endswith("-00:00"):
        raise ValueError("invalid timestamp representation")
    if int(value[17:19]) > 59:
        raise ValueError("invalid timestamp second")
    if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError("invalid timestamp offset")
    try:
        parsed = datetime.fromisoformat(value).astimezone(UTC)
        delta = parsed - _EPOCH
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid timestamp calendar or UTC range") from exc
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def format_timestamp(value: int) -> str:
    """Emit a fixed six-digit UTC representation, including four-digit years."""
    date = _EPOCH + timedelta(microseconds=value)
    return f"{date.year:04d}-{date.month:02d}-{date.day:02d}T{date.hour:02d}:{date.minute:02d}:{date.second:02d}.{date.microsecond:06d}Z"


# Compiled rather than inlined so one object is both what `validate_evidence_id` enforces and what
# `_published_pattern` publishes; a literal repeated in either place is a spelling free to drift.
_CANONICAL_EVIDENCE = re.compile(r"e_[0-9a-f]{64}")


def validate_evidence_id(value: str) -> str:
    """Require the canonical content-addressed public evidence identity."""
    if _CANONICAL_EVIDENCE.fullmatch(value) is None:
        raise ValueError("invalid evidence id")
    return value


# The one spelling SQLite's BINARY collation can match against a stored identifier. This is the
# acceptance `storage/job_retention.py:68-77` writes as `str(UUID(value)) != value`, spelled as a
# grammar the way `validate_evidence_id` above spells its own: `RecordID` now reaches every ingress
# identifier, including a thousand traversal seeds per request, and the roundtrip measured 3.3 us
# against this pattern's 0.45 us. `tests/test_identity.py` holds the two against each other over a
# generated corpus so the cheaper spelling cannot drift away from the precedent.
_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _graph_id_rejection(value: str) -> str:
    """Name the defect on the cold path, where re-parsing the rejected value costs nothing."""
    try:
        UUID(value)
    except (AttributeError, TypeError, ValueError):
        return "invalid graph id"
    return "non-canonical graph id: use the lowercase 8-4-4-4-12 spelling"


def validate_graph_id(value: str) -> str:
    """Require the canonical graph UUID spelling rather than canonicalizing a variant of it.

    SQLite compares TEXT with BINARY collation, so `550E8400-...` never matches the stored
    `550e8400-...`: the server would report an existing record as missing, and two spellings of one
    identifier would pass the duplicate checks in `models.py` as two identifiers. Rewriting the
    value instead would hide the client defect and change what a caller gets back.
    """
    if _CANONICAL_UUID.fullmatch(value) is None:
        raise ValueError(_graph_id_rejection(value))
    return value


def validate_record_id(kind: str, value: str) -> str:
    """Keep graph UUIDs and content-addressed evidence IDs disjoint."""
    if kind == "evidence":
        return validate_evidence_id(value)
    return validate_graph_id(value)


def _published_pattern(grammar: re.Pattern[str]) -> str:
    """Spell a `fullmatch` grammar as the anchored ECMA-262 `pattern` a JSON Schema publishes.

    `pattern` is an unanchored ECMA-262 search, so without the anchors the published constraint
    would accept any string merely containing an identifier. The non-capturing group keeps the
    anchors outside any alternation a future grammar adds. Both grammars here are character classes
    and counted repetitions, which ECMA-262 spells exactly as Python does;
    `tests/test_identity.py` holds that subset rather than leaving it to inspection.
    """
    return f"^(?:{grammar.pattern})$"


GRAPH_ID_PATTERN = _published_pattern(_CANONICAL_UUID)
EVIDENCE_ID_PATTERN = _published_pattern(_CANONICAL_EVIDENCE)
# `json_schema_extra` publishes metadata only: the acceptance stays the `AfterValidator` below, so a
# non-canonical evidence id is still refused by `validate_evidence_id` with its own message.
EvidenceID = Annotated[
    str,
    Field(json_schema_extra={"pattern": EVIDENCE_ID_PATTERN}),
    AfterValidator(validate_evidence_id),
]
