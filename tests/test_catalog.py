"""Catalog v3 manifest, schema, and strict validator contracts."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

import pytest

import justpen_knowledgebase_mcp.catalog as catalog_module
from justpen_knowledgebase_mcp.catalog import (
    CATALOG_FINGERPRINT,
    CATALOG_VERSION,
    EndpointView,
    catalog_manifest,
    catalog_schema,
    catalog_view,
    check_endpoint_values,
    validate_record,
)
from justpen_knowledgebase_mcp.errors import ExpectedValidationError
from justpen_knowledgebase_mcp.identity import identity_key

from . import catalog_golden as golden

CPE_NGINX = "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*"
CPE_NGINX_PRODUCT = "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*"

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
    "cwe",
    "organization",
    "email_address",
    "host_key",
    "http_fingerprint",
    "identity_tenant",
    "mta_sts_policy",
    "phone",
    "repository",
    "secret",
    "storage_bucket",
    "whois_registration",
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
    "has_weakness",
    "operated_by",
    "backed_by_bucket",
    "exposes_secret",
    "federates_with",
    "has_contact",
    "has_http_fingerprint",
    "has_mta_sts_policy",
    "owns_repository",
    "presents_host_key",
    "has_registration",
}


def _valid(kind: str, type_name: str, properties: dict[str, object]) -> None:
    validate_record(kind, type_name, properties)


def _invalid(kind: str, type_name: str, properties: dict[str, object]) -> None:
    with pytest.raises(ExpectedValidationError):
        validate_record(kind, type_name, properties)


def test_manifest_has_only_catalog_v3_types_and_stable_fingerprint() -> None:
    manifest = catalog_manifest()

    assert CATALOG_VERSION == 3
    assert manifest["version"] == 3
    assert set(manifest["nodes"]) == NODE_TYPES
    assert set(manifest["relations"]) == RELATION_TYPES
    assert CATALOG_FINGERPRINT == "81a574e62edc797065e6083f7d19025094fbbf715ff5da60f8fbdf6b2ad1e5f6"


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
    assert manifest["nodes"]["mta_sts_policy"]["identity"] == {
        "properties": ["value"],
        "scope": {"relation": "has_mta_sts_policy", "endpoint": "source"},
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
        ("asn", {"value": 4294967295}),
        ("asn", {"value": 15169, "name": "GOOGLE", "country": "US", "rir": "arin"}),
        ("ip_cidr", {"value": "8.8.8.0/24", "version": 4, "netname": "GOGL", "country": "US", "rir": "arin"}),
        ("organization", {"registry": "arin", "handle": "GOGL", "name": "Google LLC"}),
        ("spf_record", {"value": "v=spf1"}),
        ("spf_record", {"value": "v=spf1  include:example.com -all"}),
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
        ("technology", {"name": "php_7.4+x", "cpe": CPE_NGINX_PRODUCT, "categories": ["web-server"]}),
        ("technology", {"name": "nginx", "cpe": "cpe:2.3:a:f5:nginx:-:*:*:*:*:*:*:*"}),
        ("dmarc_record", {"value": "v=DMARC1"}),
        ("dmarc_record", {"value": "v=DMARC1; p=reject; rua=mailto:dmarc@example.com"}),
        ("dmarc_record", {"value": "v=DMARC1 p=none"}),
        ("dmarc_record", {"value": "v=DMARC1;" + "x" * 4087}),
        ("txt_record", {"value": "google-site-verification=abc"}),
        ("txt_record", {"value": "x"}),
        ("txt_record", {"value": "x" * 4096}),
        ("txt_record", {"value": "V=SPF1 -all"}),
        ("txt_record", {"value": "V=DMARC1; p=none"}),
        ("txt_record", {"value": "v=spf1include:_spf.google.com ~all"}),
        ("txt_record", {"value": "v=spf10"}),
        ("txt_record", {"value": "v=DMARC1p=none"}),
        ("txt_record", {"value": "v=STSv1id=1"}),
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
        ("cwe", {"value": "CWE-79"}),
        ("cwe", {"value": "CWE-1"}),
        ("cwe", {"value": "CWE-999999"}),
        ("cwe", {"value": "CWE-89", "name": "SQL Injection"}),
        (
            "endpoint",
            {
                "url": "https://example.com/",
                "method": "GET",
                "status": 200,
                "title": "Example Domain",
                "content_length": 1256,
                "content_type": "text/html",
                "webserver": "ECS (dcb/7F83)",
            },
        ),
        ("certificate", {"der_sha256": "a" * 64, "self_signed": True}),
        (
            "mta_sts_policy",
            {"value": "v=STSv1; id=1", "mode": "enforce", "max_age": 604800, "mx": ["*.example.net", "mx.example.com"]},
        ),
        ("organization", {"registry": "arin", "handle": "ORG-GOGL-1-ARIN"}),
        ("organization", {"registry": "ripe", "handle": "ORG-GC128-RIPE"}),
        ("organization", {"registry": "apnic", "handle": "A1"}),
        ("organization", {"registry": "afrinic", "handle": "X" * 64}),
        ("organization", {"registry": "ripe", "handle": "ORG-nG51-RIPE"}),
        ("organization", {"registry": "afrinic", "handle": "ORG-Ab1-AFRINIC"}),
        ("organization", {"registry": "arin", "handle": "org-gogl-1-arin"}),
        ("organization", {"registry": "lacnic", "handle": "ORG-1", "country": "br"}),
        ("email_address", {"value": "abuse@example.com"}),
        ("email_address", {"value": "first.last+tag@mail.example.co.uk"}),
        ("email_address", {"value": "a@b.io"}),
        ("email_address", {"value": "a" * 64 + "@example.com"}),
        ("host_key", {"algorithm": "ssh-ed25519", "fingerprint_sha256": "a" * 64}),
        ("host_key", {"algorithm": "sk-ecdsa-sha2-nistp256@openssh.com", "fingerprint_sha256": "0" * 64}),
        ("host_key", {"algorithm": "ssh-rsa", "fingerprint_sha256": "f" * 64, "bits": 2048}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "-1752256170"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "0"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "2147483647"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "-2147483648"}),
        ("http_fingerprint", {"kind": "body_sha256", "value": "a" * 64}),
        ("http_fingerprint", {"kind": "header_sha256", "value": "0" * 64}),
        ("identity_tenant", {"provider": "entra_id", "tenant_id": "72f988bf-86f1-41af-91ab-2d7cd011db47"}),
        ("identity_tenant", {"provider": "okta", "tenant_id": "dev-12345"}),
        ("identity_tenant", {"provider": "okta", "tenant_id": "acme_corp"}),
        ("mta_sts_policy", {"value": "v=STSv1"}),
        ("mta_sts_policy", {"value": "v=STSv1; id=20260920t000000z;"}),
        ("mta_sts_policy", {"value": "v=STSv1 id=1"}),
        ("phone", {"value": "+14155552671"}),
        ("phone", {"value": "+905321234567"}),
        ("phone", {"value": "+12"}),
        ("phone", {"value": "+" + "9" * 15}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "example-org", "name": "web-app"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "a", "name": ".github"}),
        ("repository", {"platform": "gitlab", "host": "git.example.com", "owner": "group/sub", "name": "api"}),
        ("repository", {"platform": "bitbucket", "host": "bitbucket.org", "owner": "team_x", "name": "infra"}),
        ("repository", {"platform": "gitea", "host": "code.example.com", "owner": "ops", "name": "runbooks"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "a" * 39, "name": "n" * 100}),
        ("secret", {"value_sha256": "a" * 64}),
        ("secret", {"value_sha256": "f" * 64, "verified": True, "detector": "aws"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "example-assets"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "a" * 63}),
        ("storage_bucket", {"provider": "gcp_gcs", "name": "example.appspot.com"}),
        ("storage_bucket", {"provider": "azure_blob", "name": "examplestorage"}),
        ("storage_bucket", {"provider": "gcp_gcs", "name": "my_bucket"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "1234"}),
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
        ("ip_address", {"value": "::ffff:192.0.2.1", "version": 6}),
        ("ip_address", {"value": "::ffff:c000:201", "version": 6}),
        ("ip_cidr", {"value": "192.0.2.1/24", "version": 4}),
        ("ip_cidr", {"value": "192.0.2.0", "version": 4}),
        ("ip_cidr", {"value": "192.0.2.0/255.255.255.0", "version": 4}),
        ("ip_cidr", {"value": "2001:DB8::/32", "version": 6}),
        ("ip_cidr", {"value": "192.0.2.0/24", "version": 6}),
        ("ip_cidr", {"value": "192.0.2.0/24", "version": False}),
        ("asn", {"value": 0}),
        ("asn", {"value": -1}),
        ("asn", {"value": 4294967296}),
        ("asn", {"value": True}),
        ("asn", {"value": "64512"}),
        ("asn", {"value": 15169, "country": "us"}),
        ("asn", {"value": 15169, "rir": "RIPE"}),
        ("ip_cidr", {"value": "8.8.8.0/24", "version": 4, "netname": ""}),
        ("organization", {"registry": "arin", "handle": "GOGL", "name": None}),
        ("spf_record", {"value": "v=spf10"}),
        ("spf_record", {"value": " v=spf1"}),
        ("spf_record", {"value": "v=spf1\t-all"}),
        ("spf_record", {"value": "v=spf1\n-all"}),
        ("spf_record", {"value": "v=spf1 é"}),
        ("port", {"number": 0, "transport": "tcp"}),
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
        ("endpoint", {"url": "https://[::ffff:192.0.2.1]/", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/", "method": "get"}),
        ("endpoint", {"url": "https://example.com/", "method": "M" * 33}),
        ("endpoint", {"url": "https://example.com/search?a b<>", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/search?q=%zz", "method": "GET"}),
        ("endpoint", {"url": "https://example.com/search?q=\u00e9", "method": "GET"}),
        ("endpoint", {"url": "https://example.com?q=1", "method": "GET"}),
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
        ("technology", {"name": "nginx", "cpe": CPE_NGINX}),
        ("technology", {"name": "nginx", "cpe": ""}),
        ("technology", {"name": "nginx", "cpe": "cpe:/a:f5:nginx"}),
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
        ("cwe", {"value": "cwe-79"}),
        ("cwe", {"value": "CWE-"}),
        ("cwe", {"value": "CWE-0079x"}),
        ("cwe", {"value": "CWE-1234567"}),
        ("cwe", {"value": "79"}),
        ("cwe", {"value": 79}),
        ("cwe", {"value": "CWE-79", "name": ""}),
        ("endpoint", {"url": "https://example.com/", "method": "GET", "status": "200"}),
        ("endpoint", {"url": "https://example.com/", "method": "GET", "status": 99}),
        ("endpoint", {"url": "https://example.com/", "method": "GET", "content_type": "text/html; charset=utf-8"}),
        ("endpoint", {"url": "https://example.com/", "method": "GET", "content_length": -1}),
        ("endpoint", {"url": "https://example.com/", "method": "GET", "title": None}),
        ("certificate", {"der_sha256": "a" * 64, "self_signed": "yes"}),
        ("mta_sts_policy", {"value": "v=STSv1; id=1", "mode": "Enforce"}),
        ("mta_sts_policy", {"value": "v=STSv1; id=1", "mx": ["mx2.example.com", "mx1.example.com"]}),
        ("organization", {"registry": "ARIN", "handle": "ORG-GOGL-1-ARIN"}),
        ("organization", {"registry": "iana", "handle": "ORG-1"}),
        ("organization", {"registry": "arin", "handle": "ORG-"}),
        ("organization", {"registry": "arin", "handle": "A"}),
        ("organization", {"registry": "arin", "handle": "-ORG-1"}),
        ("organization", {"registry": "arin", "handle": "ORG-1-"}),
        ("organization", {"registry": "arin", "handle": "X" * 65}),
        ("organization", {"registry": "arin", "handle": "ORG_1"}),
        ("organization", {"handle": "ORG-GOGL-1-ARIN"}),
        ("organization", {"registry": "arin"}),
        ("email_address", {"value": "Abuse@example.com"}),
        ("email_address", {"value": "abuse@localhost"}),
        ("email_address", {"value": "abuse@@example.com"}),
        ("email_address", {"value": "@example.com"}),
        ("email_address", {"value": "abuse@example.com."}),
        ("email_address", {"value": ".abuse@example.com"}),
        ("email_address", {"value": "a" * 65 + "@example.com"}),
        ("email_address", {"value": "abuse name@example.com"}),
        ("email_address", {"value": 1}),
        ("host_key", {"algorithm": "ssh-rsa", "fingerprint_sha256": "A" * 64}),
        ("host_key", {"algorithm": "rsa-sha2-512", "fingerprint_sha256": "a" * 64}),
        ("host_key", {"algorithm": "ssh-rsa", "fingerprint_sha256": "a" * 63}),
        ("host_key", {"algorithm": "ssh-rsa"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "2147483648"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "-2147483649"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "+1"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "01"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "-0"}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": "a" * 64}),
        ("http_fingerprint", {"kind": "favicon_mmh3", "value": -1752256170}),
        ("http_fingerprint", {"kind": "body_sha256", "value": "-1"}),
        ("http_fingerprint", {"kind": "body_sha256", "value": "A" * 64}),
        ("http_fingerprint", {"kind": "ja3s", "value": "a" * 64}),
        ("identity_tenant", {"provider": "entra_id", "tenant_id": "not-a-uuid"}),
        ("identity_tenant", {"provider": "entra_id", "tenant_id": "72F988BF-86F1-41AF-91AB-2D7CD011DB47"}),
        ("identity_tenant", {"provider": "okta", "tenant_id": "example.okta.com"}),
        ("identity_tenant", {"provider": "entra", "tenant_id": "example"}),
        ("identity_tenant", {"provider": "auth0", "tenant_id": "example"}),
        ("identity_tenant", {"provider": "google_workspace", "tenant_id": "c01abc23d"}),
        ("identity_tenant", {"provider": "okta", "tenant_id": "acme.corp"}),
        ("identity_tenant", {"provider": "okta", "tenant_id": ""}),
        ("identity_tenant", {"provider": "okta", "tenant_id": "Dev-12345"}),
        ("mta_sts_policy", {"value": "v=STSv10"}),
        ("mta_sts_policy", {"value": "V=STSv1"}),
        ("mta_sts_policy", {"value": " v=STSv1"}),
        ("mta_sts_policy", {"value": ""}),
        ("txt_record", {"value": "v=STSv1; id=1"}),
        ("phone", {"value": "14155552671"}),
        ("phone", {"value": "+0155552671"}),
        ("phone", {"value": "+1"}),
        ("phone", {"value": "+" + "9" * 16}),
        ("phone", {"value": "+1 415 555"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "Example-Org", "name": "web"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "group/sub", "name": "web"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "-example", "name": "web"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "ex--ample", "name": "web"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "a" * 40, "name": "web"}),
        ("repository", {"platform": "bitbucket", "host": "bitbucket.org", "owner": "team/x", "name": "web"}),
        ("repository", {"platform": "bitbucket", "host": "bitbucket.org", "owner": "t" * 63, "name": "web"}),
        ("repository", {"platform": "gitlab", "host": "git.example.com", "owner": "g" * 101, "name": "web"}),
        ("repository", {"platform": "gitlab", "host": "git.example.com", "owner": "g/" * 128, "name": "web"}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "example", "name": ".."}),
        ("repository", {"platform": "github", "host": "github.com", "owner": "example", "name": "n" * 101}),
        ("repository", {"platform": "github.com", "host": "github.com", "owner": "example", "name": "web"}),
        ("repository", {"platform": "github", "host": "GitHub.com", "owner": "example", "name": "web"}),
        ("repository", {"platform": "github", "owner": "example", "name": "web"}),
        ("secret", {"value_sha256": "A" * 64}),
        ("secret", {"value_sha256": "a" * 63}),
        ("secret", {}),
        ("secret", {"value_sha256": "a" * 64, "value": "hunter2"}),
        ("secret", {"value_sha256": "a" * 64, "password": "hunter2"}),
        ("storage_bucket", {"provider": "azure_blob", "name": "example-storage"}),
        ("storage_bucket", {"provider": "azure_blob", "name": "a" * 25}),
        ("storage_bucket", {"provider": "aws_s3", "name": "a" * 64}),
        ("storage_bucket", {"provider": "aws_s3", "name": "192.168.1.1"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "xn--example"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "example-s3alias"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "my_bucket"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "192.0.2.1"}),
        ("storage_bucket", {"provider": "do_spaces", "name": "example-space"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "Example"}),
        ("storage_bucket", {"provider": "aws_s3", "name": "ex..ample"}),
        ("storage_bucket", {"provider": "gcp_gcs", "name": "googtest"}),
        ("storage_bucket", {"provider": "gcp_gcs", "name": "my-google-bucket"}),
        ("storage_bucket", {"provider": "gcp_gcs", "name": "a" * 64 + ".example"}),
        ("storage_bucket", {"provider": "s3", "name": "example"}),
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


SUFFIXES = ["", " ", " -all", "; p=none", ";", "p=none", "include:_spf.google.com ~all", "0", " id=1"]


def _accepted(type_name: str, value: str) -> bool:
    properties: dict[str, object] = {"value": value}
    if type_name == "dkim_record":
        properties["selector"] = "default"
    try:
        validate_record("nodes", type_name, properties)
    except ExpectedValidationError:
        return False
    return True


@pytest.mark.parametrize("suffix", SUFFIXES)
@pytest.mark.parametrize(("tag", "type_name"), sorted(catalog_module._TXT_RECORD_DIVERSIONS))
def test_every_version_tag_spelling_has_exactly_one_home(tag: str, type_name: str, suffix: str) -> None:
    """A spelling the dedicated type refuses is a `txt_record`, and never refused by both.

    `v=spf1include:...` used to be refused by `spf_record` for the missing delimiter and by
    `txt_record` for the bare prefix, so the most common real SPF misconfiguration could not be
    recorded at all. The tags are read from the diversion table so a new one joins this assertion.
    """
    value = tag + suffix

    assert _accepted(type_name, value) is not _accepted("txt_record", value), value


def test_the_dedicated_types_keep_every_well_formed_spelling() -> None:
    """The widening must not also stop diverting a value that really is an SPF or MTA-STS record."""
    for value in ("v=spf1", "v=spf1 -all", "v=DMARC1", "v=DMARC1; p=none", "v=STSv1", "v=STSv1; id=1"):
        _invalid("nodes", "txt_record", {"value": value})
    for value in ("v=DKIM1", "v=DKIM1; k=rsa", "v=DKIM1p=MIGf"):
        _invalid("nodes", "txt_record", {"value": value})


@pytest.mark.parametrize("type_name", ["caa_issue", "caa_issuewild"])
def test_caa_parameter_names_are_stored_folded_with_their_extras(type_name: str) -> None:
    """The stored spelling and the identity derived from it stay in agreement, as `endpoint.url` does."""
    properties: dict[str, object] = {
        "flags": 0,
        "parameters": [
            {"name": "accountURI", "value": "https://ca.example/1", "seen": 2},
            {"name": "CAA", "value": ""},
        ],
    }

    validate_record("relations", type_name, properties)

    assert properties["parameters"] == [
        {"name": "accounturi", "value": "https://ca.example/1", "seen": 2},
        {"name": "caa", "value": ""},
    ]


@pytest.mark.parametrize("name", ["\u212a", "ACCOUNTURI\u212a", "acc ount"])
def test_folding_a_caa_name_never_widens_what_validation_accepts(name: str) -> None:
    """`"\u212a".lower()` is `"k"`, so a non-ASCII tag is left alone and refused as before."""
    _invalid("relations", "caa_issue", {"flags": 0, "parameters": [{"name": name, "value": "x"}]})


def test_a_malformed_caa_parameter_list_reaches_validation_unchanged() -> None:
    """Canonicalization runs before validation, so it must survive anything a client can send."""
    for parameters in ("text", [1], [{"name": 2, "value": "x"}], [{"value": "x"}]):
        _invalid("relations", "caa_issue", {"flags": 0, "parameters": parameters})


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
            [
                "port",
                "domain",
                "subdomain",
                "ip_address",
                "ip_cidr",
                "service",
                "endpoint",
                "certificate",
                "parameter",
                "dkim_record",
                "storage_bucket",
                "repository",
                "identity_tenant",
                "secret",
                "mta_sts_policy",
                "whois_registration",
            ],
            ["finding"],
        ),
        "has_registration": (["domain"], ["whois_registration"]),
        "presents_certificate": (["service"], ["certificate"]),
        "presents_host_key": (["service"], ["host_key"]),
        "serves_endpoint": (["service"], ["endpoint"]),
        "redirects_to": (["endpoint"], ["endpoint"]),
        "affected_by": (["service", "finding", "endpoint"], ["cve"]),
        "runs_technology": (["service", "endpoint", "domain", "subdomain"], ["technology"]),
        "protected_by": (["service", "endpoint", "domain", "subdomain"], ["technology"]),
        "backed_by_bucket": (["domain", "subdomain", "endpoint"], ["storage_bucket"]),
        "exposes_secret": (["repository", "endpoint", "storage_bucket"], ["secret"]),
        "federates_with": (d, ["identity_tenant"]),
        "has_contact": (
            ["organization", "registrar", "domain", "subdomain", "repository", "whois_registration"],
            ["email_address", "phone"],
        ),
        "has_http_fingerprint": (["endpoint"], ["http_fingerprint"]),
        "has_mta_sts_policy": (d, ["mta_sts_policy"]),
        "owns_repository": (d, ["repository"]),
        "has_dmarc": (d, ["dmarc_record"]),
        "has_dkim_selector": (d, ["dkim_record"]),
        "has_parameter": (["endpoint"], ["parameter"]),
        "has_tls_fingerprint": (["service"], ["tls_fingerprint"]),
        "registered_through": (["whois_registration"], ["registrar"]),
        "has_weakness": (["finding", "cve"], ["cwe"]),
        "operated_by": (["asn", "ip_cidr"], ["organization"]),
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
        ("has_contact", {"role": "abuse"}),
        ("has_contact", {"role": "published", "name": "security team"}),
        ("backed_by_bucket", {}),
        ("exposes_secret", {"location": "src/config.py"}),
        ("exposes_secret", {"location": "x" * 1024, "commit": "a" * 40}),
        ("federates_with", {"namespace_type": "federated"}),
        ("federates_with", {"namespace_type": "managed", "federation_brand": "Contoso"}),
        ("has_http_fingerprint", {}),
        ("has_mta_sts_policy", {}),
        ("owns_repository", {}),
        ("presents_host_key", {}),
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
        ("has_contact", {}),
        ("has_contact", {"role": "Abuse"}),
        ("has_contact", {"role": "owner"}),
        ("has_contact", {"role": 1}),
        ("exposes_secret", {}),
        ("exposes_secret", {"location": ""}),
        ("exposes_secret", {"location": "x" * 1025}),
        ("exposes_secret", {"location": "line\nbreak"}),
        ("federates_with", {"namespace_type": "Federated"}),
        ("federates_with", {"namespace_type": "unknown"}),
    ],
)
def test_invalid_relation_types_bounds_and_grammars(type_name: str, properties: dict[str, object]) -> None:
    _invalid("relations", type_name, properties)


@pytest.mark.parametrize(
    "value",
    [
        CPE_NGINX,
        "cpe:2.3:a:apache:http_server:2.4.41:*:*:*:*:*:*:*",
        "cpe:2.3:a:vendor:product:8.???:*:*:*:*:*:*:*",
        "cpe:2.3:a:vendor:pro\\:duct:*:*:*:*:*:*:*:*",
        "cpe:2.3:o:-:-:-:-:-:-:-:-:-:-",
    ],
)
def test_cpe23_rule_accepts_the_formatted_string_binding(value: str) -> None:
    assert catalog_module._valid_field(value, "cpe23")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "cpe:/a:apache:http_server:2.4.41",
        "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*",
        "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*:*",
        "cpe:2.3:x:f5:nginx:1.18.0:*:*:*:*:*:*:*",
        "CPE:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*",
        "cpe:2.3:a:f5:NGINX:1.18.0:*:*:*:*:*:*:*",
        "cpe:2.3:a:f5:" + "n" * 512 + ":*:*:*:*:*:*:*:*",
    ],
)
def test_cpe23_rule_rejects_empty_legacy_uri_and_malformed_bindings(value: str) -> None:
    """D-14: the unused `cpe23_or_empty` rule is gone; an absent cpe is an absent key, not ''."""
    assert not catalog_module._valid_field(value, "cpe23")


def test_a_versioned_cpe_is_read_through_its_escaped_colons() -> None:
    assert catalog_module._cpe_version("cpe:2.3:a:vendor:pro\\:duct:-:*:*:*:*:*:*:*") == "-"
    assert catalog_module._cpe_version("cpe:2.3:a:ven\\\\:prod:1:*:*:*:*:*:*:*") == "1"
    assert catalog_module._cpe_version("cpe:/a:f5:nginx") is None


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


def test_scope_declarations_are_derived_rather_than_mirrored_by_hand() -> None:
    """Three call sites used to repeat this list by hand; a missed edit failed writes or dropped a
    delete guard silently. They now read one declaration."""
    assert catalog_module.scope_relations() == {
        "port": "has_open_port",
        "service": "has_service",
        "finding": "has_finding",
        "dkim_record": "has_dkim_selector",
        "parameter": "has_parameter",
        "mta_sts_policy": "has_mta_sts_policy",
        "whois_registration": "has_registration",
    }
    order = catalog_module.scope_order()
    assert set(order) == set(catalog_module.scope_relations())
    assert order.index("port") < order.index("service") < order.index("finding")
    assert order.index("parameter") < order.index("finding")
    assert order.index("dkim_record") < order.index("finding")
    assert order.index("whois_registration") < order.index("finding")


def test_scope_order_refuses_a_cycle_instead_of_emitting_a_partial_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(catalog_module._RELATIONS, "has_open_port", catalog_module._relation(["service"], ["port"]))
    with pytest.raises(RuntimeError, match="cycle"):
        catalog_module.scope_order()


def test_scope_contract_rejects_a_declaration_the_storage_layer_cannot_honor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        catalog_module._NODES,
        "port",
        {
            "identity": catalog_module._identity(
                ["transport", "number"], scope={"relation": "has_open_port", "endpoint": "target"}
            ),
            "required": {"number": "uint16", "transport": ["tcp", "udp", "sctp"]},
        },
    )
    with pytest.raises(RuntimeError, match="source-scoped relation target"):
        catalog_module._ensure_scope_contract()


def test_shared_vocabulary_nodes_are_never_a_finding_source() -> None:
    """A finding names one real object. A node that unrelated hosts legitimately share - a
    technology slug, a cipher suite, a stack fingerprint, an SPF string thousands of domains
    publish verbatim - is not one object, so a finding hung there would read as applying to all of
    them. A bucket, a repository, a tenant and a secret digest each name exactly one object."""
    shared = {
        "technology",
        "tls_cipher_suite",
        "tls_fingerprint",
        "http_fingerprint",
        "host_key",
        "spf_record",
        "dmarc_record",
        "txt_record",
        "email_address",
        "phone",
        "cve",
        "cwe",
    }
    sources = set(catalog_manifest()["relations"]["has_finding"]["sources"])

    assert sources & shared == set()
    assert {
        "parameter",
        "dkim_record",
        "mta_sts_policy",
        "storage_bucket",
        "repository",
        "identity_tenant",
        "secret",
    } <= sources


def test_every_relation_endpoint_names_a_declared_node_type() -> None:
    manifest = catalog_manifest()
    nodes = set(manifest["nodes"])
    for type_name, definition in manifest["relations"].items():
        for field in ("sources", "targets"):
            assert definition[field], f"{type_name}.{field} is empty"
            assert set(definition[field]) <= nodes, f"{type_name}.{field} names an unknown node type"


def test_every_declared_rule_is_published_and_executable() -> None:
    """A rule missing from _FORMATS is undocumented, and one missing from _schema_for_rule
    degrades the discovery schema to a bare string, and one published but referenced by nothing is
    dead weight an agent still has to read. The golden cases cover the third place a rule has to
    exist, _valid_field, which rejects every value without it. Optional maps count as uses."""
    manifest = catalog_manifest()
    used: set[str] = set()
    for kind in ("nodes", "relations"):
        for type_name, definition in manifest[kind].items():
            for field, rule in {**definition["required"], **definition["optional"]}.items():
                if isinstance(rule, list):
                    continue
                assert rule in manifest["formats"], f"{kind}.{type_name}.{field} uses an unpublished rule"
                assert catalog_schema(kind, type_name)["properties"][field].get("format") == rule
                used.add(rule)

    assert set(manifest["formats"]) == used


def _plain(value: object) -> object:
    """Re-materialize the shared read view as the plain tree `catalog_manifest()` returns."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in cast("Mapping[str, object]", value).items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in cast("tuple[object, ...]", value)]
    return value


