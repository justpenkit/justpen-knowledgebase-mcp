"""Identity and timestamp codecs preserve the selected exact representation."""

import pytest

from justpen_knowledgebase_mcp.identity import format_timestamp, identity_key, parse_timestamp


def test_identity_ignores_order_and_nonidentity_fields():
    assert identity_key("nodes", "application", {"sha256": "a" * 64, "platform": "android"}) == identity_key(
        "nodes", "application", {"note": "x", "platform": "linux", "sha256": "a" * 64}
    )
    assert identity_key("relations", "signed_by", {}) == ""


def test_identity_strict_integer_and_types():
    with pytest.raises(ValueError):
        identity_key("nodes", "service", {"host": "x", "transport": "tcp", "port": 1.0})
    assert identity_key("nodes", "service", {"host": "x", "transport": "tcp", "port": 1})


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01t00:00:00Z",
        "2026-01-01T00:00:00z",
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:60Z",
        "2026-01-01T00:00:00-00:00",
        "2026-02-30T00:00:00Z",
        "2026-01-01T00:00:00.1234567Z",
        "0001-01-01T00:00:00+00:01",
        "9999-12-31T23:59:59-00:01",
    ],
)
def test_invalid_timestamp(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_timestamp_normalizes_only_metadata_to_utc_microseconds():
    assert parse_timestamp("1970-01-01T01:00:00.123456+01:00") == 123456
    assert format_timestamp(123456) == "1970-01-01T00:00:00.123456Z"
    assert format_timestamp(parse_timestamp("0001-01-01T00:00:00Z")) == "0001-01-01T00:00:00.000000Z"
