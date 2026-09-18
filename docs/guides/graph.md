# Build the recon graph

## Discover before writing

Call `kb_types` rather than inventing node or relation names:

```json
{"kind":"nodes","type":"endpoint"}
```

The type entry describes required properties, formats, enums, limits, a
ready-only count, and an `identity` object. `identity.properties` lists the
properties used in the identity. Parent-scoped types also expose
`identity.scope`, which names the relation and endpoint that choose the parent.
Additional JSON properties are allowed, but required fields are strictly typed
and validated without coercion. The MCP computes the read-only key; callers
never submit a key. A successful type listing does not prove database health,
and counts can be deferred while a pending hub delete is too expensive to count
safely.

## Attach scanner evidence to structured facts

Suppose `kb_ingest_evidence` stored a scanner result and returned an
`evidence_id`. One atomic `kb_write` can create a DNS-to-service path and attach
the evidence. The scope relations for the new port and service are part of the
same request:

```json
{
  "nodes": [
    {
      "type": "domain",
      "properties": {"value": "example.com", "scanner": "subfinder"},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "subdomain",
      "properties": {"value": "api.example.com", "status": "observed"},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "ip_address",
      "properties": {"value": "203.0.113.10", "version": 4},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "port",
      "properties": {"number": 443, "transport": "tcp"},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "service",
      "properties": {"name": "http", "secure": true},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    }
  ],
  "relations": [
    {
      "type": "has_subdomain",
      "source_ref": {"node_index": 0},
      "target_ref": {"node_index": 1},
      "properties": {},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "resolves_to",
      "source_ref": {"node_index": 1},
      "target_ref": {"node_index": 2},
      "properties": {},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "has_open_port",
      "source_ref": {"node_index": 2},
      "target_ref": {"node_index": 3},
      "properties": {},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "has_service",
      "source_ref": {"node_index": 3},
      "target_ref": {"node_index": 4},
      "properties": {},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    }
  ]
}
```

Use the real returned evidence ID; the value above only illustrates its format.
Graph and job IDs are UUIDs, while evidence IDs are content hashes.

## Identity and parent scope

Unscoped identity comes from the catalog-selected identity properties. Writing
the same endpoint identity later returns its existing UUID, and multiple graph
records can relate to that shared node. Do not copy a parent UUID or session ID
into unscoped properties to force duplication. Use `{ "id": "..." }` for an
existing node or `{ "node_index": 2 }` for a node in the same call.

`port`, `service`, and `finding` are parent-scoped. Their identity includes the
parent node UUID selected through `has_open_port`, `has_service`, or
`has_finding`, respectively. A new scoped child must arrive with exactly one of
its scope relations in the same `kb_write`; the complete batch is validated and
committed atomically. An existing scoped child can be patched by ID without
repeating its relation, but it cannot be attached to a different parent.

Delete a scoped child with `cascade: true` before deleting its scope relation or
parent node. The cascade removes the child's incident scope relation. The server
rejects deletion of that relation or parent while the child exists, preventing
orphan ports, services, and findings.

## Catalog v1 workspaces

Catalog v2 is a clean cut. A workspace created with catalog v1 is rejected at
startup; it is not migrated or opened read-only. Create a new workspace for this
version.

## Mutable records

Creation requires `type` and `properties`; an ID patch requires `id`. Patches
recursively merge objects, while arrays replace as complete values. An empty
object does not clear existing children:

```json
{"nodes":[{"id":"11111111-1111-4111-8111-111111111111","properties":{"scanner":{}}}]}
```

Remove children explicitly with RFC 6901 JSON Pointers:

```json
{
  "nodes": [{
    "id": "11111111-1111-4111-8111-111111111111",
    "remove_properties": ["/scanner/status", "/scanner/note"]
  }]
}
```

Required identity fields and relation endpoints are immutable. Omitted label or
source values are preserved; explicit `null` clears those optional metadata
fields. Property `null` remains literal JSON data. `observed_at` uses the last
writer's supplied timestamp even when it is chronologically older; it is not a
maximum-time merge.

All writes validate the merged full record and every cross-field relation rule
before one transaction commits. One invalid record or missing evidence link
rejects the entire batch.
