# naabu

- **Command:** `naabu -l hosts.txt -p 22,443 -json -cdn -o output.jsonl`
- **Version:** naabu v2, `dev` branch at commit `18264a7`
- **Schema:** [`jsonResult` and `Result.JSON`](https://github.com/projectdiscovery/naabu/blob/18264a784bb3e1b3680dd7ded09c3bf4b163fed9/pkg/runner/output.go);
    `protocol` values per [`pkg/protocol/protocol.go`](https://github.com/projectdiscovery/naabu/blob/18264a784bb3e1b3680dd7ded09c3bf4b163fed9/pkg/protocol/protocol.go#L15-L25).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

`host` is omitted when it equals `ip`. The flattened service fields (`name`, `product`, `cpes`, ...) and
`mac_address`, `mac_vendor` and `is_dead_host` need service discovery or a local segment, which the recorded
command does not use, so the mapping has no rows for them.
