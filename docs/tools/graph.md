# Graph and search tools

## `kb_types`

**Input:** `kind` is `nodes` or `relations`; optional `type`; `limit` defaults to
20 (1–100); optional cursor.

**Output:** catalog entries with required property schemas, formats and enums,
the `checks` and `canonicalize` ids each type runs with their rules in
`check_descriptions`, a `description` of what the type models and excludes and
what each property means, and ready-only counts. The page also carries the
shared `common` limits, every format with its behavior `version` and
`description`, `counts_deferred`, and `next_cursor`. Each entry has an `identity` object with a
`properties` array. Parent-scoped node types also include `scope`, for example
`{"relation":"has_open_port","endpoint":"source"}` for `port`. A pending
high-degree delete can defer counts rather than block discovery.

**Errors:** `INVALID` for a bad kind/type/page/cursor; `LIMIT` or `BUSY` for
bounded admission. Listing types is discovery and does not replace `kb_status`.

The same contract is tabulated on
[Catalog types and formats](../reference/catalog.md): every node and relation
type with its identity, parent scope, required properties and allowed endpoints,
plus the format rules, the checks, the canonicalizations and a node-to-relation matrix. Read
this page for the conventions and the reasoning; read that one to look a type up.
[ASM and OSINT coverage](../reference/asm-coverage.md) shows, field by field, which
property each supported tool's output belongs in, and
[Catalog v3 design review](../reference/catalog-review.md) records why each type
is shaped as it is.

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

The catalog is the only place a type, its required properties, its identity, its
parent scope and its allowed relation endpoint **types** are declared. `kb_types`
returns that declaration, including the format rule behind every required
property and the id of every check and canonicalization a type runs, so an
agent can read the contract instead of guessing it. A few relations also check
endpoint **values**: `has_subdomain` requires the target to end in the source,
`contains_ip` and `contains_cidr` require real containment, and
`has_registration` requires the registration's zone to be its domain's. A type may also
declare `optional` properties: each is validated whenever it is present, and it
may be absent but never null. Properties outside both maps are accepted as
submitted and are not validated.

`CONFLICT` also reports attempts to change an existing record's type, required
identity fields, or relation endpoints, which are immutable. It also reports an
identity hash collision or inconsistent stored scope, including multiple or
incomplete parent relations. Re-parenting a scoped child is invalid. Creating a
new `port`, `service`, `finding`, `dkim_record`, `mta_sts_policy`, `parameter`,
or `whois_registration` without exactly one same-request scope relation is also
invalid.

`service.properties.name` must be a member of the bundled, versioned
Nmap-derived service-name registry; the server does not normalize an arbitrary
Nmap label during `kb_write`. Names whose registry entry is TLS-capable, such as
`http`, require a strict boolean `secure` property. For other service names,
`secure` is optional, but it is a declared property and must be a boolean when
present.

Object patches merge recursively, arrays replace whole values, and `{}` leaves
existing object children. Required identity cannot change. Explicit `null`
clears label/source but is literal data inside properties, except that a declared
property rejects it; remove one with `remove_properties`. A supplied
`observed_at` is last-writer-wins rather than maximum timestamp.

Removing `/a` while setting `{"a": {}}` conflicts because the set recreates the
removed object. Removing `/a/x` while setting `{"a": {"y": 1}}` is valid: it
removes one child and merges a different child. Known catalog and mutation
errors identify the field and rule without returning submitted values.

Deleting `has_open_port`, `has_service`, `has_finding`, `has_dkim_selector`,
`has_mta_sts_policy`, `has_parameter`, or `has_registration`, or deleting its parent node, returns
`CONFLICT` while the scoped child still exists. Delete the child first with `cascade: true`; the
cascade removes its incident scope relation.

Several attribute names and attachment points are conventions the catalog does
not validate; agents must still follow them, since documentation is the only
enforcement:

`endpoint` has no dedicated HTTP-observation node. Record scan results directly
on the `endpoint` node using `status`, `title`, `content_length`, `webserver`,
and `content_type`, so agents converge on one spelling instead of forking
equivalent facts under different keys. These five are declared optional
properties, validated whenever present: `content_type` is the lowercase media
type without parameters, so strip `; charset=...` before writing. Response digests are the exception: they
are pivots rather than descriptions, so they live on `http_fingerprint` nodes
reached through `has_http_fingerprint`, not as `body_sha256` and `header_sha256`
attributes.

