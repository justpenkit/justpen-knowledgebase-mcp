# Graph and search tools

## `kb_types`

**Input:** `kind` is `nodes` or `relations`; optional `type`; `limit` defaults to
20 (1–100); optional cursor.

**Output:** catalog entries with required property schemas,
formats/enums/cross-field rules, and ready-only counts, plus common formats,
`counts_deferred`, and `next_cursor`. Each entry has an `identity` object with a
`properties` array. Parent-scoped node types also include `scope`, for example
`{"relation":"has_open_port","endpoint":"source"}` for `port`. A pending
high-degree delete can defer counts rather than block discovery.

**Errors:** `INVALID` for a bad kind/type/page/cursor; `LIMIT` or `BUSY` for
bounded admission. Listing types is discovery and does not replace `kb_status`.

## `kb_write`

**Input:** 1–100 total `nodes` plus `relations`, and at most 100 total
`evidence_add`/`evidence_remove` mutations. Creation supplies `type` and
`properties`; patch supplies `id`. Relations also supply immutable
`source_ref`/`target_ref`, each exactly one `{id}` or same-batch `{node_index}`.

**Output:** ordered node/relation acknowledgments with UUID, created/updated
flags, link counts, and property-index coverage.

**Errors:** `INVALID` for strict catalog, merge, pointer, endpoint, or batch-rule
failure; `NOT_FOUND` for an atomic missing reference/evidence set;
`CONFLICT`/`RECORD_DELETING` for immutable or pending records; `BUSY`, `LIMIT`,
or storage errors. No partial batch commits.

`CONFLICT` also reports attempts to change an existing record's type, required
identity fields, or relation endpoints, which are immutable. It also reports an
identity hash collision or inconsistent stored scope, including multiple or
incomplete parent relations. Re-parenting a scoped child is invalid. Creating a
new `port`, `service`, `finding`, `dkim_record`, or `parameter` without exactly
one same-request scope relation is also invalid.

`service.properties.name` must be a member of the bundled, versioned
Nmap-derived service-name registry; the server does not normalize an arbitrary
Nmap label during `kb_write`. Names whose registry entry is TLS-capable, such as
`http`, require a strict boolean `secure` property. For other service names,
`secure` is an optional additional property.

Object patches merge recursively, arrays replace whole values, and `{}` leaves
existing object children. Required identity cannot change. Explicit `null`
clears label/source but is literal data inside properties. A supplied
`observed_at` is last-writer-wins rather than maximum timestamp.

Removing `/a` while setting `{"a": {}}` conflicts because the set recreates the
removed object. Removing `/a/x` while setting `{"a": {"y": 1}}` is valid: it
removes one child and merges a different child. Known catalog and mutation
errors identify the field and rule without returning submitted values.

Deleting `has_open_port`, `has_service`, `has_finding`, `has_dkim_selector`, or
`has_parameter`, or deleting its parent node, returns `CONFLICT` while the
scoped child still exists. Delete the child first with `cascade: true`; the
cascade removes its incident scope relation.

Several attribute names and attachment points are conventions the catalog does
not validate; agents must still follow them, since documentation is the only
enforcement:

`endpoint` has no dedicated HTTP-observation node. Record scan results directly
on the `endpoint` node using `status`, `title`, `content_length`, `body_sha256`,
`header_sha256`, `webserver`, and `content_type`, so agents converge on one
spelling instead of forking equivalent facts under different keys.

A `dmarc_record` lives at `_dmarc.<domain>` on the wire, but `has_dmarc` attaches
it to the `domain` or `subdomain` node itself, matching `has_spf`. Do not create
a `_dmarc.example.com` subdomain node to hold it.

`txt_record` and `dkim_record` values must be normalized before writing: strip
DNS presentation-form quoting, decode escapes, concatenate a multi-string
RRset's character-strings into one value, and remove surrounding whitespace.
Version tags are matched exactly, so `v=spf1` is routed to `spf_record` while
`V=SPF1` and a leading-space spelling are accepted as a generic `txt_record`.
That is deliberate: a case-variant tag is a real misconfiguration, the
dedicated types reject it, and refusing it here too would leave it no home.
Record it as a `txt_record` and report the defect as a `finding`. Never write ephemeral
`_acme-challenge` DNS-01 challenge values as `txt_record`s; each certificate
issuance rotates the nonce, so recording them accumulates one node per renewal
with no supersession.

