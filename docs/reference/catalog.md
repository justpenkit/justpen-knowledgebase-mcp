# Catalog reference

Every table below is generated from catalog v3, the same manifest the server validates writes against. `kb_types` returns the identical contract and the same descriptions at runtime and is the source to read from a client; this page exists so the contract is reviewable without a running server. Regenerate it with `make docs-catalog`.

The catalog declares **31 node types** and **44 relation types**. Conventions that the catalog does not enforce, and the longer reasoning behind each type, live in [Graph and search](../tools/graph.md).

## What a write is checked against

| Gate                | Declared in                                        | Rejects                                                                                                                                                       |
| ------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Type name           | `nodes` / `relations` keys                         | An unknown type, on write and on schema lookup.                                                                                                               |
| Required properties | `required` map                                     | A missing required property, or one whose value fails its rule. Properties outside the map are stored as submitted and are not validated.                     |
| Format rule         | `formats`, the validator, and the discovery schema | A value that does not match the published spelling. A rule missing from any of the three places would silently weaken the contract, so a test pins all three. |
| Identity            | `identity.properties`                              | A later write that changes an identity property of an existing record.                                                                                        |
| Parent scope        | `identity.scope`                                   | A new scoped node without exactly one scope relation in the same write, a re-parenting attempt, and a parent or scope-relation delete while the child exists. |
| Endpoint types      | `sources`, `targets`, `self_edge`                  | A relation between node types it does not connect, and a self edge where none is allowed.                                                                     |
| Checks              | `checks` ids per type                              | Two properties that individually pass but disagree, and a relation whose endpoints' stored values do not actually stand in it.                                |
| Canonicalization    | `canonicalize` ids per type                        | Nothing: a declared non-canonical spelling is rewritten before validation, identity and storage.                                                              |

## Shared limits

| Setting                 | Value           | Meaning                                                                   |
| ----------------------- | --------------- | ------------------------------------------------------------------------- |
| `additional_properties` | true            | Properties outside the declared maps are accepted and stored unvalidated. |
| `coercion`              | false           | No JSON type is converted. A string `"443"` is not an integer.            |
| `depth`                 | 16              | Maximum nesting depth of the properties object.                           |
| `integers`              | `signed64`      | Integers outside signed 64-bit are rejected.                              |
| `numbers`               | `finite double` | NaN and infinity are rejected.                                            |
| `properties_bytes`      | 65536           | Maximum size of one canonical properties object, in UTF-8 bytes.          |
| `required_nonnull`      | true            | A required property may not be null or absent.                            |

## Node types

Identity is what makes two writes the same node. A parent scope adds the parent's UUID to that identity, so the same properties under two parents are two nodes.

| Node               | Identity                          | Parent scope                  | Required properties                                                                                                                                                                                                                |
| ------------------ | --------------------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `asn`              | `value`                           | —                             | `value`: `asn`                                                                                                                                                                                                                     |
| `certificate`      | `der_sha256`                      | —                             | `der_sha256`: `sha256`                                                                                                                                                                                                             |
| `cve`              | `value`                           | —                             | `value`: `cve`                                                                                                                                                                                                                     |
| `cwe`              | `value`                           | —                             | `value`: `cwe`                                                                                                                                                                                                                     |
| `dkim_record`      | `selector`                        | `has_dkim_selector` (source)  | `selector`: `dkim_selector`<br>`value`: `txt_value`                                                                                                                                                                                |
| `dmarc_record`     | `value`                           | —                             | `value`: `dmarc`                                                                                                                                                                                                                   |
| `domain`           | `value`                           | —                             | `value`: `dns_name`                                                                                                                                                                                                                |
| `email_address`    | `value`                           | —                             | `value`: `email_address`                                                                                                                                                                                                           |
| `endpoint`         | `url`, `method`                   | —                             | `method`: `method`<br>`url`: `http_url`                                                                                                                                                                                            |
| `finding`          | `title`                           | `has_finding` (source)        | `severity`: one of `info`, `low`, `medium`, `high`, `critical`<br>`title`: `printable_text_200`                                                                                                                                    |
| `host_key`         | `algorithm`, `fingerprint_sha256` | —                             | `algorithm`: one of `ssh-rsa`, `ssh-dss`, `ssh-ed25519`, `ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`, `sk-ssh-ed25519@openssh.com`, `sk-ecdsa-sha2-nistp256@openssh.com`<br>`fingerprint_sha256`: `sha256` |
| `http_fingerprint` | `kind`, `value`                   | —                             | `kind`: one of `favicon_mmh3`, `body_sha256`, `header_sha256`<br>`value`: `http_fingerprint_value`                                                                                                                                 |
| `identity_tenant`  | `provider`, `tenant_id`           | —                             | `provider`: one of `entra_id`, `okta`<br>`tenant_id`: `tenant_id`                                                                                                                                                                  |
| `ip_address`       | `value`                           | —                             | `value`: `ip`<br>`version`: `ip_version`                                                                                                                                                                                           |
| `ip_cidr`          | `value`                           | —                             | `value`: `cidr`<br>`version`: `ip_version`                                                                                                                                                                                         |
| `mta_sts_policy`   | `value`                           | `has_mta_sts_policy` (source) | `value`: `mta_sts`                                                                                                                                                                                                                 |
| `organization`     | `registry`, `handle`              | —                             | `handle`: `rir_handle`<br>`registry`: one of `arin`, `ripe`, `apnic`, `lacnic`, `afrinic`                                                                                                                                          |
| `parameter`        | `name`, `location`                | `has_parameter` (source)      | `location`: one of `query`, `body`, `header`, `cookie`, `path`<br>`name`: `parameter_name`                                                                                                                                         |
| `phone`            | `value`                           | —                             | `value`: `phone_e164`                                                                                                                                                                                                              |
| `port`             | `transport`, `number`             | `has_open_port` (source)      | `number`: `uint16`<br>`transport`: one of `tcp`, `udp`, `sctp`                                                                                                                                                                     |
| `registrar`        | `iana_id`                         | —                             | `iana_id`: `uint16`<br>`name`: `printable_text_200`                                                                                                                                                                                |
| `repository`       | `host`, `owner`, `name`           | —                             | `host`: `dns_name`<br>`name`: `repo_name`<br>`owner`: `repo_owner`<br>`platform`: one of `github`, `gitlab`, `bitbucket`, `gitea`                                                                                                  |
| `secret`           | `value_sha256`                    | —                             | `value_sha256`: `sha256`                                                                                                                                                                                                           |
| `service`          | `name`                            | `has_service` (source)        | `name`: `service_name`                                                                                                                                                                                                             |
| `spf_record`       | `value`                           | —                             | `value`: `spf`                                                                                                                                                                                                                     |
| `storage_bucket`   | `provider`, `name`                | —                             | `name`: `bucket_name`<br>`provider`: one of `aws_s3`, `gcp_gcs`, `azure_blob`                                                                                                                                                      |
| `subdomain`        | `value`                           | —                             | `value`: `dns_name`                                                                                                                                                                                                                |
| `technology`       | `name`                            | —                             | `name`: `tech_token`                                                                                                                                                                                                               |
| `tls_cipher_suite` | `version`, `name`                 | —                             | `name`: `tls_cipher_name`<br>`version`: one of `ssl30`, `tls10`, `tls11`, `tls12`, `tls13`, `dtls10`, `dtls12`, `dtls13`                                                                                                           |
| `tls_fingerprint`  | `kind`, `value`                   | —                             | `kind`: one of `jarm`, `ja3s`<br>`value`: `tls_fingerprint_value`                                                                                                                                                                  |
| `txt_record`       | `value`                           | —                             | `value`: `txt_value`                                                                                                                                                                                                               |

## Relation types

A relation's identity is scoped to its endpoints: two edges of one type between one pair of nodes are the same edge unless an identity property differs.

