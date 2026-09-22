"""Atomic v3 schema initialization and transaction-local compatibility guard."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from pydantic import ValidationError

from ..catalog import CATALOG_FINGERPRINT, CATALOG_VERSION
from ..config import WorkspacePolicy
from ..errors import ConfigurationError, ContractDimension, ContractMismatchError, UnsupportedLayoutError

if TYPE_CHECKING:
    import apsw

    from ..workspace import WorkspacePaths

SCHEMA_VERSION = 3
INDEX_FORMAT_VERSION = 1

# Every `index_state` the writers use, plus the `incomplete` total that spans
# them. A fresh workspace stores all of them at zero so a reader never has to
# decide whether a missing key means zero or a lost update.
COVERAGE_STATES = ("pending", "ready", "index_failed", "not_applicable")
COVERAGE_SEED = json.dumps(dict.fromkeys((*COVERAGE_STATES, "incomplete"), 0))

# `evidence_coverage` is a derived aggregate, so it is maintained where no caller
# can forget it: by triggers on `evidence` itself, the way `search_fts` is
# maintained by `search_insert`/`search_delete`/`search_update`. Counting the
# table per read instead costs a full scan; measured at 5.4 ms over 10 000 rows
# and 563 ms over 800 000, on every `kb_search` and every status sample, because
# `coverage()` publishes `coverage`/`incomplete` in each search response.
# `incomplete` is compared against zero rather than added raw so the stored
# aggregate keeps the truthiness the replaced `GROUP BY` read had.
COVERAGE_TRIGGERS = """
CREATE TRIGGER evidence_coverage_insert AFTER INSERT ON evidence WHEN new.lifecycle='ready' BEGIN
 UPDATE settings SET evidence_coverage=json_set(evidence_coverage,
  '$.'||new.index_state,coalesce(json_extract(evidence_coverage,'$.'||new.index_state),0)+1,
  '$.incomplete',coalesce(json_extract(evidence_coverage,'$.incomplete'),0)+(new.incomplete!=0))
 WHERE singleton=1;
END;
CREATE TRIGGER evidence_coverage_delete AFTER DELETE ON evidence WHEN old.lifecycle='ready' BEGIN
 UPDATE settings SET evidence_coverage=json_set(evidence_coverage,
  '$.'||old.index_state,coalesce(json_extract(evidence_coverage,'$.'||old.index_state),0)-1,
  '$.incomplete',coalesce(json_extract(evidence_coverage,'$.incomplete'),0)-(old.incomplete!=0))
 WHERE singleton=1;
END;
CREATE TRIGGER evidence_coverage_update AFTER UPDATE ON evidence
WHEN old.lifecycle IS NOT new.lifecycle OR old.index_state IS NOT new.index_state
 OR old.incomplete IS NOT new.incomplete
BEGIN
 UPDATE settings SET evidence_coverage=json_set(evidence_coverage,
  '$.'||old.index_state,coalesce(json_extract(evidence_coverage,'$.'||old.index_state),0)-1,
  '$.incomplete',coalesce(json_extract(evidence_coverage,'$.incomplete'),0)-(old.incomplete!=0))
 WHERE singleton=1 AND old.lifecycle='ready';
 UPDATE settings SET evidence_coverage=json_set(evidence_coverage,
  '$.'||new.index_state,coalesce(json_extract(evidence_coverage,'$.'||new.index_state),0)+1,
  '$.incomplete',coalesce(json_extract(evidence_coverage,'$.incomplete'),0)+(new.incomplete!=0))
 WHERE singleton=1 AND new.lifecycle='ready';
