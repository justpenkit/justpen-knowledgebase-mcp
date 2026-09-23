"""Golden accepted and rejected cases for every published format, check and canonicalization id.

The fingerprint covers rule ids and their versions, not the callables behind them. Each id owns its
cases here, so changing what a rule accepts means editing that id's cases, which puts the missing
version bump in front of the reviewer. `tests/test_catalog.py` refuses a manifest id without cases
and a case key the manifest no longer publishes.
"""

from __future__ import annotations

from typing import Any

CPE_NGINX = "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*"

Record = tuple[str, dict[str, Any]]
Endpoint = tuple[dict[str, Any], Record, Record]
Rewrite = tuple[str, dict[str, Any], dict[str, Any]]

# Format id -> (accepted values, rejected values), judged by the format validator alone.
FORMATS: dict[str, tuple[tuple[object, ...], tuple[object, ...]]] = {
    "alpn_tokens": ((["h2", "http/1.1"], [], ["h2", "h2"]), ("h2", ["h2 "], [""], ["é"], [1])),
    "asn": ((0, 64512, 4294967295), (-1, 4294967296, True, "64512")),
    "boolean": ((True, False), ("true", 1, 0, None)),
    "bucket_name": (
        ("example-assets", "my_bucket", "a.b.c"),
        ("ab", "Example", "ex..ample", "192.168.1.1", "-abc", "a" * 223),
    ),
    "caa_parameters": (
        ([], [{"name": "accounturi", "value": "https://ca.example/1"}], [{"name": "a", "value": ""}]),
        ("x", [1], [{"name": "-bad", "value": "x"}], [{"name": "ok", "value": "has space"}], [{"name": "ok"}]),
    ),
    "cidr": (
        ("192.0.2.0/24", "2001:db8::/32", "0.0.0.0/0"),
        ("192.0.2.1/24", "192.0.2.0", "2001:DB8::/32", "192.0.2.0/255.255.255.0", "fe80::/64%en0"),
    ),
    "cpe23": (
        (CPE_NGINX, "cpe:2.3:o:-:-:-:-:-:-:-:-:-:-", "cpe:2.3:a:vendor:pro\\:duct:*:*:*:*:*:*:*:*"),
        ("", "cpe:/a:apache:http_server:2.4.41", "CPE:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*", "cpe:2.3:a:f5"),
    ),
    "credential_key_id": (("AKIAIOSFODNN7EXAMPLE", "sk_live:1/a+b=="), ("", "AKIA IOSFODNN", "k" * 129, "\u00e9")),
    "cve": (("CVE-2026-1234", "CVE-1999-1234567"), ("cve-2026-1234", "CVE-26-1234", "CVE-2026-123", 20261234)),
    "cwe": (("CWE-79", "CWE-999999"), ("cwe-79", "CWE-1234567", "CWE-", "79", 79)),
    "dkim_selector": (("default", "selector1.sub", "a" * 63), ("Default", "s1._domainkey", "", "a" * 64, "s1-")),
    "dmarc": (
        ("v=DMARC1", "v=DMARC1; p=reject", "v=DMARC1 p=none"),
        ("v=DMARC10", "V=DMARC1", "v=DMARC1;\tp=none", "", "v=DMARC1;" + "x" * 4088),
    ),
    "dns_name": (
        ("example.com", "api.example.com", "_sip._tcp.example.com", "xn--bcher-kva.example"),
        ("Example.com", "example.com.", "com", "localhost", "192.0.2.1", "example..com", "xn--a.example.com"),
    ),
    "dns_or_explicit_empty": (("", "api.example.com"), ("192.0.2.1", "Example.com", "localhost")),
    "email_address": (
        ("abuse@example.com", "first.last+tag@mail.example.co.uk"),
        ("Abuse@example.com", "abuse@localhost", "a@b@example.com", ".abuse@example.com", "abuse name@example.com"),
    ),
    "epp_status_list": (
        (
            ["ok"],
            ["clientDeleteProhibited", "clientTransferProhibited", "serverUpdateProhibited"],
            ["redemptionPeriod"],
        ),
        (
            [],
            ["clientTransferProhibited", "clientDeleteProhibited"],
            ["ok", "ok"],
            ["client transfer prohibited"],
            ["active"],
            ["serverRecoverProhibited"],
            "ok",
        ),
    ),
    "http_fingerprint_value": (("-1752256170", "0", "a" * 64), ("+1", "A" * 64, "01", "-0", "2147483648")),
    "http_status": ((100, 200, 599), (99, 600, "200", True, 200.0)),
    "http_url": (
        (
            "https://example.com/",
            "http://[2001:db8::1]:8080/a",
            "https://api.example.com/a%2Fb",
            "https://my_service.example.com/",
            "http://localhost:8080/",
        ),
        (
            "https://example.com",
            "https://example.com:443/",
            "https://user@example.com/",
            "HTTPS://example.com/",
            "https://example.com/#a",
            "https://example.com/../a",
            "https://example.com/%2f",
        ),
    ),
    "ip": (
        ("192.0.2.1", "2001:db8::1", "::1", "64:ff9b::c000:201"),
        ("2001:DB8::1", "2001:0db8::1", "192.168.001.1", "fe80::1%en0", "192.0.2.1/32", "::ffff:192.0.2.1"),
    ),
    "ip_version": ((4, 6), (5, "4", True)),
    "iso3166_alpha2": (("US", "DE", "EU"), ("us", "USA", "U", "", "U1")),
    "media_type": (
        ("text/html", "application/vnd.api+json", "image/svg+xml"),
        ("text/html; charset=utf-8", "Text/HTML", "text", "text/", "/html", "text/html "),
    ),
    "method": (("GET", "PROPFIND", "M" * 32), ("get", "M" * 33, "")),
    "mta_sts": (("v=STSv1", "v=STSv1; id=1", "v=STSv1 id=1"), ("v=STSv10", "V=STSv1", "", " v=STSv1")),
    "mx_pattern_list": (
        ([], ["mail.example.com"], ["*.mail.protection.outlook.com", "mx1.example.com"]),
        (
            ["mx1.example.com", "mx1.example.com"],
            ["mx2.example.com", "mx1.example.com"],
            ["MX.example.com"],
            ["*.com"],
            ["mail.example.com."],
            "mail.example.com",
            [1],
        ),
    ),
    "parameter_name": (("id", "X-Request-Id", "%20foo"), ("id=1", "two words", "", "a" * 129, "café")),
    "phone_e164": (("+14155552671", "+12"), ("14155552671", "+0155552671", "+1", "+" + "9" * 16, "+1 415 555")),
    "printable_text_1024": (("x", "x" * 1024), ("", "x" * 1025, "line\nbreak", 1)),
    "printable_text_200": (("x", "x" * 200), ("", "x" * 201, "a\tb")),
    "public_suffix": (
        ("com", "co.uk", "internal", "xn--p1ai"),
        ("example.com", "Com", "com.", "", "ck", "c_m", "xn--a"),
    ),
    "redirect_status": ((301, 302, 303, 307, 308), (300, 304, "301", True)),
    "registry_domain_id": (
        ("2138514_DOMAIN_COM-VRSN", "DOM000000113746-FRNIC", "D1234567-TLD", "REDACTED-REDACTED"),
        ("google.com.br", "REDACTED FOR PRIVACY", "N/A", "D1234567", "-VRSN", "D1-TOOLONGSUF", "D\u00e9-X"),
    ),
    "repo_name": (("web-app", ".github", "n" * 100), ("..", ".", "Web", "n" * 101)),
    "repo_owner": (("example-org", "group/sub"), ("Example", "-x", "g" * 101, "")),
    "rir_handle": (("ORG-GOGL-1-ARIN", "ORG-nG51-RIPE", "A1"), ("A", "ORG-", "ORG_1", "-ORG-1", "X" * 65)),
    "service_name": (("http", "ssh", "unknown"), ("X11", "ssl/http", "definitely-not-registered")),
    "sha256": (("a" * 64, "0" * 64), ("A" * 64, "a" * 63, "a" * 65)),
    "spf": (
        ("v=spf1", "v=spf1 -all", "v=spf1 " + "a" * 4089),
        ("v=spf10", " v=spf1", "v=spf1\t-all", "v=spf1 é", "v=spf1 " + "a" * 4090),
    ),
    "srv_label": (("_ldap", "_tcp", "_" + "b" * 62), ("ldap", "_LDAP", "_ldap-", "_" + "a" * 63)),
    "tech_token": (("nginx", "php_7.4+x", "a"), ("Nginx", "-nginx", "nginx-", "a" * 64, "ngin x", "")),
    "tech_version": (("1.18.0", "2.4.41-ubuntu", "x" * 64), ("", "1.18 beta", "x" * 65, "1.0\u00e9")),
    "tenant_id": (("dev-12345", "72f988bf-86f1-41af-91ab-2d7cd011db47"), ("Dev-12345", "", "-x", "a" * 129)),
    "tls_cipher_name": (
        ("TLS_AES_128_GCM_SHA256", "TLS_NULL_WITH_NULL_NULL"),
        ("tls_aes_128_gcm_sha256", "TLS_", "ECDHE-RSA-AES128-GCM-SHA256", "TLS_AES__128"),
    ),
    "tls_fingerprint_value": (("a" * 32, "2" * 62), ("A" * 32, "a" * 61, "a" * 33)),
    "txt_value": (("x", "x" * 4096, "v=spf1include:x"), ("", "x" * 4097, "café", "line\nbreak")),
    "uint8": ((0, 255), (-1, 256, True, "1")),
    "uint16": ((0, 65535), (-1, 65536, True, "1")),
    "uint32": ((0, 86400, 4294967295), (-1, 4294967296, True, "86400")),
    "uint63": ((0, 2**63 - 1), (-1, 2**63, True, 1.5)),
    "utc_timestamp": (
        ("1995-08-14T04:00:00Z", "2026-09-23T01:31:57.079Z", "2024-02-29T23:59:59.123456Z"),
        (
            "1995-08-14T04:00:00+00:00",
            "1995-08-14 04:00:00Z",
            "1995-08-14T04:00:00",
            "2025-02-29T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-01-01T24:00:00Z",
            "2026-01-01T00:00:00.1234567Z",
            "1995-08-14t04:00:00z",
        ),
    ),
}

