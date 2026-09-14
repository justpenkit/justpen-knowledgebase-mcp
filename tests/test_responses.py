"""Tests for response envelope builders."""

import pytest

from justpen_knowledgebase_mcp.responses import error_response, success_response


def test_success_response_default_data():
    assert success_response() == {"status": "success", "data": {}}


def test_success_response_with_data():
    assert success_response({"foo": 1}) == {"status": "success", "data": {"foo": 1}}


def test_success_response_does_not_alias_default():
    a = success_response()
    a["data"]["mutated"] = True
    b = success_response()
    assert b["data"] == {}


def test_error_response_valid_type():
    assert error_response("invalid_params", "bad arg") == {
        "status": "error",
        "error_type": "invalid_params",
        "message": "bad arg",
    }


def test_error_response_unknown_type_raises():
    with pytest.raises(ValueError, match="Unknown error_type"):
        error_response("bogus_type", "x")
