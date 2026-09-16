"""Shared metadata validation preserves trusted ownership across boundaries."""

import json

import pytest

from justpen_knowledgebase_mcp.errors import StorageIOError
from justpen_knowledgebase_mcp.storage.job_ownership import checkpoint_ownership
from justpen_knowledgebase_mcp.storage.job_retention import recorded_blob

from .helpers import job


@pytest.mark.parametrize(
    "counter", [{"bytes": -1}, {"chunks": []}, {"rows_deleted": "private"}, {"bytes": True}, {"chunks": 1.5}]
)
def test_retention_rejects_counters_with_matching_trusted_digest(counter):
    row = job(blob_sha256="a" * 64, progress=json.dumps({"verified_sha256": "a" * 64, **counter}))
    with pytest.raises(StorageIOError):
        recorded_blob(row)


@pytest.mark.parametrize(
    ("trusted", "progress"),
    [("a" * 64, {"verified_sha256": "b" * 64}), (None, {"verified_sha256": "b" * 64}), ("a" * 64, {})],
)
def test_retention_rejects_row_local_disagreement(trusted, progress):
    with pytest.raises(StorageIOError):
        recorded_blob(job(blob_sha256=trusted, progress=json.dumps(progress)))


@pytest.mark.parametrize("counter", [{"bytes": -1}, {"chunks": []}, {"rows_deleted": "private"}])
def test_checkpoint_cannot_introduce_corrupt_counters(counter):
    with pytest.raises(StorageIOError):
        checkpoint_ownership(job(), {"verified_sha256": "a" * 64, **counter})
