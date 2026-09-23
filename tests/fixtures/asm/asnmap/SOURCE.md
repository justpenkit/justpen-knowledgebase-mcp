# asnmap

- **Command:** `asnmap -i AS64496,198.51.100.7 -json -o output.jsonl`
- **Version:** asnmap, `main` branch at commit `cedef8b`
- **Schema:** [`Result` and `mapToResult`](https://github.com/projectdiscovery/asnmap/blob/cedef8b116fb2ea228108bfa2eee6ed74ae31ad9/libs/types.go#L16-L85):
    `as_number` is `AS`-prefixed by `attachPrefix`, and `timestamp` is Go's `time.Now().Local().String()`.

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges, and
the AS numbers are the RFC 5398 documentation range.