A `dmarc_record` lives at `_dmarc.<domain>` on the wire, but `has_dmarc` attaches
it to the `domain` or `subdomain` node itself, matching `has_spf`. Do not create
a `_dmarc.example.com` subdomain node to hold it.

`txt_record` and `dkim_record` values must be normalized before writing: strip
DNS presentation-form quoting, decode escapes, concatenate a multi-string
RRset's character-strings into one value, and remove surrounding whitespace.
A value is routed to its dedicated type exactly when that type accepts it, so
no spelling is refused by both `txt_record` and its dedicated type. `v=spf1 -all`
is an `spf_record`; `V=SPF1 -all`, `v=spf1include:_spf.google.com ~all` and a
leading-space spelling are `txt_record`s, because `spf_record` requires the
version tag to be the whole value or to be followed by a space. `v=DMARC1` and
`v=STSv1` accept a semicolon as well as a space, so `v=DMARC1;p=none` is a
`dmarc_record`. `dkim_record` validates its value as
generic TXT text, so every `v=DKIM1` spelling belongs to it. A malformed tag is
a real misconfiguration: record it as a `txt_record` and report the defect as a
`finding`. Never write ephemeral
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

A `caa_issue` or `caa_issuewild` parameter `name` is lower-cased the same way:
the tag is case-insensitive on the wire, while the parameter list is
identity-bearing, so `accountURI` and `accounturi` would otherwise describe one
fact as two edges. The parameter `value` is a URI or a method name and stays
exactly as submitted. A tag outside ASCII is left alone and rejected, as before.

These are the two places the server rewrites a submitted value. Everything else
is stored as submitted or rejected, and `coercion` stays false: no JSON type is
converted, only these declared spellings are canonicalized.

A workspace written before this rule may hold both spellings as two edges, and
nothing merges them. No repair procedure is needed, because such a workspace
predates schema version 3 and is refused at open, so no forked pair reaches a
readable workspace. Within a workspace this release created, the fold happens on
every write, so the fork cannot form.

A versioned CPE (`cpe:2.3:a:f5:nginx:1.18.0:*:...`) belongs on the
`runs_technology` edge beside `version`, since the version is per-host. The
`technology` node, shared by every host, accepts only a product-level CPE whose
version attribute is `*` or `-`, and rejects a versioned one. Both `cpe`
properties and the edge's `version` are declared and validated: a CPE is the
lowercase 2.3 formatted string, so convert the `cpe:/a:...` URI binding nmap
prints. Omit an unknown key rather than sending an empty string.

A `dkim_record` is identified by its selector and its parent domain, not by its
key. Writing the same selector again patches the stored `value` in place, so a
rotated or hijacked answer overwrites the key material previously recorded for
that selector. This keeps the graph a current-state view, one node per selector
rather than one per rotation; the superseded key survives only in whatever
evidence the earlier write attached. Attach evidence to every `dkim_record`
write that matters.

`has_svcb_binding` records an RFC 9460 HTTPS or SVCB record. Write it only
after parsing the record's SvcParams: `alpn: []` asserts that the record carries
no ALPN parameter, and never that the writer did not look. A writer that cannot
parse them must not write the edge at all, because `alpn` is part of the
identity and an under-parsed record becomes a second edge beside the correct
one rather than an obvious error. A ServiceMode record whose TargetName is `.`
targets the owner name itself and is written as a self edge; an AliasMode
record (`priority: 0`) whose TargetName is `.` is the wire's negative record and
must not be written at all.

`issued_by` points from a certificate to its issuer's certificate. A self edge
is how the catalog records a self-signed certificate, and it is the only
structural form of that fact. Reading it back costs a `search` for the type
followed by a `get`, since relation search returns no endpoints and this edge
carries no filterable property; keep a `self_signed` attribute on the
certificate alongside the edge rather than treating the edge as a replacement.
Absence of the edge means the issuer was never written, not that the chain ends.

`covers_name` now requires `coverage`, which is part of its identity. A
wildcard SAN cannot be written as a name: `*.example.com` fails `dns_name`.
Write the wildcard's base name with `coverage: "wildcard"`, and a name the
certificate lists literally with `coverage: "exact"`. A certificate that carries
both `example.com` and `*.example.com` therefore produces two edges to one
node instead of one edge that silently loses half the fact.

A `tls_fingerprint` is a clustering pivot, not an identifier. Every host behind
one load balancer or CDN presents the same JARM, so the node answers "what else
runs this TLS stack" and never "which host is this". JARM is 62 characters and
JA3S is 32; the declared `kind` fixes the length, and the two must not be
written under one another's name.

