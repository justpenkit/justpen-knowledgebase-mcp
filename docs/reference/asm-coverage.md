# ASM and OSINT coverage

Every table below is generated from the coverage fixtures under `tests/fixtures/asm/`, one per source. Each fixture holds output derived from the tool's documented schema, a mapping that sends every emitted field to a catalog property, to evidence or to `non storable` with a reason, and the `kb_write` batches an agent sends for that output. The test suite holds the three to each other in both directions: an unmapped field, a mapped value that no write carries and a written value no field explains all fail. Regenerate the page with `make docs-catalog`.

A mapping is a recipe for agents, not an ingestion adapter: the server accepts whatever passes the [catalog](catalog.md), and these tables show which property each field belongs in.

## Secrets and redaction

Scanner-reported secret fields are replaced with `[REDACTED]` before the output is ingested as evidence. The secret's digest is computed first, from the unredacted value, and is the only form the graph keeps. These fields are redacted per source:

| Source        | Redacted fields                                                                                                                                                          |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bbot`        | `{type=FINDING,module=badsecrets}.data_json.description`, `{type=FINDING,module=nuclei}.data_json.description`, `{type=FINDING,module=trufflehog}.data_json.description` |
| `gitleaks`    | `Line`, `Match`, `Secret`                                                                                                                                                |
| `leak_corpus` | `result[].hashed_password`, `result[].password`                                                                                                                          |
| `nuclei`      | `curl-command`, `extracted-results`, `matched-at`, `request`, `response`                                                                                                 |
| `trufflehog`  | `Raw`, `RawV2`, `Redacted`, `SecretParts.*`                                                                                                                              |

Only these derivations may carry a value out of a redacted field, and each strips the secret:

| Source        | Field                                                    | Transforms                                                  | Sink                  |
| ------------- | -------------------------------------------------------- | ----------------------------------------------------------- | --------------------- |
| `bbot`        | `{type=FINDING,module=nuclei}.data_json.description`     | `bbot_nuclei_matcher`                                       | `finding.matcher`     |
| `bbot`        | `{type=FINDING,module=nuclei}.data_json.description`     | `bbot_nuclei_template`                                      | `finding.rule`        |
| `bbot`        | `{type=FINDING,module=nuclei}.data_json.description`     | `bbot_nuclei_template_id`                                   | `finding.title`       |
| `bbot`        | `{type=FINDING,module=nuclei}.data_json.description`     | `bbot_without_extracted_data`                               | `finding.description` |
| `bbot`        | `{type=FINDING,module=trufflehog}.data_json.description` | `bbot_trufflehog_secret_part`, `sha256_hex`, `hex_prefix16` | `finding.matcher`     |
| `gitleaks`    | `Secret`                                                 | `sha256_hex`                                                | `secret.value_sha256` |
| `leak_corpus` | `result[].hashed_password`                               | `sha256_hex`                                                | `secret.value_sha256` |
| `leak_corpus` | `result[].password`                                      | `sha256_hex`                                                | `secret.value_sha256` |
| `nuclei`      | `matched-at`                                             | `url_without_query`                                         | `endpoint.url`        |
| `nuclei`      | `request`                                                | `http_request_method`                                       | `endpoint.method`     |
| `trufflehog`  | `Raw`                                                    | `trufflehog_public_part`                                    | `secret.key_id`       |
| `trufflehog`  | `Raw`                                                    | `trufflehog_secret_part`, `sha256_hex`                      | `secret.value_sha256` |

Three residuals are accepted and stated rather than hidden. A secret carried in a URL path, such as a webhook token, stays in `endpoint.url`, because only the query is removed. Raw HTTP bodies and headers are evidence and are full-text indexed. A password digest is an unsalted SHA-256 of a guessable value, reversible by dictionary.

## Finding identity per source

A `finding` is keyed on `rule` and `matcher` under its parent, so the parent and the discriminator are fixed per source:

| Result                               | Parent                                                      | Matcher                                                               |
| ------------------------------------ | ----------------------------------------------------------- | --------------------------------------------------------------------- |
| nuclei `http`, `headless`            | `endpoint` of `matched-at`, query removed                   | `matcher-name`, else `extractor-name`                                 |
| nuclei `dns`                         | the `domain` or `subdomain` of `host`                       | `matcher-name`, else `extractor-name`                                 |
| nuclei `tcp` (network), `javascript` | `port` of `ip`:`port`                                       | `matcher-name`, else `extractor-name`                                 |
| nuclei `ssl`                         | `port` of `ip`:`port`                                       | `<host>:<matcher-name>` for a named host, so virtual hosts stay apart |
| nuclei DAST result                   | `parameter` named by `fuzzing_parameter` under the endpoint | `matcher-name`                                                        |
| nuclei `whois`                       | `domain` of `host`                                          | `matcher-name`                                                        |
| BBOT FINDING with a URL              | `endpoint` of the URL, query removed                        | the wrapped tool's sub-id, else `digest16(description)`               |
| BBOT FINDING without a URL           | the `domain`, `subdomain` or `ip_address` of `host`         | as above                                                              |
| BBOT trufflehog FINDING              | as above                                                    | the first 16 hex characters of the secret's `value_sha256`            |
| Manual finding                       | the node the analyst names                                  | chosen by the analyst, empty when the rule has one result             |

## Sources

### `asnmap`

- **Command:** `asnmap -i AS64496,198.51.100.7 -json -o output.jsonl`
- **Version:** asnmap, `main` branch at commit `cedef8b`
- **Files:** `output.jsonl`

| Field        | Sink                           | Transforms           | Notes                                                                                           |
| ------------ | ------------------------------ | -------------------- | ----------------------------------------------------------------------------------------------- |
| `timestamp`  | `observed_at` (write metadata) | `go_time_string_utc` | —                                                                                               |
| `input`      | evidence                       | —                    | the query the operator gave (an ASN, an IP or a name); the result fields carry what was learned |
| `as_number`  | `asn.value`                    | `asn_number`         | —                                                                                               |
| `as_name`    | `asn.name`                     | —                    | —                                                                                               |
| `as_country` | `asn.country`                  | —                    | —                                                                                               |
| `as_range`   | `ip_cidr.value`                | —                    | each member                                                                                     |
| `as_range`   | `ip_cidr.version`              | `ip_version`         | each member                                                                                     |

Constants a write carries that no field holds:

| Sink                      | Values     | Reason                            |
| ------------------------- | ---------- | --------------------------------- |
| `source` (write metadata) | `"asnmap"` | the tool that produced the output |

### `aws_sts_identity`

- **Command:** `aws sts get-caller-identity --output json`
- **Version:** AWS CLI v2, STS API version 2011-06-15
- **Files:** `caller-identity.json`

| Field     | Sink                       | Transforms    | Notes |
| --------- | -------------------------- | ------------- | ----- |
| `UserId`  | evidence                   | —             | —     |
| `Account` | `cloud_account.account_id` | —             | —     |
| `Arn`     | `cloud_account.account_id` | `arn_account` | —     |

Constants a write carries that no field holds:

| Sink                      | Values      | Reason                           |
| ------------------------- | ----------- | -------------------------------- |
| `cloud_account.provider`  | `"aws"`     | STS answers for an AWS account   |
| `source` (write metadata) | `"aws-sts"` | the API that produced the output |

### `bbot`

- **Command:** `bbot -t example.com example.org -p subdomain-enum spider -m portscan http nuclei trufflehog badsecrets -om json -y`
- **Version:** BBOT 3.0.2 (branch `stable`, commit `a6fb827`); its default `modules.trufflehog.only_verified: true`
- **Files:** `output.jsonl`

| Field                                                    | Sink                           | Transforms                                                  | Notes                                                                                                                                                   |
| -------------------------------------------------------- | ------------------------------ | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`                                                   | non storable                   | —                                                           | selects the mapping rows through the event qualifier                                                                                                    |
| `id`                                                     | non storable                   | —                                                           | BBOT's event id; the writer resolves `parent` against it                                                                                                |
| `uuid`                                                   | non storable                   | —                                                           | a per-run event uuid                                                                                                                                    |
| `scope_description`                                      | evidence                       | —                                                           | —                                                                                                                                                       |
| `netloc`                                                 | evidence                       | —                                                           | —                                                                                                                                                       |
| `host`                                                   | `subdomain.value`              | `strip_trailing_dot`                                        | only when `host_is_subdomain`                                                                                                                           |
| `host`                                                   | `domain.value`                 | `strip_trailing_dot`                                        | only when `host_is_domain`                                                                                                                              |
| `{type=DNS_NAME}.host`                                   | evidence                       | —                                                           | —                                                                                                                                                       |
| `resolved_hosts`                                         | evidence                       | —                                                           | —                                                                                                                                                       |
| `{type=OPEN_TCP_PORT}.resolved_hosts`                    | `ip_address.value`             | —                                                           | each member                                                                                                                                             |
| `{type=OPEN_TCP_PORT}.resolved_hosts`                    | `ip_address.version`           | `ip_version`                                                | each member                                                                                                                                             |
| `dns_children`                                           | evidence                       | —                                                           | —                                                                                                                                                       |
| `dns_children.*`                                         | evidence                       | —                                                           | —                                                                                                                                                       |
| `{type=DNS_NAME}.dns_children.A`                         | `ip_address.value`             | —                                                           | each member; only when `bbot_not_wildcard_placeholder`                                                                                                  |
| `{type=DNS_NAME}.dns_children.A`                         | `ip_address.version`           | `ip_version`                                                | each member; only when `bbot_not_wildcard_placeholder`                                                                                                  |
| `port`                                                   | evidence                       | —                                                           | —                                                                                                                                                       |
| `{type=OPEN_TCP_PORT}.port`                              | `port.number`                  | —                                                           | —                                                                                                                                                       |
| `web_spider_distance`                                    | non storable                   | —                                                           | crawl bookkeeping of this scan                                                                                                                          |
| `scope_distance`                                         | non storable                   | —                                                           | relative to this scan's targets                                                                                                                         |
| `scan`                                                   | non storable                   | —                                                           | the scan id                                                                                                                                             |
| `timestamp`                                              | `observed_at` (write metadata) | `epoch_utc`                                                 | —                                                                                                                                                       |
| `parent`                                                 | non storable                   | —                                                           | the parent event id; a URL whose parent is a URL gives the source of a links_to edge                                                                    |
| `parent_uuid`                                            | non storable                   | —                                                           | the parent event uuid                                                                                                                                   |
| `tags`                                                   | evidence                       | —                                                           | —                                                                                                                                                       |
| `{type=DNS_NAME}.tags`                                   | `domain.wildcard`              | `bbot_wildcard_zone`                                        | only when `bbot_names_domain`                                                                                                                           |
| `{type=DNS_NAME}.tags`                                   | `subdomain.wildcard`           | `bbot_wildcard_zone`                                        | only when `bbot_names_subdomain`                                                                                                                        |
| `{type=DNS_NAME}.tags`                                   | `subdomain.wildcard_answer`    | `bbot_wildcard_answer`                                      | only when `bbot_names_subdomain`                                                                                                                        |
| `{type=URL}.tags`                                        | `endpoint.status`              | `bbot_status_tag`                                           | —                                                                                                                                                       |
| `module`                                                 | evidence                       | —                                                           | —                                                                                                                                                       |
| `{type=FINDING,module=badsecrets}.module`                | `finding.rule`                 | `bbot_rule`                                                 | —                                                                                                                                                       |
| `{type=FINDING}.module`                                  | `finding.rule`                 | `bbot_rule`                                                 | documented, absent from the fixture                                                                                                                     |
| `module_sequence`                                        | evidence                       | —                                                           | —                                                                                                                                                       |
| `discovery_context`                                      | non storable                   | —                                                           | redacted before ingest; a trufflehog or badsecrets context quotes the secret it found, and every context is repeated in its descendants' discovery_path |
| `discovery_path`                                         | non storable                   | —                                                           | redacted before ingest; joins the discovery context of every ancestor, so it repeats any context that quotes a secret                                   |
| `parent_chain`                                           | non storable                   | —                                                           | event uuids of this scan                                                                                                                                |
| `host_metadata.*.cloud_providers.*.types`                | evidence                       | —                                                           | documented, absent from the fixture                                                                                                                     |
| `host_metadata.*.cloud_providers.*.match`                | evidence                       | —                                                           | documented, absent from the fixture                                                                                                                     |
| `{type=DNS_NAME}.data`                                   | `domain.value`                 | `bbot_dns_name`                                             | only when `bbot_names_domain`                                                                                                                           |
| `{type=DNS_NAME}.data`                                   | `subdomain.value`              | `bbot_dns_name`                                             | only when `bbot_names_subdomain`                                                                                                                        |
| `{type=OPEN_TCP_PORT}.data`                              | non storable                   | —                                                           | host:port; both are mapped from host and port                                                                                                           |
| `{type=URL}.data_json.url`                               | `endpoint.url`                 | `url_without_query`                                         | —                                                                                                                                                       |
| `{type=URL}.data_json.http_title`                        | `endpoint.title`               | —                                                           | —                                                                                                                                                       |
| `{type=URL}.data_json.redirect_location`                 | `endpoint.url`                 | `url_without_query`                                         | documented, absent from the fixture                                                                                                                     |
| `{type=URL}.data_json.hash.body_sha256`                  | `http_fingerprint.value`       | —                                                           | —                                                                                                                                                       |
| `{type=URL}.data_json.hash.header_sha256`                | `http_fingerprint.value`       | —                                                           | —                                                                                                                                                       |
| `{type=URL}.data_json.hash.body_md5`                     | non storable                   | —                                                           | the sha256 digests are the keyed fingerprints                                                                                                           |
| `{type=URL}.data_json.hash.header_md5`                   | non storable                   | —                                                           | the sha256 digests are the keyed fingerprints                                                                                                           |
| `{type=URL}.data_json.hash.body_mmh3`                    | non storable                   | —                                                           | a body hash, not a favicon hash; the sha256 digests are the keyed fingerprints                                                                          |
| `{type=URL}.data_json.hash.header_mmh3`                  | non storable                   | —                                                           | the sha256 digests are the keyed fingerprints                                                                                                           |
| `{type=FINDING}.data_json.host`                          | `subdomain.value`              | `strip_trailing_dot`                                        | only when `host_is_subdomain`                                                                                                                           |
| `{type=FINDING}.data_json.host`                          | `domain.value`                 | `strip_trailing_dot`                                        | only when `host_is_domain`                                                                                                                              |
| `{type=FINDING}.data_json.url`                           | `endpoint.url`                 | `url_without_query`                                         | —                                                                                                                                                       |
| `{type=FINDING}.data_json.name`                          | `finding.title`                | —                                                           | documented, absent from the fixture                                                                                                                     |
| `{type=FINDING,module=nuclei}.data_json.name`            | evidence                       | —                                                           | —                                                                                                                                                       |
| `{type=FINDING}.data_json.severity`                      | `finding.severity`             | `lower`                                                     | —                                                                                                                                                       |
| `{type=FINDING}.data_json.confidence`                    | `finding.confidence`           | `lower`                                                     | —                                                                                                                                                       |
| `{type=FINDING}.data_json.description`                   | `finding.description`          | —                                                           | documented, absent from the fixture                                                                                                                     |
| `{type=FINDING}.data_json.description`                   | `finding.matcher`              | `digest16`                                                  | documented, absent from the fixture                                                                                                                     |
| `{type=FINDING}.data_json.path`                          | evidence                       | —                                                           | documented, absent from the fixture                                                                                                                     |
| `{type=FINDING}.data_json.full_url`                      | evidence                       | —                                                           | documented, absent from the fixture                                                                                                                     |
| `{type=FINDING}.data_json.cves`                          | `cve.value`                    | —                                                           | each member; documented, absent from the fixture                                                                                                        |
| `{type=FINDING}.data_json.archive_url`                   | evidence                       | —                                                           | documented, absent from the fixture                                                                                                                     |
| `{type=FINDING,module=nuclei}.data_json.description`     | `finding.rule`                 | `bbot_nuclei_template`                                      | redacted before ingest                                                                                                                                  |
| `{type=FINDING,module=nuclei}.data_json.description`     | `finding.matcher`              | `bbot_nuclei_matcher`                                       | redacted before ingest                                                                                                                                  |
| `{type=FINDING,module=nuclei}.data_json.description`     | `finding.title`                | `bbot_nuclei_template_id`                                   | redacted before ingest                                                                                                                                  |
| `{type=FINDING,module=nuclei}.data_json.description`     | `finding.description`          | `bbot_without_extracted_data`                               | redacted before ingest                                                                                                                                  |
| `{type=FINDING,module=trufflehog}.data_json.name`        | `finding.rule`                 | `bbot_trufflehog_detector`                                  | —                                                                                                                                                       |
| `{type=FINDING,module=trufflehog}.data_json.name`        | `finding.title`                | —                                                           | —                                                                                                                                                       |
| `{type=FINDING,module=trufflehog}.data_json.description` | `finding.matcher`              | `bbot_trufflehog_secret_part`, `sha256_hex`, `hex_prefix16` | redacted before ingest                                                                                                                                  |
| `{type=FINDING,module=badsecrets}.data_json.name`        | `finding.title`                | —                                                           | —                                                                                                                                                       |
| `{type=FINDING,module=badsecrets}.data_json.name`        | `finding.matcher`              | `detector_token`                                            | —                                                                                                                                                       |
| `{type=FINDING,module=badsecrets}.data_json.description` | non storable                   | —                                                           | redacted before ingest; the description quotes the known secret and the product that carries it                                                         |

