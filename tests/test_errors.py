"""Bounded public error codes."""

from justpen_knowledgebase_mcp import errors


def test_public_error_codes():
    assert (
        frozenset(
            {
                "INVALID",
                "NOT_FOUND",
                "CONFLICT",
                "BUSY",
                "LIMIT",
                "PATH_DENIED",
                "IO_ERROR",
                "INDEX_ERROR",
                "CANCELLED",
                "CONFIGURATION",
                "INTERNAL",
            }
        )
        == errors.VALID_ERROR_TYPES
    )
    assert errors.ConfigurationError.error_type == "CONFIGURATION"
    assert errors.McpError.error_type == "INTERNAL"


def test_every_public_code_has_an_exception_class():
    represented = {
        cls.error_type for cls in vars(errors).values() if isinstance(cls, type) and issubclass(cls, errors.McpError)
    }
    assert represented == errors.VALID_ERROR_TYPES
