"""Frozen v1 catalog data; validation and discovery consume this single manifest."""

import hashlib
import json
from typing import Any

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
