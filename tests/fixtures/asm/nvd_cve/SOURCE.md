# nvd_cve

- **Commands:**
    - `CVE-2021-44228.json`: `curl -s "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2021-44228"`
    - `CVE-2025-24813.json`: `curl -s "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2025-24813"`
- **Version:** NVD CVE API 2.0 (schema "JSON Schema for NVD Vulnerability Data API version 2.2.4")
- **Schema:** [`cve_api_json_2.0.schema`](https://csrc.nist.gov/schema/nvd/api/2.0/cve_api_json_2.0.schema):
    top-level paging, `format`, `version`, `timestamp`, `vulnerabilities[].cve` with `id`, `sourceIdentifier`,
    `published` and `lastModified` (ISO-8601 without a zone), `vulnStatus`, `cveTags`, `descriptions`,
    `affected` (CVE 5 affected data), `metrics` (`cvssMetricV40`, `cvssMetricV31`, `cvssMetricV30`,
    `cvssMetricV2`, `ssvcV203`), `cisaExploitAdd` (`format: date`), `cisaActionDue`, `cisaRequiredAction`,
    `cisaVulnerabilityName`, `weaknesses`, `configurations` and `references`; the CVSS `cvssData` objects
    follow the FIRST [v3.1](https://www.first.org/cvss/cvss-v3.1.json) and
    [v4.0](https://www.first.org/cvss/cvss-v4.0.json) schemas.

Derived from the documented schema, not recorded from a live target. The values follow the public NVD
records for the two CVEs (research §14), with `configurations`, their `cpeMatch` lists and
`references` trimmed to keep the files small.

## Notes

- One CVE per response (`cveId=`): the preferred-metric rule reads the one `vulnerabilities[]` member.
- `cvss_score` and `cvss_vector` come from the preferred CVSS metric only: the first `Primary` entry
    across `cvssMetricV40`, `cvssMetricV31` and `cvssMetricV30`, else the first entry
    (`nvd_preferred_cvss`). CVE-2025-24813 carries the NVD's `Primary` 9.8
    (`CVSS:3.1/.../S:U/...`) and CISA-ADP's `Secondary` 10.0 (`.../S:C/...`); only the NVD's pair is
    written. CVE-2021-44228's two entries agree.
- No `cvssMetricV40` or `cvssMetricV30` rows are exercised: neither record carries one, and a live
    `cvssMetricV40` sample was not verified (research, unverified item 12). Their rows are
    `documented_only`.
- `cvssMetricV2` vectors carry no `CVSS:` prefix and the catalog's `cvss_vector` accepts 3.0, 3.1 and 4.0
    only, so v2 stays in the evidence, as do the SSVC decision points.
- Every CWE any weakness source assigns becomes a `cwe` node with a `has_weakness` edge; the
    classifications are claims, not scores, so they do not conflict. `cwe.name` is fed by the MITRE
    catalog, not by NVD.
- The response `timestamp` is when NVD produced the answer, so it is the write's `observed_at`.
