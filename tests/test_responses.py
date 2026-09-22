"""Public envelopes reject arbitrary error details."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from justpen_knowledgebase_mcp import errors
from justpen_knowledgebase_mcp.errors import MissingRecordsError, RecordConflictError
from justpen_knowledgebase_mcp.responses import (
    BlockerDetails,
    BlockingRecord,
    ErrorResult,
    MissingDetails,
    error_response,
    exception_response,
    success_response,
)

EVIDENCE = "e_" + "0" * 64


def test_success_envelope():
    assert success_response({"n": 1}) == {"status": "ok", "data": {"n": 1}}


def test_error_envelope():
    assert error_response("INVALID", "bad input") == {"status": "error", "error": "INVALID: bad input"}


def test_long_authored_error_keeps_prefix_and_envelope_limit():
    result = exception_response(errors.BusyError("x" * 2000))
    assert result["status"] == "error"
    assert result["error"].startswith("BUSY: ")
    assert len(result["error"]) == 1024


def test_broken_expected_error_mapping_returns_fixed_internal():
    error = errors.McpError("private /workspace/secret")
    error.error_type = "PRIVATE"
    assert exception_response(error) == {"status": "error", "error": "INTERNAL: operation failed"}


def test_arbitrary_details_rejected():
    with pytest.raises(ValueError):
        ErrorResult.model_validate({"error": "BUSY: wait", "details": {"sql": "secret"}})


def test_unexpected_exception_details_never_become_public():

    assert exception_response(RuntimeError("/workspace/secret")) == {
        "status": "error",
        "error": "INTERNAL: operation failed",
    }


def test_pending_timestamp_must_be_a_timestamp():
    with pytest.raises(ValueError):
        ErrorResult.model_validate(
            {
                "error": "CONFLICT: RECORD_DELETING",
                "details": {
                    "blocking_record": {"kind": "nodes", "id": "a30c42e4-89ed-416c-b938-9d6742e56119"},
                    "pending_since": "arbitrary content",
                },
            }
        )


def test_wal_busy_preserves_frozen_retry_metadata_without_parsing_messages():

    assert hasattr(errors, "WalBusyError"), "WAL admission must carry structured retry details"
    error = errors.WalBusyError("WAL_PRESSURE", 1400)
    assert exception_response(error) == {
        "status": "error",
        "error": "BUSY: WAL_PRESSURE",
        "details": {"reason": "WAL_PRESSURE", "retry_after_ms": 1400},
    }
    assert exception_response(errors.BusyError("database queue unavailable")) == {
        "status": "error",
        "error": "BUSY: database queue unavailable",
    }


def test_graph_conflict_and_missing_details_survive_sanitized_boundary():

    identifier = uuid4()
    blocker = BlockerDetails.model_validate({"blocking_record": {"kind": "nodes", "id": identifier}})
    result = exception_response(RecordConflictError("DEPENDENCIES_EXIST", blocker))
    assert result["details"]["blocking_record"]["id"] == str(identifier)
    missing = exception_response(MissingRecordsError(MissingDetails(missing_ids=[identifier])))
    assert missing["details"]["missing_ids"] == [str(identifier)]


def test_ready_blocker_nulls_and_pending_six_digit_timestamp():
    identifier = uuid4()
    ready = BlockerDetails.model_validate({"blocking_record": {"kind": "nodes", "id": identifier}})
    result = exception_response(RecordConflictError("DEPENDENCIES_EXIST", ready))
    assert result["details"]["delete_job_id"] is None
    assert result["details"]["pending_since"] is None
    pending = BlockerDetails.model_validate(
        {
            "blocking_record": {"kind": "nodes", "id": identifier},
            "delete_job_id": uuid4(),
            "pending_since": "2001-01-01T00:00:00.000000Z",
        }
    )
    assert (
        exception_response(RecordConflictError("RECORD_DELETING", pending))["details"]["pending_since"]
        == "2001-01-01T00:00:00.000000Z"
    )


@pytest.mark.parametrize(
    ("kind", "identifier"),
    [("nodes", EVIDENCE), ("relations", EVIDENCE), ("evidence", str(uuid4()))],
    ids=["nodes-evidence-id", "relations-evidence-id", "evidence-uuid"],
)
def test_a_blocker_kind_and_id_that_disagree_are_refused(kind, identifier):
    """`kind_identity` looks like a no-op over a `UUID`, and is the only check that catches these.

    `BlockingRecord.id` is `UUID | EvidenceID`, so an evidence ID under a graph kind parses as the
    second branch instead of failing, and `kind="evidence"` coerces a UUID string into the first.
    Only the model validator refuses the disagreement, and a blocker that names the wrong kind
    sends a client looking for the record in the wrong place.
    """
    with pytest.raises(ValidationError, match=r"invalid (graph|evidence) id"):
        BlockingRecord(kind=kind, id=identifier)


@pytest.mark.parametrize(
    ("kind", "identifier"), [("nodes", str(uuid4())), ("evidence", EVIDENCE)], ids=["nodes-uuid", "evidence-evidence"]
)
def test_a_blocker_kind_and_id_that_agree_are_accepted(kind, identifier):
    """The check refuses a disagreement without narrowing what a real blocker may carry."""
    assert str(BlockingRecord(kind=kind, id=identifier).id) == identifier
