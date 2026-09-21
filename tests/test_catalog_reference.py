"""Parse the published catalog reference back into data and hold it to the manifest.

Comparing rendered bytes would break every time the Markdown formatter adjusts a cell width, so
these tests read the page the website serves and reconstruct the contract from it. A type, an
endpoint, an identity or a format rule that changed in the catalog without the page being
regenerated fails here.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import justpen_knowledgebase_mcp.catalog as catalog_module
from justpen_knowledgebase_mcp.catalog import catalog_manifest

ROOT = Path(__file__).resolve().parent.parent
# Loaded by path, as tests/test_release.py loads its script: `scripts` is not an installed package.
REFERENCE_SPEC = importlib.util.spec_from_file_location("catalog_reference", ROOT / "scripts/catalog_reference.py")
assert REFERENCE_SPEC is not None
assert REFERENCE_SPEC.loader is not None
catalog_reference = importlib.util.module_from_spec(REFERENCE_SPEC)
REFERENCE_SPEC.loader.exec_module(catalog_reference)

PAGE = catalog_reference.PAGE
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
    lines = [line for line in _section(page, heading).splitlines() if line.startswith("|")]
    cells = [[cell.strip().replace("\\", "") for cell in line.strip("|").split("|")] for line in lines]
    return [row for row in cells[2:] if row]


def _names(cell: str) -> list[str]:
    return [] if cell == EMPTY else re.findall(r"`([^`]+)`", cell)


def _required(cell: str) -> dict[str, str | list[str]]:
    if cell == EMPTY:
        return {}
    required: dict[str, str | list[str]] = {}
    for item in cell.split("; "):
        name, _, rule = item.partition(": ")
        values = _names(rule)
        required[_names(name)[0]] = values if rule.startswith("one of ") else values[0]
    return required


def test_the_committed_page_is_what_the_generator_produces(page: str) -> None:
    """Formatting may differ, but no table cell may. Regenerate with `make docs-catalog`."""
    generated = render()
    for heading in ("Node types", "Relation types", "Which relations a node can carry", "Format rules"):
        assert _rows(page, heading) == _rows(generated, heading), heading


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


def test_every_published_format_rule_appears_with_its_description(page: str) -> None:
    formats = catalog_manifest()["formats"]
    rows = {row[0].strip("`"): row[1] for row in _rows(page, "Format rules")}

    assert set(rows) == set(formats)
    for name, description in formats.items():
        assert rows[name] == description


def test_the_page_documents_exactly_the_cross_field_validators_that_run(page: str) -> None:
    rows = {row[0].strip("`") for row in _rows(page, "Cross-field rules")}

    assert rows == set(catalog_module._CROSS_FIELDS)


def test_rendering_refuses_a_cross_field_description_that_has_drifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The descriptions are hand-written because the manifest does not publish them, so the
    generator is the thing that has to notice when a validator is added without one."""
    monkeypatch.setitem(catalog_module._CROSS_FIELDS, "asn", catalog_module._cross_field_registrar)

    with pytest.raises(RuntimeError, match="cross-field descriptions"):
        render()