| Relation               | Sources                                                                                                                                                                                                 | Targets                  | Self edge | Identity                                                                              | Required properties                                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ | --------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `affected_by`          | `service`, `finding`, `endpoint`                                                                                                                                                                        | `cve`                    | no        | —                                                                                     | —                                                                                                                           |
| `announced_by`         | `ip_cidr`                                                                                                                                                                                               | `asn`                    | no        | —                                                                                     | —                                                                                                                           |
| `backed_by_bucket`     | `domain`, `subdomain`, `endpoint`                                                                                                                                                                       | `storage_bucket`         | no        | —                                                                                     | —                                                                                                                           |
| `caa_issue`            | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | `flags`, `parameters`, with `parameters` hashed order-independently                   | `flags`: `uint8`<br>`parameters`: `caa_parameters`                                                                          |
| `caa_issuewild`        | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | `flags`, `parameters`, with `parameters` hashed order-independently                   | `flags`: `uint8`<br>`parameters`: `caa_parameters`                                                                          |
| `cname_to`             | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | —                                                                                     | —                                                                                                                           |
| `contains_cidr`        | `ip_cidr`                                                                                                                                                                                               | `ip_cidr`                | no        | —                                                                                     | —                                                                                                                           |
| `contains_ip`          | `ip_cidr`                                                                                                                                                                                               | `ip_address`             | no        | —                                                                                     | —                                                                                                                           |
| `covers_name`          | `certificate`                                                                                                                                                                                           | `domain`, `subdomain`    | no        | `coverage`                                                                            | `coverage`: one of `exact`, `wildcard`                                                                                      |
| `dname_to`             | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | —                                                                                     | —                                                                                                                           |
| `exposes_secret`       | `repository`, `endpoint`, `storage_bucket`                                                                                                                                                              | `secret`                 | no        | `location`                                                                            | `location`: `printable_text_1024`                                                                                           |
| `federates_with`       | `domain`, `subdomain`                                                                                                                                                                                   | `identity_tenant`        | no        | —                                                                                     | —                                                                                                                           |
| `has_contact`          | `organization`, `registrar`, `domain`, `subdomain`, `repository`                                                                                                                                        | `email_address`, `phone` | no        | `role`                                                                                | `role`: one of `abuse`, `admin`, `tech`, `registrant`, `billing`, `noc`, `security`, `published`                            |
| `has_dkim_selector`    | `domain`, `subdomain`                                                                                                                                                                                   | `dkim_record`            | no        | —                                                                                     | —                                                                                                                           |
| `has_dmarc`            | `domain`, `subdomain`                                                                                                                                                                                   | `dmarc_record`           | no        | —                                                                                     | —                                                                                                                           |
| `has_finding`          | `port`, `domain`, `subdomain`, `ip_address`, `ip_cidr`, `service`, `endpoint`, `certificate`, `parameter`, `dkim_record`, `storage_bucket`, `repository`, `identity_tenant`, `secret`, `mta_sts_policy` | `finding`                | no        | —                                                                                     | —                                                                                                                           |
| `has_http_fingerprint` | `endpoint`                                                                                                                                                                                              | `http_fingerprint`       | no        | —                                                                                     | —                                                                                                                           |
| `has_mail_exchange`    | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | `preference`                                                                          | `preference`: `uint16`                                                                                                      |
| `has_mta_sts_policy`   | `domain`, `subdomain`                                                                                                                                                                                   | `mta_sts_policy`         | no        | —                                                                                     | —                                                                                                                           |
| `has_nameserver`       | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | —                                                                                     | —                                                                                                                           |
| `has_open_port`        | `ip_address`                                                                                                                                                                                            | `port`                   | no        | —                                                                                     | —                                                                                                                           |
| `has_parameter`        | `endpoint`                                                                                                                                                                                              | `parameter`              | no        | —                                                                                     | —                                                                                                                           |
| `has_service`          | `port`                                                                                                                                                                                                  | `service`                | no        | —                                                                                     | —                                                                                                                           |
| `has_soa_primary`      | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | —                                                                                     | —                                                                                                                           |
| `has_spf`              | `domain`, `subdomain`                                                                                                                                                                                   | `spf_record`             | no        | —                                                                                     | —                                                                                                                           |
| `has_srv_target`       | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | `service`, `protocol`, `port`, `priority`, `weight`                                   | `port`: `uint16`<br>`priority`: `uint16`<br>`protocol`: `srv_label`<br>`service`: `srv_label`<br>`weight`: `uint16`         |
| `has_subdomain`        | `domain`, `subdomain`                                                                                                                                                                                   | `subdomain`              | no        | —                                                                                     | —                                                                                                                           |
| `has_svcb_binding`     | `domain`, `subdomain`                                                                                                                                                                                   | `domain`, `subdomain`    | yes       | `record_type`, `priority`, `alpn`, with `alpn` hashed order-independently             | `alpn`: `alpn_tokens`<br>`priority`: `uint16`<br>`record_type`: one of `https`, `svcb`                                      |
| `has_tls_fingerprint`  | `service`                                                                                                                                                                                               | `tls_fingerprint`        | no        | —                                                                                     | —                                                                                                                           |
| `has_txt_record`       | `domain`, `subdomain`                                                                                                                                                                                   | `txt_record`             | no        | —                                                                                     | —                                                                                                                           |
| `has_weakness`         | `finding`, `cve`                                                                                                                                                                                        | `cwe`                    | no        | —                                                                                     | —                                                                                                                           |
| `issued_by`            | `certificate`                                                                                                                                                                                           | `certificate`            | yes       | —                                                                                     | —                                                                                                                           |
| `operated_by`          | `asn`, `ip_cidr`                                                                                                                                                                                        | `organization`           | no        | —                                                                                     | —                                                                                                                           |
| `owns_repository`      | `domain`, `subdomain`                                                                                                                                                                                   | `repository`             | no        | —                                                                                     | —                                                                                                                           |
| `presents_certificate` | `service`                                                                                                                                                                                               | `certificate`            | no        | `mode`, `server_name`, `alpn_offered`, with `alpn_offered` hashed order-independently | `alpn_offered`: `alpn_tokens`<br>`mode`: one of `tls`, `dtls`, `starttls`, `quic`<br>`server_name`: `dns_or_explicit_empty` |
| `presents_host_key`    | `service`                                                                                                                                                                                               | `host_key`               | no        | —                                                                                     | —                                                                                                                           |
| `protected_by`         | `service`, `endpoint`, `domain`, `subdomain`                                                                                                                                                            | `technology`             | no        | `kind`                                                                                | `kind`: one of `waf`, `cdn`, `reverse_proxy`, `load_balancer`                                                               |
| `redirects_to`         | `endpoint`                                                                                                                                                                                              | `endpoint`               | yes       | `status`                                                                              | `status`: `redirect_status`                                                                                                 |
| `registered_through`   | `domain`                                                                                                                                                                                                | `registrar`              | no        | —                                                                                     | —                                                                                                                           |
| `resolves_to`          | `domain`, `subdomain`                                                                                                                                                                                   | `ip_address`             | no        | —                                                                                     | —                                                                                                                           |
| `reverse_resolves_to`  | `ip_address`                                                                                                                                                                                            | `domain`, `subdomain`    | no        | —                                                                                     | —                                                                                                                           |
| `runs_technology`      | `service`, `endpoint`, `domain`, `subdomain`                                                                                                                                                            | `technology`             | no        | —                                                                                     | —                                                                                                                           |
| `serves_endpoint`      | `service`                                                                                                                                                                                               | `endpoint`               | no        | —                                                                                     | —                                                                                                                           |
| `supports_tls_cipher`  | `service`                                                                                                                                                                                               | `tls_cipher_suite`       | no        | —                                                                                     | —                                                                                                                           |

## Which relations a node can carry

The same matrix as above, read from the node's side.

