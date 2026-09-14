"""Tests for the exception hierarchy and VALID_ERROR_TYPES set."""

from justpen_knowledgebase_mcp.errors import (
    VALID_ERROR_TYPES,
    DemoFailureError,
    InternalError,
    InvalidParamsError,
    McpError,
)


def test_mcp_error_base_default_type():
    assert McpError.error_type == "internal_error"


def test_invalid_params_error_type():
    err = InvalidParamsError("bad input")
    assert err.error_type == "invalid_params"
    assert str(err) == "bad input"


def test_internal_error_type():
    err = InternalError("boom")
    assert err.error_type == "internal_error"


def test_demo_failure_error_type():
    err = DemoFailureError("demo")
    assert err.error_type == "demo_failure"


def test_valid_error_types_contains_all_declared():
    assert {"invalid_params", "internal_error", "demo_failure"} == VALID_ERROR_TYPES


def test_all_error_classes_are_mcp_errors():
    for cls in (InvalidParamsError, InternalError, DemoFailureError):
        assert issubclass(cls, McpError)
