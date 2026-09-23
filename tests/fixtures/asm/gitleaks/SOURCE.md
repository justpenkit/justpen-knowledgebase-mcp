# gitleaks

- **Command:** `gitleaks git --platform github --report-format json --report-path gitleaks.json .`
    (run inside a clone whose `origin` is `https://github.com/Example-Org/Web-App`)
- **Version:** gitleaks v8.30.1 (source read at `b58d3f1`)
- **Schema:** [`report.Finding`](https://github.com/gitleaks/gitleaks/blob/b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b/report/finding.go)
    (`Line` is `json:"-"`, `Link` and `Fragment` are `omitempty`), written as one indented JSON array by
    [`JsonReporter`](https://github.com/gitleaks/gitleaks/blob/b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b/report/json.go);
    field values per [`detectRule`](https://github.com/gitleaks/gitleaks/blob/b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b/detect/detect.go#L454-L540)
    (`Match` is the whole match trimmed of newlines, `Secret` the first capture group), `Fingerprint` per
    `AddFinding` in the same file, the GitHub `Link` per `createScmLink` in
    [`detect/utils.go`](https://github.com/gitleaks/gitleaks/blob/b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b/detect/utils.go#L23-L50),
    `Date` as `AuthorDate.UTC().Format(time.RFC3339)` per
    [`sources/git.go`](https://github.com/gitleaks/gitleaks/blob/b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b/sources/git.go);
    the `generic-api-key` rule per the default
    [`gitleaks.toml`](https://github.com/gitleaks/gitleaks/blob/b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b/config/gitleaks.toml#L638).

Derived from the documented schema, not recorded from a live target.

## Notes

- The secret is the AWS documentation example secret access key, the value trufflehog reports for the
    same key, so both scanners write one `secret` node. The default config would not report it: the
    `generic-api-key` rule's stopwords include `example`. The records show the field shapes a real hit
    has; they are not a reproducible detection.
- The digest is the SHA-256 of `Secret`, never `Match`, which adds the assignment text. `Match` and
    `Secret` are redacted before the report is ingested as evidence.
- The rule's `Tags` are empty, so the Go nil slice is emitted as `null`.
- The repository comes from `Link` (`https://github.com/<owner>/<name>/blob/...`), which gitleaks builds
    from the clone's remote; the location is `<File>:<StartLine>`.