| Node               | As source                                                                                                                                                                                                                                                                                                                                                                                                                 | As target                                                                                                                                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `asn`              | `operated_by`                                                                                                                                                                                                                                                                                                                                                                                                             | `announced_by`                                                                                                                                                                                              |
| `certificate`      | `covers_name`, `has_finding`, `issued_by`                                                                                                                                                                                                                                                                                                                                                                                 | `issued_by`, `presents_certificate`                                                                                                                                                                         |
| `cve`              | `has_weakness`                                                                                                                                                                                                                                                                                                                                                                                                            | `affected_by`                                                                                                                                                                                               |
| `cwe`              | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_weakness`                                                                                                                                                                                              |
| `dkim_record`      | `has_finding`                                                                                                                                                                                                                                                                                                                                                                                                             | `has_dkim_selector`                                                                                                                                                                                         |
| `dmarc_record`     | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_dmarc`                                                                                                                                                                                                 |
| `domain`           | `backed_by_bucket`, `caa_issue`, `caa_issuewild`, `cname_to`, `dname_to`, `federates_with`, `has_contact`, `has_dkim_selector`, `has_dmarc`, `has_finding`, `has_mail_exchange`, `has_mta_sts_policy`, `has_nameserver`, `has_soa_primary`, `has_spf`, `has_srv_target`, `has_subdomain`, `has_svcb_binding`, `has_txt_record`, `owns_repository`, `protected_by`, `registered_through`, `resolves_to`, `runs_technology` | `caa_issue`, `caa_issuewild`, `cname_to`, `covers_name`, `dname_to`, `has_mail_exchange`, `has_nameserver`, `has_soa_primary`, `has_srv_target`, `has_svcb_binding`, `reverse_resolves_to`                  |
| `email_address`    | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_contact`                                                                                                                                                                                               |
| `endpoint`         | `affected_by`, `backed_by_bucket`, `exposes_secret`, `has_finding`, `has_http_fingerprint`, `has_parameter`, `protected_by`, `redirects_to`, `runs_technology`                                                                                                                                                                                                                                                            | `redirects_to`, `serves_endpoint`                                                                                                                                                                           |
| `finding`          | `affected_by`, `has_weakness`                                                                                                                                                                                                                                                                                                                                                                                             | `has_finding`                                                                                                                                                                                               |
| `host_key`         | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `presents_host_key`                                                                                                                                                                                         |
| `http_fingerprint` | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_http_fingerprint`                                                                                                                                                                                      |
| `identity_tenant`  | `has_finding`                                                                                                                                                                                                                                                                                                                                                                                                             | `federates_with`                                                                                                                                                                                            |
| `ip_address`       | `has_finding`, `has_open_port`, `reverse_resolves_to`                                                                                                                                                                                                                                                                                                                                                                     | `contains_ip`, `resolves_to`                                                                                                                                                                                |
| `ip_cidr`          | `announced_by`, `contains_cidr`, `contains_ip`, `has_finding`, `operated_by`                                                                                                                                                                                                                                                                                                                                              | `contains_cidr`                                                                                                                                                                                             |
| `mta_sts_policy`   | `has_finding`                                                                                                                                                                                                                                                                                                                                                                                                             | `has_mta_sts_policy`                                                                                                                                                                                        |
| `organization`     | `has_contact`                                                                                                                                                                                                                                                                                                                                                                                                             | `operated_by`                                                                                                                                                                                               |
| `parameter`        | `has_finding`                                                                                                                                                                                                                                                                                                                                                                                                             | `has_parameter`                                                                                                                                                                                             |
| `phone`            | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_contact`                                                                                                                                                                                               |
| `port`             | `has_finding`, `has_service`                                                                                                                                                                                                                                                                                                                                                                                              | `has_open_port`                                                                                                                                                                                             |
| `registrar`        | `has_contact`                                                                                                                                                                                                                                                                                                                                                                                                             | `registered_through`                                                                                                                                                                                        |
| `repository`       | `exposes_secret`, `has_contact`, `has_finding`                                                                                                                                                                                                                                                                                                                                                                            | `owns_repository`                                                                                                                                                                                           |
| `secret`           | `has_finding`                                                                                                                                                                                                                                                                                                                                                                                                             | `exposes_secret`                                                                                                                                                                                            |
| `service`          | `affected_by`, `has_finding`, `has_tls_fingerprint`, `presents_certificate`, `presents_host_key`, `protected_by`, `runs_technology`, `serves_endpoint`, `supports_tls_cipher`                                                                                                                                                                                                                                             | `has_service`                                                                                                                                                                                               |
| `spf_record`       | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_spf`                                                                                                                                                                                                   |
| `storage_bucket`   | `exposes_secret`, `has_finding`                                                                                                                                                                                                                                                                                                                                                                                           | `backed_by_bucket`                                                                                                                                                                                          |
| `subdomain`        | `backed_by_bucket`, `caa_issue`, `caa_issuewild`, `cname_to`, `dname_to`, `federates_with`, `has_contact`, `has_dkim_selector`, `has_dmarc`, `has_finding`, `has_mail_exchange`, `has_mta_sts_policy`, `has_nameserver`, `has_soa_primary`, `has_spf`, `has_srv_target`, `has_subdomain`, `has_svcb_binding`, `has_txt_record`, `owns_repository`, `protected_by`, `resolves_to`, `runs_technology`                       | `caa_issue`, `caa_issuewild`, `cname_to`, `covers_name`, `dname_to`, `has_mail_exchange`, `has_nameserver`, `has_soa_primary`, `has_srv_target`, `has_subdomain`, `has_svcb_binding`, `reverse_resolves_to` |
| `technology`       | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `protected_by`, `runs_technology`                                                                                                                                                                           |
| `tls_cipher_suite` | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `supports_tls_cipher`                                                                                                                                                                                       |
| `tls_fingerprint`  | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_tls_fingerprint`                                                                                                                                                                                       |
| `txt_record`       | —                                                                                                                                                                                                                                                                                                                                                                                                                         | `has_txt_record`                                                                                                                                                                                            |

## Checks

Each check runs after the required map has validated the properties it reads. A relation check may also read both endpoints' stored properties; it runs on the properties that will be stored, after a patch or a matching earlier edge has been merged. The version suffix changes whenever what the check accepts changes.

| Check                           | Types                 | Rule                                                                                                                                                                                                                                                                                       |
| ------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bucket_name_spelling.1`        | `storage_bucket`      | `name` is checked against the declared `provider`: length, grammar, and the prefixes, suffixes and substrings that provider reserves.                                                                                                                                                      |
| `contains_cidr_proper_subnet.1` | `contains_cidr`       | The target network must be a proper subnet of the source, at the same IP version.                                                                                                                                                                                                          |
| `contains_ip_member.1`          | `contains_ip`         | The target address must fall inside the source network, at the same IP version.                                                                                                                                                                                                            |
| `dns_name_kind.1`               | `domain`, `subdomain` | `value` must classify as this type against the bundled PSL: a registrable domain for `domain`, a name below one for `subdomain`.                                                                                                                                                           |
| `has_subdomain_suffix.1`        | `has_subdomain`       | The target's `value` must end in `.` plus the source's `value`.                                                                                                                                                                                                                            |
| `http_fingerprint_value_kind.1` | `http_fingerprint`    | `favicon_mmh3` requires the signed 32-bit integer spelling; `body_sha256` and `header_sha256` require 64 lowercase hex characters.                                                                                                                                                         |
| `ip_address_version.1`          | `ip_address`          | `version` must equal the version of the address in `value`.                                                                                                                                                                                                                                |
| `ip_cidr_version.1`             | `ip_cidr`             | `version` must equal the version of the network in `value`.                                                                                                                                                                                                                                |
| `registrar_iana_assigned.1`     | `registrar`           | `iana_id` must be at least 1, because 0 is what an agent emits for a missing field.                                                                                                                                                                                                        |
| `repository_owner_spelling.1`   | `repository`          | `owner` is checked against the grammar and length of the declared `platform`, and only `gitlab` accepts a `/` for nested groups.                                                                                                                                                           |
| `secret_plaintext_keys.1`       | `secret`              | The node is rejected if it carries `value`, `secret`, `plaintext`, `password`, `token`, `key`, `credential`, `match` or `raw`, so the credential itself cannot reach storage.                                                                                                              |
| `service_secure_flag.1`         | `service`             | A TLS-capable registry entry, such as `http`, additionally requires a boolean `secure`.                                                                                                                                                                                                    |
| `tenant_id_spelling.1`          | `identity_tenant`     | `entra_id` requires a canonical lowercase UUID; `okta` requires the bare organization slug, so a dot is rejected.                                                                                                                                                                          |
| `tls_fingerprint_length.1`      | `tls_fingerprint`     | `value` must be 62 characters for `jarm` and 32 for `ja3s`.                                                                                                                                                                                                                                |
| `txt_record_diversion.1`        | `txt_record`          | A `value` that the dedicated type for its version tag accepts is rejected here: `v=spf1` belongs to `spf_record`, `v=DMARC1` to `dmarc_record`, `v=DKIM1` to `dkim_record` and `v=STSv1` to `mta_sts_policy`. A malformed tagged value, such as `v=spf1include:...`, stays a `txt_record`. |

