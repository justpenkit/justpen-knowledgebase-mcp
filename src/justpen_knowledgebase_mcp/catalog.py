"""Frozen v1 catalog data; validation and discovery consume this single manifest."""

import hashlib
import ipaddress
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from .errors import ExpectedValidationError
from .mutations import validate_properties

CATALOG_VERSION = 1
# Canonical serialized contract stays immutable; callers receive a fresh tree.
CATALOG_JSON = r"""
{
  "common": {
    "additional_properties": true,
    "coercion": false,
    "depth": 16,
    "integers": "signed64",
    "numbers": "finite double",
    "properties_bytes": 65536,
    "required_nonnull": true
  },
  "formats": {
    "alpn_list": "`alpn_list` is an explicit empty string for no ALPN offer, or a comma-separated\nordered list of unique ASCII tokens, each matching `[A-Za-z0-9./_-]{1,255}`,\nwith no whitespace and at most 1024 bytes total. This is a bounded text subset\nof wire ALPN; unsupported opaque protocol bytes stay in evidence. The field\nrecords the offered list, not the negotiated result. A missing/unknown offer is\nnot silently treated as empty. Negotiated ALPN, version/cipher, status, banner,\nHTTP status, role permissions, authentication result evidence and per-context\nobservations belong in edge extras.",
    "dns": "Lowercase ASCII LDH labels separated by dots, each 1\u201363 bytes with alphanumeric first/last character, total \u2264253 bytes; no trailing dot, wildcard, underscore, Unicode, empty label or all-numeric dotted address. Single-label hostnames allowed. ASCII A-label spellings fit this grammar; no IDNA conversion or Unicode-equivalence claim.",
    "dns_or_explicit_empty": "dns policy or explicit empty string; IP literals rejected",
    "host": "Either `dns` or `ip`; an IPv6 host property is unbracketed.",
    "http_url": "`http_url` is an ASCII string of 1\u20138192 bytes consisting of absolute `http://` or\n`https://`, a lowercase `host`, optional decimal port, slash-starting path, and\noptional nonempty query. IPv6 authority uses brackets around the canonical `ip`.\nReject userinfo, fragments, whitespace/control characters, backslashes, Unicode,\ninvalid percent escapes, an empty host, and an empty query marker (`...?`).\nExplicit port has no leading zero, is 1\u201365535, and must not equal the scheme's\ndefault (80/443); omit those defaults. Path is mandatory (`https://x/`, not\n`https://x`). Non-percent ASCII characters in path must belong to RFC3986 pchar\nplus `/`; query additionally allows `?`. Percent escapes require uppercase hex.\nReject complete path segments `.` and `..`; escaped dot segments are retained,\nnot decoded. No IRI acceptance is implied. A permissive URL parser alone is not\nvalidation: inspect authority, path/query and original spelling explicitly.\n\nPath/query case, repeated slashes, parameter order, `+`, escaped unreserved\ncharacters and percent-encoded byte sequences are preserved. `/a` and `/%61`\nmay be distinct; no claim of semantic URL dedup. Nondefault port spelling and\nscheme/host case have one accepted representation. URL ports occur in a string\nby definition; standalone `port` properties still require strict JSON integers.\n",
    "ip": "IPv4 dotted decimal, four octets 0\u2013255, no leading zeros except `0`; IPv6 compressed lowercase hexadecimal canonical spelling equivalent to `IPv6Address.compressed`, longest zero run/first tie, never compress a single zero group. No zone ID, bracket, CIDR or dotted IPv4 tail. This selects one RFC5952-style KB policy including hex-form IPv4-mapped addresses. Compare parsed representation to input; never replace input.",
    "location": "text(4096), complete source/affected location meaningful independently of graph links; qualified URL, realm-qualified network target, or artifact digest plus internal path/symbol. Not a graph UUID or bare local label like `login` when multiple targets exist. Format syntax alone cannot prove adequate qualification.",
    "method": "1\u201332 ASCII characters matching `[A-Z][A-Z0-9!#$%&'*+.^_\\x60\\u007c~-]*`; supports case-sensitive uppercase extension methods, rejects lowercase.",
    "port": "JSON integer 1\u201365535, never boolean, float or digit string.",
    "realm": "text(1024), a literal authority identifier supported by evidence, e.g. `https://id.example.com/`, `EXAMPLE`, or `com.example.bank`. It is not a node UUID, generated graph scope or session identifier. Exact case preserved; no inferred cross-realm equivalence.",
    "selector": "Same as text(1024), or explicit empty string to mean the whole named resource.",
    "sha256": "Exactly 64 lowercase ASCII hexadecimal characters; digest of the specified bytes.",
    "text(N)": "JSON string, 1\u2013N UTF-8 bytes, no Unicode Cc control characters, no leading/trailing Unicode whitespace; internal spaces and case preserved."
  },
  "nodes": {
    "application": {
      "identity": [
        "sha256"
      ],
      "required": {
        "platform": [
          "android",
          "ios",
          "windows",
          "macos",
          "linux",
          "multi"
        ],
        "sha256": "sha256"
      }
    },
    "certificate": {
      "identity": [
        "der_sha256"
      ],
      "required": {
        "der_sha256": "sha256"
      }
    },
    "credential_hint": {
      "identity": [
        "realm",
        "subject",
        "kind",
        "location",
        "selector"
      ],
      "required": {
        "kind": [
          "password",
          "token",
          "api_key",
          "private_key",
          "session",
          "candidate_username"
        ],
        "location": "location",
        "realm": "realm",
        "selector": "selector",
        "subject": "text(512)"
      }
    },
    "domain": {
      "identity": [
        "name"
      ],
      "required": {
        "name": "dns"
      }
    },
    "endpoint": {
      "identity": [
        "url",
        "method"
      ],
      "required": {
        "method": "method",
        "url": "http_url"
      }
    },
    "finding": {
      "identity": [
        "rule_namespace",
        "rule_id",
        "location",
        "selector"
      ],
      "required": {
        "location": "location",
        "rule_id": "text(256)",
        "rule_namespace": "text(256)",
        "selector": "selector",
        "severity": [
          "info",
          "low",
          "medium",
          "high",
          "critical",
          "unknown"
        ],
        "title": "text(512)"
      }
    },
    "hostname": {
      "identity": [
        "name"
      ],
      "required": {
        "name": "dns"
      }
    },
    "ip": {
      "identity": [
        "address"
      ],
      "required": {
        "address": "ip"
      }
    },
    "principal": {
      "identity": [
        "realm",
        "name",
        "kind"
      ],
      "required": {
        "kind": [
          "user",
          "service",
          "group"
        ],
        "name": "text(512)",
        "realm": "realm"
      }
    },
    "service": {
      "identity": [
        "host",
        "transport",
        "port"
      ],
      "required": {
        "host": "host",
        "port": "port",
        "transport": [
          "tcp",
          "udp"
        ]
      }
    }
  },
  "relations": {
    "affects": {
      "identity": [
        "context"
      ],
      "required": {
        "context": "text(1024)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "finding"
      ],
      "targets": [
        "ip",
        "hostname",
        "domain",
        "service",
        "endpoint",
        "certificate",
        "application",
        "principal",
        "credential_hint"
      ]
    },
    "aliases": {
      "identity": [
        "vantage"
      ],
      "required": {
        "vantage": "text(512)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "hostname"
      ],
      "targets": [
        "hostname"
      ]
    },
    "authenticates_as": {
      "identity": [
        "context"
      ],
      "required": {
        "context": "text(1024)",
        "result": [
          "candidate",
          "valid",
          "invalid"
        ]
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "credential_hint"
      ],
      "targets": [
        "principal"
      ]
    },
    "contacts": {
      "identity": [
        "context",
        "basis"
      ],
      "required": {
        "basis": [
          "static",
          "dynamic"
        ],
        "context": "text(1024)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "application"
      ],
      "targets": [
        "endpoint",
        "service"
      ]
    },
    "exposes_credential": {
      "identity": [
        "context"
      ],
      "required": {
        "context": "text(1024)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "application",
        "endpoint",
        "service"
      ],
      "targets": [
        "credential_hint"
      ]
    },
    "has_role": {
      "identity": [
        "context",
        "role"
      ],
      "required": {
        "context": "text(1024)",
        "role": "text(256)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "principal"
      ],
      "targets": [
        "application",
        "endpoint",
        "service"
      ]
    },
    "member_of": {
      "identity": [
        "context"
      ],
      "required": {
        "context": "text(1024)"
      },
      "rule": "target kind group and equal realms",
      "self_edge": false,
      "sources": [
        "principal"
      ],
      "targets": [
        "principal"
      ]
    },
    "name_in_domain": {
      "identity": [],
      "required": {},
      "rule": "hostname equals domain or ends with dot plus domain",
      "self_edge": false,
      "sources": [
        "hostname"
      ],
      "targets": [
        "domain"
      ]
    },
    "offers_service": {
      "identity": [
        "vantage"
      ],
      "required": {
        "vantage": "text(512)"
      },
      "rule": "source address/name equals service.host",
      "self_edge": false,
      "sources": [
        "ip",
        "hostname"
      ],
      "targets": [
        "service"
      ]
    },
    "presents_certificate": {
      "identity": [
        "vantage",
        "server_name",
        "mode",
        "alpn_offered"
      ],
      "required": {
        "alpn_offered": "alpn_list",
        "mode": [
          "tls",
          "dtls",
          "starttls"
        ],
        "server_name": "dns_or_explicit_empty",
        "vantage": "text(512)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "service"
      ],
      "targets": [
        "certificate"
      ]
    },
    "redirects_to": {
      "identity": [
        "context"
      ],
      "required": {
        "context": "text(1024)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "endpoint"
      ],
      "targets": [
        "endpoint"
      ]
    },
    "resolves_to": {
      "identity": [
        "vantage"
      ],
      "required": {
        "vantage": "text(512)"
      },
      "rule": "",
      "self_edge": false,
      "sources": [
        "hostname",
        "domain"
      ],
      "targets": [
        "ip"
      ]
    },
    "serves_endpoint": {
      "identity": [
        "vantage",
        "context"
      ],
      "required": {
        "context": "text(1024)",
        "vantage": "text(512)"
      },
      "rule": "tcp; effective URL port equals service.port; DNS service.host equals URL host; IP virtual hosting allowed",
      "self_edge": false,
      "sources": [
        "service"
      ],
      "targets": [
        "endpoint"
      ]
    },
    "signed_by": {
      "identity": [],
      "required": {},
      "rule": "",
      "self_edge": false,
      "sources": [
        "application"
      ],
      "targets": [
        "certificate"
      ]
    },
    "subdomain_of": {
      "identity": [],
      "required": {},
      "rule": "proper DNS suffix on label boundary",
      "self_edge": false,
      "sources": [
        "domain"
      ],
      "targets": [
        "domain"
      ]
    }
  },
  "version": 1
}
"""
CATALOG_FINGERPRINT = hashlib.sha256(
    json.dumps(json.loads(CATALOG_JSON), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
).hexdigest()


def catalog_manifest() -> dict[str, Any]:
    """Return an isolated copy of the fixed executable catalog contract."""
    return json.loads(CATALOG_JSON)


def validate_record(kind: str, type_name: str, properties: dict[str, Any]) -> None:
    """Enforce the canonical manifest's required properties without coercion."""
    validate_properties(properties)
    definitions = catalog_manifest().get(kind, {})
    if type_name not in definitions:
        raise ExpectedValidationError("unknown catalog type")
    for field, rule in definitions[type_name]["required"].items():
        if field not in properties or not _valid_field(properties[field], rule):
            raise ExpectedValidationError(f"/properties/{field}: expected {rule}")


def _valid_field(value: object, rule: str | list[str]) -> bool:
    if isinstance(rule, list):
        return type(value) is str and value in rule
    if rule == "port":
        return type(value) is int and 1 <= value <= 65535
    if type(value) is not str:
        return False
    validators: dict[str, Callable[[str], bool]] = {
        "ip": _valid_ip,
        "dns": _valid_dns,
        "http_url": _valid_url,
        "host": lambda text: _valid_ip(text) or _valid_dns(text),
        "dns_or_explicit_empty": lambda text: text == "" or _valid_dns(text),
        "sha256": lambda text: re.fullmatch(r"[0-9a-f]{64}", text) is not None,
        "method": lambda text: re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}", text) is not None,
        "alpn_list": _valid_alpn,
    }
    if rule in validators:
        return validators[rule](value)
    return _valid_text(value, rule)


