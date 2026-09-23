"""Render the catalog contract as the documentation table set agents and readers share.

`kb_types` already returns the live contract, but nothing on the website listed it. Writing that
list by hand would drift the moment a type changes, so this module derives every table from the
same manifest the server validates against. `tests/test_catalog_reference.py` parses the published
page back into data and compares it to the manifest, so a stale page fails the suite rather than
misleading an agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from justpen_knowledgebase_mcp.catalog import CATALOG_VERSION, catalog_manifest, rule_descriptions

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

PAGE = Path("docs/reference/catalog.md")
EMPTY = "—"

GATES = [
    ("Type name", "`nodes` / `relations` keys", "An unknown type, on write and on schema lookup."),
    (
        "Required properties",
        "`required` map",
        "A missing required property, or one whose value fails its rule. Properties outside the map are stored as submitted and are not validated.",
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


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
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


def render() -> str:
    """Return the complete reference page for the catalog the server currently enforces."""
    manifest = catalog_manifest()
    common = cast("dict[str, Any]", manifest["common"])
    formats = cast("dict[str, str]", manifest["formats"])
    sections = [
        "# Catalog reference",
        "",
        f"Every table below is generated from catalog v{CATALOG_VERSION}, the same manifest the server"
        " validates writes against. `kb_types` returns the identical contract at runtime and is the"
        " source to read from a client; this page exists so the contract is reviewable without a"
        " running server. Regenerate it with `make docs-catalog`.",
        "",
        f"The catalog declares **{len(manifest['nodes'])} node types** and"
        f" **{len(manifest['relations'])} relation types**. Conventions that the catalog does not"
        " enforce, and the reasoning behind each type, live in"
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
            [
                [
                    "`additional_properties`",
                    str(common["additional_properties"]).lower(),
                    "Properties outside the required map are accepted and stored unvalidated.",
                ],
                [
                    "`coercion`",
                    str(common["coercion"]).lower(),
                    'No JSON type is converted. A string `"443"` is not an integer.',
                ],
                [
                    "`required_nonnull`",
                    str(common["required_nonnull"]).lower(),
                    "A required property may not be null or absent.",
                ],
                ["`depth`", str(common["depth"]), "Maximum nesting depth of the properties object."],
                [
                    "`properties_bytes`",
                    str(common["properties_bytes"]),
                    "Maximum size of one canonical properties object, in UTF-8 bytes.",
                ],
                ["`integers`", f"`{common['integers']}`", "Integers outside signed 64-bit are rejected."],
                ["`numbers`", f"`{common['numbers']}`", "NaN and infinity are rejected."],
            ],
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
        "Every required property that is not an enum names one of these rules.",
        "",
        _table(["Rule", "Accepted spelling"], [[f"`{name}`", text] for name, text in sorted(formats.items())]),
        "",
    ]
    return "\n".join(sections)


def main() -> None:
    """Write the reference page beside the rest of the documentation."""
    target = Path(__file__).resolve().parent.parent / PAGE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()