def _serialized_view() -> str:
    """Serialize the shared read view exactly as `CATALOG_JSON` was serialized."""
    return json.dumps(_plain(catalog_view()), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_catalog_view_is_one_shared_object_matching_the_isolated_manifest() -> None:
    """The view is the same parse `catalog_manifest()` performs, done once and shared. If these ever
    disagree, a pure reader and a copying caller would be validating against different catalogs."""
    assert catalog_view() is catalog_view()
    assert _plain(catalog_view()) == catalog_manifest()
    assert _serialized_view() == catalog_module.CATALOG_JSON


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("nodes",),
        ("nodes", "domain"),
        ("nodes", "domain", "required"),
        ("nodes", "domain", "identity"),
        ("nodes", "port", "identity", "scope"),
        ("relations",),
        ("relations", "caa_issue"),
        ("relations", "caa_issue", "identity", "order_independent"),
        ("common",),
        ("formats",),
    ],
)
def test_catalog_view_mappings_refuse_mutation_at_every_depth(path: tuple[str, ...]) -> None:
    """MappingProxyType is shallow, so the wrapping has to go all the way down. A seventh caller
    that writes into any nested member would corrupt the catalog for the rest of the process, not
    for itself, and the symptom would be an unrelated test failing only when this one ran first."""
    target = catalog_view()
    for key in path:
        target = target[key]
    assert isinstance(target, MappingProxyType)
    with pytest.raises(TypeError):
        cast("dict[str, object]", target)["injected"] = "x"
    with pytest.raises(TypeError):
        del cast("dict[str, object]", target)[next(iter(target))]
    with pytest.raises(AttributeError):
        cast("dict[str, object]", target).pop(next(iter(target)))
    with pytest.raises(AttributeError):
        cast("dict[str, object]", target).update({"injected": "x"})


