"""Catalog v2 manifest, discovery schema, and strict record validators."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import TYPE_CHECKING, Any, cast

from .errors import ExpectedValidationError
from .mutations import validate_properties
from .psl import classify_dns_name
from .service_names import is_service_name, secure_required

if TYPE_CHECKING:
    from collections.abc import Callable

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
    "http_url": (
        "Canonical absolute ASCII http/https URL with lowercase host, mandatory path, no userinfo, fragment, "
        "whitespace, backslash, Unicode, default explicit port, dot path segment, or lowercase percent escape. "
        "A submitted query string is validated and then removed before identity and storage, so one endpoint "
        "holds one path; parameter names belong to parameter nodes."
    ),
    "ip": "Canonical IPv4Address.compressed or lowercase IPv6Address.compressed spelling, without scope or prefix.",
    "ip_version": "A strict JSON integer equal to 4 or 6.",
    "method": "One to 32 characters matching an uppercase HTTP method token.",
    "parameter_name": (
        "1 to 128 printable ASCII characters without space, `&`, `=`, or `#`; one single parameter name, "
        "never a raw query string."
    ),
    "printable_text_200": "A string of 1-200 printable Unicode characters.",
    "redirect_status": "A strict JSON integer in 301, 302, 303, 307, or 308.",
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
    "ip_address": {
        "identity": _identity(["value"]),
        "required": {"value": "ip", "version": "ip_version"},
    },
    "ip_cidr": {
        "identity": _identity(["value"]),
        "required": {"value": "cidr", "version": "ip_version"},
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
    "port": {
        "identity": _identity(["transport", "number"], scope=_SCOPE_OPEN_PORT),
        "required": {"number": "uint16", "transport": ["tcp", "udp", "sctp"]},
    },
    "registrar": {
        "identity": _identity(["iana_id"]),
        "required": {"iana_id": "uint16", "name": "printable_text_200"},
    },
    "service": {
        "identity": _identity(["name"], scope=_SCOPE_SERVICE),
        "required": {"name": "service_name"},
    },
    "spf_record": {"identity": _identity(["value"]), "required": {"value": "spf"}},
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
    "affected_by": _relation(["service", "finding"], ["cve"]),
    "announced_by": _relation(["ip_cidr"], ["asn"]),
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
    "has_dkim_selector": _relation(_D, ["dkim_record"]),
    "has_dmarc": _relation(_D, ["dmarc_record"]),
    "has_finding": _relation(
        ["port", "domain", "subdomain", "ip_address", "ip_cidr", "service", "endpoint", "certificate"],
        ["finding"],
    ),
    "has_mail_exchange": _relation(
        _D,
        _D,
        required={"preference": "uint16"},
        identity=_identity(["preference"]),
        self_edge=True,
    ),
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
    "protected_by": _relation(
        ["service", "endpoint"],
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
    "runs_technology": _relation(["service", "endpoint"], ["technology"]),
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


def canonicalize_record(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    """Rewrite the declared non-canonical spellings in place before identity and storage.

    Only `endpoint.url` is rewritten: its query string is dropped so one path is one node rather
    than one node per observed parameter value. Parameter names live on `parameter` nodes. This is
    value canonicalization, not the JSON type coercion `_COMMON["coercion"]` refuses.
    """
    if kind != "nodes" or type_name != "endpoint":
        return
    url = properties.get("url")
    if type(url) is str and "?" in url:
        properties["url"] = url.split("?", 1)[0]


def validate_record(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    """Canonicalize declared spellings, then enforce required properties and cross-field rules."""
    canonicalize_record(kind, type_name, properties)
    validate_properties(properties)
    manifest = catalog_manifest()
    if kind not in ("nodes", "relations"):
        raise ExpectedValidationError("unknown catalog type")
    definitions = cast("dict[str, dict[str, Any]]", manifest[kind])
    if type_name not in definitions:
        raise ExpectedValidationError("unknown catalog type")
    required = cast("dict[str, str | list[str]]", definitions[type_name]["required"])
    for field, rule in required.items():
        if field not in properties or not _valid_field(properties[field], rule):
            raise ExpectedValidationError(f"/properties/{field}: expected {rule}")
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


def _cross_field_txt_record(_type_name: str, properties: dict[str, Any]) -> None:
    value = cast("str", properties["value"])
    if value.startswith(("v=spf1", "v=DMARC1", "v=DKIM1")):
        raise ExpectedValidationError("/properties/value: use the dedicated TXT record type")


# Every entry runs after the required map validated the properties it reads.
_CROSS_FIELDS: dict[str, Callable[[str, dict[str, Any]], None]] = {
    "domain": _cross_field_dns_name,
    "subdomain": _cross_field_dns_name,
    "ip_address": _cross_field_ip_address,
    "ip_cidr": _cross_field_ip_cidr,
    "service": _cross_field_service,
    "tls_fingerprint": _cross_field_tls_fingerprint,
    "registrar": _cross_field_registrar,
    "txt_record": _cross_field_txt_record,
}


def _validate_cross_fields(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    check = _CROSS_FIELDS.get(type_name) if kind == "nodes" else None
    if check is not None:
        check(type_name, properties)


# NIST IR 7695 formatted-string binding: `part` plus ten colon-separated attribute components.
_CPE_COMPONENT = r"(?:[*\-]|\?*\*?(?:[a-z0-9._\-~]|\\[!-~])+\*?\?*)"
_CPE23 = re.compile(r"cpe:2\.3:[aho*\-]:" + ":".join([_CPE_COMPONENT] * 10))


def _valid_field(value: object, rule: str | list[str]) -> bool:
    if isinstance(rule, list):
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
        "cidr": lambda text: _parse_cidr(text) is not None,
        "cpe23_or_empty": lambda text: text == "" or (len(text) <= 512 and _CPE23.fullmatch(text) is not None),
        "cve": lambda text: re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", text) is not None,
        "cwe": lambda text: re.fullmatch(r"CWE-[0-9]{1,6}", text) is not None,
        "dkim_selector": _valid_dkim_selector,
        "dmarc": _valid_dmarc,
        "dns_name": lambda text: _dns_kind(text) is not None,
        "dns_or_explicit_empty": lambda text: text == "" or _dns_kind(text) is not None,
        "http_url": _valid_url,
        "ip": lambda text: _parse_ip(text) is not None,
        "method": lambda text: re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}", text) is not None,
        "parameter_name": lambda text: (
            1 <= len(text) <= 128 and all(0x21 <= ord(char) <= 0x7E and char not in "&=#" for char in text)
        ),
        "printable_text_200": lambda text: 1 <= len(text) <= 200 and text.isprintable(),
        "rir_handle": lambda text: re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}[A-Za-z0-9]", text) is not None,
        "service_name": is_service_name,
        "sha256": lambda text: re.fullmatch(r"[0-9a-f]{64}", text) is not None,
        "spf": _valid_spf,
        "srv_label": lambda text: re.fullmatch(r"_[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?", text) is not None,
        "tech_token": lambda text: re.fullmatch(r"[a-z0-9](?:[a-z0-9._+-]{0,61}[a-z0-9])?", text) is not None,
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