`operated_by` points an `asn` or an `ip_cidr` at the organization that holds
it, keyed on the registry and the registry's own handle. A handle is unique
within one RIR and never across them, so both properties are identity. Free-text
organization names are not: "Google LLC", "Google Inc." and "Google" are the
same holder, so `name` is a declared optional property a rescan patches in
place. It is not required, because an RDAP entity's name can be redacted while
the handle remains. The `asn` and `ip_cidr` nodes declare the registry facts
that describe the number resource itself: `name` or `netname`, the two-letter
`country`, and the `rir` that assigned it. `domain` is not a source here: domain registration is expressed by a
`whois_registration`, and a registrant organization has no RIR handle to key on.
A handle is case-sensitive and is written exactly as the registry publishes it:
RIPE and AFRINIC derive handles from the organisation name and keep its case, so
`ORG-nG51-RIPE` is the handle and `ORG-NG51-RIPE` is a different string that the
registry does not publish. Never uppercase one. An abuse contact is an
`email_address` or `phone` reached through `has_contact` with `role: "abuse"`,
not an attribute on this node; treat it as low-confidence either way, because
the registries themselves state the value is frequently wrong or absent.

A `finding` is keyed on `rule` and `matcher` under its parent, the way nuclei
deduplicates by template, matcher and target. `rule` is the reporting tool and
its own rule id, such as `nuclei:http-missing-security-headers`; `matcher`
separates the results one rule reports on one parent, such as a nuclei matcher
name, or is `""` when the rule has one result. `title` and `severity` are
required but not identity, so a template renamed between releases patches the
title instead of forking the finding. Two results of one rule that both send
`matcher: ""` on one parent are one finding: pick a discriminator per source. The
parent is fixed per source as well: a nuclei HTTP result hangs off the
`endpoint` it matched, a network or TLS result off the `port`, a DNS result off
the name. For a TLS result on a named host, prefix the matcher with the host
(`www.example.com:expired-ssl`), so two virtual hosts behind one address stay
two findings. `unknown` severity means none was assigned; it is not below `info`.

`has_weakness` classifies a `finding` or a `cve` as an instance of a CWE
weakness class. Both sources are real: a scanner assigns the class to its own
finding, and the NVD assigns it to a published CVE. A `finding` title is free
text, so two scanners reporting the same reflected XSS produce two unjoinable
titles; the CWE id is the canonical spelling that joins them. Write it as MITRE
publishes it, `CWE-79`, not the lowercase `cwe-79` some tools emit. `name` is an
attribute, not required, because a template that carries a cwe-id often carries
no title for it.

`registered_through` accepts a `whois_registration` source only: the
sponsoring registrar is a fact of one registration, so after a drop and a
re-registration elsewhere each registration keeps its own registrar edge
instead of the name accumulating two. A `registrar` is keyed
on its IANA id, which survives the renames and acquisitions that make the name
unstable; `0` is rejected because it is what an agent emits for a missing field.
Many ccTLD responses carry no IANA id at all, and those domains simply get no
registrar node.

Registration dates, EPP status and DNSSEC state describe one registration of the
name, not the name itself, so they live on a `whois_registration` node reached
from the `domain` through `has_registration`. The node is keyed on the zone the
name is registered in (`registry`, the domain without its first label) and the
repository object id the registry assigns, published as the RDAP `handle` or the
WHOIS `Registry Domain ID`. A renewal keeps the id, so it patches the node in
place; a drop and re-registration gets a new id and therefore a new node, and the
earlier registration keeps its own dates. Many ccTLD registries publish no id,
redact it (RFC 9537 lists it in `redacted`), or publish a placeholder such as
`REDACTED`. Write no `whois_registration` then: keep the response as evidence
linked to the `domain`, and never invent a key from the name or a redaction
string, which `registry_domain_id_assigned.1` refuses. `whois_server` is the
registry's WHOIS server; the `Registrar WHOIS Server` line of a WHOIS response
describes the registrar and stays in evidence. The registrant, admin, tech and
billing contacts of a registration attach to it through `has_contact`, and a
`domain` or `subdomain` refuses those four roles; the name keeps the roles it
publishes itself, such as `abuse`, `security` and `published`.

`technology` and `tls_cipher_suite` are workspace-global shared-vocabulary
nodes referenced by every host that matches. Neither is a `has_finding` source:
attaching a host-specific finding to either would appear to apply to every host
in the workspace that shares the node.