Constants a write carries that no field holds:

| Sink                      | Values                             | Reason                                                                                 |
| ------------------------- | ---------------------------------- | -------------------------------------------------------------------------------------- |
| `endpoint.method`         | `"GET"`                            | BBOT's http module fetches every URL with GET, and a FINDING's url is one such URL     |
| `links_to.element` (edge) | `""`                               | a spidered URL's parent URL is the page it was excavated from; BBOT reports no element |
| `port.transport`          | `"tcp"`                            | the event type is OPEN_TCP_PORT                                                        |
| `http_fingerprint.kind`   | `"body_sha256"`, `"header_sha256"` | the hash field name fixes the fingerprint kind                                         |
| `finding.scanner`         | `"bbot"`                           | the tool that reported the finding                                                     |
| `source` (write metadata) | `"bbot"`                           | the tool that produced the output                                                      |

### `cdncheck`

- **Command:** `cdncheck -i targets.txt -jsonl -resp -o output.jsonl`
- **Version:** cdncheck v1.3.1 (commit `a06260a`)
- **Files:** `output.jsonl`

| Field        | Sink                           | Transforms                           | Notes                                           |
| ------------ | ------------------------------ | ------------------------------------ | ----------------------------------------------- |
| `timestamp`  | `observed_at` (write metadata) | `rfc3339_utc`                        | —                                               |
| `input`      | `subdomain.value`              | `strip_trailing_dot`, `if_subdomain` | —                                               |
| `input`      | `domain.value`                 | `strip_trailing_dot`, `if_domain`    | —                                               |
| `ip`         | `ip_address.value`             | —                                    | —                                               |
| `ip`         | `ip_address.version`           | `ip_version`                         | —                                               |
| `cdn`        | non storable                   | —                                    | implied by cdn_name, which names the provider   |
| `waf`        | non storable                   | —                                    | implied by waf_name, which names the provider   |
| `cloud`      | non storable                   | —                                    | implied by cloud_name, which names the provider |
| `cdn_name`   | `ip_address.cdn_provider`      | `detector_token`, `if_ip_input`      | —                                               |
| `cdn_name`   | `technology.name`              | `detector_token`, `if_name_input`    | —                                               |
| `waf_name`   | `ip_address.waf_provider`      | `detector_token`, `if_ip_input`      | —                                               |
| `waf_name`   | `technology.name`              | `detector_token`, `if_name_input`    | —                                               |
| `cloud_name` | `ip_address.cloud_provider`    | `detector_token`, `if_ip_input`      | —                                               |
| `cloud_name` | `technology.name`              | `detector_token`, `if_name_input`    | —                                               |

Constants a write carries that no field holds:

| Sink                       | Values       | Reason                                                                            |
| -------------------------- | ------------ | --------------------------------------------------------------------------------- |
| `protected_by.kind` (edge) | `"cdn"`      | cdn_name on a name input: the field name fixes the kind (waf_name would give waf) |
| `source` (write metadata)  | `"cdncheck"` | the tool that produced the output                                                 |

### `dnsx`

- **Command:** `dnsx -l hosts.txt -json -a -aaaa -cname -ns -txt -mx -soa -caa -cdn -asn -o output.jsonl`
- **Version:** dnsx, `dev` branch at commit `d261a79`, with retryabledns v1.0.116
- **Files:** `output.jsonl`

| Field             | Sink                                  | Transforms                                               | Notes                                                                                                               |
| ----------------- | ------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `timestamp`       | `observed_at` (write metadata)        | `rfc3339_utc`                                            | —                                                                                                                   |
| `host`            | `domain.value`                        | `strip_trailing_dot`, `dmarc_owner_name`, `if_domain`    | —                                                                                                                   |
| `host`            | `subdomain.value`                     | `strip_trailing_dot`, `dmarc_owner_name`, `if_subdomain` | —                                                                                                                   |
| `mx`              | `domain.value`                        | `strip_trailing_dot`, `if_domain`                        | each member                                                                                                         |
| `mx`              | `subdomain.value`                     | `strip_trailing_dot`, `if_subdomain`                     | each member                                                                                                         |
| `ns`              | `domain.value`                        | `strip_trailing_dot`, `if_domain`                        | each member                                                                                                         |
| `ns`              | `subdomain.value`                     | `strip_trailing_dot`, `if_subdomain`                     | each member                                                                                                         |
| `cname`           | `domain.value`                        | `strip_trailing_dot`, `if_domain`                        | each member                                                                                                         |
| `cname`           | `subdomain.value`                     | `strip_trailing_dot`, `if_subdomain`                     | each member                                                                                                         |
| `ttl`             | non storable                          | —                                                        | resolver cache lifetime of the answer, not a fact of the name                                                       |
| `resolver`        | non storable                          | —                                                        | the resolver that answered: scanner configuration                                                                   |
| `status_code`     | non storable                          | —                                                        | only names that answered are written; the rcode stays in evidence                                                   |
| `query-time`      | non storable                          | —                                                        | volatile response metric                                                                                            |
| `a`               | `ip_address.value`                    | —                                                        | each member                                                                                                         |
| `a`               | `ip_address.version`                  | `ip_version`                                             | each member                                                                                                         |
| `aaaa`            | `ip_address.value`                    | —                                                        | each member                                                                                                         |
| `aaaa`            | `ip_address.version`                  | `ip_version`                                             | each member                                                                                                         |
| `cname`           | `cloud_resource.hostname`             | `if_cloud_hostname`                                      | each member                                                                                                         |
| `cname`           | `cloud_resource.service`              | `cloud_service_or_none`                                  | each member                                                                                                         |
| `cname`           | `cloud_resource.region`               | `cloud_region`                                           | each member                                                                                                         |
| `soa[].name`      | `domain.value`                        | `strip_trailing_dot`, `if_domain`                        | —                                                                                                                   |
| `soa[].name`      | `subdomain.value`                     | `strip_trailing_dot`, `if_subdomain`                     | —                                                                                                                   |
| `soa[].ns`        | `domain.value`                        | `strip_trailing_dot`, `if_domain`                        | —                                                                                                                   |
| `soa[].ns`        | `subdomain.value`                     | `strip_trailing_dot`, `if_subdomain`                     | —                                                                                                                   |
| `soa[].mailbox`   | evidence                              | —                                                        | the zone's RNAME in DNS-name form; no has_contact role describes a zone administrator mailbox                       |
| `soa[].serial`    | non storable                          | —                                                        | zone version counter, changes on every zone edit                                                                    |
| `soa[].refresh`   | non storable                          | —                                                        | zone timer, secondary-server configuration                                                                          |
| `soa[].retry`     | non storable                          | —                                                        | zone timer, secondary-server configuration                                                                          |
| `soa[].expire`    | non storable                          | —                                                        | zone timer, secondary-server configuration                                                                          |
| `soa[].minttl`    | non storable                          | —                                                        | negative-caching TTL, resolver configuration                                                                        |
| `txt`             | `txt_record.value`                    | `if_txt_record`                                          | each member                                                                                                         |
| `txt`             | `spf_record.value`                    | `if_spf_record`                                          | each member                                                                                                         |
| `txt`             | `dmarc_record.value`                  | `if_dmarc_record`                                        | each member                                                                                                         |
| `caa`             | evidence                              | —                                                        | retryabledns keeps only the CAA value and drops flags and tag; the typed rows read the tagged record from `all`     |
| `all`             | `has_mail_exchange.preference` (edge) | `rr_mx_preference`                                       | each member                                                                                                         |
| `all`             | `caa_issue.flags` (edge)              | `rr_caa_issue_flags`                                     | each member                                                                                                         |
| `all`             | `caa_issue.parameters` (edge)         | `rr_caa_issue_parameters`                                | each member                                                                                                         |
| `all`             | `caa_issuewild.flags` (edge)          | `rr_caa_issuewild_flags`                                 | each member                                                                                                         |
| `all`             | `caa_issuewild.parameters` (edge)     | `rr_caa_issuewild_parameters`                            | each member                                                                                                         |
| `all`             | `domain.value`                        | `rr_caa_issuer`, `if_domain`                             | each member                                                                                                         |
| `all`             | `subdomain.value`                     | `rr_caa_issuer`, `if_subdomain`                          | each member                                                                                                         |
| `all`             | `email_address.value`                 | `rr_caa_iodef_email`                                     | each member                                                                                                         |
| `all`             | `endpoint.url`                        | `rr_caa_iodef_url`                                       | each member                                                                                                         |
| `cdn`             | non storable                          | —                                                        | implied by cdn-name, which names the provider                                                                       |
| `cdn-name`        | `technology.name`                     | `detector_token`                                         | —                                                                                                                   |
| `cdn-type`        | `protected_by.kind` (edge)            | `cdncheck_protection_kind`                               | —                                                                                                                   |
| `asn.as-number`   | `asn.value`                           | `asn_number`                                             | —                                                                                                                   |
| `asn.as-name`     | `asn.name`                            | —                                                        | —                                                                                                                   |
| `asn.as-country`  | `asn.country`                         | —                                                        | —                                                                                                                   |
| `asn.as-range`    | `ip_cidr.value`                       | —                                                        | each member                                                                                                         |
| `asn.as-range`    | `ip_cidr.version`                     | `ip_version`                                             | each member                                                                                                         |
| `srv`             | evidence                              | —                                                        | documented, absent from the fixture; not queried: -srv is not in the recorded flags                                 |
| `ptr`             | evidence                              | —                                                        | documented, absent from the fixture; not queried: -ptr is not in the recorded flags                                 |
| `raw`             | evidence                              | —                                                        | documented, absent from the fixture; dnsx blanks `raw` unless -raw is given, which the recorded command does not    |
| `status_code_raw` | non storable                          | —                                                        | documented, absent from the fixture; numeric rcode; omitted when 0 (NOERROR)                                        |
| `hosts_file`      | non storable                          | —                                                        | documented, absent from the fixture; the answer came from the local hosts file, not DNS; such a name is not written |

Constants a write carries that no field holds:

| Sink                      | Values    | Reason                                                           |
| ------------------------- | --------- | ---------------------------------------------------------------- |
| `has_contact.role` (edge) | `"iodef"` | a CAA iodef property is the iodef contact (RFC 8659 section 4.4) |
| `endpoint.method`         | `"POST"`  | RFC 6546 delivers IODEF reports to an https iodef URL by POST    |
| `source` (write metadata) | `"dnsx"`  | the tool that produced the output                                |

### `entra_realm`

- **Version:** Microsoft identity platform v2.0 OpenID discovery document; `getuserrealm.srf` JSON (`json=1`).
- **Files:** `getuserrealm.json`, `openid-configuration.json`

| Field                                        | Sink                                   | Transforms           | Notes                                                                             |
| -------------------------------------------- | -------------------------------------- | -------------------- | --------------------------------------------------------------------------------- |
| `issuer`                                     | `identity_tenant.tenant_id`            | `openid_tenant_id`   | —                                                                                 |
| `token_endpoint`                             | `identity_tenant.tenant_id`            | `openid_tenant_id`   | —                                                                                 |
| `token_endpoint_auth_methods_supported`      | evidence                               | —                    | —                                                                                 |
| `jwks_uri`                                   | evidence                               | —                    | —                                                                                 |
| `response_modes_supported`                   | evidence                               | —                    | —                                                                                 |
| `subject_types_supported`                    | evidence                               | —                    | —                                                                                 |
| `id_token_signing_alg_values_supported`      | evidence                               | —                    | —                                                                                 |
| `response_types_supported`                   | evidence                               | —                    | —                                                                                 |
| `scopes_supported`                           | evidence                               | —                    | —                                                                                 |
| `request_uri_parameter_supported`            | evidence                               | —                    | —                                                                                 |
| `userinfo_endpoint`                          | evidence                               | —                    | —                                                                                 |
| `authorization_endpoint`                     | evidence                               | —                    | —                                                                                 |
| `device_authorization_endpoint`              | evidence                               | —                    | —                                                                                 |
| `http_logout_supported`                      | evidence                               | —                    | —                                                                                 |
| `frontchannel_logout_supported`              | evidence                               | —                    | —                                                                                 |
| `end_session_endpoint`                       | evidence                               | —                    | —                                                                                 |
| `claims_supported`                           | evidence                               | —                    | —                                                                                 |
| `kerberos_endpoint`                          | evidence                               | —                    | —                                                                                 |
| `mtls_endpoint_aliases.token_endpoint`       | evidence                               | —                    | —                                                                                 |
| `tls_client_certificate_bound_access_tokens` | evidence                               | —                    | —                                                                                 |
| `tenant_region_scope`                        | evidence                               | —                    | —                                                                                 |
| `cloud_instance_name`                        | evidence                               | —                    | —                                                                                 |
| `cloud_graph_host_name`                      | evidence                               | —                    | —                                                                                 |
| `msgraph_host`                               | evidence                               | —                    | —                                                                                 |
| `rbac_url`                                   | evidence                               | —                    | —                                                                                 |
| `State`                                      | non storable                           | —                    | an undocumented getuserrealm code with no published meaning; it stays in evidence |
| `UserState`                                  | non storable                           | —                    | an undocumented getuserrealm code with no published meaning; it stays in evidence |
| `Login`                                      | evidence                               | —                    | —                                                                                 |
| `NameSpaceType`                              | `federates_with.namespace_type` (edge) | `lower`              | —                                                                                 |
| `DomainName`                                 | `domain.value`                         | `strip_trailing_dot` | —                                                                                 |
| `FederationGlobalVersion`                    | non storable                           | —                    | an undocumented getuserrealm code with no published meaning; it stays in evidence |
| `AuthURL`                                    | `subdomain.value`                      | `url_host`           | —                                                                                 |
| `FederationBrandName`                        | non storable                           | —                    | the tenant's display brand, a company name; company types are out of scope        |
| `AuthNForwardType`                           | non storable                           | —                    | an undocumented getuserrealm code with no published meaning; it stays in evidence |
| `CloudInstanceName`                          | evidence                               | —                    | —                                                                                 |
| `CloudInstanceIssuerUri`                     | evidence                               | —                    | —                                                                                 |

Constants a write carries that no field holds:

| Sink                       | Values          | Reason                                                                                |
| -------------------------- | --------------- | ------------------------------------------------------------------------------------- |
| `identity_tenant.provider` | `"entra_id"`    | the OpenID configuration comes from login.microsoftonline.com, the Entra ID authority |
| `source` (write metadata)  | `"entra_realm"` | the source that produced the output                                                   |