def _valid_alpn(value: str) -> bool:
    tokens = value.split(",")
    return value == "" or (
        len(value.encode("utf-8")) <= 1024
        and len(tokens) == len(set(tokens))
        and all(re.fullmatch(r"[A-Za-z0-9./_-]{1,255}", token) is not None for token in tokens)
    )


def _valid_text(value: str, rule: str) -> bool:
    if rule == "selector" and value == "":
        return True
    maximum = {"selector": 1024, "realm": 1024, "location": 4096}.get(rule)
    if maximum is None and rule.startswith("text("):
        maximum = int(rule[5:-1])
    return (
        maximum is not None
        and 1 <= len(value.encode("utf-8")) <= maximum
        and value == value.strip()
        and not any(unicodedata.category(char) == "Cc" for char in value)
    )


def _valid_ip(value: str) -> bool:

    try:
        if "%" in value or (":" in value and "." in value):
            return False
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address):
            groups = [
                format(int.from_bytes(address.packed[index : index + 2], "big"), "x") for index in range(0, 16, 2)
            ]
            best_start, best_length = -1, 1
            for start in range(8):
                end = start
                while end < 8 and groups[end] == "0":
                    end += 1
                if end - start > best_length:
                    best_start, best_length = start, end - start
            if best_start >= 0:
                expected = ":".join(groups[:best_start]) + "::" + ":".join(groups[best_start + best_length :])
            else:
                expected = ":".join(groups)
            return expected == value
        return str(address) == value
    except ValueError:
        return False


