"""Cursor schemas are strict, bounded, and bound to current query semantics."""

import base64
import json
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.cursors import CursorBinding


def binding(**changes):
    return CursorBinding(
        workspace_id=changes.get("workspace_id", "00000000-0000-0000-0000-000000000001"),
        query_epoch=changes.get("query_epoch", 1),
        kind="nodes",
        owner_id=changes.get("owner_id"),
        view="links",
        query={"a": 1},
    )


def test_round_trip_and_binding_changes():
    cursor = binding().encode(99)
    assert binding().decode(cursor) == 99
    for other in (binding(query_epoch=2), binding(owner_id=str(uuid4())), binding(workspace_id=str(uuid4()))):
        with pytest.raises(ValueError):
            other.decode(cursor)


@pytest.mark.parametrize(
    "change", [{"version": True}, {"after_id": -1}, {"after_id": 2**63}, {"extra": 1}, {"query_epoch": 1.0}]
)
def test_strict_schema(change):
    original = binding().encode(0)
    data = json.loads(base64.urlsafe_b64decode(original + "=" * (-len(original) % 4)))
    data.update(change)
    with pytest.raises(ValueError):
        binding().decode(base64.urlsafe_b64encode(json.dumps(data).encode()).decode())


def test_duplicate_members_and_byte_limit():
    with pytest.raises(ValueError):
        binding().decode(base64.urlsafe_b64encode(b'{"version":1,"version":1}').decode())
    with pytest.raises(ValueError):
        binding().decode("a" * 4097)


def test_deeply_nested_cursor_is_invalid_not_an_internal_failure():
    payload = b"[" * 1100 + b"0" + b"]" * 1100
    with pytest.raises(ValueError):
        binding().decode(base64.urlsafe_b64encode(payload).decode())
