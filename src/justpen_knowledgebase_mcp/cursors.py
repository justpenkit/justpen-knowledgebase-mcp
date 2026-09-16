"""One bounded, unauthenticated live keyset cursor format for graph queries."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from .identity import validate_record_id
from .mutations import canonical_json


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor field")
        result[key] = value
    return result


@dataclass(frozen=True)
class CursorBinding:
    """Expected semantics rebuilt by the server inside the guarded snapshot."""

    workspace_id: str
    query_epoch: int
    kind: str
    owner_id: str | None
    view: str
    query: dict[str, Any]

    def _fields(self) -> dict[str, Any]:
        return {
            "version": 1,
            "workspace_id": self.workspace_id,
            "query_epoch": self.query_epoch,
            "kind": self.kind,
            "owner_id": self.owner_id,
            "view": self.view,
            "query_fingerprint": hashlib.sha256(canonical_json(self.query).encode("utf-8")).hexdigest(),
        }

    def encode(self, after_id: int, after_kind: str | None = None) -> str:
        """Encode only a strict nonnegative signed64 keyset position."""
        if type(after_id) is not int or not 0 <= after_id < 2**63:
            raise ValueError("invalid cursor position")
        self._validate_kind(after_kind)
        payload = canonical_json({**self._fields(), "after_id": after_id, "after_kind": after_kind}).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        if len(encoded) > 4096:
            raise ValueError("cursor exceeds byte budget")
        return encoded

    def _validate_kind(self, after_kind: str | None) -> None:
        if self.kind == "evidence" and self.view == "links":
            if after_kind not in ("nodes", "relations"):
                raise ValueError("evidence links require association kind")
        elif after_kind is not None:
            raise ValueError("association kind is not valid for this view")

    def decode(self, cursor: str) -> int:
        """Decode a scalar position for single-source pagination."""
        return self.decode_position(cursor)[0]

    def decode_position(self, cursor: str) -> tuple[int, str | None]:
        """Validate schema and current binding; never accept SQL from a cursor."""
        if len(cursor) > 4096 or re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", cursor) is None:
            raise ValueError("invalid cursor encoding")
        try:
            data = json.loads(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True),
                object_pairs_hook=_unique_object,
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid cursor payload") from exc
        expected = self._fields()
        if type(data) is not dict:
            raise ValueError("cursor object required")
        data = cast("dict[str, Any]", data)
        if set(data) != {*expected, "after_id", "after_kind"}:
            raise ValueError("invalid cursor fields")
        for field in ("version", "query_epoch", "after_id"):
            if type(data[field]) is not int or not 0 <= data[field] < 2**63:
                raise ValueError("invalid cursor integer")
        _validate_owners(data)
        if any(type(data[field]) is not str for field in ("kind", "view", "query_fingerprint")):
            raise ValueError("invalid cursor string")
        if any(data[key] != value for key, value in expected.items()):
            raise ValueError("cursor binding mismatch")
        self._validate_kind(data["after_kind"])
        return data["after_id"], data["after_kind"]


def _validate_owners(data: dict[str, Any]) -> None:
    for field in ("workspace_id", "owner_id"):
        if field == "owner_id" and data[field] is None:
            continue
        if type(data[field]) is not str:
            raise ValueError("invalid cursor owner")
        if field == "owner_id":
            validate_record_id(data["kind"], data[field])
        else:
            UUID(data[field])