## Canonicalizations

Each rewrites a declared non-canonical spelling in place before validation, identity and storage.

| Canonicalization            | Types                        | Rewrite                                                                                                                                                                           |
| --------------------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caa_parameter_name_fold.1` | `caa_issue`, `caa_issuewild` | ASCII parameter names are lowercased, because RFC 8659 tags are case-insensitive while the parameter list is identity-bearing. A non-ASCII name is left for validation to reject. |
| `endpoint_url_drop_query.1` | `endpoint`                   | Everything from the first `?` in `url` is removed before the URL is validated and hashed, so one path is one endpoint; parameter names belong to `parameter` nodes.               |

## Format rules

Every declared property that is not an enum names one of these rules. The version changes whenever what the rule accepts changes.

| Rule                     | Version | Accepted spelling                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `alpn_tokens`            | 1       | An array of zero or more ASCII tokens matching `[A-Za-z0-9./_-]{1,255}`; order is not identity-significant and duplicates are preserved.                                                                                                                                                                                                                                              |
| `asn`                    | 1       | A strict JSON integer from 0 through 4294967295.                                                                                                                                                                                                                                                                                                                                      |
| `bucket_name`            | 1       | The provider-global name of an object-storage bucket: 3 to 222 lowercase ASCII characters from letters, digits, hyphen, underscore and dot, starting and ending alphanumeric, without a doubled dot, and never a dotted-quad IPv4 address. Every provider is stricter than this union rule, and the declared provider fixes which of the narrower spellings is accepted.              |
| `caa_parameters`         | 1       | An array of objects with name and value strings. Names start alphanumeric and continue alphanumeric or hyphen. Values are empty or use ASCII 0x21-0x3A and 0x3C-0x7E.                                                                                                                                                                                                                 |
| `cidr`                   | 1       | Canonical strict IPv4 or IPv6 network with an explicit prefix length.                                                                                                                                                                                                                                                                                                                 |
| `cpe23_or_empty`         | 1       | The empty string, or a lowercase CPE 2.3 formatted string (NIST IR 7695): 'cpe:2.3:' followed by the part and ten colon-separated components, each '*', '-', or an escaped attribute value optionally anchored by '*' or a run of '?', at most 512 characters. The legacy 'cpe:/' URI binding is rejected. No declared property uses this rule yet.                                   |
| `cve`                    | 1       | A string matching `CVE-[0-9]{4}-[0-9]{4,}` exactly.                                                                                                                                                                                                                                                                                                                                   |
| `cwe`                    | 1       | A string matching `CWE-[0-9]{1,6}` exactly, uppercase as MITRE publishes it.                                                                                                                                                                                                                                                                                                          |
| `dkim_selector`          | 1       | A lowercase ASCII DKIM selector of at most 253 bytes, as one or more dot-separated labels of at most 63 bytes each, written without the `_domainkey` suffix or the domain. In practice it is far shorter, since `<selector>._domainkey.<domain>` must itself fit in 253 bytes.                                                                                                        |
| `dmarc`                  | 1       | Printable ASCII of at most 4096 characters beginning with 'v=DMARC1' followed by a semicolon, a normal space, or end of text.                                                                                                                                                                                                                                                         |
| `dns_name`               | 1       | A lowercase ASCII domain or subdomain spelling classified by the bundled ICANN PSL; at least two labels, labels at most 63 bytes, total at most 253 bytes, and no trailing dot.                                                                                                                                                                                                       |
| `dns_or_explicit_empty`  | 1       | dns_name or the explicit empty string for no SNI offer; IP literals are rejected.                                                                                                                                                                                                                                                                                                     |
| `email_address`          | 1       | A lowercase ASCII mailbox of at most 254 characters: an RFC 5322 dot-atom local part of at most 64 characters, one `@`, and a domain that satisfies dns_name. Quoted local parts, address literals and display names are rejected. A local part is case-sensitive on the wire; this rule requires the lowercase spelling anyway, so one mailbox is one node.                          |
| `http_fingerprint_value` | 1       | Either a signed 32-bit decimal integer written in ASCII without a leading zero or a plus sign, for a MurmurHash3 favicon hash, or exactly 64 lowercase hexadecimal characters for a response digest. The declared kind fixes which one is accepted.                                                                                                                                   |
| `http_url`               | 1       | Canonical absolute ASCII http/https URL with lowercase host, mandatory path, no userinfo, fragment, whitespace, backslash, Unicode, default explicit port, dot path segment, or lowercase percent escape. A query string is removed before identity and storage, so one endpoint holds one path; parameter names belong to parameter nodes.                                           |
| `ip`                     | 1       | Canonical IPv4Address.compressed or lowercase IPv6Address.compressed spelling, without scope or prefix.                                                                                                                                                                                                                                                                               |
| `ip_version`             | 1       | A strict JSON integer equal to 4 or 6.                                                                                                                                                                                                                                                                                                                                                |
| `method`                 | 1       | One to 32 characters matching an uppercase HTTP method token.                                                                                                                                                                                                                                                                                                                         |
| `mta_sts`                | 1       | Printable ASCII of at most 4096 characters beginning with 'v=STSv1' followed by a semicolon, a normal space, or end of text: the TXT record at `_mta-sts.<domain>`, not the policy file body.                                                                                                                                                                                         |
| `parameter_name`         | 1       | 1 to 128 printable ASCII characters without space, `&`, `=`, or `#`; one single parameter name, never a raw query string.                                                                                                                                                                                                                                                             |
| `phone_e164`             | 1       | An E.164 number: `+`, a leading digit from 1 through 9, and in total 2 to 15 digits.                                                                                                                                                                                                                                                                                                  |
| `printable_text_1024`    | 1       | A string of 1-1024 printable Unicode characters.                                                                                                                                                                                                                                                                                                                                      |
| `printable_text_200`     | 1       | A string of 1-200 printable Unicode characters.                                                                                                                                                                                                                                                                                                                                       |
| `redirect_status`        | 1       | A strict JSON integer in 301, 302, 303, 307, or 308.                                                                                                                                                                                                                                                                                                                                  |
| `repo_name`              | 1       | A 1-100 character lowercase ASCII repository name from letters, digits, dot, underscore and hyphen, holding at least one alphanumeric character and never the reserved `.` or `..`. Hosting platforms resolve names case-insensitively, so the lowercase spelling keeps one repository one node.                                                                                      |
| `repo_owner`             | 1       | A 1-255 character lowercase ASCII owner path of one or more `/`-separated segments, each 1-100 characters from letters, digits, dot, underscore and hyphen and each starting and ending alphanumeric. The separator exists for nested GitLab groups; the declared platform fixes whether more than one segment is accepted.                                                           |
| `rir_handle`             | 1       | A regional-registry object handle of 2 to 64 ASCII characters, starting and ending alphanumeric and continuing alphanumeric or hyphen. Handles are case-sensitive and stored exactly as the registry publishes them: RIPE and AFRINIC preserve the organisation name's case (ORG-nG51-RIPE), so never uppercase one. Handles are unique within one registry, never across registries. |
| `service_name`           | 1       | A member of the bundled versioned service name whitelist.                                                                                                                                                                                                                                                                                                                             |
| `sha256`                 | 1       | Exactly 64 lowercase ASCII hexadecimal characters.                                                                                                                                                                                                                                                                                                                                    |
| `spf`                    | 1       | Printable ASCII beginning with `v=spf1` followed by a normal space or end of text.                                                                                                                                                                                                                                                                                                    |
| `srv_label`              | 1       | A 2-63 byte lowercase ASCII SRV label beginning with underscore.                                                                                                                                                                                                                                                                                                                      |
| `tech_token`             | 1       | A 1-63 character lowercase ASCII technology slug that starts and ends alphanumeric and may contain interior dot, underscore, plus, or hyphen.                                                                                                                                                                                                                                         |
| `tenant_id`              | 1       | A 1-128 character lowercase ASCII identity-tenant identifier that starts and ends alphanumeric and may contain interior dot, underscore or hyphen. The declared provider fixes the narrower spelling: a canonical lowercase UUID for Entra ID, a bare organization slug for Okta.                                                                                                     |
| `tls_cipher_name`        | 1       | The spelling of an IANA TLS cipher suite name: 5 to 128 uppercase ASCII characters beginning 'TLS\_', with underscore-separated alphanumeric components. Shape only; membership in the IANA registry is not checked.                                                                                                                                                                  |
| `tls_fingerprint_value`  | 1       | Exactly 32 lowercase hexadecimal characters for a JA3S MD5 digest, or exactly 62 for a JARM fingerprint. The declared kind fixes which length is accepted.                                                                                                                                                                                                                            |
| `txt_value`              | 1       | 1 to 4096 printable ASCII characters, the concatenated and unquoted character-strings of one TXT RRset.                                                                                                                                                                                                                                                                               |
| `uint16`                 | 1       | A strict JSON integer from 0 through 65535.                                                                                                                                                                                                                                                                                                                                           |
| `uint8`                  | 1       | A strict JSON integer from 0 through 255.                                                                                                                                                                                                                                                                                                                                             |

