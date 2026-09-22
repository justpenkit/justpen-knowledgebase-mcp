"""Catalog v2 manifest, discovery schema, and strict record validators."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from .errors import ExpectedValidationError
from .mutations import validate_properties
from .psl import classify_dns_name
from .service_names import is_service_name, secure_required

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

CATALOG_VERSION = 2

_COMMON = {
    "additional_properties": True,
    "coercion": False,
    "depth": 16,
    "integers": "signed64",
    "numbers": "finite double",
    "properties_bytes": 65536,
    "required_nonnull": True,
}

_FORMATS = {
    "alpn_tokens": (
        "An array of zero or more ASCII tokens matching `[A-Za-z0-9./_-]{1,255}`; order is not identity-significant "
        "and duplicates are preserved."
    ),
    "asn": "A strict JSON integer from 0 through 4294967295.",
    "bucket_name": (
        "The provider-global name of an object-storage bucket: 3 to 222 lowercase ASCII characters from "
        "letters, digits, hyphen, underscore and dot, starting and ending alphanumeric, without a doubled "
        "dot, and never a dotted-quad IPv4 address. Every provider is stricter than this union rule, and "
        "the declared provider fixes which of the narrower spellings is accepted."
    ),
    "caa_parameters": (
        "An array of objects with name and value strings. Names start alphanumeric and continue alphanumeric or "
        "hyphen. Values are empty or use ASCII 0x21-0x3A and 0x3C-0x7E."
    ),
    "cidr": "Canonical strict IPv4 or IPv6 network with an explicit prefix length.",
    "cpe23_or_empty": (
        "The empty string, or a lowercase CPE 2.3 formatted string (NIST IR 7695): 'cpe:2.3:' followed by the "
        "part and ten colon-separated components, each '*', '-', or an escaped attribute value optionally "
        "anchored by '*' or a run of '?', at most 512 characters. The legacy 'cpe:/' URI binding is rejected. "
        "No required map references this rule, so a stored cpe property is never checked against it; the rule "
        "states the spelling writers must produce and readers must re-validate."
    ),
    "cve": "A string matching `CVE-[0-9]{4}-[0-9]{4,}` exactly.",
    "cwe": "A string matching `CWE-[0-9]{1,6}` exactly, uppercase as MITRE publishes it.",
    "dkim_selector": (
        "A lowercase ASCII DKIM selector of at most 253 bytes - in practice far less, since the owner name "
        "`<selector>._domainkey.<domain>` must itself fit in 253 bytes - as one or more dot-separated labels "
        "of at most 63 bytes each, written without the `_domainkey` suffix or the domain."
    ),
    "dmarc": (
        "Printable ASCII of at most 4096 characters beginning with 'v=DMARC1' followed by a semicolon, a normal "
        "space, or end of text."
    ),
    "dns_name": (
        "A lowercase ASCII domain or subdomain spelling classified by the bundled ICANN PSL; at least two labels, "
        "labels at most 63 bytes, total at most 253 bytes, and no trailing dot."
    ),
    "dns_or_explicit_empty": "dns_name or the explicit empty string for no SNI offer; IP literals are rejected.",
    "email_address": (
        "A lowercase ASCII mailbox of at most 254 characters: an RFC 5322 dot-atom local part of at most 64 "
        "characters, one `@`, and a domain that satisfies dns_name. Quoted local parts, address literals and "
        "display names are rejected. A local part is case-sensitive on the wire; this rule requires the "
        "lowercase spelling anyway, so one mailbox is one node."
    ),
    "http_fingerprint_value": (
        "Either a signed 32-bit decimal integer written in ASCII without a leading zero or a plus sign, for a "
        "MurmurHash3 favicon hash, or exactly 64 lowercase hexadecimal characters for a response digest. The "
        "declared kind fixes which one is accepted."
    ),
    "http_url": (
        "Canonical absolute ASCII http/https URL with lowercase host, mandatory path, no userinfo, fragment, "
        "whitespace, backslash, Unicode, default explicit port, dot path segment, or lowercase percent escape. "
        "A submitted query string is validated and then removed before identity and storage, so one endpoint "
        "holds one path; parameter names belong to parameter nodes."
    ),
    "ip": "Canonical IPv4Address.compressed or lowercase IPv6Address.compressed spelling, without scope or prefix.",
    "ip_version": "A strict JSON integer equal to 4 or 6.",
    "method": "One to 32 characters matching an uppercase HTTP method token.",
    "mta_sts": (
        "Printable ASCII of at most 4096 characters beginning with 'v=STSv1' followed by a semicolon, a normal "
        "space, or end of text: the TXT record at `_mta-sts.<domain>`, not the policy file body."
    ),
    "parameter_name": (
        "1 to 128 printable ASCII characters without space, `&`, `=`, or `#`; one single parameter name, "
        "never a raw query string."
    ),
    "phone_e164": "An E.164 number: `+`, a leading digit from 1 through 9, and in total 2 to 15 digits.",
    "printable_text_1024": "A string of 1-1024 printable Unicode characters.",
    "printable_text_200": "A string of 1-200 printable Unicode characters.",
    "redirect_status": "A strict JSON integer in 301, 302, 303, 307, or 308.",
    "repo_name": (
        "A 1-100 character lowercase ASCII repository name from letters, digits, dot, underscore and hyphen, "
        "holding at least one alphanumeric character and never the reserved `.` or `..`. Hosting platforms "
        "resolve names case-insensitively, so the lowercase spelling is required to keep one repository one node."
    ),
    "repo_owner": (
        "A 1-255 character lowercase ASCII owner path of one or more `/`-separated segments, each 1-100 "
        "characters from letters, digits, dot, underscore and hyphen and each starting and ending "
        "alphanumeric. The separator exists for nested GitLab groups; the declared platform fixes whether "
        "more than one segment is accepted."
    ),
    "rir_handle": (
        "A regional-registry object handle of 2 to 64 ASCII characters, starting and ending alphanumeric and "
        "continuing alphanumeric or hyphen. Handles are case-sensitive and stored exactly as the registry "
        "publishes them: RIPE and AFRINIC derive them from the organisation name and preserve its case "
        "(ORG-nG51-RIPE, ORG-Ab1-AFRINIC), so a writer must never uppercase one. Handles are unique within one "
        "registry, never across registries."
    ),
    "service_name": "A member of the bundled versioned service name whitelist.",
    "sha256": "Exactly 64 lowercase ASCII hexadecimal characters.",
    "spf": "Printable ASCII beginning with `v=spf1` followed by a normal space or end of text.",
    "srv_label": "A 2-63 byte lowercase ASCII SRV label beginning with underscore.",
    "tech_token": (
        "A 1-63 character lowercase ASCII technology slug that starts and ends alphanumeric and may contain "
        "interior dot, underscore, plus, or hyphen."
    ),
    "tenant_id": (
        "A 1-128 character lowercase ASCII identity-tenant identifier that starts and ends alphanumeric and "
        "may contain interior dot, underscore or hyphen. The declared provider fixes the narrower spelling, "
        "and every member of the provider enum has one: a canonical lowercase UUID for Entra ID, a bare "
        "organization slug for Okta."
    ),
    "tls_cipher_name": (
        "The spelling of an IANA TLS cipher suite name: 5 to 128 uppercase ASCII characters beginning 'TLS_', "
        "with underscore-separated alphanumeric components. Shape only; membership in the IANA registry is not "
        "checked, so a well-formed name that no suite bears is accepted."
    ),
    "tls_fingerprint_value": (
        "Exactly 32 lowercase hexadecimal characters for a JA3S MD5 digest, or exactly 62 for a JARM "
        "fingerprint. The declared kind fixes which length is accepted."
    ),
    "txt_value": (
        "1 to 4096 printable ASCII characters, the concatenated and unquoted character-strings of one TXT RRset."
    ),
    "uint8": "A strict JSON integer from 0 through 255.",
    "uint16": "A strict JSON integer from 0 through 65535.",
}


def _identity(
    properties: list[str],
    *,
    scope: dict[str, str] | None = None,
    order_independent: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"properties": properties}
    if scope is not None:
        result["scope"] = scope
    if order_independent is not None:
        result["order_independent"] = order_independent
    return result


def _relation(
    sources: list[str],
    targets: list[str],
    *,
    required: dict[str, str | list[str]] | None = None,
    identity: dict[str, object] | None = None,
    self_edge: bool = False,
) -> dict[str, object]:
    return {
        "identity": identity if identity is not None else _identity([]),
        "required": required if required is not None else {},
        "self_edge": self_edge,
        "sources": sources,
        "targets": targets,
    }


_SCOPE_OPEN_PORT = {"relation": "has_open_port", "endpoint": "source"}
_SCOPE_SERVICE = {"relation": "has_service", "endpoint": "source"}
_SCOPE_FINDING = {"relation": "has_finding", "endpoint": "source"}
_SCOPE_DKIM = {"relation": "has_dkim_selector", "endpoint": "source"}
_SCOPE_PARAMETER = {"relation": "has_parameter", "endpoint": "source"}
_SCOPE_MTA_STS = {"relation": "has_mta_sts_policy", "endpoint": "source"}
_CAA_ORDER: dict[str, object] = {
    "property": "parameters",
    "algorithm": "sha256",
    "projection": ["name", "value"],
    "sort": ["name", "value"],
    "preserve_duplicates": True,
}
_SVCB_ALPN_ORDER: dict[str, object] = {
    "property": "alpn",
    "algorithm": "sha256",
    "sort": "value",
    "preserve_duplicates": True,
}
_ALPN_ORDER: dict[str, object] = {
    "property": "alpn_offered",
    "algorithm": "sha256",
    "sort": "value",
    "preserve_duplicates": True,
}

_NODES = {
    "asn": {"identity": _identity(["value"]), "required": {"value": "asn"}},
    "certificate": {
        "identity": _identity(["der_sha256"]),
        "required": {"der_sha256": "sha256"},
    },
    "cve": {"identity": _identity(["value"]), "required": {"value": "cve"}},
    "cwe": {"identity": _identity(["value"]), "required": {"value": "cwe"}},
    "dkim_record": {
        "identity": _identity(["selector"], scope=_SCOPE_DKIM),
        "required": {"selector": "dkim_selector", "value": "txt_value"},
    },
    "dmarc_record": {"identity": _identity(["value"]), "required": {"value": "dmarc"}},
    "domain": {"identity": _identity(["value"]), "required": {"value": "dns_name"}},
    "email_address": {"identity": _identity(["value"]), "required": {"value": "email_address"}},
    "endpoint": {
        "identity": _identity(["url", "method"]),
        "required": {"url": "http_url", "method": "method"},
    },
    "finding": {
        "identity": _identity(["title"], scope=_SCOPE_FINDING),
        "required": {
            "title": "printable_text_200",
            "severity": ["info", "low", "medium", "high", "critical"],
        },
    },
    "host_key": {
        "identity": _identity(["algorithm", "fingerprint_sha256"]),
        "required": {
            "algorithm": [
                "ssh-rsa",
                "ssh-dss",
                "ssh-ed25519",
                "ecdsa-sha2-nistp256",
                "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp521",
                "sk-ssh-ed25519@openssh.com",
                "sk-ecdsa-sha2-nistp256@openssh.com",
            ],
            "fingerprint_sha256": "sha256",
        },
    },
    "http_fingerprint": {
        "identity": _identity(["kind", "value"]),
        "required": {
            "kind": ["favicon_mmh3", "body_sha256", "header_sha256"],
            "value": "http_fingerprint_value",
        },
    },
    "identity_tenant": {
        "identity": _identity(["provider", "tenant_id"]),
        "required": {"provider": ["entra_id", "okta"], "tenant_id": "tenant_id"},
    },
    "ip_address": {
        "identity": _identity(["value"]),
        "required": {"value": "ip", "version": "ip_version"},
    },
    "ip_cidr": {
        "identity": _identity(["value"]),
        "required": {"value": "cidr", "version": "ip_version"},
    },
    "mta_sts_policy": {
        "identity": _identity(["value"], scope=_SCOPE_MTA_STS),
        "required": {"value": "mta_sts"},
    },
    "organization": {
        "identity": _identity(["registry", "handle"]),
        "required": {
            "registry": ["arin", "ripe", "apnic", "lacnic", "afrinic"],
            "handle": "rir_handle",
        },
    },
    "parameter": {
        "identity": _identity(["name", "location"], scope=_SCOPE_PARAMETER),
        "required": {
            "name": "parameter_name",
            "location": ["query", "body", "header", "cookie", "path"],
        },
    },
    "phone": {"identity": _identity(["value"]), "required": {"value": "phone_e164"}},
    "port": {
        "identity": _identity(["transport", "number"], scope=_SCOPE_OPEN_PORT),
        "required": {"number": "uint16", "transport": ["tcp", "udp", "sctp"]},
    },
    "registrar": {
        "identity": _identity(["iana_id"]),
        "required": {"iana_id": "uint16", "name": "printable_text_200"},
    },
    "repository": {
        "identity": _identity(["host", "owner", "name"]),
        "required": {
            "platform": ["github", "gitlab", "bitbucket", "gitea"],
            "host": "dns_name",
            "owner": "repo_owner",
            "name": "repo_name",
        },
    },
    "secret": {
        "identity": _identity(["value_sha256"]),
        "required": {"value_sha256": "sha256"},
    },
    "service": {
        "identity": _identity(["name"], scope=_SCOPE_SERVICE),
        "required": {"name": "service_name"},
    },
    "spf_record": {"identity": _identity(["value"]), "required": {"value": "spf"}},
    "storage_bucket": {
        "identity": _identity(["provider", "name"]),
        "required": {"provider": ["aws_s3", "gcp_gcs", "azure_blob"], "name": "bucket_name"},
    },
    "subdomain": {"identity": _identity(["value"]), "required": {"value": "dns_name"}},
    "technology": {
        "identity": _identity(["name"]),
        "required": {"name": "tech_token"},
    },
    "tls_cipher_suite": {
        "identity": _identity(["version", "name"]),
        "required": {
            "version": ["ssl30", "tls10", "tls11", "tls12", "tls13", "dtls10", "dtls12", "dtls13"],
            "name": "tls_cipher_name",
        },
    },
    "tls_fingerprint": {
        "identity": _identity(["kind", "value"]),
        "required": {"kind": ["jarm", "ja3s"], "value": "tls_fingerprint_value"},
    },
    "txt_record": {"identity": _identity(["value"]), "required": {"value": "txt_value"}},
}

_D = ["domain", "subdomain"]
_RELATIONS = {
    "affected_by": _relation(["service", "finding", "endpoint"], ["cve"]),
    "announced_by": _relation(["ip_cidr"], ["asn"]),
    "backed_by_bucket": _relation(["domain", "subdomain", "endpoint"], ["storage_bucket"]),
    "caa_issue": _relation(
        _D,
        _D,
        required={"flags": "uint8", "parameters": "caa_parameters"},
        identity=_identity(["flags", "parameters"], order_independent=_CAA_ORDER),
        self_edge=True,
    ),
    "caa_issuewild": _relation(
        _D,
        _D,
        required={"flags": "uint8", "parameters": "caa_parameters"},
        identity=_identity(["flags", "parameters"], order_independent=_CAA_ORDER),
        self_edge=True,
    ),
    "cname_to": _relation(_D, _D, self_edge=True),
    "contains_cidr": _relation(["ip_cidr"], ["ip_cidr"]),
    "contains_ip": _relation(["ip_cidr"], ["ip_address"]),
    "covers_name": _relation(
        ["certificate"],
        _D,
        required={"coverage": ["exact", "wildcard"]},
        identity=_identity(["coverage"]),
    ),
    "dname_to": _relation(_D, _D, self_edge=True),
    "exposes_secret": _relation(
        ["repository", "endpoint", "storage_bucket"],
        ["secret"],
        required={"location": "printable_text_1024"},
        identity=_identity(["location"]),
    ),
    "federates_with": _relation(_D, ["identity_tenant"]),
    "has_contact": _relation(
        ["organization", "registrar", "domain", "subdomain", "repository"],
        ["email_address", "phone"],
        required={"role": ["abuse", "admin", "tech", "registrant", "billing", "noc", "security", "published"]},
        identity=_identity(["role"]),
    ),
    "has_dkim_selector": _relation(_D, ["dkim_record"]),
    "has_dmarc": _relation(_D, ["dmarc_record"]),
    "has_finding": _relation(
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
        ],
        ["finding"],
    ),
    "has_http_fingerprint": _relation(["endpoint"], ["http_fingerprint"]),
    "has_mail_exchange": _relation(
        _D,
        _D,
        required={"preference": "uint16"},
        identity=_identity(["preference"]),
        self_edge=True,
    ),
    "has_mta_sts_policy": _relation(_D, ["mta_sts_policy"]),
    "has_nameserver": _relation(_D, _D, self_edge=True),
    "has_open_port": _relation(["ip_address"], ["port"]),
    "has_parameter": _relation(["endpoint"], ["parameter"]),
    "has_service": _relation(["port"], ["service"]),
    "has_soa_primary": _relation(_D, _D, self_edge=True),
    "has_spf": _relation(_D, ["spf_record"]),
    "has_srv_target": _relation(
        _D,
        _D,
        required={
            "service": "srv_label",
            "protocol": "srv_label",
            "port": "uint16",
            "priority": "uint16",
            "weight": "uint16",
        },
        identity=_identity(["service", "protocol", "port", "priority", "weight"]),
        self_edge=True,
    ),
    "has_subdomain": _relation(_D, ["subdomain"]),
    "has_svcb_binding": _relation(
        _D,
        _D,
        required={"record_type": ["https", "svcb"], "priority": "uint16", "alpn": "alpn_tokens"},
        identity=_identity(["record_type", "priority", "alpn"], order_independent=_SVCB_ALPN_ORDER),
        self_edge=True,
    ),
    "has_tls_fingerprint": _relation(["service"], ["tls_fingerprint"]),
    "has_txt_record": _relation(_D, ["txt_record"]),
    "has_weakness": _relation(["finding", "cve"], ["cwe"]),
    "issued_by": _relation(["certificate"], ["certificate"], self_edge=True),
    "operated_by": _relation(["asn", "ip_cidr"], ["organization"]),
    "owns_repository": _relation(_D, ["repository"]),
    "presents_certificate": _relation(
        ["service"],
        ["certificate"],
        required={
            "mode": ["tls", "dtls", "starttls", "quic"],
            "server_name": "dns_or_explicit_empty",
            "alpn_offered": "alpn_tokens",
        },
        identity=_identity(["mode", "server_name", "alpn_offered"], order_independent=_ALPN_ORDER),
    ),
    "presents_host_key": _relation(["service"], ["host_key"]),
    "protected_by": _relation(
        ["service", "endpoint", "domain", "subdomain"],
        ["technology"],
        required={"kind": ["waf", "cdn", "reverse_proxy", "load_balancer"]},
        identity=_identity(["kind"]),
    ),
    "redirects_to": _relation(
        ["endpoint"],
        ["endpoint"],
        required={"status": "redirect_status"},
        identity=_identity(["status"]),
        self_edge=True,
    ),
    "registered_through": _relation(["domain"], ["registrar"]),
    "resolves_to": _relation(_D, ["ip_address"]),
    "reverse_resolves_to": _relation(["ip_address"], _D),
    "runs_technology": _relation(["service", "endpoint", "domain", "subdomain"], ["technology"]),
    "serves_endpoint": _relation(["service"], ["endpoint"]),
    "supports_tls_cipher": _relation(["service"], ["tls_cipher_suite"]),
}

_CATALOG = {
    "common": _COMMON,
    "formats": _FORMATS,
    "nodes": _NODES,
    "relations": _RELATIONS,
    "version": CATALOG_VERSION,
}


def scope_relations() -> dict[str, str]:
    """Map every parent-scoped node type to the relation that supplies its single parent."""
    return {
        name: cast("dict[str, str]", definition["identity"]["scope"])["relation"]
        for name, definition in _NODES.items()
        if "scope" in cast("dict[str, Any]", definition["identity"])
    }


def scope_order() -> tuple[str, ...]:
    """Order scoped node types parent-first, so a scoped parent is resolved before its scoped child.

    A write resolves scoped nodes in this order, so a type whose scope relation accepts another
    scoped type as a source must come later. Deriving the order keeps that true by construction
    rather than by remembering to reorder a hand-written tuple.
    """
    scoped = scope_relations()
    pending = {
        child: {source for source in cast("list[str]", _RELATIONS[relation]["sources"]) if source in scoped}
        for child, relation in scoped.items()
    }
    resolved: list[str] = []
    while pending:
        ready = sorted(child for child, parents in pending.items() if parents.issubset(resolved))
        if not ready:
            raise RuntimeError("parent-scoped node types form a cycle")
        resolved.extend(ready)
        for child in ready:
            del pending[child]
    return tuple(resolved)


def _ensure_scope_contract() -> None:
    """Fail at import if a scope declaration names something the storage layer cannot honor."""
    for child, relation in scope_relations().items():
        definition = cast("dict[str, Any] | None", _RELATIONS.get(relation))
        scope = cast("dict[str, str]", cast("dict[str, Any]", _NODES[child]["identity"])["scope"])
        if definition is None or scope["endpoint"] != "source" or child not in definition["targets"]:
            raise RuntimeError(f"scope declaration for {child} is not a source-scoped relation target")
        if re.fullmatch(r"[a-z_]+", relation) is None:
            raise RuntimeError(f"scope relation name {relation} is not a bare identifier")
    scope_order()


_ensure_scope_contract()

# The canonical serialized contract is immutable; callers receive a fresh tree.
CATALOG_JSON = json.dumps(_CATALOG, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _ensure_data_loaded() -> None:
    """Load and verify bundled registries while the catalog module imports."""
    classify_dns_name("example.com")
    if not is_service_name("unknown"):
        raise RuntimeError("bundled service name registry is missing required sentinel")


def _catalog_fingerprint() -> str:
    _ensure_data_loaded()
    return hashlib.sha256(CATALOG_JSON.encode("utf-8")).hexdigest()


CATALOG_FINGERPRINT = _catalog_fingerprint()


def catalog_manifest() -> dict[str, Any]:
    """Return an isolated copy of the fixed executable catalog contract."""
    return cast("dict[str, Any]", json.loads(CATALOG_JSON))


def _read_only(value: object) -> object:
    """Rebuild one parsed catalog subtree out of containers that refuse in-place mutation."""
    if isinstance(value, dict):
        return MappingProxyType({key: _read_only(item) for key, item in cast("dict[str, object]", value).items()})
    if isinstance(value, list):
        return tuple(_read_only(item) for item in cast("list[object]", value))
    return value


# `catalog_manifest()` re-parses `CATALOG_JSON` on every call, and the write path reaches it several
# times per record inside `BEGIN IMMEDIATE`. This is the same parse performed once at import, for
# callers that only read. The wrapping goes all the way down rather than proxying the top mapping
# alone: a shallow proxy would still let a reader mutate one type definition in place and corrupt
# the catalog for the rest of the process, which is the exact failure this handle must not enable.
_CATALOG_VIEW: Mapping[str, Any] = cast("Mapping[str, Any]", _read_only(json.loads(CATALOG_JSON)))


def catalog_view() -> Mapping[str, Any]:
    """Return the shared read-only catalog; callers that mutate use `catalog_manifest()` instead.

    Mappings are `MappingProxyType` and JSON arrays are tuples, at every depth, so the structure
    cannot be edited by accident and cannot be handed onward into a response that edits it.
    """
    return _CATALOG_VIEW


# Both CAA relation types carry the same parameter list, and `_CAA_ORDER` makes it identity-bearing.
# RFC 8659 parameter tags are case-insensitive, so `accounturi` and `accountURI` described one fact
# and forked it into two edges. The fold belongs here rather than in `identity.py`: canonicalizing
# the property keeps the stored spelling and the identity derived from it in agreement, exactly as
# `endpoint.url` already does, whereas folding only the digest would leave one edge holding whichever
# spelling was written last. Declaring the rule in the catalog is not available - it changes
# `CATALOG_JSON` and with it the fingerprint `SchemaGuard.check` compares for exact equality, which
# no existing workspace can be upgraded past.
_CAA_PARAMETER_TYPES = ("caa_issue", "caa_issuewild")


def _canonicalize_caa_parameters(properties: dict[str, Any]) -> None:
    """Fold ASCII parameter tags only; a tag this leaves alone is one validation refuses anyway.

    `"\u212a".lower()` is `"k"`, so folding a non-ASCII name would admit a spelling
    `_valid_caa_parameters` rejects today. Canonicalization runs before validation and must not
    widen it.
    """
    parameters = properties.get("parameters")
    if type(parameters) is not list:
        return
    items = cast("list[Any]", parameters)
    if any(type(item) is not dict or type(cast("dict[str, Any]", item).get("name")) is not str for item in items):
        return
    folded = [(cast("dict[str, Any]", item), cast("str", item["name"])) for item in items]
    properties["parameters"] = [{**item, "name": name.lower() if name.isascii() else name} for item, name in folded]


def canonicalize_record(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    """Rewrite the declared non-canonical spellings in place before identity and storage.

    Two rewrites exist. `endpoint.url` drops its query string, so one path is one node rather than
    one node per observed parameter value; parameter names live on `parameter` nodes. A CAA
    parameter name is case-folded, because the tag is case-insensitive on the wire while the
    parameter list is identity-bearing. Both are value canonicalization, not the JSON type coercion
    `_COMMON["coercion"]` refuses.
    """
    if kind == "nodes" and type_name == "endpoint":
        url = properties.get("url")
        if type(url) is str and "?" in url:
            properties["url"] = url.split("?", 1)[0]
    elif kind == "relations" and type_name in _CAA_PARAMETER_TYPES:
        _canonicalize_caa_parameters(properties)


def _published_rule(rule: str | list[str] | tuple[str, ...]) -> str | list[str]:
    """Spell one required rule the way clients already receive it, whatever container holds it."""
    return list(rule) if isinstance(rule, tuple) else rule


def validate_record(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    """Canonicalize declared spellings, then enforce required properties and cross-field rules."""
    canonicalize_record(kind, type_name, properties)
    validate_properties(properties)
    manifest = catalog_view()
    if kind not in ("nodes", "relations"):
        raise ExpectedValidationError("unknown catalog type")
    definitions = cast("Mapping[str, Mapping[str, Any]]", manifest[kind])
    if type_name not in definitions:
        raise ExpectedValidationError("unknown catalog type")
    required = cast("Mapping[str, str | list[str] | tuple[str, ...]]", definitions[type_name]["required"])
    for field, rule in required.items():
        if field not in properties or not _valid_field(properties[field], rule):
            # The shared view spells a JSON array as a tuple; the published message keeps the list
            # spelling clients already receive, so routing this read changes no client-visible text.
            raise ExpectedValidationError(f"/properties/{field}: expected {_published_rule(rule)}")
    _validate_cross_fields(kind, type_name, properties)


def _cross_field_dns_name(type_name: str, properties: dict[str, Any]) -> None:
    value = properties["value"]
    if type(value) is not str or _dns_kind(value) != type_name:
        raise ExpectedValidationError("/properties/value: DNS name type mismatch")


def _cross_field_ip_address(_type_name: str, properties: dict[str, Any]) -> None:
    value = properties["value"]
    address = _parse_ip(value) if type(value) is str else None
    if address is None or address.version != properties["version"]:
        raise ExpectedValidationError("IP address version mismatch")


def _cross_field_ip_cidr(_type_name: str, properties: dict[str, Any]) -> None:
    value = properties["value"]
    network = _parse_cidr(value) if type(value) is str else None
    if network is None or network.version != properties["version"]:
        raise ExpectedValidationError("IP network version mismatch")


def _cross_field_service(_type_name: str, properties: dict[str, Any]) -> None:
    name = cast("str", properties["name"])
    if secure_required(name) and ("secure" not in properties or type(properties["secure"]) is not bool):
        raise ExpectedValidationError("/properties/secure: expected boolean")


def _cross_field_tls_fingerprint(_type_name: str, properties: dict[str, Any]) -> None:
    expected = 62 if properties["kind"] == "jarm" else 32
    if len(cast("str", properties["value"])) != expected:
        raise ExpectedValidationError("/properties/value: fingerprint length does not match kind")


def _cross_field_registrar(_type_name: str, properties: dict[str, Any]) -> None:
    if cast("int", properties["iana_id"]) < 1:
        raise ExpectedValidationError("/properties/iana_id: expected an assigned IANA registrar id")


# One dedicated node type per version tag. Diverting on the bare prefix left a malformed tag with
# no home at all: `v=spf1include:...`, a missing space after the version tag and the most common
# real SPF misconfiguration, was refused by `spf_record` for the absent delimiter and by
# `txt_record` for the prefix. The diversion therefore asks the dedicated type's own value rule,
# read from the catalog rather than restated here, so the two acceptances partition every spelling
# of a tag. `dkim_record` validates its value as `txt_value`, so every `v=DKIM1` spelling already
# has a home and the bare prefix remains the whole rule for it.
_TXT_RECORD_DIVERSIONS: tuple[tuple[str, str], ...] = (
    ("v=spf1", "spf_record"),
    ("v=DMARC1", "dmarc_record"),
    ("v=DKIM1", "dkim_record"),
    ("v=STSv1", "mta_sts_policy"),
)


def _cross_field_txt_record(_type_name: str, properties: dict[str, Any]) -> None:
    value = cast("str", properties["value"])
    definitions = cast("Mapping[str, Mapping[str, Any]]", catalog_view()["nodes"])
    for tag, type_name in _TXT_RECORD_DIVERSIONS:
        if value.startswith(tag) and _valid_field(value, definitions[type_name]["required"]["value"]):
            raise ExpectedValidationError("/properties/value: use the dedicated TXT record type")


# The digest kinds share one rule; favicon_mmh3 is the one that does not.
_FINGERPRINT_DIGEST_KINDS = ("body_sha256", "header_sha256")
_SHA256_TEXT = re.compile(r"[0-9a-f]{64}")


def _cross_field_http_fingerprint(_type_name: str, properties: dict[str, Any]) -> None:
    value = cast("str", properties["value"])
    if properties["kind"] == "favicon_mmh3":
        if not _valid_signed_int32_text(value):
            raise ExpectedValidationError("/properties/value: favicon_mmh3 expects a signed 32-bit integer")
    elif _SHA256_TEXT.fullmatch(value) is None:
        raise ExpectedValidationError("/properties/value: a response digest expects 64 lowercase hex characters")


# One entry per provider enum member; _ensure_cross_field_contract refuses a missing one at import.
_TENANT_RULES: dict[str, str] = {
    "entra_id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
    "okta": r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?",
}


def _cross_field_identity_tenant(_type_name: str, properties: dict[str, Any]) -> None:
    provider = cast("str", properties["provider"])
    if re.fullmatch(_TENANT_RULES[provider], cast("str", properties["tenant_id"])) is None:
        raise ExpectedValidationError(f"/properties/tenant_id: not a {provider} tenant spelling")


# None means the platform nests owner segments, so repo_owner is already the whole rule.
_REPO_OWNER_RULES: dict[str, tuple[int, str] | None] = {
    "github": (39, r"[a-z0-9](?:-?[a-z0-9])*"),
    "bitbucket": (62, r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?"),
    "gitea": (40, r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?"),
    "gitlab": None,
}


def _cross_field_repository(_type_name: str, properties: dict[str, Any]) -> None:
    platform, owner = cast("str", properties["platform"]), cast("str", properties["owner"])
    rule = _REPO_OWNER_RULES[platform]
    if rule is None:
        return
    if "/" in owner:
        raise ExpectedValidationError(f"/properties/owner: a {platform} owner holds one segment")
    limit, grammar = rule
    if len(owner) > limit or re.fullmatch(grammar, owner) is None:
        raise ExpectedValidationError(f"/properties/owner: not a {platform} owner spelling")


# A secret node holds a digest so that occurrences join; the secret itself must never reach storage,
# where an additional property would also be property-indexed and full-text searchable.
_SECRET_PLAINTEXT_KEYS = frozenset(
    {"value", "secret", "plaintext", "password", "token", "key", "credential", "match", "raw"}
)


def _cross_field_secret(_type_name: str, properties: dict[str, Any]) -> None:
    carried = sorted(_SECRET_PLAINTEXT_KEYS.intersection(properties))
    if carried:
        raise ExpectedValidationError(f"/properties/{carried[0]}: a secret node never carries the secret itself")


_BUCKET_RULES: dict[str, tuple[int, int, str]] = {
    "aws_s3": (3, 63, r"[a-z0-9][a-z0-9.-]*[a-z0-9]"),
    "gcp_gcs": (3, 222, r"[a-z0-9][a-z0-9._-]*[a-z0-9]"),
    "azure_blob": (3, 24, r"[a-z0-9]+"),
}


def _cross_field_storage_bucket(_type_name: str, properties: dict[str, Any]) -> None:
    provider, name = cast("str", properties["provider"]), cast("str", properties["name"])
    low, high, grammar = _BUCKET_RULES[provider]
    if not low <= len(name) <= high or re.fullmatch(grammar, name) is None:
        raise ExpectedValidationError(f"/properties/name: not a {provider} bucket spelling")
    if provider == "aws_s3" and (name.startswith(("xn--", "sthree-")) or name.endswith(("-s3alias", "--ol-s3"))):
        raise ExpectedValidationError("/properties/name: reserved aws_s3 bucket prefix or suffix")
    if provider == "gcp_gcs" and (name.startswith("goog") or "google" in name):
        raise ExpectedValidationError("/properties/name: reserved gcp_gcs bucket name")
    if provider == "gcp_gcs" and any(len(label) > 63 for label in name.split(".")):
        raise ExpectedValidationError("/properties/name: gcp_gcs dotted components hold at most 63 characters")


# Every entry runs after the required map validated the properties it reads.
_CROSS_FIELDS: dict[str, Callable[[str, dict[str, Any]], None]] = {
    "domain": _cross_field_dns_name,
    "subdomain": _cross_field_dns_name,
    "http_fingerprint": _cross_field_http_fingerprint,
    "identity_tenant": _cross_field_identity_tenant,
    "ip_address": _cross_field_ip_address,
    "ip_cidr": _cross_field_ip_cidr,
    "repository": _cross_field_repository,
    "secret": _cross_field_secret,
    "service": _cross_field_service,
    "storage_bucket": _cross_field_storage_bucket,
    "tls_fingerprint": _cross_field_tls_fingerprint,
    "registrar": _cross_field_registrar,
    "txt_record": _cross_field_txt_record,
}


def _enum(kind: str, type_name: str, field: str) -> set[str]:
    definitions = cast("dict[str, dict[str, Any]]", _CATALOG[kind])
    return set(cast("list[str]", definitions[type_name]["required"][field]))


def _ensure_cross_field_contract() -> None:
    """Fail at import if a per-member cross-field table stops covering its own enum.

    Each table below is indexed by an enum member, so a member added without its entry would raise
    KeyError as INTERNAL instead of rejecting the value, and a member silently inheriting another
    member's branch would accept a spelling nobody checked.
    """
    tables = {
        ("nodes", "identity_tenant", "provider"): set(_TENANT_RULES),
        ("nodes", "repository", "platform"): set(_REPO_OWNER_RULES),
        ("nodes", "storage_bucket", "provider"): set(_BUCKET_RULES),
        ("nodes", "http_fingerprint", "kind"): {"favicon_mmh3", *_FINGERPRINT_DIGEST_KINDS},
    }
    for (kind, type_name, field), covered in tables.items():
        declared = _enum(kind, type_name, field)
        if declared != covered:
            raise RuntimeError(f"cross-field table for {type_name}.{field} does not cover {declared ^ covered}")
    nodes = cast("dict[str, dict[str, Any]]", _CATALOG["nodes"])
    for tag, type_name in _TXT_RECORD_DIVERSIONS:
        if "value" not in nodes.get(type_name, {}).get("required", {}):
            raise RuntimeError(f"txt_record diverts {tag} to {type_name}, which has no value rule")


_ensure_cross_field_contract()


def cross_field_types() -> tuple[str, ...]:
    """Return the node types carrying a cross-field rule, which the manifest does not publish."""
    return tuple(sorted(_CROSS_FIELDS))


def _validate_cross_fields(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    check = _CROSS_FIELDS.get(type_name) if kind == "nodes" else None
    if check is not None:
        check(type_name, properties)


# NIST IR 7695 formatted-string binding: `part` plus ten colon-separated attribute components.
_CPE_COMPONENT = r"(?:[*\-]|\?*\*?(?:[a-z0-9._\-~]|\\[!-~])+\*?\?*)"
_CPE23 = re.compile(r"cpe:2\.3:[aho*\-]:" + ":".join([_CPE_COMPONENT] * 10))


def _valid_field(value: object, rule: str | list[str] | tuple[str, ...]) -> bool:
    # An enum rule arrives as a list from `catalog_manifest()` and as a tuple from the shared view;
    # `str` is a Sequence too, so the check names the two containers rather than the protocol.
    if isinstance(rule, (list, tuple)):
        return type(value) is str and value in rule
    integer_validators: dict[str, Callable[[int], bool]] = {
        "asn": lambda item: 0 <= item <= 4294967295,
        "ip_version": lambda item: item in (4, 6),
        "redirect_status": lambda item: item in (301, 302, 303, 307, 308),
        "uint8": lambda item: 0 <= item <= 255,
        "uint16": lambda item: 0 <= item <= 65535,
    }
    if rule in integer_validators:
        return type(value) is int and integer_validators[rule](value)
    if rule == "alpn_tokens":
        return _valid_alpn_tokens(value)
    if rule == "caa_parameters":
        return _valid_caa_parameters(value)
    if type(value) is not str:
        return False
    validators: dict[str, Callable[[str], bool]] = {
        "bucket_name": _valid_bucket_name,
        "cidr": lambda text: _parse_cidr(text) is not None,
        "cpe23_or_empty": lambda text: text == "" or (len(text) <= 512 and _CPE23.fullmatch(text) is not None),
        "cve": lambda text: re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", text) is not None,
        "cwe": lambda text: re.fullmatch(r"CWE-[0-9]{1,6}", text) is not None,
        "dkim_selector": _valid_dkim_selector,
        "dmarc": _valid_dmarc,
        "dns_name": lambda text: _dns_kind(text) is not None,
        "dns_or_explicit_empty": lambda text: text == "" or _dns_kind(text) is not None,
        "email_address": _valid_email_address,
        "http_fingerprint_value": lambda text: (
            re.fullmatch(r"[0-9a-f]{64}", text) is not None or _valid_signed_int32_text(text)
        ),
        "http_url": _valid_url,
        "ip": lambda text: _parse_ip(text) is not None,
        "method": lambda text: re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}", text) is not None,
        "mta_sts": _valid_mta_sts,
        "parameter_name": lambda text: (
            1 <= len(text) <= 128 and all(0x21 <= ord(char) <= 0x7E and char not in "&=#" for char in text)
        ),
        "phone_e164": lambda text: re.fullmatch(r"\+[1-9][0-9]{1,14}", text) is not None,
        "printable_text_200": lambda text: 1 <= len(text) <= 200 and text.isprintable(),
        "printable_text_1024": lambda text: 1 <= len(text) <= 1024 and text.isprintable(),
        "repo_name": _valid_repo_name,
        "repo_owner": _valid_repo_owner,
        "rir_handle": lambda text: re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}[A-Za-z0-9]", text) is not None,
        "service_name": is_service_name,
        "sha256": lambda text: re.fullmatch(r"[0-9a-f]{64}", text) is not None,
        "spf": _valid_spf,
        "srv_label": lambda text: re.fullmatch(r"_[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?", text) is not None,
        "tech_token": lambda text: re.fullmatch(r"[a-z0-9](?:[a-z0-9._+-]{0,61}[a-z0-9])?", text) is not None,
        "tenant_id": lambda text: re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?", text) is not None,
        "tls_fingerprint_value": lambda text: re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{62}", text) is not None,
        "tls_cipher_name": lambda text: (
            5 <= len(text) <= 128 and re.fullmatch(r"TLS_[A-Z0-9]+(?:_[A-Z0-9]+)*", text) is not None
        ),
        "txt_value": lambda text: 1 <= len(text) <= 4096 and all(0x20 <= ord(char) <= 0x7E for char in text),
    }
    validator = validators.get(rule)
    return validator is not None and validator(value)


def _valid_dkim_selector(value: str) -> bool:
    if not value.isascii() or not 1 <= len(value) <= 253:
        return False
    return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None for label in value.split("."))


def _valid_signed_int32_text(value: str) -> bool:
    """Accept one spelling of a signed 32-bit integer: no plus, no leading zero, and no negative zero."""
    if re.fullmatch(r"(?:0|-?[1-9][0-9]{0,9})", value) is None:
        return False
    return -(2**31) <= int(value) <= 2**31 - 1


def _valid_bucket_name(value: str) -> bool:
    if not 3 <= len(value) <= 222 or ".." in value:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*[a-z0-9]", value) is None:
        return False
    return re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", value) is None


def _valid_repo_name(value: str) -> bool:
    """`.` and `..` are excluded by the alphanumeric requirement, which every real name satisfies."""
    if not 1 <= len(value) <= 100:
        return False
    return re.fullmatch(r"[a-z0-9._-]+", value) is not None and any(character.isalnum() for character in value)


def _valid_repo_owner(value: str) -> bool:
    if not 1 <= len(value) <= 255:
        return False
    segments = value.split("/")
    return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?", segment) is not None for segment in segments)


def _valid_email_address(value: str) -> bool:
    if not value.isascii() or not 3 <= len(value) <= 254 or value != value.lower():
        return False
    local, separator, domain = value.rpartition("@")
    if separator == "" or not 1 <= len(local) <= 64 or _dns_kind(domain) is None:
        return False
    atom = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
    return re.fullmatch(rf"{atom}(?:\.{atom})*", local) is not None


def _valid_mta_sts(value: str) -> bool:
    if not 1 <= len(value) <= 4096:
        return False
    if not (value == "v=STSv1" or value.startswith(("v=STSv1;", "v=STSv1 "))):
        return False
    return all(0x20 <= ord(char) <= 0x7E for char in value)


def _valid_spf(value: str) -> bool:
    return (value == "v=spf1" or value.startswith("v=spf1 ")) and all(0x20 <= ord(char) <= 0x7E for char in value)


def _valid_dmarc(value: str) -> bool:
    if not 1 <= len(value) <= 4096:
        return False
    if not (value == "v=DMARC1" or value.startswith(("v=DMARC1;", "v=DMARC1 "))):
        return False
    return all(0x20 <= ord(char) <= 0x7E for char in value)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return address if address.compressed == value else None


def _parse_cidr(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if "/" not in value or "%" in value:
        return None
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return None
    return network if network.with_prefixlen == value else None


def _valid_normal_dns_label(label: str) -> bool:
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None:
        return False
    if label.startswith("xn--"):
        try:
            decoded = label.encode("ascii").decode("idna")
            return decoded.encode("idna").decode("ascii") == label
        except UnicodeError:
            return False
    return True


def _valid_subdomain_label(label: str) -> bool:
    if label.startswith("xn--"):
        return _valid_normal_dns_label(label)
    return re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label) is not None and any(
        character.isalnum() for character in label
    )


def _domain_label_start(labels: list[str]) -> int | None:
    for index in range(1, len(labels)):
        try:
            if classify_dns_name(".".join(labels[index:])) == "domain":
                return index
        except ExpectedValidationError:
            continue
    return None


def _dns_kind(value: str) -> str | None:
    if not value.isascii() or not 1 <= len(value.encode("ascii")) <= 253 or value.endswith("."):
        return None
    if re.fullmatch(r"[0-9.]+", value) is not None:
        return None
    labels = value.split(".")
    if len(labels) < 2 or any(not label or len(label.encode("ascii")) > 63 for label in labels):
        return None
    try:
        kind = classify_dns_name(value)
    except ExpectedValidationError:
        return None
    if kind == "domain":
        return kind if all(_valid_normal_dns_label(label) for label in labels) else None
    domain_start = _domain_label_start(labels)
    if domain_start is None:
        return None
    if not all(_valid_subdomain_label(label) for label in labels[:domain_start]):
        return None
    return kind if all(_valid_normal_dns_label(label) for label in labels[domain_start:]) else None


def _valid_hostname(value: str) -> bool:
    if not value.isascii() or not 1 <= len(value) <= 253 or value.endswith("."):
        return False
    if "." in value and re.fullmatch(r"[0-9.]+", value) is not None:
        return False
    labels = value.split(".")
    return all(_valid_normal_dns_label(label) for label in labels)


def _valid_url(value: str) -> bool:
    if not value.isascii() or not 1 <= len(value) <= 8192:
        return False
    match = re.fullmatch(r"(https?)://(\[[^\]]+\]|[^/:?#]+)(?::([0-9]+))?(/[^?#]*)(?:\?([^#]+))?", value)
    if match is None:
        return False
    scheme, host, port, path, query = match.groups()
    if host.startswith("["):
        address = _parse_ip(host[1:-1])
        if address is None or address.version != 6:
            return False
    elif _parse_ip(host) is None and not _valid_hostname(host):
        return False
    if port is not None and (
        port.startswith("0") or not 1 <= int(port) <= 65535 or int(port) == (443 if scheme == "https" else 80)
    ):
        return False
    if any(segment in (".", "..") for segment in path.split("/")):
        return False
    pchar = r"(?:[A-Za-z0-9._~!$&'()*+,;=:@/]|%[0-9A-F]{2})*"
    return re.fullmatch(pchar, path) is not None and (
        query is None or re.fullmatch(pchar.replace("@/", "@/?"), query) is not None
    )


def _valid_alpn_tokens(value: object) -> bool:
    if type(value) is not list:
        return False
    tokens = cast("list[object]", value)
    return all(type(token) is str and re.fullmatch(r"[A-Za-z0-9./_-]{1,255}", token) is not None for token in tokens)


def _valid_caa_parameters(value: object) -> bool:
    if type(value) is not list:
        return False
    for item in cast("list[object]", value):
        if type(item) is not dict:
            return False
        parameter = cast("dict[object, object]", item)
        name = parameter.get("name")
        content = parameter.get("value")
        if type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", name) is None:
            return False
        if type(content) is not str or not all(
            0x21 <= ord(char) <= 0x3A or 0x3C <= ord(char) <= 0x7E for char in content
        ):
            return False
    return True


def _schema_for_rule(rule: str | list[str]) -> dict[str, Any]:
    if isinstance(rule, list):
        return {"type": "string", "enum": rule}
    integer_rules: dict[str, dict[str, Any]] = {
        "asn": {"type": "integer", "minimum": 0, "maximum": 4294967295, "format": rule},
        "ip_version": {"type": "integer", "enum": [4, 6], "format": rule},
        "redirect_status": {"type": "integer", "enum": [301, 302, 303, 307, 308], "format": rule},
        "uint8": {"type": "integer", "minimum": 0, "maximum": 255, "format": rule},
        "uint16": {"type": "integer", "minimum": 0, "maximum": 65535, "format": rule},
    }
    if rule in integer_rules:
        return integer_rules[rule]
    if rule == "alpn_tokens":
        return {"type": "array", "items": {"type": "string"}, "format": rule}
    if rule == "caa_parameters":
        return {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "value"],
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "additionalProperties": True,
            },
            "format": rule,
        }
    return {"type": "string", "format": rule}


def catalog_schema(kind: str, type_name: str) -> dict[str, Any]:
    """Expose JSON types, exact formats, limits, and identity metadata."""
    manifest = catalog_manifest()
    if kind not in ("nodes", "relations") or type_name not in manifest[kind]:
        raise ExpectedValidationError("unknown catalog type")
    definition = cast("dict[str, Any]", manifest[kind][type_name])
    required = cast("dict[str, str | list[str]]", definition["required"])
    return {
        "type": "object",
        "required": list(required),
        "properties": {name: _schema_for_rule(rule) for name, rule in required.items()},
        "additionalProperties": True,
        "x-identity": definition["identity"],
        "x-maxUtf8Bytes": manifest["common"]["properties_bytes"],
        "x-maxDepth": manifest["common"]["depth"],
    }
