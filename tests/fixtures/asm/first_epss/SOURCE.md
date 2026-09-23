# first_epss

- **Command:** `curl -s "https://api.first.org/data/v1/epss?cve=CVE-2021-44228,CVE-2025-24813"`
- **Version:** FIRST EPSS API v1 (`"version": "1.0"`)
- **Schema:** [EPSS API](https://api.first.org/epss/) response: `status`, `status-code`, `version`,
    `access`, `total`, `offset`, `limit` and `data[]` of `cve`, `epss`, `percentile` and `date`; `epss` and
    `percentile` are JSON strings with nine decimals and `date` is `YYYY-MM-DD` (research §14, live
    response `https://api.first.org/data/v1/epss?cve=CVE-2021-44228`).

Derived from the documented schema, not recorded from a live target. The scores follow the public EPSS
values for the model date shown.

## Notes

- `epss` and `percentile` are parsed from their string spelling into numbers (`float_value`).
- `date` is the EPSS model date the scores belong to, so it is the write's `observed_at` (midnight UTC).
