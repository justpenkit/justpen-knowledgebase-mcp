"""Load the ASM coverage fixtures and compute what their mappings say must be written.

Each directory under `tests/fixtures/asm/` holds one source's schema-derived output files,
`SOURCE.md`, `mapping.json` and `writes.json`. This module parses the output files, flattens every
record into leaf paths, resolves each leaf to its mapping rows, and computes, per file, the property
values the rows expect and the values the writes actually carry. The unit tests in
`tests/test_asm_coverage.py` compare them; `tests/tools/test_asm_fixtures.py` replays the writes
through the MCP tools.
"""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from justpen_knowledgebase_mcp.catalog import catalog_view, scope_relations
from justpen_knowledgebase_mcp.identity import identity_key

from .asm_xml import parse_xml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "asm"
REDACTED = "[REDACTED]"

# Loaded by path, as tests/test_catalog_reference.py loads its generator: `scripts` is not a package.
_SPEC = importlib.util.spec_from_file_location("asm_transforms", ROOT / "scripts" / "asm_transforms.py")
assert _SPEC is not None
assert _SPEC.loader is not None
transforms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(transforms)

TYPED_SINKS = ("nodes", "relations", "meta")


@dataclass(frozen=True)
class Row:
    """One mapping row: a path pattern, where its value goes, and how it gets there."""

    path: str
    sink: str
    transform: tuple[str, ...] = ()
    redact: bool = False
    documented_only: bool = False
    each: bool = False
    when: str | None = None
    reason: str = ""

    @property
    def qualifiers(self) -> dict[str, str]:
        match = re.match(r"\{([^}]*)\}\.", self.path)
        if match is None:
            return {}
        return dict(item.split("=", 1) for item in match.group(1).split(","))

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(re.sub(r"^\{[^}]*\}\.", "", self.path).split("."))

    @property
    def specificity(self) -> tuple[int, int]:
        return sum(segment != "*" for segment in self.segments), len(self.qualifiers)

    @property
    def wildcard(self) -> bool:
        return "*" in self.segments


@dataclass
class Source:
    """One fixture directory, parsed."""

    name: str
    files: dict[str, str]
    rows: list[Row]
    derived: list[dict[str, Any]]
    intended_merges: list[dict[str, Any]]
    batches: list[dict[str, Any]]
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def sink_key(sink: object) -> str:
    """`nodes.<type>.<property>`, `relations.<type>.<property>`, `meta.<field>`, `evidence` or `non_storable`."""
    if isinstance(sink, str):
        return sink
    item = cast("dict[str, str]", sink)
    if "node" in item:
        return f"nodes.{item['node']}.{item['property']}"
    if "relation" in item:
        return f"relations.{item['relation']}.{item['property']}"
    return f"meta.{item['meta']}"


def source_names() -> list[str]:
    return sorted(path.name for path in FIXTURES.iterdir() if (path / "mapping.json").is_file())


def _kv_record(text: str) -> dict[str, Any]:
    """`Key: value` lines; a repeated key becomes a list, comments and notices are skipped."""
    record: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((">>>", "%", "#", "NOTICE", "TERMS", "URL of")) or ":" not in stripped:
            continue
        key, _separator, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if key in record:
            existing = record[key]
            record[key] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            record[key] = value
    return record


