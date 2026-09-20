"""Catalog v2 manifest, schema, and strict validator contracts."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

import justpen_knowledgebase_mcp.catalog as catalog_module
from justpen_knowledgebase_mcp.catalog import (
    CATALOG_FINGERPRINT,
    CATALOG_VERSION,
    catalog_manifest,
    catalog_schema,
    validate_record,
)
from justpen_knowledgebase_mcp.errors import ExpectedValidationError
from justpen_knowledgebase_mcp.identity import identity_key

CPE_NGINX = "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*"

NODE_TYPES = {
    "domain",
    "subdomain",
    "ip_address",
    "ip_cidr",
    "asn",
    "spf_record",
    "port",
    "service",
    "finding",
    "certificate",
    "endpoint",
    "cve",
    "technology",
    "dmarc_record",
    "txt_record",
    "tls_cipher_suite",
    "dkim_record",
    "parameter",
    "tls_fingerprint",
    "registrar",
}
RELATION_TYPES = {
    "resolves_to",
    "has_subdomain",
    "cname_to",
    "dname_to",
    "contains_ip",
    "contains_cidr",
    "announced_by",
    "has_nameserver",
    "has_soa_primary",
    "reverse_resolves_to",
    "has_mail_exchange",
    "has_srv_target",
    "caa_issue",
    "caa_issuewild",
    "has_spf",
    "has_open_port",
    "has_service",
    "has_finding",
    "presents_certificate",
    "serves_endpoint",
    "redirects_to",
    "affected_by",
    "runs_technology",
    "protected_by",
    "has_dmarc",
    "has_txt_record",
    "supports_tls_cipher",
    "covers_name",
    "has_svcb_binding",
    "issued_by",
    "has_dkim_selector",
    "has_parameter",
    "has_tls_fingerprint",
    "registered_through",
}


def _valid(kind: str, type_name: str, properties: dict[str, object]) -> None:
    validate_record(kind, type_name, properties)


def _invalid(kind: str, type_name: str, properties: dict[str, object]) -> None:
    with pytest.raises(ExpectedValidationError):
        validate_record(kind, type_name, properties)


def test_manifest_has_only_catalog_v2_types_and_stable_fingerprint() -> None:
    manifest = catalog_manifest()

    assert CATALOG_VERSION == 2
    assert manifest["version"] == 2
    assert set(manifest["nodes"]) == NODE_TYPES
    assert set(manifest["relations"]) == RELATION_TYPES
    assert CATALOG_FINGERPRINT == "1f843e1beed1ef44063555cee189e803f866acd0a042655126908ffaa4f27ee1"


def test_fingerprint_computation_eagerly_loads_both_bundled_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def classify(name: str) -> str:
        calls.append(("psl", name))
        return "domain"

    def service_name(name: str) -> bool:
        calls.append(("service", name))
        return True

    monkeypatch.setattr(catalog_module, "classify_dns_name", classify)
    monkeypatch.setattr(catalog_module, "is_service_name", service_name)

    assert catalog_module._catalog_fingerprint() == CATALOG_FINGERPRINT
    assert calls == [("psl", "example.com"), ("service", "unknown")]


def test_fingerprint_computation_propagates_bundled_loader_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_closed(_name: str) -> str:
        raise RuntimeError("corrupted snapshot")

    monkeypatch.setattr(catalog_module, "classify_dns_name", fail_closed)

    with pytest.raises(RuntimeError, match="corrupted snapshot"):
        catalog_module._catalog_fingerprint()


def test_manifest_declares_property_and_parent_scoped_identity() -> None:
    manifest = catalog_manifest()

    assert manifest["nodes"]["ip_address"]["identity"] == {"properties": ["value"]}
    assert manifest["relations"]["has_mail_exchange"]["identity"] == {"properties": ["preference"]}
    assert manifest["nodes"]["port"]["identity"] == {
        "properties": ["transport", "number"],
        "scope": {"relation": "has_open_port", "endpoint": "source"},
    }
    assert manifest["nodes"]["service"]["identity"] == {
        "properties": ["name"],
        "scope": {"relation": "has_service", "endpoint": "source"},
    }
    assert manifest["nodes"]["finding"]["identity"] == {
        "properties": ["title"],
        "scope": {"relation": "has_finding", "endpoint": "source"},
    }


@pytest.mark.parametrize(
    ("type_name", "properties"),
    [
        ("domain", {"value": "example.com"}),
        ("domain", {"value": "example.co.uk"}),
        ("domain", {"value": "corp.internal"}),
        ("domain", {"value": "xn--bcher-kva.example"}),
        ("subdomain", {"value": "api.example.com"}),
        ("subdomain", {"value": "db.dev.corp.internal"}),
        ("subdomain", {"value": "_sip._tcp.example.com"}),
        ("subdomain", {"value": "xn--bcher-kva.example.com"}),
        ("subdomain", {"value": f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 61}"}),
        ("ip_address", {"value": "192.0.2.1", "version": 4}),
        ("ip_address", {"value": "2001:db8::1", "version": 6}),
        ("ip_cidr", {"value": "0.0.0.0/0", "version": 4}),
        ("ip_cidr", {"value": "2001:db8::/128", "version": 6}),
        ("asn", {"value": 0}),
        ("asn", {"value": 4294967295}),
        ("spf_record", {"value": "v=spf1"}),
        ("spf_record", {"value": "v=spf1  include:example.com -all"}),
        ("port", {"number": 0, "transport": "tcp"}),
        ("port", {"number": 65535, "transport": "sctp"}),
        ("service", {"name": "http", "secure": True}),
        ("service", {"name": "http", "secure": False}),
        ("service", {"name": "ssh"}),
        ("service", {"name": "ssh", "secure": "observed"}),
        ("service", {"name": "unknown"}),
        ("finding", {"title": "x", "severity": "info"}),
        ("finding", {"title": "x" * 200, "severity": "critical"}),
        ("certificate", {"der_sha256": "a" * 64}),
        ("endpoint", {"url": "https://api.example.com/a%2Fb?q=X", "method": "PROPFIND"}),
        ("endpoint", {"url": "https://example.com/?", "method": "GET"}),
        ("endpoint", {"url": "https://[2001:db8::1]:8443/", "method": "GET"}),
        ("endpoint", {"url": "http://x/", "method": "M" * 32}),
        ("cve", {"value": "CVE-2026-1234"}),
        ("cve", {"value": "CVE-1999-1234567"}),
        ("technology", {"name": "nginx"}),
        ("technology", {"name": "a"}),
        ("technology", {"name": "a" * 63}),
        ("technology", {"name": "php_7.4+x", "cpe": CPE_NGINX, "categories": ["web-server"]}),
        ("dmarc_record", {"value": "v=DMARC1"}),
        ("dmarc_record", {"value": "v=DMARC1; p=reject; rua=mailto:dmarc@example.com"}),
        ("dmarc_record", {"value": "v=DMARC1 p=none"}),
        ("dmarc_record", {"value": "v=DMARC1;" + "x" * 4087}),
        ("txt_record", {"value": "google-site-verification=abc"}),
        ("txt_record", {"value": "x"}),
        ("txt_record", {"value": "x" * 4096}),
        ("txt_record", {"value": "V=SPF1 -all"}),
        ("txt_record", {"value": "V=DMARC1; p=none"}),
        ("tls_cipher_suite", {"version": "tls13", "name": "TLS_AES_128_GCM_SHA256"}),
        ("tls_cipher_suite", {"version": "tls12", "name": "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256"}),
        ("tls_cipher_suite", {"version": "ssl30", "name": "TLS_RSA_WITH_3DES_EDE_CBC_SHA"}),
        ("tls_cipher_suite", {"version": "dtls13", "name": "TLS_NULL_WITH_NULL_NULL"}),
        ("dkim_record", {"selector": "default", "value": "v=DKIM1; k=rsa; p=MIGf"}),
        ("dkim_record", {"selector": "s1", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "selector1.sub", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "a" * 63, "value": "p=MIGf"}),
        ("parameter", {"name": "id", "location": "query"}),
        ("parameter", {"name": "X-Request-Id", "location": "header"}),
        ("parameter", {"name": "a" * 128, "location": "body"}),
        ("parameter", {"name": "%20foo", "location": "path"}),
        ("parameter", {"name": "session", "location": "cookie", "reflected": True}),
        (
            "tls_fingerprint",
            {"kind": "jarm", "value": "22222222222222222222222222222222222222222222222222222222222222"},
        ),
        ("tls_fingerprint", {"kind": "ja3s", "value": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}),
        ("registrar", {"iana_id": 292, "name": "MarkMonitor Inc."}),
        ("registrar", {"iana_id": 1, "name": "a"}),
        ("registrar", {"iana_id": 65535, "name": "x" * 200}),
    ],
)
def test_valid_node_fields_and_boundaries(type_name: str, properties: dict[str, object]) -> None:
    _valid("nodes", type_name, properties)


@pytest.mark.parametrize(
    ("type_name", "properties"),
    [
        ("domain", {"value": "api.example.com"}),
        ("subdomain", {"value": "example.com"}),
        ("domain", {"value": "localhost"}),
        ("subdomain", {"value": "localhost"}),
        ("domain", {"value": "com"}),
        ("domain", {"value": "Example.com"}),
        ("domain", {"value": "example.com."}),
        ("domain", {"value": "-example.com"}),
        ("domain", {"value": "example..com"}),
        ("domain", {"value": f"{'a' * 64}.com"}),
        ("subdomain", {"value": f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}"}),
        ("domain", {"value": 1}),
        ("subdomain", {"value": "_sip.example_.com"}),
        ("subdomain", {"value": "xn--a.example.com"}),
        ("ip_address", {"value": "192.0.2.1", "version": 6}),
        ("ip_address", {"value": "2001:db8::1", "version": 4}),
        ("ip_address", {"value": "2001:0db8::1", "version": 6}),
        ("ip_address", {"value": "2001:DB8::1", "version": 6}),
        ("ip_address", {"value": "fe80::1%en0", "version": 6}),
        ("ip_address", {"value": "192.0.2.1/24", "version": 4}),
        ("ip_address", {"value": "192.168.001.1", "version": 4}),
        ("ip_address", {"value": "192.0.2.1", "version": True}),
        ("ip_address", {"value": "192.0.2.1", "version": "4"}),
        ("ip_cidr", {"value": "192.0.2.1/24", "version": 4}),
        ("ip_cidr", {"value": "192.0.2.0", "version": 4}),
        ("ip_cidr", {"value": "192.0.2.0/255.255.255.0", "version": 4}),
        ("ip_cidr", {"value": "2001:DB8::/32", "version": 6}),
        ("ip_cidr", {"value": "192.0.2.0/24", "version": 6}),
        ("ip_cidr", {"value": "192.0.2.0/24", "version": False}),
        ("asn", {"value": -1}),
        ("asn", {"value": 4294967296}),
        ("asn", {"value": True}),
        ("asn", {"value": "64512"}),
        ("spf_record", {"value": "v=spf10"}),
        ("spf_record", {"value": " v=spf1"}),
        ("spf_record", {"value": "v=spf1\t-all"}),
        ("spf_record", {"value": "v=spf1\n-all"}),
        ("spf_record", {"value": "v=spf1 é"}),
        ("port", {"number": -1, "transport": "tcp"}),
        ("port", {"number": 65536, "transport": "tcp"}),
        ("port", {"number": True, "transport": "tcp"}),
        ("port", {"number": "443", "transport": "tcp"}),
        ("port", {"number": 443, "transport": "TCP"}),
        ("service", {"name": "http"}),
        ("service", {"name": "http", "secure": 1}),
        ("service", {"name": "X11"}),
        ("service", {"name": "ssl/http"}),
        ("service", {"name": "definitely-not-registered"}),
        ("finding", {"title": "", "severity": "low"}),
        ("finding", {"title": "x" * 201, "severity": "low"}),
        ("finding", {"title": "line\nbreak", "severity": "low"}),
        ("finding", {"title": 1, "severity": "low"}),
        ("finding", {"title": "title", "severity": "unknown"}),
        ("certificate", {"der_sha256": "A" * 64}),
        ("certificate", {"der_sha256": "a" * 63}),
        ("endpoint", {"url": "HTTPS://example.com/", "method": "GET"}),
        ("endpoint", {"url": "https://API.example.com/", "method": "GET"}),
        ("endpoint", {"url": "https://example.com:443/", "method": "GET"}),
        ("endpoint", {"url": "https://example.com", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/%2f", "method": "GET"}),
        ("endpoint", {"url": "https://user@example.com/", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/#a", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/../a", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/é", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/\\a", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/%GG", "method": "GET"}),
        ("endpoint", {"url": "https://192.168.001.1/", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/", "method": "get"}),
        ("endpoint", {"url": "https://example.com/", "method": "M" * 33}),
        ("cve", {"value": "cve-2026-1234"}),
        ("cve", {"value": "CVE-26-1234"}),
        ("cve", {"value": "CVE-2026-123"}),
        ("cve", {"value": 20261234}),
        ("technology", {"name": "Nginx"}),
        ("technology", {"name": "NGINX"}),
        ("technology", {"name": ""}),
        ("technology", {"name": "-nginx"}),
        ("technology", {"name": "nginx-"}),
        ("technology", {"name": "a" * 64}),
        ("technology", {"name": "ngin x"}),
        ("technology", {"name": 1}),
        ("dmarc_record", {"value": "v=DMARC10"}),
        ("dmarc_record", {"value": "V=DMARC1; p=none"}),
        ("dmarc_record", {"value": "v=dmarc1; p=none"}),
        ("dmarc_record", {"value": " v=DMARC1"}),
        ("dmarc_record", {"value": "v=DMARC1;\tp=none"}),
        ("dmarc_record", {"value": "v=DMARC1; p=none é"}),
        ("dmarc_record", {"value": "v=DMARC1;" + "x" * 4088}),
        ("dmarc_record", {"value": ""}),
        ("dmarc_record", {"value": 1}),
        ("txt_record", {"value": ""}),
        ("txt_record", {"value": "x" * 4097}),
        ("txt_record", {"value": "line\nbreak"}),
        ("txt_record", {"value": "café"}),
        ("txt_record", {"value": 1}),
        ("txt_record", {"value": "v=spf1 -all"}),
        ("txt_record", {"value": "v=DMARC1; p=none"}),
        ("txt_record", {"value": "v=DKIM1; k=rsa; p=MIGf"}),
        ("tls_cipher_suite", {"version": "ssl20", "name": "TLS_RSA_WITH_3DES_EDE_CBC_SHA"}),
        ("tls_cipher_suite", {"version": "TLS13", "name": "TLS_AES_128_GCM_SHA256"}),
        ("tls_cipher_suite", {"version": "tls12", "name": "ECDHE-RSA-AES128-GCM-SHA256"}),
        ("tls_cipher_suite", {"version": "tls12", "name": "SSL_CK_RC4_128_WITH_MD5"}),
        ("tls_cipher_suite", {"version": "tls13", "name": "tls_aes_128_gcm_sha256"}),
        ("tls_cipher_suite", {"version": "tls13", "name": "TLS_"}),
        ("tls_cipher_suite", {"version": "tls13", "name": "TLS_AES__128_GCM_SHA256"}),
        ("tls_cipher_suite", {"version": "tls13", "name": "TLS_A" + "_A" * 100}),
        ("tls_cipher_suite", {"version": "tls13", "name": 1}),
        ("dkim_record", {"selector": "s1._domainkey", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "_domainkey", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "Default", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "a" * 64, "value": "p=MIGf"}),
        ("dkim_record", {"selector": "s1-", "value": "p=MIGf"}),
        ("dkim_record", {"selector": "s1", "value": ""}),
        ("dkim_record", {"selector": "s1"}),
        ("dkim_record", {"selector": 1, "value": "p=MIGf"}),
        ("parameter", {"name": "id=1&page=2", "location": "query"}),
        ("parameter", {"name": "id=1", "location": "query"}),
        ("parameter", {"name": "id#frag", "location": "query"}),
        ("parameter", {"name": "two words", "location": "query"}),
        ("parameter", {"name": "", "location": "query"}),
        ("parameter", {"name": "a" * 129, "location": "query"}),
        ("parameter", {"name": "café", "location": "query"}),
        ("parameter", {"name": "id", "location": "GET"}),
        ("parameter", {"name": "id"}),
        ("parameter", {"name": 1, "location": "query"}),
        ("tls_fingerprint", {"kind": "jarm", "value": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}),
        (
            "tls_fingerprint",
            {"kind": "ja3s", "value": "22222222222222222222222222222222222222222222222222222222222222"},
        ),
        ("tls_fingerprint", {"kind": "jarm", "value": "2" * 61}),
        ("tls_fingerprint", {"kind": "ja3s", "value": "A" * 32}),
        ("tls_fingerprint", {"kind": "ja3", "value": "a" * 32}),
        ("tls_fingerprint", {"value": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}),
        ("registrar", {"iana_id": 0, "name": "unset"}),
        ("registrar", {"iana_id": -1, "name": "negative"}),
        ("registrar", {"iana_id": 65536, "name": "over"}),
        ("registrar", {"iana_id": 292}),
        ("registrar", {"iana_id": "292", "name": "string id"}),
    ],
)
def test_invalid_node_types_bounds_and_noncanonical_spellings(type_name: str, properties: dict[str, object]) -> None:
    _invalid("nodes", type_name, properties)


def test_endpoint_url_drops_its_query_string_before_identity_and_storage() -> None:
    """One path is one endpoint: parameter values must not mint a node each."""
    keys = set()
    for url in (
        "https://api.example.com/search?q=1",
        "https://api.example.com/search?q=2&page=3",
        "https://api.example.com/search",
    ):
        properties: dict[str, object] = {"url": url, "method": "GET"}
        keys.add(identity_key("nodes", "endpoint", properties))
        assert properties["url"] == "https://api.example.com/search"
    assert len(keys) == 1
    trailing: dict[str, object] = {"url": "https://api.example.com/?", "method": "GET"}
    validate_record("nodes", "endpoint", trailing)
    assert trailing["url"] == "https://api.example.com/"


def test_canonicalization_leaves_every_other_type_and_property_untouched() -> None:
    properties: dict[str, object] = {"url": "https://api.example.com/a?b=1", "method": "GET", "title": "Search?x"}
    validate_record("nodes", "endpoint", properties)
    assert properties["title"] == "Search?x"
    untouched: dict[str, object] = {"value": "google-site-verification=a?b"}
    validate_record("nodes", "txt_record", untouched)
    assert untouched["value"] == "google-site-verification=a?b"


def test_extras_survive_and_properties_size_bound_is_retained() -> None:
    properties: dict[str, object] = {
        "value": "192.0.2.1",
        "version": 4,
        "extra": {"score": 1.5, "note": None},
    }

    _valid("nodes", "ip_address", properties)
    assert properties["extra"] == {"score": 1.5, "note": None}
    _invalid("nodes", "spf_record", {"value": "v=spf1", "extra": "x" * 65536})


def test_relation_endpoint_matrices_are_exact() -> None:
    d = ["domain", "subdomain"]
    matrices: dict[str, tuple[list[str], list[str]]] = {
        "resolves_to": (d, ["ip_address"]),
        "has_subdomain": (d, ["subdomain"]),
        "cname_to": (d, d),
        "dname_to": (d, d),
        "contains_ip": (["ip_cidr"], ["ip_address"]),
        "contains_cidr": (["ip_cidr"], ["ip_cidr"]),
        "announced_by": (["ip_cidr"], ["asn"]),
        "has_nameserver": (d, d),
        "has_soa_primary": (d, d),
        "reverse_resolves_to": (["ip_address"], d),
        "has_mail_exchange": (d, d),
        "has_srv_target": (d, d),
        "caa_issue": (d, d),
        "caa_issuewild": (d, d),
        "has_spf": (d, ["spf_record"]),
        "has_open_port": (["ip_address"], ["port"]),
        "has_service": (["port"], ["service"]),
        "has_finding": (
            ["port", "domain", "subdomain", "ip_address", "ip_cidr", "service", "endpoint", "certificate"],
            ["finding"],
        ),
        "presents_certificate": (["service"], ["certificate"]),
        "serves_endpoint": (["service"], ["endpoint"]),
        "redirects_to": (["endpoint"], ["endpoint"]),
        "affected_by": (["service", "finding"], ["cve"]),
        "runs_technology": (["service", "endpoint"], ["technology"]),
        "protected_by": (["service", "endpoint"], ["technology"]),
        "has_dmarc": (d, ["dmarc_record"]),
        "has_dkim_selector": (d, ["dkim_record"]),
        "has_parameter": (["endpoint"], ["parameter"]),
        "has_tls_fingerprint": (["service"], ["tls_fingerprint"]),
        "registered_through": (["domain"], ["registrar"]),
        "has_txt_record": (d, ["txt_record"]),
        "supports_tls_cipher": (["service"], ["tls_cipher_suite"]),
        "covers_name": (["certificate"], d),
        "has_svcb_binding": (d, d),
        "issued_by": (["certificate"], ["certificate"]),
    }
    relations = catalog_manifest()["relations"]

    assert set(relations) == set(matrices)
    for type_name, (sources, targets) in matrices.items():
        assert relations[type_name]["sources"] == sources
        assert relations[type_name]["targets"] == targets


@pytest.mark.parametrize(
    ("type_name", "properties"),
    [
        ("resolves_to", {}),
        ("has_mail_exchange", {"preference": 0}),
        ("has_mail_exchange", {"preference": 65535}),
        (
            "has_srv_target",
            {"service": "_ldap", "protocol": "_tcp", "port": 0, "priority": 65535, "weight": 20},
        ),
        (
            "has_srv_target",
            {"service": "_a", "protocol": "_" + "b" * 62, "port": 65535, "priority": 0, "weight": 65535},
        ),
        ("caa_issue", {"flags": 0, "parameters": []}),
        (
            "caa_issuewild",
            {
                "flags": 255,
                "parameters": [
                    {"name": "validationmethods", "value": "dns-01", "ignored": True},
                    {"name": "accounturi", "value": ""},
                ],
            },
        ),
        ("presents_certificate", {"mode": "starttls", "server_name": "", "alpn_offered": []}),
        (
            "presents_certificate",
            {"mode": "tls", "server_name": "api.example.com", "alpn_offered": ["h2", "h2", "http/1.1"]},
        ),
        ("presents_certificate", {"mode": "tls", "server_name": "", "alpn_offered": ["a" * 255] * 5}),
        ("redirects_to", {"status": 301}),
        ("redirects_to", {"status": 308}),
        ("affected_by", {}),
        ("runs_technology", {}),
        ("runs_technology", {"version": "1.18.0", "cpe": CPE_NGINX}),
        ("protected_by", {"kind": "waf"}),
        ("protected_by", {"kind": "load_balancer"}),
        ("has_dmarc", {}),
        ("has_txt_record", {}),
        ("supports_tls_cipher", {"preferred": True, "curve": "x25519"}),
        ("covers_name", {"coverage": "exact"}),
        ("covers_name", {"coverage": "wildcard"}),
        ("has_svcb_binding", {"record_type": "https", "priority": 1, "alpn": ["h2", "h3"]}),
        ("has_svcb_binding", {"record_type": "svcb", "priority": 0, "alpn": []}),
        ("has_svcb_binding", {"record_type": "https", "priority": 65535, "alpn": ["h3"]}),
        ("issued_by", {}),
        ("presents_certificate", {"mode": "quic", "server_name": "example.com", "alpn_offered": ["h3"]}),
    ],
)
def test_valid_relation_fields_and_boundaries(type_name: str, properties: dict[str, object]) -> None:
    _valid("relations", type_name, properties)


@pytest.mark.parametrize(
    ("type_name", "properties"),
    [
        ("has_mail_exchange", {"preference": -1}),
        ("has_mail_exchange", {"preference": 65536}),
        ("has_mail_exchange", {"preference": True}),
        ("has_mail_exchange", {"preference": "10"}),
        (
            "has_srv_target",
            {"service": "ldap", "protocol": "_tcp", "port": 389, "priority": 0, "weight": 0},
        ),
        (
            "has_srv_target",
            {"service": "_LDAP", "protocol": "_tcp", "port": 389, "priority": 0, "weight": 0},
        ),
        (
            "has_srv_target",
            {"service": "_ldap-", "protocol": "_tcp", "port": 389, "priority": 0, "weight": 0},
        ),
        (
            "has_srv_target",
            {"service": "_" + "a" * 63, "protocol": "_tcp", "port": 389, "priority": 0, "weight": 0},
        ),
        (
            "has_srv_target",
            {"service": "_ldap", "protocol": "_tcp", "port": True, "priority": 0, "weight": 0},
        ),
        ("caa_issue", {"flags": 256, "parameters": []}),
        ("caa_issue", {"flags": False, "parameters": []}),
        ("caa_issue", {"flags": 0, "parameters": {}}),
        ("caa_issue", {"flags": 0, "parameters": [{"name": "-bad", "value": "dns-01"}]}),
        ("caa_issue", {"flags": 0, "parameters": [{"name": "ok", "value": "has space"}]}),
        ("caa_issue", {"flags": 0, "parameters": [{"name": "ok", "value": "bad;value"}]}),
        ("caa_issue", {"flags": 0, "parameters": [{"name": "ok"}]}),
        ("presents_certificate", {"mode": "TLS", "server_name": "", "alpn_offered": []}),
        ("presents_certificate", {"mode": "tls", "server_name": "192.0.2.1", "alpn_offered": []}),
        ("presents_certificate", {"mode": "tls", "server_name": "", "alpn_offered": "h2"}),
        ("presents_certificate", {"mode": "tls", "server_name": "", "alpn_offered": [""]}),
        ("presents_certificate", {"mode": "tls", "server_name": "", "alpn_offered": ["h2 "]}),
        ("presents_certificate", {"mode": "tls", "server_name": "", "alpn_offered": ["é"]}),
        ("redirects_to", {"status": 300}),
        ("redirects_to", {"status": True}),
        ("redirects_to", {"status": "301"}),
        ("protected_by", {}),
        ("protected_by", {"kind": "WAF"}),
        ("protected_by", {"kind": "ids"}),
        ("protected_by", {"kind": 1}),
        ("covers_name", {}),
        ("covers_name", {"coverage": "Exact"}),
        ("covers_name", {"coverage": "san"}),
        ("has_svcb_binding", {"record_type": "https", "priority": 1}),
        ("has_svcb_binding", {"record_type": "HTTPS", "priority": 1, "alpn": ["h2"]}),
        ("has_svcb_binding", {"record_type": "https", "priority": -1, "alpn": ["h2"]}),
        ("has_svcb_binding", {"record_type": "https", "priority": 65536, "alpn": ["h2"]}),
        ("has_svcb_binding", {"record_type": "https", "priority": 1, "alpn": "h2,h3"}),
        ("has_svcb_binding", {"record_type": "https", "priority": 1, "alpn": ["h2 h3"]}),
        ("presents_certificate", {"mode": "QUIC", "server_name": "", "alpn_offered": []}),
    ],
)
def test_invalid_relation_types_bounds_and_grammars(type_name: str, properties: dict[str, object]) -> None:
    _invalid("relations", type_name, properties)


@pytest.mark.parametrize(
    "value",
    [
        "",
        CPE_NGINX,
        "cpe:2.3:a:apache:http_server:2.4.41:*:*:*:*:*:*:*",
        "cpe:2.3:a:vendor:product:8.???:*:*:*:*:*:*:*",
        "cpe:2.3:a:vendor:pro\\:duct:*:*:*:*:*:*:*:*",
        "cpe:2.3:o:-:-:-:-:-:-:-:-:-:-",
    ],
)
def test_cpe23_rule_accepts_the_formatted_string_binding(value: str) -> None:
    assert catalog_module._valid_field(value, "cpe23_or_empty")


@pytest.mark.parametrize(
    "value",
    [
        "cpe:/a:apache:http_server:2.4.41",
        "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*",
        "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*:*",
        "cpe:2.3:x:f5:nginx:1.18.0:*:*:*:*:*:*:*",
        "CPE:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*",
        "cpe:2.3:a:f5:NGINX:1.18.0:*:*:*:*:*:*:*",
        "cpe:2.3:a:f5:" + "n" * 512 + ":*:*:*:*:*:*:*:*",
    ],
)
def test_cpe23_rule_rejects_legacy_uri_and_malformed_bindings(value: str) -> None:
    assert not catalog_module._valid_field(value, "cpe23_or_empty")


def test_cpe23_rule_is_published_without_a_consuming_required_map() -> None:
    manifest = catalog_manifest()
    referenced = {
        rule
        for kind in ("nodes", "relations")
        for definition in manifest[kind].values()
        for rule in definition["required"].values()
        if isinstance(rule, str)
    }

    assert "cpe23_or_empty" in manifest["formats"]
    assert "cpe23_or_empty" not in referenced


def test_every_order_independent_property_is_required_by_its_own_type() -> None:
    """An order-independent property missing from `required` is not a validation failure: it reaches
    _order_independent_hash and raises a bare KeyError, which surfaces as INTERNAL."""
    manifest = catalog_manifest()
    for kind in ("nodes", "relations"):
        for type_name, definition in manifest[kind].items():
            rule = definition["identity"].get("order_independent")
            if rule is not None:
                assert rule["property"] in definition["required"], f"{kind}.{type_name}"
                assert rule["property"] in definition["identity"]["properties"], f"{kind}.{type_name}"


def test_caa_and_alpn_identity_declarations_are_order_independent() -> None:
    relations = catalog_manifest()["relations"]
    expected_caa = {
        "property": "parameters",
        "algorithm": "sha256",
        "projection": ["name", "value"],
        "sort": ["name", "value"],
        "preserve_duplicates": True,
    }
    for type_name in ("caa_issue", "caa_issuewild"):
        identity = relations[type_name]["identity"]
        assert identity["order_independent"] == expected_caa
    assert relations["presents_certificate"]["identity"]["order_independent"] == {
        "property": "alpn_offered",
        "algorithm": "sha256",
        "sort": "value",
        "preserve_duplicates": True,
    }


def test_discovery_schema_exposes_json_types_identity_and_limits() -> None:
    port_schema = catalog_schema("nodes", "port")
    caa_schema = catalog_schema("relations", "caa_issue")
    certificate_schema = catalog_schema("relations", "presents_certificate")

    assert port_schema["additionalProperties"] is True
    assert port_schema["properties"]["number"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 65535,
        "format": "uint16",
    }
    assert port_schema["properties"]["transport"]["enum"] == ["tcp", "udp", "sctp"]
    assert port_schema["x-identity"] == {
        "properties": ["transport", "number"],
        "scope": {"relation": "has_open_port", "endpoint": "source"},
    }
    assert caa_schema["properties"]["parameters"]["type"] == "array"
    assert certificate_schema["properties"]["alpn_offered"]["type"] == "array"
    assert port_schema["x-maxUtf8Bytes"] == 65536
    assert port_schema["x-maxDepth"] == 16
    technology_schema = catalog_schema("nodes", "technology")
    assert technology_schema["properties"]["name"] == {"type": "string", "format": "tech_token"}
    assert technology_schema["x-identity"] == {"properties": ["name"]}


def test_catalog_returns_isolated_manifest_and_rejects_unknown_types() -> None:
    first = catalog_manifest()
    nodes = first["nodes"]
    assert isinstance(nodes, Mapping)
    nodes["domain"]["required"]["value"] = "changed"

    assert catalog_manifest()["nodes"]["domain"]["required"]["value"] == "dns_name"
    _invalid("nodes", "hostname", {"value": "example.com"})
    _invalid("nodes", "ip", {"value": "192.0.2.1"})
    with pytest.raises(ExpectedValidationError):
        catalog_schema("nodes", "application")
