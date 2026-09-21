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

from justpen_knowledgebase_mcp.catalog import CATALOG_VERSION, catalog_manifest, cross_field_types

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

PAGE = Path("docs/reference/catalog.md")
EMPTY = "—"

# The cross-field rules run after the required map and are the one part of the contract the
# manifest does not publish, so each one is described here. `_cross_field_descriptions` refuses to
# render a page whose keys have drifted from `_CROSS_FIELDS`.
CROSS_FIELDS = {
    "domain": "`value` must classify as a registrable domain against the bundled PSL, not a subdomain.",
    "subdomain": "`value` must classify as a subdomain against the bundled PSL, not a registrable domain.",
    "http_fingerprint": (
        "`favicon_mmh3` requires the signed 32-bit integer spelling; `body_sha256` and "
        "`header_sha256` require 64 lowercase hex characters."
    ),
    "identity_tenant": (
        "`entra_id` requires a canonical lowercase UUID; `okta` requires the bare organization "
        "slug, so a dot is rejected."
    ),
    "ip_address": "`version` must equal the version of the address in `value`.",
    "ip_cidr": "`version` must equal the version of the network in `value`.",
    "repository": (
        "`owner` is checked against the grammar and length of the declared `platform`, and only "
        "`gitlab` accepts a `/` for nested groups."
    ),
    "secret": (
        "The node is rejected if it carries `value`, `secret`, `plaintext`, `password`, `token`, "
        "`key`, `credential`, `match` or `raw`, so the credential itself cannot reach storage."
    ),
    "service": "A TLS-capable registry entry, such as `http`, additionally requires a boolean `secure`.",
    "storage_bucket": (
        "`name` is checked against the declared `provider`: length, grammar, and the prefixes, "
        "suffixes and substrings that provider reserves."
    ),
    "tls_fingerprint": "`value` must be 62 characters for `jarm` and 32 for `ja3s`.",
    "registrar": "`iana_id` must be at least 1, because 0 is what an agent emits for a missing field.",
    "txt_record": (
        "`value` must not begin with `v=spf1`, `v=DMARC1`, `v=DKIM1` or `v=STSv1`; each has a dedicated type."
    ),
}

# Endpoint constraints enforced on stored values rather than on types, which `kb_types` also does
# not publish. Keyed by relation so a reader can find them beside the endpoint matrix.
ENDPOINT_CONSTRAINTS = {
    "has_subdomain": "The target's `value` must end in `.` plus the source's `value`.",
    "contains_ip": "The target address must fall inside the source network, at the same IP version.",
    "contains_cidr": "The target network must be a proper subnet of the source, at the same IP version.",
}

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
        "Cross-field rules",
        "Server-side, not published by `kb_types`",
        "Two properties that individually pass but disagree.",
    ),
    (
        "Endpoint values",
        "Server-side, not published by `kb_types`",
        "A containment or suffix relation whose endpoints do not actually stand in it.",
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


def _cross_field_descriptions(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    declared = set(cross_field_types())
    if declared != set(CROSS_FIELDS):
        raise RuntimeError(f"cross-field descriptions do not match the validators: {declared ^ set(CROSS_FIELDS)}")
    unknown = declared - set(manifest["nodes"])
    if unknown:
        raise RuntimeError(f"cross-field validator for an unknown node type: {sorted(unknown)}")
    return sorted(CROSS_FIELDS.items())


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
        "## Endpoint value constraints",
        "",
        "Three relations also check the endpoints' stored values, not only their types.",
        "",
        _table(
            ["Relation", "Constraint"],
            [[f"`{name}`", rule] for name, rule in sorted(ENDPOINT_CONSTRAINTS.items())],
        ),
        "",
        "## Cross-field rules",
        "",
        "Each runs after the required map has validated the properties it reads.",
        "",
        _table(["Node", "Rule"], [[f"`{name}`", rule] for name, rule in _cross_field_descriptions(manifest)]),
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
