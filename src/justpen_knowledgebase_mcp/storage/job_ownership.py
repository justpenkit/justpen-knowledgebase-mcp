"""Trusted outstanding blob ownership, independent of diagnostic JSON."""

from __future__ import annotations

import json
import re
from typing import Any, NoReturn, cast

from ..errors import ConflictError, StorageIOError

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
    value = ownership_object(raw)
    digest = value.get("verified_sha256")
    if "verified_sha256" in value and (not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None):
        raise StorageIOError("IO_ERROR: malformed published blob locator")
    return value


def checkpoint_ownership(row: dict[str, Any], progress: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Generic progress cannot discard an outstanding publication/retry locator."""
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
        previous = progress_object(row["progress"])
        if previous.get("verified_sha256") != current:
            raise StorageIOError("IO_ERROR: malformed verified blob checkpoint")
        for key in ("verified_sha256", "bytes", "stage_token"):
            if key in previous and ("verified_sha256" not in progress or key not in updated):
                updated[key] = previous[key]
    return updated, digest