END;
"""

REQUIRED_INDEXES = {
    "jobs_active_lane": "CREATE INDEX jobs_active_lane ON jobs(lane,kind,id) WHERE purge_pending=0 AND state IN ('queued','running')",
    "jobs_blob_locator": "CREATE INDEX jobs_blob_locator ON jobs(blob_sha256) WHERE blob_sha256 IS NOT NULL",
    "nodes_property_fallback": "CREATE INDEX nodes_property_fallback ON nodes(id) WHERE lifecycle='ready' AND coalesce(json_extract(metadata,'$.property_index.complete'),0)!=1",
    "relations_property_fallback": "CREATE INDEX relations_property_fallback ON relations(id,source_id,target_id) WHERE lifecycle='ready' AND coalesce(json_extract(metadata,'$.property_index.complete'),0)!=1",
}

DDL = """
CREATE TABLE settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), workspace_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL, catalog_version INTEGER NOT NULL,
 catalog_fingerprint TEXT NOT NULL, index_format_version INTEGER NOT NULL,
 managed_paths TEXT NOT NULL, query_epoch INTEGER NOT NULL DEFAULT 1,
 policy TEXT NOT NULL DEFAULT '{}', maintenance TEXT NOT NULL DEFAULT '{}',
 terminal_job_counts TEXT NOT NULL DEFAULT '{}', retention TEXT NOT NULL DEFAULT '{}',
 evidence_coverage TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 state TEXT NOT NULL, requested_at REAL NOT NULL, updated_at REAL NOT NULL,
 lease_token TEXT, lease_expires_at REAL, progress TEXT NOT NULL DEFAULT '{}',
 blob_sha256 TEXT NULL CHECK(blob_sha256 IS NULL OR
 (length(blob_sha256)=64 AND blob_sha256 NOT GLOB '*[^0-9a-f]*')),
 cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 error_code TEXT, payload TEXT NOT NULL DEFAULT '{}',
 lane TEXT NOT NULL DEFAULT 'short', finished_at REAL, result TEXT NOT NULL DEFAULT '{}',
 attempts INTEGER NOT NULL DEFAULT 0, purge_tokens TEXT NOT NULL DEFAULT '[]', purge_pending INTEGER NOT NULL DEFAULT 0 CHECK(purge_pending IN (0,1))
);
CREATE UNIQUE INDEX jobs_active_full ON jobs((1)) WHERE kind='reindex' AND json_extract(payload,'$.all')=1 AND state IN ('queued','running');
CREATE INDEX jobs_claim ON jobs(lane,state,lease_expires_at,id);
CREATE INDEX jobs_terminal ON jobs(state,finished_at,id);
CREATE INDEX jobs_purge ON jobs(id) WHERE purge_pending=1;
CREATE INDEX jobs_purge_state ON jobs(state) WHERE purge_pending=1;
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
 key TEXT NOT NULL, properties TEXT NOT NULL CHECK(json_valid(properties)),
 metadata TEXT NOT NULL DEFAULT '{}', lifecycle TEXT NOT NULL DEFAULT 'ready',
 delete_job_id TEXT, delete_cascade INTEGER, delete_requested_at INTEGER, created_at INTEGER, updated_at INTEGER, observed_at INTEGER,
 CHECK((lifecycle='ready' AND delete_job_id IS NULL AND delete_cascade IS NULL AND delete_requested_at IS NULL) OR
 (lifecycle='delete_pending' AND delete_job_id IS NOT NULL AND delete_cascade IS NOT NULL AND delete_cascade IN (0,1) AND typeof(delete_cascade)='integer' AND typeof(delete_requested_at)='integer')),
 UNIQUE(type,key)
);
CREATE TABLE relations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
 source_id INTEGER NOT NULL REFERENCES nodes(id), type TEXT NOT NULL,
 target_id INTEGER NOT NULL REFERENCES nodes(id), key TEXT NOT NULL,
 properties TEXT NOT NULL CHECK(json_valid(properties)), metadata TEXT NOT NULL DEFAULT '{}',
 lifecycle TEXT NOT NULL DEFAULT 'ready', delete_job_id TEXT, delete_cascade INTEGER, delete_requested_at INTEGER,
 created_at INTEGER, updated_at INTEGER, observed_at INTEGER,
 CHECK((lifecycle='ready' AND delete_job_id IS NULL AND delete_cascade IS NULL AND delete_requested_at IS NULL) OR
 (lifecycle='delete_pending' AND delete_job_id IS NOT NULL AND delete_cascade IS NOT NULL AND delete_cascade IN (0,1) AND typeof(delete_cascade)='integer' AND typeof(delete_requested_at)='integer')),
 UNIQUE(source_id,type,target_id,key)
);
CREATE INDEX nodes_type_ready ON nodes(type,lifecycle,id);
CREATE INDEX relations_type_ready ON relations(type,lifecycle,id);
CREATE INDEX relations_outgoing ON relations(source_id,type,target_id,id);
CREATE INDEX relations_outgoing_type_id ON relations(source_id,type,id);
CREATE INDEX relations_incoming_type_id ON relations(target_id,type,id);
CREATE INDEX relations_outgoing_id ON relations(source_id,id);
CREATE INDEX relations_incoming_id ON relations(target_id,id);
CREATE INDEX relations_incoming ON relations(target_id,type,source_id,id);
CREATE TABLE evidence (
 id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL UNIQUE,
 byte_size INTEGER NOT NULL CHECK(byte_size>=0), media_type TEXT, encoding TEXT,
 blob_path TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'ready',
 delete_job_id TEXT, delete_cascade INTEGER, delete_requested_at INTEGER, index_generation INTEGER NOT NULL DEFAULT 0,
 index_state TEXT NOT NULL DEFAULT 'pending', incomplete INTEGER NOT NULL DEFAULT 1,
 index_owner_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL, index_owner_token TEXT,
 created_at INTEGER, updated_at INTEGER,
 CHECK((lifecycle='ready' AND delete_job_id IS NULL AND delete_cascade IS NULL AND delete_requested_at IS NULL) OR
 (lifecycle='delete_pending' AND delete_job_id IS NOT NULL AND delete_cascade IS NOT NULL AND delete_cascade IN (0,1) AND typeof(delete_cascade)='integer' AND typeof(delete_requested_at)='integer'))
);
CREATE TABLE evidence_sources (
 id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id INTEGER NOT NULL REFERENCES evidence(id),
 source TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(evidence_id,source)
);
CREATE TABLE node_evidence (
 id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER NOT NULL REFERENCES nodes(id),
 evidence_id INTEGER NOT NULL REFERENCES evidence(id), UNIQUE(node_id,evidence_id)
);
CREATE INDEX node_evidence_reverse ON node_evidence(evidence_id,node_id);
CREATE TABLE relation_evidence (
 id INTEGER PRIMARY KEY AUTOINCREMENT, relation_id INTEGER NOT NULL REFERENCES relations(id),
 evidence_id INTEGER NOT NULL REFERENCES evidence(id), UNIQUE(relation_id,evidence_id)
);
CREATE INDEX relation_evidence_reverse ON relation_evidence(evidence_id,relation_id);
CREATE INDEX node_evidence_owner_id ON node_evidence(node_id,id);
CREATE INDEX relation_evidence_owner_id ON relation_evidence(relation_id,id);
CREATE INDEX node_evidence_reverse_id ON node_evidence(evidence_id,id);
CREATE INDEX relation_evidence_reverse_id ON relation_evidence(evidence_id,id);
CREATE INDEX evidence_sources_owner_id ON evidence_sources(evidence_id,id);
CREATE TABLE search_documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER REFERENCES nodes(id), relation_id INTEGER REFERENCES relations(id),
 evidence_id INTEGER REFERENCES evidence(id), pointer TEXT, label TEXT, text TEXT NOT NULL,
 byte_start INTEGER, byte_end INTEGER, line_start INTEGER, line_end INTEGER,
 overlap_owner INTEGER, index_generation INTEGER, encoding TEXT, previous_cr INTEGER DEFAULT 0,
 CHECK((node_id IS NOT NULL)+(relation_id IS NOT NULL)+(evidence_id IS NOT NULL)=1)
);
CREATE INDEX search_documents_node ON search_documents(node_id,id);
CREATE INDEX search_documents_relation ON search_documents(relation_id,id);
CREATE INDEX search_documents_evidence ON search_documents(evidence_id,id);
CREATE VIRTUAL TABLE search_fts USING fts5(text,content='search_documents',content_rowid='id',tokenize='unicode61');
CREATE TRIGGER search_insert AFTER INSERT ON search_documents BEGIN
 INSERT INTO search_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER search_delete AFTER DELETE ON search_documents BEGIN
 INSERT INTO search_fts(search_fts,rowid,text) VALUES('delete',old.id,old.text);
