"""Literal SQL for the closed graph kinds; cursor/input values are only bound data."""

import json

from ..catalog import scope_relations

# The catalog is the single declaration of which relation supplies a scoped node its parent.
SCOPE_RELATION_TYPES = frozenset(scope_relations().values())

# The names travel as one bound JSON array rather than as generated query text, so the statement
# below stays a fixed literal however many scope relations the catalog grows.
SCOPED_CHILD_RELATIONS = json.dumps(sorted(SCOPE_RELATION_TYPES))

SCOPED_CHILD_BY_PARENT = (
    "SELECT r.id FROM relations r JOIN nodes child ON child.id=r.target_id "
    "WHERE r.source_id=? AND r.type IN (SELECT value FROM json_each(?)) ORDER BY r.id LIMIT 1"
)

# KTD4 over a node row aliased `o`, so a search filters and projects many candidates in one statement;
# `storage/inventory.py` walks the same chain for one row. The chain is `o` and its scoped ancestors
# below the state root, each reached through its scope relation. Every scope relation targets only its
# own scoped type, so one `scope_relation` table serves every level; the statement defines it from the
# bound `SCOPED_CHILD_RELATIONS`. UNION drops a repeated row, so even a corrupt cycle ends.
EFFECTIVE_OWNERSHIP = (
    "(CASE WHEN o.state_root_uuid IS NULL THEN o.ownership "
    "ELSE (SELECT root.ownership FROM nodes root WHERE root.uuid=o.state_root_uuid) END)"
)
EFFECTIVE_AUTHORIZATION = (
    "(CASE WHEN o.state_root_uuid IS NULL THEN o.authorization ELSE ("
    "WITH RECURSIVE chain(id,override) AS (SELECT o.id,o.authorization_override UNION "
    "SELECT parent.id,parent.authorization_override FROM chain "
    "JOIN relations r ON r.target_id=chain.id AND r.type IN scope_relation "
    "JOIN nodes parent ON parent.id=r.source_id WHERE parent.uuid<>o.state_root_uuid) "
    "SELECT CASE WHEN root.authorization='out_of_scope' OR total(chain.override='out_of_scope')>0 "
    "THEN 'out_of_scope' WHEN root.allowlist_scoped=1 "
    "THEN iif(total(chain.override='in_scope')>0,'in_scope','out_of_scope') ELSE root.authorization END "
    "FROM chain JOIN nodes root ON root.uuid=o.state_root_uuid) END)"
)

OWNER_LOOKUP = {
    ("nodes", "id"): "SELECT * FROM nodes WHERE id=?",
    ("nodes", "uuid"): "SELECT * FROM nodes WHERE uuid=?",
    ("relations", "id"): "SELECT * FROM relations WHERE id=?",
    ("relations", "uuid"): "SELECT * FROM relations WHERE uuid=?",
    ("evidence", "id"): "SELECT * FROM evidence WHERE id=?",
    ("evidence", "uuid"): "SELECT * FROM evidence WHERE uuid=?",
}

OWNER_UPDATE = {
    "nodes": "UPDATE nodes SET properties=?,metadata=?,updated_at=?,first_seen=?,last_seen=? WHERE id=?",
    "relations": "UPDATE relations SET properties=?,metadata=?,updated_at=?,first_seen=?,last_seen=? WHERE id=?",
}

# `state_root_uuid` is fixed at creation, so an ID write rewrites only the state it may change.
NODE_STATE_UPDATE = (
    "UPDATE nodes SET ownership=?,authorization=?,allowlist_scoped=?,authorization_override=? WHERE id=?"
)

# Whether a node keeps a link to `ready` evidence outside the bound JSON array of removed UUIDs.
READY_LINK_KEPT = (
    "SELECT EXISTS(SELECT 1 FROM node_evidence l JOIN evidence e ON e.id=l.evidence_id "
    "WHERE l.node_id=? AND e.lifecycle='ready' AND e.uuid NOT IN (SELECT value FROM json_each(?)))"
)

# The state of the node an identity key names, for the rejected-identity checks (R10, R17).
NODE_STATE_BY_KEY = "SELECT uuid,ownership FROM nodes WHERE type=? AND key=?"

# R23: ready relations of the bound JSON array of types into a node, with each source's stored ownership.
RELIANCE_SOURCES = (
    "SELECT r.uuid,s.uuid,s.ownership FROM relations r JOIN nodes s ON s.id=r.source_id "
    "WHERE r.target_id=? AND r.type IN (SELECT value FROM json_each(?)) "
    "AND r.lifecycle='ready' AND s.lifecycle='ready' ORDER BY r.id"
)

