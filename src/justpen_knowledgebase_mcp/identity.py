"""Server identity and strict UTC microsecond metadata codecs."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import AfterValidator

from .catalog import catalog_manifest, validate_record
from .errors import ExpectedValidationError
from .mutations import canonical_json

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _order_independent_hash(properties: dict[str, Any], rule: dict[str, Any]) -> tuple[str, str]:
    """Return one catalog-declared collection property as a canonical digest."""
    field = cast("str", rule["property"])
    values = cast("list[Any]", properties[field])
    projection = rule.get("projection")
    if isinstance(projection, list):
        fields = cast("list[str]", projection)
        normalized = [{name: value[name] for name in fields} for value in values]
        normalized.sort(key=lambda value: tuple(value[name] for name in cast("list[str]", rule["sort"])))
    else:
        normalized = sorted(values)
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return field, digest


def identity_json(kind: str, type_name: str, properties: dict[str, Any], parent_id: str | None = None) -> str:
    """Return catalog-selected identity JSON, including a scoped parent UUID when declared."""
    validate_record(kind, type_name, properties)
    identity = cast("dict[str, Any]", catalog_manifest()[kind][type_name]["identity"])
    fields = cast("list[str]", identity["properties"])
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
    if isinstance(order_independent, dict):
        field, digest = _order_independent_hash(properties, cast("dict[str, Any]", order_independent))
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


def validate_evidence_id(value: str) -> str:
    """Require the canonical content-addressed public evidence identity."""
    if re.fullmatch(r"e_[0-9a-f]{64}", value) is None:
        raise ValueError("invalid evidence id")
    return value


def validate_record_id(kind: str, value: str) -> str:
    """Keep graph UUIDs and content-addressed evidence IDs disjoint."""
    if kind == "evidence":
        return validate_evidence_id(value)
    if len(value) != 36:
        raise ValueError("invalid graph id")
    UUID(value)
    return value


EvidenceID = Annotated[str, AfterValidator(validate_evidence_id)]
