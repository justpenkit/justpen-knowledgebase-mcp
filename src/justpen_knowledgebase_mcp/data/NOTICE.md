# Bundled data notices

## public_suffix_list_icann.txt

ICANN-section extract of the Public Suffix List, https://publicsuffix.org/.
Version, source and snapshot SHA-256 digests are recorded in
`public_suffix_list_icann.json`. The list is licensed under the Mozilla Public
License 2.0, https://mozilla.org/MPL/2.0/. Only the ICANN section is bundled;
the PRIVATE section is intentionally excluded.

## service_names.json

`service.name` whitelist derived from the service-detection names in Nmap's
`nmap-service-probes` file (repository, commit and SHA-256 recorded in the
file's `upstream` block). Nmap is distributed under the Nmap Public Source
License, https://nmap.org/npsl/. Only the service names are reused; no probe,
match expression or detection logic is included.
