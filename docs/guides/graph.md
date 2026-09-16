# Build the recon graph

## Discover before writing

Call `kb_types` rather than inventing node or relation names:

```json
{"kind":"nodes","type":"endpoint"}
```

The type entry describes required properties, the identity subset, formats,
enums, limits, and a ready-only count. Additional JSON properties are allowed,
but required fields are strictly typed and validated without coercion. The MCP
computes the read-only key; callers never submit a key. A successful type listing
does not prove database health, and counts can be deferred while a pending hub
delete is too expensive to count safely.

## Attach scanner evidence to structured facts

Suppose `kb_ingest_evidence` stored a scanner result and returned an
`evidence_id`. One atomic `kb_write` can upsert the natural identities and join
them with a same-call `node_index` reference:

```json
{
  "nodes": [
    {
      "type": "domain",
      "properties": {"name": "example.com", "scanner": "subfinder"},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "hostname",
      "properties": {"name": "api.example.com", "status": "observed"},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    }
  ],
  "relations": [
    {
      "type": "name_in_domain",
      "source_ref": {"node_index": 1},
      "target_ref": {"node_index": 0},
      "properties": {},
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    }
  ]
}
```

Use the real returned evidence ID; the value above only illustrates its format.
Graph and job IDs are UUIDs, while evidence IDs are content hashes.

## Shared nodes and multiple parents

Identity comes only from the catalog-selected required properties. Writing the
same endpoint identity later returns its existing UUID, and separate application
or service nodes can both relate to it. Do not copy a parent UUID or session ID
into endpoint properties to force duplication. Use `{ "id": "..." }` for an
existing endpoint or `{ "node_index": 2 }` for a node created in the same call.

For example, two applications can each create a `contacts` relation to one
endpoint node. Each relation carries its own required `context` and `basis`, so
static and dynamic observations do not overwrite shared endpoint facts.

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