### `first_epss`

- **Command:** `curl -s "https://api.first.org/data/v1/epss?cve=CVE-2021-44228,CVE-2025-24813"`
- **Version:** FIRST EPSS API v1 (`"version": "1.0"`)
- **Files:** `epss.json`

| Field               | Sink                           | Transforms    | Notes                                       |
| ------------------- | ------------------------------ | ------------- | ------------------------------------------- |
| `status`            | non storable                   | —             | the API call's status                       |
| `status-code`       | non storable                   | —             | the API call's HTTP status                  |
| `version`           | non storable                   | —             | the API's version, not a fact about the CVE |
| `access`            | non storable                   | —             | the API's access level                      |
| `total`             | non storable                   | —             | paging of the API response                  |
| `offset`            | non storable                   | —             | paging of the API response                  |
| `limit`             | non storable                   | —             | paging of the API response                  |
| `data[].cve`        | `cve.value`                    | —             | —                                           |
| `data[].epss`       | `cve.epss_score`               | `float_value` | —                                           |
| `data[].percentile` | `cve.epss_percentile`          | `float_value` | —                                           |
| `data[].date`       | `observed_at` (write metadata) | `rfc3339_utc` | —                                           |

Constants a write carries that no field holds:

| Sink                      | Values         | Reason                           |
| ------------------------- | -------------- | -------------------------------- |
| `source` (write metadata) | `"first-epss"` | the API that produced the output |

### `github_repository`

- **Command:** `curl -s -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/repos/Example-Org/Web-App`
- **Version:** GitHub REST API 2022-11-28, unauthenticated
- **Files:** `repository.json`

| Field                              | Sink                        | Transforms | Notes                                                                              |
| ---------------------------------- | --------------------------- | ---------- | ---------------------------------------------------------------------------------- |
| `id`                               | evidence                    | —          | —                                                                                  |
| `node_id`                          | evidence                    | —          | —                                                                                  |
| `name`                             | `repository.name`           | `lower`    | —                                                                                  |
| `full_name`                        | non storable                | —          | owner and name joined by `/`; both are mapped                                      |
| `private`                          | non storable                | —          | implied by visibility                                                              |
| `owner.login`                      | `repository.owner`          | `lower`    | —                                                                                  |
| `owner.id`                         | evidence                    | —          | —                                                                                  |
| `owner.node_id`                    | evidence                    | —          | —                                                                                  |
| `owner.avatar_url`                 | evidence                    | —          | —                                                                                  |
| `owner.gravatar_id`                | evidence                    | —          | —                                                                                  |
| `owner.url`                        | evidence                    | —          | —                                                                                  |
| `owner.html_url`                   | evidence                    | —          | —                                                                                  |
| `owner.followers_url`              | evidence                    | —          | —                                                                                  |
| `owner.following_url`              | evidence                    | —          | —                                                                                  |
| `owner.gists_url`                  | evidence                    | —          | —                                                                                  |
| `owner.starred_url`                | evidence                    | —          | —                                                                                  |
| `owner.subscriptions_url`          | evidence                    | —          | —                                                                                  |
| `owner.organizations_url`          | evidence                    | —          | —                                                                                  |
| `owner.repos_url`                  | evidence                    | —          | —                                                                                  |
| `owner.events_url`                 | evidence                    | —          | —                                                                                  |
| `owner.received_events_url`        | evidence                    | —          | —                                                                                  |
| `owner.type`                       | evidence                    | —          | —                                                                                  |
| `owner.user_view_type`             | evidence                    | —          | —                                                                                  |
| `owner.site_admin`                 | evidence                    | —          | —                                                                                  |
| `html_url`                         | `repository.host`           | `url_host` | —                                                                                  |
| `description`                      | evidence                    | —          | —                                                                                  |
| `fork`                             | `repository.fork`           | —          | —                                                                                  |
| `url`                              | evidence                    | —          | —                                                                                  |
| `forks_url`                        | evidence                    | —          | —                                                                                  |
| `keys_url`                         | evidence                    | —          | —                                                                                  |
| `collaborators_url`                | evidence                    | —          | —                                                                                  |
| `teams_url`                        | evidence                    | —          | —                                                                                  |
| `hooks_url`                        | evidence                    | —          | —                                                                                  |
| `issue_events_url`                 | evidence                    | —          | —                                                                                  |
| `events_url`                       | evidence                    | —          | —                                                                                  |
| `assignees_url`                    | evidence                    | —          | —                                                                                  |
| `branches_url`                     | evidence                    | —          | —                                                                                  |
| `tags_url`                         | evidence                    | —          | —                                                                                  |
| `blobs_url`                        | evidence                    | —          | —                                                                                  |
| `git_tags_url`                     | evidence                    | —          | —                                                                                  |
| `git_refs_url`                     | evidence                    | —          | —                                                                                  |
| `trees_url`                        | evidence                    | —          | —                                                                                  |
| `statuses_url`                     | evidence                    | —          | —                                                                                  |
| `languages_url`                    | evidence                    | —          | —                                                                                  |
| `stargazers_url`                   | evidence                    | —          | —                                                                                  |
| `contributors_url`                 | evidence                    | —          | —                                                                                  |
| `subscribers_url`                  | evidence                    | —          | —                                                                                  |
| `subscription_url`                 | evidence                    | —          | —                                                                                  |
| `commits_url`                      | evidence                    | —          | —                                                                                  |
| `git_commits_url`                  | evidence                    | —          | —                                                                                  |
| `comments_url`                     | evidence                    | —          | —                                                                                  |
| `issue_comment_url`                | evidence                    | —          | —                                                                                  |
| `contents_url`                     | evidence                    | —          | —                                                                                  |
| `compare_url`                      | evidence                    | —          | —                                                                                  |
| `merges_url`                       | evidence                    | —          | —                                                                                  |
| `archive_url`                      | evidence                    | —          | —                                                                                  |
| `downloads_url`                    | evidence                    | —          | —                                                                                  |
| `issues_url`                       | evidence                    | —          | —                                                                                  |
| `pulls_url`                        | evidence                    | —          | —                                                                                  |
| `milestones_url`                   | evidence                    | —          | —                                                                                  |
| `notifications_url`                | evidence                    | —          | —                                                                                  |
| `labels_url`                       | evidence                    | —          | —                                                                                  |
| `releases_url`                     | evidence                    | —          | —                                                                                  |
| `deployments_url`                  | evidence                    | —          | —                                                                                  |
| `created_at`                       | evidence                    | —          | —                                                                                  |
| `updated_at`                       | evidence                    | —          | —                                                                                  |
| `pushed_at`                        | evidence                    | —          | —                                                                                  |
| `git_url`                          | evidence                    | —          | —                                                                                  |
| `ssh_url`                          | evidence                    | —          | —                                                                                  |
| `clone_url`                        | evidence                    | —          | —                                                                                  |
| `svn_url`                          | evidence                    | —          | —                                                                                  |
| `homepage`                         | evidence                    | —          | —                                                                                  |
| `size`                             | evidence                    | —          | —                                                                                  |
| `stargazers_count`                 | evidence                    | —          | —                                                                                  |
| `watchers_count`                   | evidence                    | —          | —                                                                                  |
| `language`                         | evidence                    | —          | —                                                                                  |
| `has_issues`                       | evidence                    | —          | —                                                                                  |
| `has_projects`                     | evidence                    | —          | —                                                                                  |
| `has_downloads`                    | evidence                    | —          | —                                                                                  |
| `has_wiki`                         | evidence                    | —          | —                                                                                  |
| `has_pages`                        | evidence                    | —          | —                                                                                  |
| `has_discussions`                  | evidence                    | —          | —                                                                                  |
| `forks_count`                      | evidence                    | —          | —                                                                                  |
| `mirror_url`                       | evidence                    | —          | —                                                                                  |
| `archived`                         | `repository.archived`       | —          | —                                                                                  |
| `disabled`                         | evidence                    | —          | —                                                                                  |
| `open_issues_count`                | evidence                    | —          | —                                                                                  |
| `license.key`                      | evidence                    | —          | —                                                                                  |
| `license.name`                     | evidence                    | —          | —                                                                                  |
| `license.spdx_id`                  | evidence                    | —          | —                                                                                  |
| `license.url`                      | evidence                    | —          | —                                                                                  |
| `license.node_id`                  | evidence                    | —          | —                                                                                  |
| `allow_forking`                    | evidence                    | —          | —                                                                                  |
| `is_template`                      | evidence                    | —          | —                                                                                  |
| `web_commit_signoff_required`      | evidence                    | —          | —                                                                                  |
| `has_pull_requests`                | evidence                    | —          | —                                                                                  |
| `pull_request_creation_policy`     | evidence                    | —          | —                                                                                  |
| `topics`                           | evidence                    | —          | —                                                                                  |
| `visibility`                       | `repository.visibility`     | —          | —                                                                                  |
| `forks`                            | evidence                    | —          | —                                                                                  |
| `open_issues`                      | evidence                    | —          | —                                                                                  |
| `watchers`                         | evidence                    | —          | —                                                                                  |
| `default_branch`                   | `repository.default_branch` | —          | —                                                                                  |
| `temp_clone_token`                 | non storable                | —          | redacted before ingest; a short-lived clone token when the caller is authenticated |
| `custom_properties.*`              | evidence                    | —          | —                                                                                  |
| `organization.login`               | evidence                    | —          | —                                                                                  |
| `organization.id`                  | evidence                    | —          | —                                                                                  |
| `organization.node_id`             | evidence                    | —          | —                                                                                  |
| `organization.avatar_url`          | evidence                    | —          | —                                                                                  |
| `organization.gravatar_id`         | evidence                    | —          | —                                                                                  |
| `organization.url`                 | evidence                    | —          | —                                                                                  |
| `organization.html_url`            | evidence                    | —          | —                                                                                  |
| `organization.followers_url`       | evidence                    | —          | —                                                                                  |
| `organization.following_url`       | evidence                    | —          | —                                                                                  |
| `organization.gists_url`           | evidence                    | —          | —                                                                                  |
| `organization.starred_url`         | evidence                    | —          | —                                                                                  |
| `organization.subscriptions_url`   | evidence                    | —          | —                                                                                  |
| `organization.organizations_url`   | evidence                    | —          | —                                                                                  |
| `organization.repos_url`           | evidence                    | —          | —                                                                                  |
| `organization.events_url`          | evidence                    | —          | —                                                                                  |
| `organization.received_events_url` | evidence                    | —          | —                                                                                  |
| `organization.type`                | evidence                    | —          | —                                                                                  |
| `organization.user_view_type`      | evidence                    | —          | —                                                                                  |
| `organization.site_admin`          | evidence                    | —          | —                                                                                  |
| `network_count`                    | evidence                    | —          | —                                                                                  |
| `subscribers_count`                | evidence                    | —          | —                                                                                  |

Constants a write carries that no field holds:

| Sink                      | Values     | Reason                                    |
| ------------------------- | ---------- | ----------------------------------------- |
| `repository.platform`     | `"github"` | the object comes from the GitHub REST API |
| `source` (write metadata) | `"github"` | the API that produced the output          |

### `gitleaks`

- **Command:** `gitleaks git --platform github --report-format json --report-path gitleaks.json .`
- **Version:** gitleaks v8.30.1 (source read at `b58d3f1`)
- **Files:** `gitleaks.json`

| Field         | Sink                             | Transforms             | Notes                                                                                                                                                             |
| ------------- | -------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `RuleID`      | `secret.detector`                | `detector_token`       | —                                                                                                                                                                 |
| `RuleID`      | `secret.kind`                    | `gitleaks_secret_kind` | —                                                                                                                                                                 |
| `Description` | evidence                         | —                      | —                                                                                                                                                                 |
| `StartLine`   | evidence                         | —                      | —                                                                                                                                                                 |
| `EndLine`     | evidence                         | —                      | —                                                                                                                                                                 |
| `StartColumn` | evidence                         | —                      | —                                                                                                                                                                 |
| `EndColumn`   | evidence                         | —                      | —                                                                                                                                                                 |
| `Line`        | non storable                     | —                      | redacted before ingest; documented, absent from the fixture; the whole source line, secret included; `json:"-"` keeps it out of the report                        |
| `Match`       | non storable                     | —                      | redacted before ingest; the matched text, secret included                                                                                                         |
| `Secret`      | `secret.value_sha256`            | `sha256_hex`           | redacted before ingest                                                                                                                                            |
| `File`        | `exposes_secret.location` (edge) | `gitleaks_location`    | —                                                                                                                                                                 |
| `SymlinkFile` | evidence                         | —                      | —                                                                                                                                                                 |
| `Commit`      | evidence                         | —                      | —                                                                                                                                                                 |
| `Link`        | `repository.host`                | `url_host`             | —                                                                                                                                                                 |
| `Link`        | `repository.owner`               | `repo_url_owner`       | —                                                                                                                                                                 |
| `Link`        | `repository.name`                | `repo_url_name`        | —                                                                                                                                                                 |
| `Entropy`     | non storable                     | —                      | a scanner-internal score of the secret                                                                                                                            |
| `Author`      | non storable                     | —                      | commit author name: person data                                                                                                                                   |
| `Email`       | non storable                     | —                      | commit author address: person data                                                                                                                                |
| `Date`        | evidence                         | —                      | —                                                                                                                                                                 |
| `Message`     | evidence                         | —                      | —                                                                                                                                                                 |
| `Tags`        | evidence                         | —                      | —                                                                                                                                                                 |
| `Fingerprint` | evidence                         | —                      | —                                                                                                                                                                 |
| `Fragment.*`  | non storable                     | —                      | redacted before ingest; documented, absent from the fixture; never set on a reported finding (`omitempty` drops the nil pointer); it would carry the scanned text |

Constants a write carries that no field holds:

| Sink                      | Values       | Reason                                          |
| ------------------------- | ------------ | ----------------------------------------------- |
| `repository.platform`     | `"github"`   | gitleaks built the Link for the github platform |
| `source` (write metadata) | `"gitleaks"` | the tool that produced the output               |

### `httpx`

- **Command:** `httpx -json -irh -title -status-code -content-length -content-type -web-server -tech-detect -cdn -favicon -hash sha256 -jarm -ip -cname -asn -location -l hosts.txt`
- **Version:** httpx v1.12.0
- **Files:** `output.jsonl`

