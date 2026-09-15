"""Atomic v1 schema initialization and transaction-local compatibility guard."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from pydantic import ValidationError

from ..catalog import CATALOG_FINGERPRINT, CATALOG_VERSION
from ..config import WorkspacePolicy
from ..errors import ConfigurationError

if TYPE_CHECKING:
    import apsw

    from ..workspace import WorkspacePaths

SCHEMA_VERSION = 1
INDEX_FORMAT_VERSION = 1

DDL = """
CREATE TABLE settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), workspace_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL, catalog_version INTEGER NOT NULL,
 catalog_fingerprint TEXT NOT NULL, index_format_version INTEGER NOT NULL,
 managed_paths TEXT NOT NULL, query_epoch INTEGER NOT NULL DEFAULT 1,
 policy TEXT NOT NULL DEFAULT '{}', maintenance TEXT NOT NULL DEFAULT '{}',
 terminal_job_counts TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 state TEXT NOT NULL, requested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 lease_token TEXT, lease_expires_at TEXT, progress TEXT NOT NULL DEFAULT '{}',
 cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 error_code TEXT, payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
 key TEXT NOT NULL, properties TEXT NOT NULL CHECK(json_valid(properties)),
 metadata TEXT NOT NULL DEFAULT '{}', lifecycle TEXT NOT NULL DEFAULT 'active',
 delete_job_id INTEGER REFERENCES jobs(id), created_at TEXT, updated_at TEXT,
 UNIQUE(type,key)
);
CREATE TABLE relations (
 id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
 source_id INTEGER NOT NULL REFERENCES nodes(id), type TEXT NOT NULL,
 target_id INTEGER NOT NULL REFERENCES nodes(id), key TEXT NOT NULL,
 properties TEXT NOT NULL CHECK(json_valid(properties)), metadata TEXT NOT NULL DEFAULT '{}',
 lifecycle TEXT NOT NULL DEFAULT 'active', delete_job_id INTEGER REFERENCES jobs(id),
 created_at TEXT, updated_at TEXT, CHECK(source_id != target_id),
 UNIQUE(source_id,type,target_id,key)
);
CREATE INDEX relations_outgoing ON relations(source_id,type,target_id,id);
CREATE INDEX relations_incoming ON relations(target_id,type,source_id,id);
CREATE TABLE evidence (
 id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL UNIQUE,
 byte_size INTEGER NOT NULL CHECK(byte_size>=0), media_type TEXT, encoding TEXT,
 blob_path TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'active',
 delete_job_id INTEGER REFERENCES jobs(id), index_generation INTEGER NOT NULL DEFAULT 0,
 index_owner_job_id INTEGER REFERENCES jobs(id), index_owner_token TEXT,
 created_at TEXT, updated_at TEXT
);
CREATE TABLE evidence_sources (
 id INTEGER PRIMARY KEY, evidence_id INTEGER NOT NULL REFERENCES evidence(id),
 source TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(evidence_id,source)
);
CREATE TABLE node_evidence (
 id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL REFERENCES nodes(id),
 evidence_id INTEGER NOT NULL REFERENCES evidence(id), UNIQUE(node_id,evidence_id)
);
CREATE INDEX node_evidence_reverse ON node_evidence(evidence_id,node_id);
CREATE TABLE relation_evidence (
 id INTEGER PRIMARY KEY, relation_id INTEGER NOT NULL REFERENCES relations(id),
 evidence_id INTEGER NOT NULL REFERENCES evidence(id), UNIQUE(relation_id,evidence_id)
);
CREATE INDEX relation_evidence_reverse ON relation_evidence(evidence_id,relation_id);
CREATE TABLE search_documents (
 id INTEGER PRIMARY KEY, node_id INTEGER REFERENCES nodes(id), relation_id INTEGER REFERENCES relations(id),
 evidence_id INTEGER REFERENCES evidence(id), pointer TEXT, label TEXT, text TEXT NOT NULL,
 byte_start INTEGER, byte_end INTEGER, line_start INTEGER, line_end INTEGER,
 overlap_owner INTEGER, index_generation INTEGER,
 CHECK((node_id IS NOT NULL)+(relation_id IS NOT NULL)+(evidence_id IS NOT NULL)=1)
);
CREATE VIRTUAL TABLE search_fts USING fts5(text,content='search_documents',content_rowid='id');
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
        expected = (SCHEMA_VERSION, CATALOG_VERSION, CATALOG_FINGERPRINT, INDEX_FORMAT_VERSION, self.paths)
        row = connection.execute(
            "SELECT schema_version,catalog_version,catalog_fingerprint,index_format_version,managed_paths "
            "FROM settings WHERE singleton=1"
        ).get
        if row != expected:
            raise ConfigurationError("database contract or managed paths differ")

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
                connection.execute(DDL + _property_ddl("node") + _property_ddl("relation"))
                connection.execute(
                    "INSERT INTO settings(singleton,workspace_id,schema_version,catalog_version,"
                    "catalog_fingerprint,index_format_version,managed_paths,policy,terminal_job_counts) VALUES(1,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        SCHEMA_VERSION,
                        CATALOG_VERSION,
                        CATALOG_FINGERPRINT,
                        INDEX_FORMAT_VERSION,
                        self.paths,
                        WorkspacePolicy().model_dump_json(),
                        '{"completed":0,"failed_cancelled":0}',
                    ),
                )
            self.check(connection)
            self.policy(connection)
            connection.execute("COMMIT")
        except BaseException:
            if not connection.get_autocommit():
                connection.execute("ROLLBACK")
            raise
