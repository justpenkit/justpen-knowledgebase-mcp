"""Public envelopes reject arbitrary error details."""

import pytest

from justpen_knowledgebase_mcp.responses import ErrorResult, error_response, exception_response, success_response


def test_success_envelope():
    assert success_response({"n": 1}) == {"status": "ok", "data": {"n": 1}}


def test_error_envelope():
    assert error_response("INVALID", "bad input") == {"status": "error", "error": "INVALID: bad input"}


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
