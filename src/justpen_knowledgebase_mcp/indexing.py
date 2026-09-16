"""Bounded typed property acceleration; canonical documents remain untouched."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast


def value_type(value: object) -> str:
    """Return the JSON category without treating booleans as numbers."""
    categories: dict[type, str] = {
        str: "string",
        int: "number",
        float: "number",
        bool: "boolean",
        type(None): "null",
        dict: "object",
        list: "array",
    }
    return categories[type(value)]


@dataclass(frozen=True)
class PropertyRow:
    """A typed scalar, container, or omitted-string sentinel."""

    value_type: str
    value_materialized: bool
    value: Any


@dataclass(frozen=True)
class Projection:
    """Selected paths plus independent path and value coverage."""

    rows: dict[str, PropertyRow]
    paths_complete: bool
    non_array_complete: bool
    total_paths: int
    omitted_values: int

    def metadata(self) -> dict[str, Any]:
        """Describe actual coverage, never a last-pointer watermark."""
        return {
            "complete": self.paths_complete and self.omitted_values == 0,
            "paths_complete": self.paths_complete,
            "non_array_complete": self.non_array_complete,
            "indexed_paths": len(self.rows),
            "total_paths": self.total_paths,
            "omitted_values": self.omitted_values,
        }


def flatten_properties(properties: dict[str, Any], *, required_paths: set[str] | None = None) -> Projection:
    """Prioritize required and non-array paths before array descendants."""
    required = required_paths or set()
    if len(required) > 512 or any(len(path.encode("utf-8")) > 1024 for path in required):
        raise ValueError("required path budget exceeded")
    candidates: list[tuple[str, Any, bool]] = []

    def visit(value: object, path: str, *, array_ancestor: bool) -> None:
        if path:
            candidates.append((path, value, array_ancestor))
        if isinstance(value, dict):
            for key, child in cast("dict[str, Any]", value).items():
                visit(child, path + "/" + key.replace("~", "~0").replace("/", "~1"), array_ancestor=array_ancestor)
        elif isinstance(value, list):
            for index, child in enumerate(cast("list[Any]", value)):
                visit(child, path + "/" + str(index), array_ancestor=True)

    visit(properties, "", array_ancestor=False)
    eligible = [item for item in candidates if len(item[0].encode("utf-8")) <= 1024]
    eligible.sort(key=lambda item: (0 if item[0] in required else 2 if item[2] else 1, item[0].encode("utf-8")))
    rows: dict[str, PropertyRow] = {}
    for path, value, _ in eligible[:512]:
        category = value_type(value)
        materialized = category != "string" or len(value.encode("utf-8")) <= 1024
        stored = None if not materialized or category in ("object", "array", "null") else value
        rows[path] = PropertyRow(category, materialized, stored)
    return Projection(
        rows,
        len(rows) == len(candidates),
        all(path in rows for path, _, array in candidates if not array),
        len(candidates),
        sum(not row.value_materialized for row in rows.values()),
    )