| Field                | Sink                             | Transforms             | Notes                                                                                                                   |
| -------------------- | -------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `timestamp`          | `observed_at` (write metadata)   | `rfc3339_utc`          | —                                                                                                                       |
| `url`                | `endpoint.url`                   | `url_with_path`        | —                                                                                                                       |
| `input`              | evidence                         | —                      | —                                                                                                                       |
| `host`               | `subdomain.value`                | `strip_trailing_dot`   | —                                                                                                                       |
| `host_ip`            | `ip_address.value`               | —                      | —                                                                                                                       |
| `host_ip`            | `ip_address.version`             | `ip_version`           | —                                                                                                                       |
| `port`               | `port.number`                    | `int_value`            | —                                                                                                                       |
| `scheme`             | `service.name`                   | `scheme_service`       | —                                                                                                                       |
| `scheme`             | `service.secure`                 | `scheme_secure`        | —                                                                                                                       |
| `path`               | non storable                     | —                      | the path is part of the endpoint url                                                                                    |
| `method`             | `endpoint.method`                | —                      | —                                                                                                                       |
| `location`           | `endpoint.url`                   | —                      | —                                                                                                                       |
| `final_url`          | `endpoint.url`                   | —                      | documented, absent from the fixture                                                                                     |
| `status_code`        | `endpoint.status`                | —                      | —                                                                                                                       |
| `content_length`     | `endpoint.content_length`        | —                      | —                                                                                                                       |
| `content_type`       | `endpoint.content_type`          | `media_type_essence`   | —                                                                                                                       |
| `title`              | `endpoint.title`                 | —                      | —                                                                                                                       |
| `webserver`          | `endpoint.webserver`             | —                      | —                                                                                                                       |
| `header.*`           | evidence                         | —                      | —                                                                                                                       |
| `body`               | evidence                         | —                      | documented, absent from the fixture                                                                                     |
| `raw_header`         | evidence                         | —                      | documented, absent from the fixture                                                                                     |
| `a`                  | `ip_address.value`               | —                      | each member                                                                                                             |
| `a`                  | `ip_address.version`             | `ip_version`           | each member                                                                                                             |
| `aaaa`               | `ip_address.value`               | —                      | each member                                                                                                             |
| `aaaa`               | `ip_address.version`             | `ip_version`           | each member                                                                                                             |
| `cname`              | `subdomain.value`                | `strip_trailing_dot`   | each member                                                                                                             |
| `cname`              | `cloud_resource.hostname`        | `strip_trailing_dot`   | each member                                                                                                             |
| `cname`              | `cloud_resource.service`         | `cloud_service`        | each member                                                                                                             |
| `cdn`                | non storable                     | —                      | implied by cdn_name, which names the provider                                                                           |
| `cdn_name`           | `ip_address.cdn_provider`        | `detector_token`       | —                                                                                                                       |
| `cdn_type`           | non storable                     | —                      | selects which provider property cdn_name feeds; every record in this fixture is `cdn`, so cdn_name maps to cdn_provider |
| `asn.as_number`      | `asn.value`                      | `asn_number`           | —                                                                                                                       |
| `asn.as_name`        | `asn.name`                       | —                      | —                                                                                                                       |
| `asn.as_country`     | `asn.country`                    | —                      | —                                                                                                                       |
| `asn.as_range`       | `ip_cidr.value`                  | —                      | each member                                                                                                             |
| `asn.as_range`       | `ip_cidr.version`                | `ip_version`           | each member                                                                                                             |
| `tech`               | `technology.name`                | `wappalyzer_name_slug` | each member                                                                                                             |
| `tech`               | `runs_technology.version` (edge) | `wappalyzer_version`   | each member                                                                                                             |
| `favicon`            | `http_fingerprint.value`         | —                      | —                                                                                                                       |
| `favicon_md5`        | non storable                     | —                      | favicon_mmh3 is the pivot the catalog keys; md5 duplicates it                                                           |
| `favicon_path`       | evidence                         | —                      | —                                                                                                                       |
| `favicon_url`        | evidence                         | —                      | —                                                                                                                       |
| `jarm_hash`          | `tls_fingerprint.value`          | —                      | —                                                                                                                       |
| `hash.body_sha256`   | `http_fingerprint.value`         | —                      | —                                                                                                                       |
| `hash.header_sha256` | `http_fingerprint.value`         | —                      | —                                                                                                                       |
| `words`              | non storable                     | —                      | volatile response metric                                                                                                |
| `lines`              | non storable                     | —                      | volatile response metric                                                                                                |
| `time`               | non storable                     | —                      | volatile response metric                                                                                                |
| `failed`             | non storable                     | —                      | only successful probes are written                                                                                      |
| `knowledgebase.*`    | evidence                         | —                      | —                                                                                                                       |

Constants a write carries that no field holds:

| Sink                         | Values                                               | Reason                                                        |
| ---------------------------- | ---------------------------------------------------- | ------------------------------------------------------------- |
| `port.transport`             | `"tcp"`                                              | httpx probes over TCP                                         |
| `http_fingerprint.kind`      | `"favicon_mmh3"`, `"body_sha256"`, `"header_sha256"` | the field name fixes the fingerprint kind                     |
| `tls_fingerprint.kind`       | `"jarm"`                                             | jarm_hash is a JARM fingerprint                               |
| `redirects_to.status` (edge) | `301`                                                | the status_code of the record whose location names the target |
| `protected_by.kind` (edge)   | `"cdn"`                                              | cdn_type cdn with a cdn_name on the probed host               |
| `source` (write metadata)    | `"httpx"`                                            | the tool that produced the output                             |

### `katana`

- **Command:** `katana -u https://www.example.com -jsonl -o output.jsonl -d 3 -jc -kf robotstxt -fx -td -kb -do`
- **Version:** katana v1.7.0 (`internal/runner/banner.go` at commit `c1de26d`)
- **Files:** `output.jsonl`

| Field                           | Sink                             | Transforms                           | Notes       |
| ------------------------------- | -------------------------------- | ------------------------------------ | ----------- |
| `timestamp`                     | `observed_at` (write metadata)   | `rfc3339_utc`                        | —           |
| `request.method`                | `endpoint.method`                | —                                    | —           |
| `request.endpoint`              | `endpoint.url`                   | `url_without_query`, `url_with_path` | —           |
| `request.body`                  | evidence                         | —                                    | —           |
| `request.headers.*`             | evidence                         | —                                    | —           |
| `request.tag`                   | `links_to.element` (edge)        | —                                    | —           |
| `request.attribute`             | `links_to.attribute` (edge)      | —                                    | —           |
| `request.source`                | `endpoint.url`                   | `url_without_query`, `url_with_path` | —           |
| `request.raw`                   | evidence                         | —                                    | —           |
| `response.status_code`          | `endpoint.status`                | —                                    | —           |
| `response.headers.*`            | evidence                         | —                                    | —           |
| `response.headers.content-type` | `endpoint.content_type`          | `media_type_essence`                 | —           |
| `response.headers.server`       | `endpoint.webserver`             | —                                    | —           |
| `response.body`                 | evidence                         | —                                    | —           |
| `response.content_length`       | `endpoint.content_length`        | —                                    | —           |
| `response.technologies`         | `technology.name`                | `wappalyzer_name_slug`               | each member |
| `response.technologies`         | `runs_technology.version` (edge) | `wappalyzer_version`                 | each member |
| `response.raw`                  | evidence                         | —                                    | —           |
| `response.forms[].method`       | `endpoint.method`                | —                                    | —           |
| `response.forms[].action`       | `endpoint.url`                   | `url_without_query`, `url_with_path` | —           |
| `response.forms[].enctype`      | evidence                         | —                                    | —           |
| `response.forms[].parameters`   | `parameter.name`                 | —                                    | each member |
| `response.knowledgebase.*`      | evidence                         | —                                    | —           |
| `error`                         | evidence                         | —                                    | —           |

Constants a write carries that no field holds:

| Sink                      | Values     | Reason                                                                             |
| ------------------------- | ---------- | ---------------------------------------------------------------------------------- |
| `endpoint.method`         | `"GET"`    | a request.source page is one katana fetched by navigation, which is always a GET   |
| `parameter.location`      | `"body"`   | the form posts application/x-www-form-urlencoded, so its fields travel in the body |
| `source` (write metadata) | `"katana"` | the tool that produced the output                                                  |

### `leak_corpus`

- **Command:** `curl -s -H "X-API-Key: $LEAKCHECK_KEY" "https://leakcheck.io/api/v2/query/alice@example.com?type=email"`
- **Version:** LeakCheck Pro API v2
- **Files:** `leakcheck.json`

| Field                          | Sink                                | Transforms               | Notes                                                  |
| ------------------------------ | ----------------------------------- | ------------------------ | ------------------------------------------------------ |
| `success`                      | non storable                        | —                        | the API call's status, not a fact about the credential |
| `found`                        | non storable                        | —                        | the result count of this query                         |
| `quota`                        | non storable                        | —                        | the caller's remaining API quota                       |
| `result[].email`               | `email_address.value`               | `lower`                  | —                                                      |
| `result[].source.name`         | `authenticates.breach` (edge)       | `leakcheck_breach_token` | —                                                      |
| `result[].source.name`         | `authenticates.breach_title` (edge) | —                        | —                                                      |
| `result[].source.breach_date`  | `authenticates.breach_date` (edge)  | `partial_date`           | —                                                      |
| `result[].source.unverified`   | evidence                            | —                        | —                                                      |
| `result[].source.passwordless` | evidence                            | —                        | —                                                      |
| `result[].source.compilation`  | evidence                            | —                        | —                                                      |
| `result[].username`            | `authenticates.username` (edge)     | —                        | —                                                      |
| `result[].password`            | `secret.value_sha256`               | `sha256_hex`             | redacted before ingest                                 |
| `result[].hashed_password`     | `secret.value_sha256`               | `sha256_hex`             | redacted before ingest                                 |
| `result[].first_name`          | non storable                        | —                        | a person's name: personal PII                          |
| `result[].last_name`           | non storable                        | —                        | a person's name: personal PII                          |
| `result[].name`                | non storable                        | —                        | a person's name: personal PII                          |
| `result[].dob`                 | non storable                        | —                        | a date of birth: personal PII                          |
| `result[].address`             | non storable                        | —                        | a postal address: personal PII                         |
| `result[].zip`                 | non storable                        | —                        | part of a postal address: personal PII                 |
| `result[].phone`               | non storable                        | —                        | a personal phone number: personal PII                  |
| `result[].fields`              | evidence                            | —                        | —                                                      |
| `result[].collected`           | evidence                            | —                        | documented, absent from the fixture                    |

Constants a write carries that no field holds:

| Sink                      | Values                          | Reason                                                                                     |
| ------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------ |
| `secret.kind`             | `"password"`, `"password_hash"` | the field name fixes the kind: `password` is plaintext, `hashed_password` a published hash |
| `source` (write metadata) | `"leakcheck"`                   | the corpus the result came from                                                            |

### `mta_sts`

- **Version:** MTA-STS policy version `STSv1`; the body is the policy host's.
- **Files:** `mta-sts.txt`

| Field     | Sink                     | Transforms                       | Notes                                                                                                                 |
| --------- | ------------------------ | -------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `version` | non storable             | —                                | STSv1 is the only policy version, and the TXT value already carries it                                                |
| `mode`    | `mta_sts_policy.mode`    | —                                | —                                                                                                                     |
| `max_age` | `mta_sts_policy.max_age` | `int_value`                      | —                                                                                                                     |
| `mx`      | `mta_sts_policy.mx`      | `mta_sts_mx_lines`, `sorted_set` | —                                                                                                                     |
| `*`       | evidence                 | —                                | documented, absent from the fixture; an RFC 8461 sts-policy-extension field; none is defined, so it stays in evidence |

Constants a write carries that no field holds:

| Sink                      | Values                           | Reason                                                                                                                                                                                 |
| ------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `domain.value`            | `"example.com"`                  | the policy is fetched from https://mta-sts.example.com/.well-known/mta-sts.txt (SOURCE.md); the policy domain is that host without its mta-sts label, and the file body never names it |
| `mta_sts_policy.value`    | `"v=STSv1; id=20260901T000000;"` | the policy node's identity is the TXT record at \_mta-sts.example.com that announced the policy (queried alongside the fetch, SOURCE.md); the policy file body does not carry it       |
| `source` (write metadata) | `"mta_sts"`                      | the source that produced the output                                                                                                                                                    |

### `naabu`

- **Command:** `naabu -l hosts.txt -p 22,443 -json -cdn -o output.jsonl`
- **Version:** naabu v2, `dev` branch at commit `18264a7`
- **Files:** `output.jsonl`

| Field       | Sink                           | Transforms                           | Notes                                                                                                 |
| ----------- | ------------------------------ | ------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `timestamp` | `observed_at` (write metadata) | `rfc3339_utc`                        | —                                                                                                     |
| `host`      | `subdomain.value`              | `strip_trailing_dot`, `if_subdomain` | —                                                                                                     |
| `host`      | `domain.value`                 | `strip_trailing_dot`, `if_domain`    | —                                                                                                     |
| `ip`        | `ip_address.value`             | —                                    | —                                                                                                     |
| `ip`        | `ip_address.version`           | `ip_version`                         | —                                                                                                     |
| `port`      | `port.number`                  | `int_value`                          | —                                                                                                     |
| `protocol`  | `port.transport`               | —                                    | —                                                                                                     |
| `tls`       | non storable                   | —                                    | a port scan writes no service node for the flag to describe; tlsx and httpx record TLS on the service |
| `cdn`       | non storable                   | —                                    | implied by cdn-name, which names the provider                                                         |
| `cdn-name`  | `ip_address.cdn_provider`      | `detector_token`                     | —                                                                                                     |

Constants a write carries that no field holds:

| Sink                      | Values    | Reason                            |
| ------------------------- | --------- | --------------------------------- |
| `source` (write metadata) | `"naabu"` | the tool that produced the output |

### `nmap`

- **Command:** `nmap -sV -p 22,443 --open --script ssh-hostkey --script-args ssh_hostkey=sha256 -oX scan.xml www.example.com`
- **Version:** Nmap 7.95, XML output version 1.05
- **Files:** `scan.xml`