# A rejection's purge intents (KTD5): the ready scoped descendants under its state root, and its
# ready incident relations. Each branch of the union uses its own endpoint index.
REJECTION_DESCENDANTS = "SELECT id FROM nodes WHERE state_root_uuid=? AND lifecycle='ready' ORDER BY id"
REJECTION_INCIDENT = (
    "SELECT id FROM relations WHERE source_id=? AND lifecycle='ready' "
    "UNION SELECT id FROM relations WHERE target_id=? AND lifecycle='ready'"
)

OWNER_PENDING = {
    "nodes": "UPDATE nodes SET lifecycle='delete_pending',delete_job_id=?,delete_cascade=?,delete_requested_at=? WHERE id=?",
    "relations": "UPDATE relations SET lifecycle='delete_pending',delete_job_id=?,delete_cascade=?,delete_requested_at=? WHERE id=?",
    "evidence": "UPDATE evidence SET lifecycle='delete_pending',delete_job_id=?,delete_cascade=?,delete_requested_at=? WHERE id=?",
}

OWNER_DELETE = {
    "nodes": "DELETE FROM nodes WHERE id=?",
    "relations": "DELETE FROM relations WHERE id=?",
    "evidence": "DELETE FROM evidence WHERE id=?",
}

CHILD_DELETE = {
    (
        "node_evidence",
        "node_id",
    ): "DELETE FROM node_evidence WHERE rowid IN (SELECT rowid FROM node_evidence WHERE node_id=? LIMIT ?)",
    (
        "node_evidence",
        "evidence_id",
    ): "DELETE FROM node_evidence WHERE rowid IN (SELECT rowid FROM node_evidence WHERE evidence_id=? LIMIT ?)",
    (
        "relation_evidence",
        "relation_id",
    ): "DELETE FROM relation_evidence WHERE rowid IN (SELECT rowid FROM relation_evidence WHERE relation_id=? LIMIT ?)",
    (
        "relation_evidence",
        "evidence_id",
    ): "DELETE FROM relation_evidence WHERE rowid IN (SELECT rowid FROM relation_evidence WHERE evidence_id=? LIMIT ?)",
    (
        "node_property_index",
        "owner_id",
    ): "DELETE FROM node_property_index WHERE rowid IN (SELECT rowid FROM node_property_index WHERE owner_id=? LIMIT ?)",
    (
        "relation_property_index",
        "owner_id",
    ): "DELETE FROM relation_property_index WHERE rowid IN (SELECT rowid FROM relation_property_index WHERE owner_id=? LIMIT ?)",
    (
        "search_documents",
        "node_id",
    ): "DELETE FROM search_documents WHERE rowid IN (SELECT rowid FROM search_documents WHERE node_id=? LIMIT ?)",
    (
        "search_documents",
        "relation_id",
    ): "DELETE FROM search_documents WHERE rowid IN (SELECT rowid FROM search_documents WHERE relation_id=? LIMIT ?)",
    (
        "search_documents",
        "evidence_id",
    ): "DELETE FROM search_documents WHERE rowid IN (SELECT rowid FROM search_documents WHERE evidence_id=? LIMIT ?)",
    (
        "evidence_sources",
        "evidence_id",
    ): "DELETE FROM evidence_sources WHERE rowid IN (SELECT rowid FROM evidence_sources WHERE evidence_id=? LIMIT ?)",
}

CHILD_EXISTS = {
    ("node_evidence", "node_id"): "SELECT 1 FROM node_evidence WHERE node_id=? LIMIT 1",
    ("node_evidence", "evidence_id"): "SELECT 1 FROM node_evidence WHERE evidence_id=? LIMIT 1",
    ("relation_evidence", "relation_id"): "SELECT 1 FROM relation_evidence WHERE relation_id=? LIMIT 1",
    ("relation_evidence", "evidence_id"): "SELECT 1 FROM relation_evidence WHERE evidence_id=? LIMIT 1",
    ("node_property_index", "owner_id"): "SELECT 1 FROM node_property_index WHERE owner_id=? LIMIT 1",
    ("relation_property_index", "owner_id"): "SELECT 1 FROM relation_property_index WHERE owner_id=? LIMIT 1",
    ("search_documents", "node_id"): "SELECT 1 FROM search_documents WHERE node_id=? LIMIT 1",
    ("search_documents", "relation_id"): "SELECT 1 FROM search_documents WHERE relation_id=? LIMIT 1",
    ("search_documents", "evidence_id"): "SELECT 1 FROM search_documents WHERE evidence_id=? LIMIT 1",
    ("evidence_sources", "evidence_id"): "SELECT 1 FROM evidence_sources WHERE evidence_id=? LIMIT 1",
}