## Node type details

### `asn`

An autonomous system number: the routing identity a network is announced from.

**Not modeled:** Not the organization holding it (`organization` through `operated_by`) and not the prefixes it announces (`ip_cidr` through `announced_by`).

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule  | Meaning                                               |
| -------- | -------- | ----- | ----------------------------------------------------- |
| `value`  | yes      | `asn` | The AS number as an integer, without the `AS` prefix. |

### `certificate`

One X.509 certificate, identified by the SHA-256 of its DER encoding.

**Not modeled:** Not the handshake that presented it (`presents_certificate` from a `service`) and not the names it covers, which are `covers_name` edges.

A self edge through `issued_by` records a self-signed certificate. Absence of that edge means the issuer was never written, not that the chain ends.

**Identity:** `der_sha256`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property     | Required | Rule     | Meaning                                               |
| ------------ | -------- | -------- | ----------------------------------------------------- |
| `der_sha256` | yes      | `sha256` | Lowercase hex SHA-256 of the certificate's DER bytes. |

### `cve`

A published CVE record, shared by every object affected by it.

**Not modeled:** Not an observation that something is vulnerable; that is `affected_by` from the affected service, endpoint or finding. Never a finding source.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule  | Meaning                                      |
| -------- | -------- | ----- | -------------------------------------------- |
| `value`  | yes      | `cve` | The CVE id, uppercase as MITRE publishes it. |

### `cwe`

A CWE weakness class: the canonical spelling that joins findings from different scanners.

**Not modeled:** Not a finding or an advisory; those reach it through `has_weakness`. Never a finding source.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule  | Meaning                                             |
| -------- | -------- | ----- | --------------------------------------------------- |
| `value`  | yes      | `cwe` | The CWE id as MITRE publishes it, such as `CWE-79`. |

### `dkim_record`

The DKIM key record one domain publishes for one selector, scoped through `has_dkim_selector`.

**Not modeled:** Not the `<selector>._domainkey` owner name as a subdomain node.

Writing the same selector again patches `value` in place, so the node is a current-state view; a rotated key survives only in evidence attached to the earlier write.

**Identity:** `selector`<br>**Parent scope:** `has_dkim_selector` (source)<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule            | Meaning                                                                               |
| ---------- | -------- | --------------- | ------------------------------------------------------------------------------------- |
| `selector` | yes      | `dkim_selector` | The selector labels, without `._domainkey` or the domain.                             |
| `value`    | yes      | `txt_value`     | The normalized TXT value: unquoted, unescaped, with multi-string RRsets concatenated. |

### `dmarc_record`

A DMARC policy value, shared by every name that publishes the same string.

**Not modeled:** Not the `_dmarc` owner name; `has_dmarc` attaches the record to the domain or subdomain itself.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule    | Meaning                                        |
| -------- | -------- | ------- | ---------------------------------------------- |
| `value`  | yes      | `dmarc` | The normalized TXT value beginning `v=DMARC1`. |

### `domain`

A registrable domain name as the bundled public suffix list classifies it, such as `example.co.uk`.

**Not modeled:** Not a name below a registrable domain (`subdomain`) and not a public suffix. Registration facts are not properties of the name.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** `dns_name_kind.1`<br>**Canonicalizations:** —

| Property | Required | Rule       | Meaning                                                            |
| -------- | -------- | ---------- | ------------------------------------------------------------------ |
| `value`  | yes      | `dns_name` | The lowercase ASCII name without a trailing dot; IDNs in punycode. |

### `email_address`

A mailbox, reached as a contact through `has_contact`.

**Not modeled:** Not a person; person and company types are out of scope. Never a finding source.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule            | Meaning                                          |
| -------- | -------- | --------------- | ------------------------------------------------ |
| `value`  | yes      | `email_address` | The mailbox, lowercase including the local part. |

### `endpoint`

One HTTP method on one URL path: the unit a crawler or scanner observes.

**Not modeled:** Not a query string or its values (parameter names are `parameter` nodes) and not the host or service that serves it.

Response digests are pivots on `http_fingerprint` nodes, not endpoint attributes. Raw response bodies and headers belong in evidence.

**Identity:** `url`, `method`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** `endpoint_url_drop_query.1`

| Property | Required | Rule       | Meaning                                                                                 |
| -------- | -------- | ---------- | --------------------------------------------------------------------------------------- |
| `method` | yes      | `method`   | The uppercase HTTP method token.                                                        |
| `url`    | yes      | `http_url` | Absolute canonical http/https URL; a submitted query string is removed before identity. |

### `finding`

One issue a scanner or analyst reports about one object, scoped to it through `has_finding`.

**Not modeled:** Not the weakness class (`cwe`) or a published advisory (`cve`); those link through `has_weakness` and `affected_by`.

**Identity:** `title`<br>**Parent scope:** `has_finding` (source)<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule                                               | Meaning                                                        |
| ---------- | -------- | -------------------------------------------------- | -------------------------------------------------------------- |
| `severity` | yes      | one of `info`, `low`, `medium`, `high`, `critical` | The reporter's severity; the latest write wins.                |
| `title`    | yes      | `printable_text_200`                               | The finding title; with the parent, it identifies the finding. |

### `host_key`

An SSH host public key, shared by every service that presents it.

**Not modeled:** Not a host. Two hosts presenting one key are a cloned image or one machine at two addresses, which makes this a correlation pivot. Never a finding source.

**Identity:** `algorithm`, `fingerprint_sha256`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property             | Required | Rule                                                                                                                                                                                | Meaning                                                                                           |
| -------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `algorithm`          | yes      | one of `ssh-rsa`, `ssh-dss`, `ssh-ed25519`, `ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`, `sk-ssh-ed25519@openssh.com`, `sk-ecdsa-sha2-nistp256@openssh.com` | The SSH public key algorithm name.                                                                |
| `fingerprint_sha256` | yes      | `sha256`                                                                                                                                                                            | Lowercase hex SHA-256 of the raw public key blob, converted from OpenSSH's base64 `SHA256:` form. |

### `http_fingerprint`

A response-side clustering pivot: a favicon hash or a body or header digest.

**Not modeled:** Not an identifier of a host or an owner; a shared default favicon or framework page is common. Never a finding source.

**Identity:** `kind`, `value`<br>**Parent scope:** —<br>**Checks:** `http_fingerprint_value_kind.1`<br>**Canonicalizations:** —

| Property | Required | Rule                                                  | Meaning                                                                                |
| -------- | -------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `kind`   | yes      | one of `favicon_mmh3`, `body_sha256`, `header_sha256` | Which fingerprint `value` holds.                                                       |
| `value`  | yes      | `http_fingerprint_value`                              | Decimal signed 32-bit MurmurHash3 for `favicon_mmh3`, lowercase hex SHA-256 otherwise. |

### `identity_tenant`

An identity-provider tenant, such as an Entra ID directory or an Okta organization.

**Not modeled:** Not a cloud billing account or subscription and not a user.

**Identity:** `provider`, `tenant_id`<br>**Parent scope:** —<br>**Checks:** `tenant_id_spelling.1`<br>**Canonicalizations:** —

| Property    | Required | Rule                      | Meaning                                                                      |
| ----------- | -------- | ------------------------- | ---------------------------------------------------------------------------- |
| `provider`  | yes      | one of `entra_id`, `okta` | The identity provider; each member fixes one canonical `tenant_id` spelling. |
| `tenant_id` | yes      | `tenant_id`               | The tenant identifier in the provider's canonical spelling.                  |

