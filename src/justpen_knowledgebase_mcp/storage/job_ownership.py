"""Trusted outstanding blob ownership, independent of diagnostic JSON."""

from __future__ import annotations

import json
import re
from typing import Any, NoReturn, cast

from pydantic import ValidationError

from ..errors import ConflictError, StorageIOError
from ..models import JobProgress

OTHER_BLOB_OWNER_SQL = "SELECT 1 FROM jobs WHERE blob_sha256=? AND uuid<>? LIMIT 1"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate ownership metadata key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("nonfinite ownership metadata")


def ownership_object(raw: str) -> dict[str, Any]:
    """Decode metadata strictly; corrupt progress never supplies ownership proof."""
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (TypeError, ValueError) as exc:
        raise StorageIOError("IO_ERROR: malformed job ownership metadata") from exc
    if not isinstance(value, dict):
        raise StorageIOError("IO_ERROR: malformed job ownership metadata")
    return cast("dict[str, Any]", value)


def progress_object(raw: str) -> dict[str, Any]:
    """Reject malformed verified metadata even when the durable locator is absent."""
    return _validate_progress(ownership_object(raw))


def _validate_progress(value: dict[str, Any]) -> dict[str, Any]:
    try:
        JobProgress.model_validate({key: item for key, item in value.items() if key in JobProgress.model_fields})
    except ValidationError as exc:
        raise StorageIOError("IO_ERROR: malformed job progress counters") from exc
    digest = value.get("verified_sha256")
    if "verified_sha256" in value and (not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None):
        raise StorageIOError("IO_ERROR: malformed published blob locator")
    return value


def row_progress(row: dict[str, Any]) -> dict[str, Any]:
    """Validate this row only; diagnostic JSON never becomes physical ownership."""
    progress = progress_object(row["progress"])
    current = row.get("blob_sha256")
    if current is not None and (not isinstance(current, str) or re.fullmatch("[0-9a-f]{64}", current) is None):
        raise StorageIOError("IO_ERROR: malformed published blob locator")
    if progress.get("verified_sha256") != current:
        raise StorageIOError("IO_ERROR: job blob ownership metadata disagrees")
    return progress


def row_metadata(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate all object cells before admission, without changing their raw data."""
    return ownership_object(row["payload"]), row_progress(row), ownership_object(row["result"])


def checkpoint_ownership(row: dict[str, Any], progress: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Generic progress cannot discard an outstanding publication/retry locator."""
    previous = row_progress(row)
    current = row.get("blob_sha256")
    updated = dict(progress)
    if "verified_sha256" in updated:
        digest = updated["verified_sha256"]
        if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise StorageIOError("IO_ERROR: malformed verified blob checkpoint")
        if current is not None and current != digest:
            raise ConflictError("PUBLISHED_BLOB_OWNERSHIP_UNRESOLVED")
    else:
        digest = current
    if current is not None:
        for key in ("verified_sha256", "bytes", "stage_token"):
            if key in previous and ("verified_sha256" not in progress or key not in updated):
                updated[key] = previous[key]
    return _validate_progress(updated), digest


def failure_object(value: object) -> dict[str, Any]:
    """Bound failure diagnostics even if an execution callback damaged a cached claim."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
