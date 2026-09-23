"""Stored contract decisions and initialization rollback with SQL execution isolated."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import ConfigurationError, ContractMismatchError
from justpen_knowledgebase_mcp.storage import properties, schema

from .helpers import cursor, database, owner


def guard():
    workspace = Mock()
    workspace.relative.side_effect = lambda path: Path(str(path))
    for field in ("data", "db", "evidence", "tmp", "locks"):
        setattr(workspace, field, field)
    return schema.SchemaGuard(workspace)


def expected(guard):
    return (
        schema.SCHEMA_VERSION,
        schema.CATALOG_VERSION,
        schema.CATALOG_FINGERPRINT,
        schema.INDEX_FORMAT_VERSION,
        guard.paths,
    )


@pytest.mark.parametrize(
    ("field", "dimension"),
    list(
        enumerate(["schema version", "catalog version", "catalog fingerprint", "index format version", "managed paths"])
    ),
)
def test_guard_rejects_and_names_every_contract_dimension(field, dimension):
    value = guard()
    matching = expected(value)
    value.check(database(cursor(rows=[(0, "blob_sha256")]), cursor(value=matching)))
    mismatch = list(matching)
    mismatch[field] = "changed"
    with pytest.raises(ContractMismatchError) as rejected:
        value.check(database(cursor(rows=[(0, "blob_sha256")]), cursor(value=tuple(mismatch))))
    assert rejected.value.dimension == dimension
    assert dimension in str(rejected.value)
    assert "changed" not in str(rejected.value)


@pytest.mark.parametrize("row", [None, (), (schema.SCHEMA_VERSION,)])
def test_guard_names_no_dimension_when_the_contract_row_is_unreadable(row):
    # Without a comparable row there is no dimension to name, and inventing one
    # would send an operator after the wrong thing.
    with pytest.raises(ConfigurationError, match="unreadable") as rejected:
        guard().check(database(cursor(rows=[(0, "blob_sha256")]), cursor(value=row)))
    assert not isinstance(rejected.value, ContractMismatchError)


def test_guard_rejects_v1_catalog_contract():
    value = guard()
    v1 = (1, 1, "v1-catalog-fingerprint", schema.INDEX_FORMAT_VERSION, value.paths)
    # Three dimensions differ at once here; the guard reports the coarsest.
    with pytest.raises(ContractMismatchError, match="stored schema version differs") as rejected:
        value.check(database(cursor(rows=[(0, "blob_sha256")]), cursor(value=v1)))
    assert rejected.value.dimension == "schema version"
    assert "v1-catalog-fingerprint" not in str(rejected.value)


def test_guard_rejects_v2_catalog_contract():
    value = guard()
    v2_fingerprint = "d25e5c37a1e363eccfcadbd7919aac85b017b730380badd765fd3c1a9252c7c9"
    v2 = (schema.SCHEMA_VERSION, 2, v2_fingerprint, schema.INDEX_FORMAT_VERSION, value.paths)
    # The catalog version and fingerprint both differ; the guard reports the coarser one.
    with pytest.raises(ContractMismatchError, match="stored catalog version differs") as rejected:
        value.check(database(cursor(rows=[(0, "blob_sha256")]), cursor(value=v2)))
    assert rejected.value.dimension == "catalog version"
    assert v2_fingerprint not in str(rejected.value)


@pytest.mark.parametrize("raw", ["{}", "[]", "null", "invalid", '{"wal_low_bytes":true}'])
def test_policy_requires_complete_strict_persisted_schema(raw):
    with pytest.raises(ConfigurationError):
        guard().policy(database(cursor(value=raw)))
    assert guard().policy(database(cursor(value=WorkspacePolicy().model_dump_json()))) == WorkspacePolicy()


@pytest.mark.parametrize("exists", [False, True])
def test_initialization_checks_contract_before_commit(exists):
    value = guard()
    responses = [cursor(), cursor(value=exists)]
    if not exists:
        responses.extend([cursor(), *[cursor() for _ in schema.REQUIRED_INDEXES], cursor()])
    responses.extend([cursor(rows=[(0, "blob_sha256")]), cursor(value=expected(value))])
    responses.extend(cursor(value=definition) for definition in schema.REQUIRED_INDEXES.values())
    responses.extend([cursor(value=WorkspacePolicy().model_dump_json()), cursor()])
    db = database(*responses)
    value.initialize(db)
    assert db.execute.call_args_list[0].args == ("BEGIN IMMEDIATE",)
    assert db.execute.call_args.args == ("COMMIT",)
    if not exists:
        settings = next(
            call.args[1] for call in db.execute.call_args_list if call.args[0].startswith("INSERT INTO settings")
        )
        assert settings[1:5] == expected(value)[:4]
        assert json.loads(settings[-2]) == {"completed": 0, "failed_cancelled": 0}
        assert json.loads(settings[-1]) == dict.fromkeys((*schema.COVERAGE_STATES, "incomplete"), 0)


def test_failed_initialization_rolls_back_only_active_transaction():
    value = guard()
    for autocommit in (False, True):
        db = database(cursor(), cursor(value=True), cursor(value=None), cursor())
        db.get_autocommit.return_value = autocommit
        with pytest.raises(ConfigurationError):
            value.initialize(db)
        statements = [call.args[0] for call in db.execute.call_args_list]
        assert ("ROLLBACK" in statements) != autocommit
        assert "COMMIT" not in statements


def test_property_projection_replaces_derived_rows_and_coverage():
    db = database()
    row = owner()
    properties.refresh_properties(db, "nodes", row, {"name": "example.com", "large": "x" * 1100})
    rows = list(db.executemany.call_args.args[1])
    assert any(item[1] == "/name" and item[-1] == "example.com" for item in rows)
    assert any(item[1] == "/large" and item[-2:] == (0, None) for item in rows)
    metadata = row["metadata"]
    assert isinstance(metadata, str)
    assert json.loads(metadata)["property_index"]["omitted_values"] == 1


def test_guard_rejects_missing_ownership_column_before_contract_use():
    with pytest.raises(ConfigurationError, match="offline workspace upgrade required"):
        guard().check(database(cursor(rows=[(0, "progress")])))
