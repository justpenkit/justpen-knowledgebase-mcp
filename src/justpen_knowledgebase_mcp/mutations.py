"""Strict bounded JSON validation and nonmutating property merge."""

from __future__ import annotations

import copy
import json
import math
from typing import Any, cast

from .errors import ExpectedValidationError


def canonical_json(value: object) -> str:
    """Serialize JSON without changing types or Unicode property values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _visit(item: object, depth: int) -> None:
    if depth > 16:
        raise ExpectedValidationError("properties depth exceeds 16")
    if item is None or type(item) in (str, bool):
        return
    if type(item) is int:
        if not -(2**63) <= item < 2**63:
            raise ExpectedValidationError("integer outside signed64")
    elif type(item) is float:
        if not math.isfinite(item):
            raise ExpectedValidationError("number must be finite")
    elif isinstance(item, list):
        for child in cast("list[object]", item):
            _visit(child, depth + 1)
    elif isinstance(item, dict):
        _visit_object(cast("dict[object, object]", item), depth)
    else:
        raise ExpectedValidationError("not a JSON value")


def _visit_object(item: dict[object, object], depth: int) -> None:
    for key, child in item.items():
        if type(key) is not str:
            raise ExpectedValidationError("object keys must be strings")
        _visit(child, depth + 1)


def validate_properties(value: object) -> None:
    """Validate every value, with the root object at depth zero."""
    if type(value) is not dict:
        raise ExpectedValidationError("properties must be an object")
    mapping = cast("dict[str, Any]", value)
    _visit(mapping, 0)
    if len(canonical_json(mapping).encode("utf-8")) > 65536:
        raise ExpectedValidationError("properties exceed 65536 bytes")


def pointer_tokens(pointer: str) -> tuple[str, ...]:
    """Decode an object-property JSON Pointer, rejecting malformed escapes."""
    if not pointer.startswith("/"):
        raise ExpectedValidationError("property pointer must start with slash")
    result: list[str] = []
    for token in pointer[1:].split("/"):
        position = 0
        while position < len(token):
            if token[position] == "~":
                if position + 1 == len(token) or token[position + 1] not in "01":
                    raise ExpectedValidationError("invalid pointer escape")
                position += 1
            position += 1
        result.append(token.replace("~1", "/").replace("~0", "~"))
    return tuple(result)


def _write_paths(old: dict[str, Any], new: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    writes: list[tuple[str, ...]] = []
    for key, value in new.items():
        path = (*prefix, key)
        if type(old.get(key)) is dict and type(value) is dict:
            writes.extend(_write_paths(old[key], cast("dict[str, Any]", value), path))
        elif key not in old and type(value) is dict:
            writes.extend(_write_paths({}, cast("dict[str, Any]", value), path))
        else:
            writes.append(path)
    return writes


def _object_mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


def _object_paths(value: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Containers are merge points, but removing them would recreate them on merge."""
    paths: list[tuple[str, ...]] = []
    for key, child in value.items():
        if type(child) is dict:
            path = (*prefix, key)
            paths.append(path)
            paths.extend(_object_paths(cast("dict[str, Any]", child), path))
    return paths


def _remove(result: dict[str, Any], remove: tuple[str, ...]) -> None:
    target: object = result
    for token in remove[:-1]:
        if type(target) is list:
            raise ExpectedValidationError("array element removal is unsupported")
        mapping = _object_mapping(target)
        if mapping is None:
            return
        if token not in mapping:
            return
        target = mapping[token]
    if type(target) is list:
        raise ExpectedValidationError("array element removal is unsupported")
    if isinstance(target, dict):
        cast("dict[str, Any]", target).pop(remove[-1], None)


def _merge(old: dict[str, Any], new: dict[str, Any]) -> None:
    for key, value in new.items():
        if type(value) is dict and type(old.get(key)) is dict:
            _merge(old[key], cast("dict[str, Any]", value))
        else:
            old[key] = copy.deepcopy(new[key])


def merge_properties(current: dict[str, Any], patch: dict[str, Any], remove_properties: list[str]) -> dict[str, Any]:
    """Remove object properties, merge actual writes, then validate the result."""
    validate_properties(current)
    validate_properties(patch)
    removals = [pointer_tokens(pointer) for pointer in remove_properties]
    writes = _write_paths(current, patch)
    objects = _object_paths(patch)
    for remove in removals:
        if any(path[: len(remove)] == remove for path in objects):
            raise ExpectedValidationError("remove and set paths conflict")
        for write in writes:
            common = min(len(remove), len(write))
            if remove[:common] == write[:common]:
                raise ExpectedValidationError("remove and set paths conflict")
    result = copy.deepcopy(current)
    for remove in removals:
        _remove(result, remove)
    _merge(result, patch)
    validate_properties(result)
    return result