# Check id -> (records the whole validation accepts, records the check itself rejects). Every
# rejected record satisfies its type's required map, so the rejection is the check's alone.
CHECKS: dict[str, tuple[tuple[Record, ...], tuple[Record, ...]]] = {
    "asn_assigned.1": ((("asn", {"value": 1}), ("asn", {"value": 4294967295})), (("asn", {"value": 0}),)),
    "bucket_name_spelling.1": (
        (
            ("storage_bucket", {"provider": "aws_s3", "name": "example-assets"}),
            ("storage_bucket", {"provider": "gcp_gcs", "name": "example.appspot.com"}),
        ),
        (
            ("storage_bucket", {"provider": "azure_blob", "name": "example-storage"}),
            ("storage_bucket", {"provider": "aws_s3", "name": "example-s3alias"}),
            ("storage_bucket", {"provider": "gcp_gcs", "name": "googtest"}),
        ),
    ),
    "cpe_product_level.1": (
        (
            ("technology", {"name": "nginx", "cpe": "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*"}),
            ("technology", {"name": "nginx", "cpe": "cpe:2.3:a:f5:nginx:-:*:*:*:*:*:*:*"}),
            ("technology", {"name": "nginx"}),
        ),
        (("technology", {"name": "nginx", "cpe": CPE_NGINX}),),
    ),
    "dns_name_kind.1": (
        (("domain", {"value": "example.com"}), ("subdomain", {"value": "api.example.com"})),
        (("domain", {"value": "api.example.com"}), ("subdomain", {"value": "example.com"})),
    ),
    "http_fingerprint_value_kind.1": (
        (
            ("http_fingerprint", {"kind": "favicon_mmh3", "value": "-1752256170"}),
            ("http_fingerprint", {"kind": "body_sha256", "value": "a" * 64}),
        ),
        (
            ("http_fingerprint", {"kind": "body_sha256", "value": "-1"}),
            ("http_fingerprint", {"kind": "favicon_mmh3", "value": "a" * 64}),
        ),
    ),
    "ip_address_version.1": (
        (("ip_address", {"value": "192.0.2.1", "version": 4}), ("ip_address", {"value": "2001:db8::1", "version": 6})),
        (("ip_address", {"value": "192.0.2.1", "version": 6}),),
    ),
    "ip_cidr_version.1": (
        (("ip_cidr", {"value": "192.0.2.0/24", "version": 4}),),
        (("ip_cidr", {"value": "192.0.2.0/24", "version": 6}),),
    ),
    "registrar_iana_assigned.1": (
        (("registrar", {"iana_id": 292, "name": "MarkMonitor Inc."}),),
        (("registrar", {"iana_id": 0, "name": "unset"}),),
    ),
    "port_number_assigned.1": (
        (("port", {"number": 1, "transport": "tcp"}), ("port", {"number": 65535, "transport": "udp"})),
        (("port", {"number": 0, "transport": "tcp"}),),
    ),
    "registry_domain_id_assigned.1": (
        (
            ("whois_registration", {"registry": "com", "registry_domain_id": "2138514_DOMAIN_COM-VRSN"}),
            ("whois_registration", {"registry": "fr", "registry_domain_id": "DOM000000113746-FRNIC"}),
        ),
        (
            ("whois_registration", {"registry": "com", "registry_domain_id": "REDACTED-REDACTED"}),
            ("whois_registration", {"registry": "com", "registry_domain_id": "0-XX"}),
            ("whois_registration", {"registry": "com", "registry_domain_id": "0000000-VRSN"}),
            ("whois_registration", {"registry": "io", "registry_domain_id": "NotDisclosed-IO"}),
        ),
    ),
    "repository_owner_spelling.1": (
        (
            ("repository", {"platform": "github", "host": "github.com", "owner": "example-org", "name": "web"}),
            ("repository", {"platform": "gitlab", "host": "git.example.com", "owner": "group/sub", "name": "api"}),
        ),
        (
            ("repository", {"platform": "github", "host": "github.com", "owner": "group/sub", "name": "web"}),
            ("repository", {"platform": "github", "host": "github.com", "owner": "ex--ample", "name": "web"}),
        ),
    ),
    "secret_plaintext_keys.1": (
        (
            ("secret", {"value_sha256": "a" * 64, "detector": "aws", "context": {"file": "a.env"}}),
            ("exposes_secret", {"location": "src/config.py", "commit": "a" * 40}),
        ),
        (
            ("secret", {"value_sha256": "a" * 64, "password": "hunter2"}),
            ("secret", {"value_sha256": "a" * 64, "Raw": "hunter2"}),
            ("secret", {"value_sha256": "a" * 64, "nested": [{"deeper": {"rawV2": "x"}}]}),
            ("exposes_secret", {"location": "src/config.py", "Line": "AWS_SECRET=wJalr"}),
        ),
    ),
    "service_secure_flag.1": (
        (("service", {"name": "http", "secure": True}), ("service", {"name": "ssh"})),
        (("service", {"name": "http"}), ("service", {"name": "http", "secure": 1})),
    ),
    "tenant_id_spelling.1": (
        (
            ("identity_tenant", {"provider": "okta", "tenant_id": "dev-12345"}),
            ("identity_tenant", {"provider": "entra_id", "tenant_id": "72f988bf-86f1-41af-91ab-2d7cd011db47"}),
        ),
        (
            ("identity_tenant", {"provider": "entra_id", "tenant_id": "not-a-uuid"}),
            ("identity_tenant", {"provider": "okta", "tenant_id": "acme.corp"}),
        ),
    ),
    "tls_fingerprint_length.1": (
        (("tls_fingerprint", {"kind": "jarm", "value": "2" * 62}),),
        (("tls_fingerprint", {"kind": "ja3s", "value": "2" * 62}),),
    ),
    "txt_record_diversion.1": (
        (("txt_record", {"value": "v=spf1include:_spf.example.com ~all"}), ("txt_record", {"value": "V=SPF1 -all"})),
        (("txt_record", {"value": "v=spf1 -all"}), ("txt_record", {"value": "v=STSv1; id=1"})),
    ),
}