LINK_ADD = {
    "nodes": "INSERT OR IGNORE INTO node_evidence(node_id,evidence_id) VALUES(?,?)",
    "relations": "INSERT OR IGNORE INTO relation_evidence(relation_id,evidence_id) VALUES(?,?)",
}

LINK_REMOVE = {
    "nodes": "DELETE FROM node_evidence WHERE node_id=? AND evidence_id=?",
    "relations": "DELETE FROM relation_evidence WHERE relation_id=? AND evidence_id=?",
}

LINK_COUNT = {
    "nodes": "SELECT count(*) FROM node_evidence WHERE node_id=?",
    "relations": "SELECT count(*) FROM relation_evidence WHERE relation_id=?",
}

LINK_PAGE = {
    "nodes": "SELECT a.id,e.uuid FROM node_evidence a JOIN evidence e ON e.id=a.evidence_id WHERE a.node_id=? AND a.id>? ORDER BY a.id LIMIT ?",
    "relations": "SELECT a.id,e.uuid FROM relation_evidence a JOIN evidence e ON e.id=a.evidence_id WHERE a.relation_id=? AND a.id>? ORDER BY a.id LIMIT ?",
}

TARGET_PAGE = {
    "nodes": "SELECT a.id,o.uuid FROM node_evidence a JOIN nodes o ON o.id=a.node_id WHERE a.evidence_id=? AND a.id>? ORDER BY a.id LIMIT ?",
    "relations": "SELECT a.id,o.uuid FROM relation_evidence a JOIN relations o ON o.id=a.relation_id WHERE a.evidence_id=? AND a.id>? ORDER BY a.id LIMIT ?",
}

EVIDENCE_LINK_COUNT = {
    "node_evidence": "SELECT count(*) FROM node_evidence WHERE evidence_id=?",
    "relation_evidence": "SELECT count(*) FROM relation_evidence WHERE evidence_id=?",
}

# Trusted compiler slots accept only server-built expressions. Literal caller
# values never enter these templates: they travel in the parallel bindings list.
PROPERTY_SELECT = {
    "nodes": "(SELECT {expression} FROM node_property_index p WHERE p.owner_id=o.id AND {condition})",
    "relations": "(SELECT {expression} FROM relation_property_index p WHERE p.owner_id=o.id AND {condition})",
}
PROPERTY_DELETE = {
    "nodes": "DELETE FROM node_property_index WHERE owner_id=?",
    "relations": "DELETE FROM relation_property_index WHERE owner_id=?",
}
PROPERTY_INSERT = {
    "nodes": "INSERT INTO node_property_index(owner_id,path,value_type,value_materialized,value) VALUES(?,?,?,?,?)",
    "relations": "INSERT INTO relation_property_index(owner_id,path,value_type,value_materialized,value) VALUES(?,?,?,?,?)",
}
PROPERTY_BODY = {
    "nodes": "SELECT properties FROM nodes WHERE id=?",
    "relations": "SELECT properties FROM relations WHERE id=?",
}
# The last three columns are a node's effective ownership, effective authorization and state root. The
# node statement opens with the `scope_relation` table, so its first binding is `SCOPED_CHILD_RELATIONS`.
SEARCH_CANDIDATE = {
    "nodes": "WITH scope_relation(type) AS (SELECT value FROM json_each(?)) SELECT o.id,o.uuid,o.type,o.key,o.metadata,({expression}),{ownership},{authorization},o.state_root_uuid FROM nodes o WHERE {conditions} ORDER BY o.id",
    "relations": "SELECT o.id,o.uuid,o.type,o.key,o.metadata,({expression}),NULL,NULL,NULL FROM relations o WHERE {conditions} ORDER BY o.id",
}
READY = {
    "nodes": "o.lifecycle='ready'",
    "relations": "o.lifecycle='ready' AND EXISTS(SELECT 1 FROM nodes s WHERE s.id=o.source_id AND s.lifecycle='ready') AND EXISTS(SELECT 1 FROM nodes t WHERE t.id=o.target_id AND t.lifecycle='ready')",
}
ADJACENCY = {
    "source_id": "SELECT o.id,o.uuid,o.type,o.source_id,o.target_id FROM relations o WHERE o.source_id=? AND o.id>? {type_clause} AND {ready} ORDER BY o.id",
    "target_id": "SELECT o.id,o.uuid,o.type,o.source_id,o.target_id FROM relations o WHERE o.target_id=? AND o.id>? {type_clause} AND {ready} ORDER BY o.id",
}