| Field                                                         | Sink                             | Transforms                                      | Notes                                                                                              |
| ------------------------------------------------------------- | -------------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `nmaprun.@scanner`                                            | evidence                         | —                                               | the scanner name, a constant                                                                       |
| `nmaprun.@args`                                               | evidence                         | —                                               | the recorded command line; SOURCE.md restates it                                                   |
| `nmaprun.@start`                                              | evidence                         | —                                               | scan start; the host's endtime is the observation time                                             |
| `nmaprun.@startstr`                                           | evidence                         | —                                               | scan start in local text form                                                                      |
| `nmaprun.@version`                                            | evidence                         | —                                               | the nmap version; SOURCE.md restates it                                                            |
| `nmaprun.@xmloutputversion`                                   | evidence                         | —                                               | the XML format version                                                                             |
| `nmaprun.scaninfo[].@type`                                    | evidence                         | —                                               | scan technique, scanner configuration                                                              |
| `nmaprun.scaninfo[].@protocol`                                | evidence                         | —                                               | scan protocol, scanner configuration                                                               |
| `nmaprun.scaninfo[].@numservices`                             | evidence                         | —                                               | how many ports were probed, scanner configuration                                                  |
| `nmaprun.scaninfo[].@services`                                | evidence                         | —                                               | the probed port list, scanner configuration                                                        |
| `nmaprun.verbose[].@level`                                    | evidence                         | —                                               | scanner configuration                                                                              |
| `nmaprun.debugging[].@level`                                  | evidence                         | —                                               | scanner configuration                                                                              |
| `nmaprun.host[].@starttime`                                   | evidence                         | —                                               | host scan start; the endtime is the observation time                                               |
| `nmaprun.host[].@endtime`                                     | `observed_at` (write metadata)   | `epoch_utc`                                     | —                                                                                                  |
| `nmaprun.host[].status[].@state`                              | non storable                     | —                                               | only hosts that are up carry open ports to write                                                   |
| `nmaprun.host[].status[].@reason`                             | non storable                     | —                                               | scanner-internal                                                                                   |
| `nmaprun.host[].status[].@reason_ttl`                         | non storable                     | —                                               | scanner-internal                                                                                   |
| `nmaprun.host[].address[].@addr`                              | `ip_address.value`               | —                                               | —                                                                                                  |
| `nmaprun.host[].address[].@addr`                              | `ip_address.version`             | `ip_version`                                    | —                                                                                                  |
| `nmaprun.host[].address[].@addrtype`                          | non storable                     | —                                               | restates the address family that version reads off the address                                     |
| `nmaprun.host[].address[].@vendor`                            | non storable                     | —                                               | documented, absent from the fixture; MAC vendor, present only for a mac address on a local segment |
| `nmaprun.host[].hostnames[].hostname[].@name`                 | `subdomain.value`                | `strip_trailing_dot`, `if_subdomain`            | —                                                                                                  |
| `nmaprun.host[].hostnames[].hostname[].@name`                 | `domain.value`                   | `strip_trailing_dot`, `if_domain`               | —                                                                                                  |
| `nmaprun.host[].hostnames[].hostname[].@type`                 | evidence                         | —                                               | user (the target name) selects resolves_to, PTR selects reverse_resolves_to                        |
| `nmaprun.host[].ports[].extraports[].@state`                  | non storable                     | —                                               | documented, absent from the fixture; ports that are not open are not written                       |
| `nmaprun.host[].ports[].port[].@protocol`                     | `port.transport`                 | —                                               | —                                                                                                  |
| `nmaprun.host[].ports[].port[].@portid`                       | `port.number`                    | `int_value`                                     | —                                                                                                  |
| `nmaprun.host[].ports[].port[].state[].@state`                | non storable                     | —                                               | only open ports are written (--open), and the has_open_port edge is the state                      |
| `nmaprun.host[].ports[].port[].state[].@reason`               | non storable                     | —                                               | scanner-internal                                                                                   |
| `nmaprun.host[].ports[].port[].state[].@reason_ttl`           | non storable                     | —                                               | scanner-internal                                                                                   |
| `nmaprun.host[].ports[].port[].service[].@name`               | `service.name`                   | —                                               | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].@product`            | `service.product`                | —                                               | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].@product`            | `technology.name`                | `detector_token`                                | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].@version`            | `service.version`                | —                                               | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].@version`            | `runs_technology.version` (edge) | —                                               | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].@extrainfo`          | non storable                     | —                                               | free-form nmap detail; kept in evidence                                                            |
| `nmaprun.host[].ports[].port[].service[].@tunnel`             | `service.secure`                 | `nmap_tunnel_secure`                            | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].@method`             | non storable                     | —                                               | scanner-internal: table lookup or probe                                                            |
| `nmaprun.host[].ports[].port[].service[].@conf`               | non storable                     | —                                               | scanner-internal detection confidence                                                              |
| `nmaprun.host[].ports[].port[].service[].@ostype`             | evidence                         | —                                               | documented, absent from the fixture; OS family from a service banner; no catalog home              |
| `nmaprun.host[].ports[].port[].service[].@hostname`           | evidence                         | —                                               | documented, absent from the fixture; a name the banner reports, unverified                         |
| `nmaprun.host[].ports[].port[].service[].@devicetype`         | evidence                         | —                                               | documented, absent from the fixture; device class from a banner; no catalog home                   |
| `nmaprun.host[].ports[].port[].service[].@servicefp`          | evidence                         | —                                               | documented, absent from the fixture; submission fingerprint of an unmatched service                |
| `nmaprun.host[].ports[].port[].service[].cpe[].#text`         | `technology.cpe`                 | `cpe22_uri_to_cpe23`, `cpe_product_level`       | —                                                                                                  |
| `nmaprun.host[].ports[].port[].service[].cpe[].#text`         | `runs_technology.cpe` (edge)     | `cpe22_uri_to_cpe23`                            | —                                                                                                  |
| `nmaprun.host[].ports[].port[].script[].@id`                  | evidence                         | —                                               | names the script; this mapping reads ssh-hostkey                                                   |
| `nmaprun.host[].ports[].port[].script[].@output`              | `host_key.fingerprint_sha256`    | `nmap_ssh_hostkey_sha256`, `openssh_b64_to_hex` | —                                                                                                  |
| `nmaprun.host[].ports[].port[].script[].table[].elem[].@key`  | evidence                         | —                                               | names which element the text is                                                                    |
| `nmaprun.host[].ports[].port[].script[].table[].elem[].#text` | `host_key.algorithm`             | `ssh_host_key_algorithm`                        | —                                                                                                  |
| `nmaprun.host[].times[].@srtt`                                | non storable                     | —                                               | round-trip timing, volatile                                                                        |
| `nmaprun.host[].times[].@rttvar`                              | non storable                     | —                                               | round-trip timing, volatile                                                                        |
| `nmaprun.host[].times[].@to`                                  | non storable                     | —                                               | probe timeout, scanner-internal                                                                    |
| `nmaprun.runstats[].finished[].@time`                         | evidence                         | —                                               | scan end, run bookkeeping                                                                          |
| `nmaprun.runstats[].finished[].@timestr`                      | evidence                         | —                                               | scan end in local text form                                                                        |
| `nmaprun.runstats[].finished[].@summary`                      | evidence                         | —                                               | run summary text                                                                                   |
| `nmaprun.runstats[].finished[].@elapsed`                      | non storable                     | —                                               | volatile run metric                                                                                |
| `nmaprun.runstats[].finished[].@exit`                         | evidence                         | —                                               | run status                                                                                         |
| `nmaprun.runstats[].hosts[].@up`                              | evidence                         | —                                               | run statistics                                                                                     |
| `nmaprun.runstats[].hosts[].@down`                            | evidence                         | —                                               | run statistics                                                                                     |
| `nmaprun.runstats[].hosts[].@total`                           | evidence                         | —                                               | run statistics                                                                                     |

Constants a write carries that no field holds:

| Sink                      | Values   | Reason                            |
| ------------------------- | -------- | --------------------------------- |
| `source` (write metadata) | `"nmap"` | the tool that produced the output |

### `nuclei`

- **Command:** `nuclei -l targets.txt -t http/,headless/,ssl/,network/,javascript/,dns/ -t /home/analyst/custom-templates/whois-registrar-expiry.yaml -headless -jsonl -o scan.jsonl`, then `nuclei -l targets.txt -dast -jsonl -o dast.jsonl` (with `-dast` nuclei loads only fuzzing templates, so the DAST result needs its own run), concatenated into `output.jsonl`
- **Version:** nuclei v3.11.1 (fields checked at `dev` commit `a5b59a9`). Request/response pairs are
- **Files:** `output.jsonl`

| Field                                 | Sink                           | Transforms                      | Notes                                                                                                                               |
| ------------------------------------- | ------------------------------ | ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `template`                            | evidence                       | —                               | —                                                                                                                                   |
| `template-url`                        | evidence                       | —                               | —                                                                                                                                   |
| `template-id`                         | `finding.rule`                 | `nuclei_rule`                   | —                                                                                                                                   |
| `template-id`                         | `finding.matcher`              | `nuclei_matcher`                | only when `nuclei_not_ssl_result`                                                                                                   |
| `template-id`                         | `finding.matcher`              | `nuclei_matcher`, `sni_matcher` | only when `nuclei_ssl_result`                                                                                                       |
| `template-path`                       | non storable                   | —                               | a path on the scanning host's filesystem                                                                                            |
| `template-encoded`                    | evidence                       | —                               | —                                                                                                                                   |
| `info.name`                           | `finding.title`                | —                               | —                                                                                                                                   |
| `info.author`                         | non storable                   | —                               | template author handles and names; person types are out of scope                                                                    |
| `info.tags`                           | `finding.tags`                 | `sorted_set`                    | —                                                                                                                                   |
| `info.description`                    | `finding.description`          | —                               | —                                                                                                                                   |
| `info.impact`                         | evidence                       | —                               | —                                                                                                                                   |
| `info.reference`                      | evidence                       | —                               | —                                                                                                                                   |
| `info.severity`                       | `finding.severity`             | —                               | —                                                                                                                                   |
| `info.metadata.*`                     | evidence                       | —                               | —                                                                                                                                   |
| `info.classification.cve-id`          | `cve.value`                    | `upper`                         | each member                                                                                                                         |
| `info.classification.cwe-id`          | `cwe.value`                    | `upper`                         | each member                                                                                                                         |
| `info.classification.cvss-metrics`    | `finding.cvss_vector`          | —                               | —                                                                                                                                   |
| `info.classification.cvss-score`      | `finding.cvss_score`           | `cvss_one_decimal`              | —                                                                                                                                   |
| `info.classification.epss-score`      | `cve.epss_score`               | —                               | —                                                                                                                                   |
| `info.classification.epss-percentile` | `cve.epss_percentile`          | —                               | —                                                                                                                                   |
| `info.classification.cpe`             | evidence                       | —                               | —                                                                                                                                   |
| `info.remediation`                    | evidence                       | —                               | —                                                                                                                                   |
| `matcher-name`                        | non storable                   | —                               | read into finding.matcher by nuclei_matcher on the template-id rows                                                                 |
| `extractor-name`                      | non storable                   | —                               | read into finding.matcher by nuclei_matcher when no matcher-name is present                                                         |
| `type`                                | non storable                   | —                               | selects the finding's parent and the matcher transform                                                                              |
| `host`                                | `subdomain.value`              | `strip_trailing_dot`            | only when `host_is_subdomain`                                                                                                       |
| `host`                                | `domain.value`                 | `strip_trailing_dot`            | only when `host_is_domain`                                                                                                          |
| `port`                                | `port.number`                  | `int_value`                     | —                                                                                                                                   |
| `scheme`                              | `service.name`                 | `scheme_service`                | —                                                                                                                                   |
| `scheme`                              | `service.secure`               | `scheme_secure`                 | —                                                                                                                                   |
| `url`                                 | non storable                   | —                               | redacted before ingest; the scanned input: it can carry the input's query, and for tcp and javascript results it repeats matched-at |
| `path`                                | non storable                   | —                               | the request path, part of the endpoint url                                                                                          |
| `matched-at`                          | `endpoint.url`                 | `url_without_query`             | redacted before ingest; only when `nuclei_http_result`                                                                              |
| `extracted-results`                   | non storable                   | —                               | redacted before ingest; extracted response data, which can be a credential                                                          |
| `request`                             | `endpoint.method`              | `http_request_method`           | redacted before ingest; only when `nuclei_http_result`                                                                              |
| `response`                            | non storable                   | —                               | redacted before ingest; raw response data, which can carry credentials                                                              |
| `meta.*`                              | non storable                   | —                               | redacted before ingest; payload values of the matching request; default-login templates put the tried credentials here              |
| `ip`                                  | `ip_address.value`             | —                               | —                                                                                                                                   |
| `ip`                                  | `ip_address.version`           | `ip_version`                    | —                                                                                                                                   |
| `timestamp`                           | `observed_at` (write metadata) | `rfc3339_utc`                   | —                                                                                                                                   |
| `interaction.*`                       | evidence                       | —                               | documented, absent from the fixture                                                                                                 |
| `curl-command`                        | non storable                   | —                               | redacted before ingest; replays the request, credentials included                                                                   |
| `matcher-status`                      | non storable                   | —                               | always true without -ms; only matched results are written                                                                           |
| `global-matchers`                     | non storable                   | —                               | documented, absent from the fixture; marks a passive global-matcher template; the finding is the same                               |
| `is_fuzzing_result`                   | non storable                   | —                               | implied by fuzzing_parameter                                                                                                        |
| `fuzzing_method`                      | `endpoint.method`              | —                               | —                                                                                                                                   |
| `fuzzing_parameter`                   | `parameter.name`               | —                               | —                                                                                                                                   |
| `fuzzing_position`                    | `parameter.location`           | `dast_location`                 | —                                                                                                                                   |
| `analyzer_details`                    | evidence                       | —                               | documented, absent from the fixture                                                                                                 |

Constants a write carries that no field holds:

| Sink                      | Values     | Reason                                                               |
| ------------------------- | ---------- | -------------------------------------------------------------------- |
| `endpoint.method`         | `"GET"`    | a headless result carries no request; the browser navigates with GET |
| `port.transport`          | `"tcp"`    | http, headless, ssl, tcp and this javascript template all dial TCP   |
| `finding.scanner`         | `"nuclei"` | the tool that reported the finding                                   |
| `source` (write metadata) | `"nuclei"` | the tool that produced the output                                    |

### `nvd_cve`

- **Version:** NVD CVE API 2.0 (schema "JSON Schema for NVD Vulnerability Data API version 2.2.4")
- **Files:** `CVE-2021-44228.json`, `CVE-2025-24813.json`

| Field                                                                             | Sink                           | Transforms                               | Notes                                        |
| --------------------------------------------------------------------------------- | ------------------------------ | ---------------------------------------- | -------------------------------------------- |
| `resultsPerPage`                                                                  | non storable                   | —                                        | paging of the API response                   |
| `startIndex`                                                                      | non storable                   | —                                        | paging of the API response                   |
| `totalResults`                                                                    | non storable                   | —                                        | paging of the API response                   |
| `format`                                                                          | non storable                   | —                                        | the API's constant response format name      |
| `version`                                                                         | non storable                   | —                                        | the API's version, not a fact about the CVE  |
| `timestamp`                                                                       | `observed_at` (write metadata) | `rfc3339_utc`                            | —                                            |
| `vulnerabilities[].cve.id`                                                        | `cve.value`                    | —                                        | —                                            |
| `vulnerabilities[].cve.sourceIdentifier`                                          | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.published`                                                 | `cve.published`                | `rfc3339_utc`                            | —                                            |
| `vulnerabilities[].cve.lastModified`                                              | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.vulnStatus`                                                | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.cveTags`                                                   | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.descriptions[].lang`                                       | non storable                   | —                                        | advisory prose, not an asset fact            |
| `vulnerabilities[].cve.descriptions[].value`                                      | non storable                   | —                                        | advisory prose, not an asset fact            |
| `vulnerabilities[].cve.affected[].source`                                         | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].vendor`                          | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].product`                         | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].version`              | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].lessThan`             | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].versionType`          | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].status`               | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].changes[].at`         | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].changes[].status`     | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].source`                            | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].type`                              | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].cvssData.*`                        | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].cvssData.vectorString`             | `cve.cvss_vector`              | `nvd_preferred_cvss`                     | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].cvssData.baseScore`                | `cve.cvss_score`               | `cvss_one_decimal`, `nvd_preferred_cvss` | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].exploitabilityScore`               | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV31[].impactScore`                       | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].source`                             | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].type`                               | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.version`                   | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.vectorString`              | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.baseScore`                 | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.accessVector`              | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.accessComplexity`          | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.authentication`            | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.confidentialityImpact`     | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.integrityImpact`           | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].cvssData.availabilityImpact`        | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].baseSeverity`                       | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].exploitabilityScore`                | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].impactScore`                        | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].acInsufInfo`                        | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].obtainAllPrivilege`                 | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].obtainUserPrivilege`                | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].obtainOtherPrivilege`               | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.cvssMetricV2[].userInteractionRequired`            | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.ssvcV203[].source`                                 | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.ssvcV203[].ssvcData.timestamp`                     | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.ssvcV203[].ssvcData.id`                            | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.ssvcV203[].ssvcData.options[].*`                   | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.ssvcV203[].ssvcData.role`                          | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.metrics.ssvcV203[].ssvcData.version`                       | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.cisaExploitAdd`                                            | `cve.kev_added`                | —                                        | —                                            |
| `vulnerabilities[].cve.cisaActionDue`                                             | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.cisaRequiredAction`                                        | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.cisaVulnerabilityName`                                     | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.weaknesses[].source`                                       | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.weaknesses[].type`                                         | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.weaknesses[].description[].lang`                           | evidence                       | —                                        | —                                            |
| `vulnerabilities[].cve.weaknesses[].description[].value`                          | `cwe.value`                    | —                                        | —                                            |
| `vulnerabilities[].cve.configurations[].nodes[].operator`                         | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].nodes[].negate`                           | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].nodes[].cpeMatch[].vulnerable`            | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].nodes[].cpeMatch[].criteria`              | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].nodes[].cpeMatch[].versionStartIncluding` | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].nodes[].cpeMatch[].versionEndExcluding`   | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].nodes[].cpeMatch[].matchCriteriaId`       | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.configurations[].operator`                                 | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.references[].url`                                          | non storable                   | —                                        | advisory links, not asset facts              |
| `vulnerabilities[].cve.references[].source`                                       | non storable                   | —                                        | advisory links, not asset facts              |
| `vulnerabilities[].cve.references[].tags`                                         | non storable                   | —                                        | advisory links, not asset facts              |
| `vulnerabilities[].cve.affected[].affectedData[].defaultStatus`                   | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.affected[].affectedData[].versions[].lessThanOrEqual`      | non storable                   | —                                        | advisory applicability data, not asset facts |
| `vulnerabilities[].cve.metrics.cvssMetricV40[].cvssData.baseScore`                | `cve.cvss_score`               | `cvss_one_decimal`, `nvd_preferred_cvss` | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV40[].cvssData.vectorString`             | `cve.cvss_vector`              | `nvd_preferred_cvss`                     | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV40[].cvssData.*`                        | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV40[].source`                            | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV40[].type`                              | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].cvssData.baseScore`                | `cve.cvss_score`               | `cvss_one_decimal`, `nvd_preferred_cvss` | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].cvssData.vectorString`             | `cve.cvss_vector`              | `nvd_preferred_cvss`                     | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].cvssData.*`                        | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].source`                            | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].type`                              | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].exploitabilityScore`               | evidence                       | —                                        | documented, absent from the fixture          |
| `vulnerabilities[].cve.metrics.cvssMetricV30[].impactScore`                       | evidence                       | —                                        | documented, absent from the fixture          |

Constants a write carries that no field holds:

| Sink                      | Values  | Reason                           |
| ------------------------- | ------- | -------------------------------- |
| `source` (write metadata) | `"nvd"` | the API that produced the output |

### `rdap_autnum`

- **Command:** `curl -s -H 'Accept: application/rdap+json' https://rdap.db.ripe.net/autnum/64496` → `as64496.json`
- **Version:** RDAP level 0 with the NRO RDAP profile (`nro_rdap_profile_0`, `nro_rdap_profile_asn_flat_0`) and RFC
- **Files:** `as64496.json`