# Endpoint check id -> (accepted, rejected) as (relation properties, source record, target record).
ENDPOINT_CHECKS: dict[str, tuple[tuple[Endpoint, ...], tuple[Endpoint, ...]]] = {
    "contains_cidr_proper_subnet.1": (
        (
            (
                {},
                ("ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
                ("ip_cidr", {"value": "10.2.3.0/24", "version": 4}),
            ),
        ),
        (
            (
                {},
                ("ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
                ("ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
            ),
            (
                {},
                ("ip_cidr", {"value": "10.0.0.0/16", "version": 4}),
                ("ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
            ),
            (
                {},
                ("ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
                ("ip_cidr", {"value": "2001:db8::/32", "version": 6}),
            ),
        ),
    ),
    "contains_ip_member.1": (
        (
            (
                {},
                ("ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
                ("ip_address", {"value": "192.0.2.255", "version": 4}),
            ),
        ),
        (
            (
                {},
                ("ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
                ("ip_address", {"value": "192.0.3.1", "version": 4}),
            ),
            (
                {},
                ("ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
                ("ip_address", {"value": "2001:db8::1", "version": 6}),
            ),
        ),
    ),
    "has_contact_registration_roles.1": (
        (
            (
                {"role": "registrant"},
                ("whois_registration", {"registry": "com", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}),
                ("email_address", {"value": "hostmaster@example.com"}),
            ),
            (
                {"role": "billing"},
                ("whois_registration", {"registry": "com", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}),
                ("phone", {"value": "+14155552671"}),
            ),
            (
                {"role": "abuse"},
                ("domain", {"value": "example.com"}),
                ("email_address", {"value": "hostmaster@example.com"}),
            ),
            (
                {"role": "admin"},
                ("organization", {"registry": "arin", "handle": "ORG-1"}),
                ("email_address", {"value": "hostmaster@example.com"}),
            ),
        ),
        (
            (
                {"role": "registrant"},
                ("domain", {"value": "example.com"}),
                ("email_address", {"value": "hostmaster@example.com"}),
            ),
            (
                {"role": "tech"},
                ("subdomain", {"value": "www.example.com"}),
                ("email_address", {"value": "hostmaster@example.com"}),
            ),
            (
                {"role": "abuse"},
                ("whois_registration", {"registry": "com", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}),
                ("email_address", {"value": "hostmaster@example.com"}),
            ),
        ),
    ),
    "has_registration_suffix_match.1": (
        (
            (
                {},
                ("domain", {"value": "example.com"}),
                ("whois_registration", {"registry": "com", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}),
            ),
            (
                {},
                ("domain", {"value": "example.co.uk"}),
                ("whois_registration", {"registry": "co.uk", "registry_domain_id": "EXAMPLE1-UK"}),
            ),
        ),
        (
            (
                {},
                ("domain", {"value": "example.co.uk"}),
                ("whois_registration", {"registry": "uk", "registry_domain_id": "EXAMPLE1-UK"}),
            ),
            (
                {},
                ("domain", {"value": "example.com"}),
                ("whois_registration", {"registry": "net", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}),
            ),
        ),
    ),
    "has_subdomain_suffix.1": (
        (
            ({}, ("domain", {"value": "example.com"}), ("subdomain", {"value": "api.dev.example.com"})),
            ({}, ("subdomain", {"value": "dev.example.com"}), ("subdomain", {"value": "api.dev.example.com"})),
        ),
        (
            ({}, ("domain", {"value": "example.com"}), ("subdomain", {"value": "api.fakeexample.com"})),
            ({}, ("subdomain", {"value": "api.example.com"}), ("subdomain", {"value": "api.example.com"})),
        ),
    ),
}

# Canonicalization id -> (rewrites as (type, before, after), spellings left untouched as (type, value)).
CANONICALIZATIONS: dict[str, tuple[tuple[Rewrite, ...], tuple[Record, ...]]] = {
    "caa_parameter_name_fold.1": (
        (
            (
                "caa_issue",
                {"flags": 0, "parameters": [{"name": "accountURI", "value": "x", "seen": 2}]},
                {"flags": 0, "parameters": [{"name": "accounturi", "value": "x", "seen": 2}]},
            ),
        ),
        (
            ("caa_issuewild", {"flags": 0, "parameters": [{"name": "accounturi", "value": "x"}]}),
            ("caa_issue", {"flags": 0, "parameters": [{"name": "\u212a", "value": "x"}]}),
            ("caa_issue", {"flags": 0, "parameters": "text"}),
        ),
    ),
    "endpoint_url_drop_query.1": (
        (
            (
                "endpoint",
                {"url": "https://api.example.com/search?q=1&page=2", "method": "GET"},
                {"url": "https://api.example.com/search", "method": "GET"},
            ),
            (
                "endpoint",
                {"url": "https://api.example.com/?", "method": "GET"},
                {"url": "https://api.example.com/", "method": "GET"},
            ),
        ),
        (
            ("endpoint", {"url": "https://api.example.com/search", "method": "GET", "title": "Search?x"}),
            ("endpoint", {"url": "https://api.example.com/search?a b<>", "method": "GET"}),
            ("endpoint", {"url": "https://api.example.com/search?q=%zz", "method": "GET"}),
        ),
    ),
}