### `ip_address`

One IPv4 or IPv6 address.

**Not modeled:** Not a network (`ip_cidr`) and not a name that resolves to it (`resolves_to`).

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** `ip_address_version.1`<br>**Canonicalizations:** —

| Property  | Required | Rule         | Meaning                                    |
| --------- | -------- | ------------ | ------------------------------------------ |
| `value`   | yes      | `ip`         | The canonical compressed address spelling. |
| `version` | yes      | `ip_version` | 4 or 6, matching `value`.                  |

### `ip_cidr`

One IPv4 or IPv6 network with an explicit prefix length, as allocated, announced or scanned.

**Not modeled:** Not a single address (`ip_address`) and not its holder (`organization` through `operated_by`).

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** `ip_cidr_version.1`<br>**Canonicalizations:** —

| Property  | Required | Rule         | Meaning                                         |
| --------- | -------- | ------------ | ----------------------------------------------- |
| `value`   | yes      | `cidr`       | The canonical network spelling, host bits zero. |
| `version` | yes      | `ip_version` | 4 or 6, matching `value`.                       |

### `mta_sts_policy`

The MTA-STS TXT record a name publishes at `_mta-sts.<name>`, scoped through `has_mta_sts_policy`.

**Not modeled:** Not the policy file body served over HTTPS; its fields are attributes of this node.

Scoped because the TXT value is only a version pointer that unrelated tenants publish verbatim; unscoped, their policy attributes would overwrite each other.

**Identity:** `value`<br>**Parent scope:** `has_mta_sts_policy` (source)<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule      | Meaning                            |
| -------- | -------- | --------- | ---------------------------------- |
| `value`  | yes      | `mta_sts` | The TXT value beginning `v=STSv1`. |

### `organization`

A number-resource holder known to a regional internet registry, keyed on the registry and handle.

**Not modeled:** Not a company in general and not a domain registrant; person and company types are out of scope.

**Identity:** `registry`, `handle`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule                                                | Meaning                                                                     |
| ---------- | -------- | --------------------------------------------------- | --------------------------------------------------------------------------- |
| `handle`   | yes      | `rir_handle`                                        | The RIR object handle, case preserved exactly as the registry publishes it. |
| `registry` | yes      | one of `arin`, `ripe`, `apnic`, `lacnic`, `afrinic` | The RIR that issued the handle.                                             |

### `parameter`

One named input of one endpoint, by name and location, scoped through `has_parameter`.

**Not modeled:** Not a value seen for the parameter; values are sample data for evidence.

**Identity:** `name`, `location`<br>**Parent scope:** `has_parameter` (source)<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule                                               | Meaning                                       |
| ---------- | -------- | -------------------------------------------------- | --------------------------------------------- |
| `location` | yes      | one of `query`, `body`, `header`, `cookie`, `path` | Where the parameter travels in the request.   |
| `name`     | yes      | `parameter_name`                                   | One parameter name, never a raw query string. |

### `phone`

A telephone contact, reached through `has_contact`.

**Not modeled:** Not a person or a subscriber. Never a finding source.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule         | Meaning                                 |
| -------- | -------- | ------------ | --------------------------------------- |
| `value`  | yes      | `phone_e164` | The E.164 number with its leading plus. |

### `port`

One open transport port on one address, scoped to its `ip_address` through `has_open_port`.

**Not modeled:** Not a closed or filtered port: only open ports are written, and the scope edge is the state.

**Identity:** `transport`, `number`<br>**Parent scope:** `has_open_port` (source)<br>**Checks:** —<br>**Canonicalizations:** —

| Property    | Required | Rule                        | Meaning                 |
| ----------- | -------- | --------------------------- | ----------------------- |
| `number`    | yes      | `uint16`                    | The port number.        |
| `transport` | yes      | one of `tcp`, `udp`, `sctp` | The transport protocol. |

### `registrar`

An ICANN-accredited registrar, keyed on its IANA registrar id.

**Not modeled:** Not a registry or a reseller. A registrar without an IANA id gets no node.

**Identity:** `iana_id`<br>**Parent scope:** —<br>**Checks:** `registrar_iana_assigned.1`<br>**Canonicalizations:** —

| Property  | Required | Rule                 | Meaning                                                                                     |
| --------- | -------- | -------------------- | ------------------------------------------------------------------------------------------- |
| `iana_id` | yes      | `uint16`             | The IANA registrar id.                                                                      |
| `name`    | yes      | `printable_text_200` | The registrar's name; the latest write wins, because names change with renames and mergers. |

### `repository`

A source-code repository on one hosting instance, keyed on the host, owner and name.

**Not modeled:** Not an organization or a person. `owns_repository` attributes it to a name and needs evidence.

**Identity:** `host`, `owner`, `name`<br>**Parent scope:** —<br>**Checks:** `repository_owner_spelling.1`<br>**Canonicalizations:** —

| Property   | Required | Rule                                            | Meaning                                                              |
| ---------- | -------- | ----------------------------------------------- | -------------------------------------------------------------------- |
| `host`     | yes      | `dns_name`                                      | The hosting instance's host name.                                    |
| `name`     | yes      | `repo_name`                                     | The repository name, lowercase.                                      |
| `owner`    | yes      | `repo_owner`                                    | The owner path, lowercase; only `gitlab` nests groups.               |
| `platform` | yes      | one of `github`, `gitlab`, `bitbucket`, `gitea` | The hosting software, which selects the owner grammar; not identity. |

### `secret`

One exposed credential, identified only by the SHA-256 of the secret so its occurrences join.

**Not modeled:** Never the secret itself: a node carrying a plaintext-bearing key is rejected.

Hash the credential exactly as issued, without quotes, assignment prefix or trailing newline. An unsalted digest of a human-chosen password is a cracking target.

**Identity:** `value_sha256`<br>**Parent scope:** —<br>**Checks:** `secret_plaintext_keys.1`<br>**Canonicalizations:** —

| Property       | Required | Rule     | Meaning                                          |
| -------------- | -------- | -------- | ------------------------------------------------ |
| `value_sha256` | yes      | `sha256` | Lowercase hex SHA-256 of the exact secret bytes. |

### `service`

The application protocol a port speaks, scoped to that port through `has_service`.

**Not modeled:** Not the product or version implementing it; that is `runs_technology`.

**Identity:** `name`<br>**Parent scope:** `has_service` (source)<br>**Checks:** `service_secure_flag.1`<br>**Canonicalizations:** —

| Property | Required | Rule           | Meaning                                                     |
| -------- | -------- | -------------- | ----------------------------------------------------------- |
| `name`   | yes      | `service_name` | A member of the bundled Nmap-derived service-name registry. |

### `spf_record`

An SPF policy value, shared by every name that publishes the same string.

**Not modeled:** Not the publishing name (`has_spf`) and not a malformed SPF-like value, which is a `txt_record`.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule  | Meaning                                      |
| -------- | -------- | ----- | -------------------------------------------- |
| `value`  | yes      | `spf` | The normalized TXT value beginning `v=spf1`. |

### `storage_bucket`

An object-storage bucket in a provider-global namespace.

**Not modeled:** Not an Azure container (the node is the storage account) and not a regional namespace such as DigitalOcean Spaces.

**Identity:** `provider`, `name`<br>**Parent scope:** —<br>**Checks:** `bucket_name_spelling.1`<br>**Canonicalizations:** —

| Property   | Required | Rule                                     | Meaning                                                             |
| ---------- | -------- | ---------------------------------------- | ------------------------------------------------------------------- |
| `name`     | yes      | `bucket_name`                            | The provider-global bucket name, or the Azure storage account name. |
| `provider` | yes      | one of `aws_s3`, `gcp_gcs`, `azure_blob` | The storage provider, whose naming rules the name must satisfy.     |

### `subdomain`

A DNS name below a registrable domain, such as `api.example.com`.

**Not modeled:** Not the registrable domain itself (`domain`).

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** `dns_name_kind.1`<br>**Canonicalizations:** —

| Property | Required | Rule       | Meaning                                                            |
| -------- | -------- | ---------- | ------------------------------------------------------------------ |
| `value`  | yes      | `dns_name` | The lowercase ASCII name without a trailing dot; IDNs in punycode. |

### `technology`

A product, framework or service slug, shared by every host that runs it.