| Field                           | Sink                      | Transforms              | Notes                                                                                                                                                                                     |
| ------------------------------- | ------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `handle`                        | evidence                  | —                       | —                                                                                                                                                                                         |
| `startAutnum`                   | `asn.value`               | `rdap_single_autnum`    | —                                                                                                                                                                                         |
| `endAutnum`                     | non storable              | —                       | equals startAutnum for one AS; a block of several numbers is not one asn node                                                                                                             |
| `name`                          | `asn.name`                | —                       | —                                                                                                                                                                                         |
| `country`                       | `asn.country`             | —                       | —                                                                                                                                                                                         |
| `type`                          | evidence                  | —                       | documented, absent from the fixture                                                                                                                                                       |
| `status`                        | evidence                  | —                       | —                                                                                                                                                                                         |
| `entities[].handle`             | `organization.handle`     | `rdap_holder_handle`    | —                                                                                                                                                                                         |
| `entities[].vcardArray`         | `organization.name`       | `rdap_holder_name`      | —                                                                                                                                                                                         |
| `entities[].vcardArray`         | `email_address.value`     | `rdap_abuse_email`      | —                                                                                                                                                                                         |
| `entities[].vcardArray`         | `phone.value`             | `rdap_abuse_phone`      | —                                                                                                                                                                                         |
| `entities[].vcardArray`         | non storable              | —                       | maintainer and role names, the holder's postal address and phone and card metadata; person and company types are out of scope, so only the holder fn and the abuse contact leave the card |
| `entities[].roles`              | `has_contact.role` (edge) | `rdap_abuse_role`       | each member                                                                                                                                                                               |
| `entities[].links[].value`      | evidence                  | —                       | —                                                                                                                                                                                         |
| `entities[].links[].rel`        | evidence                  | —                       | —                                                                                                                                                                                         |
| `entities[].links[].href`       | evidence                  | —                       | —                                                                                                                                                                                         |
| `entities[].objectClassName`    | evidence                  | —                       | —                                                                                                                                                                                         |
| `links[].value`                 | evidence                  | —                       | —                                                                                                                                                                                         |
| `links[].rel`                   | evidence                  | —                       | —                                                                                                                                                                                         |
| `links[].href`                  | evidence                  | —                       | —                                                                                                                                                                                         |
| `links[].type`                  | evidence                  | —                       | —                                                                                                                                                                                         |
| `events[].eventAction`          | evidence                  | —                       | —                                                                                                                                                                                         |
| `events[].eventDate`            | evidence                  | —                       | —                                                                                                                                                                                         |
| `rdapConformance`               | evidence                  | —                       | —                                                                                                                                                                                         |
| `notices[].title`               | evidence                  | —                       | —                                                                                                                                                                                         |
| `notices[].description`         | evidence                  | —                       | —                                                                                                                                                                                         |
| `notices[].links[].value`       | evidence                  | —                       | —                                                                                                                                                                                         |
| `notices[].links[].rel`         | evidence                  | —                       | —                                                                                                                                                                                         |
| `notices[].links[].href`        | evidence                  | —                       | —                                                                                                                                                                                         |
| `notices[].links[].type`        | evidence                  | —                       | —                                                                                                                                                                                         |
| `remarks[].description`         | evidence                  | —                       | —                                                                                                                                                                                         |
| `port43`                        | `asn.rir`                 | `rir_from_whois_server` | —                                                                                                                                                                                         |
| `port43`                        | `organization.registry`   | `rir_from_whois_server` | —                                                                                                                                                                                         |
| `objectClassName`               | evidence                  | —                       | —                                                                                                                                                                                         |
| `redacted[].name.description`   | evidence                  | —                       | —                                                                                                                                                                                         |
| `redacted[].reason.description` | evidence                  | —                       | —                                                                                                                                                                                         |
| `redacted[].prePath`            | evidence                  | —                       | —                                                                                                                                                                                         |
| `redacted[].method`             | evidence                  | —                       | —                                                                                                                                                                                         |

Constants a write carries that no field holds:

| Sink                      | Values          | Reason                              |
| ------------------------- | --------------- | ----------------------------------- |
| `source` (write metadata) | `"rdap_autnum"` | the source that produced the output |

### `rdap_domain`

- **Version:** RDAP level 0 with the ICANN gTLD RDAP response profile (`icann_rdap_response_profile_1`); the body is the
- **Files:** `example-nohandle.json`, `example-placeholder.json`, `example.com.json`

| Field                                   | Sink                                      | Transforms                         | Notes                                                                                                                                                                       |
| --------------------------------------- | ----------------------------------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `objectClassName`                       | evidence                                  | —                                  | —                                                                                                                                                                           |
| `handle`                                | `whois_registration.registry_domain_id`   | —                                  | only when `rdap_registration_accepted`                                                                                                                                      |
| `ldhName`                               | `domain.value`                            | `strip_trailing_dot`               | —                                                                                                                                                                           |
| `ldhName`                               | `whois_registration.registry`             | `public_suffix_of`                 | only when `rdap_registration_accepted`                                                                                                                                      |
| `unicodeName`                           | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `variants`                              | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `publicIds[].type`                      | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `publicIds[].identifier`                | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `remarks[].title`                       | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `remarks[].description`                 | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `links[].value`                         | evidence                                  | —                                  | —                                                                                                                                                                           |
| `links[].rel`                           | evidence                                  | —                                  | —                                                                                                                                                                           |
| `links[].href`                          | evidence                                  | —                                  | —                                                                                                                                                                           |
| `links[].type`                          | evidence                                  | —                                  | —                                                                                                                                                                           |
| `status`                                | `whois_registration.epp_status`           | `rdap_status_to_epp`, `sorted_set` | only when `rdap_registration_accepted`                                                                                                                                      |
| `entities[].objectClassName`            | evidence                                  | —                                  | —                                                                                                                                                                           |
| `entities[].handle`                     | evidence                                  | —                                  | —                                                                                                                                                                           |
| `entities[].roles`                      | `has_contact.role` (edge)                 | `rdap_registration_role`           | each member; only when `rdap_registration_accepted`                                                                                                                         |
| `entities[].publicIds[].type`           | non storable                              | —                                  | names the identifier scheme; the gTLD profile's registrar publicId is always the IANA Registrar ID                                                                          |
| `entities[].publicIds[].identifier`     | `registrar.iana_id`                       | `int_value`                        | only when `rdap_registration_accepted`                                                                                                                                      |
| `entities[].vcardArray`                 | `registrar.name`                          | `rdap_registrar_name`              | only when `rdap_registration_accepted`                                                                                                                                      |
| `entities[].vcardArray`                 | `email_address.value`                     | `rdap_registration_contact_email`  | only when `rdap_registration_accepted`                                                                                                                                      |
| `entities[].vcardArray`                 | non storable                              | —                                  | registration contact person names (fn), the registrant org, postal address and phone are personal data; person and company types are out of scope, so they stay in evidence |
| `entities[].entities[].objectClassName` | evidence                                  | —                                  | —                                                                                                                                                                           |
| `entities[].entities[].roles`           | `has_contact.role` (edge)                 | `rdap_abuse_role`                  | each member; only when `rdap_registration_accepted`                                                                                                                         |
| `entities[].entities[].vcardArray`      | `email_address.value`                     | `rdap_abuse_email`                 | only when `rdap_registration_accepted`                                                                                                                                      |
| `entities[].entities[].vcardArray`      | `phone.value`                             | `rdap_abuse_phone`                 | only when `rdap_registration_accepted`                                                                                                                                      |
| `events[].eventAction`                  | non storable                              | —                                  | selects which property the eventDate of the same event feeds                                                                                                                |
| `events[].eventDate`                    | `whois_registration.registration_created` | `rdap_event_registration`          | only when `rdap_registration_accepted`                                                                                                                                      |
| `events[].eventDate`                    | `whois_registration.registration_updated` | `rdap_event_last_changed`          | only when `rdap_registration_accepted`                                                                                                                                      |
| `events[].eventDate`                    | `whois_registration.registration_expires` | `rdap_event_expiration`            | only when `rdap_registration_accepted`                                                                                                                                      |
| `events[].eventDate`                    | `observed_at` (write metadata)            | `rdap_event_database_update`       | —                                                                                                                                                                           |
| `secureDNS.delegationSigned`            | `whois_registration.dnssec_signed`        | —                                  | only when `rdap_registration_accepted`                                                                                                                                      |
| `secureDNS.zoneSigned`                  | non storable                              | —                                  | whether the registry's own zone is signed: a fact of the zone, not of this registration                                                                                     |
| `secureDNS.maxSigLife`                  | non storable                              | —                                  | a signature lifetime a client may request, not a registration fact                                                                                                          |
| `secureDNS.dsData[].keyTag`             | evidence                                  | —                                  | —                                                                                                                                                                           |
| `secureDNS.dsData[].algorithm`          | evidence                                  | —                                  | —                                                                                                                                                                           |
| `secureDNS.dsData[].digestType`         | evidence                                  | —                                  | —                                                                                                                                                                           |
| `secureDNS.dsData[].digest`             | evidence                                  | —                                  | —                                                                                                                                                                           |
| `secureDNS.keyData[].flags`             | evidence                                  | —                                  | documented, absent from the fixture                                                                                                                                         |
| `nameservers[].objectClassName`         | evidence                                  | —                                  | —                                                                                                                                                                           |
| `nameservers[].ldhName`                 | `subdomain.value`                         | `strip_trailing_dot`               | —                                                                                                                                                                           |
| `port43`                                | `whois_registration.whois_server`         | `strip_trailing_dot`               | only when `rdap_registration_accepted`                                                                                                                                      |
| `rdapConformance`                       | evidence                                  | —                                  | —                                                                                                                                                                           |
| `notices[].title`                       | evidence                                  | —                                  | —                                                                                                                                                                           |
| `notices[].description`                 | evidence                                  | —                                  | —                                                                                                                                                                           |
| `notices[].links[].value`               | evidence                                  | —                                  | —                                                                                                                                                                           |
| `notices[].links[].rel`                 | evidence                                  | —                                  | —                                                                                                                                                                           |
| `notices[].links[].href`                | evidence                                  | —                                  | —                                                                                                                                                                           |
| `notices[].links[].type`                | evidence                                  | —                                  | —                                                                                                                                                                           |
| `redacted[].name.type`                  | evidence                                  | —                                  | —                                                                                                                                                                           |
| `redacted[].prePath`                    | evidence                                  | —                                  | —                                                                                                                                                                           |
| `redacted[].postPath`                   | evidence                                  | —                                  | —                                                                                                                                                                           |
| `redacted[].pathLang`                   | evidence                                  | —                                  | —                                                                                                                                                                           |
| `redacted[].method`                     | evidence                                  | —                                  | —                                                                                                                                                                           |
| `redacted[].reason.description`         | evidence                                  | —                                  | —                                                                                                                                                                           |

Constants a write carries that no field holds:

| Sink                      | Values          | Reason                              |
| ------------------------- | --------------- | ----------------------------------- |
| `source` (write metadata) | `"rdap_domain"` | the source that produced the output |

### `rdap_ip`

- **Command:** `curl -s -H 'Accept: application/rdap+json' https://rdap.arin.net/registry/ip/198.51.100.0` → `ip-198.51.100.0.json`
- **Version:** RDAP level 0 with the NRO RDAP profile (`nro_rdap_profile_0`) and the `cidr0` extension; the body is
- **Files:** `ip-198.51.100.0.json`

| Field                                         | Sink                      | Transforms              | Notes                                                                                                                                                                  |
| --------------------------------------------- | ------------------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rdapConformance`                             | evidence                  | —                       | —                                                                                                                                                                      |
| `notices[].title`                             | evidence                  | —                       | —                                                                                                                                                                      |
| `notices[].description`                       | evidence                  | —                       | —                                                                                                                                                                      |
| `notices[].links[].value`                     | evidence                  | —                       | —                                                                                                                                                                      |
| `notices[].links[].rel`                       | evidence                  | —                       | —                                                                                                                                                                      |
| `notices[].links[].href`                      | evidence                  | —                       | —                                                                                                                                                                      |
| `notices[].links[].type`                      | evidence                  | —                       | —                                                                                                                                                                      |
| `objectClassName`                             | evidence                  | —                       | —                                                                                                                                                                      |
| `handle`                                      | evidence                  | —                       | —                                                                                                                                                                      |
| `startAddress`                                | `ip_cidr.value`           | `rdap_range_cidr`       | —                                                                                                                                                                      |
| `startAddress`                                | `ip_cidr.version`         | `ip_version`            | —                                                                                                                                                                      |
| `endAddress`                                  | non storable              | —                       | closes the range startAddress opens; the network value carries both bounds                                                                                             |
| `ipVersion`                                   | non storable              | —                       | restates the version the network value fixes                                                                                                                           |
| `name`                                        | `ip_cidr.netname`         | —                       | —                                                                                                                                                                      |
| `type`                                        | evidence                  | —                       | —                                                                                                                                                                      |
| `country`                                     | `ip_cidr.country`         | —                       | —                                                                                                                                                                      |
| `parentHandle`                                | evidence                  | —                       | —                                                                                                                                                                      |
| `cidr0_cidrs[].v4prefix`                      | evidence                  | —                       | —                                                                                                                                                                      |
| `cidr0_cidrs[].v6prefix`                      | evidence                  | —                       | documented, absent from the fixture                                                                                                                                    |
| `cidr0_cidrs[].length`                        | evidence                  | —                       | —                                                                                                                                                                      |
| `status`                                      | evidence                  | —                       | —                                                                                                                                                                      |
| `port43`                                      | `ip_cidr.rir`             | `rir_from_whois_server` | —                                                                                                                                                                      |
| `port43`                                      | `organization.registry`   | `rir_from_whois_server` | —                                                                                                                                                                      |
| `events[].eventAction`                        | evidence                  | —                       | —                                                                                                                                                                      |
| `events[].eventDate`                          | evidence                  | —                       | —                                                                                                                                                                      |
| `links[].value`                               | evidence                  | —                       | —                                                                                                                                                                      |
| `links[].rel`                                 | evidence                  | —                       | —                                                                                                                                                                      |
| `links[].href`                                | evidence                  | —                       | —                                                                                                                                                                      |
| `links[].type`                                | evidence                  | —                       | —                                                                                                                                                                      |
| `remarks[].title`                             | evidence                  | —                       | —                                                                                                                                                                      |
| `remarks[].description`                       | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].objectClassName`                  | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].handle`                           | `organization.handle`     | `rdap_holder_handle`    | —                                                                                                                                                                      |
| `entities[].roles`                            | non storable              | —                       | selects the holder entity (registrant, kind org) that operated_by points at                                                                                            |
| `entities[].vcardArray`                       | `organization.name`       | `rdap_holder_name`      | —                                                                                                                                                                      |
| `entities[].vcardArray`                       | non storable              | —                       | the holder's postal address and card metadata; only its fn names the organization                                                                                      |
| `entities[].port43`                           | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].events[].eventAction`             | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].events[].eventDate`               | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].links[].value`                    | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].links[].rel`                      | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].links[].href`                     | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].links[].type`                     | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].objectClassName`       | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].handle`                | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].roles`                 | `has_contact.role` (edge) | `rdap_abuse_role`       | each member                                                                                                                                                            |
| `entities[].entities[].vcardArray`            | `email_address.value`     | `rdap_abuse_email`      | —                                                                                                                                                                      |
| `entities[].entities[].vcardArray`            | `phone.value`             | `rdap_abuse_phone`      | —                                                                                                                                                                      |
| `entities[].entities[].vcardArray`            | non storable              | —                       | the contact card's fn, org and postal address; a point-of-contact name can be a person's name, and person and company types are out of scope, so they stay in evidence |
| `entities[].entities[].port43`                | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].status`                | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].events[].eventAction`  | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].events[].eventDate`    | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].links[].value`         | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].links[].rel`           | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].links[].href`          | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].links[].type`          | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].remarks[].title`       | evidence                  | —                       | —                                                                                                                                                                      |
| `entities[].entities[].remarks[].description` | evidence                  | —                       | —                                                                                                                                                                      |