@pytest.mark.parametrize(
    "path",
    [
        ("nodes", "domain", "identity", "properties"),
        ("nodes", "port", "identity", "properties"),
        ("relations", "has_open_port", "sources"),
        ("relations", "has_open_port", "targets"),
        ("relations", "caa_issue", "identity", "order_independent", "projection"),
        ("nodes", "port", "required", "transport"),
    ],
)
def test_catalog_view_arrays_are_tuples_that_refuse_mutation(path: tuple[str, ...]) -> None:
    """Every JSON array becomes a tuple, including the enum lists inside `required`, so no reader
    can append to or reorder a declaration the whole process shares."""
    target = catalog_view()
    for key in path:
        target = target[key]
    assert isinstance(target, tuple)
    with pytest.raises(TypeError):
        cast("list[object]", target)[0] = "injected"
    with pytest.raises(AttributeError):
        cast("list[object]", target).append("injected")


async def test_catalog_view_is_byte_identical_after_two_full_write_paths(kb) -> None:
    """Guards pre-mortem scenario 1: the shared handle is routed through the per-record write path,
    so a caller believed to be pure writing into it would corrupt the catalog for every later
    caller. Two batches, because the second re-enters the deduplication and merge branches the
    first creates, and any mutation from the first would already be visible to the second."""
    before = _serialized_view()
    assert before == catalog_module.CATALOG_JSON

    for iteration in range(2):
        result = await kb.write(
            {
                "nodes": [
                    {"type": "ip_address", "properties": {"value": f"192.0.2.{iteration + 1}", "version": 4}},
                    {"type": "port", "properties": {"transport": "tcp", "number": 443}},
                    {"type": "domain", "properties": {"value": f"example{iteration}.com"}},
                ],
                "relations": [
                    {
                        "type": "has_open_port",
                        "properties": {},
                        "source_ref": {"node_index": 0},
                        "target_ref": {"node_index": 1},
                    },
                    {
                        "type": "caa_issue",
                        "source_ref": {"node_index": 2},
                        "target_ref": {"node_index": 2},
                        "properties": {
                            "flags": 0,
                            "parameters": [
                                {"name": "validationmethods", "value": "dns-01"},
                                {"name": "accounturi", "value": "https://ca.example/account"},
                            ],
                        },
                    },
                ],
            }
        )
        assert len(result["nodes"]) == 3
        assert len(result["relations"]) == 2
        assert _serialized_view() == before, f"the shared catalog handle changed during iteration {iteration}"

    assert _serialized_view() == catalog_module.CATALOG_JSON
    assert catalog_manifest()["nodes"]["domain"]["required"]["value"] == "dns_name"


