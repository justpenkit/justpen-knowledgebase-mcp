"""Load the bundled ICANN Public Suffix List and classify DNS names."""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Literal, cast

from .errors import ExpectedValidationError

_DATA_PACKAGE = "justpen_knowledgebase_mcp.data"
_METADATA_RESOURCE = "public_suffix_list_icann.json"
_SNAPSHOT_RESOURCE = "public_suffix_list_icann.txt"


@dataclass(frozen=True, slots=True)
class _Rules:
    exact: frozenset[str]
    wildcards: frozenset[str]
    exceptions: frozenset[str]


def _read_resource_bytes(name: str) -> bytes:
    return resources.files(_DATA_PACKAGE).joinpath(name).read_bytes()


def _snapshot_checksum(metadata_bytes: bytes) -> str:
    try:
        document: object = json.loads(metadata_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid bundled PSL metadata") from exc
    if not isinstance(document, dict):
        raise TypeError("invalid bundled PSL metadata")
    metadata = cast("dict[str, object]", document)
    checksum = metadata.get("snapshot_sha256")
    if (
        type(checksum) is not str
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
    ):
        raise RuntimeError("invalid bundled PSL metadata checksum")
    return checksum


def _parse_rules(snapshot: bytes) -> _Rules:
    try:
        lines = snapshot.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError("invalid bundled PSL snapshot encoding") from exc

    exact: set[str] = set()
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    for line in lines:
        rule = line.strip()
        if not rule or rule.startswith("//"):
            continue
        if rule.startswith("!"):
            exceptions.add(rule[1:])
        elif rule.startswith("*."):
            wildcards.add(rule[2:])
        else:
            exact.add(rule)
    if not exact:
        raise RuntimeError("bundled PSL snapshot contains no exact rules")
    return _Rules(frozenset(exact), frozenset(wildcards), frozenset(exceptions))


@lru_cache(maxsize=1)
def _load_rules() -> _Rules:
    metadata = _read_resource_bytes(_METADATA_RESOURCE)
    snapshot = _read_resource_bytes(_SNAPSHOT_RESOURCE)
    if hashlib.sha256(snapshot).hexdigest() != _snapshot_checksum(metadata):
        raise RuntimeError("bundled PSL snapshot checksum mismatch")
    return _parse_rules(snapshot)


def rules_digest() -> str:
    """Hash the parsed rule set, not the file bytes, so a line-ending change on checkout moves nothing."""
    rules = _load_rules()
    canonical = {
        "exact": sorted(rules.exact),
        "wildcards": sorted(rules.wildcards),
        "exceptions": sorted(rules.exceptions),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _public_suffix_length(labels: list[str], rules: _Rules) -> int | None:
    exception_lengths: list[int] = []
    rule_length = 0
    for index in range(len(labels)):
        suffix = ".".join(labels[index:])
        suffix_length = len(labels) - index
        if suffix in rules.exceptions:
            exception_lengths.append(suffix_length)
        if suffix in rules.exact:
            rule_length = max(rule_length, suffix_length)
        if index > 0 and suffix in rules.wildcards:
            rule_length = max(rule_length, suffix_length + 1)
    if exception_lengths:
        return max(exception_lengths) - 1
    return rule_length or None


def classify_dns_name(name: str) -> Literal["domain", "subdomain"]:
    """Classify a DNS name using explicit ICANN rules and the locked fallback."""
    labels = name.split(".")
    public_suffix_length = _public_suffix_length(labels, _load_rules())
    if public_suffix_length is not None and len(labels) == public_suffix_length:
        raise ExpectedValidationError("DNS name is a public suffix")
    if len(labels) < 2:
        raise ExpectedValidationError("DNS name must contain at least two labels")
    if public_suffix_length is None:
        return "domain" if len(labels) == 2 else "subdomain"
    return "domain" if len(labels) == public_suffix_length + 1 else "subdomain"
