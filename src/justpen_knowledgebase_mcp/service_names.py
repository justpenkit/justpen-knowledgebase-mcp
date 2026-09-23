"""Load and query the bundled service name registry."""

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import cast

REGISTRY_VERSION = 2

_CHARSET = r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
_DATA_PACKAGE = "justpen_knowledgebase_mcp.data"
_REGISTRY_RESOURCE = "service_names.json"


@dataclass(frozen=True, slots=True)
class _Registry:
    names: frozenset[str]
    secure_required_names: frozenset[str]


def _read_resource_bytes(name: str) -> bytes:
    return resources.files(_DATA_PACKAGE).joinpath(name).read_bytes()


def _decode_registry(data: bytes) -> dict[str, object]:
    try:
        document: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid bundled service name registry JSON") from exc
    if not isinstance(document, dict):
        raise TypeError("invalid bundled service name registry structure")
    return cast("dict[str, object]", document)


@lru_cache(maxsize=1)
def _load_registry() -> _Registry:
    document = _decode_registry(_read_resource_bytes(_REGISTRY_RESOURCE))
    if type(document.get("schema_version")) is not int or document["schema_version"] != REGISTRY_VERSION:
        raise RuntimeError("unsupported bundled service name registry version")

    charset = document.get("charset")
    if type(charset) is not str:
        raise RuntimeError("invalid bundled service name registry charset")
    try:
        pattern = re.compile(charset)
    except re.error as exc:
        raise RuntimeError("invalid bundled service name registry charset") from exc
    if charset != _CHARSET:
        raise RuntimeError("unexpected bundled service name registry charset")

    raw_whitelist = document.get("whitelist")
    if not isinstance(raw_whitelist, dict) or not raw_whitelist:
        raise RuntimeError("invalid bundled service name registry whitelist")
    whitelist = cast("dict[object, object]", raw_whitelist)

    names: set[str] = set()
    secure_required_names: set[str] = set()
    for name, raw_entry in whitelist.items():
        if type(name) is not str or pattern.fullmatch(name) is None or not isinstance(raw_entry, dict):
            raise RuntimeError("invalid bundled service name registry entry")
        entry = cast("dict[str, object]", raw_entry)
        secure = entry.get("secure_required")
        if type(secure) is not bool:
            raise RuntimeError("invalid bundled service name registry secure_required flag")
        names.add(name)
        if secure:
            secure_required_names.add(name)
    return _Registry(frozenset(names), frozenset(secure_required_names))


def registry_digest() -> str:
    """Hash the parsed whitelist and its secure flags, not the file bytes."""
    registry = _load_registry()
    canonical = {
        "names": sorted(registry.names),
        "secure_required": sorted(registry.secure_required_names),
        "version": REGISTRY_VERSION,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def is_service_name(name: str) -> bool:
    """Return whether name belongs to the versioned service whitelist."""
    return name in _load_registry().names


def secure_required(name: str) -> bool:
    """Return whether the whitelisted service name requires a secure flag."""
    return name in _load_registry().secure_required_names