Constants a write carries that no field holds:

| Sink                      | Values      | Reason                              |
| ------------------------- | ----------- | ----------------------------------- |
| `source` (write metadata) | `"rdap_ip"` | the source that produced the output |

### `subfinder`

- **Version:** subfinder, `dev` branch at commit `20d140a`
- **Files:** `output.jsonl`, `sources.jsonl`

| Field                  | Sink                 | Transforms           | Notes                                                                                                                                                                              |
| ---------------------- | -------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `host`                 | `subdomain.value`    | `strip_trailing_dot` | —                                                                                                                                                                                  |
| `ip`                   | `ip_address.value`   | —                    | —                                                                                                                                                                                  |
| `ip`                   | `ip_address.version` | `ip_version`         | —                                                                                                                                                                                  |
| `input`                | `domain.value`       | `strip_trailing_dot` | —                                                                                                                                                                                  |
| `source`               | evidence             | —                    | the passive source that reported the name: provenance, kept with the output                                                                                                        |
| `sources`              | evidence             | —                    | the passive sources that reported the name (-cs): provenance                                                                                                                       |
| `wildcard_certificate` | non storable         | —                    | a source listed `*.<host>` in certificate data; that says a certificate covers names below the host, not that random labels resolve, which is what the `wildcard` property records |

Constants a write carries that no field holds:

| Sink                      | Values        | Reason                            |
| ------------------------- | ------------- | --------------------------------- |
| `source` (write metadata) | `"subfinder"` | the tool that produced the output |

### `tlsx`

- **Command:** `tlsx -l hosts.txt -p 443,8443 -sm ztls -json -san -cn -so -tv -cipher -hash sha256 -jarm -ja3 -ja3s -tps -se -ve -ce -re -o output.jsonl`
- **Version:** tlsx v1.4.0 (commit `ffe1cfe`)
- **Files:** `output.jsonl`

| Field                            | Sink                                        | Transforms                           | Notes                                                                                                                  |
| -------------------------------- | ------------------------------------------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `timestamp`                      | `observed_at` (write metadata)              | `rfc3339_utc`                        | —                                                                                                                      |
| `host`                           | `subdomain.value`                           | `strip_trailing_dot`, `if_subdomain` | —                                                                                                                      |
| `host`                           | `domain.value`                              | `strip_trailing_dot`, `if_domain`    | —                                                                                                                      |
| `ip`                             | `ip_address.value`                          | —                                    | —                                                                                                                      |
| `ip`                             | `ip_address.version`                        | `ip_version`                         | —                                                                                                                      |
| `port`                           | `port.number`                               | `int_value`                          | —                                                                                                                      |
| `probe_status`                   | non storable                                | —                                    | only successful probes are written                                                                                     |
| `tls_version`                    | `tls_cipher_suite.version`                  | —                                    | —                                                                                                                      |
| `cipher`                         | `tls_cipher_suite.name`                     | —                                    | —                                                                                                                      |
| `tls_connection`                 | non storable                                | —                                    | the TLS library tlsx used (ctls, ztls, openssl), not a target fact                                                     |
| `key_exchange`                   | evidence                                    | —                                    | documented, absent from the fixture; set in ctls mode only; the command uses -sm ztls                                  |
| `jarm_hash`                      | `tls_fingerprint.value`                     | —                                    | —                                                                                                                      |
| `ja3s_hash`                      | `tls_fingerprint.value`                     | —                                    | —                                                                                                                      |
| `ja3_hash`                       | non storable                                | —                                    | the JA3 of tlsx's own ClientHello: it fingerprints the scanner, not the target                                         |
| `sni`                            | `presents_certificate.server_name` (edge)   | —                                    | —                                                                                                                      |
| `version_enum`                   | evidence                                    | —                                    | restates the versions that cipher_enum lists                                                                           |
| `cipher_enum[].version`          | `tls_cipher_suite.version`                  | —                                    | —                                                                                                                      |
| `cipher_enum[].ciphers.secure`   | `tls_cipher_suite.name`                     | —                                    | each member                                                                                                            |
| `cipher_enum[].ciphers.weak`     | `tls_cipher_suite.name`                     | —                                    | each member                                                                                                            |
| `cipher_enum[].ciphers.insecure` | `tls_cipher_suite.name`                     | —                                    | each member                                                                                                            |
| `cipher_enum[].ciphers.unknown`  | `tls_cipher_suite.name`                     | —                                    | each member; documented, absent from the fixture                                                                       |
| `client_cert_required`           | evidence                                    | —                                    | whether the handshake asked for a client certificate; no catalog property holds it                                     |
| `expired`                        | non storable                                | —                                    | derivable from not_after at read time                                                                                  |
| `self_signed`                    | `certificate.self_signed`                   | —                                    | —                                                                                                                      |
| `mismatched`                     | `presents_certificate.name_mismatch` (edge) | —                                    | —                                                                                                                      |
| `revoked`                        | non storable                                | —                                    | documented, absent from the fixture; depends on the observer's CRL and OCSP state at scan time; emitted only when true |
| `untrusted`                      | non storable                                | —                                    | depends on the observer's trust store at scan time                                                                     |
| `not_before`                     | `certificate.not_before`                    | `rfc3339_utc`                        | —                                                                                                                      |
| `not_after`                      | `certificate.not_after`                     | `rfc3339_utc`                        | —                                                                                                                      |
| `subject_dn`                     | non storable                                | —                                    | carries the subject organization, a company name kept out by the company exclusion; subject_cn holds the name          |
| `subject_cn`                     | `certificate.subject_cn`                    | —                                    | —                                                                                                                      |
| `subject_org`                    | non storable                                | —                                    | company names, against the spirit of the company exclusion                                                             |
| `subject_an`                     | `covers_name.coverage` (edge)               | `san_coverage`                       | each member                                                                                                            |
| `subject_an`                     | `subdomain.value`                           | `san_base_name`, `if_subdomain`      | each member                                                                                                            |
| `subject_an`                     | `domain.value`                              | `san_base_name`, `if_domain`         | each member                                                                                                            |
| `serial`                         | `certificate.serial`                        | `colon_hex_serial_to_lower_hex`      | —                                                                                                                      |
| `issuer_dn`                      | `certificate.issuer_dn`                     | —                                    | —                                                                                                                      |
| `issuer_cn`                      | non storable                                | —                                    | derivable from issuer_dn                                                                                               |
| `issuer_org`                     | non storable                                | —                                    | company names, against the spirit of the company exclusion                                                             |
| `emails`                         | evidence                                    | —                                    | documented, absent from the fixture; email SANs; no certificate relation targets a mailbox                             |
| `fingerprint_hash.sha256`        | `certificate.der_sha256`                    | —                                    | —                                                                                                                      |
| `fingerprint_hash.md5`           | non storable                                | —                                    | der_sha256 is the identity; a weaker digest of the same bytes                                                          |
| `fingerprint_hash.sha1`          | non storable                                | —                                    | der_sha256 is the identity; a weaker digest of the same bytes                                                          |
| `wildcard_certificate`           | non storable                                | —                                    | implied by a `*.` SAN, which covers_name records with coverage wildcard                                                |

Constants a write carries that no field holds:

| Sink                                       | Values             | Reason                                                                                                      |
| ------------------------------------------ | ------------------ | ----------------------------------------------------------------------------------------------------------- |
| `port.transport`                           | `"tcp"`            | tlsx connects over TCP                                                                                      |
| `service.name`                             | `"unknown"`        | tlsx completes a TLS handshake but identifies no application protocol; `unknown` is the registry's sentinel |
| `service.secure`                           | `true`             | tlsx completed a TLS handshake on the port                                                                  |
| `tls_fingerprint.kind`                     | `"jarm"`, `"ja3s"` | the field name, jarm_hash or ja3s_hash, fixes the fingerprint kind                                          |
| `presents_certificate.mode` (edge)         | `"tls"`            | tlsx speaks TLS directly                                                                                    |
| `presents_certificate.server_name` (edge)  | `""`               | an IP target sends no SNI, so tlsx omits `sni`; the explicit empty name records that                        |
| `presents_certificate.alpn_offered` (edge) | `[]`               | the ztls client config sets no NextProtos, so tlsx's ClientHello offers no ALPN                             |
| `source` (write metadata)                  | `"tlsx"`           | the tool that produced the output                                                                           |

### `trufflehog`

- **Version:** trufflehog v3.97.6
- **Files:** `git.jsonl`, `s3.jsonl`

| Field                                           | Sink                             | Transforms                             | Notes                                                                                                    |
| ----------------------------------------------- | -------------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `SourceID`                                      | non storable                     | —                                      | the scan's internal source counter, meaningless outside the run                                          |
| `SourceType`                                    | non storable                     | —                                      | the numeric protobuf enum behind SourceName                                                              |
| `SourceName`                                    | evidence                         | —                                      | —                                                                                                        |
| `DetectorType`                                  | non storable                     | —                                      | the numeric protobuf enum behind DetectorName                                                            |
| `DetectorName`                                  | `secret.detector`                | `detector_token`                       | —                                                                                                        |
| `DetectorName`                                  | `secret.kind`                    | `trufflehog_secret_kind`               | —                                                                                                        |
| `DetectorDescription`                           | evidence                         | —                                      | —                                                                                                        |
| `DecoderName`                                   | evidence                         | —                                      | —                                                                                                        |
| `Verified`                                      | `secret.verified`                | —                                      | —                                                                                                        |
| `Verified`                                      | `authenticates.verified` (edge)  | —                                      | —                                                                                                        |
| `VerificationError`                             | evidence                         | —                                      | documented, absent from the fixture                                                                      |
| `VerificationFromCache`                         | evidence                         | —                                      | —                                                                                                        |
| `Raw`                                           | `secret.value_sha256`            | `trufflehog_secret_part`, `sha256_hex` | redacted before ingest                                                                                   |
| `Raw`                                           | `secret.key_id`                  | `trufflehog_public_part`               | redacted before ingest                                                                                   |
| `RawV2`                                         | non storable                     | —                                      | redacted before ingest; the key id and the secret joined; the digest and key_id carry what may be stored |
| `Redacted`                                      | non storable                     | —                                      | redacted before ingest; the display form of the credential; key_id carries the public half               |
| `ExtraData.resource_type`                       | evidence                         | —                                      | —                                                                                                        |
| `ExtraData.rotation_guide`                      | evidence                         | —                                      | —                                                                                                        |
| `ExtraData.account`                             | `cloud_account.account_id`       | —                                      | —                                                                                                        |
| `ExtraData.user_id`                             | evidence                         | —                                      | —                                                                                                        |
| `ExtraData.arn`                                 | `cloud_account.account_id`       | `arn_account`                          | —                                                                                                        |
| `ExtraData.*`                                   | evidence                         | —                                      | documented, absent from the fixture                                                                      |
| `StructuredData`                                | evidence                         | —                                      | —                                                                                                        |
| `SecretParts.*`                                 | non storable                     | —                                      | redacted before ingest; the credential's raw components, secret included                                 |
| `SourceMetadata.Data.Git.commit`                | evidence                         | —                                      | —                                                                                                        |
| `SourceMetadata.Data.Git.file`                  | `exposes_secret.location` (edge) | `trufflehog_git_location`              | —                                                                                                        |
| `SourceMetadata.Data.Git.email`                 | non storable                     | —                                      | commit author name and address: person data                                                              |
| `SourceMetadata.Data.Git.repository`            | `repository.host`                | `url_host`                             | —                                                                                                        |
| `SourceMetadata.Data.Git.repository`            | `repository.owner`               | `repo_url_owner`                       | —                                                                                                        |
| `SourceMetadata.Data.Git.repository`            | `repository.name`                | `repo_url_name`                        | —                                                                                                        |
| `SourceMetadata.Data.Git.timestamp`             | evidence                         | —                                      | —                                                                                                        |
| `SourceMetadata.Data.Git.line`                  | evidence                         | —                                      | —                                                                                                        |
| `SourceMetadata.Data.Git.repository_local_path` | non storable                     | —                                      | the scanner's temporary clone directory                                                                  |
| `SourceMetadata.Data.S3.bucket`                 | `storage_bucket.name`            | —                                      | —                                                                                                        |
| `SourceMetadata.Data.S3.file`                   | `exposes_secret.location` (edge) | —                                      | —                                                                                                        |
| `SourceMetadata.Data.S3.link`                   | evidence                         | —                                      | —                                                                                                        |
| `SourceMetadata.Data.S3.email`                  | non storable                     | —                                      | the object owner's display name: person or account holder data                                           |
| `SourceMetadata.Data.S3.timestamp`              | evidence                         | —                                      | —                                                                                                        |

Constants a write carries that no field holds:

| Sink                            | Values         | Reason                                                            |
| ------------------------------- | -------------- | ----------------------------------------------------------------- |
| `repository.platform`           | `"github"`     | the git remote's host is github.com                               |
| `cloud_account.provider`        | `"aws"`        | the AWS detector names an AWS account                             |
| `storage_bucket.provider`       | `"aws_s3"`     | the s3 source reads Amazon S3 buckets                             |
| `authenticates.username` (edge) | `""`           | an access key logs in as itself; trufflehog reports no login name |
| `authenticates.breach` (edge)   | `""`           | a scanner hit is not published in a breach corpus                 |
| `source` (write metadata)       | `"trufflehog"` | the tool that produced the output                                 |

### `whois_gtld`

- **Command:** `whois -h whois.verisign-grs.com example.com` → `example.com.txt`
- **Version:** the registry's port-43 output in the ICANN RDDS labeling-policy key names; the client version does not
- **Files:** `example.com.txt`

