"""Server identity and strict UTC microsecond metadata codecs."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .catalog import catalog_manifest, validate_record
from .mutations import canonical_json

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def identity_json(kind: str, type_name: str, properties: dict[str, Any]) -> str:
    """Return the catalog-selected scalar identity, preserving JSON types."""
    validate_record(kind, type_name, properties)
    fields = catalog_manifest()[kind][type_name]["identity"]
    selected = {field: properties[field] for field in fields}
    if any(type(value) not in (str, bool, int) for value in selected.values()):
        raise ValueError("identity only supports string, boolean and signed64")
    return canonical_json(selected)


def identity_key(kind: str, type_name: str, properties: dict[str, Any]) -> str:
    """Generate the read-only key independently of metadata and neighbors."""
    content = identity_json(kind, type_name, properties)
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