def parse_file(text: str, file_format: str) -> list[dict[str, Any]]:
    if file_format in ("jsonl", "bbot_jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if file_format == "json":
        document = json.loads(text)
        return document if isinstance(document, list) else [document]
    if file_format == "xml":
        return [parse_xml(text)]
    if file_format == "kv_text":
        return [_kv_record(text)]
    raise ValueError(f"unknown fixture format {file_format}")


def load_source(name: str) -> Source:
    directory = FIXTURES / name
    mapping = json.loads((directory / "mapping.json").read_text(encoding="utf-8"))
    rows = [
        Row(
            path=row["path"],
            sink=sink_key(row["sink"]),
            transform=tuple(row.get("transform", ())),
            redact=row.get("redact", False),
            documented_only=row.get("documented_only", False),
            each=row.get("each", False),
            when=row.get("when"),
            reason=row.get("reason", ""),
        )
        for row in mapping["rows"]
    ]
    source = Source(
        name=name,
        files=mapping["files"],
        rows=rows,
        derived=mapping.get("derived", []),
        intended_merges=mapping.get("intended_merges", []),
        batches=json.loads((directory / "writes.json").read_text(encoding="utf-8")),
    )
    for file_name, file_format in source.files.items():
        source.records[file_name] = parse_file((directory / file_name).read_text(encoding="utf-8"), file_format)
    return source


def leaves(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """Flatten objects by key and arrays of objects by `key[]`; an array of scalars is one leaf."""
    if isinstance(value, dict) and value:
        result: list[tuple[tuple[str, ...], Any]] = []
        for key, item in cast("dict[str, Any]", value).items():
            result.extend(leaves(item, (*prefix, key)))
        return result
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        head = (*prefix[:-1], prefix[-1] + "[]")
        return [leaf for item in cast("list[Any]", value) for leaf in leaves(item, head)]
    return [(prefix, value)]


def _matches(row: Row, segments: tuple[str, ...], record: dict[str, Any]) -> bool:
    if len(row.segments) != len(segments):
        return False
    if any(record.get(key) != expected for key, expected in row.qualifiers.items()):
        return False
    return all(pattern in ("*", actual) for pattern, actual in zip(row.segments, segments, strict=True))


def rows_for(source: Source, segments: tuple[str, ...], record: dict[str, Any]) -> list[Row]:
    """Every row with the most specific pattern that matches; one path may feed several sinks."""
    candidates = [row for row in source.rows if not row.documented_only and _matches(row, segments, record)]
    if not candidates:
        return []
    best = max(row.specificity for row in candidates)
    return [row for row in candidates if row.specificity == best]


def record_leaves(source: Source, file_name: str) -> list[tuple[dict[str, Any], tuple[str, ...], Any]]:
    return [(record, segments, value) for record in source.records[file_name] for segments, value in leaves(record)]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _mapped_values(row: Row, value: Any, record: dict[str, Any]) -> list[Any]:
    items = value if row.each and isinstance(value, list) else [value]
    mapped = [transforms.apply_chain(item, list(row.transform), record) for item in items if item is not None]
    # A transform returns None for a value the field does not carry, such as a version httpx omits.
    return [item for item in mapped if item is not None]


def expected_values(source: Source) -> dict[str, dict[str, set[str]]]:
    """Per file and typed sink, the canonical values the mapping says the writes must carry."""
    expected: dict[str, dict[str, set[str]]] = {}
    for file_name in source.files:
        per_file = expected.setdefault(file_name, {})
        for record, segments, value in record_leaves(source, file_name):
            for row in rows_for(source, segments, record):
                if row.sink.split(".", 1)[0] not in TYPED_SINKS:
                    continue
                if row.when is not None and not transforms.CONDITIONS[row.when](record):
                    continue
                for mapped in _mapped_values(row, value, record):
                    per_file.setdefault(row.sink, set()).add(canonical(mapped))
    return expected


def written_values(source: Source) -> dict[str, dict[str, set[str]]]:
    """Per file and sink, the canonical values the writes carry."""
    written: dict[str, dict[str, set[str]]] = {}
    for batch in source.batches:
        per_file = written.setdefault(batch["file"], {})
        request = batch["request"]
        for kind in ("nodes", "relations"):
            for record in request.get(kind, []):
                for name, value in record.get("properties", {}).items():
                    per_file.setdefault(f"{kind}.{record['type']}.{name}", set()).add(canonical(value))
                for meta in ("observed_at", "source", "label"):
                    if meta in record:
                        per_file.setdefault(f"meta.{meta}", set()).add(canonical(record[meta]))
    return written


def derived_values(source: Source) -> dict[str, set[str]]:
    return {sink_key(item["sink"]): {canonical(value) for value in item["values"]} for item in source.derived}


def redacted_values(source: Source, file_name: str) -> list[str]:
    """Every string a `redact: true` row covers in one file, before redaction."""
    values: list[str] = []
    for record, segments, value in record_leaves(source, file_name):
        if any(row.redact for row in rows_for(source, segments, record)):
            # An empty field, such as the `RawV2` a single-part detector leaves blank, hides nothing.
            values.extend(
                item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str) and item
            )
    return values


def _redact(value: Any, source: Source, record: dict[str, Any], prefix: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item, source, record, (*prefix, key)) for key, item in value.items()}
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        head = (*prefix[:-1], prefix[-1] + "[]")
        return [_redact(item, source, record, head) for item in value]
    if any(row.redact for row in rows_for(source, prefix, record)):
        return [REDACTED for _item in value] if isinstance(value, list) else REDACTED
    return value


def redacted_artifact(source: Source, file_name: str) -> str:
    """The file as evidence may hold it: every redacted leaf replaced by `[REDACTED]`.

    Formats without secret-bearing fields are ingested byte for byte.
    """
    text = (FIXTURES / source.name / file_name).read_text(encoding="utf-8")
    file_format = source.files[file_name]
    if not any(row.redact for row in source.rows) or file_format not in ("json", "jsonl", "bbot_jsonl"):
        return text
    records = [_redact(record, source, record, ()) for record in source.records[file_name]]
    if file_format == "json":
        document = json.loads(text)
        return json.dumps(records if isinstance(document, list) else records[0], indent=2) + "\n"
    return "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)


PLACEHOLDER_PARENT = "00000000-0000-4000-8000-000000000000"


def resolved_request(source: Source, index: int) -> dict[str, Any]:
    """One batch with every cross-batch `{"ref": "<batch>:<node>"}` replaced by a stand-in id."""
    request = json.loads(json.dumps(source.batches[index]["request"]))
    for relation in request.get("relations", []):
        for end in ("source_ref", "target_ref"):
            if "ref" in relation[end]:
                relation[end] = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source.name}:{relation[end]['ref']}"))}
    return request


def node_identities(source: Source) -> list[tuple[int, int, str, str]]:
    """`(batch, index, type, identity)` for every node, with a scoped node keyed under its parent's."""
    scopes = scope_relations()
    cache: dict[tuple[int, int], str] = {}

    def identity(batch: int, index: int) -> str:
        if (batch, index) in cache:
            return cache[(batch, index)]
        request = source.batches[batch]["request"]
        node = request["nodes"][index]
        parent_id = None
        relation_type = scopes.get(node["type"])
        if relation_type is not None:
            scope = next(
                relation
                for relation in request.get("relations", [])
                if relation["type"] == relation_type and relation["target_ref"].get("node_index") == index
            )
            reference = scope["source_ref"]
            parent_batch, parent_index = (
                (batch, reference["node_index"])
                if "node_index" in reference
                else tuple(int(part) for part in reference["ref"].split(":"))
            )
            parent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity(parent_batch, parent_index)))
        properties = json.loads(json.dumps(node["properties"]))
        key = identity_key("nodes", node["type"], properties, parent_id)
        cache[(batch, index)] = f"{node['type']}:{key}"
        return cache[(batch, index)]

    return [
        (batch, index, node["type"], identity(batch, index))
        for batch, item in enumerate(source.batches)
        for index, node in enumerate(item["request"].get("nodes", []))
    ]


def declared_properties(kind: str, type_name: str) -> set[str]:
    definition = catalog_view()[kind][type_name]
    return {*definition["required"], *definition["optional"]}