| Field                                    | Sink                                      | Transforms                          | Notes                                                                                                                                                                                                    |
| ---------------------------------------- | ----------------------------------------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Domain Name`                            | `domain.value`                            | `strip_trailing_dot`                | —                                                                                                                                                                                                        |
| `Domain Name`                            | `whois_registration.registry`             | `public_suffix_of`                  | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registry Domain ID`                     | `whois_registration.registry_domain_id`   | —                                   | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registrar WHOIS Server`                 | non storable                              | —                                   | the registrar's WHOIS server, a registrar-level fact: registrar is keyed by IANA id and gains no attribute, and whois_server holds only the registry's server (RDAP port43); the value stays in evidence |
| `Registrar URL`                          | non storable                              | —                                   | a registrar-level fact; registrar gains no attribute, so the value stays in evidence                                                                                                                     |
| `Updated Date`                           | `whois_registration.registration_updated` | `rfc3339_utc`                       | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Creation Date`                          | `whois_registration.registration_created` | `rfc3339_utc`                       | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registry Expiry Date`                   | `whois_registration.registration_expires` | `rfc3339_utc`                       | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registrar Registration Expiration Date` | non storable                              | —                                   | documented, absent from the fixture; the registrar's own expiry in registrar output; the registration keeps the registry's expiry                                                                        |
| `Registrar`                              | `registrar.name`                          | —                                   | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registrar IANA ID`                      | `registrar.iana_id`                       | `int_value`                         | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registrar Abuse Contact Email`          | `email_address.value`                     | —                                   | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Registrar Abuse Contact Phone`          | `phone.value`                             | `whois_phone_e164`                  | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Reseller`                               | non storable                              | —                                   | documented, absent from the fixture; a reseller company name; company types are out of scope                                                                                                             |
| `Registry Registrant ID`                 | non storable                              | —                                   | documented, absent from the fixture; a registrant contact handle, usually REDACTED; person types are out of scope                                                                                        |
| `Domain Status`                          | `whois_registration.epp_status`           | `whois_status_to_epp`, `sorted_set` | only when `whois_registration_accepted`                                                                                                                                                                  |
| `Name Server`                            | `subdomain.value`                         | `strip_trailing_dot`                | each member                                                                                                                                                                                              |
| `DNSSEC`                                 | `whois_registration.dnssec_signed`        | `whois_dnssec_bool`                 | only when `whois_registration_accepted`                                                                                                                                                                  |

Constants a write carries that no field holds:

| Sink                      | Values         | Reason                                                               |
| ------------------------- | -------------- | -------------------------------------------------------------------- |
| `has_contact.role` (edge) | `"abuse"`      | the Registrar Abuse Contact Email and Phone keys name the abuse role |
| `source` (write metadata) | `"whois_gtld"` | the source that produced the output                                  |

## Transforms

The closed set a mapping may apply, left to right, from `scripts/asm_transforms.py`:

| Transform                         | What it does                                                                                                      |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `arn_account`                     | The 12-digit account of an AWS ARN.                                                                               |
| `asn_number`                      | `AS16509`, as asnmap and httpx print it, or a bare number, as the catalog's integer.                              |
| `bbot_dns_name`                   | The name a BBOT DNS_NAME stands for, lowercased without a root dot; `_wildcard.<zone>` becomes `<zone>`.          |
| `bbot_nuclei_matcher`             | The `name: [<matcher>]` part of a BBOT nuclei description.                                                        |
| `bbot_nuclei_template`            | BBOT's nuclei module puts `template: [<id>]` in the description; lift it into `nuclei:<id>`.                      |
| `bbot_nuclei_template_id`         | The bare template id in a BBOT nuclei description, the one human-readable rule name BBOT keeps.                   |
| `bbot_rule`                       | A BBOT module name as a finding rule: `bbot:<module>`.                                                            |
| `bbot_status_tag`                 | The HTTP status in a BBOT URL event's `status-<code>` tag; None when the tags carry none.                         |
| `bbot_trufflehog_detector`        | `TruffleHog - AWS` becomes the finding rule `trufflehog:aws`.                                                     |
| `bbot_trufflehog_secret_part`     | The secret half of the credential a BBOT trufflehog description quotes, before redaction.                         |
| `bbot_wildcard_answer`            | A real DNS_NAME's tags: `wildcard` is true, `wildcard-possible` false, anything else None.                        |
| `bbot_wildcard_zone`              | True for a `_wildcard.<zone>` DNS_NAME tagged `wildcard`, whose zone answers random labels; else None.            |
| `bbot_without_extracted_data`     | A BBOT nuclei description without the ` Extracted Data: [...]` suffix, which can quote a secret.                  |
| `cdncheck_protection_kind`        | A cdncheck item type as a `protected_by` kind: `cdn` and `waf` keep their name, `cloud` (hosting) is None.        |
| `cloud_region`                    | The region a canonical provider default hostname encodes, as the catalog reads it; None when it has none.         |
| `cloud_service`                   | The cloud_resource service whose canonical default hostname this is, as the catalog decides it.                   |
| `cloud_service_or_none`           | `cloud_service` for a canonical provider default hostname; None for any other name.                               |
| `colon_hex_serial_to_lower_hex`   | Tlsx may print `0A:BC:...`; the catalog stores lowercase hex without separators or leading zeros.                 |
| `cpe22_uri_to_cpe23`              | Nmap prints `cpe:/a:vendor:product:version`; bind it as a CPE 2.3 formatted string.                               |
| `cpe_product_level`               | Set the version attribute of a CPE 2.3 string to `*`, for the shared technology node.                             |
| `cvss_one_decimal`                | A CVSS score as published, rounded to its one decimal; an integral score stays an integer.                        |
| `dast_location`                   | A nuclei `fuzzing_position` as a parameter location.                                                              |
| `detector_token`                  | A scanner rule or detector name as a tech_token: lowercase, runs of other characters `-`.                         |
| `digest16`                        | First 16 lowercase hex characters of the SHA-256 of the exact UTF-8 bytes, a bounded discriminator.               |
| `dmarc_owner_name`                | Drop the leading `_dmarc` label of a query name: RFC 7489 publishes it for the name below.                        |
| `epoch_utc`                       | A float epoch, as BBOT 3 prints `timestamp`, in the catalog's UTC spelling.                                       |
| `float_value`                     | Parse a numeric string, such as an EPSS `epss` field, into a number.                                              |
| `gitleaks_location`               | A gitleaks finding's `File` joined with its `StartLine` as `<file>:<line>`.                                       |
| `gitleaks_secret_kind`            | The secret `kind` of a gitleaks `RuleID`, from a closed per-rule table.                                           |
| `go_time_string_utc`              | Go's default `time.Time.String()` (`2006-01-02 15:04:05.999999999 -0700 MST`), as asnmap prints it, in UTC.       |
| `hex_prefix16`                    | The first 16 characters of a hex digest.                                                                          |
| `http_request_method`             | The method token of a raw HTTP request's first line.                                                              |
| `if_cloud_hostname`               | A canonical provider default hostname, lowercased without the root dot; None for any other name.                  |
| `if_dmarc_record`                 | A TXT value `dmarc_record` accepts; None for any other TXT value.                                                 |
| `if_domain`                       | A name the catalog accepts as a registrable `domain`, lowercased without the root dot; None otherwise.            |
| `if_ip_input`                     | The value when the record's `input` is an IP literal, so the tool classified that address; None otherwise.        |
| `if_name_input`                   | The value when the record's `input` is a DNS name, so the tool classified the name; None otherwise.               |
| `if_spf_record`                   | A TXT value `spf_record` accepts; None for any other TXT value.                                                   |
| `if_subdomain`                    | A name the catalog accepts as a `subdomain`, lowercased without the root dot; None otherwise.                     |
| `if_txt_record`                   | A TXT value `txt_record` accepts; None when a dedicated type such as `spf_record` claims it.                      |
| `int_value`                       | Parse a decimal string, such as nuclei's string `port`, into an integer.                                          |
| `ip_version`                      | 4 or 6, read off an address or network spelling.                                                                  |
| `leakcheck_breach_token`          | A LeakCheck `source.name` as a breach token: `leakcheck:<slug>`. LeakCheck has no separate id.                    |
| `lower`                           | Lowercase a string.                                                                                               |
| `media_type_essence`              | `text/html; charset=utf-8` becomes `text/html`.                                                                   |
| `mta_sts_mx_lines`                | Collect the repeated `mx:` values of a policy file into a list.                                                   |
| `nmap_ssh_hostkey_sha256`         | The one `SHA256:<base64>` fingerprint in an ssh-hostkey `output` (`ssh_hostkey=sha256`); None unless exactly one. |
| `nmap_tunnel_secure`              | Nmap's service `tunnel="ssl"`: the service runs over TLS. Nmap prints no other tunnel value.                      |
| `nuclei_matcher`                  | The nuclei discriminator: `matcher-name`, else `extractor-name`, else empty.                                      |
| `nuclei_rule`                     | A nuclei template id as a finding rule: `nuclei:<template-id>`.                                                   |
| `nvd_preferred_cvss`              | Keep a (rounded) base score or vector only if the preferred metric carries it, else None.                         |
| `openid_tenant_id`                | The tenant UUID inside an Entra OpenID `issuer` or `token_endpoint` URL.                                          |
| `openssh_b64_to_hex`              | Convert OpenSSH's `SHA256:<base64>` fingerprint into 64 lowercase hex characters.                                 |
| `partial_date`                    | Keep `YYYY`, `YYYY-MM` or `YYYY-MM-DD` exactly as precise as the source gives it.                                 |
| `public_suffix_of`                | The zone a registrable domain is registered in: the name without its first label.                                 |
| `rdap_abuse_email`                | The vCard `email` of an entity with the `abuse` role; None for other entities or an empty email.                  |
| `rdap_abuse_phone`                | The first international vCard `tel` of an `abuse` entity as E.164; None for other entities.                       |
| `rdap_abuse_role`                 | The RDAP role `abuse` as the `has_contact` role; None for any other role.                                         |
| `rdap_event_database_update`      | The eventDate of the ICANN-profile `last update of RDAP database` event in UTC; None otherwise.                   |
| `rdap_event_expiration`           | The eventDate of the RDAP `expiration` event in UTC; None for another event's date.                               |
| `rdap_event_last_changed`         | The eventDate of the RDAP `last changed` event in UTC; None for another event's date.                             |
| `rdap_event_registration`         | The eventDate of the RDAP `registration` event in UTC; None for another event's date.                             |
| `rdap_holder_handle`              | The handle of the number resource holder (role `registrant`, kind `org`); None for other entities.                |
| `rdap_holder_name`                | The vCard `fn` of the number resource holder (role `registrant`, kind `org`); None for other entities.            |
| `rdap_range_cidr`                 | The one network an RDAP `startAddress` and its `endAddress` span; None when the range is not one prefix.          |
| `rdap_registrar_name`             | The vCard `fn` of the entity holding this vcardArray when it has the `registrar` role; None otherwise.            |
| `rdap_registration_contact_email` | The vCard `email` of a registrant, administrative, technical or billing entity; None for others.                  |
| `rdap_registration_role`          | An RDAP registration contact role as the `has_contact` role (`technical` is `tech`); None for others.             |
| `rdap_single_autnum`              | An RDAP `startAutnum` that equals its `endAutnum`, one AS; None for a block of several.                           |
| `rdap_status_to_epp`              | RFC 8056: RDAP `client transfer prohibited` becomes EPP `clientTransferProhibited`.                               |
| `repo_url_name`                   | The second path segment of a GitHub repository, clone or file URL, without `.git`, lowercased: the name.          |
| `repo_url_owner`                  | The first path segment of a GitHub repository, clone or file URL, lowercased: the owner.                          |
| `rfc3339_utc`                     | Normalize a timestamp to the catalog's `YYYY-MM-DDTHH:MM:SS[.f]Z`; a naive one is taken as UTC.                   |
| `rir_from_whois_server`           | The RIR whose port-43 server an RDAP number object names (`whois.arin.net` is `arin`); others fail.               |
| `rr_caa_iodef_email`              | The lowercased address of a CAA `iodef` `mailto:` URL; None for another record or URL scheme.                     |
| `rr_caa_iodef_url`                | The `https:` URL of a CAA `iodef` record, exactly as published; None for another record or scheme.                |
| `rr_caa_issue_flags`              | The flags of a CAA `issue` record in presentation form; None for another record.                                  |
| `rr_caa_issue_parameters`         | The parameters of a CAA `issue` record as `{name, value}` pairs, names lowercased; None otherwise.                |
| `rr_caa_issuer`                   | The issuer domain of a CAA `issue` or `issuewild` record; None for another record or an empty issuer (`;`).       |
| `rr_caa_issuewild_flags`          | The flags of a CAA `issuewild` record in presentation form; None for another record.                              |
| `rr_caa_issuewild_parameters`     | The parameters of a CAA `issuewild` record as `{name, value}` pairs, names lowercased; None otherwise.            |
| `rr_mx_preference`                | The preference of an MX record printed `<owner> <ttl> IN MX 10 <exchange>`; None for another record.              |
| `san_base_name`                   | A certificate SAN without its `*.` wildcard label, lowercased: the name `covers_name` targets.                    |
| `san_coverage`                    | `wildcard` for a `*.` SAN and `exact` for a literal one: the `covers_name` coverage.                              |
| `scheme_secure`                   | Whether a URL scheme runs over TLS.                                                                               |
| `scheme_service`                  | `http` and `https` both speak the registry's `http` service; TLS is the `secure` flag.                            |
| `sha256_hex`                      | Lowercase hex SHA-256 of the UTF-8 bytes, untrimmed; CRLF becomes LF first, nothing else changes.                 |
| `sni_matcher`                     | For a TLS result on a named host, `<host>:<matcher>`, so two virtual hosts stay two findings.                     |
| `sorted_set`                      | Sort ascending and drop duplicates: the one spelling of a set-like array.                                         |
| `ssh_host_key_algorithm`          | A key type the `host_key` algorithm enum names, as ssh-hostkey's `type` element prints it; None otherwise.        |
| `strip_trailing_dot`              | Drop the root dot of a fully qualified DNS name and lowercase it.                                                 |
| `trufflehog_git_location`         | A trufflehog git result's `file` joined with its `line` as `<file>:<line>`.                                       |
| `trufflehog_public_part`          | The public identifier half of a two-part credential; an unknown family fails the mapping.                         |
| `trufflehog_secret_kind`          | The secret `kind` of a trufflehog `DetectorName`, from a closed per-family table.                                 |
| `trufflehog_secret_part`          | The secret material of a trufflehog result, never a public key id and never `RawV2` whole.                        |
| `upper`                           | Uppercase a string, such as the `cve-2021-44228` nuclei lowercases, back to MITRE's spelling.                     |
| `url_host`                        | The lowercase host of an absolute URL, without port or brackets.                                                  |
| `url_with_path`                   | Give a bare origin (`https://example.com`) the root path the catalog requires.                                    |
| `url_without_query`               | Drop the query and fragment of a URL; the catalog keeps one endpoint per path.                                    |
| `wappalyzer_name_slug`            | `Nginx:1.18.0` or `Microsoft ASP.NET` becomes a tech_token: lowercase, runs of other characters `-`.              |
| `wappalyzer_version`              | The version half of an httpx `Name:Version` technology entry; None when it reports no version.                    |
| `whois_dnssec_bool`               | `unsigned` is false and `signedDelegation` true; any other spelling fails the mapping.                            |
| `whois_phone_e164`                | ICANN's port-43 `+1.7035550100` phone spelling as E.164 `+17035550100`; a national number fails.                  |
| `whois_status_to_epp`             | Keep the EPP code, the first token, of each WHOIS `Domain Status:` line.                                          |

## Conditions

A row with a condition writes its value only when the record meets it, and otherwise leaves the value in evidence:

| Condition                       | Meaning                                                                                               |
| ------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `bbot_names_domain`             | A BBOT DNS_NAME stands for a registrable domain, reading `_wildcard.<zone>` as its zone.              |
| `bbot_names_subdomain`          | A BBOT DNS_NAME stands for a subdomain, reading `_wildcard.<zone>` as its zone.                       |
| `bbot_not_wildcard_placeholder` | A BBOT event names a real host, not the `_wildcard.<zone>` stand-in whose answers belong to the zone. |
| `host_is_domain`                | The record's `host` is a registrable domain.                                                          |
| `host_is_subdomain`             | The record's `host` is a name below a registrable domain, not an IP literal.                          |
| `nuclei_http_result`            | A nuclei result of an `http` or `headless` template, whose parent is the matched endpoint.            |
| `nuclei_not_ssl_result`         | A nuclei result of any template type but `ssl`.                                                       |
| `nuclei_ssl_result`             | A nuclei result of an `ssl` template, whose matcher carries the SNI host.                             |
| `rdap_registration_accepted`    | An RDAP domain object carries a real handle: present, accepted, and not listed in `redacted`.         |
| `whois_registration_accepted`   | A port-43 response carries a real `Registry Domain ID`.                                               |
