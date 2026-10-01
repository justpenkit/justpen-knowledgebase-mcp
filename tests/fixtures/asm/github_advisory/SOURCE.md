# github_advisory

- **Command:** `curl -s -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/advisories/<ghsa_id>` → `<ghsa_id>.json`
- **Version:** GitHub REST API 2022-11-28, unauthenticated
- **Schema:** [Get a global security advisory](https://docs.github.com/en/rest/security-advisories/global-advisories#get-a-global-security-advisory),
    the `global-advisory` object: `ghsa_id`, `cve_id` (null without a CVE), `identifiers[]`, prose, `references`,
    the publication, review and withdrawal times, `vulnerabilities[]` (affected package, version range, first patched
    version), `cvss_severities` (`cvss_v3` and `cvss_v4`), the deprecated `cvss`, `cwes[]`, `credits[]` and, for an
    advisory with a CVE, `epss`.

Recorded from the live API on 2026-10-01. `GHSA-f82v-jwr5-mffw` is a reviewed advisory with the CVE alias
`CVE-2025-29927`; `GHSA-c2m8-h5v5-343r` is a reviewed advisory with no CVE at capture time. The advisory ids, CVE id,
CVSS data, CWEs, times, references and affected packages are the published values.

The credited researchers are replaced by fixture values: each `credits[].user` login, id and `node_id` (and the URLs
built from them) is `example-reporter`, `example-analyst`, `example-reporter-a` or `example-reporter-b` with ids
1000001 to 1000004, and the two names in the `GHSA-f82v-jwr5-mffw` description's credits section are `Alice Example`
and `Bob Example`.

## Notes

- `ghsa_id` and `cve_id` are two advisories, and the GHSA `aliases` the CVE: the edge points toward the CVE.
    `identifiers[]` repeats both ids.
- `cvss_score` and `cvss_vector` come from the preferred family only, `cvss_v4` before `cvss_v3`
    (`github_preferred_cvss`). An absent family is a null vector with the placeholder score 0.0, which is never
    written. The GitHub score belongs to the GHSA record, so it is written on the GHSA, not on the CVE.
- `nvd_published_at` and `epss` describe the CVE as NVD and FIRST publish it; `nvd_cve` and `first_epss` feed those
    properties from the authoritative records, so these copies stay in the evidence.
- Every CWE becomes a `cwe` node with a `has_weakness` edge from the GHSA; `cwes[].name` stays in the evidence, as
    `cwe.name` is fed by the MITRE catalog.
- Descriptions, references, the affected product's repository and every `vulnerabilities[]` member are advisory
    applicability data, which the catalog keeps out of properties.