def test_enum_rule_rejection_keeps_the_published_list_spelling() -> None:
    """`validate_record` now reads the shared view, where a JSON array is a tuple. The message a
    client receives has to stay the list spelling it has always been, so routing that read changes
    no client-visible text. Without `_published_rule` this reads `expected ('info', ...)`."""
    with pytest.raises(ExpectedValidationError) as failure:
        validate_record("nodes", "finding", {"title": "t", "severity": "catastrophic"})
    assert str(failure.value.message) == (
        "/properties/severity: expected ['info', 'low', 'medium', 'high', 'critical']"
    )

    with pytest.raises(ExpectedValidationError) as scalar:
        validate_record("nodes", "domain", {"value": 1})
    assert str(scalar.value.message) == "/properties/value: expected dns_name"


def _kind(type_name: str) -> str:
    return "nodes" if type_name in catalog_manifest()["nodes"] else "relations"


def _published_ids(key: str) -> set[str]:
    manifest = catalog_manifest()
    return {rule_id for kind in ("nodes", "relations") for d in manifest[kind].values() for rule_id in d[key]}


def test_every_format_check_and_canonicalization_has_golden_cases() -> None:
    """A published id without cases, or cases for an id nobody publishes, fails here (AC-12)."""
    assert set(golden.FORMATS) == set(catalog_manifest()["formats"])
    assert set(golden.CHECKS).isdisjoint(golden.ENDPOINT_CHECKS)
    assert {*golden.CHECKS, *golden.ENDPOINT_CHECKS} == _published_ids("checks")
    assert set(golden.CANONICALIZATIONS) == _published_ids("canonicalize")
    for table in (golden.FORMATS, golden.CHECKS, golden.ENDPOINT_CHECKS, golden.CANONICALIZATIONS):
        for rule_id, (first, second) in table.items():
            assert first, rule_id
            assert second, rule_id


