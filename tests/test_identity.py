"""Identity and timestamp codecs preserve the selected exact representation."""

import pytest

from justpen_knowledgebase_mcp.identity import format_timestamp, identity_json, identity_key, parse_timestamp

PARENT = "00000000-0000-4000-8000-000000000001"
OTHER_PARENT = "00000000-0000-4000-8000-000000000002"


def test_identity_ignores_order_and_nonidentity_fields():
    assert identity_key("nodes", "domain", {"value": "example.com"}) == identity_key(
        "nodes", "domain", {"note": "x", "value": "example.com"}
    )
    assert identity_key("relations", "resolves_to", {}) == ""


def test_scoped_identity_is_canonical_and_parent_sensitive():
    properties = {"transport": "tcp", "number": 443}
    assert identity_json("nodes", "port", properties, PARENT) == (
        '{"number":443,"parent":"00000000-0000-4000-8000-000000000001","transport":"tcp"}'
    )
    assert identity_key("nodes", "port", properties, PARENT) == identity_key(
        "nodes", "port", {"number": 443, "note": "ignored", "transport": "tcp"}, PARENT
    )
    assert identity_key("nodes", "port", properties, PARENT) != identity_key("nodes", "port", properties, OTHER_PARENT)


@pytest.mark.parametrize(
    ("type_name", "properties"),
    [
        ("port", {"transport": "tcp", "number": 443}),
        ("service", {"name": "unknown"}),
        ("finding", {"title": "Open management port", "severity": "high"}),
    ],
)
def test_scoped_identity_requires_parent(type_name, properties):
    with pytest.raises(ValueError, match="parent"):
        identity_key("nodes", type_name, properties)


def test_identity_strict_integer_and_types():
    with pytest.raises(ValueError):
        identity_key("nodes", "port", {"transport": "tcp", "number": 1.0}, PARENT)
    assert identity_key("nodes", "port", {"transport": "tcp", "number": 1}, PARENT)


def test_order_independent_relation_identity_hashes_declared_collection():
    first = {
        "flags": 0,
        "parameters": [
            {"name": "validationmethods", "value": "dns-01", "ignored": 1},
            {"name": "accounturi", "value": "https://ca.example/acct/1"},
        ],
    }
    second = {
        "flags": 0,
        "parameters": [
            {"value": "https://ca.example/acct/1", "name": "accounturi"},
            {"value": "dns-01", "name": "validationmethods", "ignored": 2},
        ],
    }
    assert identity_key("relations", "caa_issue", first) == identity_key("relations", "caa_issue", second)


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