END;
CREATE TRIGGER search_update AFTER UPDATE ON search_documents BEGIN
 INSERT INTO search_fts(search_fts,rowid,text) VALUES('delete',old.id,old.text);
 INSERT INTO search_fts(rowid,text) VALUES(new.id,new.text);
END;
"""


def _intent_ddl() -> str:
    """Build immutable owner intents and sparse recovery indexes in sole v2 DDL."""
    return "".join(
        f"""
CREATE INDEX {owner}_pending_owner ON {owner}(id) WHERE lifecycle='delete_pending';
CREATE INDEX {owner}_pending_job ON {owner}(delete_job_id,id) WHERE lifecycle='delete_pending';
CREATE TRIGGER {owner}_immutable_intent BEFORE UPDATE ON {owner}
WHEN old.lifecycle='delete_pending' AND
 (new.lifecycle IS NOT old.lifecycle OR new.delete_job_id IS NOT old.delete_job_id OR
 new.delete_cascade IS NOT old.delete_cascade OR new.delete_requested_at IS NOT old.delete_requested_at)
BEGIN SELECT RAISE(ABORT,'immutable delete intent'); END;
"""
        for owner in ("nodes", "relations", "evidence")
    )


def _property_ddl(owner: str) -> str:
    return f"""
CREATE TABLE {owner}_property_index (
 owner_id INTEGER NOT NULL REFERENCES {owner}s(id), path TEXT NOT NULL COLLATE BINARY,
 value_type TEXT NOT NULL CHECK(value_type IN ('string','number','boolean','null','object','array')),
 value_materialized INTEGER NOT NULL CHECK(value_materialized IN (0,1)),
 value BLOB COLLATE BINARY, PRIMARY KEY(owner_id,path),
 CHECK(
  (value_type='string' AND ((value_materialized=1 AND typeof(value)='text') OR
                            (value_materialized=0 AND value IS NULL))) OR
  (value_type='number' AND value_materialized=1 AND typeof(value) IN ('integer','real')) OR
  (value_type='boolean' AND value_materialized=1 AND typeof(value)='integer' AND value IN (0,1)) OR
  (value_type IN ('null','object','array') AND value_materialized=1 AND value IS NULL)
 )
);
CREATE INDEX {owner}_property_lookup ON {owner}_property_index(path,value_type,value,owner_id);
"""


# Parallel to the SELECT in `SchemaGuard.check`: the name an operator can act on
# for each compared column. Several dimensions can differ at once — a v1
# workspace differs in three — so the first one in this order is reported, which
# is also the coarsest, and fixing it is what makes the rest comparable again.
_CONTRACT_DIMENSIONS: tuple[ContractDimension, ...] = (
    "schema version",
    "catalog version",
    "catalog fingerprint",
    "index format version",
    "managed paths",
)


def _contract_mismatch(row: object, expected: tuple[object, ...]) -> ConfigurationError:
    # A settings row that is absent or not the expected shape names no dimension:
    # the contract could not be read at all, so the reason stays undifferentiated.
    if not isinstance(row, tuple):
        return ConfigurationError("stored database contract is unreadable")
    stored_row = cast("tuple[object, ...]", row)
    if len(stored_row) == len(expected):
        for dimension, stored, want in zip(_CONTRACT_DIMENSIONS, stored_row, expected, strict=True):
            if stored != want:
                return ContractMismatchError(dimension)
    return ConfigurationError("stored database contract is unreadable")


class SchemaGuard:
    """Compare the stored contract and managed paths in the caller's transaction."""

    def __init__(self, workspace: WorkspacePaths) -> None:
        """Freeze relative managed paths for this process."""
        self.paths = json.dumps(
            {
                name: str(workspace.relative(getattr(workspace, name)))
                for name in ("data", "db", "evidence", "tmp", "locks")
            },
            sort_keys=True,
        )

    def check(self, connection: apsw.Connection) -> None:
        """Reject any schema/catalog/index mismatch, including additive changes."""
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "blob_sha256" not in columns:
            raise UnsupportedLayoutError("job ownership")
        expected = (SCHEMA_VERSION, CATALOG_VERSION, CATALOG_FINGERPRINT, INDEX_FORMAT_VERSION, self.paths)
        row = connection.execute(
            "SELECT schema_version,catalog_version,catalog_fingerprint,index_format_version,managed_paths "
            "FROM settings WHERE singleton=1"
        ).get
        if row != expected:
            raise _contract_mismatch(row, expected)

    def policy(self, connection: apsw.Connection) -> WorkspacePolicy:
        """Validate the complete persisted policy; malformed state is configuration."""
        value = connection.execute("SELECT policy FROM settings WHERE singleton=1").get
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, dict) or set(cast("dict[str, object]", parsed)) != set(
                WorkspacePolicy.model_fields
            ):
                raise ConfigurationError("unsupported workspace policy")
            return WorkspacePolicy.model_validate(parsed)
        except (ValidationError, TypeError, ValueError) as exc:
            raise ConfigurationError("unsupported workspace policy") from exc

    def initialize(self, connection: apsw.Connection) -> None:
        """Serialize initialization under SQLite's write lock and roll back failed DDL."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            exists = connection.execute("SELECT 1 FROM sqlite_schema WHERE name='settings'").get
            if not exists:
                connection.execute(
                    DDL + _property_ddl("node") + _property_ddl("relation") + _intent_ddl() + COVERAGE_TRIGGERS
                )
                for definition in REQUIRED_INDEXES.values():
                    connection.execute(definition)
                connection.execute(
                    "INSERT INTO settings(singleton,workspace_id,schema_version,catalog_version,"
                    "catalog_fingerprint,index_format_version,managed_paths,policy,terminal_job_counts,"
                    "evidence_coverage) VALUES(1,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        SCHEMA_VERSION,
                        CATALOG_VERSION,
                        CATALOG_FINGERPRINT,
                        INDEX_FORMAT_VERSION,
                        self.paths,
                        WorkspacePolicy().model_dump_json(),
                        '{"completed":0,"failed_cancelled":0}',
                        COVERAGE_SEED,
                    ),
                )
            self.check(connection)
            self.check_indexes(connection)
            self.policy(connection)
            connection.execute("COMMIT")
        except BaseException:
            if not connection.get_autocommit():
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def check_indexes(connection: apsw.Connection) -> None:
        """Unreleased layouts require explicit offline upgrade, never silent DDL repair."""
        for name, expected in REQUIRED_INDEXES.items():
            actual = connection.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)).get
            if not isinstance(actual, str) or " ".join(actual.split()) != " ".join(expected.split()):
                raise UnsupportedLayoutError("supporting index")