**Not modeled:** Not a per-host version, which belongs on `runs_technology`. Never a finding source.

**Identity:** `name`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule         | Meaning                     |
| -------- | -------- | ------------ | --------------------------- |
| `name`   | yes      | `tech_token` | The lowercase product slug. |

### `tls_cipher_suite`

An IANA TLS cipher suite at one protocol version, shared by every service accepting it.

**Not modeled:** Not a negotiated session. Never a finding source.

**Identity:** `version`, `name`<br>**Parent scope:** —<br>**Checks:** —<br>**Canonicalizations:** —

| Property  | Required | Rule                                                                             | Meaning                                        |
| --------- | -------- | -------------------------------------------------------------------------------- | ---------------------------------------------- |
| `name`    | yes      | `tls_cipher_name`                                                                | The IANA cipher suite name.                    |
| `version` | yes      | one of `ssl30`, `tls10`, `tls11`, `tls12`, `tls13`, `dtls10`, `dtls12`, `dtls13` | The protocol version the suite was offered at. |

### `tls_fingerprint`

A TLS stack clustering pivot, JARM or JA3S, shared by every host behind one stack.

**Not modeled:** Not a host identifier: every host behind one load balancer presents the same JARM.

**Identity:** `kind`, `value`<br>**Parent scope:** —<br>**Checks:** `tls_fingerprint_length.1`<br>**Canonicalizations:** —

| Property | Required | Rule                    | Meaning                                               |
| -------- | -------- | ----------------------- | ----------------------------------------------------- |
| `kind`   | yes      | one of `jarm`, `ja3s`   | Which fingerprint `value` holds.                      |
| `value`  | yes      | `tls_fingerprint_value` | The fingerprint: 62 characters for JARM, 32 for JA3S. |

### `txt_record`

A generic TXT value a name publishes, when no dedicated type accepts it.

**Not modeled:** Not an SPF, DMARC, DKIM or MTA-STS value its dedicated type accepts, and never an ephemeral `_acme-challenge` value.

**Identity:** `value`<br>**Parent scope:** —<br>**Checks:** `txt_record_diversion.1`<br>**Canonicalizations:** —

| Property | Required | Rule        | Meaning                   |
| -------- | -------- | ----------- | ------------------------- |
| `value`  | yes      | `txt_value` | The normalized TXT value. |

## Relation type details

### `affected_by`

The source is affected by the CVE: a vulnerable service, an endpoint a CVE template matched, or a finding reporting it.

**Not modeled:** Not a CVE merely mentioned nearby; attach the evidence for the match.

**Identity:** —<br>**Sources:** `service`, `finding`, `endpoint`<br>**Targets:** `cve`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `announced_by`

The network is announced in BGP by the AS.

**Not modeled:** Not the holder of the network; that is `operated_by`.

**Identity:** —<br>**Sources:** `ip_cidr`<br>**Targets:** `asn`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `backed_by_bucket`

A name or URL serves content from the bucket.

**Not modeled:** Not a bucket found only by guessing names; write that bucket node with its evidence instead.

**Identity:** —<br>**Sources:** `domain`, `subdomain`, `endpoint`<br>**Targets:** `storage_bucket`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `caa_issue`

A CAA `issue` property at the source name authorizes the issuer domain named by the target.

**Not modeled:** Not a wildcard authorization (`caa_issuewild`) and not an iodef reporting address.

**Identity:** `flags`, `parameters`, with `parameters` hashed order-independently<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** `caa_parameter_name_fold.1`

| Property     | Required | Rule             | Meaning                                                                                     |
| ------------ | -------- | ---------------- | ------------------------------------------------------------------------------------------- |
| `flags`      | yes      | `uint8`          | The CAA flags octet.                                                                        |
| `parameters` | yes      | `caa_parameters` | The property's parameters; names are lowercased and the list is hashed order-independently. |

### `caa_issuewild`

A CAA `issuewild` property at the source name authorizes the target issuer for wildcard names.

**Not modeled:** Not a non-wildcard authorization (`caa_issue`).

**Identity:** `flags`, `parameters`, with `parameters` hashed order-independently<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** `caa_parameter_name_fold.1`

| Property     | Required | Rule             | Meaning                                                                                     |
| ------------ | -------- | ---------------- | ------------------------------------------------------------------------------------------- |
| `flags`      | yes      | `uint8`          | The CAA flags octet.                                                                        |
| `parameters` | yes      | `caa_parameters` | The property's parameters; names are lowercased and the list is hashed order-independently. |

### `cname_to`

A CNAME record at the source name points to the target name.

**Not modeled:** Not resolution to an address (`resolves_to`) and not a subtree redirect (`dname_to`).

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

### `contains_cidr`

The source network properly contains the target network.

**Not modeled:** Not an allocation or announcement fact; containment is arithmetic, checked on write.

**Identity:** —<br>**Sources:** `ip_cidr`<br>**Targets:** `ip_cidr`<br>**Self edge:** no<br>**Checks:** `contains_cidr_proper_subnet.1`<br>**Canonicalizations:** —

### `contains_ip`

The source network contains the target address.

**Not modeled:** Not a claim that the address is in use; containment is arithmetic, checked on write.

**Identity:** —<br>**Sources:** `ip_cidr`<br>**Targets:** `ip_address`<br>**Self edge:** no<br>**Checks:** `contains_ip_member.1`<br>**Canonicalizations:** —

### `covers_name`

The certificate covers the target name, literally or through a wildcard SAN.

**Not modeled:** Not a name the certificate was merely presented for; that is `presents_certificate`.

**Identity:** `coverage`<br>**Sources:** `certificate`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule                       | Meaning                                                                            |
| ---------- | -------- | -------------------------- | ---------------------------------------------------------------------------------- |
| `coverage` | yes      | one of `exact`, `wildcard` | `exact` for a literal SAN, `wildcard` when a `*.` SAN covers the target base name. |

### `dname_to`

A DNAME record at the source name redirects its whole subtree to the target.

**Not modeled:** Not a single-name alias (`cname_to`).

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

### `exposes_secret`

The source exposes the secret at one location.

**Not modeled:** Never the secret itself; the node holds only its digest.

**Identity:** `location`<br>**Sources:** `repository`, `endpoint`, `storage_bucket`<br>**Targets:** `secret`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule                  | Meaning                                                                      |
| ---------- | -------- | --------------------- | ---------------------------------------------------------------------------- |
| `location` | yes      | `printable_text_1024` | Where the secret appears, such as a file path or URL path; identity-bearing. |

### `federates_with`

The name signs users in through the identity tenant.

**Not modeled:** Not ownership of the tenant by the name's registrant.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `identity_tenant`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_contact`

The source publishes the contact for one role.

**Not modeled:** Not a person record; person and company types are out of scope.

Use `published` for an address harvested from an organization's own surface with no declared role.

**Identity:** `role`<br>**Sources:** `organization`, `registrar`, `domain`, `subdomain`, `repository`<br>**Targets:** `email_address`, `phone`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule                                                                                     | Meaning                                                                           |
| -------- | -------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `role`   | yes      | one of `abuse`, `admin`, `tech`, `registrant`, `billing`, `noc`, `security`, `published` | What the contact is for; identity-bearing, so one address may hold several roles. |

### `has_dkim_selector`

The name publishes the DKIM selector; the scope relation of `dkim_record`.

**Not modeled:** Not a DNS delegation or subdomain relation.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `dkim_record`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_dmarc`

The name publishes the DMARC record at `_dmarc.<name>`.

**Not modeled:** Not a `_dmarc` subdomain node.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `dmarc_record`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_finding`

The finding is about the source object; the scope relation of `finding`.

**Not modeled:** Never from shared vocabulary such as a technology, fingerprint or CVE, which unrelated hosts share.

**Identity:** —<br>**Sources:** `port`, `domain`, `subdomain`, `ip_address`, `ip_cidr`, `service`, `endpoint`, `certificate`, `parameter`, `dkim_record`, `storage_bucket`, `repository`, `identity_tenant`, `secret`, `mta_sts_policy`<br>**Targets:** `finding`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_http_fingerprint`

The endpoint's response carries the fingerprint.

**Not modeled:** Not ownership: a shared fingerprint clusters, it does not attribute.

**Identity:** —<br>**Sources:** `endpoint`<br>**Targets:** `http_fingerprint`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_mail_exchange`

