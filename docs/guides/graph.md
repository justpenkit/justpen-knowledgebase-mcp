# Build the recon graph

## Discover before writing

Call `kb_types` rather than inventing node or relation names:

```json
{"kind":"nodes","type":"endpoint"}
```

The type entry describes required properties, formats, enums, limits, a
ready-only count, an `identity` object, and an `inventory` declaration.
`identity.properties` lists the properties used in the identity. Parent-scoped
types also expose `identity.scope`, which names the relation and endpoint that
choose the parent. `inventory` says whether the type holds
[inventory state](#inventory-state).
Additional JSON properties are allowed, but required fields are strictly typed
and validated without coercion. The MCP computes the read-only key; callers
never submit a key. A successful type listing does not prove database health,
and counts can be deferred while a pending hub delete is too expensive to count
safely.

## Attach scanner evidence to structured facts

Suppose `kb_ingest_evidence` stored a scanner result and returned an
`evidence_id`. One atomic `kb_write` can create a DNS-to-service path and attach
the evidence. The scope relations for the new port and service are part of the
same request. The domain and subdomain are the target's own, and the evidence
on each supports that claim. The address is still unattributed, so it is a
candidate. The port and service inherit their state from the address:

```json
{
  "nodes": [
    {
      "type": "domain",
      "properties": {"value": "example.com", "scanner": "subfinder"},
      "ownership": "owned",
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "subdomain",
      "properties": {"value": "api.example.com"},
      "ownership": "owned",
      "evidence_add": ["e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    },
    {
      "type": "ip_address",
      "properties": {"value": "203.0.113.10", "version": 4},
      "ownership": "candidate",
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
Graph and job IDs are UUIDs, while evidence IDs are content hashes. Each
acknowledgment echoes the node's effective `ownership` and `authorization`.
Ownership, authorization and the observation times are top-level fields, never
entries in `properties`.

## Identity and parent scope

Unscoped identity comes from the catalog-selected identity properties. Writing
the same endpoint identity later returns its existing UUID, and multiple graph
records can relate to that shared node. Do not copy a parent UUID or session ID
into unscoped properties to force duplication. Use `{ "id": "..." }` for an
existing node or `{ "node_index": 2 }` for a node in the same call.

`port`, `service`, `finding`, `dkim_record`, `parameter`, and `mta_sts_policy`
are parent-scoped. Their identity includes the parent node UUID selected through
`has_open_port`, `has_service`, `has_finding`, `has_dkim_selector`,
`has_parameter`, or `has_mta_sts_policy`, respectively. A new scoped child must arrive with exactly one of its scope
relations in the same `kb_write`; the complete batch is validated and committed
atomically. An existing scoped child can be patched by ID without repeating its
relation, but it cannot be attached to a different parent.

A scope parent can itself be scoped. `has_finding` accepts `parameter` and
`dkim_record` as sources, so a finding about one query parameter is keyed on that
parameter, which is in turn keyed on its endpoint. The server resolves scoped
nodes parent-first inside the batch, so one `kb_write` can create the endpoint,
the parameter, the finding and all three relations together.

Delete a scoped child with `cascade: true` before deleting its scope relation or
parent node. The cascade removes the child's incident scope relation. The server
rejects deletion of that relation or parent while the child exists, preventing
orphan ports, services, findings, DKIM records, parameters, MTA-STS policies,
and domain registrations. A chain deletes innermost first: an endpoint with a parameter that
carries a finding takes three deletes, finding, parameter, endpoint, and the two
outer ones are refused until the level below them is gone.

## Inventory state

An engagement meets three kinds of foreign asset, and only two of them belong
in the graph. Write the target's own assets as `owned` and the third-party
infrastructure the target depends on, such as a SaaS host its name CNAMEs to, as
`dependency`. Write an asset whose attribution is still open as `candidate`.
Leave unrelated neighbors in evidence and never write them as nodes: the other
tenants of a shared IP address, reverse-IP results from a `dependency` address,
or names on a certificate that belong to someone else.

Authorization is a separate axis with the values `in_scope`, `out_of_scope` and
`unknown`, because a contract can exclude an owned system or include a
permitted third-party one. Only `in_scope` authorizes active testing; `owned`
and `unknown` do not. The server performs no testing, so the agent enforces this
rule.

Each node type declares one `inventory` value, and `kb_types` publishes it
beside the vocabularies and the testing rule:

| `inventory` | Meaning                                                                                                   |
| ----------- | --------------------------------------------------------------------------------------------------------- |
| `carries`   | The node holds its own ownership and authorization. Creating one requires `ownership`.                    |
| `inherits`  | A parent-scoped node. It takes both from the root of its scope chain and may only override authorization. |
| `none`      | A vocabulary or shared record, such as `technology`. It holds no state and refuses the state fields.      |

A `carries` type may also list `allowed_ownership`, which narrows the ownership
values it accepts. `package`, a package or container image the target publishes,
lists it so that a package is never a `dependency`: a third-party component the
target runs is a `technology` instead.

Creating a `carries` node requires `ownership`. `candidate` needs nothing more;
`owned` and `dependency` are claims and need `evidence_add` on that node in the
same write. Authorization starts at `unknown` unless the creating write sets it.
A later write that matches the node by identity never changes its stored
state, so a rescan cannot undo a classification. State changes only through a
write addressed by the node's `id`:

| From                  | To                         | Evidence in the same write                 |
| --------------------- | -------------------------- | ------------------------------------------ |
| `candidate`           | `owned`, `dependency`      | required                                   |
| `owned`               | `dependency`, and back     | required                                   |
| `owned`, `dependency` | `candidate`                | not required                               |
| `candidate`           | `rejected`                 | required                                   |
| `owned`, `dependency` | `rejected`                 | refused; withdraw to `candidate` first     |
| `rejected`            | `owned`, `dependency`      | required                                   |
| `rejected`            | `candidate`                | refused                                    |
| any authorization     | `in_scope`, `out_of_scope` | required; refused on a `rejected` node     |
| any authorization     | `unknown`                  | not required; refused on a `rejected` node |
| `allowlist_scoped`    | set or cleared             | required; the node must be `in_scope`      |

A write, or an evidence deletion, that would leave a node holding a claim
(`owned`, `dependency`, `rejected`, `in_scope` or `out_of_scope`) with no link
to ready evidence is refused. A refusal from these rules starts with the item
address, such as `nodes[2]:`, and a refused transition names the current state.

A scoped child's effective authorization is the most restrictive value along its
scope chain. A child may narrow itself to `out_of_scope` with evidence, and
writing `unknown` removes that override. When the contract allows only listed
assets under a host, such as ports 80 and 443, mark the host `allowlist_scoped`.
Its scoped descendants are then `out_of_scope` until an ID write with evidence
widens a descendant, or an ancestor below the root, to `in_scope`, so a port
found later is never testable by default. An `in_scope` override is refused
under a root that is not allowlist-scoped:

```json
{
  "nodes": [
    {
      "id": "22222222-2222-4222-8222-222222222222",
      "authorization": "in_scope",
      "allowlist_scoped": true,
      "evidence_add": ["e_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]
    }
  ]
}
```

A candidate found to be neither the target's asset nor a dependency moves to
`rejected` by ID, with the evidence that shows why:

```json
{
  "nodes": [
    {
      "id": "33333333-3333-4333-8333-333333333333",
      "ownership": "rejected",
      "evidence_add": ["e_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"]
    }
  ]
}
```

The node keeps its identity, the other properties its type requires, its
evidence links and its first and last seen. The server removes its other
properties and label, resets authorization to `unknown`, and purges its
relations and scoped descendants in a job whose ID the acknowledgment returns as
`rejection_job_id`. The same write may not touch those relations or
descendants. `CONFLICT: REJECTION_BLOCKED` refuses the rejection while an
`owned` or `dependency` node relies on the candidate through `cname_to`,
`dname_to`, `has_nameserver`, `has_mail_exchange`, `has_soa_primary`,
`has_srv_target`, `has_svcb_binding`, `resolves_to`, `hosted_on`,
`backed_by_bucket` or `federates_with`; the details name that relation, and
such a candidate is a dependency.

A rejected record blocks its own re-creation. A write that matches a rejected
identity, or that creates a `subdomain` under a rejected registrable `domain`,
is refused whole with `CONFLICT: REJECTED_IDENTITY`. Nothing is written, and
the `rejected_items` details list every offending `nodes[i]` with the
`rejected_record` it hit. Drop those items, the
relations that point at them and the scoped children created under them, then
resend the rest of the batch.

To see the rejected names before writing, list the rejected records:

```json
{"kind":"nodes","ownership":"rejected","limit":100}
```

Search summaries carry no properties and a rejected record has no label, so read
the returned IDs with `kb_get` and `kind: "nodes"`: each rejected record keeps
its identity properties.

Deleting the rejected record with `kb_delete` and `cascade: true` is an explicit
purge that lifts the block.

Reads show effective state. `kb_get` node records carry `ownership`,
`authorization`, `first_seen` and `last_seen`, with `allowlist_scoped` on a root
and `state_root_id` on a scoped node. `kb_search` filters nodes on effective
`ownership` and `authorization` and leaves `rejected` records out unless the
filter asks for them. `kb_neighbors` nodes carry their effective state too.

## Earlier catalog and schema versions

Catalog v5 and schema version 4 are a clean cut. A workspace created with
catalog v1, v2, v3 or v4, or before schema version 4, is rejected at startup; it
is not migrated or opened read-only. Create a new workspace for this version.

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
fields. Property `null` remains literal JSON data, except that a property the
catalog declares rejects it; remove that property with `remove_properties`.

## First and last seen

`observed_at` on a write is the time its facts were observed, and it defaults
to the time of the write. Every node and relation reports `first_seen` and
`last_seen`, the earliest and latest observation any write has reported, and an
older observation never moves either one backward. A write is an observation
when it creates the record, matches it by identity (a rescan), supplies
`observed_at`, or sends `properties` or `remove_properties`. A write by ID that
only changes state, label, source or evidence links is not. An `observed_at`
more than five minutes past the server's clock is refused. A write observed before the record's `last_seen` may lower
`first_seen` and adds the properties the record lacks; it neither overwrites nor
removes a stored value. `kb_search` bounds graph records with `first_seen_min`,
`first_seen_max`, `last_seen_min` and `last_seen_max`.

All writes validate the merged full record and every check its type declares,
including a relation's endpoint value checks, before one transaction commits. One invalid record or missing evidence link
rejects the entire batch.
