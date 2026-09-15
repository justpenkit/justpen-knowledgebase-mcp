"""Scripted collaborator responses, without interpreting SQL or storing graph state."""

import json
from typing import Any
from unittest.mock import MagicMock

from justpen_knowledgebase_mcp.storage.jobs import Claim

NODE = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
EVIDENCE = "e_" + "a" * 64


def cursor(*, rows=(), value=None, record=None):
    result = MagicMock()
    result.__iter__.side_effect = lambda: iter(rows)
    result.get = value
    result.fetchone.return_value = tuple(record.values()) if record is not None else next(iter(rows), None)
    result.get_description.return_value = [(name, None) for name in record or {}]
    return result


def database(*responses):
    result = MagicMock()
    if responses:
        result.execute.side_effect = responses
    result.changes.return_value = 1
    return result


def owner(**overrides):
    return {
        "id": 1,
        "uuid": NODE,
        "type": "domain",
        "key": "key",
        "properties": '{"name":"example.com"}',
        "metadata": json.dumps(
            {
                "source": None,
                "label": None,
                "property_index": {
                    "complete": True,
                    "paths_complete": True,
                    "non_array_complete": True,
                    "indexed_paths": 1,
                    "total_paths": 1,
                    "omitted_values": 0,
                },
            }
        ),
        "created_at": 1,
        "updated_at": 2,
        "observed_at": 3,
        "lifecycle": "ready",
        "delete_job_id": None,
        "delete_requested_at": None,
        "delete_cascade": 1,
        **overrides,
    }


def claim(**overrides):
    values: dict[str, Any] = {
        "job_id": NODE,
        "token": OTHER,
        "kind": "ingest",
        "lane": "short",
        "payload": {},
        "progress": {},
        "expires_at": float("inf"),
        **overrides,
    }
    return Claim(**values)


def job(**overrides):
    return {
        "id": 1,
        "uuid": NODE,
        "state": "running",
        "lease_token": OTHER,
        "lease_expires_at": float("inf"),
        "purge_pending": 0,
        "cancel_requested": 0,
        "kind": "ingest",
        "lane": "short",
        "payload": "{}",
        "progress": "{}",
        "result": "{}",
        "attempts": 1,
        "finished_at": None,
        **overrides,
    }
