"""The signature-rejection message is bounded and carries rules rather than submitted values.

`tests/tools/test_signature_envelope.py` holds the envelope against a running server; these cover
the branches a real call cannot reach, and they run in the unit gate rather than the integration
suite because the message is a pure function of a Pydantic report.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastmcp.exceptions import ValidationError as SignatureError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from justpen_knowledgebase_mcp.tools.request_presence import (
    _MAX_RULES,
    _MAX_SEGMENT,
    _signature_rejection,
    _signature_response,
)


class _Arguments(BaseModel):
    """Stand in for a tool signature: closed, width-bounded and carrying one authored rule."""

    model_config = ConfigDict(extra="forbid")
    first: Annotated[str, Field(max_length=4)] = "ok"
    second: Annotated[str, Field(max_length=4)] = "ok"
    third: Annotated[str, Field(max_length=4)] = "ok"
    fourth: str = "ok"

    @field_validator("fourth")
    @classmethod
    def authored(cls, value: str) -> str:
        """Raise the shape a server-authored validator raises on a tool signature."""
        raise ValueError("an authored rule")


def _rejection(payload: dict[str, object]) -> str:
    with pytest.raises(ValidationError) as caught:
        _Arguments.model_validate(payload)
    error = SignatureError("rejected")
    error.__cause__ = caught.value
    return _signature_rejection(error)


def test_an_authored_rule_loses_the_pydantic_prefix():
    assert _rejection({"fourth": "x"}) == "invalid tool request; fourth: an authored rule"


def test_an_invented_argument_name_is_truncated_rather_than_echoed():
    """`extra_forbidden` reports the key verbatim; it is the one path segment a client authors."""
    rejection = _rejection({"k" * 200: 1})

    assert "k" * _MAX_SEGMENT + "...: Extra inputs are not permitted" in rejection
    assert "k" * (_MAX_SEGMENT + 1) not in rejection


def test_a_report_of_many_rules_stays_bounded():
    rejection = _rejection({"first": "toolong", "second": "toolong", "third": "toolong", "fourth": "toolong"})

    assert rejection.count("; ") == _MAX_RULES
    assert "fourth" not in rejection


def test_an_unreadable_report_still_names_the_refusal():
    """FastMCP raises this error only for argument validation; a different cause must not guess."""
    error = SignatureError("rejected")
    error.__cause__ = RuntimeError("not a validation report")

    assert _signature_rejection(error) == "invalid tool request"


def test_the_response_is_the_public_error_envelope():
    error = SignatureError("rejected")
    error.__cause__ = None

    assert _signature_response(error) == {"status": "error", "error": "INVALID: invalid tool request"}
