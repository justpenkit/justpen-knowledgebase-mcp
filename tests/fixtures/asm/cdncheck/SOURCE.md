# cdncheck

- **Command:** `cdncheck -i targets.txt -jsonl -resp -o output.jsonl`
- **Version:** cdncheck v1.3.1 (commit `a06260a`)
- **Schema:** [`Output`](https://github.com/projectdiscovery/cdncheck/blob/a06260a272dc92cec0747f2f369b697088899bc7/internal/runner/options.go#L18-L29);
    `itemType` is unexported and never emitted ([cdncheck.go](https://github.com/projectdiscovery/cdncheck/blob/a06260a272dc92cec0747f2f369b697088899bc7/cdncheck.go#L110)).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

An IP `input` is classified by provider range lists, so its provider is an `ip_address` attribute
(`cdn_provider`, `waf_provider`, `cloud_provider`). A name `input` is classified as a name, so the provider is
a `technology` reached through `protected_by` from the name (the plan's CDN split), and the address cdncheck
resolved it to gets no attribute.