@pytest.mark.parametrize(
    ("rule", "value", "accepted"),
    [
        (rule, value, accepted)
        for rule, cases in sorted(golden.FORMATS.items())
        for accepted, values in zip((True, False), cases, strict=True)
        for value in values
    ],
)
def test_golden_format_cases(rule: str, value: object, *, accepted: bool) -> None:
    assert catalog_module._valid_field(copy.deepcopy(value), rule) is accepted


@pytest.mark.parametrize(
    ("rule_id", "record", "accepted"),
    [
        (rule_id, record, accepted)
        for rule_id, cases in sorted(golden.CHECKS.items())
        for accepted, records in zip((True, False), cases, strict=True)
        for record in records
    ],
)
def test_golden_check_cases(rule_id: str, record: tuple[str, dict[str, object]], *, accepted: bool) -> None:
    type_name, properties = copy.deepcopy(record)
    kind = _kind(type_name)
    assert rule_id in catalog_manifest()[kind][type_name]["checks"]
    if accepted:
        validate_record(kind, type_name, properties)
        return
    for field, rule in catalog_manifest()[kind][type_name]["required"].items():
        assert catalog_module._valid_field(properties[field], rule), f"{rule_id}: {field} fails its own rule"
    with pytest.raises(ExpectedValidationError):
        catalog_module._CHECKS[rule_id](type_name, properties)


