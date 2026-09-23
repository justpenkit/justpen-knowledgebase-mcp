"""Render the catalog contract as the documentation page agents and readers share.

`kb_types` already returns the live contract, but nothing on the website listed it. Writing that
list by hand would drift the moment a type changes, so this module derives every table and every
sentence about a type from the same manifest and prose the server publishes.
`tests/test_catalog_reference.py` compares the committed page byte for byte with this output after
Markdown formatting, and parses it back into data, so a stale page fails the suite rather than
misleading an agent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from justpen_knowledgebase_mcp.catalog import (
    CATALOG_VERSION,
    catalog_manifest,
    common_descriptions,
    format_descriptions,
    rule_descriptions,
    type_description,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

PAGE = Path("docs/reference/catalog.md")
EMPTY = "—"

GATES = [
    ("Type name", "`nodes` / `relations` keys", "An unknown type, on write and on schema lookup."),
    (
        "Required properties",
        "`required` map",
        "A missing required property, or one whose value fails its rule.",
    ),
    (
        "Optional properties",
        "`optional` map",
        "A declared optional property that is null or whose value fails its rule. An absent one is accepted, and properties outside both maps are stored as submitted and are not validated.",
    ),
    (
        "Format rule",
        "`formats`, the validator, and the discovery schema",
        "A value that does not match the published spelling. A rule missing from any of the three places would silently weaken the contract, so a test pins all three.",
    ),
    ("Identity", "`identity.properties`", "A later write that changes an identity property of an existing record."),
    (
        "Parent scope",
        "`identity.scope`",
        "A new scoped node without exactly one scope relation in the same write, a re-parenting attempt, and a parent or scope-relation delete while the child exists.",
    ),
    (
        "Endpoint types",
        "`sources`, `targets`, `self_edge`",
        "A relation between node types it does not connect, and a self edge where none is allowed.",
    ),
    (
        "Checks",
        "`checks` ids per type",
        "Two properties that individually pass but disagree, and a relation whose endpoints' stored values do not"
        " actually stand in it.",
    ),
    (
        "Canonicalization",
        "`canonicalize` ids per type",
        "Nothing: a declared non-canonical spelling is rewritten before validation, identity and storage.",
    ),
]


def _code(values: Iterable[str]) -> str:
    rendered = ", ".join(f"`{value}`" for value in values)
    return rendered or EMPTY


def _rule(rule: str | list[str]) -> str:
    if isinstance(rule, list):
        return "one of " + ", ".join(f"`{value}`" for value in rule)
    return f"`{rule}`"


def _required(required: dict[str, str | list[str]]) -> str:
    """One property per line. A GFM cell cannot hold a newline, so the break is `<br>`."""
    if not required:
        return EMPTY
    return "<br>".join(f"`{name}`: {_rule(rule)}" for name, rule in required.items())


def _scope(identity: dict[str, Any]) -> str:
    scope = cast("dict[str, str] | None", identity.get("scope"))
    return EMPTY if scope is None else f"`{scope['relation']}` (source)"


def _identity(identity: dict[str, Any]) -> str:
    properties = _code(cast("list[str]", identity["properties"]))
    order = cast("dict[str, Any] | None", identity.get("order_independent"))
    if order is None:
        return properties
    return f"{properties}, with `{order['property']}` hashed order-independently"


def _cell(text: str) -> str:
    """A pipe inside a cell, even inside a code span, would end the cell."""
    return text.replace("|", "\\|")


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _rule_rows(manifest: dict[str, Any], key: str) -> list[list[str]]:
    """One row per rule id that some type lists under `key`, with every type that runs it."""
    descriptions = rule_descriptions()
    users: dict[str, list[str]] = {}
    for kind in ("nodes", "relations"):
        for name, definition in sorted(cast("dict[str, dict[str, Any]]", manifest[kind]).items()):
            for rule_id in definition[key]:
                users.setdefault(rule_id, []).append(name)
    return [[f"`{rule_id}`", _code(types), descriptions[rule_id]] for rule_id, types in sorted(users.items())]


def _node_rows(manifest: dict[str, Any]) -> list[list[str]]:
    nodes = cast("dict[str, dict[str, Any]]", manifest["nodes"])
    return [
        [
            f"`{name}`",
            _identity(definition["identity"]),
            _scope(definition["identity"]),
            _required(definition["required"]),
        ]
        for name, definition in sorted(nodes.items())
    ]


def _relation_rows(manifest: dict[str, Any]) -> list[list[str]]:
    relations = cast("dict[str, dict[str, Any]]", manifest["relations"])
    return [
        [
            f"`{name}`",
            _code(cast("list[str]", definition["sources"])),
            _code(cast("list[str]", definition["targets"])),
            "yes" if definition["self_edge"] else "no",
            _identity(definition["identity"]),
            _required(definition["required"]),
        ]
        for name, definition in sorted(relations.items())
    ]


def _matrix_rows(manifest: dict[str, Any]) -> list[list[str]]:
    relations = cast("dict[str, dict[str, Any]]", manifest["relations"])
    rows: list[list[str]] = []
    for node in sorted(cast("dict[str, Any]", manifest["nodes"])):
        outgoing = sorted(name for name, edge in relations.items() if node in edge["sources"])
        incoming = sorted(name for name, edge in relations.items() if node in edge["targets"])
        rows.append([f"`{node}`", _code(outgoing), _code(incoming)])
    return rows


def _common_value(value: object) -> str:
    if type(value) is bool:
        return str(value).lower()
    return f"`{value}`" if isinstance(value, str) else str(value)


def _type_section(kind: str, name: str, definition: dict[str, Any]) -> list[str]:
    """What one type models and excludes, how it is identified, what runs on it, and each property."""
    docs = type_description(kind, name)
    lines = [f"### `{name}`", "", docs["summary"], "", f"**Not modeled:** {docs['excludes']}", ""]
    if "notes" in docs:
        lines.extend([docs["notes"], ""])
    facts = [f"**Identity:** {_identity(definition['identity'])}"]
    if kind == "nodes":
        facts.append(f"**Parent scope:** {_scope(definition['identity'])}")
    else:
        facts.append(f"**Sources:** {_code(definition['sources'])}")
        facts.append(f"**Targets:** {_code(definition['targets'])}")
        facts.append(f"**Self edge:** {'yes' if definition['self_edge'] else 'no'}")
    facts.append(f"**Checks:** {_code(definition['checks'])}")
    facts.append(f"**Canonicalizations:** {_code(definition['canonicalize'])}")
    lines.extend(["<br>".join(facts), ""])
    required = cast("dict[str, str | list[str]]", definition["required"])
    optional = cast("dict[str, str | list[str]]", definition["optional"])
    rows = [[f"`{prop}`", "yes", _rule(rule), docs["properties"][prop]] for prop, rule in required.items()]
    rows.extend([f"`{prop}`", "no", _rule(rule), docs["properties"][prop]] for prop, rule in sorted(optional.items()))
    if rows:
        lines.extend([_table(["Property", "Required", "Rule", "Meaning"], rows), ""])
    return lines


def render() -> str:
    """Return the complete reference page for the catalog the server currently enforces."""
    manifest = catalog_manifest()
    common = cast("dict[str, Any]", manifest["common"])
    meanings = common_descriptions()
    formats = format_descriptions()
    sections = [
        "# Catalog reference",
        "",
        f"Every table below is generated from catalog v{CATALOG_VERSION}, the same manifest the server"
        " validates writes against. `kb_types` returns the identical contract and the same descriptions at"
        " runtime and is the source to read from a client; this page exists so the contract is reviewable"
        " without a running server. Regenerate it with `make docs-catalog`.",
        "",
        f"The catalog declares **{len(manifest['nodes'])} node types** and"
        f" **{len(manifest['relations'])} relation types**. Conventions that the catalog does not"
        " enforce, and the longer reasoning behind each type, live in"
        " [Graph and search](../tools/graph.md).",
        "",
        "## What a write is checked against",
        "",
        _table(["Gate", "Declared in", "Rejects"], GATES),
        "",
        "## Shared limits",
        "",
        _table(
            ["Setting", "Value", "Meaning"],
            [[f"`{name}`", _common_value(common[name]), meanings[name]] for name in sorted(common)],
        ),
        "",
        "## Bundled registries",
        "",
        "`dns_name` classifies names against the bundled ICANN public suffix list, and `service_name`"
        " accepts the bundled service whitelist. The SHA-256 of each registry's parsed content is part of"
        " the fingerprinted contract, so refreshing either refuses workspaces written under the old one.",
        "",
        _table(
            ["Registry", "Parsed-content SHA-256"],
            [[f"`{name}`", f"`{digest}`"] for name, digest in sorted(manifest["registries"].items())],
        ),
        "",
        "## Node types",
        "",
        "Identity is what makes two writes the same node. A parent scope adds the parent's UUID to"
        " that identity, so the same properties under two parents are two nodes.",
        "",
        _table(["Node", "Identity", "Parent scope", "Required properties"], _node_rows(manifest)),
        "",
        "## Relation types",
        "",
        "A relation's identity is scoped to its endpoints: two edges of one type between one pair"
        " of nodes are the same edge unless an identity property differs.",
        "",
        _table(
            ["Relation", "Sources", "Targets", "Self edge", "Identity", "Required properties"],
            _relation_rows(manifest),
        ),
        "",
        "## Which relations a node can carry",
        "",
        "The same matrix as above, read from the node's side.",
        "",
        _table(["Node", "As source", "As target"], _matrix_rows(manifest)),
        "",
        "## Checks",
        "",
        "Each check runs after the required map has validated the properties it reads. A relation check"
        " may also read both endpoints' stored properties; it runs on the properties that will be stored,"
        " after a patch or a matching earlier edge has been merged. The version suffix changes whenever"
        " what the check accepts changes.",
        "",
        _table(["Check", "Types", "Rule"], _rule_rows(manifest, "checks")),
        "",
        "## Canonicalizations",
        "",
        "Each rewrites a declared non-canonical spelling in place before validation, identity and storage.",
        "",
        _table(["Canonicalization", "Types", "Rewrite"], _rule_rows(manifest, "canonicalize")),
        "",
        "## Format rules",
        "",
        "Every declared property that is not an enum names one of these rules. The version changes"
        " whenever what the rule accepts changes.",
        "",
        _table(
            ["Rule", "Version", "Accepted spelling"],
            [[f"`{name}`", str(item["version"]), item["description"]] for name, item in sorted(formats.items())],
        ),
        "",
        "## Node type details",
        "",
    ]
    for name, definition in sorted(cast("dict[str, dict[str, Any]]", manifest["nodes"]).items()):
        sections.extend(_type_section("nodes", name, definition))
    sections.extend(["## Relation type details", ""])
    for name, definition in sorted(cast("dict[str, dict[str, Any]]", manifest["relations"]).items()):
        sections.extend(_type_section("relations", name, definition))
    return "\n".join(sections)


ROOT = Path(__file__).resolve().parent.parent
COVERAGE_PAGE = Path("docs/reference/asm-coverage.md")
FIXTURES = Path("tests/fixtures/asm")

# Loaded by path, as the tests load this module: `scripts` is not an installed package.
_TRANSFORMS_SPEC = importlib.util.spec_from_file_location("asm_transforms", ROOT / "scripts" / "asm_transforms.py")
if _TRANSFORMS_SPEC is None or _TRANSFORMS_SPEC.loader is None:
    raise RuntimeError("scripts/asm_transforms.py cannot be loaded")
asm_transforms = importlib.util.module_from_spec(_TRANSFORMS_SPEC)
_TRANSFORMS_SPEC.loader.exec_module(asm_transforms)

# Where a finding hangs and what separates two results of one rule, per source (plan §2(g)).
FINDING_PARENTS = [
    ("nuclei `http`, `headless`", "`endpoint` of `matched-at`, query removed", "`matcher-name`, else `extractor-name`"),
    ("nuclei `dns`", "the `domain` or `subdomain` of `host`", "`matcher-name`, else `extractor-name`"),
    ("nuclei `tcp` (network), `javascript`", "`port` of `ip`:`port`", "`matcher-name`, else `extractor-name`"),
    ("nuclei `ssl`", "`port` of `ip`:`port`", "`<host>:<matcher-name>` for a named host, so virtual hosts stay apart"),
    ("nuclei DAST result", "`parameter` named by `fuzzing_parameter` under the endpoint", "`matcher-name`"),
    ("nuclei `whois`", "`domain` of `host`", "`matcher-name`"),
    (
        "BBOT FINDING with a URL",
        "`endpoint` of the URL, query removed",
        "the wrapped tool's sub-id, else `digest16(description)`",
    ),
    ("BBOT FINDING without a URL", "the `domain`, `subdomain` or `ip_address` of `host`", "as above"),
    ("BBOT trufflehog FINDING", "as above", "the first 16 hex characters of the secret's `value_sha256`"),
    ("Manual finding", "the node the analyst names", "chosen by the analyst, empty when the rule has one result"),
]


def _sink_text(sink: object) -> str:
    if isinstance(sink, str):
        return sink.replace("_", " ")
    item = cast("dict[str, str]", sink)
    if "node" in item:
        return f"`{item['node']}.{item['property']}`"
    if "relation" in item:
        return f"`{item['relation']}.{item['property']}` (edge)"
    return f"`{item['meta']}` (write metadata)"


def _row_note(row: dict[str, Any]) -> str:
    notes: list[str] = []
    if row.get("redact"):
        notes.append("redacted before ingest")
    if row.get("each"):
        notes.append("each member")
    if row.get("when"):
        notes.append(f"only when `{row['when']}`")
    if row.get("documented_only"):
        notes.append("documented, absent from the fixture")
    if row.get("reason"):
        notes.append(str(row["reason"]))
    return "; ".join(notes) or EMPTY


def _source_lines(name: str) -> list[str]:
    text = (ROOT / FIXTURES / name / "SOURCE.md").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith(("- **Command:**", "- **Version:**"))]


def _source_section(name: str) -> list[str]:
    mapping = json.loads((ROOT / FIXTURES / name / "mapping.json").read_text(encoding="utf-8"))
    rows = [
        [f"`{row['path']}`", _sink_text(row["sink"]), _code(row.get("transform", [])), _row_note(row)]
        for row in mapping["rows"]
    ]
    lines = [f"### `{name}`", "", *_source_lines(name), f"- **Files:** {_code(sorted(mapping['files']))}", ""]
    lines.extend([_table(["Field", "Sink", "Transforms", "Notes"], rows), ""])
    derived = mapping.get("derived", [])
    if derived:
        lines.extend(["Constants a write carries that no field holds:", ""])
        lines.extend(
            [
                _table(
                    ["Sink", "Values", "Reason"],
                    [
                        [
                            _sink_text(item["sink"]),
                            ", ".join(f"`{json.dumps(value)}`" for value in item["values"]),
                            item["reason"],
                        ]
                        for item in derived
                    ],
                ),
                "",
            ]
        )
    return lines


def _doc_line(function: object) -> str:
    return (getattr(function, "__doc__", None) or "").strip().splitlines()[0]


def render_coverage() -> str:
    """Return the ASM and OSINT coverage page, generated from the coverage fixtures."""
    sources = sorted(path.name for path in (ROOT / FIXTURES).iterdir() if (path / "mapping.json").is_file())
    secret_rows = [
        [f"`{source}`", _code(sorted(fields))] for source, fields in sorted(asm_transforms.SECRET_FIELDS.items())
    ]
    derivations = [
        [f"`{source}`", f"`{path}`", _code(chain), f"`{sink.split('.', 1)[1]}`"]
        for source, path, chain, sink in sorted(asm_transforms.REDACTED_DERIVATIONS)
    ]
    sections = [
        "# ASM and OSINT coverage",
        "",
        "Every table below is generated from the coverage fixtures under `tests/fixtures/asm/`, one per"
        " source. Each fixture holds output derived from the tool's documented schema, a mapping that sends"
        " every emitted field to a catalog property, to evidence or to `non storable` with a reason, and"
        " the `kb_write` batches an agent sends for that output. The test suite holds the three to each other"
        " in both directions: an unmapped field, a mapped value that no write carries and a written value no"
        " field explains all fail. Regenerate the page with `make docs-catalog`.",
        "",
        "A mapping is a recipe for agents, not an ingestion adapter: the server accepts whatever passes the"
        " [catalog](catalog.md), and these tables show which property each field belongs in.",
        "",
        "## Secrets and redaction",
        "",
        "Scanner-reported secret fields are replaced with `[REDACTED]` before the output is ingested as"
        " evidence. The secret's digest is computed first, from the unredacted value, and is the only form"
        " the graph keeps. These fields are redacted per source:",
        "",
        _table(["Source", "Redacted fields"], secret_rows),
        "",
        "Only these derivations may carry a value out of a redacted field, and each strips the secret:",
        "",
        _table(["Source", "Field", "Transforms", "Sink"], derivations),
        "",
        "Three residuals are accepted and stated rather than hidden. A secret carried in a URL path, such"
        " as a webhook token, stays in `endpoint.url`, because only the query is removed. Raw HTTP bodies"
        " and headers are evidence and are full-text indexed. A password digest is an unsalted SHA-256 of a"
        " guessable value, reversible by dictionary.",
        "",
        "## Finding identity per source",
        "",
        "A `finding` is keyed on `rule` and `matcher` under its parent, so the parent and the discriminator"
        " are fixed per source:",
        "",
        _table(["Result", "Parent", "Matcher"], FINDING_PARENTS),
        "",
        "## Sources",
        "",
    ]
    for name in sources:
        sections.extend(_source_section(name))
    sections.extend(
        [
            "## Transforms",
            "",
            "The closed set a mapping may apply, left to right, from `scripts/asm_transforms.py`:",
            "",
            _table(
                ["Transform", "What it does"],
                [[f"`{name}`", _doc_line(function)] for name, function in sorted(asm_transforms.TRANSFORMS.items())],
            ),
            "",
            "## Conditions",
            "",
            "A row with a condition writes its value only when the record meets it, and otherwise leaves the value"
            " in evidence:",
            "",
            _table(
                ["Condition", "Meaning"],
                [[f"`{name}`", _doc_line(function)] for name, function in sorted(asm_transforms.CONDITIONS.items())],
            ),
            "",
        ]
    )
    return "\n".join(sections)


def main() -> None:
    """Write the reference pages beside the rest of the documentation."""
    for page, content in ((PAGE, render()), (COVERAGE_PAGE, render_coverage())):
        target = ROOT / page
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