`http_fingerprint` is the response-side twin of `tls_fingerprint`: a clustering
pivot, never an identifier. `favicon_mmh3` is the Shodan/FOFA spelling, a
MurmurHash3 32-bit **signed** hash of the **base64 encoding** of the icon bytes,
not of the raw bytes, written in decimal as a string because the same property
also carries the 64-character hex digests of `body_sha256` and `header_sha256`.
A shared favicon hash means "same default icon" at least as often as "same
organization", and a body digest changes on every deploy, so neither is evidence
of ownership on its own.

`host_key` records the SSH host key a service presents, keyed on the key type
and the SHA-256 of the raw public key blob written as 64 lowercase hex
characters. OpenSSH prints that digest base64 after the `SHA256:` prefix, so
convert it rather than storing the printed form. Two hosts presenting one key
are a cloned image or one machine behind two addresses, which makes this the
strongest host-correlation pivot in the catalog; write the edge from each
`service` and let the single shared node do the joining.

`storage_bucket` is keyed on the provider and the provider-global bucket name,
and the enum admits only providers whose namespace really is global. DigitalOcean
Spaces is not one: a Spaces name is unique per region, so two regions can hold
the same name and one node would be two buckets. The required map is per type,
not per provider, so a region cannot be required for one member alone; that
provider waits for a shape that can express it.
Each provider's naming rules are enforced against the declared provider, so an
Azure name is checked as 3 to 24 lowercase alphanumerics and an S3 name is
checked for the reserved prefixes and suffixes AWS refuses. For `azure_blob` the
name is the **storage account**, because a container is not globally unique; a
public container listing is an `endpoint`, reached the same way any other URL is.
`backed_by_bucket` says a name or a URL serves content from that bucket: write it
from the `subdomain` whose CNAME points at the bucket host, or from the
`endpoint` whose response came out of it. A bucket found only by mutating an
organization's name and probing for it has no such edge; write the node and
attach the evidence that found it.

`cloud_resource` records a provider-managed resource by the default hostname the
provider assigned to it, and `cloud_account` records the AWS account, GCP project
or Azure subscription that holds it, joined by `in_account`. Each accepted
hostname names exactly one resource at a time, which is why it can be the
identity: a takeover re-registers the same hostname, so the node stays and its
`in_account` edge moves. The same hostname is usually a `subdomain` too; that node
answers DNS questions, and `hosted_on` from it, or from a custom name that
CNAMEs to it, reaches the resource. `service` is fixed by which pattern matches,
and `region`, when sent, must be the region the hostname encodes.

