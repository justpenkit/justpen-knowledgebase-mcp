"""Prose for every catalog type, property, format and rule, published by `kb_types` beside the contract.

Nothing here changes what the server accepts, so none of it is fingerprinted: a wording fix never
refuses a workspace. `catalog.py` refuses at import a declared type, property, format or rule id
without a description here, a description for something the contract does not declare, and a text
over its length cap, which keeps every `kb_types` page inside its response budget.
"""

from __future__ import annotations

from typing import Any

# Each type: `summary` says what real thing it models, `excludes` what it deliberately does not,
# `notes` carries the durable reasoning a writer needs, and `properties` one line per declared key.
DOCS: dict[str, Any] = {
    "common": {
        "additional_properties": "Properties outside the declared maps are accepted and stored unvalidated.",
        "coercion": 'No JSON type is converted. A string `"443"` is not an integer.',
        "depth": "Maximum nesting depth of the properties object.",
        "integers": "Integers outside signed 64-bit are rejected.",
        "numbers": "NaN and infinity are rejected.",
        "optional_nonnull": (
            "A declared optional property may be absent but never null; clear one with `remove_properties`."
        ),
        "properties_bytes": "Maximum size of one canonical properties object, in UTF-8 bytes.",
        "required_nonnull": "A required property may not be null or absent.",
    },
    "formats": {
        "alpn_tokens": (
            "An array of zero or more ASCII tokens matching `[A-Za-z0-9./_-]{1,255}`; order is not "
            "identity-significant and duplicates are preserved."
        ),
        "asn": "A strict JSON integer from 0 through 4294967295.",
        "boolean": 'A strict JSON `true` or `false`; the strings `"true"` and `"1"` and the number 1 are rejected.',
        "bucket_name": (
            "The provider-global name of an object-storage bucket: 3 to 222 lowercase ASCII characters from "
            "letters, digits, hyphen, underscore and dot, starting and ending alphanumeric, without a doubled "
            "dot, and never a dotted-quad IPv4 address. Every provider is stricter than this union rule, and "
            "the declared provider fixes which of the narrower spellings is accepted."
        ),
        "caa_parameters": (
            "An array of objects with name and value strings. Names start alphanumeric and continue "
            "alphanumeric or hyphen. Values are empty or use ASCII 0x21-0x3A and 0x3C-0x7E."
        ),
        "cidr": "Canonical strict IPv4 or IPv6 network with an explicit prefix length.",
        "cpe23": (
            "A lowercase CPE 2.3 formatted string (NIST IR 7695) of at most 512 characters: 'cpe:2.3:', the "
            "part, and ten colon-separated attributes, each '*', '-', or an escaped value optionally anchored "
            "by '*' or a run of '?'. The legacy 'cpe:/' URI binding that nmap prints is rejected; convert it."
        ),
        "credential_key_id": (
            "1 to 128 characters from ASCII letters, digits and `._:/+=-`: the public identifier half of a "
            "two-part credential, such as an AWS access key id, never secret material."
        ),
        "cve": "A string matching `CVE-[0-9]{4}-[0-9]{4,}` exactly.",
        "cwe": "A string matching `CWE-[0-9]{1,6}` exactly, uppercase as MITRE publishes it.",
        "dkim_selector": (
            "A lowercase ASCII DKIM selector of at most 253 bytes, as one or more dot-separated labels of at "
            "most 63 bytes each, written without the `_domainkey` suffix or the domain. In practice it is far "
            "shorter, since `<selector>._domainkey.<domain>` must itself fit in 253 bytes."
        ),
        "dmarc": (
            "Printable ASCII of at most 4096 characters beginning with 'v=DMARC1' followed by a semicolon, a "
            "normal space, or end of text."
        ),
        "dns_name": (
            "A lowercase ASCII domain or subdomain spelling classified by the bundled ICANN PSL; at least two "
            "labels, labels at most 63 bytes, total at most 253 bytes, and no trailing dot."
        ),
        "dns_or_explicit_empty": "dns_name or the explicit empty string for no SNI offer; IP literals are rejected.",
        "email_address": (
            "A lowercase ASCII mailbox of at most 254 characters: an RFC 5322 dot-atom local part of at most 64 "
            "characters, one `@`, and a domain that satisfies dns_name. Quoted local parts, address literals "
            "and display names are rejected. A local part is case-sensitive on the wire; this rule requires "
            "the lowercase spelling anyway, so one mailbox is one node."
        ),
        "epp_status_list": (
            "A non-empty array of EPP domain status codes in their camelCase spelling, from RFC 5731 and the "
            "RFC 3915 grace periods, sorted ascending with no duplicate. Convert RDAP's spaced words: "
            "`client transfer prohibited` is `clientTransferProhibited`, `active` is `ok`."
        ),
        "http_fingerprint_value": (
            "Either a signed 32-bit decimal integer written in ASCII without a leading zero or a plus sign, for "
            "a MurmurHash3 favicon hash, or exactly 64 lowercase hexadecimal characters for a response digest. "
            "The declared kind fixes which one is accepted."
        ),
        "http_status": "A strict JSON integer HTTP status code from 100 through 599.",
        "http_url": (
            "Canonical absolute ASCII http/https URL with a lowercase host (underscore labels are accepted left of "
            "the registrable domain, as for subdomain), mandatory path, no userinfo, "
            "fragment, whitespace, backslash, Unicode, default explicit port, dot path segment, or lowercase "
            "percent escape. A submitted query string is validated with the rest of the URL and then removed "
            "before identity and storage, so one endpoint holds one path; parameter names belong to parameter nodes."
        ),
        "ip": (
            "Canonical IPv4Address.compressed or lowercase IPv6Address.compressed spelling, without scope or "
            "prefix. An IPv4-mapped IPv6 address (`::ffff:192.0.2.1`) is rejected: write the IPv4 address."
        ),
        "ip_version": "A strict JSON integer equal to 4 or 6.",
        "iso3166_alpha2": (
            "Two uppercase ASCII letters in the ISO 3166-1 alpha-2 shape, as registries publish a country: "
            "`US`, `DE`. Shape only; membership in the ISO list is not checked."
        ),
        "media_type": (
            "A lowercase media type essence `type/subtype` of RFC 6838 restricted names, without parameters: "
            "`text/html`, never `text/html; charset=utf-8` or `Text/HTML`."
        ),
        "method": "One to 32 characters matching an uppercase HTTP method token.",
        "mta_sts": (
            "Printable ASCII of at most 4096 characters beginning with 'v=STSv1' followed by a semicolon, a "
            "normal space, or end of text: the TXT record at `_mta-sts.<domain>`, not the policy file body."
        ),
        "mx_pattern_list": (
            "An array of RFC 8461 `mx` patterns, each a lowercase dns_name or `*.` followed by one, sorted "
            "ascending with no duplicate, so one policy has one spelling."
        ),
        "parameter_name": (
            "1 to 128 printable ASCII characters without space, `&`, `=`, or `#`; one single parameter name, "
            "never a raw query string."
        ),
        "phone_e164": "An E.164 number: `+`, a leading digit from 1 through 9, and in total 2 to 15 digits.",
        "printable_text_1024": "A string of 1-1024 printable Unicode characters.",
        "printable_text_200": "A string of 1-200 printable Unicode characters.",
        "public_suffix": (
            "A lowercase ASCII zone under which names are registered, per the bundled ICANN PSL: one more "
            "label makes a registrable domain, so `com` and `co.uk` qualify and `example.com` does not."
        ),
        "redirect_status": "A strict JSON integer in 301, 302, 303, 307, or 308.",
        "registry_domain_id": (
            "An RFC 5730 repository object id in ASCII, as the registry publishes it in the RDAP `handle` or the "
            "WHOIS `Registry Domain ID`: 1-80 letters, digits or underscores, a hyphen, and a 1-8 character "
            "alphanumeric repository suffix, such as `2138514_DOMAIN_COM-VRSN`. Case is preserved."
        ),
        "repo_name": (
            "A 1-100 character lowercase ASCII repository name from letters, digits, dot, underscore and "
            "hyphen, holding at least one alphanumeric character and never the reserved `.` or `..`. Hosting "
            "platforms resolve names case-insensitively, so the lowercase spelling keeps one repository one node."
        ),
        "repo_owner": (
            "A 1-255 character lowercase ASCII owner path of one or more `/`-separated segments, each 1-100 "
            "characters from letters, digits, dot, underscore and hyphen and each starting and ending "
            "alphanumeric. The separator exists for nested GitLab groups; the declared platform fixes whether "
            "more than one segment is accepted."
        ),
        "rir_handle": (
            "A regional-registry object handle of 2 to 64 ASCII characters, starting and ending alphanumeric "
            "and continuing alphanumeric or hyphen. Handles are case-sensitive and stored exactly as the "
            "registry publishes them: RIPE and AFRINIC preserve the organisation name's case (ORG-nG51-RIPE), "
            "so never uppercase one. Handles are unique within one registry, never across registries."
        ),
        "service_name": "A member of the bundled versioned service name whitelist.",
        "sha256": "Exactly 64 lowercase ASCII hexadecimal characters.",
        "spf": (
            "Printable ASCII of at most 4096 characters beginning with `v=spf1` followed by a normal space or end "
            "of text, the same bound as every other TXT value rule."
        ),
        "srv_label": "A 2-63 byte lowercase ASCII SRV label beginning with underscore.",
        "tech_token": (
            "A 1-63 character lowercase ASCII technology slug that starts and ends alphanumeric and may contain "
            "interior dot, underscore, plus, or hyphen."
        ),
        "tech_version": "1 to 64 printable ASCII characters without space: a version string as the product reports it.",
        "tenant_id": (
            "A 1-128 character lowercase ASCII identity-tenant identifier that starts and ends alphanumeric and "
            "may contain interior dot, underscore or hyphen. The declared provider fixes the narrower spelling: "
            "a canonical lowercase UUID for Entra ID, a bare organization slug for Okta."
        ),
        "tls_cipher_name": (
            "The spelling of an IANA TLS cipher suite name: 5 to 128 uppercase ASCII characters beginning "
            "'TLS_', with underscore-separated alphanumeric components. Shape only; membership in the IANA "
            "registry is not checked."
        ),
        "tls_fingerprint_value": (
            "Exactly 32 lowercase hexadecimal characters for a JA3S MD5 digest, or exactly 62 for a JARM "
            "fingerprint. The declared kind fixes which length is accepted."
        ),
        "txt_value": (
            "1 to 4096 printable ASCII characters, the concatenated and unquoted character-strings of one TXT RRset."
        ),
        "uint8": "A strict JSON integer from 0 through 255.",
        "uint16": "A strict JSON integer from 0 through 65535.",
        "uint32": "A strict JSON integer from 0 through 4294967295.",
        "uint63": "A strict JSON integer from 0 through 9223372036854775807.",
        "utc_timestamp": (
            "An instant in UTC as `YYYY-MM-DDTHH:MM:SSZ` with an optional 1-6 digit fraction before the `Z`. "
            "Offsets are rejected, so one instant has one spelling and stored values sort and compare as text."
        ),
    },
    "checks": {
        "asn_assigned.1": "`value` 0 is rejected: AS0 is reserved and never originates routes (RFC 7607).",
        "bucket_name_spelling.1": (
            "`name` is checked against the declared `provider`: length, grammar, and the prefixes, suffixes and "
            "substrings that provider reserves."
        ),
        "contains_cidr_proper_subnet.1": (
            "The target network must be a proper subnet of the source, at the same IP version."
        ),
        "contains_ip_member.1": "The target address must fall inside the source network, at the same IP version.",
        "cpe_product_level.1": (
            "A `cpe` on a `technology` must leave the version attribute `*` or `-`: the node is shared by every "
            "host, so a versioned CPE belongs on the `runs_technology` edge beside `version`."
        ),
        "dns_name_kind.1": (
            "`value` must classify as this type against the bundled PSL: a registrable domain for `domain`, a "
            "name below one for `subdomain`."
        ),
        "has_contact_registration_roles.1": (
            "From a `whois_registration`, `role` must be `registrant`, `admin`, `tech` or `billing`; from a "
            "`domain` or `subdomain`, those four roles are refused, because they belong to one registration."
        ),
        "has_registration_suffix_match.1": (
            "The registration's `registry` must be the zone of its domain: the domain's `value` without its "
            "first label."
        ),
        "has_subdomain_suffix.1": "The target's `value` must end in `.` plus the source's `value`.",
        "http_fingerprint_value_kind.1": (
            "`favicon_mmh3` requires the signed 32-bit integer spelling; `body_sha256` and `header_sha256` "
            "require 64 lowercase hex characters."
        ),
        "ip_address_version.1": "`version` must equal the version of the address in `value`.",
        "ip_cidr_version.1": "`version` must equal the version of the network in `value`.",
        "port_number_assigned.1": (
            "`number` 0 is rejected: it is what a tool emits for a missing port, and no service listens there. "
            "The `uint16` format keeps 0 because SRV and MX values may legitimately be 0."
        ),
        "registry_domain_id_assigned.1": (
            "`registry_domain_id` is rejected when its part before the last hyphen is all zeros or a redaction "
            "word such as `REDACTED`, `NONE`, `NA`, `UNKNOWN`, `PRIVATE`, `WITHHELD` or `NOTDISCLOSED`, in any case."
        ),
        "registrar_iana_assigned.1": (
            "`iana_id` must be at least 1, because 0 is what an agent emits for a missing field."
        ),
        "repository_owner_spelling.1": (
            "`owner` is checked against the grammar and length of the declared `platform`, and only `gitlab` "
            "accepts a `/` for nested groups."
        ),
        "secret_plaintext_keys.1": (
            "The record is rejected if any key at any depth, in any case, is `value`, `secret`, `plaintext`, "
            "`password`, `token`, `key`, `credential`, `match`, `raw`, `rawv2`, `redacted` or `line`, so the "
            "credential itself cannot reach storage."
        ),
        "service_secure_flag.1": "A TLS-capable registry entry, such as `http`, additionally requires a boolean `secure`.",
        "tenant_id_spelling.1": (
            "`entra_id` requires a canonical lowercase UUID; `okta` requires the bare organization slug, so a "
            "dot is rejected."
        ),
        "tls_fingerprint_length.1": "`value` must be 62 characters for `jarm` and 32 for `ja3s`.",
        "txt_record_diversion.1": (
            "A `value` that the dedicated type for its version tag accepts is rejected here: `v=spf1` belongs "
            "to `spf_record`, `v=DMARC1` to `dmarc_record`, `v=DKIM1` to `dkim_record` and `v=STSv1` to "
            "`mta_sts_policy`. A malformed tagged value, such as `v=spf1include:...`, stays a `txt_record`."
        ),
    },
    "canonicalizations": {
        "caa_parameter_name_fold.1": (
            "ASCII parameter names are lowercased, because RFC 8659 tags are case-insensitive while the "
            "parameter list is identity-bearing. A non-ASCII name is left for validation to reject."
        ),
        "endpoint_url_drop_query.1": (
            "Once the whole `url`, query included, passes `http_url`, everything from its first `?` is removed "
            "before hashing and storage, so one path is one endpoint. A URL whose query is malformed is left "
            "unchanged and rejected."
        ),
    },
    "nodes": {
        "asn": {
            "summary": "An autonomous system number: the routing identity a network is announced from.",
            "excludes": (
                "Not the organization holding it (`organization` through `operated_by`) and not the prefixes it "
                "announces (`ip_cidr` through `announced_by`)."
            ),
            "properties": {
                "value": "The AS number as an integer, without the `AS` prefix.",
                "name": "The holder's AS name as the registry or routing data publishes it; the latest write wins.",
                "country": "The country the registry records for the AS.",
                "rir": "The regional registry that assigned the AS.",
            },
        },
        "certificate": {
            "summary": "One X.509 certificate, identified by the SHA-256 of its DER encoding.",
            "excludes": (
                "Not the handshake that presented it (`presents_certificate` from a `service`) and not the names "
                "it covers, which are `covers_name` edges."
            ),
            "notes": (
                "A self edge through `issued_by` records a self-signed certificate. Absence of that edge means "
                "the issuer was never written, not that the chain ends."
            ),
            "properties": {
                "der_sha256": "Lowercase hex SHA-256 of the certificate's DER bytes.",
                "self_signed": "True when the subject signed itself; keep it beside the `issued_by` self edge.",
            },
        },
        "cve": {
            "summary": "A published CVE record, shared by every object affected by it.",
            "excludes": (
                "Not an observation that something is vulnerable; that is `affected_by` from the affected "
                "service, endpoint or finding. Never a finding source."
            ),
            "properties": {"value": "The CVE id, uppercase as MITRE publishes it."},
        },
        "cwe": {
            "summary": "A CWE weakness class: the canonical spelling that joins findings from different scanners.",
            "excludes": "Not a finding or an advisory; those reach it through `has_weakness`. Never a finding source.",
            "properties": {
                "value": "The CWE id as MITRE publishes it, such as `CWE-79`.",
                "name": "MITRE's name for the weakness class, fed by the CWE catalog rather than a scanner.",
            },
        },
        "dkim_record": {
            "summary": "The DKIM key record one domain publishes for one selector, scoped through `has_dkim_selector`.",
            "excludes": "Not the `<selector>._domainkey` owner name as a subdomain node.",
            "notes": (
                "Writing the same selector again patches `value` in place, so the node is a current-state view; "
                "a rotated key survives only in evidence attached to the earlier write."
            ),
            "properties": {
                "selector": "The selector labels, without `._domainkey` or the domain.",
                "value": "The normalized TXT value: unquoted, unescaped, with multi-string RRsets concatenated.",
            },
        },
        "dmarc_record": {
            "summary": "A DMARC policy value, shared by every name that publishes the same string.",
            "excludes": "Not the `_dmarc` owner name; `has_dmarc` attaches the record to the domain or subdomain itself.",
            "properties": {"value": "The normalized TXT value beginning `v=DMARC1`."},
        },
        "domain": {
            "summary": "A registrable domain name as the bundled public suffix list classifies it, such as `example.co.uk`.",
            "excludes": (
                "Not a name below a registrable domain (`subdomain`) and not a public suffix. Registration "
                "dates, status and DNSSEC state belong to a `whois_registration`, not to the name."
            ),
            "properties": {"value": "The lowercase ASCII name without a trailing dot; IDNs in punycode."},
        },
        "email_address": {
            "summary": "A mailbox, reached as a contact through `has_contact`.",
            "excludes": "Not a person; person and company types are out of scope. Never a finding source.",
            "properties": {"value": "The mailbox, lowercase including the local part."},
        },
        "endpoint": {
            "summary": "One HTTP method on one URL path: the unit a crawler or scanner observes.",
            "excludes": (
                "Not a query string or its values (parameter names are `parameter` nodes) and not the host or "
                "service that serves it."
            ),
            "notes": (
                "Response digests are pivots on `http_fingerprint` nodes, not endpoint attributes. Raw response "
                "bodies and headers belong in evidence, where they are full-text indexed. Only the query is "
                "removed from `url`: a secret carried in the path, such as a webhook token, stays in it."
            ),
            "properties": {
                "url": "Absolute canonical http/https URL; a valid submitted query string is removed before identity.",
                "method": "The uppercase HTTP method token.",
                "status": "The status code of the latest response observed.",
                "title": "The HTML title of the latest response.",
                "content_length": "The body length in bytes of the latest response.",
                "content_type": "The media type essence of the latest response, without parameters.",
                "webserver": "The `Server` header value of the latest response.",
            },
        },
        "finding": {
            "summary": "One issue a scanner or analyst reports about one object, scoped to it through `has_finding`.",
            "excludes": (
                "Not the weakness class (`cwe`) or a published advisory (`cve`); those link through "
                "`has_weakness` and `affected_by`."
            ),
            "properties": {
                "title": "The finding title; with the parent, it identifies the finding.",
                "severity": "The reporter's severity; the latest write wins.",
            },
        },
        "host_key": {
            "summary": "An SSH host public key, shared by every service that presents it.",
            "excludes": (
                "Not a host. Two hosts presenting one key are a cloned image or one machine at two addresses, "
                "which makes this a correlation pivot. Never a finding source."
            ),
            "properties": {
                "algorithm": "The SSH public key algorithm name.",
                "fingerprint_sha256": (
                    "Lowercase hex SHA-256 of the raw public key blob, converted from OpenSSH's base64 `SHA256:` form."
                ),
            },
        },
        "http_fingerprint": {
            "summary": "A response-side clustering pivot: a favicon hash or a body or header digest.",
            "excludes": (
                "Not an identifier of a host or an owner; a shared default favicon or framework page is "
                "common. Never a finding source."
            ),
            "properties": {
                "kind": "Which fingerprint `value` holds.",
                "value": "Decimal signed 32-bit MurmurHash3 for `favicon_mmh3`, lowercase hex SHA-256 otherwise.",
            },
        },
        "identity_tenant": {
            "summary": "An identity-provider tenant, such as an Entra ID directory or an Okta organization.",
            "excludes": "Not a cloud billing account or subscription and not a user.",
            "properties": {
                "provider": "The identity provider; each member fixes one canonical `tenant_id` spelling.",
                "tenant_id": "The tenant identifier in the provider's canonical spelling.",
            },
        },
        "ip_address": {
            "summary": "One IPv4 or IPv6 address.",
            "excludes": "Not a network (`ip_cidr`) and not a name that resolves to it (`resolves_to`).",
            "properties": {
                "value": "The canonical compressed address spelling.",
                "version": "4 or 6, matching `value`.",
            },
        },
        "ip_cidr": {
            "summary": "One IPv4 or IPv6 network with an explicit prefix length, as allocated, announced or scanned.",
            "excludes": "Not a single address (`ip_address`) and not its holder (`organization` through `operated_by`).",
            "properties": {
                "value": "The canonical network spelling, host bits zero.",
                "version": "4 or 6, matching `value`.",
                "netname": "The registry network name (RDAP `name`, WHOIS `netname`).",
                "country": "The country the registry records for the network.",
                "rir": "The regional registry that allocated the network.",
            },
        },
        "mta_sts_policy": {
            "summary": "The MTA-STS TXT record a name publishes at `_mta-sts.<name>`, scoped through `has_mta_sts_policy`.",
            "excludes": "Not the policy file body served over HTTPS; its fields are attributes of this node.",
            "notes": (
                "Scoped because the TXT value is only a version pointer that unrelated tenants publish verbatim; "
                "unscoped, their policy attributes would overwrite each other."
            ),
            "properties": {
                "value": "The TXT value beginning `v=STSv1`.",
                "mode": "The policy file's `mode`.",
                "max_age": "The policy file's `max_age`, in seconds.",
                "mx": "The policy file's `mx` patterns, sorted and without duplicates.",
            },
        },
        "organization": {
            "summary": "A number-resource holder known to a regional internet registry, keyed on the registry and handle.",
            "excludes": "Not a company in general and not a domain registrant; person and company types are out of scope.",
            "properties": {
                "registry": "The RIR that issued the handle.",
                "handle": "The RIR object handle, case preserved exactly as the registry publishes it.",
                "name": "The organization's name as the registry publishes it; free text, so never identity.",
            },
        },
        "parameter": {
            "summary": "One named input of one endpoint, by name and location, scoped through `has_parameter`.",
            "excludes": "Not a value seen for the parameter; values are sample data for evidence.",
            "properties": {
                "name": "One parameter name, never a raw query string.",
                "location": "Where the parameter travels in the request.",
            },
        },
        "phone": {
            "summary": "A telephone contact, reached through `has_contact`.",
            "excludes": "Not a person or a subscriber. Never a finding source.",
            "properties": {"value": "The E.164 number with its leading plus."},
        },
        "port": {
            "summary": "One open transport port on one address, scoped to its `ip_address` through `has_open_port`.",
            "excludes": "Not a closed or filtered port: only open ports are written, and the scope edge is the state.",
            "properties": {
                "number": "The port number.",
                "transport": "The transport protocol.",
            },
        },
        "registrar": {
            "summary": "An ICANN-accredited registrar, keyed on its IANA registrar id.",
            "excludes": "Not a registry or a reseller. A registrar without an IANA id gets no node.",
            "properties": {
                "iana_id": "The IANA registrar id.",
                "name": "The registrar's name; the latest write wins, because names change with renames and mergers.",
            },
        },
        "repository": {
            "summary": "A source-code repository on one hosting instance, keyed on the host, owner and name.",
            "excludes": "Not an organization or a person. `owns_repository` attributes it to a name and needs evidence.",
            "properties": {
                "platform": "The hosting software, which selects the owner grammar; not identity.",
                "host": "The hosting instance's host name.",
                "owner": "The owner path, lowercase; only `gitlab` nests groups.",
                "name": "The repository name, lowercase.",
            },
        },
        "secret": {
            "summary": "One exposed credential, identified only by the SHA-256 of the secret so its occurrences join.",
            "excludes": "Never the secret itself: a node carrying a plaintext-bearing key is rejected.",
            "notes": (
                "Hash the secret part's UTF-8 bytes untrimmed: trufflehog `Raw` or the secret half of a "
                "two-part `RawV2`, gitleaks `Secret` (never `Match`), a leak corpus password or its published "
                "hash; in PEM, CRLF becomes LF. Compute it, then replace every secret field with `[REDACTED]` "
                "before ingesting the output as evidence. A password digest is unsalted and reversible by "
                "dictionary: treat the workspace as holding the passwords. Always send `kind`."
            ),
            "properties": {
                "value_sha256": "Lowercase hex SHA-256 of the exact secret bytes.",
                "kind": "What the secret is; send it whenever the source says, and `other` only when it cannot.",
                "detector": "The scanner rule that found it, such as trufflehog's `aws`; not identity.",
                "verified": "Whether the scanner confirmed the credential works; the latest write wins.",
                "key_id": "The public identifier half of a two-part credential, such as an AWS access key id.",
            },
        },
        "service": {
            "summary": "The application protocol a port speaks, scoped to that port through `has_service`.",
            "excludes": "Not the product or version implementing it; that is `runs_technology`.",
            "properties": {"name": "A member of the bundled Nmap-derived service-name registry."},
        },
        "spf_record": {
            "summary": "An SPF policy value, shared by every name that publishes the same string.",
            "excludes": "Not the publishing name (`has_spf`) and not a malformed SPF-like value, which is a `txt_record`.",
            "properties": {"value": "The normalized TXT value beginning `v=spf1`."},
        },
        "storage_bucket": {
            "summary": "An object-storage bucket in a provider-global namespace.",
            "excludes": (
                "Not an Azure container (the node is the storage account) and not a regional namespace such as "
                "DigitalOcean Spaces."
            ),
            "properties": {
                "provider": "The storage provider, whose naming rules the name must satisfy.",
                "name": "The provider-global bucket name, or the Azure storage account name.",
            },
        },
        "subdomain": {
            "summary": "A DNS name below a registrable domain, such as `api.example.com`.",
            "excludes": "Not the registrable domain itself (`domain`).",
            "properties": {"value": "The lowercase ASCII name without a trailing dot; IDNs in punycode."},
        },
        "technology": {
            "summary": "A product, framework or service slug, shared by every host that runs it.",
            "excludes": "Not a per-host version, which belongs on `runs_technology`. Never a finding source.",
            "notes": (
                "The slug has no naming authority; use the product's Wappalyzer name lowercased with spaces as "
                "hyphens, and put the vendor's product-level CPE in `cpe` to anchor it."
            ),
            "properties": {
                "name": "The lowercase product slug.",
                "cpe": "The product-level CPE 2.3 name, with the version attribute `*` or `-`.",
            },
        },
        "tls_cipher_suite": {
            "summary": "An IANA TLS cipher suite at one protocol version, shared by every service accepting it.",
            "excludes": "Not a negotiated session. Never a finding source.",
            "properties": {
                "version": "The protocol version the suite was offered at.",
                "name": "The IANA cipher suite name.",
            },
        },
        "tls_fingerprint": {
            "summary": "A TLS stack clustering pivot, JARM or JA3S, shared by every host behind one stack.",
            "excludes": "Not a host identifier: every host behind one load balancer presents the same JARM.",
            "properties": {
                "kind": "Which fingerprint `value` holds.",
                "value": "The fingerprint: 62 characters for JARM, 32 for JA3S.",
            },
        },
        "whois_registration": {
            "summary": (
                "One registry-level registration of a registrable domain, known by the repository object id the "
                "registry assigns (RDAP `handle`, WHOIS `Registry Domain ID`) and scoped through `has_registration`."
            ),
            "excludes": (
                "Not the domain name, which outlives registrations; not the registrant, since person and company "
                "types are out of scope; and not a WHOIS or RDAP response, which is evidence."
            ),
            "notes": (
                "A drop and re-registration gets a new object id, so a new node, and the old one keeps its dates. "
                "When the registry publishes no id, or only a redaction placeholder, write no registration: keep "
                "the response as evidence on the domain and never invent a key."
            ),
            "properties": {
                "registry": "The zone the name is registered in, such as `com` or `co.uk`: the domain without its first label.",
                "registry_domain_id": "The registry's repository object id for this registration, exactly as published.",
                "registration_created": "When the registration was created (RDAP `registration` event).",
                "registration_updated": "When the registry last changed it (RDAP `last changed` event).",
                "registration_expires": "When it expires unless renewed (RDAP `expiration` event).",
                "epp_status": "The EPP status codes the registry reports, sorted.",
                "dnssec_signed": "Whether the registry holds DS records for the name (RDAP `delegationSigned`).",
                "whois_server": "The registry's port-43 WHOIS server, never the registrar's.",
            },
        },
        "txt_record": {
            "summary": "A generic TXT value a name publishes, when no dedicated type accepts it.",
            "excludes": (
                "Not an SPF, DMARC, DKIM or MTA-STS value its dedicated type accepts, and never an ephemeral "
                "`_acme-challenge` value."
            ),
            "properties": {"value": "The normalized TXT value."},
        },
    },
    "relations": {
        "affected_by": {
            "summary": "The source is affected by the CVE: a vulnerable service, an endpoint a CVE template matched, or a finding reporting it.",
            "excludes": "Not a CVE merely mentioned nearby; attach the evidence for the match.",
            "properties": {},
        },
        "announced_by": {
            "summary": "The network is announced in BGP by the AS.",
            "excludes": "Not the holder of the network; that is `operated_by`.",
            "properties": {},
        },
        "backed_by_bucket": {
            "summary": "A name or URL serves content from the bucket.",
            "excludes": "Not a bucket found only by guessing names; write that bucket node with its evidence instead.",
            "properties": {},
        },
        "caa_issue": {
            "summary": "A CAA `issue` property at the source name authorizes the issuer domain named by the target.",
            "excludes": "Not a wildcard authorization (`caa_issuewild`) and not an iodef reporting address.",
            "properties": {
                "flags": "The CAA flags octet.",
                "parameters": "The property's parameters; names are lowercased and the list is hashed order-independently.",
            },
        },
        "caa_issuewild": {
            "summary": "A CAA `issuewild` property at the source name authorizes the target issuer for wildcard names.",
            "excludes": "Not a non-wildcard authorization (`caa_issue`).",
            "properties": {
                "flags": "The CAA flags octet.",
                "parameters": "The property's parameters; names are lowercased and the list is hashed order-independently.",
            },
        },
        "cname_to": {
            "summary": "A CNAME record at the source name points to the target name.",
            "excludes": "Not resolution to an address (`resolves_to`) and not a subtree redirect (`dname_to`).",
            "properties": {},
        },
        "contains_cidr": {
            "summary": "The source network properly contains the target network.",
            "excludes": "Not an allocation or announcement fact; containment is arithmetic, checked on write.",
            "properties": {},
        },
        "contains_ip": {
            "summary": "The source network contains the target address.",
            "excludes": "Not a claim that the address is in use; containment is arithmetic, checked on write.",
            "properties": {},
        },
        "covers_name": {
            "summary": "The certificate covers the target name, literally or through a wildcard SAN.",
            "excludes": "Not a name the certificate was merely presented for; that is `presents_certificate`.",
            "properties": {
                "coverage": "`exact` for a literal SAN, `wildcard` when a `*.` SAN covers the target base name.",
            },
        },
        "dname_to": {
            "summary": "A DNAME record at the source name redirects its whole subtree to the target.",
            "excludes": "Not a single-name alias (`cname_to`).",
            "properties": {},
        },
        "exposes_secret": {
            "summary": "The source exposes the secret at one location.",
            "excludes": "Never the secret itself; the node holds only its digest, and plaintext-bearing keys are refused.",
            "notes": "Link evidence only after replacing every secret-bearing scanner field with `[REDACTED]`.",
            "properties": {"location": "Where the secret appears, such as a file path or URL path; identity-bearing."},
        },
        "federates_with": {
            "summary": "The name signs users in through the identity tenant.",
            "excludes": "Not ownership of the tenant by the name's registrant.",
            "properties": {
                "namespace_type": "How the realm lookup classifies the name: `managed` or `federated`.",
            },
        },
        "has_contact": {
            "summary": "The source publishes the contact for one role.",
            "excludes": "Not a person record; person and company types are out of scope.",
            "notes": (
                "Use `published` for an address harvested from an organization's own surface with no declared "
                "role. Registrant, admin, tech and billing contacts come from registration data and attach to "
                "the `whois_registration`, so a re-registration does not inherit them."
            ),
            "properties": {"role": "What the contact is for; identity-bearing, so one address may hold several roles."},
        },
        "has_dkim_selector": {
            "summary": "The name publishes the DKIM selector; the scope relation of `dkim_record`.",
            "excludes": "Not a DNS delegation or subdomain relation.",
            "properties": {},
        },
        "has_dmarc": {
            "summary": "The name publishes the DMARC record at `_dmarc.<name>`.",
            "excludes": "Not a `_dmarc` subdomain node.",
            "properties": {},
        },
        "has_finding": {
            "summary": "The finding is about the source object; the scope relation of `finding`.",
            "excludes": "Never from shared vocabulary such as a technology, fingerprint or CVE, which unrelated hosts share.",
            "properties": {},
        },
        "has_http_fingerprint": {
            "summary": "The endpoint's response carries the fingerprint.",
            "excludes": "Not ownership: a shared fingerprint clusters, it does not attribute.",
            "properties": {},
        },
        "has_mail_exchange": {
            "summary": "An MX record at the source names the target as a mail exchanger.",
            "excludes": "Not an address record; resolve the exchanger separately.",
            "properties": {"preference": "The MX preference; identity-bearing."},
        },
        "has_mta_sts_policy": {
            "summary": "The name publishes the MTA-STS policy record; its scope relation.",
            "excludes": "Not an `_mta-sts` subdomain node.",
            "properties": {},
        },
        "has_nameserver": {
            "summary": "An NS record at the source delegates it to the target name server.",
            "excludes": "Not the SOA primary (`has_soa_primary`).",
            "properties": {},
        },
        "has_open_port": {
            "summary": "The address has the port open; the scope relation of `port`.",
            "excludes": "Not a closed or filtered port.",
            "properties": {},
        },
        "has_parameter": {
            "summary": "The endpoint accepts the parameter; the scope relation of `parameter`.",
            "excludes": "Not a parameter value.",
            "properties": {},
        },
        "has_registration": {
            "summary": "The domain has this registration; the scope relation of `whois_registration`.",
            "excludes": "Not a registrar relationship, which is `registered_through` from the registration.",
            "properties": {},
        },
        "has_service": {
            "summary": "The port speaks the service; the scope relation of `service`.",
            "excludes": "Not the product implementing it (`runs_technology`).",
            "properties": {},
        },
        "has_soa_primary": {
            "summary": "The SOA record at the source names the target as its primary name server.",
            "excludes": "Not an NS delegation (`has_nameserver`).",
            "properties": {},
        },
        "has_spf": {
            "summary": "The name publishes the SPF record.",
            "excludes": "Not a malformed SPF-like TXT value (`has_txt_record`).",
            "properties": {},
        },
        "has_srv_target": {
            "summary": "An SRV record at the source names the target host for a service.",
            "excludes": "Not an open port observation; the SRV port is what the record publishes.",
            "properties": {
                "service": "The SRV service label, with its underscore.",
                "protocol": "The SRV protocol label, with its underscore.",
                "port": "The published target port.",
                "priority": "The SRV priority.",
                "weight": "The SRV weight.",
            },
        },
        "has_subdomain": {
            "summary": "The target name lies below the source name.",
            "excludes": "Not a DNS delegation; the relation is naming structure, checked on write.",
            "properties": {},
        },
        "has_svcb_binding": {
            "summary": "An HTTPS or SVCB record at the source binds it to the target, which may be the owner itself.",
            "excludes": "Never an AliasMode record whose target is `.`, the wire's negative answer.",
            "notes": "Write it only after parsing SvcParams: `alpn: []` asserts the record carries no ALPN parameter.",
            "properties": {
                "record_type": "Whether the record is HTTPS or SVCB.",
                "priority": "The SvcPriority; 0 is AliasMode.",
                "alpn": "ALPN ids from SvcParams, hashed order-independently.",
            },
        },
        "has_tls_fingerprint": {
            "summary": "The service's TLS stack has the fingerprint.",
            "excludes": "Not a host identity: a shared fingerprint clusters stacks.",
            "properties": {},
        },
        "has_txt_record": {
            "summary": "The name publishes the TXT value.",
            "excludes": "Not a value that belongs to a dedicated TXT type.",
            "properties": {},
        },
        "has_weakness": {
            "summary": "The finding or CVE is an instance of the CWE weakness class.",
            "excludes": "Not a claim of exploitability.",
            "properties": {},
        },
        "issued_by": {
            "summary": "The certificate was issued by the target certificate; a self edge marks a self-signed one.",
            "excludes": "Not trust-store validation, which depends on the observer.",
            "properties": {},
        },
        "operated_by": {
            "summary": "The AS or network is held by the RIR organization.",
            "excludes": "Not domain registration, which has no RIR handle to key on.",
            "properties": {},
        },
        "owns_repository": {
            "summary": "The name's owner is attributed the repository.",
            "excludes": "Not proof of ownership: it is an attribution claim that needs evidence.",
            "properties": {},
        },
        "presents_certificate": {
            "summary": "The service presented the certificate in a handshake.",
            "excludes": "Not the names the certificate covers (`covers_name`).",
            "properties": {
                "mode": "How TLS was reached on the service.",
                "server_name": "The SNI name offered, or empty when none was.",
                "alpn_offered": "The ALPN ids offered, hashed order-independently.",
            },
        },
        "presents_host_key": {
            "summary": "The SSH service presented the host key.",
            "excludes": "Not a claim that two services are one host, though a shared key suggests it.",
            "properties": {},
        },
        "protected_by": {
            "summary": "Traffic to the source passes through the technology acting as a WAF, CDN, reverse proxy or load balancer.",
            "excludes": "Not a range-list classification of an address, and not SaaS hosting (`runs_technology`).",
            "properties": {"kind": "The role the technology plays in front of the source."},
        },
        "redirects_to": {
            "summary": "The endpoint answered with an HTTP redirect to the target endpoint.",
            "excludes": "Not a client-side or meta refresh redirect.",
            "properties": {"status": "The redirect status code; identity-bearing."},
        },
        "registered_through": {
            "summary": "The registration is sponsored by the registrar.",
            "excludes": (
                "Not a fact about the name across time: after a transfer or a re-registration, each registration "
                "keeps its own registrar edge."
            ),
            "properties": {},
        },
        "resolves_to": {
            "summary": "The name resolves to the address through an A or AAAA record.",
            "excludes": "Not a PTR record (`reverse_resolves_to`).",
            "properties": {},
        },
        "reverse_resolves_to": {
            "summary": "A PTR record for the address names the target.",
            "excludes": "Not forward resolution (`resolves_to`).",
            "properties": {},
        },
        "runs_technology": {
            "summary": "The source runs the technology, including a name pointing at a SaaS host.",
            "excludes": "Not a protective front such as a WAF or CDN (`protected_by`).",
            "properties": {
                "version": "The version this source runs, as the product reports it.",
                "cpe": "The versioned CPE 2.3 name for what this source runs.",
            },
        },
        "serves_endpoint": {
            "summary": "The service serves the endpoint.",
            "excludes": "Not a redirect target (`redirects_to`).",
            "properties": {},
        },
        "supports_tls_cipher": {
            "summary": "The service accepts the cipher suite.",
            "excludes": "Not the suite negotiated in one session.",
            "properties": {},
        },
    },
}

# Length caps per text, so every `kb_types` page keeps its response margin as the catalog grows.
CAPS = {
    "summary": 400,
    "excludes": 400,
    "notes": 600,
    "property": 200,
    "format": 600,
    "rule": 400,
    "common": 200,
}