@pytest.mark.parametrize(
    ("rule_id", "case", "accepted"),
    [
        (rule_id, case, accepted)
        for rule_id, cases in sorted(golden.ENDPOINT_CHECKS.items())
        for accepted, endpoint_cases in zip((True, False), cases, strict=True)
        for case in endpoint_cases
    ],
)
def test_golden_endpoint_check_cases(
    rule_id: str,
    case: tuple[dict[str, object], tuple[str, dict[str, object]], tuple[str, dict[str, object]]],
    *,
    accepted: bool,
) -> None:
    relation_props, (source_type, source), (target_type, target) = copy.deepcopy(case)
    relations = [
        name
        for name, definition in catalog_manifest()["relations"].items()
        if rule_id in definition["checks"]
        and source_type in definition["sources"]
        and target_type in definition["targets"]
    ]
    assert relations, rule_id
    validate_record("nodes", source_type, source)
    validate_record("nodes", target_type, target)
    views = (EndpointView(source_type, source), EndpointView(target_type, target))
    for relation in relations:
        if accepted:
            check_endpoint_values(relation, relation_props, *views)
        else:
            with pytest.raises(ExpectedValidationError):
                check_endpoint_values(relation, relation_props, *views)


@pytest.mark.parametrize(
    ("rule_id", "type_name", "before", "after"),
    [
        (rule_id, type_name, before, after)
        for rule_id, (rewrites, kept) in sorted(golden.CANONICALIZATIONS.items())
        for type_name, before, after in (*rewrites, *((name, value, value) for name, value in kept))
    ],
)
def test_golden_canonicalization_cases(
    rule_id: str, type_name: str, before: dict[str, object], after: dict[str, object]
) -> None:
    kind = _kind(type_name)
    assert rule_id in catalog_manifest()[kind][type_name]["canonicalize"]
    properties = copy.deepcopy(before)
    catalog_module._CANONICALIZATIONS[rule_id](properties)
    assert properties == after