def _valid_dns(value: str) -> bool:

    return (
        len(value) <= 253
        and not ("." in value and re.fullmatch(r"[0-9.]+", value) is not None)
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None for label in value.split("."))
    )


def _valid_url(value: str) -> bool:

    if not value.isascii() or len(value) > 8192:
        return False
    match = re.fullmatch(r"(https?)://(\[[^\]]+\]|[^/:?#]+)(?::([0-9]+))?(/[^?#]*)(?:\?([^#]+))?", value)
    if match is None:
        return False
    scheme, host, port, path, query = match.groups()
    if host.startswith("["):
        if ":" not in host or not _valid_ip(host[1:-1]):
            return False
    elif not (_valid_dns(host) or (_valid_ip(host) and ":" not in host)):
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


def catalog_schema(kind: str, type_name: str) -> dict[str, Any]:
    """Expose JSON types and exact format references from the sole manifest."""
    manifest = catalog_manifest()
    if kind not in ("nodes", "relations") or type_name not in manifest[kind]:
        raise ExpectedValidationError("unknown catalog type")
    definition = manifest[kind][type_name]
    properties: dict[str, Any] = {}
    for name, rule in definition["required"].items():
        if isinstance(rule, list):
            properties[name] = {"type": "string", "enum": rule}
        elif rule == "port":
            properties[name] = {"type": "integer", "minimum": 1, "maximum": 65535, "format": rule}
        else:
            properties[name] = {"type": "string", "format": rule}
    return {
        "type": "object",
        "required": list(definition["required"]),
        "properties": properties,
        "additionalProperties": True,
        "x-maxUtf8Bytes": manifest["common"]["properties_bytes"],
        "x-maxDepth": manifest["common"]["depth"],
    }
