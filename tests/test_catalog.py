"""Canonical format descriptions remain distinct for discovery consumers."""

import pytest

from justpen_knowledgebase_mcp.catalog import catalog_manifest, catalog_schema, validate_record


def test_alpn_description_does_not_include_sni_rules():
    formats = catalog_manifest()["formats"]
    assert "SNI" not in formats["alpn_list"]
    assert "ALPN" in formats["alpn_list"]
    assert "IP literals rejected" in formats["dns_or_explicit_empty"]


def test_identity_fields_are_required_and_extras_survive():
    for group in ("nodes", "relations"):
        for definition in catalog_manifest()[group].values():
            assert set(definition["identity"]) <= set(definition["required"])
    properties = {"address": "2001:db8::1", "extra": {"score": 1.5, "note": None}}
    validate_record("nodes", "ip", properties)
    assert properties["extra"] == {"score": 1.5, "note": None}


@pytest.mark.parametrize(
    ("kind", "properties"),
    [
        ("ip", {"address": "2001:0db8::1"}),
        ("ip", {"address": "::ffff:192.0.2.1"}),
        ("ip", {"address": "fe80::1%en0"}),
        ("hostname", {"name": "API.example.com"}),
        ("hostname", {"name": "192.0.2.1"}),
        ("hostname", {"name": "api.example.com."}),
        ("service", {"host": "example.com", "transport": "tcp", "port": True}),
        ("service", {"host": "example.com", "transport": "tcp", "port": "443"}),
        ("application", {"sha256": "a" * 64}),
        ("unknown", {}),
    ],
)
def test_invalid_required_properties(kind, properties):
    with pytest.raises(ValueError):
        validate_record("nodes", kind, properties)


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://example.com/",
        "https://API.example.com/",
        "https://example.com:443/",
        "https://example.com",
        "https://example.com/%2f",
        "https://example.com/?",
        "https://user@example.com/",
        "https://example.com/#a",
        "https://example.com/../a",
        "https://example.com/é",
        "https://example.com/\\a",
        "https://example.com/%GG",
    ],
)
def test_url_rejects_nonselected_spelling(url):
    with pytest.raises(ValueError):
        validate_record("nodes", "endpoint", {"url": url, "method": "GET"})


@pytest.mark.parametrize(
    "url", ["https://api.example.com/a%2Fb?q=X", "https://[2001:db8::1]:8443/", "http://x/%2E?b=2&a=1"]
)
def test_url_retains_valid_literal(url):
    properties = {"url": url, "method": "PROPFIND"}
    validate_record("nodes", "endpoint", properties)
    assert properties["url"] == url


@pytest.mark.parametrize(
    ("kind", "properties"),
    [
        ("ip", {"address": "::ffff:c000:201"}),
        ("domain", {"name": "xn--bcher-kva.example"}),
        ("service", {"host": "2001:db8::1", "transport": "udp", "port": 65535}),
        ("certificate", {"der_sha256": "a" * 64}),
        ("principal", {"realm": "EXAMPLE", "name": "Alice Smith", "kind": "group"}),
        (
            "credential_hint",
            {
                "realm": "https://x/",
                "subject": "payment",
                "kind": "api_key",
                "location": "apk-sha256:" + "b" * 64 + "/config",
                "selector": "",
            },
        ),
        (
            "finding",
            {
                "rule_namespace": "team",
                "rule_id": "x",
                "location": "https://x/",
                "selector": "",
                "title": "Finding",
                "severity": "info",
            },
        ),
    ],
)
def test_remaining_catalog_shapes(kind, properties):
    validate_record("nodes", kind, properties)


@pytest.mark.parametrize(
    "properties",
    [
        {"vantage": "vpn", "server_name": "", "mode": "tls", "alpn_offered": ""},
        {"vantage": "vpn", "server_name": "x", "mode": "tls", "alpn_offered": "h2,http/1.1"},
    ],
)
def test_required_certificate_handshake_context(properties):
    validate_record("relations", "presents_certificate", properties)


@pytest.mark.parametrize("alpn", ["h2,h2", "h2, http/1.1", "h2,", "a" * 256])
def test_invalid_alpn_offer(alpn):
    with pytest.raises(ValueError):
        validate_record(
            "relations",
            "presents_certificate",
            {"vantage": "vpn", "server_name": "", "mode": "tls", "alpn_offered": alpn},
        )


def test_discovery_schema_exposes_types_identity_and_free_properties():

    schema = catalog_schema("nodes", "service")
    assert schema["additionalProperties"] is True
    assert schema["properties"]["port"] == {"type": "integer", "minimum": 1, "maximum": 65535, "format": "port"}
    assert schema["properties"]["transport"]["enum"] == ["tcp", "udp"]
    assert set(schema["required"]) == {"host", "transport", "port"}


def test_numeric_single_dns_label_is_not_a_dotted_address():
    validate_record("nodes", "hostname", {"name": "123"})