def test_endpoint_values_are_checked_only_by_the_relation_that_declares_them() -> None:
    """A relation without endpoint checks accepts any stored values its type gate let through."""
    far = EndpointView("subdomain", {"value": "unrelated.example.org"})
    check_endpoint_values("cname_to", {}, EndpointView("domain", {"value": "example.com"}), far)
    with pytest.raises(ExpectedValidationError, match="unknown catalog type"):
        check_endpoint_values("hostname_of", {}, far, far)


def _rebuilt(**changes: object) -> str:
    tables: dict[str, object] = {
        "nodes": catalog_module._NODES,
        "relations": catalog_module._RELATIONS,
        "formats": catalog_module._FORMATS,
        "checks": catalog_module._CHECKS,
        "endpoint_checks": catalog_module._ENDPOINT_CHECKS,
        "canonicalizations": catalog_module._CANONICALIZATIONS,
    }
    tables.update(changes)
    built = catalog_module._build_catalog(**tables)  # type: ignore[arg-type]
    return json.dumps(built, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _with_rule(
    kind_table: Mapping[str, Mapping[str, object]], type_name: str, key: str, ids: list[str]
) -> dict[str, Mapping[str, object]]:
    return {**kind_table, type_name: {**kind_table[type_name], key: ids}}


def test_the_builder_reproduces_the_published_contract() -> None:
    assert _rebuilt() == catalog_module.CATALOG_JSON


def test_renaming_or_versioning_a_rule_id_changes_the_contract() -> None:
    """Pre-mortem 1: the fingerprint covers ids and versions, so a rename or a bump moves it."""
    run = catalog_module._CHECKS["dns_name_kind.1"]
    checks = {key: value for key, value in catalog_module._CHECKS.items() if key != "dns_name_kind.1"}
    nodes = _with_rule(catalog_module._NODES, "domain", "checks", ["dns_name_kind.2"])
    nodes = _with_rule(nodes, "subdomain", "checks", ["dns_name_kind.2"])

    assert _rebuilt(nodes=nodes, checks={**checks, "dns_name_kind.2": run}) != catalog_module.CATALOG_JSON
    canon = catalog_module._CANONICALIZATIONS
    moved = {**canon, "endpoint_url_drop_query.2": canon["endpoint_url_drop_query.1"]}
    del moved["endpoint_url_drop_query.1"]
    nodes = _with_rule(catalog_module._NODES, "endpoint", "canonicalize", ["endpoint_url_drop_query.2"])
    assert _rebuilt(nodes=nodes, canonicalizations=moved) != catalog_module.CATALOG_JSON
    formats = {**catalog_module._FORMATS, "unused_rule": 1}
    assert _rebuilt(formats=formats) != catalog_module.CATALOG_JSON
    bumped = {**catalog_module._FORMATS, "http_url": 2}
    assert _rebuilt(formats=bumped) != catalog_module.CATALOG_JSON


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"nodes": _with_rule(catalog_module._NODES, "domain", "checks", ["dns_name_kind.9"])},
            "names rule dns_name_kind.9",
        ),
        (
            {"nodes": _with_rule(catalog_module._NODES, "domain", "checks", ["has_subdomain_suffix.1"])},
            "names rule has_subdomain_suffix.1",
        ),
        (
            {"nodes": _with_rule(catalog_module._NODES, "domain", "checks", ["DNS.1"])},
            "names rule DNS.1",
        ),
        (
            {"checks": {**catalog_module._CHECKS, "orphan_rule.1": catalog_module._CHECKS["dns_name_kind.1"]}},
            "no type uses",
        ),
        ({"formats": {**catalog_module._FORMATS, "http_url": 0}}, "positive integer behavior version"),
    ],
)
def test_the_builder_refuses_a_dangling_misplaced_or_unused_rule(changes: dict[str, object], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _rebuilt(**changes)


def test_relation_checks_run_inside_record_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-12: a relation may declare a property check, not only an endpoint check."""
    seen: list[str] = []

    def record(type_name: str, _properties: dict[str, object]) -> None:
        seen.append(type_name)

    monkeypatch.setitem(catalog_module._CHECKS, "probe_relation.1", record)
    relations = _with_rule(catalog_module._RELATIONS, "resolves_to", "checks", ["probe_relation.1"])
    view = catalog_module._read_only(json.loads(_rebuilt(relations=relations)))
    monkeypatch.setattr(catalog_module, "_CATALOG_VIEW", view)

    validate_record("relations", "resolves_to", {})

    assert seen == ["resolves_to"]


def test_every_rule_id_is_described() -> None:
    descriptions = catalog_module.rule_descriptions()
    assert set(descriptions) == _published_ids("checks") | _published_ids("canonicalize")
    assert all(descriptions.values())


@pytest.mark.parametrize("kind", ["nodes", "relations"])
def test_every_type_and_property_is_described(kind: str) -> None:
    """AC-1: every type says what it models and what it does not, and every declared property,
    format and rule has its own description."""
    for type_name, definition in catalog_manifest()[kind].items():
        docs = catalog_module.type_description(kind, type_name)
        assert docs["summary"], type_name
        assert docs["excludes"], type_name
        assert set(docs["properties"]) == {*definition["required"], *definition["optional"]}, type_name
    assert set(catalog_module.format_descriptions()) == set(catalog_manifest()["formats"])
    assert set(catalog_module.common_descriptions()) == set(catalog_manifest()["common"])


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("nodes", "domain", "summary"), "", "empty or longer"),
        (("nodes", "domain", "notes"), "x" * 601, "empty or longer"),
        (("formats", "http_url"), "x" * 601, "empty or longer"),
        (("checks", "dns_name_kind.1"), None, "disagree with the contract"),
        (("nodes", "domain", "properties", "value"), None, "exactly its declared properties"),
        (("nodes", "domain", "owner"), "x", "summary and excludes"),
    ],
)
def test_the_docs_contract_refuses_missing_stale_and_oversized_prose(
    monkeypatch: pytest.MonkeyPatch, path: tuple[str, ...], value: str | None, message: str
) -> None:
    docs = copy.deepcopy(catalog_module._DOCS)
    target = docs
    for key in path[:-1]:
        target = target[key]
    if value is None:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    monkeypatch.setattr(catalog_module, "_DOCS", docs)
    with pytest.raises(RuntimeError, match=message):
        catalog_module._ensure_docs_contract()


def test_type_description_is_an_isolated_copy() -> None:
    first = catalog_module.type_description("nodes", "domain")
    first["properties"]["value"] = "changed"
    assert catalog_module.type_description("nodes", "domain")["properties"]["value"] != "changed"


def _view_with(monkeypatch: pytest.MonkeyPatch, kind: str, type_name: str, key: str, value: object) -> None:
    """Serve validation from a rebuilt catalog in which one type declares something extra."""
    table = catalog_module._NODES if kind == "nodes" else catalog_module._RELATIONS
    changed = {**table, type_name: {**table[type_name], key: value}}
    view = catalog_module._read_only(json.loads(_rebuilt(**{kind: changed})))
    monkeypatch.setattr(catalog_module, "_CATALOG_VIEW", view)


@pytest.mark.parametrize(
    ("properties", "accepted"),
    [
        ({"value": "example.com"}, True),
        ({"value": "example.com", "note": "registered in 2004"}, True),
        ({"value": "example.com", "undeclared": None}, True),
        ({"value": "example.com", "note": ""}, False),
        ({"value": "example.com", "note": None}, False),
        ({"value": "example.com", "note": 1}, False),
    ],
)
def test_declared_optional_properties_are_validated_when_present(
    monkeypatch: pytest.MonkeyPatch, properties: dict[str, object], *, accepted: bool
) -> None:
    """AC-7: an invalid declared attribute is rejected, an absent one and an undeclared key are
    accepted, and null is never a value: clearing is `remove_properties`."""
    _view_with(monkeypatch, "nodes", "domain", "optional", {"note": "printable_text_200"})
    if accepted:
        validate_record("nodes", "domain", properties)
        return
    with pytest.raises(ExpectedValidationError) as failure:
        validate_record("nodes", "domain", properties)
    assert failure.value.message == "/properties/note: expected printable_text_200"


def test_optional_properties_reach_the_discovery_schema_but_not_its_required_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = catalog_module._NODES
    changed = {**table, "domain": {**table["domain"], "optional": {"note": "printable_text_200"}}}
    monkeypatch.setattr(catalog_module, "CATALOG_JSON", _rebuilt(nodes=changed))
    schema = catalog_schema("nodes", "domain")

    assert schema["required"] == ["value"]
    assert schema["properties"]["note"] == {"type": "string", "format": "printable_text_200"}


def _catalog_with(kind: str, type_name: str, **changes: object) -> dict[str, object]:
    table = catalog_module._NODES if kind == "nodes" else catalog_module._RELATIONS
    return json.loads(_rebuilt(**{kind: {**table, type_name: {**table[type_name], **changes}}}))


@pytest.mark.parametrize(
    ("catalog", "message"),
    [
        (_catalog_with("nodes", "domain", optional={"value": "dns_name"}), "declares \\['value'\\] twice"),
        (
            _catalog_with("nodes", "domain", identity={"properties": ["value", "note"]}, optional={"note": "cve"}),
            "identity names a property outside its required map",
        ),
        (
            {
                **_catalog_with("nodes", "domain", optional={"label": "printable_text_200"}),
                "relations": _catalog_with("relations", "resolves_to", optional={"label": "cve"})["relations"],
            },
            "optional label has one rule on",
        ),
        (_catalog_with("nodes", "secret", optional={"Password": "printable_text_200"}), "plaintext-bearing name"),
    ],
)
def test_the_property_contract_refuses_contradictory_maps(
    monkeypatch: pytest.MonkeyPatch, catalog: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(catalog_module, "_CATALOG", catalog)
    with pytest.raises(RuntimeError, match=message):
        catalog_module._ensure_property_contract()
