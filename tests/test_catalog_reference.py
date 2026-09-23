"""Hold the published catalog reference to the generator and to the manifest.

The committed page must equal the generator's output after the repository's Markdown formatting,
byte for byte, so any stale sentence fails. The remaining tests parse the page back into data and
compare it with the manifest, so a generator that drops a type, an endpoint, an identity, a rule or
a property description fails too, not only a page that was not regenerated.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import mdformat
import pytest

from justpen_knowledgebase_mcp.catalog import (
    catalog_manifest,
    format_descriptions,
    rule_descriptions,
    scope_relations,
    type_description,
)

ROOT = Path(__file__).resolve().parent.parent
# Loaded by path, as tests/test_release.py loads its script: `scripts` is not an installed package.
REFERENCE_SPEC = importlib.util.spec_from_file_location("catalog_reference", ROOT / "scripts/catalog_reference.py")
assert REFERENCE_SPEC is not None
assert REFERENCE_SPEC.loader is not None
catalog_reference = importlib.util.module_from_spec(REFERENCE_SPEC)
REFERENCE_SPEC.loader.exec_module(catalog_reference)

PAGE = catalog_reference.PAGE
# `scripts/catalog_reference.py` emits only the reference page, so the tool page is hand-written
# and its parent-scope prose is pinned here instead of regenerated.
GRAPH_PAGE = "docs/tools/graph.md"
render = catalog_reference.render
EMPTY = "—"


@pytest.fixture(scope="module")
def page() -> str:
    return (ROOT / PAGE).read_text(encoding="utf-8")


def _section(page: str, heading: str) -> str:
    start = page.index(f"\n## {heading}\n")
    remainder = page[start + 1 :]
    end = remainder.find("\n## ", 1)
    return remainder if end == -1 else remainder[:end]


def _rows(page: str, heading: str) -> list[list[str]]:
    """Markdown escapes outside code spans are formatting, so a cell is compared unescaped."""
    return _table_rows(_section(page, heading))


def _table_rows(text: str) -> list[list[str]]:
    lines = [line for line in text.splitlines() if line.startswith("|")]
    cells = [[cell.strip().replace("\\", "") for cell in line.strip("|").split("|")] for line in lines]
    return [row for row in cells[2:] if row]


def _code(values: list[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) or EMPTY


def _names(cell: str) -> list[str]:
    return [] if cell == EMPTY else re.findall(r"`([^`]+)`", cell)


def _required(cell: str) -> dict[str, str | list[str]]:
    if cell == EMPTY:
        return {}
    required: dict[str, str | list[str]] = {}
    for item in cell.split("<br>"):
        name, _, rule = item.partition(": ")
        values = _names(rule)
        required[_names(name)[0]] = values if rule.startswith("one of ") else values[0]
    return required


def _formatted(markdown: str) -> str:
    """Format the way `make format-md` does: the repository options and every installed extension.

    The CLI enables each installed parser extension by default, so the test asks the same entry
    point group rather than naming extensions a plugin upgrade could add to.
    """
    options = tomllib.loads((ROOT / ".mdformat.toml").read_text(encoding="utf-8"))
    extensions = {entry.name for entry in entry_points(group="mdformat.parser_extension")}
    return mdformat.text(markdown, options=options, extensions=extensions)


def test_the_committed_page_is_what_the_generator_produces(page: str) -> None:
    """Byte for byte after formatting (AC-3). Regenerate with `make docs-catalog`."""
    assert page == _formatted(render())


def test_every_node_type_is_listed_with_its_identity_scope_and_required_map(page: str) -> None:
    nodes = catalog_manifest()["nodes"]
    rows = {row[0].strip("`"): row for row in _rows(page, "Node types")}

    assert set(rows) == set(nodes)
    for name, definition in nodes.items():
        _type, identity, scope, required = rows[name]
        assert _names(identity)[: len(definition["identity"]["properties"])] == definition["identity"]["properties"]
        declared = definition["identity"].get("scope")
        assert _names(scope) == ([] if declared is None else [declared["relation"]])
        assert _required(required) == definition["required"]


def test_every_relation_type_is_listed_with_its_endpoints_self_edge_and_required_map(page: str) -> None:
    relations = catalog_manifest()["relations"]
    rows = {row[0].strip("`"): row for row in _rows(page, "Relation types")}

    assert set(rows) == set(relations)
    for name, definition in relations.items():
        _type, sources, targets, self_edge, identity, required = rows[name]
        assert _names(sources) == definition["sources"]
        assert _names(targets) == definition["targets"]
        assert (self_edge == "yes") is definition["self_edge"]
        assert _names(identity)[: len(definition["identity"]["properties"])] == definition["identity"]["properties"]
        assert _required(required) == definition["required"]


def test_the_node_matrix_agrees_with_the_relation_endpoints(page: str) -> None:
    """The matrix is the same fact read from the node's side; a stale one would send an agent
    looking for an edge the server refuses."""
    manifest = catalog_manifest()
    rows = {row[0].strip("`"): row for row in _rows(page, "Which relations a node can carry")}

    assert set(rows) == set(manifest["nodes"])
    for node, (_name, outgoing, incoming) in rows.items():
        expected_out = sorted(n for n, edge in manifest["relations"].items() if node in edge["sources"])
        expected_in = sorted(n for n, edge in manifest["relations"].items() if node in edge["targets"])
        assert _names(outgoing) == expected_out, node
        assert _names(incoming) == expected_in, node


def test_every_published_format_rule_appears_with_its_version_and_description(page: str) -> None:
    formats = format_descriptions()
    rows = {row[0].strip("`"): row[1:] for row in _rows(page, "Format rules")}

    assert set(rows) == set(catalog_manifest()["formats"])
    for name, item in formats.items():
        assert rows[name] == [str(item["version"]), item["description"].replace("\\", "")]


def _type_sections(page: str, heading: str) -> dict[str, str]:
    section = _section(page, heading)
    parts = section.split("\n### `")[1:]
    return {part.split("`", 1)[0]: part for part in parts}


@pytest.mark.parametrize(("kind", "heading"), [("nodes", "Node type details"), ("relations", "Relation type details")])
def test_every_type_section_shows_props_identity_and_checks(page: str, kind: str, heading: str) -> None:
    """AC-2: each type's section states what it models and excludes, its identity and scope, the
    checks and canonicalizations that run on it, and every property with its rule and meaning."""
    definitions = catalog_manifest()[kind]
    sections = _type_sections(page, heading)

    assert set(sections) == set(definitions)
    for name, definition in definitions.items():
        text = " ".join(sections[name].split())
        docs = type_description(kind, name)
        for key in ("summary", "excludes", "notes"):
            if key in docs:
                assert " ".join(docs[key].split()) in text, (name, key)
        assert f"**Identity:** {_code(definition['identity']['properties'])}" in text, name
        assert f"**Checks:** {_code(definition['checks'])}" in text, name
        assert f"**Canonicalizations:** {_code(definition['canonicalize'])}" in text, name
        scope = definition["identity"].get("scope")
        if kind == "nodes":
            expected_scope = EMPTY if scope is None else f"`{scope['relation']}` (source)"
            assert f"**Parent scope:** {expected_scope}" in text, name
        else:
            assert f"**Sources:** {_code(definition['sources'])}" in text, name
            assert f"**Targets:** {_code(definition['targets'])}" in text, name
        rows = {row[0].strip("`"): row for row in _table_rows(sections[name])}
        assert set(rows) == set(definition["required"]), name
        for prop, (_prop, required, _rule, meaning) in rows.items():
            assert required == "yes"
            assert meaning == docs["properties"][prop].replace("\\", ""), (name, prop)


@pytest.mark.parametrize(("heading", "key"), [("Checks", "checks"), ("Canonicalizations", "canonicalize")])
def test_the_page_documents_every_published_rule_with_its_types(page: str, heading: str, key: str) -> None:
    """The rule prose lives beside the callable in the catalog, so the page can only drift by not
    being regenerated; the ids and the types that run them are read back from the manifest."""
    manifest = catalog_manifest()
    expected: dict[str, list[str]] = {}
    for kind in ("nodes", "relations"):
        for name, definition in sorted(manifest[kind].items()):
            for rule_id in definition[key]:
                expected.setdefault(rule_id, []).append(name)
    rows = {row[0].strip("`"): row for row in _rows(page, heading)}

    assert set(rows) == set(expected)
    for rule_id, (_id, types, description) in rows.items():
        assert _names(types) == expected[rule_id], rule_id
        assert description == rule_descriptions()[rule_id].replace("\\", ""), rule_id


@pytest.fixture(scope="module")
def graph_page() -> str:
    """Read the tool page as one line, so an assertion survives the Markdown wrap width."""
    return " ".join((ROOT / GRAPH_PAGE).read_text(encoding="utf-8").split())


def _listed(page: str, opening: str, closing: str) -> list[str]:
    start = page.index(opening)
    return sorted(_names(page[start : page.index(closing, start)]))


def test_the_parent_scope_prose_enumerates_exactly_the_scoped_types(graph_page: str) -> None:
    """Both enumerations are derived from the catalog the server actually reads.

    The page stated `port`, `service`, `finding`, `dkim_record` and `parameter` and omitted
    `mta_sts_policy`, which `catalog.py` has declared parent-scoped since it was added. A
    hand-copied list here would drift the same way, so the expectation is `scope_relations()`.
    """
    scoped = scope_relations()

    assert _listed(graph_page, "Creating a new ", " without exactly") == sorted(scoped)
    assert _listed(graph_page, "Deleting `has_", ", or deleting its parent node") == sorted(scoped.values())