A submitted `endpoint` URL may carry a query string, but the server validates
it and then removes it: `https://example.com/search?q=1` is stored as
`https://example.com/search`, and identity is computed from the stored
spelling. A crawler that observes `?q=1` and `?q=2` therefore writes one
endpoint, not one per value. Record the parameter names themselves as
`parameter` nodes attached with `has_parameter`; a value seen during a scan is
sample data and belongs in evidence or an attribute, not in an identity.

This is the one place the server rewrites a submitted value. Everything else is
stored as submitted or rejected, and `coercion` stays false: no JSON type is
converted, only this declared spelling is canonicalized.

No required map references the `cpe23_or_empty` rule, so a stored `cpe` is
never validated against it. Produce the spelling the rule describes and
re-validate it on read rather than trusting the stored bytes.

A versioned `technology.cpe` (`cpe:2.3:a:f5:nginx:1.18.0:*:...`) belongs on the
`runs_technology` edge beside `version`, since the version is per-host. An
unversioned product CPE may sit on the `technology` node itself. Omit the key
rather than sending an empty string; a later write that sends the key overwrites
the stored value.

A `dkim_record` is identified by its selector and its parent domain, not by its
key. Writing the same selector again patches the stored `value` in place, so a
rotated or hijacked answer overwrites the key material previously recorded for
that selector. This keeps the graph a current-state view, one node per selector
rather than one per rotation; the superseded key survives only in whatever
evidence the earlier write attached. Attach evidence to every `dkim_record`
write that matters.

`technology` and `tls_cipher_suite` are workspace-global shared-vocabulary
nodes referenced by every host that matches. Neither is a `has_finding` source:
attaching a host-specific finding to either would appear to apply to every host
in the workspace that shares the node.

## `kb_get`

**Input:** `kind`, 1–100 IDs, `view` (`record` default, `links`, or `sources`),
`limit` 20 by default, and optional cursor. Association views take one owner;
`sources` is evidence-only; record view rejects a cursor.

**Output:** record view returns canonical records, `missing_ids`, and
`remaining_ids`. Links/sources return a page plus `next_cursor`. Evidence records
include lifecycle/index state; pending lifecycle remains inspectable.

**Errors:** `INVALID` for mismatched IDs/view/cursor; `RECORD_DELETING` for a
pending links/source page; `LIMIT`, `BUSY`, or storage errors. Missing record-view
IDs are data, not an all-or-nothing error.

## `kb_search`

**Input:** required `kind`; optional literal/words `query`; `sort` defaults to
`id`; graph type/key/source/endpoint/timestamp/property filters; evidence
media/index/size/created/source filters; `limit` defaults to 20. Graph search
defaults `include_evidence` to true. Omit that field entirely for `kind: "evidence"`. Relevance requires a query and rejects a cursor.

**Output:** bounded summaries and exact match references, `cursor`, `has_more`,
property filter mode/scan count, and live evidence index coverage. Relevance
returns the top 100 at most. Full properties/raw bytes require `kb_get` or
`kb_read_evidence`.

**Errors:** `INVALID` for kind-inapplicable fields, invalid cursor, query token/
byte limits, predicate AST (maximum 32 leaves and four group levels), or strict
timestamps; `LIMIT` when verification/response budgets expire; `BUSY` or storage
errors.

Property JSON Pointer traversal follows the actual object/array container.
`ne` does not match missing paths; use `exists: false`. Index uncertainty invokes
canonical JSON fallback and is reported. See [Evidence, search, and deletion](../guides/evidence-search.md).

## `kb_neighbors`

**Input:** 1–1,000 unique node UUIDs in `seed_ids`; `direction` defaults to
`both`; optional relation types; `depth` defaults to 1 (maximum 3);
`max_nodes`/`max_edges` default to 100/300 and cap at 1,000/3,000.

**Output:** ready nodes and stored directed relations, plus `truncated`, a reason
(`max_nodes`, `max_edges`, `deadline`, or `response_bytes`), and the unexpanded
frontier. It never infers relations.

**Errors:** `INVALID` for duplicate/bad/over-budget seeds; `NOT_FOUND` for missing
seeds; `BUSY` or storage errors. Relations hidden by pending deletion can make
traversal temporarily disconnected.