An MX record at the source names the target as a mail exchanger.

**Not modeled:** Not an address record; resolve the exchanger separately.

**Identity:** `preference`<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

| Property     | Required | Rule     | Meaning                              |
| ------------ | -------- | -------- | ------------------------------------ |
| `preference` | yes      | `uint16` | The MX preference; identity-bearing. |

### `has_mta_sts_policy`

The name publishes the MTA-STS policy record; its scope relation.

**Not modeled:** Not an `_mta-sts` subdomain node.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `mta_sts_policy`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_nameserver`

An NS record at the source delegates it to the target name server.

**Not modeled:** Not the SOA primary (`has_soa_primary`).

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

### `has_open_port`

The address has the port open; the scope relation of `port`.

**Not modeled:** Not a closed or filtered port.

**Identity:** —<br>**Sources:** `ip_address`<br>**Targets:** `port`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_parameter`

The endpoint accepts the parameter; the scope relation of `parameter`.

**Not modeled:** Not a parameter value.

**Identity:** —<br>**Sources:** `endpoint`<br>**Targets:** `parameter`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_service`

The port speaks the service; the scope relation of `service`.

**Not modeled:** Not the product implementing it (`runs_technology`).

**Identity:** —<br>**Sources:** `port`<br>**Targets:** `service`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_soa_primary`

The SOA record at the source names the target as its primary name server.

**Not modeled:** Not an NS delegation (`has_nameserver`).

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

### `has_spf`

The name publishes the SPF record.

**Not modeled:** Not a malformed SPF-like TXT value (`has_txt_record`).

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `spf_record`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_srv_target`

An SRV record at the source names the target host for a service.

**Not modeled:** Not an open port observation; the SRV port is what the record publishes.

**Identity:** `service`, `protocol`, `port`, `priority`, `weight`<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

| Property   | Required | Rule        | Meaning                                      |
| ---------- | -------- | ----------- | -------------------------------------------- |
| `port`     | yes      | `uint16`    | The published target port.                   |
| `priority` | yes      | `uint16`    | The SRV priority.                            |
| `protocol` | yes      | `srv_label` | The SRV protocol label, with its underscore. |
| `service`  | yes      | `srv_label` | The SRV service label, with its underscore.  |
| `weight`   | yes      | `uint16`    | The SRV weight.                              |

### `has_subdomain`

The target name lies below the source name.

**Not modeled:** Not a DNS delegation; the relation is naming structure, checked on write.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `subdomain`<br>**Self edge:** no<br>**Checks:** `has_subdomain_suffix.1`<br>**Canonicalizations:** —

### `has_svcb_binding`

An HTTPS or SVCB record at the source binds it to the target, which may be the owner itself.

**Not modeled:** Never an AliasMode record whose target is `.`, the wire's negative answer.

Write it only after parsing SvcParams: `alpn: []` asserts the record carries no ALPN parameter.

**Identity:** `record_type`, `priority`, `alpn`, with `alpn` hashed order-independently<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

| Property      | Required | Rule                   | Meaning                                              |
| ------------- | -------- | ---------------------- | ---------------------------------------------------- |
| `alpn`        | yes      | `alpn_tokens`          | ALPN ids from SvcParams, hashed order-independently. |
| `priority`    | yes      | `uint16`               | The SvcPriority; 0 is AliasMode.                     |
| `record_type` | yes      | one of `https`, `svcb` | Whether the record is HTTPS or SVCB.                 |

### `has_tls_fingerprint`

The service's TLS stack has the fingerprint.

**Not modeled:** Not a host identity: a shared fingerprint clusters stacks.

**Identity:** —<br>**Sources:** `service`<br>**Targets:** `tls_fingerprint`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_txt_record`

The name publishes the TXT value.

**Not modeled:** Not a value that belongs to a dedicated TXT type.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `txt_record`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `has_weakness`

The finding or CVE is an instance of the CWE weakness class.

**Not modeled:** Not a claim of exploitability.

**Identity:** —<br>**Sources:** `finding`, `cve`<br>**Targets:** `cwe`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `issued_by`

The certificate was issued by the target certificate; a self edge marks a self-signed one.

**Not modeled:** Not trust-store validation, which depends on the observer.

**Identity:** —<br>**Sources:** `certificate`<br>**Targets:** `certificate`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

### `operated_by`

The AS or network is held by the RIR organization.

**Not modeled:** Not domain registration, which has no RIR handle to key on.

**Identity:** —<br>**Sources:** `asn`, `ip_cidr`<br>**Targets:** `organization`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `owns_repository`

The name's owner is attributed the repository.

**Not modeled:** Not proof of ownership: it is an attribution claim that needs evidence.

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `repository`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `presents_certificate`

The service presented the certificate in a handshake.

**Not modeled:** Not the names the certificate covers (`covers_name`).

**Identity:** `mode`, `server_name`, `alpn_offered`, with `alpn_offered` hashed order-independently<br>**Sources:** `service`<br>**Targets:** `certificate`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

| Property       | Required | Rule                                     | Meaning                                           |
| -------------- | -------- | ---------------------------------------- | ------------------------------------------------- |
| `alpn_offered` | yes      | `alpn_tokens`                            | The ALPN ids offered, hashed order-independently. |
| `mode`         | yes      | one of `tls`, `dtls`, `starttls`, `quic` | How TLS was reached on the service.               |
| `server_name`  | yes      | `dns_or_explicit_empty`                  | The SNI name offered, or empty when none was.     |

### `presents_host_key`

The SSH service presented the host key.

**Not modeled:** Not a claim that two services are one host, though a shared key suggests it.

**Identity:** —<br>**Sources:** `service`<br>**Targets:** `host_key`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `protected_by`

Traffic to the source passes through the technology acting as a WAF, CDN, reverse proxy or load balancer.

**Not modeled:** Not a range-list classification of an address, and not SaaS hosting (`runs_technology`).

**Identity:** `kind`<br>**Sources:** `service`, `endpoint`, `domain`, `subdomain`<br>**Targets:** `technology`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule                                                  | Meaning                                               |
| -------- | -------- | ----------------------------------------------------- | ----------------------------------------------------- |
| `kind`   | yes      | one of `waf`, `cdn`, `reverse_proxy`, `load_balancer` | The role the technology plays in front of the source. |

### `redirects_to`

The endpoint answered with an HTTP redirect to the target endpoint.

**Not modeled:** Not a client-side or meta refresh redirect.

**Identity:** `status`<br>**Sources:** `endpoint`<br>**Targets:** `endpoint`<br>**Self edge:** yes<br>**Checks:** —<br>**Canonicalizations:** —

| Property | Required | Rule              | Meaning                                     |
| -------- | -------- | ----------------- | ------------------------------------------- |
| `status` | yes      | `redirect_status` | The redirect status code; identity-bearing. |

### `registered_through`

The domain is registered through the registrar.

**Not modeled:** Not a subdomain fact; a subdomain has no registrar.

**Identity:** —<br>**Sources:** `domain`<br>**Targets:** `registrar`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `resolves_to`

The name resolves to the address through an A or AAAA record.

**Not modeled:** Not a PTR record (`reverse_resolves_to`).

**Identity:** —<br>**Sources:** `domain`, `subdomain`<br>**Targets:** `ip_address`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `reverse_resolves_to`

A PTR record for the address names the target.

**Not modeled:** Not forward resolution (`resolves_to`).

**Identity:** —<br>**Sources:** `ip_address`<br>**Targets:** `domain`, `subdomain`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `runs_technology`

The source runs the technology, including a name pointing at a SaaS host.

**Not modeled:** Not a protective front such as a WAF or CDN (`protected_by`).

**Identity:** —<br>**Sources:** `service`, `endpoint`, `domain`, `subdomain`<br>**Targets:** `technology`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `serves_endpoint`

The service serves the endpoint.

**Not modeled:** Not a redirect target (`redirects_to`).

**Identity:** —<br>**Sources:** `service`<br>**Targets:** `endpoint`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —

### `supports_tls_cipher`

The service accepts the cipher suite.

**Not modeled:** Not the suite negotiated in one session.

**Identity:** —<br>**Sources:** `service`<br>**Targets:** `tls_cipher_suite`<br>**Self edge:** no<br>**Checks:** —<br>**Canonicalizations:** —