| Service                 | Canonical default hostname                                                              | Source                                                                                                                                  |
| ----------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `aws_cloudfront`        | `d<13 alnum>.cloudfront.net`                                                            | [CloudFront domain names](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/CNAMEs.html)                               |
| `aws_api_gateway`       | `<10 alnum>.execute-api.<region>.amazonaws.com`                                         | [Invoking a REST API](https://docs.aws.amazon.com/apigateway/latest/developerguide/how-to-call-api.html)                                |
| `aws_lambda_url`        | `<32 alnum>.lambda-url.<region>.on.aws`                                                 | [Lambda function URLs](https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html)                                            |
| `aws_elastic_beanstalk` | `<cname>.<region>.elasticbeanstalk.com`, legacy `<cname>.elasticbeanstalk.com`          | [Beanstalk domain names](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/customdomains.html)                                     |
| `aws_elb`               | `<name>-<id>.<region>.elb.amazonaws.com`, `<name>-<id>.elb.<region>.amazonaws.com`      | [Load balancer DNS names](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/application-load-balancers.html#dns-name) |
| `azure_app_service`     | `<app>.azurewebsites.net`, `<app>-<16 alnum>.<region>-NN.azurewebsites.net`             | [Unique default hostnames](https://learn.microsoft.com/en-us/azure/app-service/reference-dangling-subdomain-prevention)                 |
| `azure_cloud_service`   | `<name>.cloudapp.net`                                                                   | [Subdomain takeover](https://learn.microsoft.com/en-us/azure/security/fundamentals/subdomain-takeover)                                  |
| `azure_public_ip`       | `<label>.<region>.cloudapp.azure.com`, `<label>.<16 alnum>.<region>.cloudapp.azure.com` | [Public IP addresses](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/public-ip-addresses)                          |
| `azure_traffic_manager` | `<name>.trafficmanager.net`                                                             | [Subdomain takeover](https://learn.microsoft.com/en-us/azure/security/fundamentals/subdomain-takeover)                                  |
| `azure_front_door`      | `<name>.azurefd.net`, `<name>-<16 alnum>.<zone>.azurefd.net` (`z01`, `b01`)             | [Front Door endpoints](https://learn.microsoft.com/en-us/azure/frontdoor/endpoint)                                                      |
| `azure_api_management`  | `<name>.azure-api.net`                                                                  | [Subdomain takeover](https://learn.microsoft.com/en-us/azure/security/fundamentals/subdomain-takeover)                                  |
| `gcp_app_engine`        | `<project>.appspot.com`                                                                 | [How requests are routed](https://cloud.google.com/appengine/docs/standard/how-requests-are-routed)                                     |
| `gcp_firebase_hosting`  | `<site>.web.app`                                                                        | [Firebase multisite hosting](https://firebase.google.com/docs/hosting/multisites)                                                       |
| `gcp_firebase_rtdb`     | `<db>.firebaseio.com`, `<db>.<region>.firebasedatabase.app`                             | [Realtime Database locations](https://firebase.google.com/docs/database/locations)                                                      |

The generated-id lengths are the shapes the providers' own examples show; none
of them publishes a formal grammar, so an id outside that shape is rejected
rather than guessed at. Alias hosts are refused with a pointer to the canonical
form: the ELB `dualstack.` prefix, App Service and API Management `scm`,
`developer` and `management` hosts, App Engine `<project>.<code>.r.appspot.com`
and `-dot-` routes, and Firebase's `<site>.firebaseapp.com`. Write each of them as
a `subdomain` with `hosted_on`. Cloud Run and Cloud Functions are not modeled:
a Cloud Run service has two default URLs with no published mapping between them,
and a `cloudfunctions.net` host names a project and region, not a function.

`identity_tenant` records the identity provider a domain or subdomain federates
with, which `federates_with` attaches to that name. Every provider in the enum
has a cross-field rule that fixes one canonical spelling: `entra_id` a lowercase
UUID, `okta` the bare organization slug rather than `example.okta.com`. The enum
stops there on purpose. A Google Workspace customer id is uppercase-initial on
the wire and agents substitute the primary domain when they cannot read it, and
an Auth0 tenant name is unique per region, so neither has one spelling an agent
would reliably reproduce. The federation kind that `getuserrealm` reports belongs
on the edge as the declared optional `namespace_type` property (`managed`,
`federated`), because it changes without the pairing changing.

`repository` is keyed on the instance host, the owner and the name, all
lowercase: hosting platforms resolve names case-insensitively, so `Example/Web`
and `example/web` are one repository and must not become two nodes. The host is
in the identity because self-hosted GitLab, Gitea and GitHub Enterprise are a
routine outcome of this very enumeration, and `git.example.com/acme/web` is not
`gitlab.com/acme/web`. `platform` is required but is not identity: it names the
software, which selects the owner grammar, and only `gitlab` accepts a `/` in the
owner for nested groups. `owns_repository` from a `domain` or `subdomain` is an
attribution claim, usually made because the organization's name matches; attach
the evidence that supports it.

`secret` records an exposed credential by digest. Its identity is the SHA-256 of
the secret value alone, so the same key leaked in a repository and in a
JavaScript bundle is one node with two `exposes_secret` edges. The detector is
not identity: trufflehog calls a key `aws` where gitleaks calls it
`aws-access-token`, and a scanner renaming its own rule would fork the node.
Keep it as a `detector` attribute. Hash the credential exactly as the provider
issues it, with no surrounding quotes, assignment prefix or trailing newline,
or two observations of one key produce two digests.

Three rules follow from the digest, and only the first is enforced. The server
rejects a `secret` node or an `exposes_secret` edge that carries, at any depth
and in any case, a key named `value`, `secret`, `plaintext`, `password`,
`token`, `key`, `credential`, `match`, `raw`, `rawv2`, `redacted` or `line`,
because an additional property is stored, property-indexed and full-text
searchable, which would make a leaked plaintext searchable in the graph. Beyond
that: compute the digest from the scanner output, then replace every
secret-bearing field (trufflehog `Raw`, `RawV2` and `Redacted`, gitleaks
`Secret`, `Match` and `Line`) with `[REDACTED]` before ingesting the output as
evidence. And treat `value_sha256` itself as sensitive: an unsalted
single-round digest of a human-chosen password is reversible by dictionary, so
the store as a whole is classified at the level of the credentials it indexes.
Location is required on the `exposes_secret` edge and is part of its identity,
so one key at five paths is five edges rather than one edge overwritten four
times. The declared optional `kind`, `detector`, `verified` and `key_id` type
the credential; always send `kind` when the source names it.

`email_address` and `phone` are shared contact nodes reached through
`has_contact`, whose required `role` is part of the edge identity, so one
organization can publish one address as both `abuse` and `security`. Use
`published` for an address harvested from an organization's own surface with no
declared role. The whole local part is required lowercase: a mailbox is
case-sensitive on the wire, but tools emit inconsistent case and one mailbox has
to stay one node. `has_contact` replaces the `abuse_contact` attribute the
`organization` node used to carry; the registries' own warning still applies, so
treat an `abuse` role as low-confidence.

A CAA `iodef` property names where certificate authorities report policy
violations. Record it as `has_contact` with `role: "iodef"`: a `mailto:` address
targets its `email_address`, and an `https:` URL targets an `endpoint` with
`method: "POST"`, because RFC 6546 delivers IODEF reports by HTTPS POST. An
`endpoint` is a contact target for `iodef` only, and `iodef` never targets a
`phone`.

`authenticates` points a `secret` at what it logs in to, keyed on the login name
and the breach it was published in, each `""` when the source gives none. So one
password leaked for `alice` in two breaches is two edges, and a scanner hit with
no breach is a third. The breach is a lowercase `<corpus>:<id>` token such as
`hibp:linkedin`; slug a display name by lowercasing it and replacing each run of
other characters with one `-`, and keep the display name in `breach_title`. The
token joins spellings within one corpus only: HIBP `linkedin` and LeakCheck
`linkedin.com` stay two tokens, since nothing maps one corpus's ids to another's.

`links_to` records that a crawled page references another URL, keyed on the HTML
element that holds the reference, so a script include and an anchor to one URL
are two edges. `attribute` (`href`, `src`) is last-writer-wins. Use `element: ""`
when the crawler reports no element.

`mta_sts_policy` holds the TXT record published at `_mta-sts.<domain>`, and it
is parent-scoped through `has_mta_sts_policy`, unlike `spf_record` and
`dmarc_record`. The difference is that an SPF or DMARC value *is* the whole
fact, so two domains publishing the same string really do share one record,
while an MTA-STS TXT value is only a version pointer: providers template it, and
a date-only id such as `v=STSv1; id=20190429T010101;` is published verbatim by
many unrelated tenants. Unscoped, those tenants would collapse onto one node,
and the `mode`, `max_age` and `mx` properties that each of them reads from its
own `https://mta-sts.<domain>/.well-known/mta-sts.txt` would overwrite each
other. Those three are declared and validated; write `mx` sorted and without
duplicates. `txt_record` rejects a well-formed `v=STSv1` value for the same reason it
rejects a well-formed `v=spf1` value.

`has_finding` gained `parameter`, `dkim_record`, `mta_sts_policy`,
`storage_bucket`, `repository`, `identity_tenant` and `secret` as sources. A
finding names one real object, and each of those is one: a bucket, a repository
and a tenant have exactly one owner, a scoped record belongs to its parent
domain, and a secret digest is one credential.

It deliberately did not gain `technology`, `tls_cipher_suite`,
`tls_fingerprint`, `http_fingerprint`, `host_key`, `spf_record`, `dmarc_record`,
`txt_record`, `email_address`, `phone`, `cve` or `cwe`. Those are shared
vocabulary or coincidence values: unrelated hosts legitimately share a
technology slug, a stack fingerprint, or an SPF string thousands of domains
publish verbatim, so a host-specific finding hung there would read as applying
to all of them. A defect in an SPF record is a finding on the `domain` that
publishes it, not on the record node. `host_key` is the borderline case and
stays out, because a finding about a host key is nearly always a finding about
the host presenting it.

`protected_by` now also accepts `domain` and `subdomain`, because `dnsx` and
`cdncheck` identify a CDN or WAF from DNS alone, before any port is probed.
`runs_technology` accepts them too, which is how a name gets a vendor when
nothing is listening: a dangling CNAME at a SaaS host is the single most common
actionable DNS-stage result, and `protected_by` cannot express it because none
of its `kind` values describes SaaS hosting. `affected_by` now also accepts
`endpoint`, which is where a nuclei CVE template matches.

`kb_types` pages at `limit`, which defaults to 20. Both the node and the
relation catalogs are now larger than that, so a single default call returns a
partial list plus a `next_cursor`. Follow the cursor, or raise `limit`, before
concluding that a type does not exist.

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
